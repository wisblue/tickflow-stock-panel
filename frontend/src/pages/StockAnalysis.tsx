import { useEffect, useRef, useState } from 'react'
import { useQuery, type UseQueryResult } from '@tanstack/react-query'
import { Sparkles, LineChart, History as HistoryIcon, Loader2, ExternalLink, Bell, Trophy, Activity, Database } from 'lucide-react'
import * as echarts from 'echarts'
import type { ECharts, EChartsOption } from 'echarts'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState } from '@/components/EmptyState'
import { StockFinancialSearch } from '@/components/financials/StockFinancialSearch'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'
import { LastStockChip } from '@/components/LastStockChip'
import { AnalysisKChart, type PriceLevel, type LevelType } from '@/components/stock-analysis/AnalysisKChart'
import { api, type StockBuyRankOutput, type StockBuyRankRow, type TransactionIntradayOutput } from '@/lib/api'
import { useLastStock } from '@/lib/useLastStock'
import { QK } from '@/lib/queryKeys'
import { toast } from '@/components/Toast'
import {
  startAnalysis, findTodayReport, useHistoryReports,
  deleteReport, openHistoryReport,
} from '@/lib/stockAnalysisStore'

/**
 * 个股分析页 —— 日 K + 关键价位(压力/支撑/密集区/枢轴/前高前低)+ AI 四维分析。
 *
 * 与财务分析页的区别:
 *  - 以【行情 + 关键价位】为视觉主体(专用日 K 图表,不复用个股对话框图表)
 *  - AI 分析输出买卖区间 / 操作建议(非财务质量评级)
 *  - 报告胶囊用蓝色系,与财务分析(紫色)并存
 */
export function StockAnalysis() {
  const [symbol, setSymbol] = useState<string>('')
  const [name, setName] = useState<string>('')
  const [checking, setChecking] = useState(false)
  const [confirmReport, setConfirmReport] = useState<{ id: string; created_at: string; focus: string } | null>(null)
  const [showHistory, setShowHistory] = useState(false)
  const [previewSymbol, setPreviewSymbol] = useState<string | null>(null)
  const { last: lastStock, remember: rememberStock } = useLastStock('stock-analysis')

  const onSelect = (sym: string, nm: string) => {
    setSymbol(sym)
    setName(nm)
    setShowHistory(false)
    setConfirmReport(null)
    rememberStock(sym, nm)
  }

  const handleAnalyze = async () => {
    if (!symbol || checking) return
    setChecking(true)
    try {
      // 当日已分析过 → 二次确认(查看今日报告 / 重新分析)
      const today = await findTodayReport(symbol)
      if (today) {
        setConfirmReport({ id: today.id, created_at: today.created_at, focus: today.focus })
      } else {
        await doAnalysis()
      }
    } catch {
      await doAnalysis()
    } finally {
      setChecking(false)
    }
  }

  const doAnalysis = async () => {
    const r = await startAnalysis(symbol, name)
    if (r.error) toast(r.error, 'error')
  }

  return (
    <>
      <PageHeader
        title="个股分析"
        titleExtra={
          <span className="inline-flex items-center rounded-full border border-amber-400/30 bg-amber-400/10 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-amber-400">
            Beta
          </span>
        }
        subtitle="日 K · 关键价位 · AI 四维分析(技术 / 基本面 / 财务 / 消息面)"
        right={
          <div className="flex items-center gap-2">
            <LastStockChip stock={lastStock} onSelect={onSelect} />
            {symbol && (
              <button
                onClick={() => setShowHistory(v => !v)}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-btn border border-border text-secondary text-xs hover:text-foreground hover:bg-elevated transition-colors"
              >
                <HistoryIcon className="h-3.5 w-3.5" />
                历史报告
              </button>
            )}
          </div>
        }
      />

      <div className="w-full px-8 py-6 space-y-6">
        {/* 搜索栏 */}
        <div className="flex items-center gap-3">
          <div className="w-72">
            <StockFinancialSearch onSelect={onSelect} />
          </div>
          {symbol && (
            <>
              <button
                onClick={() => setPreviewSymbol(symbol)}
                title="查看个股日 K 详情"
                className="group flex items-center gap-2 text-sm rounded-md px-1.5 py-0.5 -mx-1.5 hover:bg-elevated transition-colors"
              >
                <span className="text-foreground font-medium group-hover:text-sky-300 transition-colors">{name || symbol}</span>
                <span className="text-[10px] font-mono text-muted">{symbol}</span>
                <ExternalLink className="h-3 w-3 text-muted opacity-0 group-hover:opacity-100 transition-opacity" />
              </button>
              <button
                onClick={handleAnalyze}
                disabled={checking}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-btn bg-gradient-to-r from-sky-500/25 to-blue-500/15 border border-sky-400/30 text-sky-300 text-xs font-medium hover:from-sky-500/35 hover:to-blue-500/25 transition-all disabled:opacity-40 disabled:cursor-not-allowed"
              >
                {checking ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}
                AI 个股分析
              </button>
              <button
                onClick={() => toast('点位提醒功能开发中,敬请期待', 'error')}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-btn border border-border/40 bg-elevated/40 text-muted text-xs font-medium hover:border-border/70 hover:text-secondary transition-all"
                title="当价格触及关键价位时提醒(开发中)"
              >
                <Bell className="h-3.5 w-3.5" />
                点位提醒
                <span className="rounded-full bg-amber-400/15 px-1.5 py-px text-[9px] font-semibold uppercase tracking-wider text-amber-400">
                  开发中
                </span>
              </button>
            </>
          )}
        </div>

        {/* 主体 */}
        {!symbol ? (
          <EmptyState
            icon={LineChart}
            title="选择一只股票开始分析"
            hint="搜索代码或名称,查看日 K 与关键价位,并可让 AI 进行技术面 / 基本面 / 财务面 / 消息面四维综合分析。"
          />
        ) : showHistory ? (
          <HistoryList symbol={symbol} />
        ) : (
          <StockAnalysisBoard symbol={symbol} />
        )}
      </div>

      {/* 二次确认:已有历史报告 */}
      {confirmReport && (
        <ConfirmModal
          report={confirmReport}
          onView={() => { openHistoryReport(confirmReport.id); setConfirmReport(null) }}
          onRedo={async () => { setConfirmReport(null); await doAnalysis() }}
          onClose={() => setConfirmReport(null)}
        />
      )}

      {/* 个股日 K 详情对话框(点击名称/代码打开) */}
      <StockPreviewDialog
        symbol={previewSymbol}
        name={previewSymbol === symbol ? name : undefined}
        triggerInfo={null}
        onClose={() => setPreviewSymbol(null)}
      />
    </>
  )
}

