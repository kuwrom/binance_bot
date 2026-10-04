// One coin, as the desk sees it: where it stands (position, resting, candidate, vetoed, watchlist, not qualified),
// why, its last scan and history, the session's read, our past trades. Buttons run the live reads on demand.
import { useEffect, useState } from 'react'
import { api, run, type RunResult } from '../api'
import { Facts, Spark } from '../components/figures'
import { Raw, Result } from '../components/Result'
import { useDesk } from '../ctx'
import { coin, compact, isNum, odds, pct, price, since, span, tone, usd, when } from '../format'
import type { Candidate, CoinRead, CoinView, Dict, StopOption } from '../types'
import { SizeCard, type Budget } from './Live'

const STANDING: Dict<[string, string]> = {
  position: ['Open short', 'ok'], resting: ['Entry resting', 'demo'], candidate: ['Tradable candidate', 'ok'],
  vetoed: ['Vetoed', 'bad'], watchlist: ['On the watchlist', 'demo'], stale: ['Not in the current scan', ''],
  unscored: ['Does not qualify now', ''], unknown: ['Unknown ticker', 'bad'],
}

// "+10 lower high on 1h" -> its weight and its text
export function Signals({ items, veto = false }: { items: string[]; veto?: boolean }) {
  if (!items.length) return null
  return (
    <ul className={'signals' + (veto ? ' veto' : '')}>
      {items.map((s) => {
        const m = s.match(/^([+-]\d+)\s+(.*)$/)
        return <li key={s}>{m ? <><span className="weight">{m[1]}</span>{m[2]}</> : s}</li>
      })}
    </ul>
  )
}

export function ScanCard({ c }: { c: Candidate }) {
  const f = c.features
  return (
    <div className="card">
      <div className="result-head">
        <span className="score">{c.score}</span>
        <div><strong>{c.tradable ? 'Tradable' : 'Vetoed'} at the last scan</strong>
          <div className="muted small-print">{when(c.ts)} · {span(c.age_s)} ago · at {price(c.price)} · regime {c.regime ?? '—'}</div></div>
      </div>
      <div className="chips">
        {c.setups.length ? c.setups.map((s) => <span className="chip on" key={s}>setup: {s}</span>) : <span className="chip">no setup detected</span>}
        {c.tape && <span className="chip">tape: {c.tape.replace(/_/g, ' ')}</span>}
        {typeof f.lifecycle_stage === 'string' && <span className="chip">{f.lifecycle_stage.replace(/_/g, ' ')}</span>}
        {f.liq_tier !== undefined && <span className="chip">liquidity tier {f.liq_tier}</span>}
      </div>
      {c.vetoes.length > 0 && <><div className="chips-label">Why it is blocked</div><Signals items={c.vetoes} veto /></>}
      {c.signals.length > 0 && <><div className="chips-label">What scored</div><Signals items={c.signals} /></>}
      <Facts items={[
        ['24h change', pct(f.change_24h, true, 1), tone(f.change_24h)], ['Off 24h high', pct(f.off_high_pct, false, 1)],
        ['Funding 8h', isNum(f.funding_8h) ? pct(f.funding_8h * 100, true, 3) : null],
        ['OI', isNum(f.oi_value) ? `$${compact(f.oi_value)} (${pct(f.oi_change_24h, true, 0)})` : null],
        ['Volume 24h', isNum(f.quote_volume) ? `$${compact(f.quote_volume)}` : null], ['RSI 1h', isNum(f.rsi_1h) ? f.rsi_1h.toFixed(0) : null],
        ['No-edge odds', c.p_target === null ? null : odds(c.p_target)]]} />
      {c.p_target !== null && <p className="muted small-print">No-edge odds: the chance a standard bracket (target {price(c.target)}
        {' '}before stop {price(c.stop)}, 24 h) wins with no thesis at all. A setup has to beat it.</p>}
      {c.outcome && <p className="small-print">What happened after: short return {pct(c.outcome.short_ret_4h, true, 1)} in 4 h,
        {' '}{pct(c.outcome.short_ret_24h, true, 1)} in 24 h, {pct(c.outcome.short_ret_72h, true, 1)} in 72 h.</p>}
    </div>
  )
}

const stopsOf = (r?: CoinRead | null): [string, StopOption][] | undefined =>
  r ? Object.entries(r.stops).filter((e): e is [string, StopOption] => typeof e[1] === 'object' && e[1] !== null) : undefined

