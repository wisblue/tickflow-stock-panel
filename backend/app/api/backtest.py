"""回测 API — 信号回测 + 因子回测 + 策略回测。"""
from __future__ import annotations

import asyncio
import json
import math
import queue
import re
import subprocess
import threading
import time
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.config import settings
from app.services.backtest import (
    BacktestConfig,
    BacktestService,
    VectorbtUnavailable,
    is_available,
)

router = APIRouter(prefix="/api/backtest", tags=["backtest"])

FACTOR_DEFAULT_DAYS = 180
STRATEGY_DEFAULT_DAYS = 365 * 3
BACKTEST_MAX_SERVER_DAYS = 186
FACTOR_MAX_SYMBOLS = 1000
BACKTEST_SERVER_GUARD_MESSAGE = (
    "当前服务器内存约 1.8GB，回测区间最多支持 6 个月；"
    "更长周期容易触发 OOM，建议在 8GB 以上内存环境或本机运行。"
)
S150_LIVE_ROOT = Path("/home/dennis/re_3/codex/prediction/Models/limit-up")
S150_STATE_FILE = S150_LIVE_ROOT / "state" / "s150_live_state.json"
S150_RUN_ROOT = S150_LIVE_ROOT / "runs" / "s150_live"
SR004_EVAL_SCRIPT = S150_LIVE_ROOT / "scripts" / "evaluate_sr004_realtime_exit.py"
SR004_WORKDIR = Path("/home/dennis/re_3/codex/prediction")
SR004_PYTHON = Path("/home/dennis/anaconda3/envs/re_3/bin/python")
S150_PRIORITY_SCRIPT = S150_LIVE_ROOT / "scripts" / "run_s150_as_priority_1445.sh"
S150_SR004_SCRIPT = S150_LIVE_ROOT / "scripts" / "run_s150_sr004_live.py"
S150_RUNBOOK = S150_LIVE_ROOT / "reports" / "s150_live_runbook.md"
S150_PRIORITY_LOG_DIR = S150_LIVE_ROOT / "logs" / "s150_as_priority"
GO_FETCHER_ROOT = Path("/home/dennis/re_3/github/go-fetcher")
GO_FETCHER_LOG_DIR = GO_FETCHER_ROOT / "logs"
GO_FETCHER_ACTIVE_SYMBOLS = Path("/home/dennis/re_3/github/tickflow-stock-panel/data/user_data/active_symbols.txt")
TDX_REDIS_ADDR = "192.168.50.68:6379"
TDX_REDIS_DB = 15
TDX_REDIS_PREFIX = "tdx:trans"


def _get_engine(request: Request):
    """获取或创建 BacktestEngine (单例，PanelCache 跨请求生效)。"""
    from app.backtest.engine import BacktestEngine
    engine = getattr(request.app.state, "backtest_engine", None)
    if engine is None:
        engine = BacktestEngine(request.app.state.repo)
        request.app.state.backtest_engine = engine
    return engine


def _resolve_start(req: BaseModel, end: date, default_days: int) -> date:
    """未传 start 使用默认区间；显式传 null/空值表示全部历史。"""
    start = getattr(req, "start")
    if start is not None:
        return start
    if "start" in req.model_fields_set:
        return date(1900, 1, 1)
    return end - timedelta(days=default_days)


def _guard_server_backtest_range(start: date, end: date):
    if not settings.backtest_range_guard:
        return
    days = (end - start).days + 1
    if days > BACKTEST_MAX_SERVER_DAYS:
        raise HTTPException(status_code=400, detail=BACKTEST_SERVER_GUARD_MESSAGE)


def _s150_date(value: Any) -> str:
    raw = str(value or "").strip().replace("-", "").replace("/", "")
    if raw.endswith(".0"):
        raw = raw[:-2]
    return re.sub(r"\D", "", raw)[:8]


def _s150_stock(value: Any) -> str:
    raw = str(value or "").strip()
    if raw.endswith(".0"):
        raw = raw[:-2]
    digits = re.sub(r"\D", "", raw)
    return digits.zfill(6)[-6:] if digits else ""


def _finite_float(value: Any) -> float | None:
    try:
        out = float(value)
    except Exception:
        return None
    return out if math.isfinite(out) else None


def _load_s150_state() -> dict[str, Any]:
    if not S150_STATE_FILE.exists():
        return {}
    try:
        return json.loads(S150_STATE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"S150 state read failed: {exc}") from exc


