# Handoff — 实况 (Live Feed) 功能

## 概述

在侧边栏「看板」下方新增「实况」标签页，实时展示 A 股分钟电报数据。

数据流：

```
~/mmrs/data/daily/etf_minute_telegrams_YYYYMMDD.md (每分钟更新)
       ↓
  /api/live-telegram → 解析 ## 标题块为结构化 JSON
       ↓
  LiveFeed.tsx → 轮询 (交易时段 5s, 非交易时段 15s)
       ↓
  Web Speech API → 新条目到达时语音播报
```

## 涉及文件

### 后端
| 文件 | 说明 |
|------|------|
| `backend/app/api/live_telegram.py` | 解析 markdown 电报文件 → 结构化 API |
| `backend/app/main.py` | 注册 router |

### 前端
| 文件 | 说明 |
|------|------|
| `src/pages/LiveFeed.tsx` | 实况页面：轮询、卡片渲染、语音播报、静音控制 |
| `src/router.tsx` | `/live` 路由 |
| `src/components/Layout.tsx` | 侧边栏导航项 (Radio 图标) |

## API 接口

```
GET /api/live-telegram?limit=30&since=HH:MM
```

响应：
```json
{
  "sections": [
    {
      "time": "11:24",
      "title": "🧩 11:24 · 概念板块分钟汇总",
      "items": [
        {"emoji": "💼", "label": "股票", "text": "茅指数概念：动量扩张14起..."},
        ...
      ]
    }
  ],
  "file": "/home/dennis/mmrs/data/daily/etf_minute_telegrams_20260807.md",
  "updated_at": "2026-08-07T11:31:59",
  "total": 302
}
```

## 功能特性

- **实时轮询**：交易时段 5s，非交易时段 15s
- **语音播报**：Web Speech API (zh-CN)，新条目到达自动朗读
- **静音切换**：状态持久化到 localStorage
- **直播指示**：交易时段绿色脉冲徽标，非交易时段"休市"
- **彩色标签**：涨停红/跌停绿/热点金/相似度蓝/操作蓝
- **动画**：新卡片从顶部滑入 + 蓝色光晕 + NEW 标记
- **暗色主题**：与面板整体设计语言一致

---

# Handoff — 热门概念 (Hot Concepts) 功能

## 概述

在侧边栏「看板」下方新增「热门」标签页，展示当日涨停股票的**概念板块分布 Treemap**。

数据流：

```
盘中：Redis (192.168.50.68:6379 DB15) → tdx:trans:* 逐笔成交
盘后/无数据：~/historical_transaction/YYYY/MM/DD.parquet 历史逐笔成交
       ↓
  最新成交价 × tushare 昨收价(pre_close) → 涨跌幅
       ↓
  涨停检测 (pct_chg ≥ limit_pct × 0.98)
       ↓
  同花顺概念映射 (data/ths_members_N.csv, 3.7MB)
       ↓
  /api/hot-concepts/treemap → ECharts treemap
```

## 涉及文件

### 后端
| 文件 | 说明 |
|------|------|
| `backend/app/api/hot_concepts.py` | 核心：Redis/Parquet 双数据源 → 涨停检测 → 概念映射 → API |
| `backend/app/main.py` (L14, L284) | 注册 router |

### 前端
| 文件 | 说明 |
|------|------|
| `src/pages/HotConcepts.tsx` | Treemap 可视化页面，ECharts 渲染，60s 自动刷新 |
| `src/lib/api.ts` | `HotConceptsResponse` 类型 + `api.hotConceptsTreemap()` |
| `src/lib/queryKeys.ts` | `QK.hotConcepts` 查询 key |
| `src/router.tsx` | `/hot-concepts` 路由 |
| `src/components/Layout.tsx` | 侧边栏导航项 |

## API 接口

```
GET /api/hot-concepts/treemap
```

参数：
- `trade_date` (可选): YYYYMMDD，如 `20260723`。不传则自动找最近有 parquet 的交易日
- `refresh` (可选): `true` 跳过缓存

响应：
```json
{
  "trade_date": "20260723",
  "unique_stocks": 127,
  "concept_count": 263,
  "treemap_pairs": 1092,
  "source": "parquet",
  "warning": null,
  "treemap_data": [
    {
      "name": "储能",
      "value": 32,
      "children": [
        {"name": "中能电气", "value": 1},
        ...
      ]
    },
    ...
  ]
}
```

字段说明：
- `unique_stocks`: 唯一涨停股票数（同一股票属于多个概念只计一次）
- `treemap_pairs`: 概念-股票对总数（有重复，仅用于 treemap 渲染）
- `source`: `"redis"` | `"parquet"` | `"none"`

## 数据源优先级

1. **Redis** (`tdx:trans:*` keys, DB15): 盘中实时，限 3000 只采样，4s 超时
2. **Parquet** (`~/historical_transaction/`): 历史逐笔成交，自动找最近可用日期

Redis 不可用/超时时自动回退 parquet，对前端透明。

## 涨停检测逻辑

- 10% 板: 主板 (0/1/5/6/7 开头)，阈值 ≥ 9.8%
- 20% 板: 创业板 (3 开头) + 科创板 (688 开头)，阈值 ≥ 19.6%
- 30% 板: 北交所 (8/9 开头)，阈值 ≥ 29.4%
- ST: 5% 限制

## 缓存

- 后端 15 分钟内存缓存（`_cached_treemap_data`）
- 前端 React Query 30s staleTime + 60s refetchInterval
- 第一个请求较慢（~4s parquet + tushare），后续命中缓存

## 已知限制

1. **Parquet 价格精度**：北交所 (920) 股票的 parquet 价格比实际低 10 倍，已通过 tushare 日线交叉验证排除假阳性
2. **概念数据**：使用同花顺静态快照 (`ths_members_N.csv`)，不包含历史成员变更
3. **Redis 全量读取不可行**：22000+ 只股票 × ~500KB/只 ≈ 11GB，盘中仅采样
4. **首次请求慢**：parquet 14M 行读取 + tushare API ≈ 4-5s，后续命中缓存
