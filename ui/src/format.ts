// How the desk writes money, prices, odds and times.

const MINUS = '−'
export const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
const signOf = (v: number, sign: boolean) => (v < 0 ? MINUS : sign && v > 0 ? '+' : '')

// $5,129.24 · +$24.58 · −$5.89 (sign: show "+" too); never "+$0.00"
export function usd(v: unknown, sign = false, digits = 2): string {
  if (!isNum(v)) return '—'
  const n = Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits })
  return `${Number(Math.abs(v).toFixed(digits)) === 0 ? '' : signOf(v, sign)}$${n}`
}

// 12.9K · 5M: caps and volumes
export function compact(v: unknown): string {
  if (!isNum(v)) return '—'
  return Math.abs(v) >= 10000 ? v.toLocaleString('en-US', { notation: 'compact', maximumFractionDigits: 1 })
    : v.toLocaleString('en-US', { maximumFractionDigits: 2 })
}

// A coin price at 5 significant digits: 0.03295 · 2.203 · 64,210.5
export function price(v: unknown): string {
  if (!isNum(v)) return '—'
  return v >= 1000 ? v.toLocaleString('en-US', { maximumFractionDigits: 1 }) : String(Number(v.toPrecision(5)))
}

export function pct(v: unknown, sign = false, digits = 2): string {
  if (!isNum(v)) return '—'
  return `${signOf(v, sign)}${Math.abs(v).toFixed(digits)}%`
}

// A probability 0..1 as "58%"
export const odds = (p: unknown): string => (isNum(p) ? `${Math.round(p * 100)}%` : '—')
// A multiple of the initial risk: "+0.8R"
export const rMult = (v: unknown, digits = 2): string => (isNum(v) ? `${signOf(v, true)}${Math.abs(v).toFixed(digits)}R` : '—')

// pos / neg / '' for signed money; a rounding-size value is flat
export const tone = (v: unknown): string => (!isNum(v) || Math.abs(v) < 0.005 ? '' : v > 0 ? 'pos' : 'neg')
export const coin = (symbol: unknown): string => String(symbol ?? '').split('/')[0]

// "3h 40m", "2d 4h", "45s": a span in seconds
export function span(seconds: unknown): string {
  if (!isNum(seconds)) return '—'
  const s = Math.max(0, Math.round(seconds))
  if (s < 90) return `${s}s`
  const m = Math.round(s / 60)
  if (m < 90) return `${m}m`
  const h = Math.floor(m / 60)
  if (h < 48) return `${h}h ${m % 60}m`
  return `${Math.floor(h / 24)}d ${h % 24}h`
}

const date = (ts: string | number) =>
  new Date(typeof ts === 'string' && !/[zZ]|[+-]\d\d:?\d\d$/.test(ts) ? ts + 'Z' : ts)   // the journal writes UTC
export const since = (ts?: string | number | null): string => (ts ? span((Date.now() - date(ts).getTime()) / 1000) : '—')

// "Oct 2, 14:31" in the browser's zone
export function when(ts?: string | number | null): string {
  if (!ts) return '—'
  return date(ts).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false })
}

// max_total_notional -> "Max total notional", risk_per_trade_pct -> "Risk per trade %"
export function label(key: string): string {
  const s = key.replace(/_pct$/, ' %').replace(/_usd$/, ' $').replace(/_/g, ' ')
  return s.charAt(0).toUpperCase() + s.slice(1)
}