def _latest_s150_manifest() -> dict[str, Any] | None:
    if not S150_RUN_ROOT.exists():
        return None
    manifests: list[tuple[str, float, Path, dict[str, Any]]] = []
    for path in S150_RUN_ROOT.glob("*_asof*/manifest.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(payload.get("model_name", "") or "") != "s150_sr004_live_v1":
            continue
        trade_date = _s150_date(payload.get("trade_date", ""))
        if not trade_date:
            continue
        manifests.append((trade_date, path.stat().st_mtime, path, payload))
    if not manifests:
        return None
    manifests.sort(key=lambda item: (item[0], item[1]), reverse=True)
    payload = dict(manifests[0][3])
    payload["_manifest_path"] = str(manifests[0][2])
    payload["_manifest_mtime"] = datetime.fromtimestamp(
        manifests[0][2].stat().st_mtime,
        tz=ZoneInfo("Asia/Shanghai"),
    ).isoformat()
    return payload


def _resolve_s150_request_date(value: str | None) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if text in {"today", "current"}:
        return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
    return _s150_date(text)


def _s150_manifest_for_date(trade_date: str) -> dict[str, Any] | None:
    if not trade_date:
        return None
    path = S150_RUN_ROOT / f"{trade_date}_asof1445" / "manifest.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"S150 manifest read failed: {exc}") from exc
    if str(payload.get("model_name", "") or "") != "s150_sr004_live_v1":
        return None
    payload = dict(payload)
    payload["_manifest_path"] = str(path)
    payload["_manifest_mtime"] = datetime.fromtimestamp(
        path.stat().st_mtime,
        tz=ZoneInfo("Asia/Shanghai"),
    ).isoformat()
    return payload


def _s150_name_map(request: Request, symbols: list[str]) -> dict[str, str]:
    repo = getattr(request.app.state, "repo", None)
    if repo is None or not symbols:
        return {}
    out: dict[str, str] = {}
    try:
        import polars as pl

        df = repo.get_instruments()
        if not df.is_empty() and "name" in df.columns:
            wanted = set(symbols)
            code_expr = (
                pl.col("code").cast(pl.Utf8)
                if "code" in df.columns
                else pl.col("symbol").cast(pl.Utf8).str.extract(r"(\d+)", 1)
            )
            hits = (
                df.with_columns(code_expr.str.zfill(6).alias("_s150_code"))
                .filter(pl.col("_s150_code").is_in(wanted))
                .select(["_s150_code", "name"])
                .to_dicts()
            )
            for row in hits:
                out.setdefault(str(row.get("_s150_code", "")), str(row.get("name", "") or ""))
    except Exception:
        pass
    missing = [symbol for symbol in symbols if symbol not in out]
    if missing:
        try:
            out.update(repo.get_name_map(missing))
        except Exception:
            pass
    return out


def _s150_trade_rows(state: dict[str, Any], name_map: dict[str, str], limit: int = 20) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cumulative = 0.0
    active = [
        rec
        for rec in state.get("s146_history", [])
        if int(rec.get("n_positions", 0) or 0) > 0 and _s150_stock(rec.get("selected_stock_code", ""))
    ]
    active = sorted(active, key=lambda rec: _s150_date(rec.get("trade_date", "")))
    for rec in active:
        day_ret = _finite_float(rec.get("day_ret"))
        if day_ret is not None:
            cumulative = (1.0 + cumulative) * (1.0 + day_ret) - 1.0
        code = _s150_stock(rec.get("selected_stock_code", ""))
        rows.append(
            {
                "index": len(rows) + 1,
                "date": _s150_date(rec.get("trade_date", "")),
                "stock_code": code,
                "stock_name": name_map.get(code, ""),
                "buy_price": _finite_float(rec.get("buy_price")),
                "sell_price": _finite_float(rec.get("sell_price")),
                "day_return": day_ret,
                "cumulative_return": cumulative if day_ret is not None else None,
                "settlement_status": str(rec.get("settlement_status", "")),
                "exit_date": _s150_date(rec.get("exit_date", "")),
                "exit_time_hhmm": int(rec.get("exit_time_hhmm")) if rec.get("exit_time_hhmm") is not None else None,
                "exit_reason": str(rec.get("exit_reason", "")),
            }
        )
    tail = rows[-limit:]
    start_index = max(0, len(rows) - len(tail))
    for offset, row in enumerate(tail, start=1):
        row["index"] = start_index + offset
    return tail


# ================================================================
# 状态
# ================================================================

@router.get("/status")
def status():
    """前端可用此接口判断回测页是否要灰显。"""
    return {"available": True}


def _empty_s150_response(trade_date: str, message: str) -> dict[str, Any]:
    checked_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
    return {
        "available": False,
        "message": message,
        "trade_date": trade_date,
        "generated_at": "",
        "data_updated_at": "",
        "checked_at": checked_at,
        "status": "missing",
        "final_action": "",
        "sell_rule_contract": "SR004_profit_trailing_latest1430",
        "elapsed_sec": None,
        "within_latency_budget": False,
        "recommendation": {
            "stock_code": "",
            "stock_name": "",
            "buy_price": None,
            "buy_price_source": "",
            "text": "今日14:45推荐：暂无",
        },
        "upstream": {"stock_code": "", "stock_name": "", "action": ""},
        "avg_day_return": None,
        "trade_count": 0,
        "settled_trade_count": 0,
        "source": {"state_file": str(S150_STATE_FILE), "manifest_path": ""},
        "update_rule": "每个交易日 14:46 以后读取 S150-SR004 预测结果。",
        "trades": [],
    }


def _status_item(
    key: str,
    label: str,
    status: str,
    message: str,
    detail: dict[str, Any] | None = None,
    fixable: bool = False,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "status": status,
        "message": message,
        "detail": detail or {},
        "fixable": bool(fixable),
    }


def _overall_status(items: list[dict[str, Any]]) -> str:
    statuses = {str(item.get("status", "")) for item in items}
    if "fail" in statuses:
        return "fail"
    if "warn" in statuses:
        return "warn"
    if "pending" in statuses:
        return "pending"
    return "ok"


def _tail_text(path: Path, max_bytes: int = 50000) -> str:
    if not path.exists() or not path.is_file():
        return ""
    size = path.stat().st_size
    with path.open("rb") as fh:
        if size > max_bytes:
            fh.seek(size - max_bytes)
        data = fh.read()
    return data.decode("utf-8", errors="ignore")


def _latest_realtime_log_timestamp(trade_date: str) -> tuple[Path | None, datetime | None, str]:
    candidates = sorted(GO_FETCHER_LOG_DIR.glob("realtime_redis_*.log"), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    today_log = GO_FETCHER_LOG_DIR / f"realtime_redis_{trade_date}.log"
    if today_log.exists():
        candidates.insert(0, today_log)
    pattern = re.compile(r"(20\d{2})/(\d{2})/(\d{2})\s+(\d{2}:\d{2}:\d{2})")
    for path in dict.fromkeys(candidates):
        text = _tail_text(path)
        matches = list(pattern.finditer(text))
        if not matches:
            continue
        match = matches[-1]
        dt = datetime.strptime(
            f"{match.group(1)}{match.group(2)}{match.group(3)} {match.group(4)}",
            "%Y%m%d %H:%M:%S",
        ).replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        return path, dt, text.splitlines()[-1] if text.splitlines() else ""
    return None, None, ""


def _process_lines() -> list[str]:
    try:
        proc = subprocess.run(
            ["ps", "-eo", "pid,lstart,args"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except Exception:
        return []
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _fake_fetcher_processes() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in _process_lines():
        if "go-fetcher" not in line or "s150fake" not in line:
            continue
        parts = line.strip().split(maxsplit=1)
        try:
            pid = int(parts[0])
        except Exception:
            continue
        out.append({"pid": pid, "cmd": line})
    return out


def _prod_fetcher_processes() -> list[str]:
    return [
        line for line in _process_lines()
        if ("go-fetcher" in line or "run_realtime_redis.sh" in line)
        and "--redis-key-prefix tdx:trans" in line
        and "--redis-key-prefix tdx:trans:" not in line
        and "--realtime" in line
    ]


def _crontab_text() -> str:
    try:
        proc = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=3, check=False)
    except Exception:
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def _read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _active_symbols_sample(limit: int = 10) -> list[str]:
    if not GO_FETCHER_ACTIVE_SYMBOLS.exists():
        return []
    symbols: list[str] = []
    for raw in re.split(r"[\s,]+", GO_FETCHER_ACTIVE_SYMBOLS.read_text(encoding="utf-8", errors="ignore")):
        digits = re.sub(r"\D", "", raw)
        if len(digits) >= 6:
            symbols.append(digits[-6:].zfill(6))
        if len(symbols) >= limit:
            break
    return symbols


def _hhmm(dt: datetime) -> int:
    return dt.hour * 100 + dt.minute


def _requires_fresh_intraday_tick(now: datetime) -> bool:
    value = _hhmm(now)
    return 925 <= value <= 1135 or 1300 <= value <= 1535


def _same_day_status_for_timestamp(now: datetime, trade_date: str, ts: datetime | None, fresh_age_sec = 300) -> tuple[str, str, float | None]:
    if ts is None:
        return "fail", "未找到时间戳", None
    age = (now - ts).total_seconds()
    if ts.strftime("%Y%m%d") != trade_date:
        if _hhmm(now) < 925:
            return "pending", "等待今日开盘数据", age
        return "fail", "未发现今日数据", age
    if _requires_fresh_intraday_tick(now) and age > fresh_age_sec:
        return "warn", f"交易时段内超过 {int(fresh_age_sec / 60)} 分钟未更新", age
    return "ok", "今日数据正常", age


def _redis_runtime_item(now: datetime, trade_date: str) -> dict[str, Any]:
    try:
        import redis
    except Exception:
        return _status_item("redis_data", "Redis 数据", "warn", "backend 未安装 redis 模块，跳过 Redis 新鲜度检测")
    host, _, port_text = TDX_REDIS_ADDR.partition(":")
    try:
        client = redis.Redis(
            host=host or "localhost",
            port=int(port_text or "6379"),
            db=TDX_REDIS_DB,
            socket_connect_timeout=0.3,
            socket_timeout=0.8,
            decode_responses=True,
        )
        client.ping()
        sample = _active_symbols_sample()
        checked = 0
        hits = 0
        latest_dt: datetime | None = None
        for symbol in sample:
            checked += 1
            raw = client.get(f"{TDX_REDIS_PREFIX}:{symbol}")
            if not raw:
                continue
            hits += 1
            for line in reversed(raw.splitlines()):
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                ts = rec.get("timestamp")
                if ts is None:
                    continue
                dt = datetime.fromtimestamp(float(ts), ZoneInfo("Asia/Shanghai"))
                if latest_dt is None or dt > latest_dt:
                    latest_dt = dt
                break
        if not sample:
            return _status_item("redis_data", "Redis 数据", "warn", "active_symbols.txt 为空，无法抽样检测 Redis 数据")
        if hits == 0:
            return _status_item("redis_data", "Redis 数据", "fail", "active symbols 抽样没有 Redis 数据", {"checked": checked})
        status, reason, age = _same_day_status_for_timestamp(now, trade_date, latest_dt)
        msg = f"抽样 {checked} 只，命中 {hits} 只，最新 {latest_dt.strftime('%H:%M:%S') if latest_dt else '未知'}，{reason}"
        return _status_item(
            "redis_data",
            "Redis 数据",
            status,
            msg,
            {"checked": checked, "hits": hits, "latest_at": latest_dt.isoformat() if latest_dt else "", "age_sec": age},
        )
    except Exception as exc:  # noqa: BLE001
        return _status_item("redis_data", "Redis 数据", "fail", f"Redis 连接/读取失败: {exc}")


def _build_s150_runtime_status(trade_date: str) -> dict[str, Any]:
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    out = S150_RUN_ROOT / f"{trade_date}_asof1445"
    manifest_path = out / "manifest.json"
    prediction_path = out / "s150_sr004_prediction.parquet"
    goal_path = out / "s150_sr004_goal_status.json"
    items: list[dict[str, Any]] = []

    items.append(_status_item(
        "runbook",
        "Runbook",
        "ok" if S150_RUNBOOK.exists() else "fail",
        "已找到 S150/SR004 14:45 Live Runbook" if S150_RUNBOOK.exists() else f"缺少 runbook: {S150_RUNBOOK}",
        {"path": str(S150_RUNBOOK)},
    ))

    cron = _crontab_text()
    items.append(_status_item(
        "cron_go_fetcher",
        "go-fetcher cron",
        "ok" if "run_realtime_redis.sh" in cron and "persist_realtime_redis_snapshot.sh" in cron else "fail",
        "09:26 realtime 与 15:35 persist cron 已安装" if "run_realtime_redis.sh" in cron and "persist_realtime_redis_snapshot.sh" in cron else "缺少 go-fetcher realtime/persist cron",
    ))
    items.append(_status_item(
        "cron_s150_priority",
        "S150 priority cron",
        "ok" if "run_s150_as_priority_1445.sh" in cron else "fail",
        "14:20 S150 A/S priority cron 已安装" if "run_s150_as_priority_1445.sh" in cron else "缺少 S150 A/S priority cron",
    ))
    items.append(_status_item(
        "s150_log_dir",
        "S150 日志目录",
        "ok" if S150_PRIORITY_LOG_DIR.exists() else "fail",
        f"日志目录存在: {S150_PRIORITY_LOG_DIR}" if S150_PRIORITY_LOG_DIR.exists() else f"日志目录缺失: {S150_PRIORITY_LOG_DIR}",
        {"path": str(S150_PRIORITY_LOG_DIR)},
        fixable=not S150_PRIORITY_LOG_DIR.exists(),
    ))

    ps_lines = _process_lines()
    prod_fetcher = _prod_fetcher_processes()
    fake_fetcher = [proc["cmd"] for proc in _fake_fetcher_processes()]
    items.append(_status_item(
        "go_fetcher_process",
        "go-fetcher 生产进程",
        "ok" if prod_fetcher else "fail",
        f"生产 realtime fetcher 正在运行，进程数 {len(prod_fetcher)}" if prod_fetcher else "未发现生产 go-fetcher realtime 进程",
        {"process_count": len(prod_fetcher)},
        fixable=not prod_fetcher,
    ))
    if fake_fetcher:
        items.append(_status_item(
            "fake_replay_process",
            "fake replay 进程",
            "warn",
            f"发现 fake replay go-fetcher 进程 {len(fake_fetcher)} 个，检测时已排除",
            {"process_count": len(fake_fetcher)},
            fixable=True,
        ))
    else:
        items.append(_status_item("fake_replay_process", "fake replay 进程", "ok", "未发现 fake replay go-fetcher 进程"))

    log_path, log_dt, log_tail = _latest_realtime_log_timestamp(trade_date)
    if log_dt is None:
        items.append(_status_item("go_fetcher_log", "实时日志", "fail", "未找到 go-fetcher realtime 日志时间戳"))
    else:
        log_status, reason, age = _same_day_status_for_timestamp(now, trade_date, log_dt)
        msg = f"最新日志 {log_dt.strftime('%H:%M:%S')} ({log_path.name if log_path else 'unknown'})，{reason}"
        items.append(_status_item(
            "go_fetcher_log",
            "实时日志",
            log_status,
            msg,
            {"path": str(log_path) if log_path else "", "latest_at": log_dt.isoformat(), "age_sec": age, "tail": log_tail},
        ))

    items.append(_redis_runtime_item(now, trade_date))

    s150_log = S150_PRIORITY_LOG_DIR / f"s150_as_priority_{trade_date}.log"
    if now.hour < 14 or (now.hour == 14 and now.minute < 20):
        s150_stage_status = "pending"
        s150_stage_msg = "14:20 S150 priority cron 尚未到启动时间"
    elif s150_log.exists():
        s150_stage_status = "ok"
        s150_stage_msg = f"已发现今日 S150 priority 日志: {s150_log.name}"
    else:
        s150_stage_status = "fail"
        s150_stage_msg = f"14:20 后仍未发现今日 S150 priority 日志: {s150_log}"
    items.append(_status_item("s150_priority_log", "S150 今日启动", s150_stage_status, s150_stage_msg, {"path": str(s150_log)}))

    if manifest_path.exists() and prediction_path.exists():
        artifact_status = "ok"
        artifact_msg = "今日 manifest 与 prediction 已产出"
    elif now.hour < 14 or (now.hour == 14 and now.minute < 46):
        artifact_status = "pending"
        artifact_msg = "14:46 前今日预测结果可未产出"
    else:
        artifact_status = "fail"
        artifact_msg = "14:46 后今日 manifest/prediction 仍缺失"
    items.append(_status_item(
        "s150_artifacts",
        "今日预测产物",
        artifact_status,
        artifact_msg,
        {"manifest": str(manifest_path), "prediction": str(prediction_path)},
    ))

    goal = _read_json_file(goal_path)
    if goal:
        goal_status = "ok" if goal.get("complete") and goal.get("latency_ok") else "fail"
        goal_msg = f"goal-status complete={goal.get('complete')} latency_ok={goal.get('latency_ok')} status={goal.get('status', '')}"
    elif now.hour < 14 or (now.hour == 14 and now.minute < 46):
        goal_status = "pending"
        goal_msg = "14:46 前 goal-status 可未生成"
    else:
        goal_status = "fail"
        goal_msg = "14:46 后 goal-status 缺失"
    items.append(_status_item("s150_goal_status", "S150 goal-status", goal_status, goal_msg, {"path": str(goal_path), "payload": goal}, fixable=manifest_path.exists()))

    return {
        "trade_date": trade_date,
        "checked_at": now.isoformat(),
        "overall_status": _overall_status(items),
        "items": items,
        "fixable_count": sum(1 for item in items if item.get("fixable")),
        "runbook_path": str(S150_RUNBOOK),
    }


@router.get("/s150-sr004")
def s150_sr004(request: Request, response: Response, trade_date: str | None = None):
    """S150-SR004 live recommendation and settled daily trade ledger."""
    response.headers["Cache-Control"] = "no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    checked_at = now.isoformat()
    requested_trade_date = _resolve_s150_request_date(trade_date)
    state = _load_s150_state()
    fallback_from_trade_date = ""
    latest_manifest = _s150_manifest_for_date(requested_trade_date) if requested_trade_date else _latest_s150_manifest()
    if requested_trade_date and latest_manifest is None:
        if requested_trade_date == now.strftime("%Y%m%d"):
            latest_manifest = _latest_s150_manifest()
            fallback_from_trade_date = requested_trade_date
        if latest_manifest is None:
            return _empty_s150_response(
                requested_trade_date,
                f"尚未找到 {requested_trade_date} 14:45 S150-SR004 生产结果。",
            )

    selected = ""
    latest_trade_date = ""
    latest_generated_at = ""
    latest_data_updated_at = ""
    manifest_path = ""
    buy_price: float | None = None
    status = "missing"
    if latest_manifest:
        selected = _s150_stock(latest_manifest.get("selected_stock_code", ""))
        latest_trade_date = _s150_date(latest_manifest.get("trade_date", ""))
        latest_generated_at = str(latest_manifest.get("generated_at", "") or "")
        latest_data_updated_at = str(latest_manifest.get("_manifest_mtime", "") or latest_generated_at)
        manifest_path = str(latest_manifest.get("_manifest_path", "") or "")
        buy_price = _finite_float(latest_manifest.get("buy_price"))
        status = str(latest_manifest.get("status", "") or "")
    elif state.get("s146_history"):
        latest_state = sorted(state.get("s146_history", []), key=lambda rec: _s150_date(rec.get("trade_date", "")))[-1]
        selected = _s150_stock(latest_state.get("selected_stock_code", ""))
        latest_trade_date = _s150_date(latest_state.get("trade_date", ""))
        buy_price = _finite_float(latest_state.get("buy_price"))
        status = str(latest_state.get("settlement_status", "") or "state_only")

    all_symbols = {
        _s150_stock(rec.get("selected_stock_code", ""))
        for rec in state.get("s146_history", [])
        if _s150_stock(rec.get("selected_stock_code", ""))
    }
    if selected:
        all_symbols.add(selected)
    upstream_selected = _s150_stock((latest_manifest.get("s122_decision") or {}).get("selected_stock_code", "")) if latest_manifest else ""
    if upstream_selected:
        all_symbols.add(upstream_selected)
    name_map = _s150_name_map(request, sorted(all_symbols))
    trades = _s150_trade_rows(state, name_map, limit=20)
    settled_returns = [row["day_return"] for row in trades if row.get("day_return") is not None]
    avg_day_return = sum(settled_returns) / len(settled_returns) if settled_returns else None

    return {
        "available": bool(latest_trade_date or trades),
        "message": (
            f"尚未找到 {fallback_from_trade_date} 14:45 S150-SR004 生产结果，显示最近交易日结果。"
            if fallback_from_trade_date
            else ("" if latest_trade_date or trades else "尚未找到 S150-SR004 生产结果。")
        ),
        "requested_trade_date": requested_trade_date,
        "fallback_from_trade_date": fallback_from_trade_date,
        "is_fallback": bool(fallback_from_trade_date),
        "trade_date": latest_trade_date,
        "generated_at": latest_generated_at,
        "data_updated_at": latest_data_updated_at,
        "checked_at": checked_at,
        "status": status,
        "final_action": str(latest_manifest.get("final_action", "") or "") if latest_manifest else "",
        "sell_rule_contract": str(latest_manifest.get("sell_rule_contract", "") or "SR004_profit_trailing_latest1430") if latest_manifest else "SR004_profit_trailing_latest1430",
        "elapsed_sec": _finite_float(latest_manifest.get("elapsed_sec")) if latest_manifest else None,
        "within_latency_budget": bool(latest_manifest.get("within_latency_budget", False)) if latest_manifest else False,
        "recommendation": {
            "stock_code": selected,
            "stock_name": name_map.get(selected, ""),
            "buy_price": buy_price,
            "buy_price_source": str(latest_manifest.get("buy_price_source", "") or "") if latest_manifest else "",
            "text": f"今日14:45推荐：{selected or '暂无'}",
        },
        "upstream": {
            "stock_code": upstream_selected,
            "stock_name": name_map.get(upstream_selected, ""),
            "action": str((latest_manifest.get("s122_decision") or {}).get("action", "") or "") if latest_manifest else "",
        },
        "avg_day_return": avg_day_return,
        "trade_count": len(trades),
        "settled_trade_count": len(settled_returns),
        "source": {
            "state_file": str(S150_STATE_FILE),
            "manifest_path": manifest_path,
        },
        "update_rule": "每个交易日 14:46 以后读取 S150-SR004 预测结果。",
        "trades": trades,
    }


@router.get("/s150-runtime-status")
def s150_runtime_status(trade_date: str | None = None):
    """Runbook-oriented environment/status check for the S150/SR004 14:45 path."""
    requested = _resolve_s150_request_date(trade_date) or datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
    return _build_s150_runtime_status(requested)


@router.post("/s150-runtime-fix")
def s150_runtime_fix(trade_date: str | None = None):
    """Apply safe one-click fixes, then return a fresh runtime status."""
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    requested = _resolve_s150_request_date(trade_date) or now.strftime("%Y%m%d")
    fixes: list[dict[str, Any]] = []

    try:
        S150_PRIORITY_LOG_DIR.mkdir(parents=True, exist_ok=True)
        fixes.append({"key": "s150_log_dir", "status": "ok", "message": f"ensured {S150_PRIORITY_LOG_DIR}"})
    except Exception as exc:  # noqa: BLE001
        fixes.append({"key": "s150_log_dir", "status": "fail", "message": str(exc)})

    fake_processes = _fake_fetcher_processes()
    fake_pids = sorted({int(proc["pid"]) for proc in fake_processes})
    if fake_pids:
        try:
            subprocess.run(["kill", "-TERM", *[str(pid) for pid in fake_pids]], capture_output=True, text=True, timeout=5, check=False)
            time.sleep(1.0)
            remaining = sorted({int(proc["pid"]) for proc in _fake_fetcher_processes() if int(proc["pid"]) in set(fake_pids)})
            if remaining:
                subprocess.run(["kill", "-KILL", *[str(pid) for pid in remaining]], capture_output=True, text=True, timeout=5, check=False)
                time.sleep(0.5)
            final_remaining = sorted({int(proc["pid"]) for proc in _fake_fetcher_processes() if int(proc["pid"]) in set(fake_pids)})
            fixes.append({
                "key": "fake_replay_process",
                "status": "ok" if not final_remaining else "warn",
                "message": (
                    f"stopped fake replay processes: {fake_pids}"
                    if not final_remaining
                    else f"attempted to stop fake replay processes, still present: {final_remaining}"
                ),
                "pids": fake_pids,
                "remaining_pids": final_remaining,
            })
        except Exception as exc:  # noqa: BLE001
            fixes.append({"key": "fake_replay_process", "status": "fail", "message": str(exc), "pids": fake_pids})
    else:
        fixes.append({"key": "fake_replay_process", "status": "skipped", "message": "no fake replay go-fetcher process found"})

    if _prod_fetcher_processes():
        fixes.append({"key": "go_fetcher_process", "status": "skipped", "message": "production realtime fetcher already running"})
    else:
        script = GO_FETCHER_ROOT / "scripts" / "run_realtime_redis.sh"
        if not script.exists():
            fixes.append({"key": "go_fetcher_process", "status": "fail", "message": f"missing start script: {script}"})
        else:
            try:
                subprocess.Popen(
                    ["bash", str(script)],
                    cwd=str(GO_FETCHER_ROOT),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                time.sleep(3.0)
                running = _prod_fetcher_processes()
                fixes.append({
                    "key": "go_fetcher_process",
                    "status": "ok" if running else "warn",
                    "message": (
                        f"started production realtime fetcher, process_count={len(running)}"
                        if running
                        else "start command launched but production process not detected yet"
                    ),
                    "process_count": len(running),
                })
            except Exception as exc:  # noqa: BLE001
                fixes.append({"key": "go_fetcher_process", "status": "fail", "message": str(exc)})

    out = S150_RUN_ROOT / f"{requested}_asof1445"
    manifest_path = out / "manifest.json"
    should_refresh_goal_status = manifest_path.exists() or now.hour > 14 or (now.hour == 14 and now.minute >= 46)
    if should_refresh_goal_status and S150_SR004_SCRIPT.exists():
        cmd = [
            str(SR004_PYTHON),
            str(S150_SR004_SCRIPT),
            "--mode",
            "goal-status",
            "--trade-date",
            requested,
            "--asof",
            "1445",
            "--max-latency-sec",
            "180",
            "--require-latency-budget",
        ]
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(SR004_WORKDIR),
                capture_output=True,
                text=True,
                timeout=45,
                check=False,
            )
            fixes.append({
                "key": "s150_goal_status",
                "status": "ok" if proc.returncode == 0 else "warn",
                "message": "refreshed goal-status" if proc.returncode == 0 else "goal-status refreshed but not passing",
                "returncode": int(proc.returncode),
                "stdout_tail": proc.stdout[-1000:],
                "stderr_tail": proc.stderr[-1000:],
            })
        except Exception as exc:  # noqa: BLE001
            fixes.append({"key": "s150_goal_status", "status": "fail", "message": str(exc)})
    else:
        fixes.append({
            "key": "s150_goal_status",
            "status": "skipped",
            "message": "14:46 前且今日 manifest 不存在，跳过 goal-status 刷新",
        })

    return {
        "trade_date": requested,
        "fixed_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "fixes": fixes,
        "status": _build_s150_runtime_status(requested),
    }


@router.get("/sr004-realtime-exit")
def sr004_realtime_exit(symbol: str, trade_date: str | None = None):
    """Evaluate one stock's SR004 realtime exit directly from Redis."""
    checked_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
    stock_code = _s150_stock(symbol)
    if not stock_code:
        raise HTTPException(status_code=400, detail="symbol must contain a 6-digit stock code")
    resolved_trade_date = _resolve_s150_request_date(trade_date) or datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
    if not SR004_EVAL_SCRIPT.exists():
        raise HTTPException(status_code=500, detail=f"SR004 evaluator not found: {SR004_EVAL_SCRIPT}")
    cmd = [
        str(SR004_PYTHON),
        str(SR004_EVAL_SCRIPT),
        "--trade-date",
        resolved_trade_date,
        "--stock-code",
        stock_code,
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(SR004_WORKDIR),
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail=f"SR004 evaluator timed out for {stock_code}") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"SR004 evaluator failed to start: {exc}") from exc

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        return {
            "status": "blocked_sr004_evaluator_failed",
            "stock_code": stock_code,
            "trade_date": resolved_trade_date,
            "checked_at": checked_at,
            "message": detail or f"SR004 evaluator failed with code {proc.returncode}",
            "transaction_persisted": False,
        }
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"SR004 evaluator returned invalid JSON: {proc.stdout[:500]}") from exc
    if isinstance(payload, dict):
        payload.setdefault("checked_at", checked_at)
        payload.setdefault("trade_date", resolved_trade_date)
        payload.setdefault("stock_code", stock_code)
        return payload
    raise HTTPException(status_code=500, detail="SR004 evaluator returned non-object JSON")


# ================================================================
# 信号回测 (现有接口，保持不变)
# ================================================================

class BacktestRequest(BaseModel):
    symbols: list[str] = Field(..., min_length=1)
    start: date | None = None
    end: date | None = None
    entries: list[str] = []
    exits: list[str] = []
    stop_loss_pct: float | None = None
    max_hold_days: int | None = None
    fees_pct: float = 0.0002
    slippage_bps: float = 5
    matching: Literal["close_t", "open_t+1"] = "close_t"


@router.post("/run")
def run(req: BacktestRequest, request: Request):
    """信号回测 — 现有接口，向后兼容。"""
    repo = request.app.state.repo
    svc = BacktestService(repo)
    end = req.end or date.today()
    start = req.start or (end - timedelta(days=365 * 3))

    cfg = BacktestConfig(
        symbols=req.symbols,
        start=start,
        end=end,
        entries=req.entries,
        exits=req.exits,
        stop_loss_pct=req.stop_loss_pct,
        max_hold_days=req.max_hold_days,
        fees_pct=req.fees_pct,
        slippage_bps=req.slippage_bps,
        matching=req.matching,
    )
    try:
        result = svc.run(cfg)
    except VectorbtUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    return asdict(result)


# ================================================================
# 因子回测
# ================================================================

class FactorColumnsResponse(BaseModel):
    columns: list[dict]


@router.get("/factor/columns")
def factor_columns():
    """返回可用的因子列列表。"""
    from app.backtest.factor import FACTOR_COLUMNS
    return {"columns": FACTOR_COLUMNS}


class FactorBacktestRequest(BaseModel):
    factor_name: str
    symbols: list[str] | None = None
    start: date | None = None
    end: date | None = None
    n_groups: int = 5
    rebalance: Literal["daily", "weekly", "monthly"] = "monthly"
    weight: Literal["equal", "factor_weight"] = "equal"
    fees_pct: float = 0.0002
    slippage_bps: float = 5.0


@router.post("/factor/run")
def factor_run(req: FactorBacktestRequest, request: Request):
    """因子回测 — IC/IR 分析 + 分层回测。"""
    from app.backtest.factor import FactorBacktestService, FactorConfig

    engine = _get_engine(request)
    svc = FactorBacktestService(engine)

    end = req.end or date.today()
    start = _resolve_start(req, end, STRATEGY_DEFAULT_DAYS)
    _guard_server_backtest_range(start, end)
    symbols = req.symbols if req.symbols else None
    if symbols is not None and len(symbols) > FACTOR_MAX_SYMBOLS:
        raise HTTPException(
            status_code=400,
            detail=f"指定标的最多支持 {FACTOR_MAX_SYMBOLS} 只，请缩小标的范围。",
        )

    cfg = FactorConfig(
        factor_name=req.factor_name,
        symbols=symbols,
        start=start,
        end=end,
        n_groups=req.n_groups,
        rebalance=req.rebalance,
        weight=req.weight,
        fees_pct=req.fees_pct,
        slippage_bps=req.slippage_bps,
    )
    result = svc.run(cfg)
    return asdict(result)


# ================================================================
# 策略回测
# ================================================================

class StrategyBacktestRequest(BaseModel):
    strategy_id: str
    symbols: list[str] | None = None
    start: date | None = None
    end: date | None = None
    params: dict | None = None
    overrides: dict | None = None
    # matching 向后兼容; 显式传 entry_fill/exit_fill 时以二者为准。
    matching: Literal["close_t", "open_t+1"] = "open_t+1"
    entry_fill: Literal["close_t", "open_t+1"] | None = None
    exit_fill: Literal["close_t", "open_t+1"] | None = None
    fees_pct: float = 0.0002
    commission_pct: float | None = None
    stamp_tax_pct: float | None = None
    slippage_bps: float = 5.0
    max_positions: int = 10
    max_exposure_pct: float = 1.0
    initial_capital: float = 1_000_000.0
    position_sizing: Literal["equal", "score_weight"] = "equal"
    mode: Literal["position", "full"] = "position"
    holding_days: int = 5


@router.post("/strategy/run")
def strategy_run(req: StrategyBacktestRequest, request: Request):
    """策略回测 — 复用 StrategyDef 体系做全周期回测。"""
    from app.backtest.strategy import StrategyBacktestService, StrategyBacktestConfig

    engine = _get_engine(request)
    strategy_engine = request.app.state.strategy_engine
    svc = StrategyBacktestService(engine, strategy_engine)

    end = req.end or date.today()
    start = _resolve_start(req, end, FACTOR_DEFAULT_DAYS)
    _guard_server_backtest_range(start, end)

    cfg = StrategyBacktestConfig(
        strategy_id=req.strategy_id,
        symbols=req.symbols if req.symbols else None,
        start=start,
        end=end,
        params=req.params,
        overrides=req.overrides,
        matching=req.matching,
        entry_fill=req.entry_fill,
        exit_fill=req.exit_fill,
        fees_pct=req.fees_pct,
        commission_pct=req.commission_pct,
        stamp_tax_pct=req.stamp_tax_pct,
        slippage_bps=req.slippage_bps,
        max_positions=req.max_positions,
        max_exposure_pct=req.max_exposure_pct,
        initial_capital=req.initial_capital,
        position_sizing=req.position_sizing,
        mode=req.mode,
        holding_days=req.holding_days,
    )
    result = svc.run(cfg)
    return asdict(result)


# ── SSE 流式回测 (实时进度 + 可取消 + 支持重连) ───────────────────

import time
import hashlib


class _BacktestJob:
    """单个回测任务的状态, 存模块级供重连使用。"""
    __slots__ = ("key", "cancel_event", "progress", "result", "error", "done", "finish_ts")

    def __init__(self, key: str):
        self.key = key
        self.cancel_event = threading.Event()
        self.progress: list[dict] = []   # 进度历史 (新连接可回放)
        self.result = None               # 完成后的结果
        self.error: str | None = None
        self.done = False
        self.finish_ts: float = 0.0


# 模块级任务表: key -> _BacktestJob
_running_jobs: dict[str, _BacktestJob] = {}
_jobs_lock = threading.Lock()
_JOB_TTL = 300  # 完成后保留 5 分钟


def _cleanup_stale_jobs():
    """清理过期任务 (完成超过 TTL 的)。"""
    now = time.time()
    stale = [k for k, j in _running_jobs.items() if j.done and now - j.finish_ts > _JOB_TTL]
    for k in stale:
        _running_jobs.pop(k, None)


def _make_job_key(
    strategy_id: str, symbols: str | None, start: str | None, end: str | None,
    matching: str, entry_fill: str | None, exit_fill: str | None,
    fees_pct: float, slippage_bps: float,
    max_positions: int, max_exposure_pct: float, initial_capital: float, position_sizing: str,
    params: str | None, overrides: str | None,
    mode: str = "position", holding_days: int = 5,
    commission_pct: float | None = None, stamp_tax_pct: float | None = None,
) -> str:
    raw = f"{strategy_id}|{symbols}|{start}|{end}|{matching}|{entry_fill}|{exit_fill}|{fees_pct}|{slippage_bps}|{max_positions}|{max_exposure_pct}|{initial_capital}|{position_sizing}|{params}|{overrides}|{mode}|{holding_days}|{commission_pct}|{stamp_tax_pct}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]


@router.get("/strategy/stream")
async def strategy_stream(
    request: Request,
    strategy_id: str,
    symbols: str | None = None,
    start: str | None = None,
    end: str | None = None,
    matching: str = "open_t+1",
    entry_fill: str | None = None,
    exit_fill: str | None = None,
    fees_pct: float = 0.0002,
    commission_pct: float | None = None,
    stamp_tax_pct: float | None = None,
    slippage_bps: float = 5.0,
    max_positions: int = 10,
    max_exposure_pct: float = 1.0,
    initial_capital: float = 1_000_000.0,
    position_sizing: str = "equal",
    params: str | None = None,
    overrides: str | None = None,
    mode: str = "position",
    holding_days: int = 5,
):
    """SSE 流式策略回测: 实时推送进度, 完成后推送结果, 支持重连 (刷新/切页后恢复)。

    - 相同参数的任务只启动一次, 多次连接订阅同一个任务
    - 断开连接不会取消任务 (除非显式调用 cancel)
    - 结果保留 5 分钟供重连

    事件类型:
      - progress: {day, total, date, equity}
      - done: {result} (完整回测结果)
      - error: {message}
    """
    from app.backtest.strategy import StrategyBacktestService, StrategyBacktestConfig

    engine = _get_engine(request)
    strategy_engine = request.app.state.strategy_engine
    svc = StrategyBacktestService(engine, strategy_engine)

    end_date = date.fromisoformat(end) if end else date.today()
    if start:
        start_date = date.fromisoformat(start)
    else:
        # 空 start = 全部历史: 用本地最早日K日期, 查不到再回退到默认窗口
        earliest = request.app.state.repo.earliest_daily_date()
        start_date = earliest or (end_date - timedelta(days=FACTOR_DEFAULT_DAYS))

    # 服务端范围保护
    guard_violated = False
    if settings.backtest_range_guard:
        days = (end_date - start_date).days + 1
        if days > BACKTEST_MAX_SERVER_DAYS:
            guard_violated = True

    job_key = _make_job_key(
        strategy_id, symbols, start, end,
        matching, entry_fill, exit_fill,
        fees_pct, slippage_bps, max_positions, max_exposure_pct, initial_capital, position_sizing,
        params, overrides,
        mode, holding_days,
        commission_pct, stamp_tax_pct,
    )

    _cleanup_stale_jobs()

    # 获取或创建任务
    with _jobs_lock:
        job = _running_jobs.get(job_key)
        if job is None:
            job = _BacktestJob(job_key)
            _running_jobs[job_key] = job
            is_new = True
        else:
            is_new = False

    async def event_generator():
        # 范围保护: 直接报错
        if guard_violated:
            yield f"event: error\ndata: {json.dumps({'message': BACKTEST_SERVER_GUARD_MESSAGE}, ensure_ascii=False)}\n\n"
            return

        # 如果是新任务, 启动回测线程
        if is_new and not job.done:
            cfg = StrategyBacktestConfig(
                strategy_id=strategy_id,
                symbols=[s.strip() for s in symbols.split(",") if s.strip()] if symbols else None,
                start=start_date,
                end=end_date,
                params=json.loads(params) if params else None,
                overrides=json.loads(overrides) if overrides else None,
                matching=matching,
                entry_fill=entry_fill,
                exit_fill=exit_fill,
                fees_pct=fees_pct,
                commission_pct=commission_pct,
                stamp_tax_pct=stamp_tax_pct,
                slippage_bps=slippage_bps,
                max_positions=int(max_positions),
                max_exposure_pct=float(max_exposure_pct),
                initial_capital=float(initial_capital),
                position_sizing=position_sizing,
                mode=mode,
                holding_days=int(holding_days),
            )

            def _run_backtest():
                try:
                    result = svc.run(cfg, lambda d: job.progress.append(d), job.cancel_event)
                    job.result = result
                    job.done = True
                    job.finish_ts = time.time()
                except Exception as e:
                    job.error = str(e)
                    job.done = True
                    job.finish_ts = time.time()

            # 启动后台线程 (不阻塞事件循环)
            threading.Thread(target=_run_backtest, daemon=True).start()

        # 订阅进度: 用读指针读 job.progress 列表 (多连接互不干扰)
        cursor = 0
        tick = 0

        try:
            while True:
                # 已完成: 推送最终结果/错误并退出
                if job.done:
                    if job.error:
                        yield f"event: error\ndata: {json.dumps({'message': job.error}, ensure_ascii=False)}\n\n"
                    elif job.result is not None:
                        r = job.result
                        if hasattr(r, "error") and r.error == "cancelled":
                            yield f"event: error\ndata: {json.dumps({'message': '回测已取消'}, ensure_ascii=False)}\n\n"
                        elif hasattr(r, "error") and r.error:
                            yield f"event: error\ndata: {json.dumps({'message': r.error}, ensure_ascii=False)}\n\n"
                        else:
                            yield f"event: done\ndata: {json.dumps(asdict(r), ensure_ascii=False, default=str)}\n\n"
                    return

                # 断开检测: 每 4 轮检查一次 (降低 GIL 抢占频率)
                tick += 1
                if tick % 4 == 0 and await request.is_disconnected():
                    break

                # 推送新进度 (从 cursor 开始读)
                prog_list = job.progress
                while cursor < len(prog_list):
                    msg = prog_list[cursor]
                    cursor += 1
                    yield f"event: progress\ndata: {json.dumps(msg, ensure_ascii=False, default=str)}\n\n"

                await asyncio.sleep(0.5)

        except asyncio.CancelledError:
            raise

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.post("/strategy/cancel")
async def strategy_cancel(request: Request):
    """取消正在运行的回测任务 (前端传 query string, 后端算 job_key)。"""
    body = await request.json()
    qs = body.get("qs", "")
    # 解析 qs 得到参数
    from urllib.parse import parse_qs
    p = parse_qs(qs)
    def _get(key: str, default: str = "") -> str:
        return p.get(key, [default])[0]
    def _get_opt_float(key: str) -> float | None:
        # 可选成本参数: 缺省或空串 → None (与 stream 侧 float | None 口径一致, 保证 job_key 对齐)。
        v = _get(key)
        return float(v) if v else None
    job_key = _make_job_key(
        _get("strategy_id"),
        _get("symbols") or None,
        _get("start") or None,
        _get("end") or None,
        _get("matching", "open_t+1"),
        _get("entry_fill") or None,
        _get("exit_fill") or None,
        float(_get("fees_pct", "0.0002")),
        float(_get("slippage_bps", "5")),
        int(_get("max_positions", "10")),
        float(_get("max_exposure_pct", "1")),
        float(_get("initial_capital", "1000000")),
        _get("position_sizing", "equal"),
        _get("params") or None,
        _get("overrides") or None,
        _get("mode", "position"),
        int(_get("holding_days", "5")),
        commission_pct=_get_opt_float("commission_pct"),
        stamp_tax_pct=_get_opt_float("stamp_tax_pct"),
    )
    job = _running_jobs.get(job_key)
    if job and not job.done:
        job.cancel_event.set()
        return {"ok": True}
    return {"ok": False, "message": "任务不存在或已完成"}
