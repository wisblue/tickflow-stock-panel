import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { PageHeader } from '@/components/PageHeader'
import { FactorBacktest } from './backtest/FactorBacktest'
import { StrategyBacktest } from './backtest/StrategyBacktest'
import { api, type S150Sr004TradeRow } from '@/lib/api'
import { fmtPct, fmtPrice, priceColorClass } from '@/lib/format'
import { BarChart3, Bot, FlaskConical } from 'lucide-react'

type Tab = 'factor' | 'strategy' | 's150'

const MODES: Record<Tab, { title: string; subtitle: string; hint: string }> = {
  factor: {
    title: '因子回测',
    subtitle: '验证单个因子是否有预测能力',
    hint: '看 IC / IR、分层收益和多空组合，适合先筛掉无效指标。',
  },
  strategy: {
    title: '策略回测',
    subtitle: '验证完整选股和交易规则',
    hint: '看净值曲线、回撤、胜率和交易明细，适合判断策略是否可执行。',
  },
  s150: {
    title: 'S150-SR004',
    subtitle: 'S150 14:45 推荐与 SR004 卖出明细',
    hint: '展示每日 14:45 生产结果，以及 SR004 决定卖价后的最近 20 日交易明细。',
  },
}

const fmtDate = (value: string) => {
  if (!value) return '—'
  if (value.length === 8) return `${value.slice(0, 4)}-${value.slice(4, 6)}-${value.slice(6, 8)}`
  return value
}

const fmtReturn = (value: number | null | undefined) => value == null ? '待结算' : fmtPct(value)

function S150StatusBadge({ status }: { status: string }) {
  const pending = status.includes('pending')
  const settled = status.includes('settled')
  return (
    <span className={`inline-flex rounded border px-1.5 py-0.5 text-[10px] ${
      settled
        ? 'border-emerald-400/30 bg-emerald-400/10 text-emerald-400'
        : pending
          ? 'border-amber-400/30 bg-amber-400/10 text-amber-400'
          : 'border-border bg-elevated text-secondary'
    }`}>
      {settled ? '已卖出' : pending ? '待卖出' : status || '—'}
    </span>
  )
}

function S150Row({ row }: { row: S150Sr004TradeRow }) {
  return (
    <tr className="border-b border-border/60 transition-colors hover:bg-elevated/35">
      <td className="px-3 py-2 font-mono text-muted">{row.index}</td>
      <td className="px-3 py-2 font-mono">{fmtDate(row.date)}</td>
      <td className="px-3 py-2 font-mono text-foreground">{row.stock_code || '—'}</td>
      <td className="px-3 py-2 text-foreground">{row.stock_name || '—'}</td>
      <td className="px-3 py-2 text-right font-mono">{fmtPrice(row.buy_price)}</td>
      <td className="px-3 py-2 text-right font-mono">
        <div className="flex items-center justify-end gap-2">
          <span>{fmtPrice(row.sell_price)}</span>
          <S150StatusBadge status={row.settlement_status} />
        </div>
      </td>
      <td className={`px-3 py-2 text-right font-mono ${priceColorClass(row.day_return)}`}>{fmtReturn(row.day_return)}</td>
      <td className={`px-3 py-2 text-right font-mono ${priceColorClass(row.cumulative_return)}`}>{fmtReturn(row.cumulative_return)}</td>
    </tr>
  )
}