// ===== 分析看板:日 K + 关键价位 =====
function StockAnalysisBoard({ symbol }: { symbol: string }) {
  const kline = useQuery({
    queryKey: ['kline', symbol, ''],
    queryFn: () => api.klineDaily(symbol, 250),
    enabled: !!symbol,
    staleTime: 60_000,
  })

  const levelsQ = useQuery({
    queryKey: QK.stockLevels(symbol),
    queryFn: () => api.stockAnalysisLevels(symbol, 250),
    enabled: !!symbol,
    staleTime: 60_000,
  })

  const buyRankQ = useQuery({
    queryKey: QK.stockBuyRank(symbol),
    queryFn: () => api.stockBuyRank(symbol),
    enabled: !!symbol,
    staleTime: 30_000,
  })

  const transactionQ = useQuery({
    queryKey: QK.transactionIntraday(symbol),
    queryFn: () => api.transactionIntraday(symbol),
    enabled: !!symbol,
    staleTime: 30_000,
  })

  if (kline.isLoading) {
    return <div className="flex items-center justify-center py-20"><Loader2 className="h-5 w-5 animate-spin text-muted" /></div>
  }

  const rows = kline.data?.rows ?? []
  if (rows.length === 0) {
    return <EmptyState icon={LineChart} title="暂无日 K 数据" hint="该标的尚未同步日 K,请先在数据页或自选页同步。" />
  }

  const levels = (levelsQ.data?.levels ?? {}) as Record<LevelType, PriceLevel[]>

  // 涨跌色:最后一根 K 线收 vs 前一根收(无前日则按开收判断)
  const last = rows[rows.length - 1]
  const prev = rows[rows.length - 2]
  const curClose = levelsQ.data?.close
  const isUp = prev ? (last.close >= prev.close) : (last.close >= last.open)

  return (
    <div className="space-y-4">
      <div className="rounded-card border border-border/60 bg-surface/40 overflow-hidden">
        <div className="px-4 py-3 border-b border-border/40">
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-2 min-w-0">
              <LineChart className="h-4 w-4 text-sky-400 shrink-0" />
              <span className="text-sm font-medium text-foreground">关键价位分析</span>
            </div>
            <div className="flex items-baseline gap-2 shrink-0">
              <span className="text-[10px] text-muted">{rows.length} 个交易日</span>
              <span className="text-[10px] text-muted/60">·</span>
              <span className="text-[10px] text-muted">当前价</span>
              <span className={`text-base font-mono font-bold ${isUp ? 'text-bull' : 'text-bear'}`}>
                {curClose?.toFixed(2) ?? '—'}
              </span>
            </div>
          </div>
        </div>
        <div className="p-3">
          <AnalysisKChart
            rows={rows}
            levels={levels}
            series={levelsQ.data?.series}
            seriesDates={levelsQ.data?.dates}
            defaultLevelTypes={['sr', 'pivot', 'keltner_s']}
            height={480}
          />
        </div>
      </div>
      <TransactionIntradayPanel symbol={symbol} query={transactionQ} />
      <StockBuyRankPanel symbol={symbol} query={buyRankQ} />
    </div>
  )
}