function LiveRead({ r }: { r: CoinRead }) {
  const stops = stopsOf(r) ?? []
  const ev = r.evidence
  return (
    <div className="card">
      <div className="result-head"><strong>Live read</strong>
        <span className={'pill ' + (r.verdict.action === 'short_setup' ? 'ok' : '')}>{r.verdict.action.replace(/_/g, ' ')}</span>
        {r.verdict.playbook && <span className="pill">playbook: {r.verdict.playbook}</span>}
        {isNum(r.verdict.size_mult) && <span className="pill">size ×{r.verdict.size_mult}</span>}</div>
      <div className="chips">
        <span className="chip">stage: {r.stage.replace(/_/g, ' ')}</span><span className="chip">game: {r.game.replace(/_/g, ' ')}</span>
        {r.controller.map((c) => <span className="chip" key={c}>{c.replace(/_/g, ' ')}</span>)}
      </div>
      <Signals items={[...r.verdict.why, ...r.verdict.notes]} />
      {ev?.hit_rate_pct && <Facts items={[
        ['Hit rate (blended)', pct(ev.hit_rate_pct.estimate, false, 0)], ['Avg 24h short return', pct(ev.avg_short_ret_24h?.estimate, true, 2)],
        ['Sample', ev.n ? `${ev.n.universe} all · ${ev.n.archetype} like it · ${ev.n.coin_in_archetype} this coin` : null]]} />}
      {stops.length > 0 && (
        <div className="scroll"><table>
          <thead><tr><th>Stop</th><th className="num">Price</th><th className="num">Away</th><th className="num">Max lev</th><th>How</th></tr></thead>
          <tbody>{stops.map(([name, s]) => (
            <tr key={name}><td>{name.replace(/_/g, ' ')}</td><td className="num">{price(s.price)}</td>
              <td className="num">{pct(s.distance_pct)}</td><td className="num">{s.allowed ? `${s.max_leverage}x` : 'not allowed'}</td>
              <td className="muted">{s.method}</td></tr>))}</tbody>
        </table></div>
      )}
      {r.registry && <div className="chips">{r.registry.nodes.map((n) => (
        <span className={'chip ' + (n.status === 'live' ? 'on' : '')} key={n.id} title={n.verdict}>{n.id}: {n.status}</span>))}</div>}
      <Raw v={r} />
    </div>
  )
}

