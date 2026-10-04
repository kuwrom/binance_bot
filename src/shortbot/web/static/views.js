// The Alerts tab (grouped by kind, so 40 repeats of one guardian error read as one line) and the Trades tab (the
// record: totals, each setup's numbers, every closed trade with its thesis and lesson; the rulebook in Advanced).
"use strict";

const Views = (() => {
  const el = R.el;

  // ------------------------------------------------------------------ alerts
  // [{kind, items: newest first}], newest group first
  function groupAlerts(rows) {
    const groups = new Map();
    for (const a of rows) {
      if (!groups.has(a.kind)) groups.set(a.kind, { kind: a.kind, items: [] });
      groups.get(a.kind).items.push(a);
    }
    return [...groups.values()];
  }

  function alertRow(a, withKind) {
    const row = el("div", "alert" + (a.acked ? " acked" : ""));
    const head = el("div", "alert-head");
    head.append(el("span", "muted", `#${a.id} · ${F.when(a.ts)}`));
    if (withKind) head.append(el("span", "pill" + (a.urgent ? " bad" : ""), a.kind));
    if (a.symbol) head.append(el("span", "sym", F.coin(a.symbol)));
    row.append(head, el("div", "msg", a.message));
    return row;
  }

  async function loadAlerts() {
    const box = $("#alerts-list");
    if (!box.childElementCount) box.replaceChildren(el("p", "muted", "loading…"));
    try {
      const data = await api("/api/alerts?limit=300");
      state.alerts = data;
      let rows = data.alerts;
      if ($("#alerts-urgent").checked) rows = rows.filter((a) => a.urgent);
      if ($("#alerts-new").checked) rows = rows.filter((a) => !a.acked);
      $("#alerts-count").textContent = `${rows.length} shown · acknowledged up to #${data.ack_id}`;
      if (!rows.length) { box.replaceChildren(el("div", "empty", "No alerts match.")); return; }
      if (!$("#alerts-group").checked) { box.replaceChildren(...rows.map((a) => alertRow(a, true))); return; }
      box.replaceChildren(...groupAlerts(rows).map((g) => {
        const urgent = g.items.some((a) => a.urgent);
        const d = el("details", "agroup");
        const s = el("summary");
        const title = el("div", "row-title");
        title.append(el("span", "pill" + (urgent ? " bad" : ""), g.kind));
        if (g.items.length > 1) title.append(el("span", "count", `×${g.items.length}`));
        title.append(el("span", "muted", F.when(g.items[0].ts)));
        s.append(title, el("div", "row-sub clamp", g.items[0].message));
        d.append(s, ...g.items.map((a) => alertRow(a, false)));
        return d;
      }));
    } catch (err) { box.replaceChildren(el("p", "error", err.message)); }
  }

  // ------------------------------------------------------------------ trades
  // A closed trade as a row; expandable into its thesis, lesson and numbers.
  function tradeRow(t, expandable = true) {
    const top = el("div", "row-title");
    top.append(el("strong", null, F.coin(t.symbol)));
    if (t.setup) top.append(el("span", "pill", t.setup));
    top.append(el("span", "muted", F.when(t.closed_at)));
    const pnl = el("span", "row-num " + F.tone(t.pnl), F.usd(t.pnl, true));
    if (!expandable) {
      const row = el("div", "row");
      const main = el("div", "row-main");
      main.append(top);
      if (t.exit_reason) main.append(el("div", "row-sub clamp1", t.exit_reason));
      row.append(main, pnl);
      return row;
    }
    const d = el("details", "trade");
    const s = el("summary", "row");
    const main = el("div", "row-main");
    main.append(top, el("div", "row-sub clamp1", t.exit_reason || ""));
    s.append(main, pnl);
    const body = el("div", "plan");
    if (t.thesis) body.append(el("div", "chips-label", "Thesis"), el("p", "thesis", t.thesis));
    if (t.lesson) body.append(el("div", "chips-label", "Lesson"), el("p", "thesis", t.lesson));
    body.append(Cards.facts([
      ["Entry → exit", `${F.price(t.entry_price)} → ${F.price(t.exit_price)}`],
      ["Size", `${F.usd(t.notional, false, 0)} · ${t.leverage}x`],
      ["Held", t.opened_at && t.closed_at ? F.span((new Date(t.closed_at) - new Date(t.opened_at)) / 1000) : null],
      ["Worst / best", F.isNum(t.mae_pct) || F.isNum(t.mfe_pct) ?
        `${F.pct(-Math.abs(t.mae_pct || 0), true)} / ${F.pct(Math.abs(t.mfe_pct || 0), true)}` : null],
      ["Fees + funding", F.isNum(t.fees) || F.isNum(t.funding) ? F.usd((t.fees || 0) + (t.funding || 0), true) : null],
      ["Experiment", t.experiment]], "wide"));
    const { entry_features: _f, ...slim } = t;
    body.append(Cards.raw(slim));
    d.append(s, body);
    return d;
  }

  function tile(label, text, tone) {
    const t = el("div", "tile");
    t.append(el("div", "tile-label", label), el("div", "tile-value " + (tone || ""), text));
    return t;
  }

  function setups(stats) {
    const t = el("table");
    const hr = el("tr");
    ["Setup", "Trades", "Win rate", "Avg R", "P&L"].forEach((h, i) => hr.append(el("th", i ? "num" : null, h)));
    t.append(hr);
    for (const [name, s] of Object.entries(stats.by_setup || {}).sort((a, b) => b[1].n - a[1].n)) {
      const tr = el("tr");
      tr.append(el("td", null, name), el("td", "num", String(s.n)), el("td", "num", F.pct(s.win_rate, false, 0)),
        el("td", "num", F.isNum(s.avg_r) ? s.avg_r.toFixed(2) : "—"),
        el("td", "num " + F.tone(s.pnl), F.usd(s.pnl, true)));
      t.append(tr);
    }
    const wrap = el("div", "scroll");
    wrap.append(t);
    return wrap;
  }

  function rulebook(j) {
    const rows = (j.learnings || []).map((l) => {
      const row = el("div", "row");
      const main = el("div", "row-main");
      const title = el("div", "row-title");
      title.append(el("strong", null, `#${l.id}`), el("span", "pill" + (l.status === "adopted" ? " ok" : ""), l.status));
      if (l.setup) title.append(el("span", "pill", l.setup));
      if (l.tag) title.append(el("span", "muted", l.tag));
      main.append(title, el("div", "row-sub", l.text));
      row.append(main);
      return row;
    });
    if (j.learnings_more) rows.push(el("div", "empty", `${j.learnings_more} more: the learnings command.`));
    $("#rulebook").replaceChildren(...(rows.length ? rows : [el("div", "empty", "No learnings yet.")]));
  }

  async function loadTrades(fresh = false) {
    let j = fresh ? null : Home.journal();
    const list = $("#trades-list");
    if (!j) {
      list.replaceChildren(el("p", "muted", "loading…"));
      const got = await settle("journal", { limit: "50" });
      if (!got.ok) { list.replaceChildren(el("p", "error", got.err)); return; }
      j = got.res.result;
    }
    const st = j.stats || {};
    const sets = Object.values(st.by_setup || {});
    const n = sets.reduce((s, x) => s + x.n, 0), wins = sets.reduce((s, x) => s + x.wins, 0);
    $("#trades-tiles").replaceChildren(tile("Closed trades", String(st.closed_trades ?? n)),
      tile("Realized P&L", F.usd(st.total_pnl, true), F.tone(st.total_pnl)),
      tile("Win rate", n ? F.pct((wins / n) * 100, false, 0) : "—"));
    $("#trades-setups").replaceChildren(setups(st));
    const closed = (j.recent_trades || []).filter((t) => t.closed_at);
    list.replaceChildren(...(closed.length ? closed.map((t) => tradeRow(t)) : [el("div", "empty", "No closed trades yet.")]));
    rulebook(j);
  }

  function init() {
    ["#alerts-urgent", "#alerts-new", "#alerts-group"].forEach((s) => $(s).addEventListener("change", loadAlerts));
    $("#alerts-refresh").addEventListener("click", loadAlerts);
    $("#trades-refresh").addEventListener("click", () => loadTrades(true));
  }

  return { init, loadAlerts, loadTrades, groupAlerts, tradeRow };
})();
