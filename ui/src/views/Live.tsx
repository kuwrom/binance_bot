// Live reads rendered for a person, not as raw JSON: the risk budget for a new short (with the size it implies at
// each stop the read offers), the book plan ("what to work next") and a fresh ladder for an open short. Each one
// calls the exchange, so each runs on a button, never on a refresh.
import { useState } from 'react'
import { run } from '../api'
import { Facts } from '../components/figures'
import { Raw } from '../components/Result'
import { useDesk } from '../ctx'
import { coin, isNum, pct, price, usd } from '../format'
import type { Dict, LadderView, StopOption } from '../types'

export interface Budget {
  risk_usd: number; risk_pct: number; base_pct: number; drawdown_pct: number; conviction: string; cell: string | null
  multipliers: Dict<number>; slot_cap_usd: number; heat_pct: number | null; pending_heat_pct: number | null
  book: { n: number; rho: number | null; n_eff: number | null }; cluster: string | null; halted_until: string | null
  blocked: boolean; reasons: string[]
}

const MULT_NAMES: Dict<string> = {
  m_dd: 'drawdown', m_vol: 'BTC volatility', m_regime: 'regime', m_corr: 'correlation', tilt: 'conviction',
  m_gov: 'governor', m_ladder: 'setup ladder', m_div: 'diversity',
}

// stops: the read's stop options (tight / wide / sweep proof), when the coin was read: size = risk / distance.
export function SizeCard({ b, stops }: { b: Budget; stops?: [string, StopOption][] }) {
  const { limits } = useDesk()
  const maxNotional = limits?.max_position_notional
  const off = Object.entries(b.multipliers).filter(([, v]) => isNum(v) && v !== 1)
  return (
    <div className="card">
      <div className="result-head"><strong>Risk budget for a new short</strong>
        {b.blocked ? <span className="pill bad">blocked</span> : <span className="pill ok">conviction {b.conviction}</span>}</div>
      <div className="hero-value small">{usd(b.risk_usd)}<span className="muted"> at risk · {pct(b.risk_pct)} of equity</span></div>
      <p className="muted small-print">Base {pct(b.base_pct, false, 1)} per trade, then
        {off.length ? ' ' + off.map(([k, v]) => `${MULT_NAMES[k] ?? k} ×${v}`).join(' · ') : ' no adjustments'}.
        Capped at {usd(b.slot_cap_usd, false, 0)} for one slot.</p>
      {stops && stops.length > 0 && !b.blocked && (
        <div className="scroll"><table className="size-table">
          <thead><tr><th>At the stop</th><th className="num">Away</th><th className="num">Size</th><th className="num">Risk taken</th><th className="num">Max lev</th></tr></thead>
          <tbody>{stops.filter(([, s]) => s.allowed && s.distance_pct > 0).map(([name, s]) => {
            const want = b.risk_usd / (s.distance_pct / 100)
            const capped = isNum(maxNotional) && want > maxNotional
            const notional = capped ? maxNotional : want
            return (
              <tr key={name}><td>{name.replace(/_/g, ' ')} {price(s.price)}</td><td className="num">{pct(s.distance_pct)}</td>
                <td className="num"><strong>{usd(notional, false, 0)}</strong>{capped && <span className="muted"> cap</span>}</td>
                <td className="num">{usd(notional * s.distance_pct / 100, false, 0)}</td>
                <td className="num muted">{s.max_leverage}x</td></tr>
            )
          })}</tbody>
        </table></div>
      )}
      {stops === undefined && <p className="muted small-print">Read the coin to see the size this budget buys at each stop.</p>}
      {stops && isNum(maxNotional) && <p className="muted small-print">"cap": the budget would buy more than the {usd(maxNotional, false, 0)} max
        position, so the size is the cap and less than the budget is at risk.</p>}
      {stops && !isNum(maxNotional) && <p className="muted small-print">Sizes before the max-position cap: open the Overview once to load the limits.</p>}
      <Facts items={[['Open heat', pct(b.heat_pct)], ['Resting heat', pct(b.pending_heat_pct)],
        ['Book', `${b.book.n} coins${isNum(b.book.n_eff) ? ` · ${b.book.n_eff} independent` : ''}`],
        ['Drawdown', pct(b.drawdown_pct)]]} />
      {b.reasons.length > 0 && <ul className="kills">{b.reasons.map((r) => <li key={r}>{r}</li>)}</ul>}
      <p className="muted small-print">open_short re-checks every limit at entry (total notional, liquidation buffer, vetoes).</p>
      <Raw v={b} />
    </div>
  )
}

interface PlanAdd { symbol: string; slot: number; playbook: string | null; setup: string | null; sector: string | null
  score: number | null; evidence: string; size_mult: number; why: string[] }
export interface BookPlan { slots_used: number; max_positions: number; free_slots: number; add: PlanAdd[];
  skip: { symbol: string; why: string }[] }

export function PlanCard({ plan }: { plan: BookPlan }) {
  const { openCoin } = useDesk()
  return (
    <div className="card">
      <div className="result-head"><strong>What to work next</strong>
        <span className="pill">{plan.free_slots} of {plan.max_positions} slots free</span></div>
      <div className="list">
        {plan.add.length ? plan.add.map((a) => (
          <button type="button" className="row cand" key={a.symbol} onClick={() => openCoin(a.symbol)}>
            <span className="score">{a.slot}</span>
            <span className="row-main">
              <span className="row-title"><strong>{coin(a.symbol)}</strong>
                {a.playbook && <span className="pill ok">{a.playbook}</span>}
                <span className={'pill' + (a.evidence === 'live' ? ' ok' : '')}>{a.evidence}</span>
                <span className="muted">size ×{a.size_mult}{isNum(a.score) ? ` · score ${a.score}` : ''}</span></span>
              <span className="row-sub clamp1">{a.why.join(' · ')}</span>
            </span>
          </button>
        )) : <div className="empty">Nothing to add: every candidate was skipped, or there is no free slot.</div>}
      </div>
      {plan.skip.length > 0 && (
        <details className="fold"><summary>{plan.skip.length} skipped, with the reason</summary>
          <ul className="kills">{plan.skip.map((s) => <li key={s.symbol}><strong>{coin(s.symbol)}</strong>: {s.why}</li>)}</ul>
        </details>
      )}
      <p className="muted small-print">The slot number is the order to work them in. Placing an entry still goes through
        propose_entries and every budget check. Each run is logged as a decision.</p>
      <Raw v={plan} />
    </div>
  )
}

// A fresh ladder for an open short (position_ladder: read-only, logs nothing), merged over the guardian's last pass.
export function useLiveLadder(symbol: string, stored?: LadderView) {
  const [live, setLive] = useState<LadderView | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const refresh = () => {
    setBusy(true)
    setError('')
    run<Dict>('position_ladder', { symbol }).then(({ result: r }) => {
      if (r && typeof r.error === 'string') setError(r.error)
      else setLive({ ...(stored ?? { entry: 0, init_r: 0, live: false, conviction: null, trade_id: 0 }), ...(r as object),
        ts: String(r.ts ?? new Date().toISOString()) } as LadderView)
    }, (e: Error) => setError(e.message)).finally(() => setBusy(false))
  }
  return { lad: live ?? stored, fresh: !!live, busy, error, refresh }
}
