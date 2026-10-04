// The desk console's shell: login, the tabs, Basic / Advanced, the menu (theme, auto-refresh, halt, log out).
import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react'
import { api, setUnauthorized, store } from './api'
import { Sheet } from './components/CommandForm'
import { DeskCtx, TABS, type Desk, type SheetRequest, type Tab } from './ctx'
import { span } from './format'
import type { Command, Me, Mode, Scan, TradingState } from './types'
import { Alerts } from './views/Alerts'
import { Commands, Docs } from './views/Console'
import { Overview, useDeskData } from './views/Overview'
import { Radar, SearchBox } from './views/Radar'
import { Trades } from './views/Trades'

const ICONS: Record<Tab, string> = {
  home: 'M3 12h4l3-8 4 16 3-8h4', radar: 'M12 12l6-6M12 3a9 9 0 109 9M12 7a5 5 0 105 5',
  trades: 'M4 19V9m5 10V5m5 14v-7m5 7V8', alerts: 'M6 9a6 6 0 0112 0c0 6 2 7 2 7H4s2-1 2-7M10 20a2 2 0 004 0',
  commands: 'M5 8l4 4-4 4M12 16h7', docs: 'M5 4h10a3 3 0 013 3v13H8a3 3 0 01-3-3zM5 17a3 3 0 013-3h10',
}
const OLD_TABS: Record<string, Tab> = { overview: 'home', console: 'commands', activity: 'trades' }   // stored before the redesign
const Icon = ({ d }: { d: string }) => <svg viewBox="0 0 24 24" aria-hidden="true"><path d={d} /></svg>

function Login({ onDone }: { onDone: () => void }) {
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setError('')
    try {
      await api('/api/login', { password })
      setPassword('')
      onDone()
    } catch (err) { setError((err as Error).message) }
  }
  return (
    <section className="login">
      <form className="card login-card" onSubmit={submit}>
        <h1>Shortbot desk</h1>
        <label htmlFor="password">Password</label>
        <input id="password" type="password" autoComplete="current-password" required autoFocus value={password}
          onChange={(e) => setPassword(e.target.value)} />
        <button type="submit" className="primary">Log in</button>
        <p className="error" role="alert">{error}</p>
      </form>
    </section>
  )
}

function Seg<T extends string>({ label, value, options, set }: { label: string; value: T; options: [T, string][]; set: (v: T) => void }) {
  return (
    <div className="seg" role="group" aria-label={label}>
      {options.map(([v, name]) => <button key={v} type="button" aria-pressed={value === v} onClick={() => set(v)}>{name}</button>)}
    </div>
  )
}

// failed: the last read of trading_state failed. Without a state to show that is "unknown", in the alarm colour.
function Status({ ks, failed, onClick }: { ks?: TradingState; failed: boolean; onClick: () => void }) {
  const ok = !!ks && !failed && ks.state === 'ACTIVE' && ks.guardian_alive
  const cls = !ks ? (failed ? 'bad' : '') : ok ? 'ok' : !failed && ks.state === 'REDUCING' && ks.guardian_alive ? 'warn' : 'bad'
  return (
    <button className={'status ' + cls} type="button" onClick={onClick}
      title={ks ? `Kill switch ${ks.state}; guardian heartbeat ${span(ks.guardian_heartbeat_age_s)} ago` : ''}>
      <span className="beacon" />
      <span>{!ks ? (failed ? 'State unknown' : '…') : failed ? `${ks.state} · not refreshed` : ok ? `Active · guardian ${span(ks.guardian_heartbeat_age_s)}` : ks.state !== 'ACTIVE' ? ks.state : 'Guardian down'}</span>
    </button>
  )
}

const MODES: [Mode, string][] = [['basic', 'Basic'], ['advanced', 'Advanced']]

