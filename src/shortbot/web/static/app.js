// The desk console's shell: login, the tabs, Basic / Advanced, the menu (theme, auto-refresh, halt, log out).
// Views live in home.js (Overview), views.js (Alerts, Trades) and console.js (Commands, reports, Docs).
"use strict";

const ADVANCED_TABS = ["commands", "docs"];
const OLD_TABS = { overview: "home", console: "commands", activity: "trades" };   // names stored before the redesign

// ------------------------------------------------------------------ login
function showLogin() {
  $("#shell").hidden = true;
  $("#login").hidden = false;
  clearInterval(state.timer);
  if ($("#sheet").open) $("#sheet").close();
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

// ------------------------------------------------------------------ tabs
function openTab(name) {
  if (!$(`#tab-${name}`) || (state.mode === "basic" && ADVANCED_TABS.includes(name))) name = "home";
  $$("#tabs .tab").forEach((b) => {
    b.classList.toggle("active", b.dataset.tab === name);
    if (b.dataset.tab === name) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
  });
  $$(".tab-page").forEach((p) => { p.hidden = p.id !== `tab-${name}`; });
  store.set("sb-tab", name);
  window.scrollTo(0, 0);
  if (name === "alerts") Views.loadAlerts();
  if (name === "trades") Views.loadTrades();
  if (name === "docs" && !$("#doc-list").childElementCount) Cmd.loadDocs();
}

const currentTab = () => { const p = [...$$(".tab-page")].find((x) => !x.hidden); return p ? p.id.slice(4) : "home"; };

// ------------------------------------------------------------------ Basic / Advanced
function setMode(mode, opts = {}) {
  state.mode = mode === "advanced" ? "advanced" : "basic";
  document.documentElement.dataset.mode = state.mode;
  store.set("sb-mode", state.mode);
  $$("[data-mode-set]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.modeSet === state.mode)));
  if (opts.quiet) return;
  if (state.mode === "basic" && ADVANCED_TABS.includes(currentTab())) openTab("home");
  if (state.mode === "advanced") Home.load();   // the governor, parameters and brief are only read in Advanced
}

// ------------------------------------------------------------------ menu, theme, refresh
function setTheme(theme) {
  if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
  else { delete document.documentElement.dataset.theme; theme = "auto"; }
  store.set("sb-theme", theme);
  $$("[data-theme-set]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.themeSet === theme)));
}

function toggleMenu(open) {
  const menu = $("#menu");
  menu.hidden = open === undefined ? !menu.hidden : !open;
  $("#menu-btn").setAttribute("aria-expanded", String(!menu.hidden));
}

function schedule() {
  clearInterval(state.timer);
  store.set("sb-auto", $("#auto").checked ? "1" : "0");
  if (!$("#auto").checked) return;
  state.timer = setInterval(() => {
    if (!document.hidden && !$("#tab-home").hidden && !$("#sheet").open) Home.load();
  }, 60000);
}

function wire() {
  $$("#tabs .tab").forEach((b) => b.addEventListener("click", () => openTab(b.dataset.tab)));
  $$("[data-mode-set]").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.modeSet)));
  $$("[data-theme-set]").forEach((b) => b.addEventListener("click", () => setTheme(b.dataset.themeSet)));
  $("#status").addEventListener("click", () => openTab("home"));
  $("#menu-btn").addEventListener("click", (e) => { e.stopPropagation(); toggleMenu(); });
  document.addEventListener("click", (e) => { if (!$("#menu").hidden && !$("#menu").contains(e.target)) toggleMenu(false); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") toggleMenu(false); });
  $("#refresh").addEventListener("click", () => Home.load({ full: true }));
  $("#auto").addEventListener("change", schedule);
  $("#halt").addEventListener("click", () => {
    toggleMenu(false);
    Cmd.open("halt_trading", { title: "Halt trading", onDone: () => Home.load() });
  });
  $("#logout").addEventListener("click", async () => {
    toggleMenu(false);
    await api("/api/logout", { body: {} }).catch(() => {});
    showLogin();
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && !$("#shell").hidden && !$("#tab-home").hidden && $("#auto").checked) Home.load();
  });
  Cmd.init();
  Views.init();
}

// ------------------------------------------------------------------ start
async function start() {
  const me = await api("/api/me");
  $("#login").hidden = true;
  $("#shell").hidden = false;
  $("#mode").textContent = me.mode;
  $("#mode").className = "pill " + (me.mode === "live" ? "bad" : "demo");
  $("#about").textContent = `experiment ${me.experiment} · code ${me.code}`;
  state.commands = await api("/api/commands");
  state.byKey = Object.fromEntries(state.commands.map((c) => [c.key, c]));
  Cmd.renderList();
  const tab = store.get("sb-tab", "home");
  openTab(OLD_TABS[tab] || tab);
  Home.load({ full: true });
  schedule();
}

setTheme(store.get("sb-theme", "auto"));
setMode(store.get("sb-mode", "basic"), { quiet: true });
$("#auto").checked = store.get("sb-auto", "1") !== "0";
wire();
start().catch(() => showLogin());
