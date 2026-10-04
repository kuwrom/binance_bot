// The Overview. Basic answers in order: is the desk safe, how is the money, what is on the book and is it working.
// Advanced adds the governor, the risk parameters and the brief Claude gets. A failed read keeps the last good frame.
import { useCallback, useEffect, useRef, useState } from 'react'
import { api, settle, type Settled } from '../api'
import { Meter, Notice, Spark, type SparkPoint } from '../components/figures'
import { useDesk } from '../ctx'
import { coin, isNum, pct, span, tone, usd, when } from '../format'
import type { Account, Alerts, Book, Entry, Governor, Journal, Mode, Position, RiskParams, TradingState } from '../types'
import { Advanced } from './Advanced'
import { groupAlerts } from './Alerts'
import { EntryRow, PositionCard } from './PositionCard'
import { TradeRow } from './Trades'

const JOURNAL_EVERY_MS = 5 * 60 * 1000   // the journal is big: closed trades change rarely

export interface DeskData {
  trading_state?: TradingState; account?: Account; positions?: Position[]; pending_entries?: Entry[]
  governor_status?: Governor; risk_params?: RiskParams; brief?: string; journal?: Journal; alerts?: Alerts
  book?: Book; stale?: string
}
// errors: {command: why its last read failed}. A read that fails keeps its last good value in `data`, if it has one.
export interface DeskState { data: DeskData; errors: Record<string, string>; updated: Date | null; loading: boolean }

// What the Overview reads on every refresh (tests/test_web.py checks each name against the command registry).
const BASIC_READS = ['trading_state', 'account', 'positions', 'pending_entries', 'governor_status']
const ADVANCED_READS = ['risk_params', 'brief']

// Polls the desk. `active` = the Overview is on screen; `auto` = refresh every 60 s while the page is visible.
export function useDeskData(mode: Mode, active: boolean, auto: boolean, paused: boolean) {
  const [state, setState] = useState<DeskState>({ data: {}, errors: {}, updated: null, loading: false })
  const journalAt = useRef(0)
  const load = useCallback(async (full = false) => {
    setState((s) => ({ ...s, loading: true }))
    const keys = [...BASIC_READS, ...(mode === 'advanced' ? ADVANCED_READS : [])]
    const wantJournal = full || !journalAt.current || Date.now() - journalAt.current > JOURNAL_EVERY_MS
    const [alerts, book, journal, ...rest] = await Promise.all([
      api<Alerts>('/api/alerts?limit=300').catch(() => null), api<Book>('/api/research/book').catch(() => null),
      wantJournal ? settle<Journal>('journal', { limit: '50' }) : null, ...keys.map((k) => settle<unknown>(k))])
    const got: Record<string, Settled<unknown>> = Object.fromEntries(keys.map((k, i) => [k, rest[i]]))
    if (journal?.ok) journalAt.current = Date.now()
    setState((s) => {
      const data: DeskData = { ...s.data }
      for (const k of keys) { const g = got[k]; if (g.ok) (data as Record<string, unknown>)[k] = g.res.result }
      if (journal?.ok) data.journal = journal.res.result
      if (alerts) data.alerts = alerts
      if (book) data.book = book
      data.stale = keys.map((k) => { const g = got[k]; return g.ok ? g.res.stale_code : undefined }).find(Boolean)
      const errors = Object.fromEntries(keys.flatMap((k) => { const g = got[k]; return g.ok ? [] : [[k, g.err]] }))
      return { data, errors, updated: new Date(), loading: false }
    })
  }, [mode])

  useEffect(() => { void load(true) }, [load])
  useEffect(() => {
    if (!auto) return
    const tick = () => { if (!document.hidden && active && !paused) void load() }
    const timer = setInterval(tick, 60000)
    document.addEventListener('visibilitychange', tick)
    return () => { clearInterval(timer); document.removeEventListener('visibilitychange', tick) }
  }, [auto, active, paused, load])
  return { ...state, load }
}

