// The Results tab: does any of this work? Our own trades by setup, then the measured edge of each setup over every
// logged bracket (observed P(target first) against the no-edge baseline), then each closed trade with its thesis and
// lesson. Advanced adds the edge by tape state, the rulebook and the reports.
import { useEffect, useState } from 'react'
import { run, settle, type RunResult } from '../api'
import { Facts } from '../components/figures'
import { Raw, Result } from '../components/Result'
import { useDesk } from '../ctx'
import { coin, isNum, odds, pct, price, rMult, span, tone, usd, when } from '../format'
import type { Calibration, Dict, Edge, Journal, Trade } from '../types'

// A closed trade as a row; expandable into its thesis, lesson and numbers.
export function TradeRow({ t, expandable = true }: { t: Trade; expandable?: boolean }) {
  const { openCoin } = useDesk()
  const top = (
    <div className="row-title">
      {expandable ? <strong>{coin(t.symbol)}</strong>
        : <button className="link coin-link" type="button" onClick={() => openCoin(t.symbol)}><strong>{coin(t.symbol)}</strong></button>}
      {t.setup && <span className="pill">{t.setup}</span>}
      <span className="muted">{when(t.closed_at)}</span>
    </div>
  )
  const pnl = <span className={'row-num ' + tone(t.pnl)}>{usd(t.pnl, true)}</span>
  const main = <div className="row-main">{top}{t.exit_reason && <div className="row-sub clamp1">{t.exit_reason}</div>}</div>
  if (!expandable) return <div className="row">{main}{pnl}</div>
  const { entry_features: _features, ...slim } = t
  return (
    <details className="trade">
      <summary className="row">{main}{pnl}</summary>
      <div className="plan">
        {t.thesis && <div><div className="chips-label">Thesis</div><p className="thesis">{t.thesis}</p></div>}
        {t.exit_reason && <div><div className="chips-label">Exit</div><p className="thesis">{t.exit_reason}</p></div>}
        {t.lesson && <div><div className="chips-label">Lesson</div><p className="thesis">{t.lesson}</p></div>}
        <Facts className="wide" items={[
          ['Entry → exit', `${price(t.entry_price)} → ${price(t.exit_price)}`],
          ['Size', `${usd(t.notional, false, 0)} · ${t.leverage}x`],
          ['Held', t.opened_at && t.closed_at ? span((new Date(t.closed_at).getTime() - new Date(t.opened_at).getTime()) / 1000) : null],
          ['Worst / best', isNum(t.mae_pct) || isNum(t.mfe_pct) ? `${pct(-Math.abs(t.mae_pct ?? 0), true)} / ${pct(Math.abs(t.mfe_pct ?? 0), true)}` : null],
          ['Fees + funding', isNum(t.fees) || isNum(t.funding) ? usd((t.fees ?? 0) + (t.funding ?? 0), true) : null],
          ['Experiment', t.experiment]]} />
        <Raw v={slim} />
      </div>
    </details>
  )
}

function Tile({ label, text, tone: t = '' }: { label: string; text: string; tone?: string }) {
  return <div className="tile"><div className="tile-label">{label}</div><div className={'tile-value ' + t}>{text}</div></div>
}

