// The Overview's cards for what is on the book: the banner (anything that needs the owner now), one card per open
// short with its price ladder and plan, and the resting entries.
"use strict";

const Cards = (() => {
  const el = R.el;
  const opened = new Set();   // symbols whose plan is unfolded

  function facts(pairs, cls) {
    const dl = el("dl", "facts" + (cls ? " " + cls : ""));
    for (const [k, v, tone] of pairs) {
      if (v === null || v === undefined || v === "") continue;
      const d = el("div", "fact");
      const dd = el("dd", tone || null);
      dd.append(v instanceof Node ? v : document.createTextNode(String(v)));
      d.append(el("dt", null, k), dd);
      dl.append(d);
    }
    return dl;
  }

  function raw(v) {
    const d = el("details", "raw adv");
    d.append(el("summary", null, "Raw JSON"), el("pre", null, JSON.stringify(v, null, 2)));
    return d;
  }

  // ------------------------------------------------------------------ banner
  function notice(level, title, lines) {
    const n = el("div", "notice " + level);
    n.setAttribute("role", level === "crit" ? "alert" : "status");
    const body = el("div", "notice-body");
    body.append(el("strong", null, title));
    for (const l of lines.filter(Boolean)) body.append(el("p", null, l));
    n.append(el("span", "notice-icon", level === "crit" ? "!" : "i"), body);
    return n;
  }

  function banner({ ks, acct, gov, positions, stale }) {
    const out = [];
    if (ks && ks.state && ks.state !== "ACTIVE") {
      out.push(notice(ks.state === "HALTED" ? "crit" : "warn", `${ks.state}: no new entries`, [
        ks.reason, ks.by && `Set by ${ks.by}${ks.at ? " · " + F.when(ks.at) : ""}`,
        ks.allowed && `Still allowed: ${ks.allowed}`, ks.clear && `Clears: ${ks.clear}`]));
    } else if (ks && ks.guardian_alive === false) {
      out.push(notice("crit", "Guardian is not alive", [
        `Last heartbeat ${F.span(ks.guardian_heartbeat_age_s)} ago: fills, stops and plans are unguarded.`]));
    }
    for (const p of positions || []) {
      if (p.UNPROTECTED) {
        out.push(notice("crit", `${F.coin(p.symbol)} has no stop on the exchange`, [
          p.error || "The short is open without a protective stop. Close it or set a stop."]));
      }
    }
    if (gov && gov.halt) {
      out.push(notice("warn", "Governor paused new entries", [
        (gov.reasons || []).join(" · "), gov.until && `Until ${F.when(gov.until)}`]));
    }
    if (acct && acct.degraded) out.push(notice("warn", "P&L unverified", ["Day and week P&L are the old estimates: the equity marks could not be read."]));
    if (stale) out.push(notice("warn", "Web server runs old code", [stale, "Writes are refused until it restarts."]));
    return out;
  }

  // ------------------------------------------------------------------ an open short
  function plan(p) {
    const pl = p.plan || {};
    const box = el("div", "plan");
    if (p.thesis) box.append(el("p", "thesis", p.thesis));
    const hit = p.targets_hit || 0;
    const targets = (pl.targets || []).map((t, i) =>
      `${F.price(t.price)} (${Math.round((t.fraction || 0) * 100)}%)${i < hit ? " ✓" : ""}`).join(" · ");
    let timeStop = null;
    if (F.isNum(pl.time_stop_hours)) {
      const left = p.opened_at ? new Date(p.opened_at).getTime() / 1000 + pl.time_stop_hours * 3600 - Date.now() / 1000
        : null;
      timeStop = `${pl.time_stop_hours} h` + (left === null ? "" : left > 0 ? ` · ${F.span(left)} left` : " · due");
    }
    box.append(facts([["Trigger", pl.trigger], ["Invalidation", pl.invalidation], ["Targets", targets || null],
      ["Time stop", timeStop], ["Worst / best so far", F.isNum(p.mae_pct) || F.isNum(p.mfe_pct) ?
        `${F.pct(-Math.abs(p.mae_pct || 0), true)} / ${F.pct(Math.abs(p.mfe_pct || 0), true)}` : null]], "wide"));
    if (pl.auto) {
      const row = el("div", "chips");
      row.append(el("span", "chips-label", "Guardian runs"));
      for (const [k, name] of [["targets", "targets"], ["breakeven", "breakeven"], ["trail", "trailing"],
        ["time_stop", "time stop"]]) {
        row.append(el("span", "chip " + (pl.auto[k] ? "on" : "off"), `${pl.auto[k] ? "✓" : "✕"} ${name}`));
      }
      box.append(row);
    }
    if ((pl.kill_conditions || []).length) {
      const ul = el("ul", "kills");
      for (const k of pl.kill_conditions) ul.append(el("li", null, k));
      box.append(el("div", "chips-label", "Exit early if"), ul);
    }
    const tech = facts([["Leverage", p.leverage ? `${p.leverage}x ${p.margin_mode || ""}` : null],
      ["Liquidation", F.isNum(p.liquidation) ? F.price(p.liquidation) : null],
      ["Contracts", p.contracts], ["Trade", p.trade_id ? `#${p.trade_id} · ${p.journal_status}` : p.journal_status],
      ["Opened", F.when(p.opened_at)]], "adv");
    box.append(tech, raw(p));
    return box;
  }

  function position(p, reload) {
    const card = el("article", "card short" + (p.UNPROTECTED ? " unprotected" : ""));
    const head = el("header", "pos-head");
    const name = el("div", "pos-name");
    const sym = el("h3", null, F.coin(p.symbol));
    sym.append(el("span", "quote", "short"));
    const meta = el("div", "pos-meta");
    if (p.setup) meta.append(el("span", "pill", p.setup));
    meta.append(el("span", "muted", p.opened_at ? `open ${F.since(p.opened_at)}` : "not in journal"));
    name.append(sym, meta);
    const pnl = el("div", "pos-pnl");
    pnl.append(el("div", "pos-usd " + F.tone(p.unrealized_pnl), F.usd(p.unrealized_pnl, true)),
      el("div", "pos-pct " + F.tone(p.pnl_pct), F.pct(p.pnl_pct, true)));
    head.append(name, pnl);
    card.append(head, V.ladder(p));
    card.append(facts([
      ["Stop", p.UNPROTECTED ? "NONE" : F.isNum(p.stop_distance_pct) ? `${F.pct(p.stop_distance_pct)} away` : "—",
        p.UNPROTECTED ? "neg" : ""],
      ["Size", F.usd(p.notional, false, 0)], ["Entry", F.price(p.entry)],
      ["Targets hit", `${p.targets_hit || 0} of ${((p.plan && p.plan.targets) || []).length}`]]));

    const more = el("details", "pos-more");
    more.open = opened.has(p.symbol);   // a refresh rebuilds the card: keep what the owner unfolded
    more.addEventListener("toggle", () => { if (more.open) opened.add(p.symbol); else opened.delete(p.symbol); });
    more.append(el("summary", null, "Thesis and plan"), plan(p));
    const acts = el("div", "pos-actions");
    const stop = el("button", null, "Move stop");
    stop.addEventListener("click", () => Cmd.open("move_stop", {
      title: `Move the ${F.coin(p.symbol)} stop`, prefill: { symbol: p.symbol }, onDone: reload }));
    const close = el("button", "danger-ghost", "Close…");
    close.addEventListener("click", () => Cmd.open("close_position", {
      title: `Close ${F.coin(p.symbol)}`, prefill: { symbol: p.symbol, expect_contracts: p.contracts },
      onDone: reload }));
    acts.append(stop, close);
    card.append(more, acts);
    return card;
  }

  // ------------------------------------------------------------------ a resting entry
  function pending(e, reload) {
    const row = el("div", "row");
    const main = el("div", "row-main");
    const title = el("div", "row-title");
    title.append(el("strong", null, F.coin(e.symbol)));
    if (e.setup) title.append(el("span", "pill", e.setup));
    if (e.status && e.status !== "pending") title.append(el("span", "pill warn", e.status));
    const left = F.isNum(e.expires_ms) ? (e.expires_ms - Date.now()) / 1000 : null;
    const size = F.isNum(e.amount) && F.isNum(e.limit_price) ? F.usd(e.amount * e.limit_price, false, 0) : null;
    main.append(title, el("div", "row-sub", [
      `sell at ${F.price(e.limit_price)}`, `stop ${F.price(e.stop_price)}`, size,
      left === null ? null : left > 0 ? `expires in ${F.span(left)}` : "expired"].filter(Boolean).join(" · ")));
    if (e.hint) main.append(el("div", "row-sub warn-text", e.hint));
    if (e.level_reason) main.append(el("div", "row-sub adv", e.level_reason));
    const cancel = el("button", "ghost small", "Cancel");
    cancel.title = `Cancel group ${e.group_id}`;
    cancel.addEventListener("click", () => Cmd.open("cancel_entry_group", {
      title: `Cancel the ${F.coin(e.symbol)} entries`, prefill: { group_id: e.group_id }, onDone: reload }));
    row.append(main, cancel);
    return row;
  }

  return { banner, position, pending, facts, raw, notice };
})();
