import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { BellRing, ChevronRight, Clock3, Loader2, X } from 'lucide-react'
import { api, type ModelV4Sr013RealtimeRow } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'

function pct(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(Number(value))) return '—'
  return `${Number(value) >= 0 ? '+' : ''}${(Number(value) * 100).toFixed(2)}%`
}

function price(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(Number(value))) return '—'
  return Number(value).toFixed(2)
}

function time(value: string | undefined): string {
  if (!value) return '待触发'
  return value.length >= 5 ? value.slice(0, 5) : value
}

function returnClass(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(Number(value)) || Number(value) === 0) return 'text-secondary'
  return Number(value) > 0 ? 'text-bull' : 'text-bear'
}

function sellSortKey(row: ModelV4Sr013RealtimeRow): string {
  if (row.sell_time) return `0-${row.sell_time}-${row.stock_code}`
  if (row.status === 'sell_triggered_fill_pending') return `1-${row.signal_time || '99:99:99'}-${row.stock_code}`
  return `2-${row.stock_code}`
}

function StatusLabel({ row }: { row: ModelV4Sr013RealtimeRow }) {
  if (row.status === 'sell_triggered') {
    return <span className="text-bull">已触发</span>
  }
  if (row.status === 'sell_triggered_fill_pending') {
    return <span className="text-warning">信号待成交</span>
  }
  if (row.status === 'waiting_for_reference_price') {
    return <span className="text-warning">等待T日收盘价</span>
  }
  if (row.status === 'waiting_for_transaction_data') {
    return <span className="text-muted">等待逐笔</span>
  }
  return <span className="text-muted">持有中</span>
}

function TickerItem({ row }: { row: ModelV4Sr013RealtimeRow }) {
  return (
    <span className="inline-flex shrink-0 items-center gap-2 border-r border-border/60 px-4 text-[11px]">
      <span className="font-mono font-semibold text-foreground">{row.stock_code}</span>
      <span className="max-w-20 truncate text-secondary">{row.stock_name || '—'}</span>
      <span className="text-muted">卖 {time(row.sell_time)}</span>
      <span className="font-mono text-foreground">{price(row.sell_price)}</span>
      <span className={cn('font-mono', returnClass(row.gross_return))}>毛 {pct(row.gross_return)}</span>
      <span className={cn('font-mono', returnClass(row.actual_return))}>实际 {pct(row.actual_return)}</span>
      <StatusLabel row={row} />
    </span>
  )
}

