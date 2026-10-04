// Shared by every view: the API client, the session's state, and how the desk writes money, prices and times.
"use strict";

const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);
const state = { commands: [], byKey: {}, timer: null, current: null, mode: "basic", alerts: null };

const store = {
  get(k, d) { try { return localStorage.getItem(k) ?? d; } catch (_) { return d; } },   // storage may be blocked
  set(k, v) { try { localStorage.setItem(k, v); } catch (_) { /* storage blocked */ } },
};

async function api(path, opts = {}) {
  const init = { credentials: "same-origin", headers: { "X-Shortbot": "1" }, ...opts };
  if (opts.body !== undefined) {
    init.method = "POST";
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }
  const res = await fetch(path, init);
  if (res.status === 401 && path !== "/api/login") { showLogin(); throw new Error("log in first"); }
  const type = res.headers.get("content-type") || "";
  const data = type.includes("json") ? await res.json() : await res.text();
  if (!res.ok) throw new Error((data && data.error) || `${res.status} ${res.statusText}`);
  return data;
}

const run = (key, args = {}) => api(`/api/run/${encodeURIComponent(key)}`, { body: { args } });

async function settle(key, args) {
  try { return { ok: true, res: await run(key, args) }; } catch (err) { return { ok: false, err: err.message }; }
}

const F = (() => {
  const isNum = (v) => typeof v === "number" && Number.isFinite(v);
  const MINUS = "−";
  const signOf = (v, sign) => (v < 0 ? MINUS : sign && v > 0 ? "+" : "");

  // $5,129.24 · +$24.58 · −$5.89 (sign: show "+" too)
  function usd(v, sign = false, digits = 2) {
    if (!isNum(v)) return "—";
    const n = Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
    return `${Number(Math.abs(v).toFixed(digits)) === 0 ? "" : signOf(v, sign)}$${n}`;   // never "+$0.00"
  }
  // 12.9K · 5M: caps and volumes
  function compact(v) {
    if (!isNum(v)) return "—";
    return Math.abs(v) >= 10000 ? v.toLocaleString("en-US", { notation: "compact", maximumFractionDigits: 1 }) :
      v.toLocaleString("en-US", { maximumFractionDigits: 2 });
  }
  // A coin price at 5 significant digits: 0.03295 · 2.203 · 64,210.5
  function price(v) {
    if (!isNum(v)) return "—";
    return v >= 1000 ? v.toLocaleString("en-US", { maximumFractionDigits: 1 }) : String(Number(v.toPrecision(5)));
  }
  function pct(v, sign = false, digits = 2) {
    if (!isNum(v)) return "—";
    return `${signOf(v, sign)}${Math.abs(v).toFixed(digits)}%`;
  }
  const tone = (v) => (!isNum(v) || Math.abs(v) < 0.005 ? "" : v > 0 ? "pos" : "neg");   // a rounding-size value is flat
  const coin = (symbol) => String(symbol || "").split("/")[0];

  // "3h 40m", "2d 4h", "45s": a span in seconds
  function span(s) {
    if (!isNum(s)) return "—";
    s = Math.max(0, Math.round(s));
    if (s < 90) return `${s}s`;
    const m = Math.round(s / 60);
    if (m < 90) return `${m}m`;
    const h = Math.floor(m / 60);
    if (h < 48) return `${h}h ${m % 60}m`;
    return `${Math.floor(h / 24)}d ${h % 24}h`;
  }
  const since = (ts) => (ts ? span((Date.now() - new Date(ts).getTime()) / 1000) : "—");
  // "Oct 2, 14:31" in the browser's zone
  function when(ts) {
    if (!ts) return "—";
    const d = new Date(typeof ts === "string" && !/[zZ]|[+-]\d\d:?\d\d$/.test(ts) ? ts + "Z" : ts);
    return d.toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
      hour12: false });
  }
  // max_total_notional -> "Max total notional", risk_per_trade_pct -> "Risk per trade %"
  const label = (key) => {
    const s = String(key).replace(/_pct$/, " %").replace(/_usd$/, " $").replace(/_/g, " ");
    return s.charAt(0).toUpperCase() + s.slice(1);
  };

  return { isNum, usd, compact, price, pct, tone, coin, span, since, when, label };
})();
