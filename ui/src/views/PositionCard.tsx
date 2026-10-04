// What is on the book: one card per open short (price ladder, the odds of each next level, the state score and
// what the ladder rule would hold, the thesis and plan) and a row per resting entry with its fill and target odds.
import { useState } from 'react'
import { Facts, Ladder } from '../components/figures'
import { Raw } from '../components/Result'
import { useDesk } from '../ctx'
import { coin, isNum, odds, pct, price, rMult, since, span, tone, usd, when } from '../format'
import type { Entry, EntryOdds, LadderView, Position } from '../types'
import { useLiveLadder } from './Live'

const AUTO: [string, string][] = [['targets', 'targets'], ['breakeven', 'breakeven'], ['trail', 'trailing'], ['time_stop', 'time stop']]

// The guardian's last ladder pass for this short: P(level before stop) against the no-edge baseline, EV in R.
function Odds({ lad, fresh, busy, error, refresh }: { lad?: LadderView; fresh: boolean; busy: boolean; error: string; refresh: () => void }) {
  if (!lad || !lad.levels.length) {
    return <div className="odds"><div className="odds-head"><span className="chips-label">No odds logged for this short yet</span>
      <button type="button" className="ghost small" disabled={busy} onClick={refresh}>{busy ? 'computing…' : 'Compute odds now'}</button></div>
      {error && <p className="error small-print">{error}</p>}</div>
  }
  const held = Math.round(lad.x_actual * 100), wants = Math.round(lad.x_star * 100)
  const parts = Object.entries(lad.parts)
  return (
    <div className="odds">
      <div className="odds-head">
        <span className="chips-label">Odds to the next levels</span>
        <span className="muted">model vs no-edge · {fresh ? 'live now' : `guardian, ${since(lad.ts)} ago`}
          <button type="button" className="link refresh" disabled={busy} onClick={refresh}>{busy ? 'computing…' : 'Refresh'}</button></span>
      </div>
      <div className="scroll">
        <table className="odds-table">
          <thead><tr><th>Level</th><th className="num">Reach first</th><th className="num">No edge</th>
            <th className="num">Reward</th><th className="num">EV</th></tr></thead>
          <tbody>
            {lad.levels.map((lv, i) => (
              <tr key={lv.target}>
                <td>T{i + 1} {price(lv.target)}</td>
                <td className="num"><span className="bar" style={{ width: `${Math.round(lv.p_model * 100)}%` }} />
                  <strong>{odds(lv.p_model)}</strong></td>
                <td className="num muted">{odds(lv.p0)}</td>
                <td className="num">{rMult(lv.reward_R)}</td>
                <td className={'num ' + tone(lv.ev_R)}>{rMult(lv.ev_R)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {error && <p className="error small-print">{error}</p>}
      <div className="chips">
        <span className={'chip ' + (lad.score > 0 ? 'on' : lad.score < 0 ? 'bad' : '')}>State score {lad.score > 0 ? '+' : ''}{lad.score}</span>
        {parts.map(([k, v]) => <span className="chip" key={k}>{k.replace(/_/g, ' ')} {v > 0 ? '+' : ''}{v}</span>)}
        <span className={'chip ' + (lad.action === 'hold' ? '' : 'warn')}>
          Rule: {lad.action}{wants !== held ? ` to ${wants}% (holding ${held}%)` : ` at ${held}%`}
          {lad.live ? '' : ' · shadow'}
        </span>
      </div>
    </div>
  )
}

function PlanBody({ p }: { p: Position }) {
  const pl = p.plan ?? {}
  const hit = p.targets_hit ?? 0
  const targets = (pl.targets ?? []).map((t, i) =>
    `${price(t.price)} (${Math.round((t.fraction || 0) * 100)}%)${i < hit ? ' ✓' : ''}`).join(' · ')
  let timeStop: string | null = null
  if (isNum(pl.time_stop_hours)) {
    const left = p.opened_at ? new Date(p.opened_at).getTime() / 1000 + pl.time_stop_hours * 3600 - Date.now() / 1000 : null
    timeStop = `${pl.time_stop_hours} h` + (left === null ? '' : left > 0 ? ` · ${span(left)} left` : ' · due')
  }
  return (
    <div className="plan">
      {p.thesis && <p className="thesis">{p.thesis}</p>}
      <Facts className="wide" items={[['Trigger', pl.trigger], ['Invalidation', pl.invalidation],
        ['Targets', targets || null], ['Time stop', timeStop],
        ['Worst / best so far', isNum(p.mae_pct) || isNum(p.mfe_pct)
          ? `${pct(-Math.abs(p.mae_pct ?? 0), true)} / ${pct(Math.abs(p.mfe_pct ?? 0), true)}` : null]]} />
      {pl.auto && (
        <div className="chips">
          <span className="chips-label">Guardian runs</span>
          {AUTO.map(([k, name]) => <span key={k} className={'chip ' + (pl.auto?.[k] ? 'on' : 'off')}>{pl.auto?.[k] ? '✓' : '✕'} {name}</span>)}
        </div>
      )}
      {(pl.kill_conditions ?? []).length > 0 && (
        <div><div className="chips-label">Exit early if</div>
          <ul className="kills">{pl.kill_conditions?.map((k) => <li key={k}>{k}</li>)}</ul></div>
      )}
      <Facts className="adv" items={[['Leverage', p.leverage ? `${p.leverage}x ${p.margin_mode ?? ''}` : null],
        ['Liquidation', isNum(p.liquidation) ? price(p.liquidation) : null], ['Contracts', p.contracts],
        ['Trade', p.trade_id ? `#${p.trade_id} · ${p.journal_status}` : p.journal_status], ['Opened', when(p.opened_at)]]} />
      <Raw v={p} />
    </div>
  )
}

export function PositionCard({ p, lad: stored, reload }: { p: Position; lad?: LadderView; reload: () => void }) {
  const { openSheet, openCoin } = useDesk()
  const live = useLiveLadder(p.symbol, stored)
  const lad = live.lad
  const [open, setOpen] = useState(false)   // survives refreshes: the card is keyed by symbol
  const name = coin(p.symbol)
  const stop = isNum(p.exchange_stop) ? p.exchange_stop : null
  const rNow = lad && lad.init_r > 0 ? (p.entry - p.mark) / lad.init_r : null
  const atStop = stop !== null ? (p.entry - stop) * p.contracts : null
  return (
    <article className={'card short' + (p.UNPROTECTED ? ' unprotected' : '')}>
      <header className="pos-head">
        <div className="pos-name">
          <h3><button className="link coin-link" type="button" onClick={() => openCoin(p.symbol)}>{name}</button>
            <span className="quote">short</span></h3>
          <div className="pos-meta">
            {p.setup && <span className="pill">{p.setup}</span>}
            {lad?.conviction && <span className="pill">conviction {lad.conviction}</span>}
            <span className="muted">{p.opened_at ? `open ${since(p.opened_at)}` : 'not in journal'}</span>
          </div>
        </div>
        <div className="pos-pnl">
          <div className={'pos-usd ' + tone(p.unrealized_pnl)}>{usd(p.unrealized_pnl, true)}</div>
          <div className={'pos-pct ' + tone(p.pnl_pct)}>{pct(p.pnl_pct, true)}{rNow !== null && ` · ${rMult(rNow)}`}</div>
        </div>
      </header>
      <Ladder entry={p.entry} mark={p.mark} stop={stop} targets={(p.plan?.targets ?? []).map((t) => t.price)} />
      <Facts items={[
        ['Stop', p.UNPROTECTED ? 'NONE' : isNum(p.stop_distance_pct) ? `${pct(p.stop_distance_pct)} away` : '—', p.UNPROTECTED ? 'neg' : ''],
        ['If stopped', atStop === null ? null : usd(atStop, true), tone(atStop)],
        ['Size', usd(p.notional, false, 0)],
        ['Targets hit', `${p.targets_hit ?? 0} of ${(p.plan?.targets ?? []).length}`]]} />
      <Odds lad={lad} fresh={live.fresh} busy={live.busy} error={live.error} refresh={live.refresh} />
      <details className="pos-more" open={open} onToggle={(e) => setOpen(e.currentTarget.open)}>
        <summary>Thesis and plan</summary>
        <PlanBody p={p} />
      </details>
      <div className="pos-actions">
        <button type="button" onClick={() => openSheet({ cmd: 'move_stop', title: `Move the ${name} stop`,
          prefill: { symbol: p.symbol }, onDone: reload })}>Move stop</button>
        <button type="button" className="danger-ghost" onClick={() => openSheet({ cmd: 'close_position', title: `Close ${name}`,
          prefill: { symbol: p.symbol, expect_contracts: p.contracts }, onDone: reload })}>Close…</button>
      </div>
    </article>
  )
}

export function EntryRow({ e, o, reload }: { e: Entry; o?: EntryOdds; reload: () => void }) {
  const { openSheet, openCoin } = useDesk()
  const left = isNum(e.expires_ms) ? (e.expires_ms - Date.now()) / 1000 : null
  const size = isNum(e.amount) && isNum(e.limit_price) ? usd(e.amount * e.limit_price, false, 0) : null
  const risk = isNum(e.amount) && isNum(e.limit_price) && isNum(e.stop_price)   // an external order has neither
    ? usd((e.limit_price - e.stop_price) * e.amount, true, 0) : null
  return (
    <div className="row">
      <div className="row-main">
        <div className="row-title">
          <button className="link coin-link" type="button" onClick={() => openCoin(e.symbol)}><strong>{coin(e.symbol)}</strong></button>
          {e.setup && <span className="pill">{e.setup}</span>}
          {e.status !== 'pending' && <span className="pill warn">{e.status}</span>}
          {o?.p_fill !== undefined && <span className="pill demo">fills {odds(o.p_fill)}</span>}
          {o?.p_target !== undefined && <span className="pill demo">target first {odds(o.p_target)}</span>}
        </div>
        <div className="row-sub">{[`sell at ${price(e.limit_price)}`, `stop ${price(e.stop_price)}`, size,
          risk && `${risk} if stopped`, left === null ? null : left > 0 ? `expires in ${span(left)}` : 'expired']
          .filter(Boolean).join(' · ')}</div>
        {e.hint && <div className="row-sub warn-text">{e.hint}</div>}
        {e.level_reason && <div className="row-sub adv">{e.level_reason}</div>}
      </div>
      <button type="button" className="ghost small" title={`Cancel group ${e.group_id}`}
        onClick={() => openSheet({ cmd: 'cancel_entry_group', title: `Cancel the ${coin(e.symbol)} entries`,
          prefill: { group_id: e.group_id }, onDone: reload })}>Cancel</button>
    </div>
  )
}
