import { useEffect, useMemo, useRef } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import * as echarts from 'echarts'
import type {
  DefaultLabelFormatterCallbackParams,
  ECharts,
  EChartsOption,
  TooltipComponentFormatterCallbackParams,
} from 'echarts'
import { AlertCircle, CheckCircle2, Loader2, RefreshCw, TrendingUp } from 'lucide-react'
import { api, type HotConceptsResponse } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState } from '@/components/EmptyState'

function useECharts(option: EChartsOption | null, deps: unknown[] = []) {
  const chartRef = useRef<HTMLDivElement>(null)
  const instanceRef = useRef<ECharts | null>(null)

  useEffect(() => {
    if (!chartRef.current) return
    instanceRef.current = echarts.init(chartRef.current, undefined, { renderer: 'canvas' })
    const handleResize = () => instanceRef.current?.resize()
    window.addEventListener('resize', handleResize)
    return () => {
      window.removeEventListener('resize', handleResize)
      instanceRef.current?.dispose()
      instanceRef.current = null
    }
  }, [])

  useEffect(() => {
    if (!instanceRef.current || !option) return
    instanceRef.current.setOption(option, { notMerge: true })
  }, [option, ...deps])

  return chartRef
}

function escapeHtml(value: unknown) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;')
}

function buildTreemapOption(data: HotConceptsResponse): EChartsOption {
  const darkColors = [
    '#183a3f', '#342936', '#26384a', '#393522', '#3a272d',
    '#243a31', '#30303e', '#3a3026', '#20373b', '#343940',
  ]
  const treemapData = data.treemap_data.map((concept, index) => ({
    name: concept.name,
    value: concept.value,
    stocks: concept.children.map(stock => ({ name: stock.name, code: stock.code || '' })),
    itemStyle: { color: darkColors[index % darkColors.length] },
  }))
  const totalStocks = data.unique_stocks

  return {
    tooltip: {
      backgroundColor: 'rgba(10, 14, 20, 0.96)',
      borderColor: '#374151',
      borderWidth: 1,
      textStyle: { color: '#d1d5db', fontSize: 12 },
      formatter: (rawParams: TooltipComponentFormatterCallbackParams) => {
        const params = Array.isArray(rawParams) ? rawParams[0] : rawParams
        const item = params.data as { stocks?: { name: string; code: string }[] }
        const stocks = item.stocks
          ?.map(stock => `${escapeHtml(stock.name)} <span style="color:#6b7280">${escapeHtml(stock.code)}</span>`)
          .join('<br/>') || '暂无'
        return `<b>${escapeHtml(params.name)}</b><br/>涨停数: ${escapeHtml(params.value)}<br/>${stocks}`
      },
    },
    series: [
      {
        type: 'treemap',
        roam: false,
        nodeClick: false,
        breadcrumb: { show: false },
        label: {
          show: true,
          position: 'insideTopLeft',
          align: 'left',
          verticalAlign: 'top',
          padding: [10, 9],
          formatter: (params: DefaultLabelFormatterCallbackParams) => {
            const item = params.data as { stocks?: { name: string; code: string }[] }
            const rows = (item.stocks ?? [])
              .map(stock => `{stockName|${stock.name}}{stockCode|${stock.code}}`)
              .join('\n')
            return `{title|${params.name}}  {count|${params.value}只}\n${rows}`
          },
          rich: {
            title: { fontSize: 14, fontWeight: 'bold', lineHeight: 24, color: '#e5e7eb' },
            count: { fontSize: 10, color: '#9ca3af' },
            stockName: { width: 76, fontSize: 11, lineHeight: 18, color: '#cbd5e1' },
            stockCode: {
              width: 48,
              align: 'right',
              fontSize: 9,
              fontFamily: 'JetBrains Mono, monospace',
              color: '#6b7280',
            },
          },
        },
        upperLabel: {
          show: false,
        },
        itemStyle: {
          borderColor: '#0b0f14',
          borderWidth: 2,
          gapWidth: 3,
          borderRadius: 3,
        },
        emphasis: {
          itemStyle: {
            borderColor: '#64748b',
            borderWidth: 2,
          },
        },
        data: treemapData,
      },
    ],
    title: {
      text: `热门概念 Top 10 · 涨停分布（共 ${totalStocks} 只涨停）`,
      left: 'center',
      top: 0,
      textStyle: { fontSize: 16, fontWeight: 'bold', color: '#d1d5db' },
    },
  }
}

function HotConceptsChart({ data }: { data: HotConceptsResponse }) {
  const option = useMemo(() => buildTreemapOption(data), [data])
  const chartRef = useECharts(option)

  return <div ref={chartRef} className="h-full min-h-[600px] w-full" />
}

