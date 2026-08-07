# Handoff — 实况 (Live Feed) 功能

## 概述

在侧边栏「看板」下方新增「实况」标签页，实时展示 A 股分钟电报数据，支持 ChatTTS GPU 语音播报。

## 架构

```
~/mmrs/data/daily/etf_minute_telegrams_YYYYMMDD.md (每分钟更新)
       ↓
  /api/live-telegram → 解析 ## 标题块为结构化 JSON
       ↓
  LiveFeed.tsx → 轮询 (交易时段 5s, 非交易时段 15s)
       ↓
  点击 ▶ → POST /api/live-telegram/clips?stream=true
       ↓
  FastAPI (3018) ──proxy──▶ ChatTTS GPU Server (8765)
       ↓                        │ RTX 3090, 模型常驻显存
  NDJSON 流                       │ 逐句推理 → ffmpeg → MP3
       ↓                        │ sha256(text+seed) 缓存
  前端队列播放                     │ 首次 ~6s/句, 缓存 <0.01s
  片段 1/12 → 2/12 → ...
```

## 涉及文件

### 后端 (tickflow-stock-panel)
| 文件 | 说明 |
|------|------|
| `backend/app/api/live_telegram.py` | 电报解析 + clips 生成 + 音频代理 |
| `backend/app/main.py` | 注册 router |

### 前端 (tickflow-stock-panel)
| 文件 | 说明 |
|------|------|
| `src/pages/LiveFeed.tsx` | 实况页面：轮询、流式播放、音色选择、缓存命中 |
| `src/router.tsx` | `/live` 路由 |
| `src/components/Layout.tsx` | 侧边栏导航项 (Radio 图标) |

### GPU 服务 (mmrs)
| 文件 | 说明 |
|------|------|
| `app/audio/exp/chattts_server.py` | GPU 常驻 HTTP 服务，逐句流式生成 MP3 |
| `app/audio/exp/gen_audio_cli.py` | CLI 单次生成工具 |
| `app/audio/exp/gen_seed.py` | 音色样本试听工具 |

## API 接口

```
GET  /api/live-telegram?limit=30          → 电报数据
GET  /api/live-telegram/seeds              → 可用音色列表
POST /api/live-telegram/clips?text=...&seed=2&stream=true  → NDJSON 流式片段
GET  /api/live-telegram/audio/{name}       → MP3 文件 (代理 GPU 服务)
```

### clips 响应 (stream=true, NDJSON)
```json
{"url": "/api/live-telegram/audio/clip_xxx_0.mp3", "duration": 2.65, "text": "涨停62只", "mp3_kb": 21, "index": 0, "total": 10, "gen_time": 4.7}
{"url": "/api/live-telegram/audio/clip_xxx_1.mp3", "duration": 3.99, "text": "跌停5只", "mp3_kb": 31, "index": 1, "total": 10, "gen_time": 0.8}
...
```

### seeds 响应
```json
{"seeds": [
  {"id": 2,    "label": "沉稳男声", "engine": "chattts"},
  {"id": 6616, "label": "年轻女声", "engine": "chattts"},
  {"id": 1111, "label": "磁性女声", "engine": "chattts"}
]}
```

## 语音引擎

| 引擎 | 音色 | 延迟 | 说明 |
|------|------|------|------|
| ChatTTS · 沉稳男声 | seed=2 | 首句 ~5s, 缓存 <0.01s | GPU 推理，MP3 64kbps |
| ChatTTS · 年轻女声 | seed=6616 | 同上 | |
| ChatTTS · 磁性女声 | seed=1111 | 同上 | |
| Web Speech | 浏览器默认 | 即时 | 无须后端，质量较低 |

## 启动命令

```bash
# 1. GPU 常驻服务（必须先启动）
cd ~/mmrs/app/audio/exp
python3 chattts_server.py --port 8765 --device cuda &

# 2. 后端
cd ~/mmrs/re_3/github/tickflow-stock-panel/backend
uv run uvicorn app.main:app --host 0.0.0.0 --port 3018

# 3. 打开 http://localhost:3018/live
```

## 功能特性

- **实时轮询**：交易时段 5s，非交易时段 15s
- **流式语音**：逐句生成 + NDJSON 流，首句到达即播放
- **内容缓存**：sha256(text+seed) 去重，重复内容秒级响应
- **MP3 压缩**：64kbps mono，6:1 压缩比（50s 音频仅 400KB）
- **独立播放**：每分钟卡片右上角 ▶ 按钮
- **音色选择**：下拉菜单切换 ChatTTS 种子 / Web Speech
- **播放进度**：按钮旁显示 "3/12" 片段计数
- **暗色主题**：与面板设计语言一致
- **彩色标签**：涨停红/跌停绿/热点金/相似度蓝

## 缓存

- 目录：`/tmp/chattts_audio/cache/<hash>/manifest.json`
- Key：`sha256(text + seed)[:16]`
- 重启 GPU 服务后缓存仍然有效
- 清理：`rm -rf /tmp/chattts_audio/cache/`