function TransactionIntradayPanel({ symbol, query }: { symbol: string; query: UseQueryResult<TransactionIntradayOutput> }) {
  const data = query.data
  const rows = data?.rows ?? []

  return (
    <div className="rounded-card border border-border/60 bg-surface/40 overflow-hidden">
      <div className="px-4 py-3 border-b border-border/40">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2 min-w-0">
            <Activity className="h-4 w-4 text-cyan-400 shrink-0" />
            <span className="text-sm font-medium text-foreground">Transaction 分时资金</span>
            {data?.trade_date && <span className="text-[10px] text-muted">{data.trade_date}</span>}
          </div>
          <div className="flex items-center gap-2 text-[10px] text-muted shrink-0">
            {query.isFetching && <Loader2 className="h-3 w-3 animate-spin" />}
            {data?.summary && (
              <>
                <span>{data.summary.min_time}-{data.summary.max_time}</span>
                <span>{data.summary.points} 点</span>
              </>
            )}
          </div>
        </div>
      </div>

      {query.isLoading ? (
        <div className="flex items-center justify-center py-10"><Loader2 className="h-5 w-5 animate-spin text-muted" /></div>
      ) : !data?.available || rows.length === 0 ? (
        <div className="flex items-center gap-2 px-4 py-5 text-xs text-muted">
          <Database className="h-4 w-4" />
          <span>{data?.message || '暂无 transaction 分时数据'}</span>
        </div>
      ) : (
        <div className="p-4 space-y-3">
          <div className="grid grid-cols-2 md:grid-cols-5 gap-2 text-right">
            <div className="text-left">
              <div className="text-[10px] text-muted">源文件</div>
              <div className="truncate text-[10px] text-secondary">{data.source_path || '—'}</div>
            </div>
            <Metric label="最新价" value={fmtNum(data.summary?.last_price, 2)} />
            <Metric label="全量净额" value={`${fmtSigned(data.summary?.full_net_w, 0)}w`} tone={(data.summary?.full_net_w ?? 0) >= 0 ? 'up' : 'down'} />
            <Metric label="主力净额" value={`${fmtSigned(data.summary?.main_net_w, 0)}w`} tone={(data.summary?.main_net_w ?? 0) >= 0 ? 'up' : 'down'} />
            <Metric label="盘后额" value={`${fmtNum(data.summary?.after_amount_w, 0)}w`} />
          </div>
          <TransactionIntradayChart data={data} symbol={symbol} />
        </div>
      )}
    </div>
  )
}