function Shell({ me, commands, onLogout }: { me: Me; commands: Command[]; onLogout: () => void }) {
  const [mode, setModeState] = useState<Mode>(store.get('sb-mode', 'basic') === 'advanced' ? 'advanced' : 'basic')
  const [tab, setTab] = useState<Tab>(() => { const t = store.get('sb-tab', 'home'); return OLD_TABS[t] ?? (t as Tab) })
  const [theme, setThemeState] = useState(store.get('sb-theme', 'auto'))
  const [auto, setAuto] = useState(store.get('sb-auto', '1') !== '0')
  const [menu, setMenu] = useState(false)
  const [sheet, setSheet] = useState<SheetRequest | null>(null)
  const [coinSym, setCoinSym] = useState<string | null>(null)
  const [scan, setScan] = useState<Scan | null>(null)

  const visible = TABS.filter((t) => mode === 'advanced' || !t.advanced)
  const active = visible.some((t) => t.id === tab) ? tab : 'home'
  const openTab = useCallback((t: Tab) => { setTab(t); store.set('sb-tab', t); window.scrollTo(0, 0) }, [])
  const openCoin = useCallback((s: string) => { setCoinSym(s || null); if (s) openTab('radar') }, [openTab])
  const poll = useDeskData(mode, active === 'home', auto, !!sheet)
  const limits = poll.data.account?.limits
  const desk = useMemo<Desk>(() => ({ mode, commands, byKey: Object.fromEntries(commands.map((c) => [c.key, c])),
    limits, openTab, openCoin, openSheet: setSheet }), [mode, commands, limits, openTab, openCoin])

  useEffect(() => { document.documentElement.dataset.mode = mode; store.set('sb-mode', mode) }, [mode])
  useEffect(() => {
    if (theme === 'light' || theme === 'dark') document.documentElement.dataset.theme = theme
    else delete document.documentElement.dataset.theme
    store.set('sb-theme', theme)
  }, [theme])
  useEffect(() => { store.set('sb-auto', auto ? '1' : '0') }, [auto])
  useEffect(() => {
    if (!menu) return
    const close = (e: Event) => {
      if (e instanceof KeyboardEvent ? e.key === 'Escape' : !(e.target as Element).closest('#menu, #menu-btn')) setMenu(false)
    }
    document.addEventListener('click', close)
    document.addEventListener('keydown', close)
    return () => { document.removeEventListener('click', close); document.removeEventListener('keydown', close) }
  }, [menu])

  const ks = poll.data.trading_state
  const urgent = (poll.data.alerts?.alerts ?? []).filter((x) => x.urgent && !x.acked).length
  useEffect(() => { api<Scan>('/api/research/candidates').then(setScan, () => {}) }, [])   // the search box's suggestions

  return (
    <DeskCtx.Provider value={desk}>
      <div id="shell">
        <header className="top">
          <div className="brand"><Icon d="M3 6l6 6 4-4 8 9M21 12v5h-5" /><span>shortbot</span>
            <span className={'pill ' + (me.mode === 'live' ? 'bad' : 'demo')}>{me.mode}</span></div>
          <Status ks={ks} failed={'trading_state' in poll.errors} onClick={() => openTab('home')} />
          <nav id="tabs" aria-label="Sections">
            {visible.map((t) => (
              <button key={t.id} type="button" className={'tab' + (active === t.id ? ' active' : '')}
                aria-current={active === t.id ? 'page' : undefined} onClick={() => openTab(t.id)}>
                <Icon d={ICONS[t.id]} /><span>{t.label}</span>
                {t.id === 'alerts' && urgent > 0 && <span className="badge">{urgent > 99 ? '99+' : urgent}</span>}
              </button>
            ))}
          </nav>
          <div className="wide-only"><SearchBox symbols={scan?.symbols ?? []} compact /></div>
          <div className="wide-only"><Seg label="View" value={mode} options={MODES} set={setModeState} /></div>
          <button id="menu-btn" className="icon-btn" type="button" aria-label="Menu" aria-haspopup="true" aria-expanded={menu}
            onClick={() => setMenu((m) => !m)}><Icon d="M4 7h16M4 12h16M4 17h16" /></button>
          {menu && (
            <div id="menu" className="menu">
              <div className="menu-row"><span className="menu-label">View</span><Seg label="View" value={mode} options={MODES} set={setModeState} /></div>
              <div className="menu-row"><span className="menu-label">Theme</span>
                <Seg label="Theme" value={theme} options={[['auto', 'Auto'], ['light', 'Light'], ['dark', 'Dark']]} set={setThemeState} /></div>
              <label className="menu-row check"><span className="menu-label">Refresh every 60 s</span>
                <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} /></label>
              <p className="menu-about">experiment {me.experiment} · code {me.code}</p>
              <button className="danger" type="button" onClick={() => { setMenu(false); setSheet({ cmd: 'halt_trading', title: 'Halt trading',
                onDone: () => void poll.load() }) }}>Halt trading…</button>
              <button className="ghost" type="button" onClick={() => { setMenu(false); onLogout() }}>Log out</button>
            </div>
          )}
        </header>
        <main>
          {active === 'home' && <Overview desk={poll} />}
          {active === 'radar' && <Radar symbol={coinSym} scan={scan} setScan={setScan} />}
          {active === 'trades' && <Trades journal={poll.data.journal} />}
          {active === 'alerts' && <Alerts />}
          {active === 'commands' && <Commands />}
          {active === 'docs' && <Docs />}
        </main>
      </div>
      <Sheet req={sheet} onClose={() => setSheet(null)} />
    </DeskCtx.Provider>
  )
}

export function App() {
  const [session, setSession] = useState<{ me: Me; commands: Command[] } | null>(null)
  const [checked, setChecked] = useState(false)
  const start = useCallback(async () => {
    try {
      const me = await api<Me>('/api/me')
      setSession({ me, commands: await api<Command[]>('/api/commands') })
    } catch { setSession(null) }
    setChecked(true)
  }, [])
  useEffect(() => { setUnauthorized(() => setSession(null)); void start() }, [start])
  if (!checked) return null
  if (!session) return <Login onDone={() => void start()} />
  const logout = () => { void api('/api/logout', {}).catch(() => {}).then(() => setSession(null)) }
  return <Shell me={session.me} commands={session.commands} onLogout={logout} />
}
