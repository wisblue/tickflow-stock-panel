"""Shared active-stock set for realtime transaction fetchers.

The Go fetcher reads ``active_symbols.txt`` with ``--active-symbols-file``.
Pages such as positions and stock analysis register symbols here so newly
focused stocks are refreshed by the realtime Redis transaction loop.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import UTC, datetime
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

_SYMBOL_RE = re.compile(r"^\d{6}$")
_LOCK = threading.RLock()


def _normalize_sources(row: dict, fallback: str = "manual") -> list[str]:
    values = row.get("sources")
    raw = values if isinstance(values, list) else []
    primary = str(row.get("source") or "").strip()
    if primary:
        raw = [primary, *raw]
    sources: list[str] = []
    for value in raw:
        source = str(value or "").strip()
        if source and source not in sources:
            sources.append(source)
    return sources or [fallback]


def _primary_source(sources: list[str], preferred: str = "") -> str:
    if "positions" in sources:
        return "positions"
    if preferred in sources:
        return preferred
    return sources[0] if sources else "manual"


def has_source(row: dict, source: str) -> bool:
    return str(source or "").strip() in _normalize_sources(row)


def _dir() -> Path:
    p = settings.data_dir / "user_data"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _json_path() -> Path:
    return _dir() / "active_stocks.json"


def active_symbols_path() -> Path:
    return _dir() / "active_symbols.txt"


def normalize_symbol(symbol: str) -> str:
    value = str(symbol or "").strip().upper()
    digits = re.sub(r"\D", "", value)
    if len(digits) >= 6:
        value = digits[-6:]
    return value if _SYMBOL_RE.match(value) else ""


def _load_rows() -> list[dict]:
    p = _json_path()
    if not p.exists():
        return []
    try:
        rows = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("active_stocks.json malformed: %s", exc)
        return []
    if not isinstance(rows, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = normalize_symbol(str(row.get("symbol") or ""))
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        sources = _normalize_sources(row)
        primary = _primary_source(sources, str(row.get("source") or ""))
        out.append({
            "symbol": symbol,
            "name": str(row.get("name") or ""),
            "source": primary,
            "sources": sources,
            "updated_at": str(row.get("updated_at") or ""),
        })
    return out


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_name(
        f".{path.name}.tmp.{os.getpid()}.{threading.get_ident()}"
    )
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _write_rows(rows: list[dict]) -> None:
    normalized_rows: list[dict] = []
    for row in rows:
        symbol = normalize_symbol(str(row.get("symbol") or ""))
        if not symbol:
            continue
        sources = _normalize_sources(row)
        normalized_rows.append({
            **row,
            "symbol": symbol,
            "source": _primary_source(sources, str(row.get("source") or "")),
            "sources": sources,
        })
    rows = normalized_rows
    _atomic_write_text(
        _json_path(), json.dumps(rows, ensure_ascii=False, indent=2)
    )
    existing_symbols: list[str] = []
    p = active_symbols_path()
    if p.exists():
        try:
            for raw in re.split(r"[\s,]+", p.read_text(encoding="utf-8", errors="ignore")):
                symbol = normalize_symbol(raw)
                if symbol:
                    existing_symbols.append(symbol)
        except Exception as exc:
            logger.warning("active_symbols.txt read failed before merge: %s", exc)

    merged: list[str] = []
    seen: set[str] = set()
    for symbol in [*existing_symbols, *(row["symbol"] for row in rows)]:
        if symbol in seen:
            continue
        seen.add(symbol)
        merged.append(symbol)
    _atomic_write_text(p, "\n".join(merged) + ("\n" if merged else ""))


def list_symbols() -> list[dict]:
    with _LOCK:
        rows = _load_rows()
        if not active_symbols_path().exists():
            _write_rows(rows)
        return rows


def add(symbol: str, name: str = "", source: str = "manual") -> list[dict]:
    with _LOCK:
        normalized = normalize_symbol(symbol)
        if not normalized:
            return list_symbols()
        rows = _load_rows()
        current = next((row for row in rows if row.get("symbol") == normalized), {})
        requested_source = str(source or "manual").strip() or "manual"
        sources = _normalize_sources(current) if current else []
        if requested_source not in sources:
            sources.append(requested_source)
        rows = [row for row in rows if row.get("symbol") != normalized]
        rows.insert(0, {
            "symbol": normalized,
            "name": name or str(current.get("name") or ""),
            "source": _primary_source(sources, requested_source),
            "sources": sources,
            "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        })
        _write_rows(rows)
        return rows


def add_many(symbols: list[str], source: str = "manual") -> list[dict]:
    with _LOCK:
        rows = _load_rows()
        existing = {row["symbol"]: row for row in rows}
        now = datetime.now(UTC).isoformat(timespec="seconds")
        requested_source = str(source or "manual").strip() or "manual"
        ordered: list[dict] = []
        ordered_seen: set[str] = set()
        for symbol in symbols:
            normalized = normalize_symbol(symbol)
            if not normalized or normalized in ordered_seen:
                continue
            ordered_seen.add(normalized)
            current = existing.get(normalized, {})
            sources = _normalize_sources(current) if current else []
            if requested_source not in sources:
                sources.append(requested_source)
            ordered.append({
                "symbol": normalized,
                "name": str(current.get("name") or ""),
                "source": _primary_source(sources, requested_source),
                "sources": sources,
                "updated_at": now,
            })
        seen = {row["symbol"] for row in ordered}
        ordered.extend(row for row in rows if row["symbol"] not in seen)
        _write_rows(ordered)
        return ordered


def sync_source(symbols: list[str], source: str) -> list[dict]:
    with _LOCK:
        requested_source = str(source or "manual").strip() or "manual"
        targets = list(
            dict.fromkeys(
                normalized
                for normalized in (normalize_symbol(symbol) for symbol in symbols)
                if normalized
            )
        )
        target_set = set(targets)
        existing = {row["symbol"]: row for row in _load_rows()}
        now = datetime.now(UTC).isoformat(timespec="seconds")
        synced: list[dict] = []
        for symbol, row in existing.items():
            sources = _normalize_sources(row)
            if symbol in target_set:
                if requested_source not in sources:
                    sources.append(requested_source)
            else:
                sources = [item for item in sources if item != requested_source]
            if not sources:
                continue
            synced.append({
                **row,
                "source": _primary_source(sources, str(row.get("source") or "")),
                "sources": sources,
                "updated_at": now if symbol in target_set else row.get("updated_at", ""),
            })
        present = {row["symbol"] for row in synced}
        for symbol in targets:
            if symbol in present:
                continue
            synced.append({
                "symbol": symbol,
                "name": "",
                "source": requested_source,
                "sources": [requested_source],
                "updated_at": now,
            })
        order = {symbol: index for index, symbol in enumerate(targets)}
        synced.sort(key=lambda row: (order.get(row["symbol"], len(order)), row["symbol"]))
        _write_rows(synced)
        return synced


def remove(symbol: str) -> list[dict]:
    with _LOCK:
        normalized = normalize_symbol(symbol)
        rows = [row for row in _load_rows() if row.get("symbol") != normalized]
        _write_rows(rows)
        return rows
