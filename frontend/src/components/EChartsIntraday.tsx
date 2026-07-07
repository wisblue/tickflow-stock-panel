import { useEffect, useMemo, useRef, useState } from 'react'
import * as echarts from 'echarts'
import type { ECharts, EChartsOption } from 'echarts'
import type { MinuteKlineRow, TransactionIntradayRow } from '@/lib/api'
import { useChartTheme, type ChartTheme } from '@/lib/theme'

type YMode = 'adaptive' | 'limit'
export type IntradayIndicator = 'macd' | 'rsi' | 'kdj' | 'boll' | 'moneyflow'

// 序列颜色 (双主题通用); 画布轴/网格/十字线等主题相关色走 ChartTheme
const THEME = {
  line: '#3B82F6',
  areaFill: 'rgba(59,130,246,0.40)',
  avgLine: '#F59E0B',
  volUp: 'rgba(240,68,56,0.6)',
  volDown: 'rgba(18,183,106,0.6)',
}

interface Props {
  data: MinuteKlineRow[]
  height?: number
  prevClose?: number
  date?: string
  symbol?: string
  onPriceHover?: (price: number | null) => void
  showLimitLines?: boolean
  showAvgLine?: boolean
  indicators?: IntradayIndicator[]
  moneyFlowRows?: TransactionIntradayRow[]
}

function fmtTime(dt: string): string {
  const match = dt.match(/(\d{2}):(\d{2})/)
  if (!match) return dt.slice(11, 16)
  const h = (parseInt(match[1]) + 8) % 24
  return `${String(h).padStart(2, '0')}:${match[2]}`
}

function computeAvgPrice(data: MinuteKlineRow[]): number[] {
  // 分时均线 = 累计成交额 / 累计成交量(手→股)
  const result: number[] = []
  let sumAmt = 0
  let sumVol = 0
  for (const d of data) {
    sumAmt += d.amount
    sumVol += d.volume * 100
    result.push(sumVol > 0 ? sumAmt / sumVol : d.close)
  }
  return result
}

function fmtAmt(v: number): string {
  if (v >= 1_000_000_000) return `${(v / 1_000_000_000).toFixed(2)}亿`
  if (v >= 10_000) return `${(v / 10_000).toFixed(0)}万`
  return v.toFixed(0)
}

function isValidPrice(v: number | null | undefined): v is number {
  return typeof v === 'number' && Number.isFinite(v) && v > 0
}

/** 生成全天分时时间刻度 9:30 ~ 11:30, 13:00 ~ 15:00, 每分钟一个点 (共242个) */
function generateFullDayTimes(): string[] {
  const times: string[] = []
  // 上午 9:30 ~ 11:30 (121 分钟)
  for (let h = 9; h <= 11; h++) {
    const startM = h === 9 ? 30 : 0
    const endM = h === 11 ? 30 : 59
    for (let m = startM; m <= endM; m++) {
      times.push(`${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`)
    }
  }
  // 下午 13:00 ~ 15:00 (121 分钟)
  for (let h = 13; h <= 15; h++) {
    const endM = h === 15 ? 0 : 59
    for (let m = 0; m <= endM; m++) {
      times.push(`${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`)
    }
  }
  return times
}

const FULL_DAY_TIMES = generateFullDayTimes()

function alignByMinute<T>(data: MinuteKlineRow[], values: T[]): (T | null)[] {
  const timeIndexMap = new Map(FULL_DAY_TIMES.map((t, i) => [t, i]))
  const out = new Array(FULL_DAY_TIMES.length).fill(null) as (T | null)[]
  for (let i = 0; i < data.length; i++) {
    const idx = timeIndexMap.get(fmtTime(data[i].datetime))
    if (idx !== undefined) out[idx] = values[i]
  }
  return out
}

function ema(values: number[], period: number): number[] {
  const k = 2 / (period + 1)
  const out: number[] = []
  for (let i = 0; i < values.length; i++) {
    out.push(i === 0 ? values[i] : values[i] * k + out[i - 1] * (1 - k))
  }
  return out
}

function rollingMean(values: number[], period: number): (number | null)[] {
  const out: (number | null)[] = []
  let sum = 0
  for (let i = 0; i < values.length; i++) {
    sum += values[i]
    if (i >= period) sum -= values[i - period]
    out.push(i >= period - 1 ? sum / period : null)
  }
  return out
}

