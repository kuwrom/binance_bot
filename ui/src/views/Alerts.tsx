// The Alerts tab: grouped by kind, so 40 repeats of one guardian error read as one line.
import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import { coin, when } from '../format'
import type { Alert, Alerts as AlertsData } from '../types'

// [{kind, items: newest first}], newest group first
export function groupAlerts(rows: Alert[]): { kind: string; items: Alert[] }[] {
  const groups = new Map<string, Alert[]>()
  for (const a of rows) groups.set(a.kind, [...(groups.get(a.kind) ?? []), a])
  return [...groups.entries()].map(([kind, items]) => ({ kind, items }))
}

function AlertRow({ a, withKind }: { a: Alert; withKind: boolean }) {
  return (
    <div className={'alert' + (a.acked ? ' acked' : '')}>
      <div className="alert-head">
        <span className="muted">#{a.id} · {when(a.ts)}</span>
        {withKind && <span className={'pill' + (a.urgent ? ' bad' : '')}>{a.kind}</span>}
        {a.symbol && <span className="sym">{coin(a.symbol)}</span>}
      </div>
      <div className="msg">{a.message}</div>
    </div>
  )
}

function Toggle({ on, set, children }: { on: boolean; set: (v: boolean) => void; children: string }) {
  return <label className="toggle"><input type="checkbox" checked={on} onChange={(e) => set(e.target.checked)} /><span>{children}</span></label>
}

export function Alerts() {
  const [data, setData] = useState<AlertsData | null>(null)
  const [error, setError] = useState('')
  const [fresh, setFresh] = useState(true)
  const [urgent, setUrgent] = useState(true)
  const [group, setGroup] = useState(true)
  const load = useCallback(() => {
    api<AlertsData>('/api/alerts?limit=300').then(setData, (e: Error) => setError(e.message))
  }, [])
  useEffect(load, [load])
  const rows = (data?.alerts ?? []).filter((a) => (!urgent || a.urgent) && (!fresh || !a.acked))
  return (
    <section className="tab-page">
      <div className="toolbar">
        <Toggle on={fresh} set={setFresh}>New only</Toggle>
        <Toggle on={urgent} set={setUrgent}>Urgent only</Toggle>
        <Toggle on={group} set={setGroup}>Group by kind</Toggle>
        <button className="ghost small" type="button" onClick={load}>Refresh</button>
      </div>
      {data && <p className="muted">{rows.length} shown · acknowledged up to #{data.ack_id}</p>}
      {error && <p className="error">{error}</p>}
      <div className="card list">
        {!data ? <p className="muted">loading…</p> : !rows.length ? <div className="empty">No alerts match.</div>
          : !group ? rows.map((a) => <AlertRow key={a.id} a={a} withKind />)
            : groupAlerts(rows).map((g) => (
              <details className="agroup" key={g.kind}>
                <summary>
                  <div className="row-title">
                    <span className={'pill' + (g.items.some((a) => a.urgent) ? ' bad' : '')}>{g.kind}</span>
                    {g.items.length > 1 && <span className="count">×{g.items.length}</span>}
                    <span className="muted">{when(g.items[0].ts)}</span>
                  </div>
                  <div className="row-sub clamp">{g.items[0].message}</div>
                </summary>
                {g.items.map((a) => <AlertRow key={a.id} a={a} withKind={false} />)}
              </details>
            ))}
      </div>
    </section>
  )
}