// Observed P(target before stop) against the no-edge baseline, per group. Only a significant row is an edge.
function EdgeTable({ groups, name }: { groups: Dict<Edge>; name: string }) {
  const rows = Object.entries(groups).sort((a, b) => b[1].n - a[1].n)
  return (
    <div className="scroll">
      <table>
        <thead><tr><th>{name}</th><th className="num">Episodes</th><th className="num">Target first</th>
          <th className="num">No edge</th><th className="num">Edge</th><th className="num">Avg R</th><th>Verdict</th></tr></thead>
        <tbody>
          {rows.map(([k, e]) => (
            <tr key={k}>
              <td>{k}</td><td className="num">{e.n}</td><td className="num">{odds(e.p_target_observed)}</td>
              <td className="num muted">{odds(e.p_baseline)}</td>
              <td className={'num ' + (e.edge_significant ? tone(e.edge_pp) : '')}>{e.edge_pp > 0 ? '+' : ''}{e.edge_pp} pp</td>
              <td className={'num ' + (e.edge_significant ? tone(e.avg_R_ex_time) : '')}>{rMult(e.avg_R_ex_time)}</td>
              <td>{e.edge_significant ? <span className={'pill ' + (e.edge_pp > 0 ? 'ok' : 'bad')}>{e.edge_pp > 0 ? 'edge' : 'negative'}</span>
                : <span className="pill">{e.n < 30 ? 'too few' : 'not proven'}</span>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

const REPORTS: [string, string, Dict<string>][] = [['audit', 'Audit (24 h)', { since: '24h' }],
  ['research_report', 'Research report', {}], ['calibration_report', 'Calibration', {}], ['list_setups', 'Setups', {}],
  ['journal', 'Full journal', {}]]

export function Trades({ journal }: { journal?: Journal }) {
  const { mode } = useDesk()
  const [own, setOwn] = useState<Journal | undefined>(journal)
  const [cal, setCal] = useState<Calibration | null>(null)
  const [report, setReport] = useState<{ key: string; res: RunResult | { error: string } | null } | null>(null)
  useEffect(() => { if (journal) setOwn(journal) }, [journal])
  useEffect(() => {
    if (!journal) void settle<Journal>('journal', { limit: '50' }).then((g) => { if (g.ok) setOwn(g.res.result) })
    void settle<Calibration>('calibration_report').then((g) => { if (g.ok) setCal(g.res.result) })
  }, [])   // eslint-disable-line react-hooks/exhaustive-deps
  const runReport = (key: string, args: Dict<string>) => {
    setReport({ key, res: null })
    run(key, args).then((res) => setReport({ key, res }), (e: Error) => setReport({ key, res: { error: e.message } }))
  }
  if (!own) return <section className="tab-page"><p className="muted">loading…</p></section>
  const st = own.stats
  const sets = Object.entries(st.by_setup).sort((a, b) => b[1].n - a[1].n)
  const n = sets.reduce((s, [, x]) => s + x.n, 0), wins = sets.reduce((s, [, x]) => s + x.wins, 0)
  const closed = own.recent_trades.filter((t) => t.closed_at)
  return (
    <section className="tab-page">
      <div className="tiles">
        <Tile label="Closed trades" text={String(st.closed_trades ?? n)} />
        <Tile label="Realized P&L" text={usd(st.total_pnl, true)} tone={tone(st.total_pnl)} />
        <Tile label="Win rate" text={n ? pct((wins / n) * 100, false, 0) : '—'} />
      </div>
      <section className="block">
        <header className="block-head"><h2>Our trades by setup</h2></header>
        <div className="card" id="trades-setups">
          <div className="scroll">
            <table>
              <thead><tr><th>Setup</th><th className="num">Trades</th><th className="num">Win rate</th>
                <th className="num">Avg R</th><th className="num">P&L</th></tr></thead>
              <tbody>
                {sets.map(([name, s]) => (
                  <tr key={name}><td>{name}</td><td className="num">{s.n}</td><td className="num">{pct(s.win_rate, false, 0)}</td>
                    <td className="num">{rMult(s.avg_r)}</td><td className={'num ' + tone(s.pnl)}>{usd(s.pnl, true)}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
          {n < 30 && <p className="muted small-print">{n} trades is an anecdote: read the measured edge below before trusting a win rate.</p>}
        </div>
      </section>
      {cal && (
        <section className="block">
          <header className="block-head"><h2>Measured edge by setup</h2></header>
          <div className="card">
            <EdgeTable groups={cal.expectancy_by_setup} name="Setup" />
            <p className="muted small-print">{cal.labelled_brackets} labelled brackets over {cal.days} days. {cal.inference}.
              Edge = observed target-first rate minus the no-edge baseline; only a proven row is real drift.</p>
            {mode === 'advanced' && <details className="fold"><summary>By tape state</summary>
              <EdgeTable groups={cal.expectancy_by_tape_state} name="Tape state" /></details>}
          </div>
        </section>
      )}
      <section className="block">
        <header className="block-head"><h2>Closed trades</h2></header>
        <div className="card list">
          {closed.length ? closed.map((t) => <TradeRow key={t.id} t={t} />) : <div className="empty">No closed trades yet.</div>}
        </div>
      </section>
      {mode === 'advanced' && (
        <>
          <section className="block">
            <header className="block-head"><h2>Rulebook</h2></header>
            <details className="card fold">
              <summary>Current learnings, best first</summary>
              <div className="list">
                {own.learnings.map((l) => (
                  <div className="row" key={l.id}><div className="row-main">
                    <div className="row-title"><strong>#{l.id}</strong>
                      <span className={'pill' + (l.status === 'adopted' ? ' ok' : '')}>{l.status}</span>
                      {l.setup && <span className="pill">{l.setup}</span>}{l.tag && <span className="muted">{l.tag}</span>}</div>
                    <div className="row-sub">{l.text}</div>
                  </div></div>
                ))}
                {own.learnings_more > 0 && <div className="empty">{own.learnings_more} more: the learnings command.</div>}
              </div>
            </details>
          </section>
          <section className="block">
            <header className="block-head"><h2>Reports</h2></header>
            <div className="toolbar">
              {REPORTS.map(([key, name, args]) => <button key={key} type="button" onClick={() => runReport(key, args)}>{name}</button>)}
            </div>
            {report && (report.res ? <Result title={report.key} res={report.res} /> : <p className="muted">running {report.key}…</p>)}
          </section>
        </>
      )}
    </section>
  )
}
