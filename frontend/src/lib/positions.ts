export interface PositionStock {
  symbol: string
  name: string
  added_at: string
}

const POSITIONS_KEY = 'positions-list'
const ACTIVE_KEY = 'positions-active-symbol'
export const POSITIONS_CHANGED_EVENT = 'positions-changed'

function normalizeSymbol(symbol: string): string {
  return String(symbol || '').trim()
}

function emitPositionsChanged() {
  window.dispatchEvent(new Event(POSITIONS_CHANGED_EVENT))
}

export function loadPositions(): PositionStock[] {
  try {
    const raw = localStorage.getItem(POSITIONS_KEY)
    const rows = raw ? JSON.parse(raw) : []
    if (!Array.isArray(rows)) return []
    return rows
      .map((row) => ({
        symbol: normalizeSymbol(row?.symbol),
        name: String(row?.name || ''),
        added_at: String(row?.added_at || ''),
      }))
      .filter((row) => row.symbol)
  } catch {
    return []
  }
}

export function savePositions(rows: PositionStock[]) {
  try {
    localStorage.setItem(POSITIONS_KEY, JSON.stringify(rows))
  } catch {
    // ignore storage failures
  }
  emitPositionsChanged()
}

export function getActivePositionSymbol(): string {
  try {
    return normalizeSymbol(localStorage.getItem(ACTIVE_KEY) || '')
  } catch {
    return ''
  }
}

export function setActivePositionSymbol(symbol: string) {
  const normalized = normalizeSymbol(symbol)
  try {
    if (normalized) localStorage.setItem(ACTIVE_KEY, normalized)
    else localStorage.removeItem(ACTIVE_KEY)
  } catch {
    // ignore storage failures
  }
  emitPositionsChanged()
}

export function addPositionStock(symbol: string, name = ''): PositionStock[] {
  const normalized = normalizeSymbol(symbol)
  if (!normalized) return loadPositions()
  const rows = loadPositions()
  const existing = rows.find((row) => row.symbol === normalized)
  const next = existing
    ? rows.map((row) => row.symbol === normalized ? { ...row, name: name || row.name } : row)
    : [...rows, { symbol: normalized, name, added_at: new Date().toISOString() }]
  savePositions(next)
  setActivePositionSymbol(normalized)
  return next
}

export function removePositionStock(symbol: string): PositionStock[] {
  const normalized = normalizeSymbol(symbol)
  const rows = loadPositions()
  const next = rows.filter((row) => row.symbol !== normalized)
  savePositions(next)
  if (getActivePositionSymbol() === normalized) {
    setActivePositionSymbol(next[0]?.symbol || '')
  }
  return next
}

export function subscribePositionsChanged(callback: () => void): () => void {
  const onStorage = (event: StorageEvent) => {
    if (event.key === POSITIONS_KEY || event.key === ACTIVE_KEY) callback()
  }
  window.addEventListener(POSITIONS_CHANGED_EVENT, callback)
  window.addEventListener('storage', onStorage)
  return () => {
    window.removeEventListener(POSITIONS_CHANGED_EVENT, callback)
    window.removeEventListener('storage', onStorage)
  }
}
