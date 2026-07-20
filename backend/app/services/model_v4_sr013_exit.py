# ruff: noqa: RUF001
"""Realtime SR013 ACT5 sell evaluation for current T+1 positions.

The service is self-contained inside the panel. It reads current transactions
from Redis, uses the official T-day close as the common strategy and displayed
return basis, and never persists transaction parquet.
"""
from __future__ import annotations

import json
import logging
import math
import re
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import polars as pl
import redis

from app.config import settings
from app.services import active_stocks, watchlist

logger = logging.getLogger(__name__)

TZ_SHANGHAI = timezone(timedelta(hours=8))
TX_ROOT = Path("/home/dennis/historical_transaction")
RULE_NAME = "V4_SR013_ACT5_TCLOSE_GB2_CATA1100_FIRST1445"
RULE_FAMILY = "sr013_act5_tclose_profit_trail_plus_catastrophe_guard"
RULE_DESCRIPTION = (
    "盈利保护：10:00起，以T日收盘价为基准，盘中最高涨幅达到5%后，"
    "若快照涨幅较峰值回落至少2个百分点，则按下一可见分钟首笔成交卖出。"
    "风险保护：11:00起，若最高涨幅低于3%、当前跌幅达到4%且价格不高于当时VWAP，"
    "则按下一可见分钟首笔成交卖出。以上均未触发时，14:45首笔成交卖出。"
)
SNAPSHOT_TIMES = (945, 1000, 1030, 1100, 1300, 1330, 1400, 1430)
TRAIL_START_HHMM = 1000
TRAIL_ACTIVATION_RETURN = 0.05
TRAIL_GIVEBACK = 0.02
CATASTROPHE_START_HHMM = 1100
CATASTROPHE_RETURN = -0.04
CATASTROPHE_MAX_MFE = 0.03
CATASTROPHE_MAX_PRICE_VS_VWAP = 0.0
FALLBACK_SIGNAL_HHMM = 1444
FALLBACK_FILL_HHMM = 1445
FEE_RATE_PER_SIDE = 0.0005
MIN_HHMM = 925
MAX_HHMM = 1500


def _norm_symbol(value: Any) -> str:
    raw = str(value or "").strip().split(".", 1)[0]
    digits = re.sub(r"\D", "", raw)
    return digits.zfill(6)[-6:] if digits else ""


def _norm_date(value: Any) -> str:
    raw = str(value or "").strip().replace("-", "").replace("/", "")
    return re.sub(r"\D", "", raw)[:8]


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _format_hhmmss(value: Any) -> str:
    try:
        raw = int(value)
    except (TypeError, ValueError):
        return ""
    return f"{raw // 10000:02d}:{(raw // 100) % 100:02d}:{raw % 100:02d}"


def _tx_path(trade_date: str) -> Path:
    return TX_ROOT / trade_date[:4] / trade_date[4:6] / f"{trade_date[6:8]}.parquet"


@lru_cache(maxsize=1)
def _transaction_dates() -> tuple[str, ...]:
    if not TX_ROOT.exists():
        return ()
    dates: set[str] = set()
    for path in TX_ROOT.glob("*/*/*.parquet"):
        if path.name.endswith("-ac.parquet") or ".manifest" in path.name:
            continue
        year, month, day = path.parent.parent.name, path.parent.name, path.stem
        if re.fullmatch(r"\d{4}", year) and re.fullmatch(r"\d{2}", month) and re.fullmatch(r"\d{2}", day):
            dates.add(f"{year}{month}{day}")
    return tuple(sorted(dates))


def _previous_date(trade_date: str) -> str:
    prior = [date for date in _transaction_dates() if date < trade_date]
    return prior[-1] if prior else ""


