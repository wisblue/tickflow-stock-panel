"""实时电报 — 解析 etf_minute_telegrams 文件，提供结构化数据给前端。

数据文件每分钟更新一次（A 股交易时段 9:30–15:30），
每个 ## 标题块为一个 section，包含时间、标题和条目列表。

ChatTTS 音频生成通过系统 Python (conda re_3) 子进程调用，
避免在 backend venv 中安装 torch 等重型依赖。
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Query
from fastapi.responses import Response

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/live-telegram", tags=["live-telegram"])

# 电报文件路径 — 文件名后缀为当天日期
TELEGRAM_DIR = Path("/home/dennis/mmrs/data/daily")

# ---- ChatTTS GPU 常驻服务 ----

_GPU_SERVER = "http://127.0.0.1:8765"

AVAILABLE_SEEDS = {
    2: "沉稳男声",
    6616: "年轻女声",
    1111: "磁性女声",
}


def _generate_clips(text: str, seed: int) -> list[dict]:
    """调用 GPU 常驻服务，分句批量生成音频片段。"""
    import urllib.request
    import json as _json

    body = _json.dumps({"text": text, "seed": seed}).encode()
    req = urllib.request.Request(
        f"{_GPU_SERVER}/generate",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = _json.loads(resp.read())
    return data.get("clips", [])


def _telegram_path() -> Path | None:
    """返回当天电报文件路径。"""
    today = datetime.now().strftime("%Y%m%d")
    path = TELEGRAM_DIR / f"etf_minute_telegrams_{today}.md"
    if path.exists():
        return path
    # 回退：查找最新的电报文件
    candidates = sorted(
        TELEGRAM_DIR.glob("etf_minute_telegrams_*.md"),
        reverse=True,
    )
    return candidates[0] if candidates else None


# 解析一行条目：(emoji, bold_label, content)
_ITEM_RE = re.compile(r"^-?\s*(.[^\s*]*)\s*\*?\*?([^*\n]*?)\*?\*?[：:](.*)$")


def _parse_items(lines: list[str]) -> list[dict]:
    """将 section 内的 bullet 行解析为结构化条目。"""
    items: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line.startswith("-"):
            continue
        content = line[1:].strip()
        m = _ITEM_RE.match(line)
        if m:
            emoji = m.group(1).strip()
            label = m.group(2).strip().rstrip("*").strip()
            text = m.group(3).strip()
            items.append({
                "emoji": emoji,
                "label": label,
                "text": text,
            })
        else:
            # 回退：无法匹配时保留原始内容
            items.append({
                "emoji": "",
                "label": "",
                "text": content,
            })
    return items


def _parse_sections(text: str) -> list[dict]:
    """解析 markdown 文件为 section 列表，最新在前。"""
    sections: list[dict] = []
    current_title = ""
    current_lines: list[str] = []

    for line in text.split("\n"):
        if line.startswith("## "):
            # 保存上一个 section
            if current_title:
                sections.append({
                    "time": _extract_time(current_title),
                    "title": current_title,
                    "items": _parse_items(current_lines),
                })
            current_title = line[3:].strip()
            current_lines = []
        elif line.startswith("# "):
            # 文件主标题，跳过
            continue
        elif current_title:
            current_lines.append(line)

    # 最后一个 section
    if current_title:
        sections.append({
            "time": _extract_time(current_title),
            "title": current_title,
            "items": _parse_items(current_lines),
        })

    # 去重：相同时间 + 相同标题 + 相同条目数的 section 只保留第一个
    seen = set()
    deduped: list[dict] = []
    for s in sections:
        key = (s["time"], s["title"], len(s["items"]))
        if key not in seen:
            seen.add(key)
            deduped.append(s)

    # 按时间倒序（最新在前），相同时间按出现顺序
    deduped.sort(key=lambda s: s["time"] or "", reverse=True)
    return deduped


def _extract_time(title: str) -> str:
    """从标题提取 HH:MM 时间字符串。"""
    m = re.search(r"(\d{2}:\d{2})", title)
    return m.group(1) if m else ""


@router.get("")
def get_live_telegrams(
    limit: int = Query(20, ge=1, le=200, description="返回最近 N 条"),
    since: str | None = Query(None, description="只返回此时间之后的条目 (ISO格式)"),
):
    """获取解析后的实时电报数据，最新条目在前。"""
    path = _telegram_path()
    if path is None:
        return {"sections": [], "file": None, "updated_at": None}

    text = path.read_text(encoding="utf-8")
    mtime = path.stat().st_mtime
    updated_at = datetime.fromtimestamp(mtime).isoformat()

    all_sections = _parse_sections(text)

    # since 过滤
    if since:
        filtered: list[dict] = []
        for s in all_sections:
            if s["time"] and s["time"] > since:
                filtered.append(s)
            else:
                break  # 已按时间倒序
        all_sections = filtered

    return {
        "sections": all_sections[:limit],
        "file": str(path),
        "updated_at": updated_at,
        "total": len(all_sections),
    }


@router.get("/raw")
def get_raw_telegram():
    """返回原始 markdown 文本（用于调试）。"""
    path = _telegram_path()
    if path is None:
        return {"raw": "", "file": None}
    return {
        "raw": path.read_text(encoding="utf-8"),
        "file": str(path),
    }


@router.get("/header")
def get_header_state():
    """返回仪表盘顶部持久信息栏的三项数据。

    从传播 pipeline 写入的 header sidecar JSON 读取，包含：
    1. hot_concepts — 热门概念板块 + 方向信号 (▲加速/▼衰减/→维持/🆕新出)
    2. watchlist — 状态机跟踪的个股 (观察/关注/待确认/确认)
    3. market_summary — 市场主线摘要 + 涨跌停计数
    """
    path = _telegram_path()
    if path is None:
        return {
            "hot_concepts": [],
            "watchlist": [],
            "market_summary": {"main_theme": "", "key_stocks": [], "limit_up_count": 0, "limit_down_count": 0},
            "updated_at": None,
        }

    header_path = path.with_suffix(".header.json")
    if not header_path.exists():
        # Fallback: try any .header.json in the telegram dir
        candidates = sorted(
            TELEGRAM_DIR.glob("etf_minute_telegrams_*.header.json"),
            reverse=True,
        )
        header_path = candidates[0] if candidates else None

    if header_path is None:
        return {
            "hot_concepts": [],
            "watchlist": [],
            "market_summary": {"main_theme": "", "key_stocks": [], "limit_up_count": 0, "limit_down_count": 0},
            "updated_at": None,
        }

    try:
        raw = header_path.read_text(encoding="utf-8")
        return json.loads(raw)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read header sidecar: %s", exc)
        return {
            "hot_concepts": [],
            "watchlist": [],
            "market_summary": {"main_theme": "", "key_stocks": [], "limit_up_count": 0, "limit_down_count": 0},
            "updated_at": None,
        }


@router.get("/seeds")
def get_available_seeds():
    """返回可用的 ChatTTS 音色种子列表。"""
    return {
        "seeds": [
            {"id": seed_id, "label": label, "engine": "chattts"}
            for seed_id, label in AVAILABLE_SEEDS.items()
        ],
    }


@router.post("/clips")
def generate_clips(
    text: str = Query(..., description="播报文本"),
    seed: int = Query(2, description="ChatTTS 音色种子"),
    stream: bool = Query(False, description="逐句流式输出 (NDJSON)"),
):
    """调用 GPU 常驻服务生成音频片段。stream=True 时逐句返回 NDJSON。"""
    if seed not in AVAILABLE_SEEDS:
        return Response(
            content=f"不支持的种子: {seed}，可选: {list(AVAILABLE_SEEDS)}",
            status_code=400,
        )
    try:
        if stream:
            return _stream_clips(text, seed)
        clips = _generate_clips(text, seed)
        for c in clips:
            name = c["url"].rsplit("/", 1)[-1]
            c["url"] = f"/api/live-telegram/audio/{name}"
        return {"clips": clips}
    except Exception as e:
        logger.exception("ChatTTS 片段生成失败")
        return Response(
            content=f"生成失败: {e}",
            status_code=500,
        )


def _stream_clips(text: str, seed: int):
    """流式代理 GPU 服务的 NDJSON 输出，改写 URL 路径。"""
    import urllib.request
    import json as _json

    body = _json.dumps({"text": text, "seed": seed, "stream": True}).encode()
    req = urllib.request.Request(
        f"{_GPU_SERVER}/generate",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    def _iter():
        with urllib.request.urlopen(req, timeout=60) as resp:
            for line in resp:
                line = line.decode("utf-8").strip()
                if not line:
                    continue
                clip = _json.loads(line)
                name = clip["url"].rsplit("/", 1)[-1]
                clip["url"] = f"/api/live-telegram/audio/{name}"
                yield _json.dumps(clip, ensure_ascii=False) + "\n"

    from fastapi.responses import StreamingResponse
    return StreamingResponse(_iter(), media_type="application/x-ndjson")


@router.get("/audio/{name}")
def proxy_audio(name: str):
    """代理 GPU 服务的 WAV 文件到前端。"""
    import urllib.request
    try:
        with urllib.request.urlopen(f"{_GPU_SERVER}/audio/{name}", timeout=10) as resp:
            return Response(
                content=resp.read(),
                media_type="audio/mpeg",
            )
    except Exception:
        return Response(content="音频文件未找到", status_code=404)
