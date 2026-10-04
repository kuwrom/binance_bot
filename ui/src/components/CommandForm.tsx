// One desk command as a form (generated from /api/commands), in the Commands tab or in a sheet opened from a card.
// A write shows what it will send and asks once more, in the page, before it runs.
import { useEffect, useRef, useState, type FormEvent } from 'react'
import { run, type RunResult } from '../api'
import { useDesk, type SheetRequest } from '../ctx'
import { label } from '../format'
import type { Command, Dict, Field } from '../types'
import { Md, Result, Value } from './Result'

function Input({ f, id, value, onChange }: { f: Field; id: string; value: string; onChange: (v: string) => void }) {
  const common = { id, name: f.name, required: f.required, value }
  if (f.kind === 'bool' || f.kind === 'choice') {
    const opts = f.kind === 'bool' ? ['true', 'false'] : (f.choices ?? []).map(String)
    return (
      <select {...common} onChange={(e) => onChange(e.target.value)}>
        {(f.kind === 'bool' || !f.required) && <option value="">default ({String(f.default ?? 'none')})</option>}
        {opts.map((o) => <option key={o} value={o}>{o}</option>)}
      </select>
    )
  }
  if (f.kind === 'json') {
    return <textarea {...common} rows={4} onChange={(e) => onChange(e.target.value)}
      placeholder={f.type.startsWith('dict') ? 'JSON, or KEY=VALUE lines' : 'JSON'} />
  }
  const number = f.kind === 'int' || f.kind === 'float'
  const hint = f.kind === 'list' ? 'values separated by commas or spaces'
    : !f.required && f.default !== null ? `default: ${f.default}` : undefined
  return <input {...common} type={number ? 'number' : 'text'} step={f.kind === 'float' ? 'any' : undefined}
    inputMode={number ? 'decimal' : undefined} placeholder={hint} onChange={(e) => onChange(e.target.value)} />
}

interface Props {
  cmd: Command
  title?: string
  prefill?: Dict<string | number>
  compact?: boolean           // a sheet: optional fields folded, the one-line help instead of the full doc
  onDone?: () => void
}

export function CommandForm({ cmd, title, prefill = {}, compact = false, onDone }: Props) {
  const [values, setValues] = useState<Dict<string>>(() =>
    Object.fromEntries(Object.entries(prefill).map(([k, v]) => [k, String(v)])))
  const [confirm, setConfirm] = useState<Dict<string> | null>(null)
  const [running, setRunning] = useState(false)
  const [results, setResults] = useState<{ id: number; res: RunResult | { error: string } }[]>([])
  const uid = useRef(Math.random().toString(36).slice(2, 7)).current
  const main = cmd.fields.filter((f) => f.required || f.name in prefill)
  const rest = cmd.fields.filter((f) => !main.includes(f))

  const send = async (args: Dict<string>) => {
    setConfirm(null)
    setRunning(true)
    let res: RunResult | { error: string }
    try {
      res = await run(cmd.key, args)
      const r = res.result as Dict | null
      if (!(r && typeof r === 'object' && r.ok === false)) onDone?.()
    } catch (err) {
      res = { error: (err as Error).message }
    }
    setResults((prev) => [{ id: Date.now(), res }, ...prev])
    setRunning(false)
  }
  const submit = (e: FormEvent) => {
    e.preventDefault()
    const args = Object.fromEntries(Object.entries(values).filter(([, v]) => v !== ''))
    if (cmd.write) setConfirm(args)
    else void send(args)
  }
  const field = (f: Field) => {
    const note = (compact ? [f.help] : [f.name, f.type, f.help]).filter(Boolean).join(' · ')
    return (
      <div className="field" key={f.name}>
        <label htmlFor={`f-${uid}-${f.name}`}>{label(f.name)}{f.required ? ' *' : ''}</label>
        <Input f={f} id={`f-${uid}-${f.name}`} value={values[f.name] ?? ''}
          onChange={(v) => setValues((prev) => ({ ...prev, [f.name]: v }))} />
        {note && <small className="muted">{note}</small>}
      </div>
    )
  }

  return (
    <div className="cmd-box">
      <div className="result-head">
        <h2>{title ?? cmd.key}</h2>
        {cmd.write && <span className="pill bad">write</span>}
        {cmd.weight && !compact && <span className="pill">{cmd.weight}</span>}
      </div>
      {compact ? cmd.help && <p className="muted">{cmd.help}</p> : cmd.doc && <Md text={cmd.doc} className="md doc" />}
      <form className="fields" onSubmit={submit}>
        {main.map(field)}
        {rest.length > 0 && (
          <details className="optional" open={!compact}>
            <summary>Optional ({rest.length})</summary>
            <div className="fields">{rest.map(field)}</div>
          </details>
        )}
        {confirm ? (
          <div className="confirm">
            <strong>Run {cmd.key}? It changes the desk.</strong>
            <Value v={confirm} />
            <div className="confirm-row">
              <button type="button" className="danger" autoFocus onClick={() => void send(confirm)}>Run {cmd.key}</button>
              <button type="button" className="ghost" onClick={() => setConfirm(null)}>Cancel</button>
            </div>
          </div>
        ) : (
          <button type="submit" className={cmd.write ? 'danger' : 'primary'} disabled={running}>
            {running ? `running ${cmd.key}…` : cmd.write ? `Review ${cmd.key}` : `Run ${cmd.key}`}
          </button>
        )}
      </form>
      <div className="cmd-out">
        {results.map((r) => <Result key={r.id} title={cmd.key} res={r.res} write={cmd.write} />)}
      </div>
    </div>
  )
}

// A command opened from a card: centered on a desktop, rising from the bottom on a phone.
export function Sheet({ req, onClose }: { req: SheetRequest | null; onClose: () => void }) {
  const { byKey } = useDesk()
  const ref = useRef<HTMLDialogElement>(null)
  const cmd = req ? byKey[req.cmd] : undefined
  useEffect(() => {
    const d = ref.current
    if (!d) return
    if (cmd && !d.open) d.showModal()
    if (!cmd && d.open) d.close()
  }, [cmd])
  return (
    <dialog ref={ref} className="sheet" aria-label="Run a command" onClose={onClose}
      onClick={(e) => { if (e.target === ref.current) onClose() }}>
      <button className="icon-btn sheet-close" type="button" aria-label="Close" onClick={onClose}>
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" /></svg>
      </button>
      {req && cmd && <CommandForm key={req.cmd + JSON.stringify(req.prefill ?? {})} cmd={cmd} title={req.title}
        prefill={req.prefill} compact onDone={req.onDone} />}
    </dialog>
  )
}