@lru_cache(maxsize=64)
def _load_historical_stock_day(trade_date: str, symbol: str) -> pd.DataFrame:
    path = _tx_path(trade_date)
    if not path.exists():
        return pd.DataFrame()
    try:
        available = set(pl.read_parquet_schema(path).keys())
        code_col = "stock_code" if "stock_code" in available else "symbol" if "symbol" in available else ""
        if not code_col or "time_hhmm" not in available or "price" not in available:
            return pd.DataFrame()
        wanted = [
            code_col,
            "time_hhmm",
            "time_hhmmss",
            "price",
            "vol",
            "transaction_time",
            "chrono_row_in_symbol",
        ]
        wanted = [column for column in wanted if column in available]
        normalized_code = pl.col(code_col).cast(pl.Utf8).str.extract(r"(\d+)", 1).str.zfill(6)
        frame = (
            pl.scan_parquet(path)
            .select(wanted)
            .with_columns(normalized_code.alias("_normalized_code"))
            .filter(pl.col("_normalized_code") == symbol)
            .drop("_normalized_code")
            .collect()
        )
        if frame.is_empty():
            return pd.DataFrame()
        if code_col != "stock_code":
            frame = frame.rename({code_col: "stock_code"})
        out = frame.to_pandas()
    except Exception as exc:
        logger.warning("SR013 historical transaction read failed for %s/%s: %s", trade_date, symbol, exc)
        return pd.DataFrame()

    out["stock_code"] = out["stock_code"].map(_norm_symbol)
    out["time_hhmm"] = pd.to_numeric(out["time_hhmm"], errors="coerce")
    if "time_hhmmss" not in out.columns:
        out["time_hhmmss"] = out["time_hhmm"] * 100
    out["time_hhmmss"] = pd.to_numeric(out["time_hhmmss"], errors="coerce")
    out["price"] = pd.to_numeric(out["price"], errors="coerce")
    if "vol" in out.columns:
        out["vol"] = pd.to_numeric(out["vol"], errors="coerce").fillna(0.0)
    else:
        out["vol"] = 0.0
    out["source_row_order"] = np.arange(len(out), dtype=np.int64)
    if "chrono_row_in_symbol" in out.columns:
        out["chrono_row_in_symbol"] = pd.to_numeric(
            out["chrono_row_in_symbol"], errors="coerce"
        ).fillna(out["source_row_order"])
    else:
        out["chrono_row_in_symbol"] = out["source_row_order"]
    out = out[
        out["time_hhmm"].notna()
        & out["time_hhmmss"].notna()
        & out["price"].gt(0)
        & out["time_hhmm"].between(MIN_HHMM, MAX_HHMM)
    ].copy()
    return out.sort_values(
        ["time_hhmm", "time_hhmmss", "chrono_row_in_symbol"], kind="mergesort"
    ).reset_index(drop=True)


def _redis_client() -> redis.Redis:
    host, _, port_text = str(settings.tdx_redis_addr or "localhost:6379").partition(":")
    return redis.Redis(
        host=host or "localhost",
        port=int(port_text or 6379),
        db=int(settings.tdx_redis_db),
        password=settings.tdx_redis_password or None,
        decode_responses=True,
        socket_timeout=3.0,
        socket_connect_timeout=3.0,
    )


def _redis_rows(client: redis.Redis, trade_date: str, symbol: str) -> pd.DataFrame:
    prefix = settings.tdx_redis_key_prefix or "tdx:trans"
    dated_key = f"{prefix}:{trade_date}:{symbol}"
    plain_key = f"{prefix}:{symbol}"
    try:
        key = dated_key if client.exists(dated_key) else plain_key
        raw = client.get(key)
    except Exception as exc:
        logger.warning("SR013 Redis read failed for %s: %s", symbol, exc)
        return pd.DataFrame()
    if not raw:
        return pd.DataFrame()

    rows: list[dict[str, Any]] = []
    for source_row_order, line in enumerate(str(raw).splitlines()):
        try:
            obj = json.loads(line)
            timestamp = float(obj.get("timestamp"))
            dt = datetime.fromtimestamp(timestamp, tz=TZ_SHANGHAI)
            if dt.strftime("%Y%m%d") != trade_date:
                continue
            price = _finite(obj.get("price"))
            volume = _finite(obj.get("vol")) or 0.0
            if price is None or price <= 0:
                continue
            rows.append(
                {
                    "stock_code": _norm_symbol(obj.get("symbol") or symbol),
                    "time_hhmm": dt.hour * 100 + dt.minute,
                    "time_hhmmss": dt.hour * 10000 + dt.minute * 100 + dt.second,
                    "price": price,
                    "vol": max(volume, 0.0),
                    "transaction_time": timestamp,
                    "source_row_order": source_row_order,
                    "chrono_row_in_symbol": source_row_order,
                }
            )
        except (TypeError, ValueError, json.JSONDecodeError, OSError):
            continue
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    frame = frame[frame["time_hhmm"].between(MIN_HHMM, MAX_HHMM)].copy()
    frame = frame.drop_duplicates(
        subset=["transaction_time", "time_hhmmss", "price", "vol"], keep="first"
    )
    return frame.sort_values(
        ["time_hhmm", "time_hhmmss", "source_row_order"], kind="mergesort"
    ).reset_index(drop=True)