function rollingStd(values: number[], means: (number | null)[], period: number): (number | null)[] {
  return values.map((_, i) => {
    const mean = means[i]
    if (mean == null || i < period - 1) return null
    let variance = 0
    for (let j = i - period + 1; j <= i; j++) {
      variance += Math.pow(values[j] - mean, 2)
    }
    return Math.sqrt(variance / period)
  })
}

function computeMacd(data: MinuteKlineRow[]) {
  const closes = data.map(d => d.close)
  const fast = ema(closes, 12)
  const slow = ema(closes, 26)
  const dif = closes.map((_, i) => fast[i] - slow[i])
  const dea = ema(dif, 9)
  const hist = dif.map((v, i) => (v - dea[i]) * 2)
  return { dif, dea, hist }
}

function computeRsi(data: MinuteKlineRow[], period = 14): (number | null)[] {
  const closes = data.map(d => d.close)
  const out: (number | null)[] = []
  let gain = 0
  let loss = 0
  for (let i = 0; i < closes.length; i++) {
    if (i === 0) {
      out.push(null)
      continue
    }
    const chg = closes[i] - closes[i - 1]
    const up = Math.max(chg, 0)
    const down = Math.max(-chg, 0)
    if (i <= period) {
      gain += up
      loss += down
      out.push(i === period ? 100 - 100 / (1 + (gain / period) / Math.max(loss / period, 1e-9)) : null)
    } else {
      gain = (gain * (period - 1) + up) / period
      loss = (loss * (period - 1) + down) / period
      out.push(100 - 100 / (1 + gain / Math.max(loss, 1e-9)))
    }
  }
  return out
}

function computeKdj(data: MinuteKlineRow[], period = 9) {
  const k: (number | null)[] = []
  const d: (number | null)[] = []
  const j: (number | null)[] = []
  let prevK = 50
  let prevD = 50
  for (let i = 0; i < data.length; i++) {
    if (i < period - 1) {
      k.push(null); d.push(null); j.push(null)
      continue
    }
    const slice = data.slice(i - period + 1, i + 1)
    const low = Math.min(...slice.map(r => r.low))
    const high = Math.max(...slice.map(r => r.high))
    const rsv = high === low ? 50 : (data[i].close - low) / (high - low) * 100
    prevK = prevK * 2 / 3 + rsv / 3
    prevD = prevD * 2 / 3 + prevK / 3
    k.push(prevK)
    d.push(prevD)
    j.push(3 * prevK - 2 * prevD)
  }
  return { k, d, j }
}

function computeBoll(data: MinuteKlineRow[]) {
  const closes = data.map(d => d.close)
  const mid = rollingMean(closes, 20)
  const std = rollingStd(closes, mid, 20)
  return {
    mid,
    upper: mid.map((v, i) => v == null || std[i] == null ? null : v + 2 * std[i]!),
    lower: mid.map((v, i) => v == null || std[i] == null ? null : v - 2 * std[i]!),
  }
}

function buildMoneyFlowSeries(rows: TransactionIntradayRow[] | undefined): { full: (number | null)[]; main: (number | null)[] } {
  const full = new Array(FULL_DAY_TIMES.length).fill(null) as (number | null)[]
  const main = new Array(FULL_DAY_TIMES.length).fill(null) as (number | null)[]
  const timeIndexMap = new Map(FULL_DAY_TIMES.map((t, i) => [t, i]))
  for (const row of rows ?? []) {
    if (row.time_hhmmss > 150000) continue
    const hh = Math.floor(row.time_hhmmss / 10000)
    const mm = Math.floor(row.time_hhmmss / 100) % 100
    const idx = timeIndexMap.get(`${String(hh).padStart(2, '0')}:${String(mm).padStart(2, '0')}`)
    if (idx === undefined) continue
    full[idx] = row.full_net_w
    main[idx] = row.main_net_w
  }
  return { full, main }
}

/** 根据 symbol 判断涨跌停幅度 (创业板/科创板 ±20%, 北交所 ±30%, 其余 ±10%) */
function getLimitPct(symbol?: string): number {
  if (!symbol) return 0.10
  if (symbol.endsWith('.BJ')) return 0.30                                  // 北交所
  if (symbol.startsWith('300') || symbol.startsWith('301')) return 0.20  // 创业板
  if (symbol.startsWith('688') || symbol.startsWith('689')) return 0.20  // 科创板
  return 0.10
}

