// The shapes the console reads. Commands answer with what their MCP tool returns; /api/research/* with web/research.py.

export type Mode = 'basic' | 'advanced'
export type Json = unknown
export type Dict<T = Json> = Record<string, T>

export interface Me { mode: string; experiment: string; code: string }

export interface Field {
  name: string; kind: 'str' | 'int' | 'float' | 'bool' | 'choice' | 'list' | 'json'; required: boolean
  default: Json; choices: Json[] | null; help: string | null; type: string
}
export interface Command {
  key: string; group: string; write: boolean; weight: string | null; help: string; doc: string; fields: Field[]
}

export interface TradingState {
  state: string; by: string | null; reason: string | null; at: string | null; allowed: string | null
  clear: string | null; guardian_heartbeat_age_s: number | null; guardian_alive: boolean
}
export interface Account {
  mode: string; equity: number; available: number; unrealized: number; day_pnl: number; week_pnl: number
  degraded: boolean; equity_peak: number; limits: Dict<number>; experiment: string
}
export interface Target { price: number; fraction: number }
export interface Plan {
  trigger?: string; invalidation?: string; targets?: Target[]; time_stop_hours?: number | null
  kill_conditions?: string[]; auto?: Dict<boolean>
}
export interface Position {
  symbol: string; contracts: number; entry: number; mark: number; notional: number; unrealized_pnl: number
  pnl_pct: number | null; leverage: number | null; margin_mode: string | null; liquidation: number | null
  exchange_stop: number | null; stop_distance_pct: number | null; UNPROTECTED: boolean | string
  trade_id: number | null; setup: string | null; thesis: string | null; plan: Plan | null
  opened_at: string | null; mae_pct: number | null; mfe_pct: number | null; targets_hit: number | null
  journal_status: string; error?: string
}
export interface Entry {
  id: number; group_id: string; symbol: string; status: string; expires_ms: number | null; limit_price: number
  stop_price: number; amount: number; setup: string | null; level_reason: string | null; hint?: string
}
export interface Governor {
  halt: boolean; until: string | null; m_gov: number; drawdown_pct: number; reasons: string[]
  daily: { cap_usd: number; room_usd: number }; regime: string | null; cell: string | null
  streak: { current: number } | null; open_risk_usd: number
}
export interface RiskParams {
  overrides_active: boolean; experiment: string; effective: Dict<number>; baseline_env: Dict<number>
  overrides: Dict<number>; bounds: Dict<[number, number]>; note: string
}
export interface Trade {
  id: number; symbol: string; status: string; opened_at: string | null; closed_at: string | null
  entry_price: number | null; exit_price: number | null; notional: number | null; leverage: number | null
  setup: string | null; thesis: string | null; pnl: number | null; exit_reason: string | null; lesson: string | null
  experiment: string | null; fees: number | null; funding: number | null; mae_pct: number | null
  mfe_pct: number | null; entry_features?: Json
}
export interface SetupStats { n: number; wins: number; pnl: number; win_rate: number; avg_r: number | null }
export interface Learning { id: number; status: string; tag: string | null; setup: string | null; text: string }
export interface Journal {
  stats: { closed_trades: number; total_pnl: number; by_setup: Dict<SetupStats> }
  recent_trades: Trade[]; learnings: Learning[]; learnings_more: number
}
export interface Alert { id: number; ts: string; symbol: string | null; kind: string; message: string; urgent: boolean; acked: boolean }
export interface Alerts { ack_id: number; alerts: Alert[] }

// ---- research (web/research.py)
export interface Candidate {
  id: number; symbol: string; ts: string; age_s: number; price: number; score: number; tradable: boolean
  regime: string | null; setups: string[]; vetoes: string[]; signals: string[]; tape: string | null
  p_target: number | null; stop: number | null; target: number | null
  outcome: Dict<number | null> | null; features: Dict<number | string>
}
export interface Watch { symbol: string; added_ts: string; reason: string | null; until_ms: number }
export interface Scan {
  hours: number; as_of: string | null; scored: number; n_tradable: number; n_vetoed: number
  tradable: Candidate[]; vetoed: Candidate[]; watchlist: Watch[]; symbols: string[]
}
export interface CoinView {
  symbol: string; standing: { state: string; why: string }
  trade: Dict | null; entries: Dict[]; watch: Watch | null; latest: Candidate | null
  market: { ts: string; price: number | null; funding_rate: number | null; quote_volume_24h: number | null } | null
  history: { ts: string; score: number; tradable: boolean; price: number; vetoes: number }[]
  outcomes: { rows: number; avg_short_ret_24h: number; hit_rate: number } | null
  read: { ts: string; read: string; bias: string; levels: Json; invalidation: number | null; expires_ms: number } | null
  trades: { id: number; setup: string | null; pnl: number | null; closed_at: string | null; exit_reason: string | null }[]
}
export interface LadderLevel {
  target: number; p0: number; p_model: number; p_stop_model?: number; reward_R?: number; ev_R?: number
}
export interface LadderView {
  trade_id: number; ts: string; price: number; score: number; parts: Dict<number>; levels: LadderLevel[]
  x_star: number; x_actual: number; action: string; entry: number; init_r: number; live: boolean
  conviction: string | null
}
export interface EntryOdds { p_fill?: number; fill_hours?: number; p_target?: number; p_analyst?: number | null }
export interface Book { ladders: Dict<LadderView>; entries: Dict<EntryOdds> }

export interface Edge {
  n: number; days: number; p_target_observed: number; ci95: [number, number] | null; p_baseline: number
  edge_pp: number; edge_significant: boolean; avg_R_ex_time: number | null
}
export interface Calibration {
  labelled_brackets: number; days: number; inference: string
  model_calibration?: { n: number; brier: number }
  expectancy_by_setup: Dict<Edge>; expectancy_by_tape_state: Dict<Edge>; how_to_read: string
}

// read_coin: the live read of one coin
export interface StopOption { price: number; distance_pct: number; method: string; allowed: boolean; max_leverage: number | null }
export interface CoinRead {
  symbol: string; last: number; score: number; stage: string; game: string; controller: string[]
  verdict: { action: string; playbook: string | null; size_mult: number | null; notes: string[]; why: string[] }
  setups_detected: Json[]; stops: Dict<StopOption | string>
  evidence?: { hit_rate_pct?: Dict<number>; avg_short_ret_24h?: Dict<number>; n?: Dict<number> }
  registry?: { nodes: { id: string; status: string; verdict: string }[] }
}
