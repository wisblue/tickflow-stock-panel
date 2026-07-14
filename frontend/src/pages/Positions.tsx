import { useCallback, useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { BriefcaseBusiness, Check, Loader2, Plus, Search, Trash2 } from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState } from '@/components/EmptyState'
import { StockDailyKChart, type StockDailyKChartResult } from '@/components/StockDailyKChart'
import { StockIntradayChart } from '@/components/StockIntradayChart'
import type { IntradayIndicator } from '@/components/EChartsIntraday'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import {
  addPositionStock,
  getActivePositionSymbol,
  loadPositions,
  removePositionStock,
  setActivePositionSymbol,
  subscribePositionsChanged,
  type PositionStock,
} from '@/lib/positions'

const INTRADAY_INDICATOR_KEYS = ['macd', 'rsi', 'kdj', 'boll', 'moneyflow'] as const
const INTRADAY_INDICATOR_LABELS: Record<IntradayIndicator, string> = {
  macd: 'MACD',
  rsi: 'RSI',
  kdj: 'KDJ',
  boll: 'BOLL',
  moneyflow: '资金',
}
const POSITIONS_INTRADAY_INDICATORS_KEY = 'positions-intraday-indicators'

function loadIntradayIndicators(): IntradayIndicator[] {
  try {
    const raw = localStorage.getItem(POSITIONS_INTRADAY_INDICATORS_KEY)
    const rows = raw ? JSON.parse(raw) : []
    if (!Array.isArray(rows)) return []
    const allowed = new Set<IntradayIndicator>(INTRADAY_INDICATOR_KEYS as unknown as IntradayIndicator[])
    return rows.filter((row): row is IntradayIndicator => allowed.has(row))
  } catch {
    return []
  }
}

function saveIntradayIndicators(rows: IntradayIndicator[]) {
  try {
    localStorage.setItem(POSITIONS_INTRADAY_INDICATORS_KEY, JSON.stringify(rows))
  } catch {
    // ignore storage failures
  }
}

