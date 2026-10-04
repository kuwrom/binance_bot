// The Overview. Basic answers three questions in order: is the desk safe, how is the money, what is on the book.
// Advanced adds the governor, the risk parameters and the brief Claude gets. A failed read keeps the last good frame.
"use strict";

const Home = (() => {
  const el = R.el;
  const JOURNAL_EVERY_MS = 5 * 60 * 1000;   // the journal is big: closed trades change rarely
  const last = {};
  let journalAt = 0;

  const value = (got) => (got && got.ok ? got.res.result : null);
  const failed = (title, got) => el("p", "error", `${title}: ${got ? got.err : "no answer"}`);

  // ------------------------------------------------------------------ header chip
  function status(ks) {
    const chip = $("#status");
    const ok = ks && ks.state === "ACTIVE" && ks.guardian_alive;
    chip.className = "status " + (!ks ? "" : ok ? "ok" : ks.state === "REDUCING" && ks.guardian_alive ? "warn" : "bad");
    $("#status-text").textContent = !ks ? "unknown" : ok ? `Active · guardian ${F.span(ks.guardian_heartbeat_age_s)}`
      : ks.state !== "ACTIVE" ? ks.state : "Guardian down";
    chip.title = ks ? `Kill switch ${ks.state}; guardian heartbeat ${F.span(ks.guardian_heartbeat_age_s)} ago` : "";
  }

  // ------------------------------------------------------------------ hero: equity, P&L, the realized curve
  function delta(label, v, sub) {
    const d = el("div", "delta");
    d.append(el("span", "delta-label", label), el("span", "delta-value " + F.tone(v), F.usd(v, true)));
    if (sub) d.append(el("span", "delta-sub", sub));
    return d;
  }

  function curve(journal) {
    const closed = (journal.recent_trades || []).filter((t) => t.closed_at && F.isNum(t.pnl))
      .sort((a, b) => String(a.closed_at).localeCompare(String(b.closed_at)));
    let cum = 0;
    const pts = [{ y: 0, value: F.usd(0), label: "before the first trade" }];
    for (const t of closed) {
      cum += t.pnl;
      pts.push({ y: cum, value: F.usd(cum, true), label: `${F.coin(t.symbol)} ${F.usd(t.pnl, true)} · ${F.when(t.closed_at)}` });
    }
    const box = el("div", "hero-chart");
    const head = el("div", "chart-head");
    const total = journal.stats && F.isNum(journal.stats.total_pnl) ? journal.stats.total_pnl : cum;
    head.append(el("span", null, `Realized P&L · last ${closed.length} closed trades`),
      el("strong", F.tone(total), F.usd(total, true)));
    box.append(head, V.spark(pts));
    return box;
  }

  function hero(acct, journal) {
    const box = $("#hero");
    if (!acct) return;
    const start = acct.equity - (acct.day_pnl || 0);
    const body = el("div", "hero-figures");
    body.append(el("div", "eyebrow", "Equity"), el("div", "hero-value", F.usd(acct.equity)));
    const deltas = el("div", "deltas");
    deltas.append(delta("Today", acct.day_pnl, start > 0 ? F.pct((acct.day_pnl / start) * 100, true) : null),
      delta("This week", acct.week_pnl), delta("Open P&L", acct.unrealized));
    const free = el("div", "delta");
    free.append(el("span", "delta-label", "Free margin"), el("span", "delta-value", F.usd(acct.available, false, 0)));
    deltas.append(free);
    body.append(deltas);
    box.replaceChildren(body);
    if (journal) box.append(curve(journal));
  }

  // ------------------------------------------------------------------ what is on the book
  function book(positions, pend) {
    const reload = () => load({ full: true });
    const pos = $("#positions");
    if (positions) {
      $("#pos-count").textContent = positions.length ? String(positions.length) : "";
      pos.replaceChildren(...(positions.length ? positions.map((p) => Cards.position(p, reload))
        : [el("div", "card empty", "Flat. No open shorts.")]));
    }
    if (pend) {
      $("#pending-count").textContent = pend.length ? String(pend.length) : "";
      $("#pending").replaceChildren(...(pend.length ? pend.map((e) => Cards.pending(e, reload))
        : [el("div", "empty", "No resting entries.")]));
    }
  }

  // ------------------------------------------------------------------ risk room
  function risk(acct, gov, positions) {
    const box = $("#risk");
    if (!acct || !gov) return;
    const lim = acct.limits || {};
    const out = [];
    const daily = gov.daily || {};
    if (F.isNum(daily.cap_usd)) {
      out.push(V.meter({ label: "Daily loss cap", used: daily.cap_usd - daily.room_usd, cap: daily.cap_usd,
        text: `${F.usd(daily.room_usd, false, 0)} left of ${F.usd(daily.cap_usd, false, 0)}` }));
    }
    const heat = acct.equity * (lim.max_portfolio_heat_pct || 0) / 100;
    if (heat > 0) {
      out.push(V.meter({ label: "Risk at the stops", used: gov.open_risk_usd || 0, cap: heat,
        text: `${F.usd(gov.open_risk_usd || 0, false, 0)} of ${F.usd(heat, false, 0)}` }));
    }
    if (positions && lim.max_total_notional) {
      const used = positions.reduce((s, p) => s + (p.notional || 0), 0);
      out.push(V.meter({ label: "Exposure", used, cap: lim.max_total_notional,
        text: `${F.usd(used, false, 0)} of ${F.usd(lim.max_total_notional, false, 0)} · ${positions.length} of ${lim.max_positions} shorts` }));
    }
    if (lim.dd_stop_pct) {
      out.push(V.meter({ label: "Drawdown from peak", used: gov.drawdown_pct || 0, cap: lim.dd_stop_pct,
        tick: lim.dd_halve_pct, text: F.pct(gov.drawdown_pct || 0),
        hint: `Size halves at ${lim.dd_halve_pct}%, entries stop at ${lim.dd_stop_pct}%` }));
    }
    const chips = el("div", "chips");
    if (gov.regime) chips.append(el("span", "chip", `Regime: ${gov.regime}`));
    if (gov.cell) chips.append(el("span", "chip adv", `Cell: ${gov.cell}`));
    if (F.isNum(gov.m_gov)) chips.append(el("span", "chip" + (gov.m_gov < 1 ? " off" : " adv"), `Sizing ×${gov.m_gov}`));
    const streak = gov.streak && gov.streak.current;
    if (streak) chips.append(el("span", "chip off", `Loss streak ${streak}`));
    out.push(chips);
    box.replaceChildren(...out);
  }

  // ------------------------------------------------------------------ alerts that need the owner
  function attention(data) {
    const box = $("#attention");
    const open = data.alerts.filter((a) => a.urgent && !a.acked);
    const badge = $("#alerts-badge");
    badge.textContent = open.length > 99 ? "99+" : String(open.length);
    badge.hidden = !open.length;
    if (!open.length) { box.replaceChildren(el("div", "empty", "Nothing needs you right now.")); return; }
    const rows = Views.groupAlerts(open).slice(0, 4).map((g) => {
      const row = el("div", "row");
      const main = el("div", "row-main");
      const title = el("div", "row-title");
      title.append(el("span", "pill bad", g.kind));
      if (g.items.length > 1) title.append(el("span", "muted", `×${g.items.length}`));
      title.append(el("span", "muted", F.when(g.items[0].ts)));
      main.append(title, el("div", "row-sub clamp", g.items[0].message));
      row.append(main);
      return row;
    });
    const all = el("button", "link", `All ${open.length} urgent alerts`);
    all.addEventListener("click", () => openTab("alerts"));
    box.replaceChildren(...rows, all);
  }

  function recent(journal) {
    const closed = (journal.recent_trades || []).filter((t) => t.closed_at).slice(0, 5);
    const rows = closed.map((t) => Views.tradeRow(t, false));
    const all = el("button", "link", "All trades");
    all.addEventListener("click", () => openTab("trades"));
    $("#recent").replaceChildren(...(rows.length ? [...rows, all] : [el("div", "empty", "No closed trades yet.")]));
  }

  // ------------------------------------------------------------------ advanced
  function params(got) {
    const box = $("#params");
    const rp = value(got);
    if (!rp) { box.replaceChildren(failed("risk_params", got)); return; }
    // The overridden limits first; the ones still at the .env baseline fold away.
    const table = (keys) => {
      const t = el("table", "params");
      const hr = el("tr");
      ["Limit", "Now", ".env", "Bounds"].forEach((h, i) => hr.append(el("th", i ? "num" : null, h)));
      t.append(hr);
      for (const k of keys) {
        const tr = el("tr", k in over ? "over" : null);
        const b = (rp.bounds || {})[k];
        tr.append(el("td", null, F.label(k)), el("td", "num", F.compact(rp.effective[k])),
          el("td", "num muted", F.compact((rp.baseline_env || {})[k])),
          el("td", "num muted", b ? `${F.compact(b[0])} – ${F.compact(b[1])}` : ""));
        t.append(tr);
      }
      const wrap = el("div", "scroll");
      wrap.append(t);
      return wrap;
    };
    const over = rp.overrides || {};
    const keys = Object.keys(rp.effective || {});
    const changed = keys.filter((k) => k in over), same = keys.filter((k) => !(k in over));
    const rest = el("details", "fold");
    rest.append(el("summary", null, `${same.length} limits at the .env baseline`), table(same));
    const head = el("div", "result-head");
    head.append(el("span", "muted", rp.overrides_active ? `Overridden by experiment ${rp.experiment}:`
      : "No overrides: the .env baseline applies."));
    const btn = el("button", "ghost small", "Change");
    btn.addEventListener("click", () => Cmd.open("set_risk_params", { title: "Change risk parameters",
      onDone: () => load({ full: true }) }));
    head.append(btn);
    box.replaceChildren(head, ...(changed.length ? [table(changed)] : []), rest,
      el("p", "muted small-print", rp.note || ""), Cards.raw(rp));
  }

  function advanced(got) {
    params(got.risk_params);
    $("#governor").replaceChildren(got.governor_status.ok ? R.result("governor_status", got.governor_status.res)
      : failed("governor_status", got.governor_status));
    const b = $("#brief");
    if (got.brief.ok) b.innerHTML = MD.render(String(got.brief.res.result));
    else b.replaceChildren(failed("brief", got.brief));
  }

  // ------------------------------------------------------------------ load
  async function load({ full = false } = {}) {
    const btn = $("#refresh");
    btn.disabled = true;
    $("#tab-home").classList.add("loading");
    const adv = state.mode === "advanced";
    const keys = ["trading_state", "account", "positions", "pending_entries", "governor_status",
      ...(adv ? ["risk_params", "brief"] : [])];
    const wantJournal = full || !last.journal || Date.now() - journalAt > JOURNAL_EVERY_MS;
    const [alerts, journal, ...rest] = await Promise.all([
      api("/api/alerts?limit=300").catch(() => null), wantJournal ? settle("journal", { limit: "50" }) : null,
      ...keys.map((k) => settle(k))]);
    const got = Object.fromEntries(keys.map((k, i) => [k, rest[i]]));
    for (const k of keys) if (got[k].ok) last[k] = value(got[k]);
    if (journal && journal.ok) { last.journal = value(journal); journalAt = Date.now(); }
    if (alerts) state.alerts = alerts;
    const stale = keys.map((k) => got[k].ok && got[k].res.stale_code).find(Boolean);

    status(last.trading_state);
    $("#banner").replaceChildren(...Cards.banner({ ks: last.trading_state, acct: last.account,
      gov: last.governor_status, positions: last.positions, stale }),
    ...keys.filter((k) => !got[k].ok && !(k in last)).map((k) => failed(k, got[k])));
    hero(last.account, last.journal);
    book(last.positions, last.pending_entries);
    risk(last.account, last.governor_status, last.positions);
    if (state.alerts) attention(state.alerts);
    if (last.journal) recent(last.journal);
    if (adv) advanced(got);

    const missed = keys.filter((k) => !got[k].ok);
    $("#updated").textContent = missed.length ? `${missed.join(", ")} failed · showing the last good read`
      : "Updated " + new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    $("#updated").classList.toggle("error", missed.length > 0);
    $("#tab-home").classList.remove("loading");
    btn.disabled = false;
  }

  const journal = () => last.journal;
  return { load, journal };
})();