def _quote_map(request: Any, symbols: list[str]) -> dict[str, dict[str, Any]]:
    if not symbols:
        return {}
    try:
        rows = watchlist.fetch_quotes(symbols, request.app.state.capabilities)
    except Exception as exc:
        logger.warning("SR013 batch quote fetch failed: %s", exc)
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = _norm_symbol(row.get("symbol"))
        if symbol:
            out[symbol] = row
    return out


def _t_close_reference(
    *, trade_date: str, symbol: str, quote: dict[str, Any]
) -> tuple[float | None, str, str, dict[str, Any]]:
    entry_date = _previous_date(trade_date)
    today = datetime.now(TZ_SHANGHAI).strftime("%Y%m%d")
    if trade_date == today:
        for key in ("prev_close", "pre_close", "previous_close", "last_close"):
            value = _finite(quote.get(key))
            if value is not None and value > 0:
                return value, f"tickflow_quote.{key}", entry_date, {"price_field": key}

    if not entry_date:
        return None, "", "", {}
    day = _load_historical_stock_day(entry_date, symbol)
    regular = day[day["time_hhmm"].between(930, 1500)] if not day.empty else day
    if regular.empty:
        return None, "", entry_date, {}
    point = regular.iloc[-1]
    value = _finite(point["price"])
    if value is None or value <= 0:
        return None, "", entry_date, {}
    return (
        value,
        "historical_transaction_last_fill_le_1500",
        entry_date,
        {
            "price_time_hhmm": int(point["time_hhmm"]),
            "price_time_hhmmss": int(point["time_hhmmss"]),
            "transaction_path": str(_tx_path(entry_date)),
        },
    )


