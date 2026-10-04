// The desk's figures: a meter (one ratio against a limit), a price ladder (where the mark sits between targets and
// stop), a sparkline with a crosshair readout, and the small shared pieces (facts, notices). Positions are set
// through React's style prop, which writes the CSSOM: allowed by the page's CSP.
import { useEffect, useRef, useState, type KeyboardEvent, type PointerEvent, type ReactNode } from 'react'
import { isNum, price } from '../format'

const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v))

// used / cap as a track. The fill turns warning at 60% and critical at 85%; `tick` marks a threshold on the track.
export function Meter({ label, used, cap, text, tick, hint }: {
  label: string; used: number; cap: number; text: string; tick?: number; hint?: string
}) {
  const frac = cap > 0 && isNum(used) ? clamp(used / cap, 0, 1) : 0
  return (
    <div className={'meter' + (frac >= 0.85 ? ' crit' : frac >= 0.6 ? ' warn' : '')}>
      <div className="meter-head"><span className="meter-label">{label}</span><span className="meter-text">{text}</span></div>
      <div className="meter-track" role="meter" aria-label={`${label}: ${text}`} aria-valuemin={0} aria-valuemax={100}
        aria-valuenow={Math.round(frac * 100)}>
        <div className="meter-fill" style={{ width: `${frac > 0 ? Math.max(frac * 100, 1.5) : 0}%` }} />
        {isNum(tick) && cap > 0 && <div className="meter-tick" style={{ left: `${clamp(tick / cap, 0, 1) * 100}%` }} />}
      </div>
      {hint && <div className="meter-hint">{hint}</div>}
    </div>
  )
}

export interface LadderProps { entry: number; mark: number; stop: number | null; targets: number[] }

// A short's price ladder, low prices left: targets, entry, the live mark and the exchange stop. The band between
// entry and mark is the open P&L (left of entry = profit for a short). Stop and entry are labelled first; a label
// that would overlap one already placed is hidden once the ladder has a width (the facts under it carry the numbers).
export function Ladder({ entry, mark, stop, targets }: LadderProps) {
  const box = useRef<HTMLDivElement>(null)
  const ts = targets.filter(isNum).sort((a, b) => a - b)
  const all = [...ts, entry, mark, ...(stop === null ? [] : [stop])].filter(isNum)
  useEffect(() => {
    const el = box.current
    if (!el) return
    const fit = () => {
      const kept: DOMRect[] = []
      el.querySelectorAll<HTMLElement>('.ladder-lab:not(.mark)').forEach((lab) => {
        lab.hidden = false
        const r = lab.getBoundingClientRect()
        lab.hidden = kept.some((q) => r.left < q.right + 4 && r.right > q.left - 4)
        if (!lab.hidden) kept.push(r)
      })
    }
    const ro = new ResizeObserver(fit)
    ro.observe(el)
    return () => ro.disconnect()
  })
  if (all.length < 2) return <div className="ladder" />
  const lo = Math.min(...all), hi = Math.max(...all)
  const at = (x: number) => (hi > lo ? ((x - lo) / (hi - lo)) * 100 : 50)
  const edge = (x: number) => (at(x) < 12 ? { cls: ' at-start', style: { left: 0 } }
    : at(x) > 88 ? { cls: ' at-end', style: { right: 0 } } : { cls: '', style: { left: `${at(x)}%` } })
  const levels = [
    ...(stop === null ? [] : [{ x: stop, name: 'stop', cls: 'stop' }]),
    { x: entry, name: 'entry', cls: 'entry' },
    ...ts.slice().reverse().map((x, i) => ({ x, name: `T${i + 1}`, cls: 'target' })),
  ]
  const m = edge(mark)
  return (
    <div className="ladder" ref={box} role="img" aria-label={`mark ${price(mark)}, entry ${price(entry)}` +
      (stop !== null ? `, stop ${price(stop)}` : ', no stop') + (ts.length ? `, targets ${ts.map(price).join(' and ')}` : '')}>
      <div className="ladder-track">
        <div className={'ladder-band ' + (mark <= entry ? 'pos' : 'neg')}
          style={{ left: `${Math.min(at(entry), at(mark))}%`, width: `${Math.abs(at(entry) - at(mark))}%` }} />
      </div>
      {levels.map((lv) => {
        const e = edge(lv.x)
        return [
          <div key={lv.name + 't'} className={'ladder-tick ' + lv.cls} style={{ left: `${at(lv.x)}%` }} />,
          <div key={lv.name} className={`ladder-lab ${lv.cls}${e.cls}`} style={e.style}>
            <span className="ladder-name">{lv.name}</span><span>{price(lv.x)}</span></div>,
        ]
      })}
      <div className="ladder-dot" style={{ left: `${at(mark)}%` }} />
      <div className={`ladder-lab mark${m.cls}`} style={m.style}><span className="ladder-name">mark</span><span>{price(mark)}</span></div>
    </div>
  )
}

