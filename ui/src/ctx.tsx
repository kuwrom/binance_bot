// What every view can reach: the view mode, the command registry, and the three ways to go somewhere
// (a tab, a coin's page, a command sheet).
import { createContext, useContext } from 'react'
import type { Command, Dict, Mode } from './types'

export type Tab = 'home' | 'radar' | 'trades' | 'alerts' | 'commands' | 'docs'
export const TABS: { id: Tab; label: string; advanced?: boolean }[] = [
  { id: 'home', label: 'Overview' }, { id: 'radar', label: 'Radar' }, { id: 'trades', label: 'Results' },
  { id: 'alerts', label: 'Alerts' }, { id: 'commands', label: 'Commands', advanced: true },
  { id: 'docs', label: 'Docs', advanced: true },
]

export interface SheetRequest {
  cmd: string                        // the command's key
  title?: string
  prefill?: Dict<string | number>
  onDone?: () => void                // after a run that was not refused
}

export interface Desk {
  mode: Mode
  commands: Command[]
  byKey: Dict<Command>
  limits?: Dict<number>              // the effective risk limits, from the last account read
  openTab: (tab: Tab) => void
  openCoin: (symbol: string) => void
  openSheet: (req: SheetRequest) => void
}

export const DeskCtx = createContext<Desk>(null as unknown as Desk)
export const useDesk = () => useContext(DeskCtx)
