"""热门概念板块 — 涨停股票 → 概念板块 Treemap 数据。

数据流:
  主要: parquet 历史逐笔成交 → 每只股票最后成交价 → 对比 tushare 昨收 → 涨停股 → THS 概念映射 → Treemap
  实时: Redis (tdx:trans:{symbol}) → 最新价 → 对比昨收 → 涨停股 → THS 概念映射 → Treemap（盘中）
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import redis
from fastapi import APIRouter, Query, Request

from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/hot-concepts", tags=["hot-concepts"])

ProgressCallback = Callable[[str, int, str], None]


def _noop_progress(_stage: str, _progress: int, _message: str) -> None:
    pass

# 各板涨跌幅限制
STOCK_LIMITS: dict[str, float] = {
    "0": 10, "1": 10, "2": 20, "3": 20, "4": 30,
    "5": 10, "6": 10, "7": 10, "8": 30, "9": 30,
}

# 过滤掉太泛化的概念
_GENERIC_CONCEPTS = {
    "融资融券", "国企改革", "专精特新", "深股通", "沪股通", "深港通",
    "沪港通", "创投", "共同富裕示范区", "粤港澳大湾区",
    "雄安新区", "2024年报预增", "人民币贬值受益",
}

# 同花顺概念成员文件
_THS_MEMBERS_PATH = Path(__file__).resolve().parent.parent.parent.parent / "data" / "ths_members_N.csv"

# 历史逐笔成交 parquet 根目录
_HISTORICAL_ROOT = Path.home() / "historical_transaction"

# tushare token
_TS_TOKEN = "483cf3c22271f6e62a180906d13eeb490668d55e6b38e037a0ac1fd0"

# ===== tushare 公共连接 =====

_ts_pro = None

def _get_ts_pro():
    global _ts_pro
    if _ts_pro is None:
        import tushare as ts
        ts.set_token(_TS_TOKEN)
        _ts_pro = ts.pro_api()
    return _ts_pro


# ===== Redis 连接 =====

def _redis_client() -> redis.Redis | None:
    try:
        host, _, port_text = str(settings.tdx_redis_addr or "192.168.50.68:6379").partition(":")
        return redis.Redis(
            host=host or "192.168.50.68",
            port=int(port_text or 6379),
            db=int(settings.tdx_redis_db),
            password=settings.tdx_redis_password or None,
            decode_responses=True,
            socket_timeout=3.0,
            socket_connect_timeout=3.0,
        )
    except Exception:
        return None


# ===== parquet 数据读取 =====

def _read_latest_prices_from_parquet(trade_date: str) -> dict[str, float]:
    """从 parquet 历史逐笔成交中提取每只股票最后成交价。

    参数:
        trade_date: 交易日，格式 YYYYMMDD (如 20260723)

    返回:
        {symbol: last_price} — 每只股票当天最后一笔成交价
    """
    dt = datetime.strptime(trade_date, "%Y%m%d")
    parquet_path = _HISTORICAL_ROOT / str(dt.year) / f"{dt.month:02d}" / f"{dt.day:02d}.parquet"

    if not parquet_path.exists():
        logger.warning("Parquet not found: %s", parquet_path)
        return {}

    logger.info("Reading parquet: %s", parquet_path)
    df = pd.read_parquet(parquet_path, columns=["symbol", "price", "time_hhmmss", "trade_date"])

    if df.empty:
        return {}

    # 只用盘中数据 (09:30 - 15:00)，过滤集合竞价 09:25
    df = df[df["time_hhmmss"] >= 93000]

    # 按 symbol 分组，取每只股票最后一笔成交价
    last_prices = df.groupby("symbol")["price"].last()

    result: dict[str, float] = {}
    for sym, price in last_prices.items():
        result[sym] = float(price)

    logger.info("Parquet: %d stocks with closing prices", len(result))
    return result


# ===== Redis 实时价格（盘中） =====

def _parse_last_price(raw: str) -> float | None:
    """从 JSONL 原始数据中提取最后一笔有效成交价。"""
    try:
        lines = raw.strip().split("\n")
        obj = json.loads(lines[-1])
        price = obj.get("price")
        if price and float(price) > 0:
            return float(price)
    except (json.JSONDecodeError, KeyError, ValueError, IndexError):
        pass
    try:
        for line in reversed(raw.strip().split("\n")[-10:]):
            obj = json.loads(line)
            price = obj.get("price")
            if price and float(price) > 0:
                return float(price)
    except Exception:
        pass
    return None


def _read_latest_prices_from_redis(max_symbols: int = 3000, timeout_sec: float = 4.0) -> dict[str, float]:
    """从 Redis 快速采样股票最新成交价。

    数据量极大（22000+ 只 × ~500KB/只），全量读取不现实。
    采样前 N 只股票的最后一笔成交价，足以检测涨停。
    超时或失败返回空，由上层回退到 parquet。
    """
    import time as _time
    t_start = _time.time()

    client = _redis_client()
    if client is None:
        return {}

    all_symbols: list[str] = []
    try:
        for key in client.scan_iter(match="tdx:trans:*", count=2000):
            if _time.time() - t_start > 1.5:  # scan 限时 1.5s
                break
            symbol = key.split(":")[2]
            if not symbol.isdigit() or len(symbol) != 6:
                continue
            all_symbols.append(symbol)
            if len(all_symbols) >= max_symbols:
                break
    except Exception as exc:
        logger.warning("Redis scan failed: %s", exc)
        return {}

    if len(all_symbols) < 10:
        return {}

    logger.info("Redis: fetching %d symbols (%.1fs scan)", len(all_symbols), _time.time() - t_start)

    # Pipeline 批量读取
    pipe = client.pipeline(transaction=False)
    for sym in all_symbols:
        pipe.get(f"tdx:trans:{sym}")

    try:
        results = pipe.execute()
    except Exception as exc:
        logger.warning("Redis pipeline failed: %s", exc)
        return {}

    latest: dict[str, float] = {}
    for sym, raw in zip(all_symbols, results, strict=False):
        if _time.time() - t_start > timeout_sec:
            break
        if raw:
            price = _parse_last_price(raw)
            if price is not None:
                latest[sym] = price

    elapsed = _time.time() - t_start
    logger.info("Redis: %d prices parsed in %.1fs", len(latest), elapsed)
    return latest


def _read_realtime_quote_snapshot(quote_service: Any) -> tuple[list[dict], str | None]:
    """Read the already-populated full-market quote cache without triggering I/O."""
    if quote_service is None:
        return [], None
    try:
        frame, quote_date = quote_service.get_enriched_today()
    except Exception as exc:
        logger.warning("Realtime quote snapshot unavailable: %s", exc)
        return [], None
    if frame is None or frame.is_empty():
        return [], None

    columns = [
        column
        for column in ("symbol", "name", "close", "last_price", "prev_close")
        if column in frame.columns
    ]
    if "symbol" not in columns or "prev_close" not in columns:
        return [], str(quote_date).replace("-", "") if quote_date else None
    price_column = "close" if "close" in columns else "last_price"
    if price_column not in columns:
        return [], str(quote_date).replace("-", "") if quote_date else None

    raw_rows = frame.select(columns).to_dicts()
    symbols = [str(row.get("symbol") or "") for row in raw_rows]
    try:
        name_map = quote_service.get_name_map(symbols)
    except Exception as exc:
        logger.warning("Instrument name map unavailable: %s", exc)
        name_map = {}

    rows: list[dict] = []
    for row in raw_rows:
        symbol = str(row.get("symbol") or "")
        code = symbol.split(".", 1)[0]
        try:
            price = float(row.get(price_column) or 0)
            prev_close = float(row.get("prev_close") or 0)
        except (TypeError, ValueError):
            continue
        if len(code) != 6 or price <= 0 or prev_close <= 0:
            continue
        rows.append({
            "code": code,
            "name": row.get("name") or name_map.get(symbol) or code,
            "price": price,
            "prev_close": prev_close,
        })
    date_text = str(quote_date).replace("-", "") if quote_date else None
    return rows, date_text


# ===== 日期工具 =====

def _best_trade_date() -> str:
    """找一个有数据的交易日：优先今天，否则往回找有 parquet 的最近交易日。"""
    today = datetime.now()

    # 尝试最近 7 天，找有 parquet 文件的最近日期
    for offset in range(7):
        dt = today - timedelta(days=offset)
        d_str = dt.strftime("%Y%m%d")
        parquet_path = _HISTORICAL_ROOT / str(dt.year) / f"{dt.month:02d}" / f"{dt.day:02d}.parquet"
        logger.info("_best_trade_date: checking %s → %s", d_str, parquet_path.exists())
        if parquet_path.exists():
            logger.info("_best_trade_date: found %s", d_str)
            return d_str

    logger.warning("_best_trade_date: no parquet found, returning today")
    return today.strftime("%Y%m%d")


# ===== 涨停检测 & 概念映射 =====

def _get_limit_pct(code: str) -> float:
    """根据股票代码判断涨停幅度。"""
    if code.startswith("688"):
        return 20.0
    return STOCK_LIMITS.get(code[0], 10) if code else 10.0


def _load_ths_members() -> pd.DataFrame:
    """加载同花顺概念成分股。"""
    if not _THS_MEMBERS_PATH.exists():
        logger.warning("THS members file not found: %s", _THS_MEMBERS_PATH)
        return pd.DataFrame()

    dfc = pd.read_csv(_THS_MEMBERS_PATH)
    dfc["code"] = dfc["code"].astype(str).str.zfill(6)
    dfc = dfc[dfc["cnpt_code"].astype(str).str.startswith(("886", "885", "881"))]
    dfc = dfc[["ths_concept", "code", "cnpt_code"]]
    dfc = dfc[~dfc["ths_concept"].isin(_GENERIC_CONCEPTS)]
    return dfc


def _build_treemap(
    df: pd.DataFrame,
    on_progress: ProgressCallback = _noop_progress,
) -> list[dict]:
    """将涨停股票 DataFrame 转为 ECharts treemap 格式。"""
    on_progress("concepts", 72, "加载同花顺概念成员映射")
    dfc = _load_ths_members()

    if dfc.empty:
        result: list[dict] = []
        for _, row in df.iterrows():
            pct_str = f"({row['涨跌幅']}%)" if pd.notna(row.get("涨跌幅")) else ""
            result.append({
                "name": f"{row['股票名称']}{pct_str}",
                "value": 1,
            })
        return result

    on_progress("concepts", 82, f"匹配 {len(df)} 只涨停股票的概念")
    df = df.merge(dfc, how="left", left_on="股票代码", right_on="code")

    grouped = df.groupby("ths_concept").agg(
        count=("股票名称", "count"),
        stocks=("股票名称", lambda x: list(x)),
        codes=("股票代码", lambda x: list(x)),
    ).reset_index()
    grouped = grouped.sort_values(["count", "ths_concept"], ascending=[False, True]).head(10)

    treemap_data: list[dict] = []
    for _, row in grouped.iterrows():
        concept_name = row["ths_concept"] if pd.notna(row["ths_concept"]) else "其他"
        children = [
            {"name": name, "code": code, "value": 1}
            for name, code in zip(row["stocks"], row["codes"], strict=False)
        ]
        treemap_data.append({
            "name": concept_name,
            "value": int(row["count"]),
            "children": children,
        })

    return treemap_data


def _detect_limit_ups_from_quotes(rows: list[dict]) -> list[dict]:
    """Detect limit-ups from the current quote snapshot (price + prev_close)."""
    detected: list[dict] = []
    for row in rows:
        code = str(row["code"])
        price = float(row["price"])
        prev_close = float(row["prev_close"])
        pct_chg = (price - prev_close) / prev_close * 100
        if pct_chg >= _get_limit_pct(code) * 0.98:
            detected.append({
                "股票代码": code,
                "最新价": round(price, 2),
                "涨跌幅": round(pct_chg, 2),
                "股票名称": row.get("name") or code,
            })
    return detected


# ===== 核心：从价格数据检测涨停 =====

def _detect_limit_ups(
    prices: dict[str, float],
    trade_date: str,
    on_progress: ProgressCallback = _noop_progress,
) -> tuple[list[dict], str | None]:
    """通用涨停检测：给定 {symbol: price} 映射和交易日，返回涨停股票列表。

    返回: (行列表, 错误信息)
    """
    if len(prices) < 10:
        return [], f"价格数据不足 (仅 {len(prices)} 只股票)"

    # 获取昨收价
    try:
        on_progress("preclose", 48, f"加载 {trade_date} 的昨收数据")
        pro = _get_ts_pro()
        df_daily = pro.daily(trade_date=trade_date)
        if df_daily is None or len(df_daily) == 0:
            return [], f"tushare daily 无 {trade_date} 数据"

        preclose_map: dict[str, float] = {}
        name_map: dict[str, str] = {}
        for _, row in df_daily.iterrows():
            code = row["ts_code"].replace(".SZ", "").replace(".SH", "").replace(".BJ", "")
            pre_close = float(row.get("pre_close", 0) or 0)
            if pre_close > 0:
                preclose_map[code] = pre_close
            name_map[code] = row.get("name", code)
        logger.info("Preclose map: %d stocks from tushare daily %s", len(preclose_map), trade_date)
    except Exception as exc:
        logger.warning("Failed to get preclose: %s", exc)
        return [], f"tushare 昨收数据获取失败: {exc}"

    if not preclose_map:
        return [], "昨收数据为空"

    rows: list[dict] = []
    on_progress("detect", 62, f"识别 {len(prices)} 只股票中的涨停股")
    for code, price in prices.items():
        preclose = preclose_map.get(code)
        if preclose and preclose > 0:
            pct_chg = (price - preclose) / preclose * 100
            limit_pct = _get_limit_pct(code)
            if pct_chg >= limit_pct * 0.98:
                rows.append({
                    "股票代码": code,
                    "最新价": round(price, 2),
                    "涨跌幅": round(pct_chg, 2),
                    "股票名称": name_map.get(code, code),
                })

    if not rows:
        return [], f"{trade_date} 未检测到涨停股票"

    return rows, None


# ===== 主入口：parquet 优先 → Redis 回退 =====

def build_treemap_data(
    trade_date: str | None = None,
    quote_service: Any = None,
    on_progress: ProgressCallback = _noop_progress,
) -> dict:
    """构建 treemap 数据。今日优先实时行情缓存，历史日期读取 parquet。

    返回: 完整 API 响应的 dict（不含缓存层）
    """
    on_progress("prepare", 3, "准备热门概念计算")
    requested_date = trade_date if isinstance(trade_date, str) and len(trade_date) == 8 else None
    today = datetime.now().strftime("%Y%m%d")

    prices: dict[str, float] = {}
    source = "none"
    warning = None
    rows: list[dict] = []
    realtime_available = False

    # 今日数据直接复用 QuoteService 已有的全市场快照，不读取巨大 Redis 逐笔值。
    if requested_date in (None, today):
        on_progress("realtime", 12, "读取全市场实时行情快照")
        quote_rows, quote_date = _read_realtime_quote_snapshot(quote_service)
        if quote_date == today and len(quote_rows) >= 10:
            realtime_available = True
            trade_date = today
            source = "realtime_quotes"
            on_progress("detect", 42, f"识别 {len(quote_rows)} 只实时股票中的涨停股")
            rows = _detect_limit_ups_from_quotes(quote_rows)

    if not realtime_available:
        # 明确历史日期或今日实时快照尚未就绪时，回退最近的完整交易日。
        trade_date = requested_date or _best_trade_date()
        if requested_date is None and trade_date != today:
            warning = f"今日实时行情暂不可用，已回退到最近交易日 {trade_date}"
        on_progress("parquet", 25, f"读取 {trade_date} 的逐笔成交文件")
        prices = _read_latest_prices_from_parquet(trade_date)
        source = "parquet"

    if not realtime_available and len(prices) < 10:
        return {
            "trade_date": trade_date,
            "unique_stocks": 0,
            "concept_count": 0,
            "treemap_pairs": 0,
            "treemap_data": [],
            "source": "none",
            "warning": f"无可用数据（Redis 和 parquet 均不足，仅 {len(prices)} 只股票）",
        }

    # 历史数据需要另取昨收后检测涨停；实时快照已经包含 prev_close。
    err = None
    if not realtime_available:
        rows, err = _detect_limit_ups(prices, trade_date, on_progress)
    if err or not rows:
        return {
            "trade_date": trade_date,
            "unique_stocks": 0,
            "concept_count": 0,
            "treemap_pairs": 0,
            "treemap_data": [],
            "source": source,
            "warning": err or warning,
        }

    on_progress("concepts", 68, f"为 {len(rows)} 只涨停股票生成概念分布")
    df = pd.DataFrame(rows)
    unique_stocks = len(df)
    treemap_data = _build_treemap(df, on_progress)
    on_progress("finalize", 94, "整理热门概念图数据")

    return {
        "trade_date": trade_date,
        "unique_stocks": unique_stocks,
        "concept_count": len(treemap_data),
        "treemap_pairs": sum(d["value"] for d in treemap_data),  # 概念-股票对总数（有重复）
        "treemap_data": treemap_data,
        "source": source,
        "warning": warning,
    }


# ===== 缓存与按需后台任务 =====

_cache: dict = {"data": None, "ts": datetime.min, "key": None}


def _cached_treemap_data(
    trade_date: str | None = None,
    quote_service: Any = None,
) -> dict:
    global _cache
    now = datetime.now()
    key = trade_date or now.strftime("%Y%m%d")
    cached = _cache["data"]
    cache_is_current = not (
        trade_date is None
        and cached is not None
        and cached.get("trade_date") != key
    )
    if (
        cached is not None
        and _cache["key"] == key
        and cache_is_current
        and (now - _cache["ts"]).total_seconds() < 900
    ):
        return _cache["data"]
    data = build_treemap_data(trade_date, quote_service)
    _cache = {"data": data, "ts": now, "key": key}
    return data


_job_lock = threading.Lock()
_job_task: asyncio.Task | None = None
_job: dict[str, Any] = {
    "status": "idle",
    "stage": "idle",
    "progress": 0,
    "message": "进入热门页面后开始计算",
    "started_at": None,
    "finished_at": None,
    "duration_s": None,
    "log": [],
    "data": None,
    "error": None,
}


def _job_snapshot() -> dict[str, Any]:
    with _job_lock:
        return {
            **_job,
            "log": list(_job["log"]),
        }


def _set_job_progress(stage: str, progress: int, message: str) -> None:
    with _job_lock:
        _job.update({
            "stage": stage,
            "progress": max(0, min(100, int(progress))),
            "message": message,
        })
        if not _job["log"] or _job["log"][-1]["message"] != message:
            _job["log"].append({
                "stage": stage,
                "progress": int(progress),
                "message": message,
                "at": datetime.now().isoformat(timespec="seconds"),
            })
            _job["log"] = _job["log"][-12:]


async def _run_hot_concepts_job(trade_date: str | None, quote_service: Any) -> None:
    global _cache
    started = time.perf_counter()
    try:
        data = await asyncio.to_thread(
            build_treemap_data,
            trade_date,
            quote_service,
            _set_job_progress,
        )
        cache_key = trade_date or datetime.now().strftime("%Y%m%d")
        _cache = {"data": data, "ts": datetime.now(), "key": cache_key}
        with _job_lock:
            _job.update({
                "status": "succeeded",
                "stage": "done",
                "progress": 100,
                "message": f"完成：{data['unique_stocks']} 只涨停，{data['concept_count']} 个概念",
                "finished_at": datetime.now().isoformat(timespec="seconds"),
                "duration_s": round(time.perf_counter() - started, 2),
                "data": data,
            })
    except Exception as exc:
        logger.exception("Hot concepts background job failed")
        with _job_lock:
            _job.update({
                "status": "failed",
                "stage": "failed",
                "message": "热门概念计算失败",
                "finished_at": datetime.now().isoformat(timespec="seconds"),
                "duration_s": round(time.perf_counter() - started, 2),
                "error": str(exc),
            })


# ===== API 端点 =====

@router.get("/treemap")
def hot_concepts_treemap(
    request: Request,
    trade_date: str | None = Query(None, description="交易日期 YYYYMMDD，默认最近交易日"),
    refresh: bool = Query(False, description="跳过缓存强制刷新"),
):
    """返回热门概念板块 treemap 数据，供前端 ECharts 渲染。

    优先 parquet 历史数据，盘中自动切换 Redis 实时数据。
    """
    quote_service = getattr(request.app.state, "quote_service", None)
    if refresh:
        _cache["ts"] = datetime.min
        return build_treemap_data(trade_date, quote_service)
    return _cached_treemap_data(trade_date, quote_service)


@router.post("/treemap/jobs")
async def start_hot_concepts_job(
    request: Request,
    trade_date: str | None = Query(None, description="交易日期 YYYYMMDD，默认今天"),
    refresh: bool = Query(False, description="强制重新计算"),
):
    """Start the on-demand calculation and return immediately."""
    global _job_task
    with _job_lock:
        if _job["status"] == "running":
            return {**_job, "log": list(_job["log"])}

        today_key = datetime.now().strftime("%Y%m%d")
        cache_key = trade_date or today_key
        cached = _cache["data"]
        cache_valid = (
            not refresh
            and cached is not None
            and _cache["key"] == cache_key
            and cached.get("trade_date") == cache_key
            and (datetime.now() - _cache["ts"]).total_seconds() < 900
        )
        if cache_valid:
            _job.update({
                "status": "succeeded",
                "stage": "done",
                "progress": 100,
                "message": f"已使用缓存：{cached['unique_stocks']} 只涨停，{cached['concept_count']} 个概念",
                "data": cached,
                "error": None,
            })
            return {**_job, "log": list(_job["log"])}

        _job.update({
            "status": "running",
            "stage": "prepare",
            "progress": 1,
            "message": "启动热门概念计算",
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "finished_at": None,
            "duration_s": None,
            "log": [],
            "data": None,
            "error": None,
        })

    quote_service = getattr(request.app.state, "quote_service", None)
    _job_task = asyncio.create_task(_run_hot_concepts_job(trade_date, quote_service))
    return _job_snapshot()


@router.get("/treemap/jobs/current")
def current_hot_concepts_job():
    """Return current progress without starting any work."""
    return _job_snapshot()