function TransactionIntradayChart({ data, symbol }: { data: TransactionIntradayOutput; symbol: string }) {
  const ref = useRef<HTMLDivElement>(null)
  const chartRef = useRef<ECharts | null>(null)
  const rows = data.rows ?? []

  useEffect(() => {
    if (!ref.current) return
    if (!chartRef.current) {
      chartRef.current = echarts.init(ref.current, undefined, { renderer: 'canvas' })
    }
    const chart = chartRef.current
    const times = rows.map((r) => r.time)
    const price = rows.map((r) => r.price)
    const fullNet = rows.map((r) => r.full_net_w)
    const mainNet = rows.map((r) => r.main_net_w)
    const afterAmt = rows.map((r) => r.after_amount_cum_w)
    const option: EChartsOption = {
      backgroundColor: 'transparent',
      animation: false,
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross' },
        formatter: (params: any) => {
          const items = Array.isArray(params) ? params : [params]
          const idx = items[0]?.dataIndex ?? 0
          const r = rows[idx]
          if (!r) return ''
          return [
            `${symbol} ${r.time}`,
            `价格 ${r.price.toFixed(2)}`,
            `全量净额 ${fmtSigned(r.full_net_w, 0)}w`,
            `主力净额 ${fmtSigned(r.main_net_w, 0)}w`,
            r.after_amount_cum_w > 0 ? `盘后累计 ${fmtNum(r.after_amount_cum_w, 0)}w` : '',
          ].filter(Boolean).join('<br/>')
        },
      },
      legend: {
        top: 4,
        right: 8,
        textStyle: { color: '#94a3b8', fontSize: 10 },
        itemWidth: 12,
        itemHeight: 6,
      },
      grid: { left: 48, right: 58, top: 34, bottom: 28 },
      xAxis: {
        type: 'category',
        data: times,
        boundaryGap: false,
        axisLabel: { color: '#94a3b8', fontSize: 10, hideOverlap: true },
        axisLine: { lineStyle: { color: 'rgba(148,163,184,0.25)' } },
        axisTick: { show: false },
      },
      yAxis: [
        {
          type: 'value',
          scale: true,
          name: '价格',
          nameTextStyle: { color: '#94a3b8', fontSize: 10 },
          axisLabel: { color: '#94a3b8', fontSize: 10 },
          splitLine: { lineStyle: { color: 'rgba(148,163,184,0.12)' } },
        },
        {
          type: 'value',
          scale: true,
          name: '万元',
          nameTextStyle: { color: '#94a3b8', fontSize: 10 },
          axisLabel: { color: '#94a3b8', fontSize: 10, formatter: (v: number) => `${Math.round(v / 10000)}亿` },
          splitLine: { show: false },
        },
      ],
      dataZoom: [
        { type: 'inside', xAxisIndex: 0 },
        { type: 'slider', height: 16, bottom: 4, borderColor: 'rgba(148,163,184,0.2)', textStyle: { color: '#94a3b8', fontSize: 9 } },
      ],
      series: [
        {
          name: '价格',
          type: 'line',
          yAxisIndex: 0,
          data: price,
          smooth: false,
          showSymbol: false,
          lineStyle: { width: 1.4, color: '#38bdf8' },
        },
        {
          name: '全量净额',
          type: 'line',
          yAxisIndex: 1,
          data: fullNet,
          smooth: false,
          showSymbol: false,
          lineStyle: { width: 1.2, color: '#f59e0b' },
        },
        {
          name: '主力净额',
          type: 'line',
          yAxisIndex: 1,
          data: mainNet,
          smooth: false,
          showSymbol: false,
          lineStyle: { width: 1.2, color: '#ef4444' },
        },
        {
          name: '盘后额',
          type: 'line',
          yAxisIndex: 1,
          data: afterAmt,
          smooth: false,
          showSymbol: false,
          lineStyle: { width: 1, color: '#22c55e', type: 'dashed' },
        },
      ],
    }
    chart.setOption(option, true)
    const resize = () => chart.resize()
    window.addEventListener('resize', resize)
    return () => window.removeEventListener('resize', resize)
  }, [data, rows, symbol])

  useEffect(() => () => chartRef.current?.dispose(), [])

  return <div ref={ref} className="h-[360px] w-full" />
}

