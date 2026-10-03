// The desk console: login, overview, alerts, every command as a form, journal presets, docs.
"use strict";

const $ = (s) => document.querySelector(s);
const state = { commands: [], byKey: {}, timer: null, current: null };

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

// ------------------------------------------------------------------ login
function showLogin() {
  $("#shell").hidden = true;
  $("#login").hidden = false;
  clearInterval(state.timer);
  $("#password").focus();
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#login-error").textContent = "";
  try {
    await api("/api/login", { body: { password: $("#password").value } });
    $("#password").value = "";
    start();
  } catch (err) { $("#login-error").textContent = err.message; }
});

$("#logout").addEventListener("click", async () => { await api("/api/logout", { body: {} }).catch(() => {}); showLogin(); });

// ------------------------------------------------------------------ tabs
document.querySelectorAll("#tabs .tab").forEach((b) => b.addEventListener("click", () => openTab(b.dataset.tab)));

function openTab(name) {
  document.querySelectorAll("#tabs .tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab-page").forEach((p) => { p.hidden = p.id !== `tab-${name}`; });
  try { localStorage.setItem("sb-tab", name); } catch (_) { /* storage blocked */ }
  if (name === "alerts") loadAlerts();
  if (name === "docs" && !$("#doc-list").childElementCount) loadDocs();
}

// ------------------------------------------------------------------ overview
function tile(label, value, cls) {
  const t = R.el("div", "tile" + (cls ? " " + cls : ""));
  t.append(R.el("div", "tile-label", label), R.el("div", "tile-value", value));
  return t;
}

const money = (v) => (typeof v === "number" ? v.toLocaleString(undefined, { maximumFractionDigits: 2 }) : "—");
const sign = (v) => (typeof v !== "number" ? "" : v > 0 ? "pos" : v < 0 ? "neg" : "");

async function settle(key, args) {
  try { return { ok: true, res: await run(key, args) }; } catch (err) { return { ok: false, err: err.message }; }
}

async function loadOverview() {
  $("#refresh").disabled = true;
  const keys = ["trading_state", "account", "positions", "pending_entries", "governor_status", "risk_params"];
  const [brief, ...rest] = await Promise.all([settle("brief"), ...keys.map((k) => settle(k))]);
  const got = Object.fromEntries(keys.map((k, i) => [k, rest[i]]));
  const val = (k) => (got[k].ok ? got[k].res.result : null);

  const ks = val("trading_state") || {};
  const acct = val("account") || {};
  const pos = val("positions");
  const pend = val("pending_entries");
  const tiles = $("#ov-state");
  tiles.replaceChildren(
    tile("Kill switch", ks.state || "?", ks.state === "ACTIVE" ? "ok" : "bad"),
    tile("Guardian", ks.guardian_alive ? `alive · ${ks.guardian_heartbeat_age_s}s` : "NOT ALIVE",
      ks.guardian_alive ? "ok" : "bad"),
    tile("Equity", money(acct.equity)),
    tile("Day P&L", money(acct.day_pnl), sign(acct.day_pnl)),
    tile("Week P&L", money(acct.week_pnl), sign(acct.week_pnl)),
    tile("Positions", Array.isArray(pos) ? String(pos.length) : "?"),
    tile("Pending entries", Array.isArray(pend) ? String(pend.length) : "?"),
  );

  const b = $("#ov-brief");
  b.replaceChildren();
  if (brief.ok) {
    const d = R.el("div", "md");
    d.innerHTML = MD.render(String(brief.res.result));
    b.append(d);
  } else b.append(R.el("p", "error", "brief: " + brief.err));

  const cards = $("#ov-cards");
  cards.replaceChildren();
  const titles = { positions: "Open shorts", pending_entries: "Pending entries", governor_status: "Governor",
    risk_params: "Risk parameters" };
  for (const k of Object.keys(titles)) {
    const card = got[k].ok ? R.result(titles[k], got[k].res) : R.result(titles[k], { error: got[k].err });
    if (k === "risk_params") {
      const btn = R.el("button", "ghost", "Change risk parameters");
      btn.addEventListener("click", () => { openTab("console"); selectCommand("set_risk_params"); });
      card.querySelector(".result-head").append(btn);
    }
    cards.append(card);
  }
  $("#updated").textContent = "updated " + new Date().toLocaleTimeString();
  $("#refresh").disabled = false;
}