export function Coin({ symbol, onClose }: { symbol: string; onClose: () => void }) {
  const { openSheet, mode } = useDesk()
  const [view, setView] = useState<CoinView | null>(null)
  const [error, setError] = useState('')
  const [live, setLive] = useState<{ key: string; res: RunResult | { error: string } | null } | null>(null)
  const [tick, setTick] = useState(0)
  const [read, setRead] = useState<{ r: CoinRead | null; err: string; busy: boolean } | null>(null)
  const [budget, setBudget] = useState<{ b: Budget | null; err: string; busy: boolean } | null>(null)
  useEffect(() => {
    setView(null); setError(''); setLive(null); setRead(null); setBudget(null)
    api<CoinView>('/api/research/coin?symbol=' + encodeURIComponent(symbol)).then(setView, (e: Error) => setError(e.message))
  }, [symbol, tick])
  const readNow = () => {
    setRead({ r: null, err: '', busy: true })
    run<CoinRead>('read_coin', { symbol }).then(({ result }) => setRead({ r: result, err: '', busy: false }),
      (e: Error) => setRead({ r: null, err: e.message, busy: false }))
  }
  const sizeNow = () => {
    setBudget({ b: null, err: '', busy: true })
    run<Budget>('risk_budget', { symbol }).then(({ result }) => setBudget({ b: result, err: '', busy: false }),
      (e: Error) => setBudget({ b: null, err: e.message, busy: false }))
  }
  const ask = (key: string, args: Dict<string>) => {
    setLive({ key, res: null })
    run(key, args).then((res) => setLive({ key, res }), (e: Error) => setLive({ key, res: { error: e.message } }))
  }
  if (error) return <div className="card coin"><p className="error">{error}</p><button type="button" className="ghost small" onClick={onClose}>Close</button></div>
  if (!view) return <div className="card coin"><p className="muted">looking up {symbol}…</p></div>
  const [name, cls] = STANDING[view.standing.state] ?? [view.standing.state, '']
  const sym = view.symbol
  const scores = view.history.slice().reverse().map((h) => ({ y: h.score, value: `score ${h.score}`,
    label: `${h.tradable ? 'tradable' : `${h.vetoes} veto${h.vetoes === 1 ? '' : 'es'}`} · ${when(h.ts)}` }))
  return (
    <div className="coin">
      <div className="card coin-head">
        <div className="pos-head">
          <div className="pos-name"><h3>{coin(sym)}<span className={'pill ' + cls}>{name}</span></h3>
            <p className="thesis">{view.standing.why}</p>
            {view.market && <p className="muted small-print">Last logged {when(view.market.ts)}: price {price(view.market.price)}
              {' '}· 24h volume ${compact(view.market.quote_volume_24h)}
              {isNum(view.market.funding_rate) && ` · funding ${pct(view.market.funding_rate * 100, true, 4)}`}</p>}</div>
          <button className="icon-btn" type="button" aria-label="Close" onClick={onClose}>
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" /></svg></button>
        </div>
        <div className="toolbar">
          <button type="button" className="primary" disabled={read?.busy} onClick={readNow}>Read it now</button>
          <button type="button" disabled={budget?.busy} onClick={sizeNow}>Size a short</button>
          <button type="button" onClick={() => ask('chart', { symbol: sym })}>Chart</button>
          <button type="button" onClick={() => openSheet({ cmd: 'watch', title: view.watch ? `Stop watching ${coin(sym)}` : `Watch ${coin(sym)}`,
            prefill: view.watch ? { symbol: sym, remove: 'true' } : { symbol: sym }, onDone: () => setTick((t) => t + 1) })}>
            {view.watch ? 'Unwatch' : 'Watch'}</button>
          {mode === 'advanced' && <button type="button" onClick={() => ask('coin_snapshot', { symbol: sym })}>Snapshot</button>}
          {mode === 'advanced' && <button type="button" onClick={() => ask('coin_profile', { symbol: sym })}>Profile</button>}
        </div>
      </div>
      {read && (read.busy ? <p className="muted">reading {coin(sym)} (live market read)…</p>
        : read.r ? <LiveRead r={read.r} /> : <p className="card error">read_coin: {read.err}</p>)}
      {budget && (budget.busy ? <p className="muted">sizing a short on {coin(sym)}…</p>
        : budget.b ? <SizeCard b={budget.b} stops={stopsOf(read?.r)} /> : <p className="card error">risk_budget: {budget.err}</p>)}
      {live && (live.res === null ? <p className="muted">running {live.key} (live market read)…</p>
        : <Result title={live.key} res={live.res} />)}
      {view.latest && <ScanCard c={view.latest} />}
      {scores.length > 1 && (
        <div className="card">
          <div className="chart-head"><span>Scan score · last {scores.length} scans</span><strong>{scores[scores.length - 1].value}</strong></div>
          <Spark points={scores} />
          {view.outcomes && <p className="muted small-print">After {view.outcomes.rows} labelled scans of this coin, a short made
            {' '}{pct(view.outcomes.avg_short_ret_24h, true, 1)} in 24 h on average, positive {odds(view.outcomes.hit_rate)} of the time.
            Scan rows overlap, so this is an anecdote about the coin, not a sample.</p>}
        </div>
      )}
      {view.read && (
        <div className="card">
          <div className="result-head"><strong>Session read</strong><span className="pill">bias {view.read.bias}</span>
            <span className="muted">{since(view.read.ts)} ago</span></div>
          <p className="thesis">{view.read.read}</p>
          <Facts items={[['Invalidation', isNum(view.read.invalidation) ? price(view.read.invalidation) : null]]} />
        </div>
      )}
      {view.trades.length > 0 && (
        <div className="card list">
          <div className="chips-label">Our past trades on {coin(sym)}</div>
          {view.trades.map((t) => (
            <div className="row" key={t.id}><div className="row-main">
              <div className="row-title"><strong>#{t.id}</strong>{t.setup && <span className="pill">{t.setup}</span>}
                <span className="muted">{when(t.closed_at)}</span></div>
              {t.exit_reason && <div className="row-sub clamp1">{t.exit_reason}</div>}</div>
              <span className={'row-num ' + tone(t.pnl)}>{usd(t.pnl, true)}</span></div>
          ))}
        </div>
      )}
      <Raw v={view} />
    </div>
  )
}