def _clock_context(trade_date: str) -> tuple[int, int]:
    """Return current HHMM and the latest causally completed clock minute."""
    now = datetime.now(TZ_SHANGHAI)
    today = now.strftime("%Y%m%d")
    if trade_date < today:
        return 1501, 1500
    if trade_date > today:
        return 0, 0
    current_hhmm = now.hour * 100 + now.minute
    minute_id = now.hour * 60 + now.minute
    if current_hhmm <= 925:
        return current_hhmm, 0
    if current_hhmm <= 1130:
        completed_id = minute_id - 1
        return current_hhmm, (completed_id // 60) * 100 + completed_id % 60
    if current_hhmm < 1301:
        return current_hhmm, 1130
    if current_hhmm <= 1500:
        completed_id = minute_id - 1
        return current_hhmm, (completed_id // 60) * 100 + completed_id % 60
    return current_hhmm, 1500


def _normalize_path(day: pd.DataFrame, reference_price: float) -> pd.DataFrame:
    if day.empty:
        return pd.DataFrame()
    out = day.copy()
    out["time_hhmm"] = pd.to_numeric(out["time_hhmm"], errors="coerce")
    out["time_hhmmss"] = pd.to_numeric(out["time_hhmmss"], errors="coerce")
    out["price"] = pd.to_numeric(out["price"], errors="coerce")
    if "vol" in out.columns:
        out["vol"] = pd.to_numeric(out["vol"], errors="coerce").fillna(0.0).clip(lower=0.0)
    else:
        out["vol"] = 0.0
    if "chrono_row_in_symbol" not in out.columns:
        out["chrono_row_in_symbol"] = np.arange(len(out), dtype=np.int64)
    out["chrono_row_in_symbol"] = pd.to_numeric(
        out["chrono_row_in_symbol"], errors="coerce"
    ).fillna(pd.Series(np.arange(len(out), dtype=np.int64), index=out.index))
    out = out[
        out["time_hhmm"].notna()
        & out["time_hhmmss"].notna()
        & out["price"].gt(0)
        & out["time_hhmm"].between(MIN_HHMM, MAX_HHMM)
    ].copy()
    out = out.sort_values(
        ["time_hhmm", "time_hhmmss", "chrono_row_in_symbol"], kind="mergesort"
    ).reset_index(drop=True)
    out["gross_return"] = out["price"] / float(reference_price) - 1.0
    out["price_volume"] = out["price"] * out["vol"]
    return out


def _snapshot_state(path: pd.DataFrame, snapshot_hhmm: int) -> dict[str, Any] | None:
    upto = path[path["time_hhmm"].le(int(snapshot_hhmm))]
    if upto.empty:
        return None
    point = upto.iloc[-1]
    total_volume = float(upto["vol"].sum())
    vwap = (
        float(upto["price_volume"].sum()) / total_volume
        if total_volume > 0
        else float(upto["price"].mean())
    )
    current_price = float(point["price"])
    current_return = float(point["gross_return"])
    return {
        "snapshot_time_hhmm": int(snapshot_hhmm),
        "signal_time_hhmmss": int(point["time_hhmmss"]),
        "signal_price": current_price,
        "signal_gross_return": current_return,
        "observed_mfe": float(upto["gross_return"].max()),
        "observed_mae": float(upto["gross_return"].min()),
        "asof_vwap": vwap,
        "price_vs_asof_vwap": current_price / vwap - 1.0 if vwap > 0 else np.nan,
        "path_rows_asof": int(len(upto)),
    }


def _first_fill_after_snapshot(path: pd.DataFrame, signal_hhmm: int) -> pd.Series | None:
    eligible = path[
        path["time_hhmm"].gt(int(signal_hhmm))
        & path["time_hhmm"].le(FALLBACK_FILL_HHMM)
    ]
    return eligible.iloc[0] if not eligible.empty else None


def _first_fill_at_1445(path: pd.DataFrame) -> pd.Series | None:
    eligible = path[path["time_hhmm"].eq(FALLBACK_FILL_HHMM)]
    return eligible.iloc[0] if not eligible.empty else None


def _evaluate_rule(
    day: pd.DataFrame,
    *,
    reference_price: float,
    current_hhmm: int,
    completed_through_hhmm: int,
) -> dict[str, Any]:
    path = _normalize_path(day, reference_price)
    if path.empty:
        return {"status": "waiting_for_transaction_data", "snapshot_states": []}

    states: list[dict[str, Any]] = []
    chosen: dict[str, Any] | None = None
    chosen_reason = ""
    for snapshot_hhmm in SNAPSHOT_TIMES:
        if snapshot_hhmm > completed_through_hhmm:
            continue
        state = _snapshot_state(path, snapshot_hhmm)
        if state is None:
            continue
        giveback = state["observed_mfe"] - state["signal_gross_return"]
        trail_fire = (
            snapshot_hhmm >= TRAIL_START_HHMM
            and state["observed_mfe"] >= TRAIL_ACTIVATION_RETURN
            and giveback >= TRAIL_GIVEBACK
        )
        catastrophe_fire = (
            snapshot_hhmm >= CATASTROPHE_START_HHMM
            and state["observed_mfe"] < CATASTROPHE_MAX_MFE
            and state["signal_gross_return"] <= CATASTROPHE_RETURN
            and state["price_vs_asof_vwap"] <= CATASTROPHE_MAX_PRICE_VS_VWAP
        )
        state.update(
            {
                "giveback_from_mfe": giveback,
                "trail_fire": int(trail_fire),
                "catastrophe_fire": int(catastrophe_fire),
            }
        )
        states.append(state)
        if trail_fire or catastrophe_fire:
            chosen = state
            chosen_reason = "sr013_act5_profit_trail" if trail_fire else "confirmed_catastrophe_guard"
            break

    if chosen is not None:
        signal_hhmm = int(chosen["snapshot_time_hhmm"])
        signal_hhmmss = int(chosen["signal_time_hhmmss"])
        fill = _first_fill_after_snapshot(path, signal_hhmm)
    elif current_hhmm >= FALLBACK_FILL_HHMM:
        signal_hhmm = FALLBACK_SIGNAL_HHMM
        upto = path[path["time_hhmm"].le(FALLBACK_SIGNAL_HHMM)]
        signal_hhmmss = int(upto.iloc[-1]["time_hhmmss"]) if not upto.empty else FALLBACK_SIGNAL_HHMM * 100
        chosen_reason = "forced_first_fill_1445"
        fill = _first_fill_at_1445(path)
    else:
        latest_state = states[-1] if states else None
        return {
            "status": "holding",
            "snapshot_states": states,
            "latest_snapshot": latest_state,
            "observed_mfe": latest_state.get("observed_mfe") if latest_state else None,
            "giveback_from_mfe": latest_state.get("giveback_from_mfe") if latest_state else None,
        }

    reason_labels = {
        "sr013_act5_profit_trail": "SR013 ACT5盈利回撤",
        "confirmed_catastrophe_guard": "SR013灾难保护",
        "forced_first_fill_1445": "SR013 14:45兜底",
    }
    event = {
        "event": chosen_reason,
        "signal_time_hhmm": signal_hhmm,
        "signal_time_hhmmss": signal_hhmmss,
        "signal_time": _format_hhmmss(signal_hhmmss),
        "sell_reason_label": reason_labels[chosen_reason],
    }
    if chosen is not None:
        event.update(
            {
                "signal_price": chosen["signal_price"],
                "signal_gross_return": chosen["signal_gross_return"],
                "observed_mfe": chosen["observed_mfe"],
                "giveback_from_mfe": chosen["giveback_from_mfe"],
                "price_vs_asof_vwap": chosen["price_vs_asof_vwap"],
            }
        )
    if fill is None:
        return {
            "status": "sell_triggered_fill_pending",
            "sell_reason": chosen_reason,
            "sell_reason_label": reason_labels[chosen_reason],
            "signal_time_hhmm": signal_hhmm,
            "signal_time_hhmmss": signal_hhmmss,
            "signal_time": _format_hhmmss(signal_hhmmss),
            "sell_time": "",
            "sell_price": None,
            "event": event,
            "snapshot_states": states,
        }

    sell_price = float(fill["price"])
    sell_hhmmss = int(fill["time_hhmmss"])
    event.update(
        {
            "sell_time_hhmm": int(fill["time_hhmm"]),
            "sell_time_hhmmss": sell_hhmmss,
            "sell_time": _format_hhmmss(sell_hhmmss),
            "sell_price": sell_price,
        }
    )
    return {
        "status": "sell_triggered",
        "sell_reason": chosen_reason,
        "sell_reason_label": reason_labels[chosen_reason],
        "signal_time_hhmm": signal_hhmm,
        "signal_time_hhmmss": signal_hhmmss,
        "signal_time": _format_hhmmss(signal_hhmmss),
        "sell_time_hhmm": int(fill["time_hhmm"]),
        "sell_time_hhmmss": sell_hhmmss,
        "sell_time": _format_hhmmss(sell_hhmmss),
        "sell_price": sell_price,
        "strategy_gross_return": sell_price / reference_price - 1.0,
        "strategy_net_return": sell_price * (1.0 - FEE_RATE_PER_SIDE)
        / (reference_price * (1.0 + FEE_RATE_PER_SIDE))
        - 1.0,
        "event": event,
        "snapshot_states": states,
    }


def _evaluate_one(
    *,
    symbol: str,
    name: str,
    trade_date: str,
    current: pd.DataFrame,
    current_source: str,
    quote: dict[str, Any],
    current_hhmm: int,
    completed_through_hhmm: int,
) -> dict[str, Any]:
    latest_price = _finite(quote.get("price")) or _finite(quote.get("last_price"))
    if latest_price is None and not current.empty:
        latest_price = _finite(current.iloc[-1]["price"])
    reference_price, reference_source, entry_date, reference_meta = _t_close_reference(
        trade_date=trade_date, symbol=symbol, quote=quote
    )
    common = {
        "stock_code": symbol,
        "stock_name": name,
        "sell_rule": RULE_NAME,
        "entry_date": entry_date,
        "t_close_price": reference_price,
        "t_close_price_source": reference_source,
        "t_close_price_meta": reference_meta,
        "buy_price": reference_price,
        "buy_price_source": reference_source,
        "latest_price": latest_price,
        "last_transaction_time": _format_hhmmss(current.iloc[-1]["time_hhmmss"])
        if not current.empty
        else "",
        "data_source": f"tickflow_batch_quote+{current_source}",
        "current_hhmm": current_hhmm,
        "completed_through_hhmm": completed_through_hhmm,
        "parameters": {
            "reference": "T_close",
            "snapshot_times": list(SNAPSHOT_TIMES),
            "trail_activation_return": TRAIL_ACTIVATION_RETURN,
            "trail_giveback": TRAIL_GIVEBACK,
            "catastrophe_start_hhmm": CATASTROPHE_START_HHMM,
            "catastrophe_return": CATASTROPHE_RETURN,
            "catastrophe_max_mfe": CATASTROPHE_MAX_MFE,
            "fallback_fill_hhmm": FALLBACK_FILL_HHMM,
        },
    }
    if reference_price is None or reference_price <= 0:
        return {
            **common,
            "status": "waiting_for_reference_price",
            "signal_time": "",
            "sell_time": "",
            "sell_price": None,
            "gross_return": None,
            "actual_return": None,
            "event": None,
        }

    rule = _evaluate_rule(
        current,
        reference_price=reference_price,
        current_hhmm=current_hhmm,
        completed_through_hhmm=completed_through_hhmm,
    )
    sell_price = _finite(rule.get("sell_price"))
    mark_price = sell_price if sell_price is not None else latest_price
    displayed_return = mark_price / reference_price - 1.0 if mark_price is not None else None
    return {
        **common,
        **{key: value for key, value in rule.items() if key != "snapshot_states"},
        "gross_return": displayed_return,
        "actual_return": displayed_return,
        "position_gross_return": displayed_return,
        "latest_position_return": latest_price / reference_price - 1.0
        if latest_price is not None
        else None,
        "snapshot_count": len(rule.get("snapshot_states", [])),
    }


def _row_sort_key(row: dict[str, Any]) -> tuple[int, str, str]:
    sell_time = str(row.get("sell_time") or "")
    if sell_time:
        return 0, sell_time, str(row.get("stock_code") or "")
    if str(row.get("status") or "") == "sell_triggered_fill_pending":
        return 1, str(row.get("signal_time") or "99:99:99"), str(row.get("stock_code") or "")
    return 2, str(row.get("stock_code") or ""), ""


def evaluate_positions(request: Any, trade_date: str | None = None) -> dict[str, Any]:
    """Return one-minute refreshed SR013 status for all ``source=positions`` rows."""
    resolved_date = _norm_date(trade_date) or datetime.now(TZ_SHANGHAI).strftime("%Y%m%d")
    active_rows = [
        row
        for row in active_stocks.list_symbols()
        if str(row.get("source") or "") == "positions"
    ]
    symbols = list(
        dict.fromkeys(
            symbol for symbol in (_norm_symbol(row.get("symbol")) for row in active_rows) if symbol
        )
    )
    name_map = {
        _norm_symbol(row.get("symbol")): str(row.get("name") or "") for row in active_rows
    }
    missing_names = [symbol for symbol in symbols if not name_map.get(symbol)]
    if missing_names:
        try:
            repo_names = request.app.state.repo.get_name_map()
            normalized_names = {
                _norm_symbol(key): str(value or "")
                for key, value in repo_names.items()
                if _norm_symbol(key) and value
            }
            name_map.update(
                {
                    symbol: normalized_names[symbol]
                    for symbol in missing_names
                    if normalized_names.get(symbol)
                }
            )
        except Exception:
            pass

    today = datetime.now(TZ_SHANGHAI).strftime("%Y%m%d")
    quotes = _quote_map(request, symbols) if resolved_date == today else {}
    for symbol, quote in quotes.items():
        if not name_map.get(symbol):
            name_map[symbol] = str(quote.get("name") or "")

    current_hhmm, completed_through_hhmm = _clock_context(resolved_date)
    client = _redis_client()
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for symbol in symbols:
        try:
            if resolved_date < today:
                current = _load_historical_stock_day(resolved_date, symbol)
                current_source = "historical_transaction_exact_order"
            else:
                current = _redis_rows(client, resolved_date, symbol)
                current_source = "redis_transaction_in_memory"
            rows.append(
                _evaluate_one(
                    symbol=symbol,
                    name=name_map.get(symbol, ""),
                    trade_date=resolved_date,
                    current=current,
                    current_source=current_source,
                    quote=quotes.get(symbol, {}),
                    current_hhmm=current_hhmm,
                    completed_through_hhmm=completed_through_hhmm,
                )
            )
        except Exception as exc:
            logger.exception("SR013 evaluation failed for %s", symbol)
            errors.append(f"{symbol}: {exc}")

    rows.sort(key=_row_sort_key)

    return {
        "available": True,
        "trade_date": resolved_date,
        "checked_at": datetime.now(TZ_SHANGHAI).isoformat(timespec="seconds"),
        "refresh_interval_seconds": 60,
        "rule_name": RULE_NAME,
        "rule_family": RULE_FAMILY,
        "rule_description": RULE_DESCRIPTION,
        "rule_contract": (
            "T-close reference; completed snapshots; ACT5 MFE>=5%; giveback>=2pp; "
            "catastrophe guard from 11:00; first later transaction; 14:45 first-fill fallback"
        ),
        "symbols_source": "data/user_data/active_stocks.json where source=positions",
        "quote_request": "one TickFlow quote batch for all position symbols per refresh",
        "transaction_persisted": False,
        "rows": rows,
        "count": len(rows),
        "errors": errors,
    }