$("#refresh").addEventListener("click", loadOverview);
$("#auto").addEventListener("change", schedule);

function schedule() {
  clearInterval(state.timer);
  if ($("#auto").checked) state.timer = setInterval(() => { if (!$("#tab-overview").hidden) loadOverview(); }, 60000);
}

// ------------------------------------------------------------------ alerts
async function loadAlerts() {
  const box = $("#alerts-list");
  box.replaceChildren(R.el("p", "muted", "loading…"));
  try {
    const data = await api("/api/alerts?limit=300");
    let rows = data.alerts;
    if ($("#alerts-urgent").checked) rows = rows.filter((a) => a.urgent);
    if ($("#alerts-new").checked) rows = rows.filter((a) => !a.acked);
    $("#alerts-count").textContent = `${rows.length} shown · acknowledged up to #${data.ack_id}`;
    const list = R.el("div", "alerts");
    for (const a of rows) {
      const row = R.el("div", "alert" + (a.urgent ? " urgent" : "") + (a.acked ? " acked" : ""));
      row.append(R.el("span", "muted", `#${a.id} ${a.ts.slice(5, 16).replace("T", " ")}`),
        R.el("span", "pill" + (a.urgent ? " bad" : ""), a.kind), R.el("span", "sym", a.symbol || ""),
        R.el("span", "msg", a.message));
      list.append(row);
    }
    box.replaceChildren(list);
  } catch (err) { box.replaceChildren(R.el("p", "error", err.message)); }
}

["#alerts-refresh", "#alerts-urgent", "#alerts-new"].forEach((s) =>
  $(s).addEventListener(s === "#alerts-refresh" ? "click" : "change", loadAlerts));

// ------------------------------------------------------------------ commands
function renderCommandList() {
  const q = $("#cmd-search").value.trim().toLowerCase();
  const groups = {};
  for (const c of state.commands) {
    if (q && !(c.key.toLowerCase().includes(q) || c.help.toLowerCase().includes(q))) continue;
    (groups[c.group] = groups[c.group] || []).push(c);
  }
  const box = $("#cmd-groups");
  box.replaceChildren();
  for (const [g, cmds] of Object.entries(groups)) {
    box.append(R.el("h4", null, g));
    for (const c of cmds) {
      const b = R.el("button", "cmd" + (c.write ? " write" : "") + (state.current === c.key ? " active" : ""));
      b.append(R.el("span", null, c.key));
      if (c.write) b.append(R.el("span", "pill bad", "write"));
      b.title = c.help;
      b.addEventListener("click", () => selectCommand(c.key));
      box.append(b);
    }
  }
}

$("#cmd-search").addEventListener("input", renderCommandList);

function input(f) {
  const id = `f-${f.name}`;
  let e;
  if (f.kind === "bool") {
    e = R.el("select");
    for (const [v, t] of [["", `default (${f.default})`], ["true", "true"], ["false", "false"]]) {
      const o = R.el("option", null, t); o.value = v; e.append(o);
    }
  } else if (f.kind === "choice") {
    e = R.el("select");
    if (!f.required) { const o = R.el("option", null, `default (${f.default ?? "none"})`); o.value = ""; e.append(o); }
    for (const c of f.choices) { const o = R.el("option", null, String(c)); o.value = String(c); e.append(o); }
  } else if (f.kind === "json") {
    e = R.el("textarea");
    e.rows = 4;
    e.placeholder = f.type.startsWith("dict") ? 'JSON, or KEY=VALUE lines' : "JSON";
  } else {
    e = R.el("input");
    e.type = f.kind === "int" || f.kind === "float" ? "number" : "text";
    if (f.kind === "float") e.step = "any";
    if (f.kind === "list") e.placeholder = "values separated by commas or spaces";
    if (!f.required && f.default !== null && f.kind !== "list") e.placeholder = `default: ${f.default}`;
  }
  e.id = id;
  e.name = f.name;
  return e;
}