export interface SparkPoint { y: number; value: string; label: string }

// points in time order. One series, so no legend: the card's title names it. Hover, touch or arrow keys read a point.
export function Spark({ points }: { points: SparkPoint[] }) {
  const [hover, setHover] = useState<number | null>(null)
  if (points.length < 2) return <div className="spark"><p className="muted">Not enough history for a curve yet.</p></div>
  const ys = points.map((p) => p.y)
  const lo = Math.min(0, ...ys), hi = Math.max(0, ...ys)
  const X = (i: number) => (i / (points.length - 1)) * 100
  const Y = (y: number) => 8 + (1 - (hi > lo ? (y - lo) / (hi - lo) : 0.5)) * 84
  const line = points.map((p, i) => `${i ? 'L' : 'M'}${X(i).toFixed(2)},${Y(p.y).toFixed(2)}`).join(' ')
  const i = hover ?? points.length - 1
  const move = (e: PointerEvent<HTMLDivElement>) => {
    const r = e.currentTarget.getBoundingClientRect()
    setHover(clamp(Math.round(((e.clientX - r.left) / r.width) * (points.length - 1)), 0, points.length - 1))
  }
  const key = (e: KeyboardEvent) => {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return
    e.preventDefault()
    setHover(clamp(i + (e.key === 'ArrowLeft' ? -1 : 1), 0, points.length - 1))
  }
  return (
    <div className={'spark' + (hover !== null ? ' hover' : '')} tabIndex={0} role="img" onPointerMove={move}
      onPointerDown={move} onPointerLeave={() => setHover(null)} onBlur={() => setHover(null)} onKeyDown={key}
      aria-label={`${points.length} points, ending at ${points[points.length - 1].value}. Arrow keys step through them.`}>
      <svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
        <path className="spark-area" d={`${line} L100,${Y(0).toFixed(2)} L0,${Y(0).toFixed(2)} Z`} />
        <line className="spark-zero" x1={0} x2={100} y1={Y(0)} y2={Y(0)} />
        <path className="spark-line" d={line} />
      </svg>
      <div className="spark-cross" style={{ left: `${X(i)}%` }} />
      <div className="spark-dot" style={{ left: `${X(i)}%`, top: `${Y(points[i].y)}%` }} />
      <div className="spark-tip" role="status" style={{ left: `${X(i)}%`, transform: `translate(${-X(i)}%, -100%)` }}>
        <strong>{points[i].value}</strong><span>{points[i].label}</span>
      </div>
    </div>
  )
}

export type Fact = [label: string, value: ReactNode, tone?: string]

export function Facts({ items, className = '' }: { items: Fact[]; className?: string }) {
  const shown = items.filter(([, v]) => v !== null && v !== undefined && v !== '')
  if (!shown.length) return null
  return (
    <dl className={'facts ' + className}>
      {shown.map(([k, v, tone]) => <div className="fact" key={k}><dt>{k}</dt><dd className={tone}>{v}</dd></div>)}
    </dl>
  )
}

export function Notice({ level, title, lines }: { level: 'crit' | 'warn'; title: string; lines: (string | null | false | undefined)[] }) {
  return (
    <div className={'notice ' + level} role={level === 'crit' ? 'alert' : 'status'}>
      <span className="notice-icon">{level === 'crit' ? '!' : 'i'}</span>
      <div className="notice-body"><strong>{title}</strong>{lines.filter(Boolean).map((l, i) => <p key={i}>{l}</p>)}</div>
    </div>
  )
}