function Banner({ d, errors }: { d: DeskData; errors: Record<string, string> }) {
  const ks = d.trading_state, gov = d.governor_status
  const others = Object.entries(errors).filter(([k]) => k !== 'trading_state')
  return (
    <div className="banner">
      {errors.trading_state && (
        <Notice level="crit" title={ks ? 'Desk state not refreshed' : 'Desk state unknown'} lines={[
          ks ? 'The kill switch and guardian below are from the last good read.'
            : 'The kill switch and the guardian could not be read: treat the desk as unguarded until this clears.',
          errors.trading_state]} />
      )}
      {others.length > 0 && <Notice level="warn" title={`Could not read ${others.map(([k]) => k.replace(/_/g, ' ')).join(', ')}`}
        lines={others.slice(0, 4).map(([k, e]) => `${k}: ${e}`)} />}
      {ks && ks.state !== 'ACTIVE' ? (
        <Notice level={ks.state === 'HALTED' ? 'crit' : 'warn'} title={`${ks.state}: no new entries`} lines={[ks.reason,
          ks.by && `Set by ${ks.by}${ks.at ? ' · ' + when(ks.at) : ''}`, ks.allowed && `Still allowed: ${ks.allowed}`,
          ks.clear && `Clears: ${ks.clear}`]} />
      ) : ks && !ks.guardian_alive && (
        <Notice level="crit" title="Guardian is not alive" lines={[
          `Last heartbeat ${span(ks.guardian_heartbeat_age_s)} ago: fills, stops and plans are unguarded.`]} />
      )}
      {(d.positions ?? []).filter((p) => p.UNPROTECTED).map((p) => (
        <Notice key={p.symbol} level="crit" title={`${coin(p.symbol)} has no stop on the exchange`}
          lines={[p.error ?? 'The short is open without a protective stop. Close it or set a stop.']} />
      ))}
      {gov?.halt && <Notice level="warn" title="Governor paused new entries" lines={[gov.reasons.join(' · '),
        gov.until && `Until ${when(gov.until)}`]} />}
      {d.account?.degraded && <Notice level="warn" title="P&L unverified"
        lines={['Day and week P&L are the old estimates: the equity marks could not be read.']} />}
      {d.stale && <Notice level="warn" title="Web server runs old code" lines={[d.stale, 'Writes are refused until it restarts.']} />}
    </div>
  )
}

function Delta({ label, v, sub }: { label: string; v: number; sub?: string | null }) {
  return <div className="delta"><span className="delta-label">{label}</span>
    <span className={'delta-value ' + tone(v)}>{usd(v, true)}</span>{sub && <span className="delta-sub">{sub}</span>}</div>
}

function Hero({ acct, journal }: { acct: Account; journal?: Journal }) {
  const start = acct.equity - (acct.day_pnl || 0)
  const closed = (journal?.recent_trades ?? []).filter((t) => t.closed_at && isNum(t.pnl))
    .sort((a, b) => String(a.closed_at).localeCompare(String(b.closed_at)))
  let cum = 0
  const pts: SparkPoint[] = [{ y: 0, value: usd(0), label: 'before the first trade' }]
  for (const t of closed) {
    cum += t.pnl ?? 0
    pts.push({ y: cum, value: usd(cum, true), label: `${coin(t.symbol)} ${usd(t.pnl, true)} · ${when(t.closed_at)}` })
  }
  const total = journal?.stats.total_pnl ?? cum
  return (
    <section className="card hero" aria-label="Equity">
      <div className="hero-figures">
        <div className="eyebrow">Equity</div>
        <div className="hero-value">{usd(acct.equity)}</div>
        <div className="deltas">
          <Delta label="Today" v={acct.day_pnl} sub={start > 0 ? pct((acct.day_pnl / start) * 100, true) : null} />
          <Delta label="This week" v={acct.week_pnl} />
          <Delta label="Open P&L" v={acct.unrealized} />
          <div className="delta"><span className="delta-label">Free margin</span>
            <span className="delta-value">{usd(acct.available, false, 0)}</span></div>
        </div>
      </div>
      {journal && (
        <div className="hero-chart">
          <div className="chart-head"><span>Realized P&L · last {closed.length} closed trades</span>
            <strong className={tone(total)}>{usd(total, true)}</strong></div>
          <Spark points={pts} />
        </div>
      )}
    </section>
  )
}

