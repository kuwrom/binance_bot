// Renders any command result: objects as key/value tables, lists of objects as tables, long or Markdown text
// through md.ts (escaped), chart paths as images. The raw JSON is one click away.
import type { ReactNode } from 'react'
import type { RunResult } from '../api'
import { render as md } from '../md'
import type { Dict, Json } from '../types'

const isObj = (v: Json): v is Dict => v !== null && typeof v === 'object' && !Array.isArray(v)
const short = (v: Json) => {
  const s = JSON.stringify(v)
  return s.length > 160 ? s.slice(0, 157) + '…' : s
}

export function Md({ text, className = 'md' }: { text: string; className?: string }) {
  return <div className={className} dangerouslySetInnerHTML={{ __html: md(text) }} />
}

function Scalar({ v, k }: { v: Json; k: string }) {
  if (v === null || v === undefined) return <span className="muted">—</span>
  if (typeof v === 'boolean') return <span className={v ? 'pill ok' : 'pill bad'}>{String(v)}</span>
  if (typeof v === 'number') {
    const signed = /pnl|_r$|^r$|realized|unrealized/i.test(k)
    return <span className={'num' + (signed ? (v > 0 ? ' pos' : v < 0 ? ' neg' : '') : '')}>
      {Number.isInteger(v) ? String(v) : String(+v.toFixed(6))}</span>
  }
  const s = String(v)
  if (/\.png$/.test(s) && k === 'path') {
    return <div className="chart"><img src={'/api/png?path=' + encodeURIComponent(s)} alt={s} /><div className="muted">{s}</div></div>
  }
  if (s.includes('\n') || /^#{1,3} |\*\*|^- /m.test(s)) return <Md text={s} />
  return <span>{s}</span>
}

function Table({ rows, depth }: { rows: Dict[]; depth: number }) {
  const cols: string[] = []
  for (const r of rows.slice(0, 200)) for (const k of Object.keys(r)) if (!cols.includes(k)) cols.push(k)
  const shown = cols.slice(0, 16)
  return (
    <div className="scroll">
      <table>
        <thead><tr>{shown.map((c) => <th key={c}>{c}</th>)}</tr></thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>
              {shown.map((c) => {
                const v = r[c]
                const nested = isObj(v) || Array.isArray(v)
                return (
                  <td key={c}>
                    {!nested ? <Scalar v={v} k={c} />
                      : depth < 1 && JSON.stringify(v).length > 160
                        ? <details><summary>{short(v)}</summary><Value v={v} k={c} depth={depth + 1} /></details>
                        : <code>{short(v)}</code>}
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
      {cols.length > 16 && <div className="muted">{cols.length - 16} more columns: see Raw JSON</div>}
    </div>
  )
}

export function Value({ v, k = '', depth = 0 }: { v: Json; k?: string; depth?: number }): ReactNode {
  if (Array.isArray(v)) {
    if (!v.length) return <span className="muted">none</span>
    if (v.every(isObj)) return <Table rows={v as Dict[]} depth={depth} />
    return <ul className="plain">{v.map((x, i) => <li key={i}>
      {isObj(x) || Array.isArray(x) ? <Value v={x} k={k} depth={depth + 1} /> : <Scalar v={x} k={k} />}</li>)}</ul>
  }
  if (isObj(v)) {
    const keys = Object.keys(v)
    if (!keys.length) return <span className="muted">{'{}'}</span>
    if (depth > 3) return <code>{short(v)}</code>
    return (
      <table className="kv"><tbody>
        {keys.map((key) => <tr key={key}><th>{key}</th><td><Value v={v[key]} k={key} depth={depth + 1} /></td></tr>)}
      </tbody></table>
    )
  }
  return <Scalar v={v} k={k} />
}

export function Raw({ v, always = false }: { v: Json; always?: boolean }) {
  return <details className={'raw' + (always ? '' : ' adv')}><summary>Raw JSON</summary><pre>{JSON.stringify(v, null, 2)}</pre></details>
}

// A result card: title, status badge, rendered value, raw JSON. `res` is a run's answer or {error}.
export function Result({ title, res, write = false }: { title: string; res: RunResult | { error: string }; write?: boolean }) {
  if ('error' in res) {
    return <div className="card result"><div className="result-head"><h3>{title}</h3><span className="pill bad">error</span></div>
      <p className="error">{res.error}</p></div>
  }
  const r = res.result
  const failed = isObj(r) && r.ok === false
  return (
    <div className="card result">
      <div className="result-head">
        <h3>{title}</h3>
        {failed ? <span className="pill bad">{isObj(r) && r.unknown ? 'unknown: re-read positions' : 'refused'}</span>
          : write && <span className="pill ok">done</span>}
        <span className="muted">{res.ms} ms</span>
      </div>
      {res.stale_code && <p className="warn">{res.stale_code}</p>}
      {failed && isObj(r) && typeof r.error === 'string' && <p className="error">{r.error}</p>}
      <Value v={r} />
      <Raw v={r} always />
    </div>
  )
}