export function ModelV4SellTicker() {
  const [open, setOpen] = useState(false)
  const query = useQuery({
    queryKey: QK.modelV4Sr013Realtime,
    queryFn: () => api.modelV4Sr013Realtime(),
    staleTime: 0,
    retry: false,
    refetchInterval: 60_000,
    refetchIntervalInBackground: true,
  })
  const rows = query.data?.rows ?? []
  const sortedRows = useMemo(
    () => [...rows].sort((left, right) => sellSortKey(left).localeCompare(sellSortKey(right))),
    [rows],
  )
  const tickerRows = useMemo(
    () => (sortedRows.length > 0 ? [...sortedRows, ...sortedRows] : []),
    [sortedRows],
  )

  return (
    <>
      <button
        type="button"
        className="sticky top-12 z-40 flex h-10 w-full items-center overflow-hidden border-b border-border bg-surface/95 text-left shadow-sm backdrop-blur md:top-0"
        onClick={() => setOpen(true)}
        title="打开今日持仓卖出明细"
        aria-label="打开今日持仓卖出明细"
      >
        <span className="flex h-full shrink-0 items-center gap-1.5 border-r border-border bg-elevated/80 px-3 text-[10px] font-semibold tracking-wide text-accent">
          <BellRing className="h-3.5 w-3.5" />
          今日持仓卖出监控
        </span>
        {query.isLoading ? (
          <span className="flex items-center gap-1.5 px-3 text-[11px] text-muted">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            正在读取持仓
          </span>
        ) : query.isError ? (
          <span className="px-3 text-[11px] text-warning">实时卖出监控暂不可用</span>
        ) : rows.length === 0 ? (
          <span className="px-3 text-[11px] text-muted">暂无 source=positions 持仓</span>
        ) : (
          <span className="model-v4-ticker-track flex min-w-max items-center">
            {tickerRows.map((row, index) => <TickerItem key={`${row.stock_code}-${index}`} row={row} />)}
          </span>
        )}
        <ChevronRight className="ml-auto mr-3 h-4 w-4 shrink-0 text-muted" />
      </button>

      {open && (
        <div className="fixed inset-0 z-[100] flex items-start justify-end bg-base/45 p-3 pt-14 backdrop-blur-sm md:pt-3">
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby="model-v4-sell-title"
            className="flex max-h-[calc(100vh-1.5rem)] w-full max-w-5xl flex-col overflow-hidden rounded-card border border-border bg-surface shadow-2xl"
          >
            <div className="flex items-start justify-between gap-4 border-b border-border px-4 py-3">
              <div>
                <div id="model-v4-sell-title" className="flex items-center gap-2 text-sm font-semibold text-foreground">
                  <BellRing className="h-4 w-4 text-accent" />
                  今日持仓卖出监控
                </div>
                <div className="mt-1 flex flex-wrap items-center gap-2 text-[10px] text-muted">
                  <span>{query.data?.trade_date || '—'}</span>
                  <span>SR013 ACT5 · T日收盘基准 · 5%激活 · 回撤2pp · 14:45兜底</span>
                  <span className="inline-flex items-center gap-1"><Clock3 className="h-3 w-3" />每分钟刷新</span>
                </div>
                <p className="mt-2 max-w-4xl text-[11px] leading-5 text-secondary">
                  {query.data?.rule_description || '盈利保护从10:00开始；风险保护从11:00开始；未触发时14:45卖出。'}
                </p>
              </div>
              <button
                type="button"
                onClick={() => setOpen(false)}
                className="rounded-btn p-1.5 text-muted transition-colors hover:bg-elevated hover:text-foreground"
                title="关闭"
                aria-label="关闭"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
            <div className="min-h-0 overflow-auto">
              <table className="w-full min-w-[1040px] text-xs">
                <thead className="sticky top-0 z-10 bg-elevated text-[10px] text-muted">
                  <tr>
                    <th className="px-3 py-2 text-left font-medium">股票代码</th>
                    <th className="px-3 py-2 text-left font-medium">股票名称</th>
                    <th className="px-3 py-2 text-left font-medium">信号时间</th>
                    <th className="px-3 py-2 text-left font-medium">卖出时间</th>
                    <th className="px-3 py-2 text-right font-medium">卖出价格</th>
                    <th className="px-3 py-2 text-right font-medium">T日收盘</th>
                    <th className="px-3 py-2 text-left font-medium">卖出规则</th>
                    <th className="px-3 py-2 text-right font-medium">毛收益</th>
                    <th className="px-3 py-2 text-right font-medium">实际收益</th>
                    <th className="px-3 py-2 text-right font-medium">最新价</th>
                    <th className="px-3 py-2 text-left font-medium">状态</th>
                  </tr>
                </thead>
                <tbody>
                  {sortedRows.map((row) => (
                    <tr key={row.stock_code} className="border-t border-border/60 hover:bg-elevated/40">
                      <td className="px-3 py-2 font-mono font-semibold text-foreground">{row.stock_code}</td>
                      <td className="px-3 py-2 text-secondary">{row.stock_name || '—'}</td>
                      <td className="px-3 py-2 font-mono text-secondary">{time(row.signal_time)}</td>
                      <td className="px-3 py-2 font-mono text-secondary">{time(row.sell_time)}</td>
                      <td className="px-3 py-2 text-right font-mono text-foreground">{price(row.sell_price)}</td>
                      <td className="px-3 py-2 text-right font-mono text-foreground">{price(row.t_close_price)}</td>
                      <td className="max-w-64 truncate px-3 py-2 text-secondary" title={row.sell_rule || ''}>{row.sell_reason_label || row.sell_rule || '—'}</td>
                      <td className={cn('px-3 py-2 text-right font-mono', returnClass(row.gross_return))}>{pct(row.gross_return)}</td>
                      <td className={cn('px-3 py-2 text-right font-mono', returnClass(row.actual_return))}>{pct(row.actual_return)}</td>
                      <td className="px-3 py-2 text-right font-mono text-foreground">{price(row.latest_price)}</td>
                      <td className="px-3 py-2"><StatusLabel row={row} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {rows.length === 0 && <div className="px-4 py-8 text-center text-xs text-muted">今日暂无持仓数据</div>}
            </div>
            <div className="flex items-center justify-between border-t border-border px-4 py-2 text-[10px] text-muted">
              <span>ACT5信号与收益统一使用T日收盘价；毛收益/实际收益 =（卖出价或最新价）÷ T日收盘价 − 1</span>
              <span>{query.data?.checked_at ? `更新 ${query.data.checked_at.slice(11, 19)}` : '—'}</span>
            </div>
          </div>
        </div>
      )}
    </>
  )
}