function RiskRoom({ acct, gov, positions }: { acct: Account; gov: Governor; positions?: Position[] }) {
  const lim = acct.limits
  const heat = (acct.equity * (lim.max_portfolio_heat_pct || 0)) / 100
  const used = (positions ?? []).reduce((s, p) => s + (p.notional || 0), 0)
  return (
    <div className="card meters">
      {isNum(gov.daily?.cap_usd) && <Meter label="Daily loss cap" used={gov.daily.cap_usd - gov.daily.room_usd}
        cap={gov.daily.cap_usd} text={`${usd(gov.daily.room_usd, false, 0)} left of ${usd(gov.daily.cap_usd, false, 0)}`} />}
      {heat > 0 && <Meter label="Risk at the stops" used={gov.open_risk_usd || 0} cap={heat}
        text={`${usd(gov.open_risk_usd || 0, false, 0)} of ${usd(heat, false, 0)}`} />}
      {positions && lim.max_total_notional > 0 && <Meter label="Exposure" used={used} cap={lim.max_total_notional}
        text={`${usd(used, false, 0)} of ${usd(lim.max_total_notional, false, 0)} · ${positions.length} of ${lim.max_positions} shorts`} />}
      {lim.dd_stop_pct > 0 && <Meter label="Drawdown from peak" used={gov.drawdown_pct || 0} cap={lim.dd_stop_pct}
        tick={lim.dd_halve_pct} text={pct(gov.drawdown_pct || 0)}
        hint={`Size halves at ${lim.dd_halve_pct}%, entries stop at ${lim.dd_stop_pct}%`} />}
      {lim.risk_per_trade_pct > 0 && (
        <p className="small-print muted">Next trade risks up to <strong>{usd(acct.equity * lim.risk_per_trade_pct / 100 * (gov.m_gov ?? 1), false, 0)}</strong>
          {' '}({pct(lim.risk_per_trade_pct, false, 1)} of equity{gov.m_gov !== 1 ? ` × governor ${gov.m_gov}` : ''}), before the per-coin
          adjustments and the heat and daily caps: size a coin on the Radar for the exact figure.</p>
      )}
      <div className="chips">
        {gov.regime && <span className="chip">Regime: {gov.regime}</span>}
        {gov.cell && <span className="chip adv">Cell: {gov.cell}</span>}
        {isNum(gov.m_gov) && <span className={'chip' + (gov.m_gov < 1 ? ' warn' : ' adv')}>Sizing ×{gov.m_gov}</span>}
        {!!gov.streak?.current && <span className="chip warn">Loss streak {gov.streak.current}</span>}
      </div>
    </div>
  )
}

function Attention({ alerts }: { alerts: Alerts }) {
  const { openTab } = useDesk()
  const open = alerts.alerts.filter((a) => a.urgent && !a.acked)
  if (!open.length) return <div className="card list"><div className="empty">Nothing needs you right now.</div></div>
  return (
    <div className="card list">
      {groupAlerts(open).slice(0, 4).map((g) => (
        <div className="row" key={g.kind}>
          <div className="row-main">
            <div className="row-title"><span className="pill bad">{g.kind}</span>
              {g.items.length > 1 && <span className="muted">×{g.items.length}</span>}
              <span className="muted">{when(g.items[0].ts)}</span></div>
            <div className="row-sub clamp">{g.items[0].message}</div>
          </div>
        </div>
      ))}
      <button className="link" type="button" onClick={() => openTab('alerts')}>All {open.length} urgent alerts</button>
    </div>
  )
}

