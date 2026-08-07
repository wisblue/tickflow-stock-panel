"""实时电报 — 解析 etf_minute_telegrams 文件，提供结构化数据给前端。

数据文件每分钟更新一次（A 股交易时段 9:30–15:30），
每个 ## 标题块为一个 section，包含时间、标题和条目列表。
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Query

router = APIRouter(prefix="/api/live-telegram", tags=["live-telegram"])

# 电报文件路径 — 文件名后缀为当天日期
TELEGRAM_DIR = Path("/home/dennis/mmrs/data/daily")


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