export function HotConcepts() {
  const qc = useQueryClient()
  const statusQuery = useQuery({
    queryKey: QK.hotConceptsJob,
    queryFn: api.hotConceptsStatus,
    refetchInterval: query => query.state.data?.status === 'running' ? 1_000 : false,
    staleTime: 0,
  })
  const startJob = useMutation({
    mutationFn: (refresh: boolean) => api.hotConceptsStart(refresh),
    onSuccess: job => qc.setQueryData(QK.hotConceptsJob, job),
  })

  useEffect(() => {
    startJob.mutate(false)
    // The backend coalesces duplicate starts caused by React StrictMode.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const job = statusQuery.data
  const data = job?.data

  const isWorking = !job || job.status === 'idle' || job.status === 'running' || startJob.isPending
  if (isWorking) {
    return (
      <div className="flex h-full flex-col">
        <PageHeader title="热门概念" />
        <div className="flex flex-1 items-center justify-center overflow-auto px-5 py-8">
          <div className="w-full max-w-xl">
            <div className="flex items-start gap-3">
              <Loader2 className="mt-0.5 h-5 w-5 shrink-0 animate-spin text-accent" />
              <div className="min-w-0 flex-1">
                <div className="text-sm font-medium text-foreground">
                  {job?.message || '正在启动热门概念计算'}
                </div>
                <div className="mt-1 text-xs text-muted">
                  {job?.started_at ? `开始于 ${job.started_at.slice(11, 19)}` : '正在创建后台任务'}
                </div>
              </div>
              <span className="font-mono text-sm text-secondary">{job?.progress ?? 0}%</span>
            </div>

            <div className="mt-4 h-1.5 overflow-hidden rounded-full bg-elevated">
              <div
                className="h-full rounded-full bg-accent transition-[width] duration-300"
                style={{ width: `${job?.progress ?? 2}%` }}
              />
            </div>

            {job?.log && job.log.length > 0 && (
              <div className="mt-6 space-y-2 border-t border-border/70 pt-4">
                {job.log.map((entry, index) => {
                  const current = index === job.log.length - 1
                  return (
                    <div key={`${entry.at}-${entry.stage}`} className="flex items-start gap-2 text-xs">
                      {current ? (
                        <Loader2 className="mt-px h-3.5 w-3.5 shrink-0 animate-spin text-accent" />
                      ) : (
                        <CheckCircle2 className="mt-px h-3.5 w-3.5 shrink-0 text-bull" />
                      )}
                      <span className={current ? 'text-foreground' : 'text-muted'}>{entry.message}</span>
                      <span className="ml-auto shrink-0 font-mono text-[10px] text-muted/70">
                        {entry.at.slice(11, 19)}
                      </span>
                    </div>
                  )
                })}
              </div>
            )}
          </div>
        </div>
      </div>
    )
  }

  if (job.status === 'failed' || statusQuery.error || startJob.error || !data) {
    const error = job?.error || statusQuery.error?.message || startJob.error?.message
    return (
      <div className="flex flex-col h-full">
        <PageHeader title="热门概念" />
        <EmptyState
          icon={AlertCircle}
          title="热门概念计算失败"
          hint={error || '后台任务未返回数据'}
        />
      </div>
    )
  }

  if (data.treemap_data.length === 0) {
    return (
      <div className="flex flex-col h-full">
        <PageHeader title="热门概念" />
        <EmptyState
          icon={TrendingUp}
          title="暂无涨停股票"
          hint={data.trade_date ? `交易日期: ${data.trade_date}` : '当前非交易时段或无涨停数据'}
        />
      </div>
    )
  }

  return (
    <div className="flex flex-col h-full">
      <PageHeader
        title="热门概念"
        titleExtra={
          <span className="text-xs text-muted">
            共 {data.unique_stocks} 只涨停 · {data.concept_count} 个概念
          </span>
        }
        right={
          <button
            onClick={() => startJob.mutate(true)}
            disabled={startJob.isPending}
            className="p-1.5 hover:bg-surface text-muted disabled:opacity-50 cursor-pointer"
            title="刷新数据"
          >
            <RefreshCw className={`h-4 w-4 ${startJob.isPending ? 'animate-spin' : ''}`} />
          </button>
        }
      />
      {data.warning && (
        <div className="border-b border-warning/20 bg-warning/5 px-5 py-2 text-xs text-warning">
          {data.warning}
        </div>
      )}
      <div className="flex-1 p-3">
        <HotConceptsChart data={data} />
      </div>
    </div>
  )
}