// A read with no value yet: still loading, or failed (never "empty": an unread book is not a flat book).
function Unread({ what, error, updated }: { what: string; error?: string; updated: Date | null }) {
  if (error) return <div className="card empty error">{what} could not be read: {error}</div>
  return <div className="card empty">{updated ? `${what} could not be read.` : 'loading…'}</div>
}

export function Overview({ desk }: { desk: ReturnType<typeof useDeskData> }) {
  const { mode, openTab } = useDesk()
  const { data: d, errors, updated, loading, load } = desk
  const failed = Object.keys(errors)
  const stale = failed.filter((k) => (d as Record<string, unknown>)[k] !== undefined)
  const reload = () => void load(true)
  const closed = (d.journal?.recent_trades ?? []).filter((t) => t.closed_at).slice(0, 5)
  return (
    <section className="tab-page">
      <Banner d={d} errors={errors} />
      <div className="home">
        <div className="col">
          {d.account ? <Hero acct={d.account} journal={d.journal} />
            : <section className="block"><Unread what="Equity and P&L" error={errors.account} updated={updated} /></section>}
          <section className="block">
            <header className="block-head"><h2>Open shorts</h2><span className="count">{d.positions?.length || ''}</span></header>
            <div className="stack">
              {!d.positions ? <Unread what="Open shorts" error={errors.positions} updated={updated} />
                : d.positions.length ? d.positions.map((p) => <PositionCard key={p.symbol} p={p} lad={d.book?.ladders[p.symbol]} reload={reload} />)
                  : <div className="card empty">Flat. No open shorts.</div>}
            </div>
          </section>
          <section className="block">
            <header className="block-head"><h2>Resting entries</h2><span className="count">{d.pending_entries?.length || ''}</span></header>
            {!d.pending_entries ? <Unread what="Resting entries" error={errors.pending_entries} updated={updated} /> : (
              <div className="card list">
                {d.pending_entries.length ? d.pending_entries.map((e) => <EntryRow key={e.id} e={e} o={d.book?.entries[String(e.id)]} reload={reload} />)
                  : <div className="empty">No resting entries.</div>}
              </div>
            )}
          </section>
          <section className="block">
            <header className="block-head"><h2>Recent trades</h2></header>
            <div className="card list">
              {closed.length ? closed.map((t) => <TradeRow key={t.id} t={t} expandable={false} />) : <div className="empty">No closed trades yet.</div>}
              {closed.length > 0 && <button className="link" type="button" onClick={() => openTab('trades')}>All trades and win rates</button>}
            </div>
          </section>
        </div>
        <div className="col">
          <section className="block">
            <header className="block-head"><h2>Risk room</h2></header>
            {d.account && d.governor_status ? <RiskRoom acct={d.account} gov={d.governor_status} positions={d.positions} />
              : <Unread what="Risk limits" error={errors.governor_status ?? errors.account} updated={updated} />}
          </section>
          <section className="block">
            <header className="block-head"><h2>Needs attention</h2></header>
            {d.alerts && <Attention alerts={d.alerts} />}
          </section>
          {mode === 'advanced' && <Advanced d={d} reload={reload} />}
        </div>
      </div>
      <footer className="foot">
        <span className={failed.length ? 'error' : 'muted'}>
          {failed.length ? `${failed.join(', ')} failed` + (stale.length ? ` · showing the last good read of ${stale.join(', ')}` : '')
            : updated ? 'Updated ' + updated.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : 'loading…'}
          {loading && updated ? ' · refreshing…' : ''}
        </span>
        <button className="ghost small" type="button" disabled={loading} onClick={reload}>Refresh</button>
      </footer>
    </section>
  )
}
