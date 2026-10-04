// The Radar: what the system is looking at. A search box for any coin, then the guardian's latest scan as stored in
// the journal (no exchange call): tradable candidates best score first, the vetoed ones with the reason, the watchlist.
import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { api, run, type RunResult } from '../api'
import { Notice } from '../components/figures'
import { Result } from '../components/Result'
import { useDesk } from '../ctx'
import { coin, isNum, pct, span, tone, when } from '../format'
import type { Candidate, Scan } from '../types'
import { Coin } from './Coin'
import { PlanCard, type BookPlan } from './Live'

const WINDOWS = [1, 6, 24]
const SCAN_STALE_S = 30 * 60   // the guardian scans every few minutes: an older newest scan means it is not scanning

export function SearchBox({ symbols, compact = false }: { symbols: string[]; compact?: boolean }) {
  const { openCoin } = useDesk()
  const [q, setQ] = useState('')
  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (q.trim()) { openCoin(q.trim().toUpperCase()); setQ('') }
  }
  return (
    <form className={'search' + (compact ? ' compact' : '')} onSubmit={submit} role="search">
      <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="6" /><path d="M20 20l-4.5-4.5" /></svg>
      <input type="search" value={q} onChange={(e) => setQ(e.target.value)} list={compact ? "coin-symbols-top" : "coin-symbols"} autoCapitalize="characters"
        placeholder={compact ? 'Look up a coin' : 'Look up a coin: SOL, HBAR…'} aria-label="Look up a coin" enterKeyHint="search" />
      <datalist id={compact ? "coin-symbols-top" : "coin-symbols"}>{symbols.map((s) => <option key={s} value={coin(s)} />)}</datalist>
      {!compact && <button type="submit" className="primary">Look up</button>}
    </form>
  )
}

function CandidateRow({ c }: { c: Candidate }) {
  const { openCoin } = useDesk()
  const ch = c.features.change_24h
  return (
    <button type="button" className="row cand" onClick={() => openCoin(c.symbol)}>
      <span className={'score' + (c.tradable ? '' : ' off')}>{c.score}</span>
      <span className="row-main">
        <span className="row-title">
          <strong>{coin(c.symbol)}</strong>
          {c.setups.map((s) => <span className="pill ok" key={s}>{s}</span>)}
          {c.tape && c.tape !== 'no_edge' && <span className="pill">{c.tape.replace(/_/g, ' ')}</span>}
          {isNum(ch) && <span className={'num ' + tone(ch)}>{pct(ch, true, 1)}</span>}
        </span>
        <span className={'row-sub clamp1' + (c.tradable ? '' : ' warn-text')}>
          {(c.tradable ? c.signals.slice(0, 3) : c.vetoes.slice(0, 2)).join(' · ')}</span>
      </span>
      <span className="muted small-print">{span(c.age_s)}</span>
    </button>
  )
}

