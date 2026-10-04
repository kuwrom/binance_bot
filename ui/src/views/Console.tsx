// Advanced: every desk command as a form (the Commands tab) and the repo's documents, read-only (the Docs tab).
// Both are a list beside a pane; on a phone one at a time.
import { useEffect, useState } from 'react'
import { api } from '../api'
import { CommandForm } from '../components/CommandForm'
import { render as md } from '../md'
import { useDesk } from '../ctx'

export function Commands() {
  const { commands, byKey } = useDesk()
  const [q, setQ] = useState('')
  const [current, setCurrent] = useState<string | null>(null)
  const needle = q.trim().toLowerCase()
  const groups = new Map<string, typeof commands>()
  for (const c of commands) {
    if (needle && !(c.key.toLowerCase().includes(needle) || c.help.toLowerCase().includes(needle))) continue
    groups.set(c.group, [...(groups.get(c.group) ?? []), c])
  }
  const cmd = current ? byKey[current] : undefined
  return (
    <section className={'tab-page split' + (cmd ? ' picked' : '')}>
      <aside className="split-list">
        <input type="search" placeholder="Filter commands" aria-label="Filter commands" value={q} onChange={(e) => setQ(e.target.value)} />
        {[...groups.entries()].map(([g, cmds]) => (
          <div key={g}>
            <h4>{g}</h4>
            {cmds.map((c) => (
              <button key={c.key} type="button" className={'cmd' + (current === c.key ? ' active' : '')} title={c.help}
                onClick={() => { setCurrent(c.key); window.scrollTo(0, 0) }}>
                <span>{c.key}</span>{c.write && <span className="pill bad">write</span>}
              </button>
            ))}
          </div>
        ))}
        {!groups.size && <p className="muted">No command matches.</p>}
      </aside>
      <div className="split-main">
        <button className="link back" type="button" onClick={() => setCurrent(null)}>‹ All commands</button>
        {cmd ? <div className="card"><CommandForm key={cmd.key} cmd={cmd} /></div>
          : <p className="card muted">Pick a command. Writes are marked and ask before they run; they go through the same
            checks, vetoes and audit trail as Claude's tools.</p>}
      </div>
    </section>
  )
}

export function Docs() {
  const [docs, setDocs] = useState<string[]>([])
  const [q, setQ] = useState('')
  const [open, setOpen] = useState<{ path: string; html: string } | null>(null)
  const [error, setError] = useState('')
  useEffect(() => { api<string[]>('/api/docs').then(setDocs, (e: Error) => setError(e.message)) }, [])
  const show = (path: string) => {
    api<string>('/api/doc?path=' + encodeURIComponent(path)).then((text) => {
      setOpen({ path, html: md(text) })   // md() escapes everything first
      setError('')
      window.scrollTo(0, 0)
    }, (e: Error) => setError(e.message))
  }
  return (
    <section className={'tab-page split' + (open ? ' picked' : '')}>
      <aside className="split-list">
        <input type="search" placeholder="Filter documents" aria-label="Filter documents" value={q} onChange={(e) => setQ(e.target.value)} />
        <ul>
          {docs.filter((d) => d.toLowerCase().includes(q.trim().toLowerCase())).map((d) => (
            <li key={d}><button className="link" type="button" onClick={() => show(d)}>{d}</button></li>
          ))}
        </ul>
      </aside>
      <div className="split-main">
        <button className="link back" type="button" onClick={() => setOpen(null)}>‹ All documents</button>
        <article className="card md">
          {error && <p className="error">{error}</p>}
          {open ? <><div className="muted">{open.path}</div><div dangerouslySetInnerHTML={{ __html: open.html }} /></>
            : <p className="muted">Pick a document.</p>}
        </article>
      </div>
    </section>
  )
}