function selectCommand(key) {
  const c = state.byKey[key];
  if (!c) return;
  state.current = key;
  renderCommandList();
  const box = $("#cmd-form");
  box.replaceChildren();
  const head = R.el("div", "result-head");
  head.append(R.el("h2", null, c.key));
  if (c.write) head.append(R.el("span", "pill bad", "write: changes the desk"));
  if (c.weight) head.append(R.el("span", "pill", c.weight));
  box.append(head);
  if (c.doc) { const d = R.el("div", "md doc"); d.innerHTML = MD.render(c.doc); box.append(d); }
  const form = R.el("form", "fields");
  for (const f of c.fields) {
    const row = R.el("div", "field");
    const lab = R.el("label", null, f.name + (f.required ? " *" : ""));
    lab.htmlFor = `f-${f.name}`;
    row.append(lab, input(f));
    row.append(R.el("small", "muted", [f.type, f.help].filter(Boolean).join(" · ")));
    form.append(row);
  }
  const go = R.el("button", c.write ? "danger" : "primary", c.write ? `Run ${c.key} (write)` : `Run ${c.key}`);
  go.type = "submit";
  form.append(go);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const args = {};
    for (const f of c.fields) {
      const v = form.elements[f.name].value;
      if (v !== "") args[f.name] = v;
    }
    if (c.write && !confirm(`Run ${c.key}? It changes the desk.\n\n${JSON.stringify(args, null, 2)}`)) return;
    go.disabled = true;
    const out = $("#cmd-result");
    out.prepend(R.el("p", "muted running", `running ${c.key}…`));
    try {
      const res = await run(c.key, args);
      out.querySelector(".running")?.remove();
      out.prepend(R.result(c.key, res, { write: c.write }));
    } catch (err) {
      out.querySelector(".running")?.remove();
      out.prepend(R.result(c.key, { error: err.message }));
    } finally { go.disabled = false; }
  });
  box.append(form);
}

// ------------------------------------------------------------------ journal presets
document.querySelectorAll("[data-preset]").forEach((b) => b.addEventListener("click", async () => {
  const key = b.dataset.preset;
  const args = key === "audit" ? { since: "24h" } : key === "journal" ? { limit: "50" } : {};
  const out = $("#activity-result");
  out.replaceChildren(R.el("p", "muted", `running ${key}…`));
  try { out.replaceChildren(R.result(key, await run(key, args))); }
  catch (err) { out.replaceChildren(R.result(key, { error: err.message })); }
}));

// ------------------------------------------------------------------ docs
async function loadDocs() {
  const docs = await api("/api/docs");
  const render = () => {
    const q = $("#doc-search").value.trim().toLowerCase();
    const ul = $("#doc-list");
    ul.replaceChildren();
    for (const d of docs.filter((x) => x.toLowerCase().includes(q))) {
      const li = R.el("li");
      const a = R.el("button", "link", d);
      a.addEventListener("click", () => openDoc(d));
      li.append(a);
      ul.append(li);
    }
  };
  $("#doc-search").oninput = render;
  render();
}

async function openDoc(path) {
  const view = $("#doc-view");
  try {
    const text = await api("/api/doc?path=" + encodeURIComponent(path));
    view.innerHTML = MD.render(text);
    view.prepend(R.el("div", "muted", path));
    view.scrollTop = 0;
  } catch (err) { view.replaceChildren(R.el("p", "error", err.message)); }
}

// ------------------------------------------------------------------ start
async function start() {
  const me = await api("/api/me");
  $("#login").hidden = true;
  $("#shell").hidden = false;
  $("#mode").textContent = me.mode;
  $("#mode").className = "pill " + (me.mode === "live" ? "bad" : "ok");
  $("#experiment").textContent = `experiment: ${me.experiment} · code ${me.code}`;
  state.commands = await api("/api/commands");
  state.byKey = Object.fromEntries(state.commands.map((c) => [c.key, c]));
  renderCommandList();
  let tab = "overview";
  try { tab = localStorage.getItem("sb-tab") || tab; } catch (_) { /* storage blocked */ }
  openTab(tab);
  loadOverview();
  schedule();
}

start().catch(() => showLogin());