export function Radar({ symbol, scan, setScan }: { symbol: string | null; scan: Scan | null; setScan: (s: Scan) => void }) {
  const { mode, openCoin } = useDesk()
  const [hours, setHours] = useState(6)
  const [setup, setSetup] = useState('')
  const [error, setError] = useState('')
  const [live, setLive] = useState<{ key: string; res: RunResult | { error: string } | null } | null>(null)
  const load = useCallback(() => {
    api<Scan>(`/api/research/candidates?hours=${hours}`).then((s) => { setScan(s); setError('') }, (e: Error) => setError(e.message))
  }, [hours, setScan])
  useEffect(load, [load])
  const [plan, setPlan] = useState<{ p: BookPlan | null; err: string; busy: boolean } | null>(null)
  const workNext = () => {
    setPlan((prev) => ({ p: prev?.p ?? null, err: '', busy: true }))
    run<BookPlan>('book_plan').then(({ result }) => setPlan({ p: result, err: '', busy: false }),
      (e: Error) => setPlan({ p: null, err: e.message, busy: false }))
  }
  const ask = (key: string) => {
    setLive({ key, res: null })
    run(key).then((res) => { setLive({ key, res }); load() }, (e: Error) => setLive({ key, res: { error: e.message } }))
  }
  const age = scan?.as_of ? (Date.now() - new Date(scan.as_of).getTime()) / 1000 : null
  const setups = [...new Set((scan?.tradable ?? []).flatMap((c) => c.setups))].sort()
  const tradable = (scan?.tradable ?? []).filter((c) => !setup || c.setups.includes(setup))
  return (
    <section className="tab-page">
      <SearchBox symbols={scan?.symbols ?? []} />
      {symbol && <Coin symbol={symbol} onClose={() => openCoin('')} />}
      <section className="block">
        <header className="block-head"><h2>What to work next</h2>
          <button type="button" className="ghost small" disabled={plan?.busy} onClick={workNext}>
            {plan?.busy ? 'planning…' : plan ? 'Plan again' : 'Plan the book'}</button></header>
        {!plan && <p className="muted small-print">Scans the movers now, reads the top ones and fits them to the free slots
          of the book (book_plan: a live read of the exchange, logged as a decision).</p>}
        {plan?.p && <PlanCard plan={plan.p} />}
        {plan?.err && <p className="card error">book_plan: {plan.err}</p>}
      </section>
      <section className="block">
        <header className="block-head">
          <h2>Tradable candidates</h2><span className="count">{scan ? scan.n_tradable : ''}</span>
          <div className="seg" role="group" aria-label="Scan window">
            {WINDOWS.map((h) => <button key={h} type="button" aria-pressed={hours === h} onClick={() => setHours(h)}>{h} h</button>)}
          </div>
        </header>
        {error && <p className="error">{error}</p>}
        {age !== null && age > SCAN_STALE_S && <Notice level="warn" title={`The newest scan is ${span(age)} old`}
          lines={['The guardian scans every few minutes. These candidates are history, not the market now: check that the guardian is running.']} />}
        {scan && <p className="muted small-print">{scan.as_of ? `Last scan ${when(scan.as_of)} · ` : ''}{scan.scored} coins scored in
          {' '}{scan.hours} h · {scan.n_tradable} tradable · {scan.n_vetoed} vetoed. The guardian scans; this page only reads it.</p>}
        {setups.length > 1 && (
          <div className="toolbar">
            <label className="toggle"><input type="radio" name="setup" checked={!setup} onChange={() => setSetup('')} /><span>All</span></label>
            {setups.map((s) => <label className="toggle" key={s}><input type="radio" name="setup" checked={setup === s}
              onChange={() => setSetup(s)} /><span>{s}</span></label>)}
          </div>
        )}
        <div className="card list">
          {!scan ? <p className="muted">loading…</p> : tradable.length ? tradable.map((c) => <CandidateRow key={c.id} c={c} />)
            : <div className="empty">Nothing tradable in the last {hours} h. {scan.scored ? '' : 'No scan in that window: is the guardian running?'}</div>}
        </div>
      </section>
      {scan && scan.n_vetoed > 0 && (
        <section className="block">
          <header className="block-head"><h2>Vetoed</h2><span className="count">{scan.n_vetoed}</span></header>
          <details className="card fold">
            <summary>Scored but blocked, with the reason</summary>
            <div className="list">{scan.vetoed.map((c) => <CandidateRow key={c.id} c={c} />)}</div>
          </details>
        </section>
      )}
      <section className="block">
        <header className="block-head"><h2>Watchlist</h2><span className="count">{scan?.watchlist.length || ''}</span></header>
        <div className="card list">
          {scan?.watchlist.length ? scan.watchlist.map((w) => (
            <button type="button" className="row cand" key={w.symbol} onClick={() => openCoin(w.symbol)}>
              <span className="row-main"><span className="row-title"><strong>{coin(w.symbol)}</strong>
                <span className="muted">until {when(w.until_ms)}</span></span>
              {w.reason && <span className="row-sub">{w.reason}</span>}</span>
            </button>
          )) : <div className="empty">Nothing watched. Look a coin up and press Watch to keep it in focus.</div>}
        </div>
      </section>
      {mode === 'advanced' && (
        <section className="block">
          <header className="block-head"><h2>Live reads</h2></header>
          <div className="toolbar">
            <button type="button" onClick={() => ask('market_map')}>Market map</button>
            <button type="button" onClick={() => ask('scan_shorts')}>Scan now</button>
          </div>
          <p className="muted small-print">These call the exchange. A scan logs every coin it scores as a research candidate.</p>
          {live && (live.res ? <Result title={live.key} res={live.res} /> : <p className="muted">running {live.key}…</p>)}
        </section>
      )}
    </section>
  )
}