/** 计算实际涨跌停价 (四舍五入到2位小数) 和实际涨跌停幅度 */
function getLimitPrices(prevClose: number, symbol?: string): {
  limitUp: number      // 涨停价 (四舍五入)
  limitDown: number    // 跌停价 (四舍五入)
  upPct: number        // 实际涨停幅度 (如 9.97)
  downPct: number      // 实际跌停幅度 (如 -9.97)
} {
  const pct = getLimitPct(symbol)
  const rawUp = prevClose * (1 + pct)
  const rawDown = prevClose * (1 - pct)
  // A股涨跌停价四舍五入到分 (2位小数)
  const limitUp = Math.round(rawUp * 100) / 100
  const limitDown = Math.round(rawDown * 100) / 100
  const upPct = (limitUp - prevClose) / prevClose * 100
  const downPct = (limitDown - prevClose) / prevClose * 100
  return { limitUp, limitDown, upPct, downPct }
}

function buildOption(
  data: MinuteKlineRow[],
  prevClose: number | undefined,
  avgPrices: number[],
  lineColor: string,
  areaColor: string,
  yMode: YMode,
  ct: ChartTheme,
  symbol?: string,
  showLimitLines = true,
  showAvgLine = true,
  indicators: IntradayIndicator[] = [],
  moneyFlowRows?: TransactionIntradayRow[],
): EChartsOption {
  // 将数据映射到全天时间轴上的正确位置
  const timeIndexMap = new Map(FULL_DAY_TIMES.map((t, i) => [t, i]))
  const closes = new Array(FULL_DAY_TIMES.length).fill(null) as (number | null)[]
  const highs = new Array(FULL_DAY_TIMES.length).fill(null) as (number | null)[]
  const lows = new Array(FULL_DAY_TIMES.length).fill(null) as (number | null)[]
  const avgData = new Array(FULL_DAY_TIMES.length).fill(null) as (number | null)[]
  const volumes = new Array(FULL_DAY_TIMES.length).fill(null) as (any | null)[]
  const showSubGrid = indicators.some(key => key === 'macd' || key === 'rsi' || key === 'kdj')
  const showMoneyFlow = indicators.includes('moneyflow')
  const boll = indicators.includes('boll') ? computeBoll(data) : null
  const macd = indicators.includes('macd') ? computeMacd(data) : null
  const rsi = indicators.includes('rsi') ? computeRsi(data) : null
  const kdj = indicators.includes('kdj') ? computeKdj(data) : null
  const moneyFlow = showMoneyFlow ? buildMoneyFlowSeries(moneyFlowRows) : null
  const xVolIndex = showSubGrid ? 2 : 1
  const ySubIndex = showSubGrid ? 1 : -1
  const yVolIndex = showSubGrid ? 2 : 1

  const volNeutral = 'rgba(161,161,170,0.5)'
  for (let i = 0; i < data.length; i++) {
    const timeKey = fmtTime(data[i].datetime)
    const idx = timeIndexMap.get(timeKey)
    if (idx !== undefined) {
      closes[idx] = data[i].close
      highs[idx] = data[i].high
      lows[idx] = data[i].low
      avgData[idx] = avgPrices[i]
      volumes[idx] = {
        value: data[i].volume,
        itemStyle: {
          color: data[i].close > data[i].open ? THEME.volUp : data[i].close < data[i].open ? THEME.volDown : volNeutral,
        },
      }
    }
  }

  const areaStyle: any = {
    color: {
      type: 'linear',
      x: 0, y: 0, x2: 0, y2: 1,
      colorStops: [
        { offset: 0, color: areaColor },
        { offset: 1, color: 'rgba(0,0,0,0)' },
      ],
    },
  }

  const markLineData: any[] = []
  if (prevClose != null) {
    markLineData.push({
      yAxis: prevClose,
      lineStyle: { color: ct.crosshair, type: 'dashed', width: 1 },
      label: { show: false },
      symbol: 'none',
    })
  }

  let yMin: number | undefined
  let yMax: number | undefined
  let maxDiff = 0
  if (isValidPrice(prevClose) && data.length > 0) {
    const priceArrays = showAvgLine ? [closes, highs, lows, avgData] : [closes, highs, lows]
    for (const arr of priceArrays) {
      for (const v of arr) {
        if (!isValidPrice(v)) continue
        const diff = Math.abs(v - prevClose)
        if (diff > maxDiff) maxDiff = diff
      }
    }

    if (showLimitLines && yMode === 'limit') {
      const { limitUp, limitDown } = getLimitPrices(prevClose, symbol)
      const limitDiffUp = limitUp - prevClose
      const limitDiffDown = prevClose - limitDown
      const limitDiff = Math.max(limitDiffUp, limitDiffDown)
      // 涨跌停模式: Y 轴按实际涨跌停价
      maxDiff = limitDiff
      yMin = prevClose - maxDiff
      yMax = prevClose + maxDiff
      // 加 markLine 标注涨停价和跌停价 (仅虚线, 不显示文字)
      markLineData.push(
        {
          yAxis: limitUp,
          lineStyle: { color: 'rgba(199,64,64,0.4)', type: 'dashed', width: 1 },
          label: { show: false },
          symbol: 'none',
        },
        {
          yAxis: limitDown,
          lineStyle: { color: 'rgba(45,155,101,0.4)', type: 'dashed', width: 1 },
          label: { show: false },
          symbol: 'none',
        },
      )
    } else {
      // 自适应模式: Y 轴按实际涨跌幅对称, 但不超出实际涨跌停范围
      if (showLimitLines) {
        const { limitUp, limitDown } = getLimitPrices(prevClose, symbol)
        const limitDiff = Math.max(limitUp - prevClose, prevClose - limitDown)
        maxDiff = Math.min(maxDiff, limitDiff)
      }
      if (!showLimitLines && maxDiff > 0) {
        maxDiff *= 1.1
      }
      // 至少保证一个可视范围 (防止数据平时 maxDiff=0)。指数不使用涨跌停范围，最小范围要更紧，否则低波动指数会被压成横线。
      const minDiff = showLimitLines ? prevClose * 0.01 : prevClose * 0.001
      if (maxDiff < minDiff) maxDiff = minDiff
      yMin = prevClose - maxDiff
      yMax = prevClose + maxDiff
    }
  }
  const percentAxisShown = isValidPrice(prevClose) && yMin != null && yMax != null
  const moneyFlowAxisIndex = (showSubGrid ? 3 : 2) + (percentAxisShown ? 1 : 0)

  // x 轴标签: 9:30, 10:30, 11:30/13:00, 14:00, 15:00
  // 11:30(idx 120) 和 13:00(idx 121) 相邻会重叠, 合并为一个标签
  const xAxisLabelMap: Record<number, string> = {
    0: '9:30',
    60: '10:30',
    120: '11:30/13:00',
    181: '14:00',
    241: '15:00',
  }
  const xAxisLabelFormatter = (_value: string, idx: number) => {
    return xAxisLabelMap[idx] ?? ''
  }

  return {
    animation: false,
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'axis',
      backgroundColor: 'transparent',
      borderWidth: 0,
      textStyle: { fontSize: 0 },
      formatter: () => '',
      axisPointer: {
        type: 'cross',
        label: {
          show: true,
          backgroundColor: ct.tooltipBg,
          borderColor: ct.tooltipBorder,
          borderWidth: 1,
          padding: [2, 5],
          color: ct.tooltipText,
          fontSize: 10,
          fontFamily: 'JetBrains Mono, monospace',
        },
        crossStyle: { color: ct.crosshair, type: 'dashed', width: 1 },
        lineStyle: { color: ct.crosshair, type: 'dashed', width: 1 },
      },
    },
    axisPointer: {
      link: [{ xAxisIndex: 'all' }],
    },
    legend: {
      show: showMoneyFlow,
      left: 60,
      top: 0,
      itemWidth: 12,
      itemHeight: 6,
      textStyle: { color: ct.text, fontSize: 10 },
    },
    grid: showSubGrid
      ? [
          { left: 60, right: showMoneyFlow ? 72 : 55, top: 24, bottom: '46%' },
          { left: 60, right: showMoneyFlow ? 72 : 55, top: '58%', bottom: '24%' },
          { left: 60, right: showMoneyFlow ? 72 : 55, top: '80%', bottom: 20 },
        ]
      : [
          { left: 60, right: showMoneyFlow ? 72 : 55, top: 24, bottom: '28%' },
          { left: 60, right: showMoneyFlow ? 72 : 55, top: '74%', bottom: 20 },
        ],
    xAxis: [
      {
        type: 'category',
        data: FULL_DAY_TIMES,
        boundaryGap: false,
        axisPointer: {
          show: true,
          lineStyle: { color: ct.crosshair, type: 'dashed', width: 1 },
          label: {
            show: true,
            backgroundColor: ct.tooltipBg,
            borderColor: ct.tooltipBorder,
            borderWidth: 1,
            padding: [2, 4],
            color: ct.tooltipText,
            fontSize: 10,
            fontFamily: 'JetBrains Mono, monospace',
            formatter: (params: any) => {
              return params.value ?? ''
            },
          },
        },
        axisLine: { show: false },
        axisLabel: {
          color: ct.text,
          fontSize: 10,
          fontFamily: 'JetBrains Mono, monospace',
          formatter: xAxisLabelFormatter,
          interval: 0,
        },
        axisTick: { show: false },
        splitLine: {
          show: true,
          lineStyle: { color: ct.grid },
        },
      },
      {
        type: 'category',
        gridIndex: 1,
        data: FULL_DAY_TIMES,
        boundaryGap: false,
        axisLine: { show: false },
        axisLabel: { show: false },
        axisTick: { show: false },
        splitLine: { show: false },
      },
      ...(showSubGrid ? [{
        type: 'category' as const,
        gridIndex: 2,
        data: FULL_DAY_TIMES,
        boundaryGap: false,
        axisLine: { show: false },
        axisLabel: { show: false },
        axisTick: { show: false },
        splitLine: { show: false },
      }] : []),
    ],
    yAxis: [
      {
        type: 'value',
        min: yMin,
        max: yMax,
        interval: maxDiff || undefined,
        splitArea: { show: false },
        axisLine: { show: false },
        axisTick: { show: false },
        splitLine: { lineStyle: { color: ct.grid } },
        axisPointer: {
          label: {
            formatter: (params: any) => {
              const v = params.value
              return typeof v === 'number' ? v.toFixed(2) : ''
            },
          },
        },
        axisLabel: {
          color: ct.text,
          fontSize: 10,
          fontFamily: 'JetBrains Mono, monospace',
          formatter: (v: number) => v.toFixed(2),
        },
      },
      {
        scale: true,
        gridIndex: showSubGrid ? 1 : 1,
        splitNumber: 2,
        axisLine: { show: false },
        axisTick: { show: false },
        splitLine: { lineStyle: { color: showSubGrid ? ct.grid : 'transparent' } },
        axisLabel: {
          show: showSubGrid,
          color: ct.text,
          fontSize: 10,
          fontFamily: 'JetBrains Mono, monospace',
          formatter: (v: number) => Math.abs(v) >= 10 ? v.toFixed(0) : v.toFixed(2),
        },
      },
      ...(showSubGrid ? [{
        scale: true,
        gridIndex: 2,
        splitNumber: 2,
        axisLine: { show: false },
        axisTick: { show: false },
        splitLine: { show: false },
        axisLabel: { show: false },
      }] : []),
      ...(percentAxisShown ? [{
        type: 'value' as const,
        position: 'right' as const,
        gridIndex: 0,
        min: yMin,
        max: yMax,
        interval: maxDiff || undefined,
        splitArea: { show: false },
        axisLine: { show: false },
        axisTick: { show: false },
        splitLine: { show: false },
        axisPointer: {
          label: {
            formatter: (params: any) => {
              const v = params.value
              if (typeof v !== 'number') return ''
              const pct = (v - prevClose) / prevClose * 100
              if (Math.abs(pct) < 0.01) return '0.00%'
              return (pct > 0 ? '+' : '') + pct.toFixed(2) + '%'
            },
          },
        },
        axisLabel: {
          color: ct.text,
          fontSize: 10,
          fontFamily: 'JetBrains Mono, monospace',
          formatter: (v: number) => {
            const pct = (v - prevClose) / prevClose * 100
            if (Math.abs(pct) < 0.01) return '0.00%'
            return (pct > 0 ? '+' : '') + pct.toFixed(2) + '%'
          },
        },
      }] : []),
      ...(showMoneyFlow ? [{
        type: 'value' as const,
        position: 'right' as const,
        gridIndex: 0,
        scale: true,
        splitLine: { show: false },
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: {
          color: ct.text,
          fontSize: 10,
          fontFamily: 'JetBrains Mono, monospace',
          formatter: (v: number) => Math.abs(v) >= 10000 ? `${Math.round(v / 10000)}亿` : `${Math.round(v)}w`,
        },
      }] : []),
    ],
    series: [
      {
        name: '价格',
        type: 'line',
        data: closes,
        smooth: false,
        symbol: 'none',
        cursor: 'crosshair',
        lineStyle: { width: 1.2, color: lineColor },
        areaStyle,
        connectNulls: true,
        markLine: markLineData.length > 0 ? { symbol: 'none', data: markLineData, animation: false, silent: true } : undefined,
      },
      ...(showAvgLine ? [{
        name: '均价',
        type: 'line' as const,
        data: avgData,
        smooth: false,
        symbol: 'none',
        cursor: 'crosshair',
        lineStyle: { width: 1, color: THEME.avgLine },
        connectNulls: true,
      }] : []),
      ...(boll ? [
        {
          name: 'BOLL上',
          type: 'line' as const,
          data: alignByMinute(data, boll.upper),
          symbol: 'none',
          lineStyle: { width: 0.8, color: '#A855F7', opacity: 0.85 },
          connectNulls: true,
        },
        {
          name: 'BOLL中',
          type: 'line' as const,
          data: alignByMinute(data, boll.mid),
          symbol: 'none',
          lineStyle: { width: 0.8, color: '#94A3B8', opacity: 0.75 },
          connectNulls: true,
        },
        {
          name: 'BOLL下',
          type: 'line' as const,
          data: alignByMinute(data, boll.lower),
          symbol: 'none',
          lineStyle: { width: 0.8, color: '#A855F7', opacity: 0.85 },
          connectNulls: true,
        },
      ] : []),
      ...(showMoneyFlow && moneyFlow ? [
        {
          name: '全量净额',
          type: 'line' as const,
          yAxisIndex: moneyFlowAxisIndex,
          data: moneyFlow.full,
          smooth: false,
          symbol: 'none',
          lineStyle: { width: 1, color: '#F59E0B' },
          connectNulls: true,
        },
        {
          name: '主力净额',
          type: 'line' as const,
          yAxisIndex: moneyFlowAxisIndex,
          data: moneyFlow.main,
          smooth: false,
          symbol: 'none',
          lineStyle: { width: 1, color: '#EF4444' },
          connectNulls: true,
        },
      ] : []),
      ...(macd && showSubGrid ? [
        {
          name: 'MACD',
          type: 'bar' as const,
          xAxisIndex: 1,
          yAxisIndex: ySubIndex,
          data: alignByMinute(data, macd.hist).map(v => v == null ? null : {
            value: v,
            itemStyle: { color: v >= 0 ? THEME.volUp : THEME.volDown },
          }),
        },
        {
          name: 'DIF',
          type: 'line' as const,
          xAxisIndex: 1,
          yAxisIndex: ySubIndex,
          data: alignByMinute(data, macd.dif),
          symbol: 'none',
          lineStyle: { width: 1, color: '#38BDF8' },
          connectNulls: true,
        },
        {
          name: 'DEA',
          type: 'line' as const,
          xAxisIndex: 1,
          yAxisIndex: ySubIndex,
          data: alignByMinute(data, macd.dea),
          symbol: 'none',
          lineStyle: { width: 1, color: '#F97316' },
          connectNulls: true,
        },
      ] : []),
      ...(rsi && showSubGrid ? [{
        name: 'RSI',
        type: 'line' as const,
        xAxisIndex: 1,
        yAxisIndex: ySubIndex,
        data: alignByMinute(data, rsi),
        symbol: 'none',
        lineStyle: { width: 1, color: '#22C55E' },
        connectNulls: true,
      }] : []),
      ...(kdj && showSubGrid ? [
        {
          name: 'K',
          type: 'line' as const,
          xAxisIndex: 1,
          yAxisIndex: ySubIndex,
          data: alignByMinute(data, kdj.k),
          symbol: 'none',
          lineStyle: { width: 1, color: '#38BDF8' },
          connectNulls: true,
        },
        {
          name: 'D',
          type: 'line' as const,
          xAxisIndex: 1,
          yAxisIndex: ySubIndex,
          data: alignByMinute(data, kdj.d),
          symbol: 'none',
          lineStyle: { width: 1, color: '#F59E0B' },
          connectNulls: true,
        },
        {
          name: 'J',
          type: 'line' as const,
          xAxisIndex: 1,
          yAxisIndex: ySubIndex,
          data: alignByMinute(data, kdj.j),
          symbol: 'none',
          lineStyle: { width: 1, color: '#A855F7' },
          connectNulls: true,
        },
      ] : []),
      {
        name: '成交量',
        type: 'bar',
        data: volumes,
        xAxisIndex: xVolIndex,
        yAxisIndex: yVolIndex,
        cursor: 'crosshair',
      },
    ],
  }
}