function S150Sr004Panel() {
  const query = useQuery({
    queryKey: ['backtest', 's150-sr004'],
    queryFn: () => api.s150Sr004(),
    refetchInterval: 60_000,
  })
  const data = query.data
  const recommendation = data?.recommendation
  const selectedText = recommendation?.stock_code
    ? `今日14:45推荐：${recommendation.stock_code}${recommendation.stock_name ? ` ${recommendation.stock_name}` : ''}`
    : '今日14:45推荐：暂无'

  return (
    <div className="flex h-full min-h-0 flex-col gap-3">
      <section className="rounded-btn border border-border bg-surface p-4">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
          <div>
            <div className="text-xs text-secondary">S150-SR004</div>
            <h2 className="mt-1 text-xl font-semibold tracking-tight text-foreground">{selectedText}</h2>
            <div className="mt-1 text-xs text-muted">
              {data?.update_rule ?? '每个交易日 14:45 以后，S150-SR004 预测结果产出后自动更新。'}
            </div>
          </div>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:w-[34rem]">
            <div className="rounded-btn border border-border/70 bg-base/55 px-3 py-2">
              <div className="text-[11px] text-secondary">交易日</div>
              <div className="mt-1 font-mono text-sm text-foreground">{fmtDate(data?.trade_date ?? '')}</div>
            </div>
            <div className="rounded-btn border border-border/70 bg-base/55 px-3 py-2">
              <div className="text-[11px] text-secondary">买入价格</div>
              <div className="mt-1 font-mono text-sm text-foreground">{fmtPrice(recommendation?.buy_price)}</div>
            </div>
            <div className="rounded-btn border border-border/70 bg-base/55 px-3 py-2">
              <div className="text-[11px] text-secondary">日均收益</div>
              <div className={`mt-1 font-mono text-sm ${priceColorClass(data?.avg_day_return)}`}>{fmtReturn(data?.avg_day_return)}</div>
            </div>
            <div className="rounded-btn border border-border/70 bg-base/55 px-3 py-2">
              <div className="text-[11px] text-secondary">已结算</div>
              <div className="mt-1 font-mono text-sm text-foreground">{data?.settled_trade_count ?? 0}/{data?.trade_count ?? 0}</div>
            </div>
          </div>
        </div>
      </section>

      <section className="flex min-h-0 flex-1 flex-col rounded-btn border border-border bg-surface">
        <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-3">
          <div>
            <h3 className="text-sm font-medium text-foreground">每日交易明细</h3>
            <div className="mt-0.5 text-xs text-muted">最近 20 个 S150-SR004 交易日，卖出价格由 SR004 决定。</div>
          </div>
          {query.isFetching && <span className="text-xs text-muted">更新中...</span>}
        </div>

        {query.isLoading ? (
          <div className="grid flex-1 place-items-center text-sm text-muted">加载中...</div>
        ) : query.isError ? (
          <div className="grid flex-1 place-items-center px-4 text-sm text-danger">S150-SR004 数据加载失败</div>
        ) : !data?.available ? (
          <div className="grid flex-1 place-items-center px-4 text-sm text-muted">{data?.message || '暂无数据'}</div>
        ) : (
          <div className="min-h-0 flex-1 overflow-auto">
            <table className="min-w-[56rem] w-full text-left text-xs">
              <thead className="sticky top-0 z-10 bg-surface text-secondary">
                <tr className="border-b border-border">
                  <th className="px-3 py-2 font-medium">序号</th>
                  <th className="px-3 py-2 font-medium">日期</th>
                  <th className="px-3 py-2 font-medium">股票代码</th>
                  <th className="px-3 py-2 font-medium">股票名称</th>
                  <th className="px-3 py-2 text-right font-medium">买入价格</th>
                  <th className="px-3 py-2 text-right font-medium">卖出价格</th>
                  <th className="px-3 py-2 text-right font-medium">日收益</th>
                  <th className="px-3 py-2 text-right font-medium">累计收益</th>
                </tr>
              </thead>
              <tbody>
                {(data.trades ?? []).map(row => <S150Row key={`${row.date}-${row.stock_code}-${row.index}`} row={row} />)}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  )
}

export function Backtest() {
  const [activeTab, setActiveTab] = useState<Tab>('strategy')

  const modeSwitch = (
    <div className="inline-flex rounded-btn border border-border bg-surface/80 p-0.5 shadow-sm">
      {(['factor', 'strategy', 's150'] as const).map(tab => {
        const Icon = tab === 'factor' ? BarChart3 : tab === 'strategy' ? FlaskConical : Bot
        const active = activeTab === tab
        return (
          <button
            key={tab}
            onClick={() => setActiveTab(tab)}
            className={`inline-flex items-center gap-1.5 rounded-[5px] px-3 py-1.5 text-xs font-medium transition-colors cursor-pointer ${
              active
                ? 'bg-accent text-white shadow-sm'
                : 'text-secondary hover:bg-elevated hover:text-foreground'
            }`}
          >
            <Icon className="h-3.5 w-3.5" />
            {MODES[tab].title}
          </button>
        )
      })}
    </div>
  )

  return (
    <div className="min-h-full bg-base flex flex-col">
      <PageHeader
        title="回测工作台"
        subtitle={`${MODES[activeTab].title} · ${MODES[activeTab].hint}`}
        right={modeSwitch}
        className="shrink-0 bg-base/95"
      />

      <main className="flex-1 min-h-0 px-3 pb-3 pt-3 lg:px-4 lg:pb-4">
        {activeTab === 'factor' && <FactorBacktest />}
        {activeTab === 'strategy' && <StrategyBacktest />}
        {activeTab === 's150' && <S150Sr004Panel />}
      </main>
    </div>
  )
}
