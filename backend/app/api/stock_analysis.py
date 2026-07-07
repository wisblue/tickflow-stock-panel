"""个股分析 API — 关键价位 + AI 四维分析 + 报告持久化。

路由前缀: /api/stock-analysis

端点:
  GET  /levels?symbol=         11 类关键价位(图表 markLine 数据源)
  POST /analyze                AI 流式四维分析(NDJSON)
  GET  /reports                历史报告列表
  POST /reports                保存一条报告
  DELETE /reports/{report_id}  删除一条报告
"""
from __future__ import annotations

import csv
import json
import logging
import math
import os
import re
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.indicators.levels import compute_levels, summarize_levels
from app.services import stock_reports
from app.services.stock_analyzer import analyze_stock_stream

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stock-analysis", tags=["stock-analysis"])

DEFAULT_PREDICTION_ROOT = Path("/home/dennis/re_3/codex/prediction")
DEFAULT_STOCK_BUY_RANK_PYTHON = Path("/home/dennis/anaconda3/envs/re_3/bin/python")
DEFAULT_TRANSACTION_ROOT = Path("/home/dennis/historical_transaction")
DEFAULT_TDX_REDIS_ADDR = "localhost:6379"
DEFAULT_TDX_REDIS_DB = 15
MARKET_TZ = ZoneInfo("Asia/Shanghai")
STOCK_BUY_RANK_COMPARE_PATTERNS = (
    "s112_vs_stock_buy_rank_compare.csv",
    "s114_a_vs_stock_buy_rank_compare.csv",
    "s114_vs_stock_buy_rank_compare.csv",
)
STOCK_BUY_RANK_SCAN_PATTERNS = (
    "stock_buy_rank_grade_AS_all.csv",
    "stock_buy_rank_grade_AS_top.csv",
)


def _to_float_list(series: pl.Series) -> list:
    """polars Series → JSON 安全的 float 列表(null/NaN → None)。"""
    out: list = []
    for v in series.to_list():
        if v is None:
            out.append(None)
            continue
        try:
            f = float(v)
            out.append(round(f, 2) if math.isfinite(f) else None)
        except (TypeError, ValueError):
            out.append(None)
    return out


def _prediction_root() -> Path:
    raw = os.getenv("PREDICTION_ROOT") or os.getenv("STOCK_BUY_RANK_ROOT")
    return Path(raw).expanduser().resolve() if raw else DEFAULT_PREDICTION_ROOT


def _transaction_root() -> Path:
    raw = os.getenv("HISTORICAL_TRANSACTION_ROOT") or os.getenv("TRANSACTION_ROOT")
    return Path(raw).expanduser().resolve() if raw else DEFAULT_TRANSACTION_ROOT


def _stock_code(value: str | None) -> str:
    m = re.search(r"(\d{6})", str(value or ""))
    return m.group(1) if m else ""


def _path_date_asof(path: Path) -> tuple[str, int]:
    text = str(path)
    date_match = re.search(r"(20\d{6})", text)
    asof_match = re.search(r"asof(\d{3,4})", text) or re.search(r"_(\d{3,4})(?:/|$)", text)
    return (
        date_match.group(1) if date_match else "",
        int(asof_match.group(1)) if asof_match else 0,
    )


def _run_sort_key(path: Path) -> tuple[str, int, float]:
    trade_date, asof = _path_date_asof(path)
    return (trade_date, asof, path.stat().st_mtime)


