"""Shared active-stock set for realtime transaction fetchers.

The Go fetcher reads ``active_symbols.txt`` with ``--active-symbols-file``.
Pages such as positions and stock analysis register symbols here so newly
focused stocks are refreshed by the realtime Redis transaction loop.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

_SYMBOL_RE = re.compile(r"^\d{6}$")


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
    except Exception as exc:  # noqa: BLE001
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
        out.append({
            "symbol": symbol,
            "name": str(row.get("name") or ""),
            "source": str(row.get("source") or "manual"),
            "updated_at": str(row.get("updated_at") or ""),
        })
    return out


def _write_rows(rows: list[dict]) -> None:
    rows = [row for row in rows if normalize_symbol(str(row.get("symbol") or ""))]
    _json_path().write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    active_symbols_path().write_text("\n".join(row["symbol"] for row in rows) + ("\n" if rows else ""), encoding="utf-8")


def list_symbols() -> list[dict]:
    rows = _load_rows()
    if not active_symbols_path().exists():
        _write_rows(rows)
    return rows


def add(symbol: str, name: str = "", source: str = "manual") -> list[dict]:
    normalized = normalize_symbol(symbol)
    if not normalized:
        return list_symbols()
    rows = _load_rows()
    rows = [row for row in rows if row.get("symbol") != normalized]
    rows.insert(0, {
        "symbol": normalized,
        "name": name,
        "source": source or "manual",
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    _write_rows(rows)
    return rows


def add_many(symbols: list[str], source: str = "manual") -> list[dict]:
    rows = _load_rows()
    existing = {row["symbol"]: row for row in rows}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ordered: list[dict] = []
    ordered_seen: set[str] = set()
    for symbol in symbols:
        normalized = normalize_symbol(symbol)
        if not normalized or normalized in ordered_seen:
            continue
        ordered_seen.add(normalized)
        current = existing.get(normalized, {})
        ordered.append({
            "symbol": normalized,
            "name": str(current.get("name") or ""),
            "source": source or str(current.get("source") or "manual"),
            "updated_at": now,
        })
    seen = {row["symbol"] for row in ordered}
    ordered.extend(row for row in rows if row["symbol"] not in seen)
    _write_rows(ordered)
    return ordered


def remove(symbol: str) -> list[dict]:
    normalized = normalize_symbol(symbol)
    rows = [row for row in _load_rows() if row.get("symbol") != normalized]
    _write_rows(rows)
    return rows