export function EChartsIntraday({
  data,
  height = 320,
  prevClose,
  date,
  symbol,
  onPriceHover,
  showLimitLines = true,
  showAvgLine = true,
  indicators = [],
  moneyFlowRows,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<ECharts | null>(null)
  const roRef = useRef<ResizeObserver | null>(null)
  const moRef = useRef<MutationObserver | null>(null)
  const dataRef = useRef(data)
  dataRef.current = data
  const onPriceHoverRef = useRef(onPriceHover)
  onPriceHoverRef.current = onPriceHover
  // 全日索引 → 数据数组索引 的映射 (ref 避免重建 chart)
  const fullDayToDataIdx = useRef<Map<number, number>>(new Map())

  const [infoIdx, setInfoIdx] = useState(data.length - 1)
  const [yMode, setYMode] = useState<YMode>('adaptive')
  const ct = useChartTheme()
  const avgPrices = useMemo(() => computeAvgPrice(data), [data])

  // 分时线颜色：基于最新价 vs 昨收
  const lastClose = data.length > 0 ? data[data.length - 1].close : null
  const lineIsUp = lastClose != null && prevClose != null ? lastClose > prevClose : true
  const lineIsFlat = lastClose != null && prevClose != null ? lastClose === prevClose : false
  const lineColor = lineIsFlat ? '#A1A1AA' : lineIsUp ? '#C74040' : '#2D9B65'
  const areaFill = lineIsFlat ? 'rgba(180,180,190,0.40)' : lineIsUp ? 'rgba(199,64,64,0.40)' : 'rgba(34,197,94,0.40)'

  useEffect(() => {
    setInfoIdx(data.length - 1)
  }, [data.length])

  useEffect(() => {
    const el = containerRef.current
    if (!el) return

    let chart = chartRef.current
    if (!chart) {
      chart = echarts.init(el, undefined, { renderer: 'canvas' })
      chartRef.current = chart
      // 强制 canvas 使用十字光标，覆盖 ECharts 默认的 pointer
      const forceCursor = () => {
        const canvases = el.querySelectorAll('canvas')
        canvases.forEach(c => { c.style.setProperty('cursor', 'crosshair', 'important') })
      }
      forceCursor()
      // MutationObserver: ECharts 内部可能重建/修改 canvas 属性，持续强制 cursor
      const mo = new MutationObserver(forceCursor)
      mo.observe(el, { childList: true, subtree: true, attributes: true, attributeFilter: ['style', 'class'] })
      moRef.current = mo
      roRef.current = new ResizeObserver(() => {
        chart!.resize()
        forceCursor()
      })
      roRef.current.observe(el)

      chart.on('updateAxisPointer', (event: any) => {
        const axesInfo = event.axesInfo
        if (!axesInfo) return
        for (const info of Object.values(axesInfo)) {
          const val = (info as any)?.value
          if (val == null) continue
          const fullDayIdx = typeof val === 'number' ? val : -1
          if (fullDayIdx >= 0) {
            const dataIdx = fullDayToDataIdx.current.get(fullDayIdx) ?? -1
            setInfoIdx(dataIdx)
            const d = dataRef.current
            if (dataIdx >= 0 && dataIdx < d.length) {
              onPriceHoverRef.current?.(d[dataIdx].close)
            }
            return
          }
        }
      })

      chart.on('globalout', () => {
        onPriceHoverRef.current?.(null)
      })
    }

    if (data.length > 0) {
      // 构建全日索引 → 数据索引 的映射
      const timeIndexMap = new Map(FULL_DAY_TIMES.map((t, i) => [t, i]))
      const mapping = new Map<number, number>()
      for (let i = 0; i < data.length; i++) {
        const timeKey = fmtTime(data[i].datetime)
        const fullDayIdx = timeIndexMap.get(timeKey)
        if (fullDayIdx !== undefined) {
          mapping.set(fullDayIdx, i)
        }
      }
      fullDayToDataIdx.current = mapping

      chart.setOption(buildOption(data, prevClose, avgPrices, lineColor, areaFill, yMode, ct, symbol, showLimitLines, showAvgLine, indicators, moneyFlowRows), true)
    } else {
      chart.clear()
    }
  }, [data, prevClose, height, lineColor, areaFill, yMode, ct, symbol, showLimitLines, showAvgLine, indicators, moneyFlowRows])

  useEffect(() => {
    return () => {
      chartRef.current?.off('updateAxisPointer')
      chartRef.current?.off('globalout')
      moRef.current?.disconnect()
      roRef.current?.disconnect()
      chartRef.current?.dispose()
      chartRef.current = null
      moRef.current = null
      roRef.current = null
    }
  }, [])

  const d = infoIdx >= 0 && infoIdx < data.length ? data[infoIdx] : null
  const avg = d != null ? avgPrices[infoIdx] : null
  const chg = d && prevClose != null ? d.close - prevClose : null
  const isUp = chg != null ? chg > 0 : true
  const isFlat = chg != null ? chg === 0 : false
  const priceClr = isFlat ? '#A1A1AA' : isUp ? '#C74040' : '#2D9B65'

  return (
    <div className="w-full">
      {/* 按钮行: 切换式按钮组, 居右 */}
      {showLimitLines && <div className="flex items-center justify-end px-1 pb-0.5">
        <div className="inline-flex items-center rounded bg-elevated overflow-hidden">
          <button
            onClick={() => setYMode('adaptive')}
            className={`px-2.5 py-0.5 text-[10px] font-mono cursor-pointer transition-colors ${
              yMode === 'adaptive'
                ? 'bg-accent/20 text-accent'
                : 'text-muted hover:text-secondary'
            }`}
          >
            自适应
          </button>
          <div className="w-px h-3 bg-border/40" />
          <button
            onClick={() => setYMode('limit')}
            className={`px-2.5 py-0.5 text-[10px] font-mono cursor-pointer transition-colors ${
              yMode === 'limit'
                ? 'bg-accent/20 text-accent'
                : 'text-muted hover:text-secondary'
            }`}
          >
            涨跌停
          </button>
        </div>
      </div>}
      <div style={{ backgroundColor: ct.infoBarBg }}>
        {/* 第一行: 日期 + OHLC */}
        <div className="flex items-center gap-x-2 px-2 font-mono text-[11px] select-none flex-wrap" style={{ height: 20 }}>
          {!d && <span className="text-muted">—</span>}
          {d && (
            <>
              {date && <span className="text-muted">{date}</span>}
              <span className="text-muted">开</span>
              <span style={{ color: priceClr }}>{d.open.toFixed(2)}</span>
              <span className="text-muted">高</span>
              <span style={{ color: priceClr }}>{d.high.toFixed(2)}</span>
              <span className="text-muted">低</span>
              <span style={{ color: priceClr }}>{d.low.toFixed(2)}</span>
              <span className="text-muted">收</span>
              <span style={{ color: priceClr }} className="font-semibold">{d.close.toFixed(2)}</span>
            </>
          )}
        </div>
        {/* 第二行: 价格+均价+量+额 */}
        <div className="flex items-center gap-x-4 px-2 font-mono text-[11px] select-none" style={{ height: 20 }}>
          {d && (
            <>
              <span className="flex items-center gap-x-1">
                <span style={{ display: 'inline-block', width: 14, height: 2, background: priceClr }} />
                <span style={{ color: priceClr }}>{d.close.toFixed(2)}</span>
              </span>
              {showAvgLine && <span className="flex items-center gap-x-1">
                <span style={{ display: 'inline-block', width: 14, height: 2, background: THEME.avgLine }} />
                <span style={{ color: THEME.avgLine }}>{avg?.toFixed(2)}</span>
              </span>}
              <span className="text-muted">量</span>
              <span className="text-secondary">{d.volume.toFixed(0)}</span>
              <span className="text-muted">额</span>
              <span className="text-secondary">{fmtAmt(d.amount)}</span>
            </>
          )}
        </div>
      </div>
      <div ref={containerRef} className="w-full" style={{ height: height - 42, cursor: 'crosshair' }} />
    </div>
  )
}