export function Positions() {
  const [searchParams, setSearchParams] = useSearchParams()
  const [positions, setPositions] = useState<PositionStock[]>(() => loadPositions())
  const [activeSymbol, setActiveSymbol] = useState(() => getActivePositionSymbol())
  const [chartInfo, setChartInfo] = useState<StockDailyKChartResult | null>(null)
  const [intradayIndicators, setIntradayIndicators] = useState<IntradayIndicator[]>(() => loadIntradayIndicators())

  const refreshLocal = useCallback(() => {
    const nextRows = loadPositions()
    const nextActive = getActivePositionSymbol()
    setPositions(nextRows)
    setActiveSymbol(nextActive || nextRows[0]?.symbol || '')
  }, [])

  useEffect(() => subscribePositionsChanged(refreshLocal), [refreshLocal])

  useEffect(() => {
    const fromUrl = searchParams.get('symbol') || ''
    if (fromUrl && fromUrl !== activeSymbol) {
      setActivePositionSymbol(fromUrl)
      setActiveSymbol(fromUrl)
    }
  }, [activeSymbol, searchParams])

  useEffect(() => {
    if (!activeSymbol && positions[0]?.symbol) {
      setActivePositionSymbol(positions[0].symbol)
      setActiveSymbol(positions[0].symbol)
    }
  }, [activeSymbol, positions])

  const activePosition = useMemo(
    () => positions.find((row) => row.symbol === activeSymbol),
    [activeSymbol, positions],
  )

  const registerActiveStock = useCallback((symbol: string, name = '') => {
    if (!symbol) return
    api.activeStockAdd(symbol, name, 'positions').catch((error) => {
      console.warn('register active stock failed', error)
    })
  }, [])

  useEffect(() => {
    if (positions.length === 0) return
    api.activeStocksBatchAdd(positions.map((row) => row.symbol), 'positions').catch((error) => {
      console.warn('sync positions active stocks failed', error)
    })
  }, [positions])

  const activate = useCallback((symbol: string) => {
    if (!symbol) return
    const row = positions.find(item => item.symbol === symbol)
    registerActiveStock(symbol, row?.name || '')
    setActivePositionSymbol(symbol)
    setActiveSymbol(symbol)
    setChartInfo(null)
    setSearchParams({ symbol })
  }, [positions, registerActiveStock, setSearchParams])

  const addStock = useCallback((symbol: string, name: string) => {
    addPositionStock(symbol, name)
    registerActiveStock(symbol, name)
    setSearchParams({ symbol })
  }, [registerActiveStock, setSearchParams])

  const removeStock = useCallback((symbol: string) => {
    const next = removePositionStock(symbol)
    const nextActive = getActivePositionSymbol() || next[0]?.symbol || ''
    if (nextActive) setSearchParams({ symbol: nextActive })
    else setSearchParams({})
  }, [setSearchParams])

  const latestDate = chartInfo?.rows?.at(-1)?.date ?? null
  const prevClose = chartInfo?.rawRows?.at(-2)?.close
  const moneyFlowEnabled = intradayIndicators.includes('moneyflow')
  const transactionQ = useQuery({
    queryKey: QK.transactionIntraday(activeSymbol),
    queryFn: () => api.transactionIntraday(activeSymbol),
    enabled: !!activeSymbol && moneyFlowEnabled,
    staleTime: 0,
    refetchInterval: moneyFlowEnabled ? 5_000 : false,
  })
  const sr004Q = useQuery({
    queryKey: QK.sr004RealtimeExit(activeSymbol, latestDate ?? undefined),
    queryFn: () => api.sr004RealtimeExit(activeSymbol, latestDate ?? undefined),
    enabled: !!activeSymbol && !!latestDate,
    staleTime: 0,
    retry: false,
    refetchInterval: 60_000,
  })
  const sr004SellPrice = sr004Q.data?.sell_price
  const sr004SellPriceLine = useMemo(() => {
    if (typeof sr004SellPrice !== 'number' || !Number.isFinite(sr004SellPrice) || sr004SellPrice <= 0) return undefined
    if (!String(sr004Q.data?.status || '').startsWith('sell_triggered')) return undefined
    const pending = sr004Q.data?.status === 'sell_triggered_fill_pending'
    return {
      price: sr004SellPrice,
      label: `${pending ? 'SR004待成交' : 'SR004卖出'} ${sr004SellPrice.toFixed(2)}`,
    }
  }, [sr004Q.data?.status, sr004SellPrice])
  const toggleIntradayIndicator = useCallback((key: IntradayIndicator) => {
    setIntradayIndicators(prev => {
      const next = prev.includes(key) ? prev.filter(item => item !== key) : [...prev, key]
      saveIntradayIndicators(next)
      return next
    })
  }, [])

  return (
    <div className="flex flex-col h-full">
      <PageHeader
        title="持仓"
        titleExtra={
          <span className="inline-flex items-baseline gap-1 rounded-md bg-elevated/70 px-2 py-0.5 text-[11px]">
            <span className="font-mono font-semibold text-secondary">{positions.length}</span>
            <span className="text-muted/60">只</span>
          </span>
        }
        subtitle="本地持仓观察列表 · 日 K 与分时图"
        right={
          <div className="w-80">
            <PositionSearch
              existingSymbols={positions.map((row) => row.symbol)}
              onAdd={addStock}
              onSelect={activate}
            />
          </div>
        }
      />

      <div className="flex-1 min-h-0 overflow-y-auto px-6 py-5">
        {positions.length === 0 ? (
          <EmptyState
            icon={BriefcaseBusiness}
            title="暂无持仓"
            hint="在右上角搜索股票并加入持仓列表。"
          />
        ) : !activeSymbol ? (
          <EmptyState icon={BriefcaseBusiness} title="选择一只持仓" hint="从左侧持仓列表选择股票查看图表。" />
        ) : (
          <div className="space-y-4">
            <div className="flex items-center justify-between gap-3 rounded-card border border-border/60 bg-surface/40 px-4 py-3">
              <div className="min-w-0">
                <div className="flex items-baseline gap-2">
                  <span className="font-mono text-lg font-semibold text-foreground">{activeSymbol}</span>
                  <span className="text-sm text-secondary">{activePosition?.name || chartInfo?.name || '—'}</span>
                </div>
                <div className="mt-1 text-[10px] text-muted">默认打开上次激活的持仓股票</div>
              </div>
              <button
                onClick={() => removeStock(activeSymbol)}
                className="inline-flex items-center gap-1.5 rounded-btn border border-danger/25 bg-danger/10 px-3 py-1.5 text-xs text-danger hover:bg-danger/20 transition-colors"
              >
                <Trash2 className="h-3.5 w-3.5" />
                移除
              </button>
            </div>

            <section className="rounded-card border border-border/60 bg-surface/40 overflow-hidden">
              <div className="border-b border-border/40 px-4 py-3 text-sm font-medium text-foreground">分时线</div>
              <div className="p-3">
                <div className="flex items-center gap-1.5 px-1 pb-0.5">
                  {INTRADAY_INDICATOR_KEYS.map(key => {
                    const active = intradayIndicators.includes(key)
                    return (
                      <button
                        key={key}
                        onClick={() => toggleIntradayIndicator(key)}
                        className={`rounded px-2 py-0.5 text-[10px] font-mono transition-colors ${
                          active
                            ? 'bg-accent/20 text-accent'
                            : 'bg-elevated text-muted hover:text-secondary'
                        }`}
                      >
                        {INTRADAY_INDICATOR_LABELS[key]}
                      </button>
                    )
                  })}
                  {(transactionQ.isFetching || sr004Q.isFetching) && <Loader2 className="h-3.5 w-3.5 animate-spin text-muted" />}
                </div>
                <StockIntradayChart
                  symbol={activeSymbol}
                  date={latestDate}
                  prevClose={prevClose}
                  height={720}
                  indicators={intradayIndicators}
                  moneyFlowRows={transactionQ.data?.rows}
                  refetchInterval={60_000}
                  sellPriceLine={sr004SellPriceLine}
                />
              </div>
            </section>

            <section className="rounded-card border border-border/60 bg-surface/40 overflow-hidden">
              <div className="border-b border-border/40 px-4 py-3 text-sm font-medium text-foreground">日 K 线</div>
              <div className="p-3">
                <StockDailyKChart
                  symbol={activeSymbol}
                  height={430}
                  visibleBars={90}
                  onDataChange={setChartInfo}
                />
              </div>
            </section>
          </div>
        )}
      </div>
    </div>
  )
}