function StockBuyRankPanel({ symbol, query }: { symbol: string; query: UseQueryResult<StockBuyRankOutput> }) {
  const data = query.data
  const rows = data?.rows ?? []
  const best = data?.best ?? rows.find((r) => r.sbr_rank === 1)
  const matched = data?.matched ?? rows.find((r) => r.stock_code === symbol)
  const primary = matched ?? best
  const dateText = data?.trade_date ? `${data.trade_date}${data.asof ? ` ${String(data.asof).padStart(4, '0')}` : ''}` : 'latest'
  const primaryComments = primary ? stockBuyRankComments(primary) : []

  return (
    <div className="rounded-card border border-border/60 bg-surface/40 overflow-hidden">
      <div className="px-4 py-3 border-b border-border/40">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2 min-w-0">
            <Trophy className="h-4 w-4 text-amber-400 shrink-0" />
            <span className="text-sm font-medium text-foreground">Stock-Buy-Rank 输出</span>
            {data?.source_model && <span className="text-[10px] text-muted">来自 {data.source_model}</span>}
          </div>
          <div className="flex items-center gap-2 text-[10px] text-muted shrink-0">
            {query.isFetching && <Loader2 className="h-3 w-3 animate-spin" />}
            <span>{dateText}</span>
            {data?.status && <span className={data.status === 'pass' ? 'text-success' : 'text-warning'}>{data.status}</span>}
          </div>
        </div>
      </div>

      {query.isLoading ? (
        <div className="flex items-center justify-center py-10"><Loader2 className="h-5 w-5 animate-spin text-muted" /></div>
      ) : !data?.available ? (
        <div className="flex items-center gap-2 px-4 py-5 text-xs text-muted">
          <Database className="h-4 w-4" />
          <span>{data?.message || '暂无 stock-buy-rank 输出'}</span>
        </div>
      ) : (
        <div className="p-4 space-y-4">
          {data.message && (
            <div className="flex items-center gap-2 rounded-md border border-border/40 bg-elevated/30 px-3 py-2 text-xs text-muted">
              <Activity className="h-3.5 w-3.5 text-sky-400" />
              <span>{data.message}</span>
            </div>
          )}

          {primary && (
            <div className="grid grid-cols-1 md:grid-cols-[1fr_auto] gap-3">
              <div className="min-w-0">
                <div className="text-[10px] text-muted mb-1">{matched ? '当前个股扫描结果' : '最终买入优先'}</div>
                <div className="flex flex-wrap items-baseline gap-2">
                  <span className="text-lg font-semibold text-foreground">{primary.stock_code}</span>
                  <span className="text-sm text-secondary">{primary.name || '—'}</span>
                  <span className="text-xs text-amber-300">Rank {primary.sbr_rank ?? '—'} {primary.sbr_label || ''}</span>
                </div>
                {primaryComments.length > 0 && (
                  <div className="mt-2 flex flex-wrap gap-1.5">
                    {primaryComments.map((comment) => (
                      <span
                        key={comment}
                        className="rounded-md border border-amber-400/25 bg-amber-400/10 px-2 py-0.5 text-[10px] text-amber-200"
                      >
                        {comment}
                      </span>
                    ))}
                  </div>
                )}
              </div>
              <div className="grid grid-cols-4 gap-2 text-right">
                <Metric label="SBR分" value={fmtNum(primary.sbr_score, 0)} />
                <Metric label="涨跌" value={`${fmtSigned(primary.ret_pct, 1)}%`} tone={(primary.ret_pct ?? 0) >= 0 ? 'up' : 'down'} />
                <Metric label="主动净买" value={`${fmtSigned(primary.net_w, 0)}w`} tone={(primary.net_w ?? 0) >= 0 ? 'up' : 'down'} />
                <Metric label="等级" value={primary.sbr_grade || '—'} />
              </div>
              {hasMainForce(primary) && (
                <div className="md:col-span-2 grid grid-cols-2 md:grid-cols-5 gap-2 rounded-md border border-border/40 bg-elevated/20 px-3 py-2 text-right">
                  <div className="text-left">
                    <div className="text-[10px] text-muted">主力估算</div>
                    <div className="text-[10px] text-secondary">均笔金额代理</div>
                  </div>
                  <Metric label="主力净" value={`${fmtSigned(primary.main_net_w, 0)}w`} tone={(primary.main_net_w ?? 0) >= 0 ? 'up' : 'down'} />
                  <Metric label="顶主力" value={`${fmtSigned(primary.main_top_net_w, 0)}w`} tone={(primary.main_top_net_w ?? 0) >= 0 ? 'up' : 'down'} />
                  <Metric label="底主力" value={`${fmtSigned(primary.main_bot_net_w, 0)}w`} tone={(primary.main_bot_net_w ?? 0) >= 0 ? 'up' : 'down'} />
                  <Metric label="主力占比" value={`${fmtNum(primary.main_share_pct, 1)}%`} />
                </div>
              )}
              {hasAfterHours(primary) && (
                <div className="md:col-span-2 grid grid-cols-2 md:grid-cols-5 gap-2 rounded-md border border-cyan-400/25 bg-cyan-400/10 px-3 py-2 text-right">
                  <div className="text-left">
                    <div className="text-[10px] text-cyan-300">盘后定价</div>
                    <div className="text-[10px] text-secondary">15:05-15:30</div>
                  </div>
                  <Metric label="盘后额" value={`${fmtNum(primary.after_amt_w, 0)}w`} />
                  <Metric label="盘后主力" value={`${fmtNum(primary.after_main_amt_w, 0)}w`} />
                  <Metric label="主力占比" value={`${fmtNum(primary.after_main_share_pct, 1)}%`} />
                  <Metric label="盘后价" value={fmtNum(primary.after_price, 2)} />
                </div>
              )}
            </div>
          )}

          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead className="text-muted">
                <tr className="border-b border-border/40">
                  <th className="py-2 pr-3 text-left font-medium">SBR</th>
                  <th className="py-2 pr-3 text-left font-medium">股票</th>
                  <th className="py-2 pr-3 text-right font-medium">分数</th>
                  <th className="py-2 pr-3 text-right font-medium">收盘</th>
                  <th className="py-2 pr-3 text-right font-medium">涨跌</th>
                  <th className="py-2 pr-3 text-right font-medium">主动净买</th>
                  <th className="py-2 pr-3 text-right font-medium">主力净</th>
                  <th className="py-2 pr-3 text-left font-medium">判断</th>
                  <th className="py-2 text-right font-medium">模型排位</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r: StockBuyRankRow) => {
                  const comments = stockBuyRankComments(r)
                  return (
                    <tr key={r.stock_code} className={`border-b border-border/25 last:border-0 ${r.stock_code === symbol ? 'bg-sky-400/5' : ''}`}>
                      <td className="py-2 pr-3 text-left text-amber-300">{r.sbr_rank ?? '—'}</td>
                      <td className="py-2 pr-3 text-left">
                        <span className="font-mono text-foreground">{r.stock_code}</span>
                        <span className="ml-2 text-secondary">{r.name || '—'}</span>
                      </td>
                      <td className="py-2 pr-3 text-right font-mono text-foreground">{fmtNum(r.sbr_score, 0)}</td>
                      <td className="py-2 pr-3 text-right font-mono text-secondary">{fmtNum(r.close, 2)}</td>
                      <td className={`py-2 pr-3 text-right font-mono ${(r.ret_pct ?? 0) >= 0 ? 'text-bull' : 'text-bear'}`}>{fmtSigned(r.ret_pct, 1)}%</td>
                      <td className={`py-2 pr-3 text-right font-mono ${(r.net_w ?? 0) >= 0 ? 'text-bull' : 'text-bear'}`}>{fmtSigned(r.net_w, 0)}w</td>
                      <td className={`py-2 pr-3 text-right font-mono ${(r.main_net_w ?? 0) >= 0 ? 'text-bull' : 'text-bear'}`}>{fmtSigned(r.main_net_w, 0)}w</td>
                      <td className="py-2 pr-3 text-left text-[10px] text-secondary">
                        {comments.length > 0 ? (
                          <div className="flex max-w-80 flex-wrap gap-1">
                            {comments.slice(0, 3).map((comment) => (
                              <span key={comment} className="rounded border border-border/40 bg-elevated/35 px-1.5 py-0.5">
                                {comment}
                              </span>
                            ))}
                          </div>
                        ) : '—'}
                      </td>
                      <td className="py-2 text-right font-mono text-muted">{r.model_rank ?? '—'}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>

          {data.source_path && <div className="truncate text-[10px] text-muted/70">源文件: {data.source_path}</div>}
        </div>
      )}
    </div>
  )
}

function stockBuyRankComments(row: StockBuyRankRow): string[] {
  const parts = [row.sbr_label, row.reasons]
    .flatMap((value) => String(value || '').split('|'))
    .map((value) => value.trim())
    .filter(Boolean)
  return Array.from(new Set(parts))
}

function hasMainForce(row: StockBuyRankRow): boolean {
  return [row.main_net_w, row.main_top_net_w, row.main_bot_net_w, row.main_share_pct]
    .some((value) => typeof value === 'number' && Number.isFinite(value))
}

function hasAfterHours(row: StockBuyRankRow): boolean {
  return typeof row.after_amt_w === 'number' && Number.isFinite(row.after_amt_w) && row.after_amt_w > 0
}

function Metric({ label, value, tone }: { label: string; value: string; tone?: 'up' | 'down' }) {
  return (
    <div>
      <div className="text-[10px] text-muted">{label}</div>
      <div className={`font-mono text-sm ${tone === 'up' ? 'text-bull' : tone === 'down' ? 'text-bear' : 'text-foreground'}`}>{value}</div>
    </div>
  )
}

function fmtNum(v: unknown, digits: number): string {
  return typeof v === 'number' && Number.isFinite(v) ? v.toFixed(digits) : '—'
}

function fmtSigned(v: unknown, digits: number): string {
  return typeof v === 'number' && Number.isFinite(v) ? `${v >= 0 ? '+' : ''}${v.toFixed(digits)}` : '—'
}

// ===== 历史报告列表 =====
function HistoryList({ symbol }: { symbol: string }) {
  const { reports, loaded } = useHistoryReports()
  const mine = reports.filter(r => r.symbol === symbol)

  if (!loaded) {
    return <div className="flex items-center justify-center py-20"><Loader2 className="h-5 w-5 animate-spin text-muted" /></div>
  }
  if (mine.length === 0) {
    return <EmptyState icon={HistoryIcon} title="暂无历史报告" hint={`还没有 ${symbol} 的个股分析报告,点击「AI 个股分析」生成第一份。`} />
  }

  return (
    <div className="space-y-2">
      {mine.map(r => (
        <div key={r.id} className="rounded-card border border-border/60 bg-surface/40 p-3 hover:border-border transition-colors">
          <div className="flex items-center justify-between gap-3">
            <button onClick={() => openHistoryReport(r.id)} className="flex-1 text-left min-w-0">
              <div className="flex items-center gap-2">
                <span className="text-xs text-secondary">{fmtRelative(r.created_at)}</span>
                {r.close && <span className="text-[10px] font-mono text-muted">价 {r.close.toFixed(2)}</span>}
                {r.focus && <span className="text-[10px] text-sky-300/70 truncate">关注: {r.focus}</span>}
              </div>
              <div className="mt-1 text-xs text-muted truncate">{r.summary || '点击查看完整报告'}</div>
            </button>
            <button
              onClick={() => { deleteReport(r.id); toast('已删除', 'success') }}
              className="shrink-0 text-[10px] text-muted hover:text-danger transition-colors px-2 py-1"
            >
              删除
            </button>
          </div>
        </div>
      ))}
    </div>
  )
}

// ===== 二次确认弹窗 =====
function ConfirmModal({ report, onView, onRedo, onClose }: {
  report: { id: string; created_at: string; focus: string }
  onView: () => void
  onRedo: () => void
  onClose: () => void
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm p-4" onClick={onClose}>
      <div
        className="w-full max-w-sm bg-surface border border-border rounded-2xl p-5 shadow-2xl"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center gap-2 mb-2">
          <HistoryIcon className="h-4 w-4 text-sky-400" />
          <span className="text-sm font-medium text-foreground">该个股已有分析报告</span>
        </div>
        <p className="text-xs text-secondary leading-relaxed mb-1">
          最近一次报告生成于 <span className="text-foreground">{fmtRelative(report.created_at)}</span>。
        </p>
        {report.focus && <p className="text-xs text-muted mb-1">关注点: {report.focus}</p>}
        <p className="text-xs text-muted mb-4">可直接查看历史,或重新生成一份新报告。</p>
        <div className="flex gap-2">
          <button onClick={onView}
            className="flex-1 h-8 rounded-lg bg-elevated border border-border text-xs text-secondary hover:text-foreground transition-colors">
            查看历史
          </button>
          <button onClick={onRedo}
            className="flex-1 h-8 rounded-lg bg-gradient-to-r from-sky-500/20 to-blue-500/15 border border-sky-400/30 text-xs text-sky-300 hover:from-sky-500/30 transition-all">
            重新分析
          </button>
        </div>
      </div>
    </div>
  )
}

function fmtRelative(iso: string): string {
  try {
    const t = new Date(iso).getTime()
    const diff = Date.now() - t
    if (diff < 60_000) return '刚刚'
    if (diff < 3600_000) return `${Math.floor(diff / 60_000)} 分钟前`
    if (diff < 86400_000) return `${Math.floor(diff / 3600_000)} 小时前`
    if (diff < 7 * 86400_000) return `${Math.floor(diff / 86400_000)} 天前`
    return new Date(iso).toLocaleDateString('zh-CN')
  } catch { return iso }
}
