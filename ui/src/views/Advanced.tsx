// The Overview's Advanced column: the risk limits (overrides beside the .env baseline), the governor, the brief.
import { Md, Raw, Value } from '../components/Result'
import { useDesk } from '../ctx'
import { compact, label } from '../format'
import type { RiskParams } from '../types'
import type { DeskData } from './Overview'

function Limits({ rp, keys }: { rp: RiskParams; keys: string[] }) {
  return (
    <div className="scroll">
      <table className="params">
        <thead><tr><th>Limit</th><th className="num">Now</th><th className="num">.env</th><th className="num">Bounds</th></tr></thead>
        <tbody>
          {keys.map((k) => {
            const b = rp.bounds[k]
            return (
              <tr key={k} className={k in rp.overrides ? 'over' : undefined}>
                <td>{label(k)}</td><td className="num">{compact(rp.effective[k])}</td>
                <td className="num muted">{compact(rp.baseline_env[k])}</td>
                <td className="num muted">{b ? `${compact(b[0])} – ${compact(b[1])}` : ''}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

export function Advanced({ d, reload }: { d: DeskData; reload: () => void }) {
  const { openSheet } = useDesk()
  const rp = d.risk_params
  const keys = Object.keys(rp?.effective ?? {})
  const changed = keys.filter((k) => rp && k in rp.overrides), same = keys.filter((k) => !changed.includes(k))
  return (
    <>
      <section className="block">
        <header className="block-head"><h2>Risk parameters</h2></header>
        {rp && (
          <div className="card" id="params">
            <div className="result-head">
              <span className="muted">{rp.overrides_active ? `Overridden by experiment ${rp.experiment}:` : 'No overrides: the .env baseline applies.'}</span>
              <button className="ghost small" type="button" onClick={() => openSheet({ cmd: 'set_risk_params',
                title: 'Change risk parameters', onDone: reload })}>Change</button>
            </div>
            {changed.length > 0 && <Limits rp={rp} keys={changed} />}
            <details className="fold"><summary>{same.length} limits at the .env baseline</summary><Limits rp={rp} keys={same} /></details>
            <p className="muted small-print">{rp.note}</p>
            <Raw v={rp} />
          </div>
        )}
      </section>
      <section className="block">
        <header className="block-head"><h2>Governor</h2></header>
        {d.governor_status && <details className="card fold"><summary>Breakers, streak test and ladders</summary>
          <Value v={d.governor_status} /></details>}
      </section>
      <section className="block">
        <header className="block-head"><h2>Session brief</h2></header>
        {d.brief && <details className="card fold"><summary>What Claude reads at session start</summary><Md text={String(d.brief)} /></details>}
      </section>
    </>
  )
}