function PositionSearch({
  existingSymbols,
  onAdd,
  onSelect,
}: {
  existingSymbols: string[]
  onAdd: (symbol: string, name: string) => void
  onSelect: (symbol: string) => void
}) {
  const [query, setQuery] = useState('')
  const [open, setOpen] = useState(false)

  const search = useQuery({
    queryKey: QK.instrumentSearch(query, 'stock,etf'),
    queryFn: () => api.instrumentSearch(query, 20, 'stock,etf'),
    enabled: query.trim().length > 0,
    staleTime: 30_000,
  })
  const results = search.data?.results ?? []

  return (
    <div className="relative">
      <div className="relative">
        <Search className="absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
        <input
          value={query}
          onChange={(event) => { setQuery(event.target.value); setOpen(true) }}
          onFocus={() => { if (query.trim()) setOpen(true) }}
          onBlur={() => window.setTimeout(() => setOpen(false), 120)}
          placeholder="搜索股票加入持仓"
          className="h-9 w-full rounded-btn border border-border bg-elevated pl-9 pr-9 text-sm text-foreground placeholder:text-muted focus:border-accent/50 focus:outline-none"
        />
        {search.isFetching && <Loader2 className="absolute right-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 animate-spin text-muted" />}
      </div>

      {open && query.trim() && (
        <div className="absolute right-0 top-full z-50 mt-1 max-h-[340px] w-full overflow-y-auto rounded-card border border-border bg-base shadow-xl">
          {search.isLoading ? (
            <div className="flex items-center justify-center gap-2 px-4 py-6 text-xs text-muted">
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              搜索中…
            </div>
          ) : results.length === 0 ? (
            <div className="px-4 py-6 text-center text-xs text-muted">未找到匹配股票</div>
          ) : (
            results.map((row) => {
              const exists = existingSymbols.includes(row.symbol)
              return (
                <div key={row.symbol} className="flex items-center gap-2 px-3 py-2 text-xs hover:bg-elevated">
                  <button
                    type="button"
                    onClick={() => {
                      if (exists) onSelect(row.symbol)
                      else onAdd(row.symbol, row.name)
                      setQuery('')
                      setOpen(false)
                    }}
                    className="flex min-w-0 flex-1 items-center gap-2 text-left"
                  >
                    <span className="w-[82px] shrink-0 font-mono text-foreground">{row.symbol}</span>
                    <span className="truncate text-secondary">{row.name}</span>
                  </button>
                  <button
                    type="button"
                    onMouseDown={(event) => event.preventDefault()}
                    onClick={() => {
                      if (exists) onSelect(row.symbol)
                      else onAdd(row.symbol, row.name)
                      setQuery('')
                      setOpen(false)
                    }}
                    className={`rounded p-1 transition-colors ${exists ? 'bg-accent/10 text-accent' : 'text-muted hover:bg-accent/10 hover:text-accent'}`}
                    title={exists ? '查看持仓' : '加入持仓'}
                  >
                    {exists ? <Check className="h-3.5 w-3.5" /> : <Plus className="h-3.5 w-3.5" />}
                  </button>
                </div>
              )
            })
          )}
        </div>
      )}
    </div>
  )
}