def _as_float(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _as_int(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _read_compare_rows(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        for raw in csv.DictReader(f):
            stock = _stock_code(raw.get("stock_code") or raw.get("symbol") or raw.get("ts_code"))
            if not stock:
                continue
            model_rank = (
                _as_int(raw.get("s114_a_rank"))
                or _as_int(raw.get("s114_rank"))
                or _as_int(raw.get("s112_rank"))
            )
            model_score = (
                _as_float(raw.get("s114_score"))
                or _as_float(raw.get("s112_score"))
                or _as_float(raw.get("score"))
            )
            rows.append({
                "stock_code": stock,
                "name": raw.get("name") or "",
                "model_rank": model_rank,
                "model_score": model_score,
                "sbr_rank": _as_int(raw.get("sbr_rank")),
                "sbr_label": raw.get("sbr_label") or raw.get("label") or "",
                "sbr_score": _as_int(raw.get("sbr_score")),
                "close": _as_float(raw.get("close")),
                "ret_pct": _as_float(raw.get("ret_pct")),
                "net_w": _as_int(raw.get("net_w")),
                "main_net_w": _as_int(raw.get("main_net_w") or raw.get("main_net")),
                "main_top_net_w": _as_int(raw.get("main_top_net_w") or raw.get("main_top_net")),
                "main_bot_net_w": _as_int(raw.get("main_bot_net_w") or raw.get("main_bot_net")),
                "main_l30_net_w": _as_int(raw.get("main_l30_net_w") or raw.get("main_l30_net")),
                "main_share_pct": _as_float(raw.get("main_share_pct") or raw.get("main_share")),
                "after_amt_w": _as_int(raw.get("after_amt_w") or raw.get("after_amt")),
                "after_main_amt_w": _as_int(raw.get("after_main_amt_w") or raw.get("after_main_amt")),
                "after_main_share_pct": _as_float(raw.get("after_main_share_pct") or raw.get("after_main_share")),
                "after_price": _as_float(raw.get("after_price")),
                "ret_3d_pct": _as_float(raw.get("ret_3d_pct")),
                "sbr_grade": raw.get("sbr_grade") or raw.get("grade") or "",
                "rank_delta": (
                    _as_int(raw.get("rank_delta_s114_a_minus_sbr"))
                    or _as_int(raw.get("rank_delta_s112_minus_sbr"))
                ),
                "reasons": raw.get("main_reason") or raw.get("reasons") or "",
            })
    return sorted(rows, key=lambda x: (x.get("sbr_rank") or 999, x.get("model_rank") or 999))


def _read_scan_rows(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        for raw in csv.DictReader(f):
            stock = _stock_code(raw.get("symbol") or raw.get("stock_code") or raw.get("ts_code"))
            if not stock:
                continue
            rows.append({
                "stock_code": stock,
                "name": raw.get("name") or "",
                "model_rank": None,
                "model_score": None,
                "sbr_rank": None,
                "sbr_label": "",
                "sbr_score": _as_int(raw.get("score")),
                "close": _as_float(raw.get("close")),
                "ret_pct": _as_float(raw.get("ret")),
                "net_w": _as_int(raw.get("net")),
                "main_net_w": _as_int(raw.get("main_net_w") or raw.get("main_net")),
                "main_top_net_w": _as_int(raw.get("main_top_net_w") or raw.get("main_top_net")),
                "main_bot_net_w": _as_int(raw.get("main_bot_net_w") or raw.get("main_bot_net")),
                "main_l30_net_w": _as_int(raw.get("main_l30_net_w") or raw.get("main_l30_net")),
                "main_share_pct": _as_float(raw.get("main_share_pct") or raw.get("main_share")),
                "after_amt_w": _as_int(raw.get("after_amt_w") or raw.get("after_amt")),
                "after_main_amt_w": _as_int(raw.get("after_main_amt_w") or raw.get("after_main_amt")),
                "after_main_share_pct": _as_float(raw.get("after_main_share_pct") or raw.get("after_main_share")),
                "after_price": _as_float(raw.get("after_price")),
                "ret_3d_pct": _as_float(raw.get("ret_3d")),
                "sbr_grade": raw.get("grade") or "",
                "rank_delta": None,
                "reasons": raw.get("reasons") or "",
                "t_max": _as_int(raw.get("t_max")),
            })
    rows = sorted(rows, key=lambda x: (-(x.get("sbr_score") or -999), x["stock_code"]))
    for idx, row in enumerate(rows, start=1):
        row["sbr_rank"] = idx
        if idx == 1:
            row["sbr_label"] = "买入"
        elif idx <= 5:
            row["sbr_label"] = "关注"
        elif idx <= 20:
            row["sbr_label"] = "观察"
        else:
            row["sbr_label"] = "扫描"
    return rows


def _artifact_model_name(path: Path) -> str:
    name = path.name
    if "stock_buy_rank_grade" in name:
        return "Stock-Buy-Rank Scan"
    if "s112" in name:
        return "S112"
    if "s114_a" in name:
        return "S114-A"
    if "s114" in name:
        return "S114"
    return "Stock-Buy-Rank"


def _final_report_excerpt(path: Path) -> str:
    report_candidates = [
        path.with_name("stock_buy_rank_report.txt"),
        path.with_name("s114_a_stock_buy_rank_report.txt"),
    ]
    report = next((p for p in report_candidates if p.exists()), None)
    if not report:
        return ""
    text = report.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()
    start = 0
    for i, line in enumerate(lines):
        if "最终排名" in line:
            start = max(0, i - 2)
            break
    return "\n".join(lines[start:start + 80]).strip()


def _parse_stock_buy_rank_stdout(stdout: str) -> list[dict]:
    rows: list[dict] = []
    line_re = re.compile(
        r"^\s*(?P<label>.*?)\s+(?P<stock>\d{6})\s+(?P<name>\S+)\s+score=\s*(?P<score>-?\d+)"
        r"\s+close=(?P<close>-?\d+(?:\.\d+)?)\s+ret=(?P<ret>N/A|[+-]?\d+(?:\.\d+)?)%"
        r"\s+net=(?P<net>[+-]?\d+)w(?:\s+main=(?P<main>[+-]?\d+)w)?"
        r"\s+3d=(?P<ret3d>N/A|[+-]?\d+(?:\.\d+)?)%\s+grade=(?P<grade>\S+)"
    )
    in_final = False
    for line in stdout.splitlines():
        if "最终排名" in line:
            in_final = True
            continue
        if in_final and line.strip().startswith("最佳:"):
            break
        if not in_final:
            continue
        match = line_re.match(line)
        if not match:
            continue
        rec = match.groupdict()
        rows.append({
            "stock_code": rec["stock"],
            "name": rec["name"],
            "model_rank": None,
            "model_score": None,
            "sbr_rank": len(rows) + 1,
            "sbr_label": " ".join(rec["label"].split()),
            "sbr_score": int(rec["score"]),
            "close": float(rec["close"]),
            "ret_pct": None if rec["ret"] == "N/A" else float(rec["ret"]),
            "net_w": int(rec["net"]),
            "main_net_w": int(rec["main"]) if rec.get("main") else None,
            "main_top_net_w": None,
            "main_bot_net_w": None,
            "main_l30_net_w": None,
            "main_share_pct": None,
            "after_amt_w": None,
            "after_main_amt_w": None,
            "after_main_share_pct": None,
            "after_price": None,
            "ret_3d_pct": None if rec["ret3d"] == "N/A" else float(rec["ret3d"]),
            "sbr_grade": rec["grade"],
            "rank_delta": None,
            "reasons": "",
        })

    main_re = re.compile(
        r"^\s*(?P<stock>\d{6})\s+(?P<main>[+-]?\d+)\s+(?P<top>[+-]?\d+)\s+(?P<bot>[+-]?\d+)"
        r"\s+(?P<l30>[+-]?\d+)\s+(?P<amt>\d+)\s+(?P<share>\d+(?:\.\d+)?)%"
    )
    in_main = False
    main_map: dict[str, dict] = {}
    for line in stdout.splitlines():
        if "主力行为" in line:
            in_main = True
            continue
        if in_main and "评分明细" in line:
            break
        if not in_main:
            continue
        match = main_re.match(line)
        if not match:
            continue
        rec = match.groupdict()
        main_map[rec["stock"]] = {
            "main_net_w": int(rec["main"]),
            "main_top_net_w": int(rec["top"]),
            "main_bot_net_w": int(rec["bot"]),
            "main_l30_net_w": int(rec["l30"]),
            "main_share_pct": float(rec["share"]),
        }

    reason_map: dict[str, list[str]] = {}
    after_re = re.compile(
        r"^\s*(?P<stock>\d{6})\s+(?P<amt>\d+)\s+(?P<main>\d+)\s+(?P<share>\d+(?:\.\d+)?)%"
        r"\s+(?P<price>-?\d+(?:\.\d+)?)\s+\d+\s+\d+(?:\.\d+)?w"
    )
    in_after = False
    after_map: dict[str, dict] = {}
    for line in stdout.splitlines():
        if "盘后定价交易" in line:
            in_after = True
            continue
        if in_after and "评分明细" in line:
            break
        if not in_after:
            continue
        match = after_re.match(line)
        if not match:
            continue
        rec = match.groupdict()
        after_map[rec["stock"]] = {
            "after_amt_w": int(rec["amt"]),
            "after_main_amt_w": int(rec["main"]),
            "after_main_share_pct": float(rec["share"]),
            "after_price": float(rec["price"]),
        }

    current_stock = ""
    detail_re = re.compile(r"^\s*(?P<stock>\d{6})\s+\S+\s+—")
    for line in stdout.splitlines():
        m = detail_re.match(line)
        if m:
            current_stock = m.group("stock")
            reason_map.setdefault(current_stock, [])
            continue
        stripped = line.strip()
        if current_stock and stripped and not stripped.startswith("=") and not stripped.startswith("-"):
            if " | " in stripped or stripped.startswith("✓ "):
                raw_reason = stripped.removeprefix("✓ ").strip()
                parts = [p.strip() for p in raw_reason.split(" | ") if p.strip()]
                reason_map.setdefault(current_stock, []).extend(parts)

    for row in rows:
        if row["stock_code"] in main_map:
            row.update(main_map[row["stock_code"]])
        if row["stock_code"] in after_map:
            row.update(after_map[row["stock_code"]])
        reasons = []
        for reason in reason_map.get(row["stock_code"]) or []:
            if reason and reason not in reasons:
                reasons.append(reason)
        row["reasons"] = " | ".join(reasons)
    return rows


def _direct_stock_buy_rank(symbol: str) -> dict | None:
    stock = _stock_code(symbol)
    if not stock:
        return None
    root = _prediction_root()
    script = root / "scripts" / "stock_buy_rank.py"
    if not script.exists():
        return None
    python_exe = Path(os.getenv("STOCK_BUY_RANK_PYTHON") or DEFAULT_STOCK_BUY_RANK_PYTHON)
    if not python_exe.exists():
        python_exe = Path(os.getenv("PYTHON") or "python")
    cmd = [str(python_exe), str(script), "--symbols", stock]
    result = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        logger.warning("stock-buy-rank direct run failed rc=%s stderr=%s", result.returncode, result.stderr[-1000:])
        return None
    rows = _parse_stock_buy_rank_stdout(result.stdout)
    matched = next((r for r in rows if r["stock_code"] == stock), None)
    if not matched:
        return None
    trade_date_match = re.search(r"date\s+(20\d{6})", result.stdout)
    best = rows[0] if rows else matched
    return {
        "available": True,
        "message": "按当前输入股票直接运行 stock_buy_rank.py。",
        "trade_date": trade_date_match.group(1) if trade_date_match else "",
        "asof": None,
        "source_model": "Stock-Buy-Rank Direct",
        "source_path": "scripts/stock_buy_rank.py",
        "status": "pass",
        "elapsed_sec": None,
        "symbol_matched": True,
        "best": best,
        "matched": matched,
        "rows": rows,
        "report_excerpt": result.stdout,
    }


def _transaction_path_for_date(trade_date: str) -> Path:
    root = _transaction_root()
    return root / trade_date[:4] / trade_date[4:6] / f"{trade_date[6:8]}.parquet"


def _latest_transaction_path() -> Path | None:
    root = _transaction_root()
    if not root.exists():
        return None
    paths = [
        path
        for path in root.glob("20[0-9][0-9]/[0-1][0-9]/[0-3][0-9].parquet")
        if path.stem.isdigit()
    ]
    return sorted(paths, key=lambda p: (p.parent.parent.name, p.parent.name, p.stem))[-1] if paths else None


def _transaction_trade_date(path: Path) -> str:
    return f"{path.parent.parent.name}{path.parent.name}{path.stem}"


def _redis_intraday_frame(symbol: str, trade_date: str | None = None) -> tuple[pl.DataFrame | None, str]:
    try:
        import redis
    except ImportError:
        return None, ""

    addr = os.getenv("TDX_REDIS_ADDR") or os.getenv("REDIS_ADDR") or DEFAULT_TDX_REDIS_ADDR
    host, _, port_text = addr.partition(":")
    try:
        port = int(port_text or "6379")
        db = int(os.getenv("TDX_REDIS_DB") or os.getenv("REDIS_DB") or DEFAULT_TDX_REDIS_DB)
        client = redis.Redis(
            host=host or "localhost",
            port=port,
            db=db,
            password=os.getenv("TDX_REDIS_PASSWORD") or None,
            socket_connect_timeout=0.2,
            socket_timeout=0.5,
            decode_responses=True,
        )
        raw = client.get(f"{os.getenv('TDX_REDIS_KEY_PREFIX', 'tdx:trans')}:{symbol}")
    except Exception as exc:
        logger.debug("tdx redis read failed for %s: %s", symbol, exc)
        return None, ""

    if not raw:
        return None, ""

    records = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if _stock_code(rec.get("symbol")) != symbol:
            continue
        ts = _as_float(rec.get("timestamp"))
        price = _as_float(rec.get("price"))
        if ts is None or price is None:
            continue
        dt = datetime.fromtimestamp(ts, MARKET_TZ)
        rec_date = dt.strftime("%Y%m%d")
        if trade_date and rec_date != trade_date:
            continue
        records.append({
            "time_hhmmss": dt.hour * 10000 + dt.minute * 100 + dt.second,
            "time_text": dt.strftime("%H:%M:%S"),
            "price": price,
            "vol": _as_float(rec.get("vol")) or 0.0,
            "trade_count": max(_as_float(rec.get("num")) or 1.0, 1.0),
            "bs_flag": _as_int(rec.get("buy_or_sell")) if _as_int(rec.get("buy_or_sell")) is not None else 2,
            "_trade_date": rec_date,
        })

    if not records:
        return None, ""
    frame = pl.DataFrame(records).sort("time_hhmmss")
    date_text = trade_date or str(frame["_trade_date"][-1])
    return frame.drop("_trade_date"), f"redis:{addr}/db{db}/tdx:trans:{symbol}:{date_text}"


def _parquet_intraday_frame(symbol: str, trade_date: str | None = None) -> tuple[pl.DataFrame | None, str, str]:
    path = _transaction_path_for_date(trade_date) if trade_date else _latest_transaction_path()
    if path is None or not path.exists():
        return None, "", "暂无 transaction parquet"

    date_text = trade_date or _transaction_trade_date(path)
    required = ["time_hhmmss", "time_text", "symbol", "stock_code", "price", "vol", "trade_count", "bs_flag"]
    schema = pl.scan_parquet(path).collect_schema()
    available_cols = [col for col in required if col in schema.names()]
    if "symbol" not in available_cols and "stock_code" not in available_cols:
        return None, date_text, "transaction parquet 缺少 symbol/stock_code"

    symbol_filter = (
        pl.col("symbol").cast(pl.Utf8).str.extract(r"(\d+)", 1).str.zfill(6).eq(symbol)
        if "symbol" in available_cols
        else pl.lit(False)
    )
    stock_code_filter = (
        pl.col("stock_code").cast(pl.Utf8).str.extract(r"(\d+)", 1).str.zfill(6).eq(symbol)
        if "stock_code" in available_cols
        else pl.lit(False)
    )
    frame = (
        pl.scan_parquet(path)
        .filter(symbol_filter | stock_code_filter)
        .select([col for col in ["time_hhmmss", "time_text", "price", "vol", "trade_count", "bs_flag"] if col in available_cols])
        .with_columns(
            pl.col("time_hhmmss").cast(pl.Int64, strict=False),
            pl.col("price").cast(pl.Float64, strict=False),
            pl.col("vol").cast(pl.Float64, strict=False).fill_null(0.0),
            pl.col("trade_count").cast(pl.Float64, strict=False).fill_null(1.0).clip(lower_bound=1.0),
            pl.col("bs_flag").cast(pl.Int64, strict=False).fill_null(2),
        )
        .drop_nulls(["time_hhmmss", "price"])
        .sort("time_hhmmss")
        .collect()
    )
    try:
        source_path = str(path.relative_to(_transaction_root()))
    except ValueError:
        source_path = str(path)
    return frame, date_text, source_path


def _build_transaction_intraday(symbol: str, trade_date: str | None = None) -> dict:
    stock = _stock_code(symbol)
    if not stock:
        return {"available": False, "message": "symbol 不能为空", "rows": []}

    frame, source_path = _redis_intraday_frame(stock, trade_date)
    date_text = trade_date or ""
    if frame is not None and not frame.is_empty():
        if not date_text:
            date_text = source_path.rsplit(":", 1)[-1]
    else:
        frame, date_text, source_path = _parquet_intraday_frame(stock, trade_date)
        if frame is None:
            return {"available": False, "message": source_path or "暂无 transaction parquet", "rows": []}

    if frame.is_empty():
        return {"available": False, "message": f"{date_text} 未找到 {stock} 的 transaction 数据", "rows": []}

    frame = frame.with_columns(
        (pl.col("price") * pl.col("vol") * 100.0).alias("_amount"),
        (pl.col("price") * pl.col("vol") * 100.0 / pl.col("trade_count") / 10000.0).alias("_avg_trade_w"),
    )
    regular = frame.filter(pl.col("bs_flag") != 5)
    main_threshold = 0.0
    if not regular.is_empty():
        main_flow = regular.filter(pl.col("_avg_trade_w") >= 5.0)
        if main_flow.height < max(10, int(regular.height * 0.01)):
            q = regular.select(pl.col("_avg_trade_w").quantile(0.98)).item()
            main_threshold = float(q or 0.0)
        else:
            main_threshold = 5.0

    frame = frame.with_columns(
        pl.when(pl.col("bs_flag") == 0).then(pl.col("_amount") / 10000.0)
        .when(pl.col("bs_flag") == 1).then(-pl.col("_amount") / 10000.0)
        .otherwise(0.0)
        .alias("_full_delta_w"),
        pl.when((pl.col("bs_flag") != 5) & (pl.col("_avg_trade_w") >= main_threshold) & (pl.col("bs_flag") == 0))
        .then(pl.col("_amount") / 10000.0)
        .when((pl.col("bs_flag") != 5) & (pl.col("_avg_trade_w") >= main_threshold) & (pl.col("bs_flag") == 1))
        .then(-pl.col("_amount") / 10000.0)
        .otherwise(0.0)
        .alias("_main_delta_w"),
        pl.when(pl.col("bs_flag") == 5).then(pl.col("_amount") / 10000.0).otherwise(0.0).alias("_after_amount_w"),
    )

    grouped = (
        frame.group_by("time_hhmmss", maintain_order=True)
        .agg(
            pl.col("time_text").last().alias("time"),
            pl.col("price").last().alias("price"),
            pl.col("_full_delta_w").sum().alias("full_delta_w"),
            pl.col("_main_delta_w").sum().alias("main_delta_w"),
            pl.col("_after_amount_w").sum().alias("after_amount_w"),
            pl.col("vol").sum().alias("volume"),
            pl.col("trade_count").sum().alias("trade_count"),
        )
        .sort("time_hhmmss")
        .with_columns(
            pl.col("full_delta_w").cum_sum().alias("full_net_w"),
            pl.col("main_delta_w").cum_sum().alias("main_net_w"),
            pl.col("after_amount_w").cum_sum().alias("after_amount_cum_w"),
        )
    )
    rows = []
    for row in grouped.iter_rows(named=True):
        rows.append({
            "time": row["time"] or f"{int(row['time_hhmmss']) // 10000:02d}:{(int(row['time_hhmmss']) // 100) % 100:02d}:{int(row['time_hhmmss']) % 100:02d}",
            "time_hhmmss": int(row["time_hhmmss"]),
            "price": round(float(row["price"]), 4),
            "full_net_w": round(float(row["full_net_w"]), 2),
            "main_net_w": round(float(row["main_net_w"]), 2),
            "full_delta_w": round(float(row["full_delta_w"]), 2),
            "main_delta_w": round(float(row["main_delta_w"]), 2),
            "after_amount_w": round(float(row["after_amount_w"]), 2),
            "after_amount_cum_w": round(float(row["after_amount_cum_w"]), 2),
            "volume": int(row["volume"] or 0),
            "trade_count": int(row["trade_count"] or 0),
        })

    return {
        "available": True,
        "message": "",
        "symbol": stock,
        "trade_date": date_text,
        "source_path": source_path,
        "main_threshold_w": round(main_threshold, 2),
        "rows": rows,
        "summary": {
            "points": len(rows),
            "raw_rows": frame.height,
            "min_time": rows[0]["time"] if rows else "",
            "max_time": rows[-1]["time"] if rows else "",
            "last_price": rows[-1]["price"] if rows else None,
            "full_net_w": rows[-1]["full_net_w"] if rows else 0,
            "main_net_w": rows[-1]["main_net_w"] if rows else 0,
            "after_amount_w": rows[-1]["after_amount_cum_w"] if rows else 0,
        },
    }


def _read_artifact_candidate(path: Path, kind: str) -> list[dict]:
    return _read_scan_rows(path) if kind == "scan" else _read_compare_rows(path)


def _build_stock_buy_rank_response(path: Path, rows: list[dict], kind: str, symbol: str) -> dict:
    root = _prediction_root()
    trade_date, asof = _path_date_asof(path)
    if not asof and rows:
        asof = int(rows[0].get("t_max") or 0)

    wanted = _stock_code(symbol)
    matched = next((r for r in rows if wanted and r["stock_code"] == wanted), None)
    best = next((r for r in rows if r.get("sbr_rank") == 1), None)
    display_rows = rows
    if kind == "scan":
        top_rows = rows[:10]
        display_rows = top_rows if not matched or matched in top_rows else top_rows + [matched]

    audit_path = path.with_name("stock_buy_rank_audit.json")
    audit = {}
    if audit_path.exists():
        try:
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            audit = {}

    source_path = str(path)
    try:
        source_path = str(path.relative_to(root))
    except ValueError:
        pass

    return {
        "available": True,
        "message": "" if kind == "compare" else "显示 realtime stock-buy-rank 扫描结果；该股票不在最新 final top-5 compare 输出中。",
        "trade_date": trade_date or audit.get("trade_date", ""),
        "asof": asof or audit.get("asof"),
        "source_model": _artifact_model_name(path),
        "source_path": source_path,
        "status": audit.get("status", "pass" if rows else ""),
        "elapsed_sec": audit.get("elapsed_sec"),
        "symbol_matched": bool(matched),
        "best": best,
        "matched": matched,
        "rows": display_rows,
        "report_excerpt": _final_report_excerpt(path),
    }


def _read_stock_buy_rank_artifact(symbol: str | None = None) -> dict:
    wanted = _stock_code(symbol)
    if wanted:
        direct = _direct_stock_buy_rank(wanted)
        if direct:
            return direct

    root = _prediction_root()
    runs = root / "Models" / "limit-up" / "runs"
    if not runs.exists():
        return {
            "available": False,
            "message": f"未找到 stock-buy-rank 输出目录: {runs}",
            "rows": [],
        }

    groups = [
        ("compare", STOCK_BUY_RANK_COMPARE_PATTERNS),
        ("scan", STOCK_BUY_RANK_SCAN_PATTERNS),
    ]
    fallback: tuple[Path, list[dict], str] | None = None
    for kind, patterns in groups:
        candidates: list[Path] = []
        for pattern in patterns:
            candidates.extend(runs.glob(f"**/{pattern}"))
        for candidate in sorted(set(candidates), key=_run_sort_key, reverse=True):
            rows = _read_artifact_candidate(candidate, kind)
            if not rows:
                continue
            if fallback is None:
                fallback = (candidate, rows, kind)
            if wanted and any(r["stock_code"] == wanted for r in rows):
                return _build_stock_buy_rank_response(candidate, rows, kind, wanted)
            if not wanted:
                return _build_stock_buy_rank_response(candidate, rows, kind, "")

    if fallback:
        candidate, rows, kind = fallback
        return _build_stock_buy_rank_response(candidate, rows, kind, wanted)
    return {"available": False, "message": "暂无 stock-buy-rank 输出", "rows": []}


def _build_series(df: pl.DataFrame) -> dict:
    """提取带状指标(布林带 / Keltner通道 / ATR止损)的每日时间序列。

    这些指标的本质是"每日一条线",随 MA/ATR/σ 漂移,画成曲线才能体现通道形态。
    其余固定价位(枢轴/前高前低等)不在此,仍用水平 markLine。

    返回结构(每个 value 都是按日期对齐的数组):
      {
        "boll":      {"upper": [...], "lower": [...]},
        "keltner_s": {"upper": [...], "lower": [...]},   # 短期 MA20±2ATR
        "keltner_m": {"upper": [...], "lower": [...]},   # 中期 MA60±2.5ATR
        "keltner_l": {"upper": [...], "lower": [...]},   # 长期 MA120±3ATR
        "atr":       {"stop_loss": [...], "take_profit": [...]},  # close∓2ATR
      }
    """
    if df.is_empty() or "close" not in df.columns:
        return {}

    out: dict[str, dict] = {}
    close = df["close"]
    has_atr = "atr_14" in df.columns

    # 布林带(上/下/中轨;中轨 = MA20,数据层已预计算)
    if "boll_upper" in df.columns and "boll_lower" in df.columns:
        out["boll"] = {
            "upper": _to_float_list(df["boll_upper"]),
            "lower": _to_float_list(df["boll_lower"]),
            "mid": _to_float_list(df["ma20"]) if "ma20" in df.columns else None,
        }

    # Keltner 通道三档(需要 ATR)
    if has_atr:
        atr = df["atr_14"]
        # MA120 现场算(不在预计算列中)
        ma120 = df.select(pl.col("close").rolling_mean(120))["close"] if df.height >= 120 else None

        def _channel(ma: pl.Series, n: float) -> dict:
            return {
                "upper": _to_float_list(ma + n * atr),
                "lower": _to_float_list(ma - n * atr),
            }

        if "ma20" in df.columns:
            out["keltner_s"] = _channel(df["ma20"], 2.0)
        if "ma60" in df.columns:
            out["keltner_m"] = _channel(df["ma60"], 2.5)
        if ma120 is not None:
            out["keltner_l"] = _channel(ma120, 3.0)

        # ATR 止损/止盈: close ± 2×ATR(跟随行情漂移的动态止损线)
        out["atr"] = {
            "stop_loss": _to_float_list(close - 2 * atr),
            "take_profit": _to_float_list(close + 2 * atr),
        }

    return out


@router.get("/levels")
def get_levels(
    request: Request,
    symbol: str = Query(..., description="标的代码,如 000001.SZ"),
    days: int = Query(120, ge=30, le=500, description="计算样本天数"),
):
    """计算 11 类关键价位(成交密集区压力支撑 / 枢轴点 / 前高前低 /
    布林带 / Keltner短中长 / ATR止损 / 缺口 / 斐波那契 / 整数关口)。

    返回 {levels: {sr, pivot, extreme, boll, keltner_s, keltner_m, keltner_l,
    atr_stop, gap, fib, round}, close, summary, dates, series}。
    前端按 levels 的 key 渲染开关按钮,逐组显隐 markLine / 曲线。
    """
    if not symbol:
        raise HTTPException(400, "symbol 不能为空")

    repo = request.app.state.repo
    end = date.today()
    start = end - timedelta(days=days * 2)
    # 按资产类型分流: ETF/指数走独立 enriched 存储, 股票保持原路径
    df = repo.get_daily_asset(repo.resolve_asset_type(symbol), symbol, start, end)
    if df.is_empty():
        return {"levels": {"sr": [], "pivot": [], "extreme": [],
                           "boll": [], "keltner_s": [], "keltner_m": [], "keltner_l": [],
                           "atr_stop": [], "gap": [], "fib": [], "round": []},
                "close": None, "summary": "无数据", "symbol": symbol,
                "dates": [], "series": {}}

    levels = compute_levels(df)
    close = float(df.tail(1)["close"][0]) if "close" in df.columns else None
    # 日期 + 带状曲线序列(供前端画 Keltner/ATR/布林带曲线)
    dates = df["date"].to_list()
    series = _build_series(df)
    return {
        "levels": levels,
        "close": close,
        "summary": summarize_levels(levels, close),
        "symbol": symbol,
        "dates": [str(d) for d in dates],
        "series": series,
    }


@router.get("/stock-buy-rank")
def get_stock_buy_rank(symbol: str = Query("", description="可选:优先返回包含该股票的最新输出")):
    """读取 prediction 工作区内 stock-buy-rank 输出。"""
    try:
        return _read_stock_buy_rank_artifact(symbol or None)
    except Exception as exc:  # noqa: BLE001
        logger.exception("stock-buy-rank artifact read failed")
        raise HTTPException(500, f"读取 stock-buy-rank 输出失败: {exc}") from exc


@router.get("/transaction-intraday")
def get_transaction_intraday(
    symbol: str = Query(..., description="标的代码,如 000725"),
    trade_date: str = Query("", description="可选: YYYYMMDD,默认最新 transaction parquet"),
):
    """从最新 historical_transaction parquet 构建分时价格/全量主动净额/主力净额序列。"""
    try:
        return _build_transaction_intraday(symbol, trade_date or None)
    except Exception as exc:  # noqa: BLE001
        logger.exception("transaction intraday read failed")
        raise HTTPException(500, f"读取 transaction 分时失败: {exc}") from exc


class AnalyzeRequest(BaseModel):
    """AI 个股分析请求。"""
    symbol: str
    focus: str = ""  # 可选:用户追加的分析关注点


@router.post("/analyze")
async def analyze_stock(request: Request, req: AnalyzeRequest):
    """AI 个股四维分析 — NDJSON 流式返回。

    组合 K 线(技术指标)+ 财务表 + 关键价位 → 实战派提示词 →
    流式调用 LLM → 逐 chunk 以 NDJSON 推给前端(每行一个 JSON)。
    """
    if not req.symbol:
        raise HTTPException(400, "symbol 不能为空")

    repo = request.app.state.repo
    data_dir = repo.store.data_dir

    async def stream_gen():
        async for chunk in analyze_stock_stream(repo, data_dir, req.symbol, req.focus):
            yield chunk + "\n"

    return StreamingResponse(
        stream_gen(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ================================================================
# 报告 CRUD(历史报告持久化)
# ================================================================

class SaveReportRequest(BaseModel):
    """保存一条 AI 个股分析报告。"""
    symbol: str
    name: str = ""
    focus: str = ""
    content: str
    summary: str = ""
    close: float | None = None
    levels: dict | None = None


@router.get("/reports")
def list_reports(request: Request):
    """获取全部历史报告(按时间降序,后端已裁剪到上限)。"""
    return {"reports": stock_reports.list_reports()}


@router.post("/reports")
def save_report(request: Request, req: SaveReportRequest):
    """保存一条报告。"""
    report = stock_reports.save_report({
        "symbol": req.symbol,
        "name": req.name,
        "focus": req.focus,
        "content": req.content,
        "summary": req.summary,
        "close": req.close,
        "levels": req.levels,
    })
    return {"ok": True, "report": report}


@router.delete("/reports/{report_id}")
def delete_report(request: Request, report_id: str):
    """删除一条报告。"""
    ok = stock_reports.delete_report(report_id)
    return {"ok": ok}
