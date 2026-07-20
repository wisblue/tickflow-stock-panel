"""Realtime model-v4 BB20 sell-rule evaluation for current positions.

The backtest implementation lives in the prediction workspace and remains
unchanged.  This service is deliberately self-contained for the panel:

* active symbols are read from ``data/user_data/active_stocks.json`` through
  :mod:`active_stocks` and filtered to ``source == positions``;
* one TickFlow batch request supplies the latest quote for all symbols;
* Redis transaction streams supply the causal minute bars used by the BB20
  rule, with the previous regular session used as warm-up;
* no transaction parquet is written by the realtime path.
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
RULE_NAME = "V4_BB20_MIDLINE_CLOSE_DOWNCROSS_SLOPE0P15_TOL0P05_OR_LIMITUP_LATEST1445"
WINDOW = 20
BOLLINGER_K = 2.0
SLOPE_THRESHOLD = 0.0015
DOWNWARD_TOLERANCE = 0.0005
MIN_HHMM = 925
MAX_HHMM = 1500
SELL_CAP_HHMM = 1445


def _norm_symbol(value: Any) -> str:
    raw = str(value or "").strip().split(".", 1)[0]
    digits = re.sub(r"\D", "", raw)
    return digits.zfill(6)[-6:] if digits else ""


def _norm_date(value: Any) -> str:
    raw = str(value or "").strip().replace("-", "").replace("/", "")
    return re.sub(r"\D", "", raw)[:8]


def _finite(value: Any) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _hhmm(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _format_hhmmss(value: Any) -> str:
    raw = _hhmm(value)
    if raw is None:
        return ""
    return f"{raw // 10000:02d}:{(raw // 100) % 100:02d}:{raw % 100:02d}"


def _tx_path(trade_date: str) -> Path:
    return TX_ROOT / trade_date[:4] / trade_date[4:6] / f"{trade_date[6:8]}.parquet"


@lru_cache(maxsize=1)
def _transaction_dates() -> tuple[str, ...]:
    if not TX_ROOT.exists():
        return ()
    values: set[str] = set()
    for path in TX_ROOT.glob("*/*/*.parquet"):
        if path.name.endswith("-ac.parquet") or ".manifest" in path.name:
            continue
        if re.fullmatch(r"\d{4}", path.parent.parent.name) and re.fullmatch(r"\d{2}", path.parent.name) and re.fullmatch(r"\d{2}", path.stem):
            values.add(f"{path.parent.parent.name}{path.parent.name}{path.stem}")
    return tuple(sorted(values))


def _previous_date(trade_date: str) -> str:
    prior = [value for value in _transaction_dates() if value < trade_date]
    return prior[-1] if prior else ""


@lru_cache(maxsize=4)
def _load_transaction_day(trade_date: str) -> pd.DataFrame:
    path = _tx_path(trade_date)
    if not path.exists():
        return pd.DataFrame()
    try:
        available = set(pl.read_parquet_schema(path).keys())
        code_col = "stock_code" if "stock_code" in available else "symbol"
        columns = [code_col, "time_hhmm", "time_hhmmss", "price"]
        columns = [column for column in columns if column in available]
        frame = pl.read_parquet(path, columns=columns)
        if code_col not in frame.columns or "price" not in frame.columns or "time_hhmm" not in frame.columns:
            return pd.DataFrame()
        frame = frame.rename({code_col: "stock_code"})
        frame = frame.with_columns(
            pl.col("stock_code").cast(pl.Utf8).str.extract(r"(\d+)", 1).str.zfill(6),
            pl.col("time_hhmm").cast(pl.Int64, strict=False),
            pl.col("time_hhmmss").cast(pl.Int64, strict=False) if "time_hhmmss" in frame.columns else pl.lit(None).cast(pl.Int64).alias("time_hhmmss"),
            pl.col("price").cast(pl.Float64, strict=False),
        ).filter(pl.col("time_hhmm").is_not_null() & pl.col("price").gt(0))
        return frame.to_pandas()
    except Exception as exc:  # noqa: BLE001
        logger.warning("model-v4 warm-up transaction read failed for %s: %s", trade_date, exc)
        return pd.DataFrame()


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
    except Exception as exc:  # noqa: BLE001
        logger.warning("model-v4 Redis read failed for %s: %s", symbol, exc)
        return pd.DataFrame()
    if not raw:
        return pd.DataFrame()

    rows: list[dict[str, Any]] = []
    for line in str(raw).splitlines():
        try:
            obj = json.loads(line)
            timestamp = float(obj.get("timestamp"))
            dt = datetime.fromtimestamp(timestamp, tz=TZ_SHANGHAI)
            if dt.strftime("%Y%m%d") != trade_date:
                continue
            price = _finite(obj.get("price"))
            if price is None or price <= 0:
                continue
            hhmmss = dt.hour * 10000 + dt.minute * 100 + dt.second
            rows.append({
                "stock_code": _norm_symbol(obj.get("symbol") or symbol),
                "time_hhmm": dt.hour * 100 + dt.minute,
                "time_hhmmss": hhmmss,
                "price": price,
                "transaction_time": timestamp,
            })
        except (TypeError, ValueError, json.JSONDecodeError, OSError):
            continue
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    frame = frame[frame["time_hhmm"].between(MIN_HHMM, MAX_HHMM)].copy()
    return frame.drop_duplicates(subset=["transaction_time", "time_hhmmss", "price"], keep="first").sort_values(
        ["time_hhmm", "time_hhmmss", "transaction_time"], kind="mergesort"
    ).reset_index(drop=True)


def _aggregate_minutes(frame: pd.DataFrame, session: str) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame()
    work = frame[frame["time_hhmm"].between(MIN_HHMM, MAX_HHMM)].copy()
    if work.empty:
        return pd.DataFrame()
    work["minute_id"] = (work["time_hhmm"] // 100) * 60 + (work["time_hhmm"] % 100)
    bars = (
        work.groupby("minute_id", sort=True)
        .agg(
            minute_open=("price", "first"),
            minute_high=("price", "max"),
            minute_low=("price", "min"),
            minute_close=("price", "last"),
            minute_last_hhmmss=("time_hhmmss", "last"),
        )
        .reset_index()
    )
    bars["session"] = session
    return bars


def _first_in_next_minute(frame: pd.DataFrame, signal_hhmm: int) -> pd.Series | None:
    """Return the first transaction strictly after the completed signal minute."""
    if frame.empty:
        return None
    out = frame[frame["time_hhmm"].gt(int(signal_hhmm))]
    return out.iloc[0] if not out.empty else None


def _build_bars(previous: pd.DataFrame, current: pd.DataFrame) -> pd.DataFrame:
    parts = []
    if not previous.empty:
        parts.append(_aggregate_minutes(previous[previous["time_hhmm"].between(930, 1500)], "warmup"))
    if not current.empty:
        parts.append(_aggregate_minutes(current, "exit"))
    if not parts:
        return pd.DataFrame()
    bars = pd.concat(parts, ignore_index=True, sort=False)
    bars["bb_middle"] = bars["minute_close"].rolling(WINDOW, min_periods=WINDOW).mean()
    bars["bb_std"] = bars["minute_close"].rolling(WINDOW, min_periods=WINDOW).std(ddof=0)
    bars["bb_upper"] = bars["bb_middle"] + BOLLINGER_K * bars["bb_std"]
    bars["bb_lower"] = bars["bb_middle"] - BOLLINGER_K * bars["bb_std"]
    bars["bb_middle_prev"] = bars["bb_middle"].shift(1)
    bars["bb_middle_slope"] = bars["bb_middle"].pct_change()
    bars["prev_close"] = bars["minute_close"].shift(1)
    bars["close_downcross"] = (
        bars["session"].eq("exit")
        & bars["prev_close"].gt(bars["bb_middle_prev"])
        & bars["minute_close"].le(bars["bb_middle"] * (1.0 - DOWNWARD_TOLERANCE))
        & bars["bb_middle_slope"].lt(SLOPE_THRESHOLD)
    ).fillna(False)
    return bars[bars["session"].eq("exit")].reset_index(drop=True)


def _limit_ratio(symbol: str, name: str) -> float:
    upper = name.upper()
    if "ST" in upper or "退" in name:
        return 0.05
    if symbol.startswith(("300", "301", "688", "689")):
        return 0.20
    if symbol.startswith(("8", "4")):
        return 0.30
    return 0.10


def _entry_map() -> dict[str, dict[str, Any]]:
    """Use the latest pending S150 state as the position cost basis when present."""
    state_path = Path("/home/dennis/re_3/codex/prediction/Models/limit-up/state/s150_live_state.json")
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in payload.get("s146_history", []):
        symbol = _norm_symbol(row.get("selected_stock_code"))
        buy = _finite(row.get("buy_price"))
        if symbol and buy and buy > 0 and str(row.get("settlement_status")) == "pending_sr004_exit":
            out[symbol] = {
                "buy_price": buy,
                "buy_date": _norm_date(row.get("trade_date")),
                "buy_price_source": str(row.get("buy_price_source") or "s150_live_state"),
            }
    return out


def _quote_map(request: Any, symbols: list[str]) -> dict[str, dict[str, Any]]:
    if not symbols:
        return {}
    try:
        rows = watchlist.fetch_quotes(symbols, request.app.state.capabilities)
    except Exception as exc:  # noqa: BLE001
        logger.warning("model-v4 batch quote fetch failed: %s", exc)
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = _norm_symbol(row.get("symbol"))
        if symbol:
            out[symbol] = row
    return out


def _session_open_price(current: pd.DataFrame, quote: dict[str, Any]) -> tuple[float | None, str]:
    """Resolve the regular-session open used as the return benchmark.

    TickFlow's realtime quote is the preferred source because it carries the
    official auction/open value.  Redis is a fallback for a partial quote
    response; auction prints before 09:30 are deliberately excluded.
    """
    quote_open = _finite(quote.get("open"))
    if quote_open is not None and quote_open > 0:
        return quote_open, "tickflow_quote.open"
    if current.empty:
        return None, ""
    regular = current[current["time_hhmm"].ge(930)].sort_values(
        ["time_hhmm", "time_hhmmss", "transaction_time"], kind="mergesort"
    )
    if regular.empty:
        return None, ""
    value = _finite(regular.iloc[0]["price"])
    return (value, "redis_first_regular_transaction") if value is not None else (None, "")


def _evaluate_one(
    *,
    symbol: str,
    name: str,
    trade_date: str,
    current: pd.DataFrame,
    quote: dict[str, Any],
    entry: dict[str, Any],
    client: redis.Redis,
) -> dict[str, Any]:
    buy_price = _finite(entry.get("buy_price"))
    latest_price = _finite(quote.get("price")) or _finite(quote.get("last_price"))
    if latest_price is None and not current.empty:
        latest_price = _finite(current.iloc[-1]["price"])
    open_price, open_price_source = _session_open_price(current, quote)
    previous_date = _norm_date(entry.get("buy_date")) or _previous_date(trade_date)
    previous = _load_transaction_day(previous_date)
    bars = _build_bars(previous[previous["stock_code"].eq(symbol)] if not previous.empty else pd.DataFrame(), current)
    signals = bars[bars["close_downcross"]].copy() if not bars.empty else pd.DataFrame()
    limit_event: dict[str, Any] | None = None
    if not previous.empty and not current.empty:
        prev_stock = previous[previous["stock_code"].eq(symbol)]
        if not prev_stock.empty:
            pre_close = _finite(prev_stock.sort_values(["time_hhmm", "time_hhmmss"]).iloc[-1]["price"])
            if pre_close:
                limit_price = round(pre_close * (1.0 + _limit_ratio(symbol, name)) + 1e-8, 2)
                hit = current[current["price"].ge(limit_price - 0.005)]
                if not hit.empty:
                    seen_trigger_minutes: set[int] = set()
                    for trigger in hit.itertuples(index=False):
                        trigger_hhmm = int(trigger.time_hhmm)
                        if trigger_hhmm in seen_trigger_minutes:
                            continue
                        seen_trigger_minutes.add(trigger_hhmm)
                        point = _first_in_next_minute(current, trigger_hhmm)
                        # A broken next-minute open invalidates this trigger;
                        # continue scanning for a later re-trigger.
                        if point is None or float(point["price"]) < limit_price - 0.005:
                            continue
                        limit_event = {
                            "event": "limit_up_hit",
                            "signal_time": _format_hhmmss(trigger.time_hhmmss),
                            "sell_time": _format_hhmmss(point["time_hhmmss"]),
                            "sell_price": _finite(point["price"]),
                        }
                        break
    down_event: dict[str, Any] | None = None
    if not signals.empty:
        signal = signals.iloc[0]
        signal_hhmm = int((signal["minute_id"] // 60) * 100 + signal["minute_id"] % 60)
        point = _first_in_next_minute(current, signal_hhmm)
        if point is not None:
            down_event = {
                "event": "bb_midline_downcross",
                "signal_time": _format_hhmmss(signal["minute_last_hhmmss"]),
                "sell_time": _format_hhmmss(point["time_hhmmss"]),
                "sell_price": _finite(point["price"]),
                "signal_price": _finite(signal["minute_close"]),
                "middle_line": _finite(signal["bb_middle"]),
                "middle_line_slope": _finite(signal["bb_middle_slope"]),
            }
    events = [event for event in (down_event, limit_event) if event and event.get("sell_price")]
    selected = min(events, key=lambda event: event["sell_time"]) if events else None
    sell_price = _finite(selected.get("sell_price")) if selected else None
    sell_time = str(selected.get("sell_time") or "") if selected else ""
    signal_time = str(selected.get("signal_time") or "") if selected else (_format_hhmmss(signals.iloc[0]["minute_last_hhmmss"]) if not signals.empty else "")
    reason = str(selected.get("event") or "") if selected else ""
    # Both displayed returns are benchmarked to today's regular-session open,
    # never to a position cost basis.  Before a fill, use the latest quote;
    # after a fill, use the first transaction of the next minute.
    mark_return = (latest_price / open_price - 1.0) if latest_price and open_price else None
    gross_return = (sell_price / open_price - 1.0) if sell_price and open_price else mark_return
    actual_return = gross_return
    return {
        "stock_code": symbol,
        "stock_name": name,
        "signal_time": signal_time,
        "sell_time": sell_time,
        "sell_price": sell_price,
        "sell_rule": RULE_NAME,
        "gross_return": gross_return,
        "actual_return": actual_return,
        "open_price": open_price,
        "open_price_source": open_price_source,
        "latest_price": latest_price,
        "buy_price": buy_price,
        "buy_price_source": entry.get("buy_price_source") or "",
        "status": "sell_triggered" if selected else "holding",
        "event": selected,
        "last_transaction_time": _format_hhmmss(current.iloc[-1]["time_hhmmss"]) if not current.empty else "",
        "data_source": "tickflow_batch_quote+redis_transaction",
        "parameters": {"window": WINDOW, "bollinger_k": BOLLINGER_K, "slope_threshold": SLOPE_THRESHOLD, "downward_tolerance": DOWNWARD_TOLERANCE},
    }


def evaluate_positions(request: Any, trade_date: str | None = None) -> dict[str, Any]:
    """Return one-minute refreshed BB20 sell status for all position symbols."""
    resolved_date = _norm_date(trade_date) or datetime.now(TZ_SHANGHAI).strftime("%Y%m%d")
    active_rows = [row for row in active_stocks.list_symbols() if str(row.get("source") or "") == "positions"]
    symbols = [_norm_symbol(row.get("symbol")) for row in active_rows]
    symbols = list(dict.fromkeys(symbol for symbol in symbols if symbol))
    name_map = {symbol: str(row.get("name") or "") for symbol, row in zip(symbols, active_rows)}
    missing_names = [symbol for symbol in symbols if not name_map.get(symbol)]
    if missing_names:
        try:
            # Repository instruments use exchange-qualified symbols (e.g.
            # ``000725.SZ``), while active_stocks.json stores six-digit codes.
            # Normalize both sides before merging so names are not lost.
            repo_names = request.app.state.repo.get_name_map()
            normalized_names = {
                _norm_symbol(key): str(value or "")
                for key, value in repo_names.items()
                if _norm_symbol(key) and value
            }
            name_map.update({symbol: normalized_names[symbol] for symbol in missing_names if normalized_names.get(symbol)})
        except Exception:  # noqa: BLE001
            pass
    quotes = _quote_map(request, symbols)
    for symbol, quote in quotes.items():
        if not name_map.get(symbol):
            name_map[symbol] = str(quote.get("name") or "")
    entries = _entry_map()
    # The active-stocks file may optionally carry a broker/imported cost basis.
    # It takes precedence for that symbol; otherwise use pending S150 state.
    for row in active_rows:
        symbol = _norm_symbol(row.get("symbol"))
        buy_price = _finite(row.get("buy_price") or row.get("entry_price"))
        if symbol and buy_price and buy_price > 0:
            entries[symbol] = {
                "buy_price": buy_price,
                "buy_date": _norm_date(row.get("buy_date") or row.get("entry_date")),
                "buy_price_source": str(row.get("buy_price_source") or "active_stocks.json"),
            }
    client = _redis_client()
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for symbol in symbols:
        try:
            current = _redis_rows(client, resolved_date, symbol)
            rows.append(_evaluate_one(symbol=symbol, name=name_map.get(symbol, ""), trade_date=resolved_date, current=current, quote=quotes.get(symbol, {}), entry=entries.get(symbol, {}), client=client))
        except Exception as exc:  # noqa: BLE001
            logger.exception("model-v4 evaluation failed for %s", symbol)
            errors.append(f"{symbol}: {exc}")
    return {
        "available": True,
        "trade_date": resolved_date,
        "checked_at": datetime.now(TZ_SHANGHAI).isoformat(timespec="seconds"),
        "refresh_interval_seconds": 60,
        "rule_name": RULE_NAME,
        "rule_contract": "previous-session warm-up; BB20(2); slope<0.15%; close tolerance=0.05%; limit-up next-minute confirmation; first transaction in next minute",
        "symbols_source": "data/user_data/active_stocks.json where source=positions",
        "quote_request": "one TickFlow quote batch for all position symbols per refresh",
        "rows": rows,
        "count": len(rows),
        "errors": errors,
    }
