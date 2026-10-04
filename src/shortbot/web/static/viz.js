// The desk's three figures, built as DOM (textContent only; positions set through the CSSOM, which the CSP allows):
// a meter (one ratio against a limit), a price ladder (where the mark sits between targets and stop), and a sparkline
// with a crosshair readout.
"use strict";

const V = (() => {
  const el = R.el;
  const NS = "http://www.w3.org/2000/svg";
  const svg = (tag, attrs) => {
    const e = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    return e;
  };
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

  // used / cap as a track. The fill turns warning at 60% and critical at 85%; `tick` marks a threshold on the track.
  function meter({ label, used, cap, text, tick, hint }) {
    const frac = cap > 0 && F.isNum(used) ? clamp(used / cap, 0, 1) : 0;
    const m = el("div", "meter" + (frac >= 0.85 ? " crit" : frac >= 0.6 ? " warn" : ""));
    const head = el("div", "meter-head");
    head.append(el("span", "meter-label", label), el("span", "meter-text", text));
    const track = el("div", "meter-track");
    track.setAttribute("role", "meter");
    track.setAttribute("aria-label", `${label}: ${text}`);
    track.setAttribute("aria-valuemin", "0");
    track.setAttribute("aria-valuemax", "100");
    track.setAttribute("aria-valuenow", String(Math.round(frac * 100)));
    const fill = el("div", "meter-fill");
    fill.style.width = `${frac > 0 ? Math.max(frac * 100, 1.5) : 0}%`;
    track.append(fill);
    if (F.isNum(tick) && cap > 0) {
      const t = el("div", "meter-tick");
      t.style.left = `${clamp(tick / cap, 0, 1) * 100}%`;
      track.append(t);
    }
    m.append(head, track);
    if (hint) m.append(el("div", "meter-hint", hint));
    return m;
  }

  // A short's price ladder, low prices left: targets, entry, the live mark and the exchange stop. The band between
  // entry and mark is the open P&L (left of entry = profit for a short).
  function ladder(p) {
    const targets = ((p.plan && p.plan.targets) || []).map((t) => t.price).filter(F.isNum).sort((a, b) => a - b);
    const stop = F.isNum(p.exchange_stop) ? p.exchange_stop : null;
    const all = [...targets, p.entry, p.mark, stop].filter(F.isNum);
    const box = el("div", "ladder");
    if (all.length < 2) return box;
    const lo = Math.min(...all), hi = Math.max(...all);
    const at = (x) => (hi > lo ? ((x - lo) / (hi - lo)) * 100 : 50);
    const place = (node, x) => {
      const pos = at(x);
      if (pos < 12) { node.classList.add("at-start"); node.style.left = "0"; }
      else if (pos > 88) { node.classList.add("at-end"); node.style.right = "0"; }
      else node.style.left = `${pos}%`;
      return node;
    };
    const track = el("div", "ladder-track");
    const band = el("div", "ladder-band " + (p.mark <= p.entry ? "pos" : "neg"));
    band.style.left = `${Math.min(at(p.entry), at(p.mark))}%`;
    band.style.width = `${Math.abs(at(p.entry) - at(p.mark))}%`;
    track.append(band);
    box.append(track);

    // Stop and entry are labelled first; a target label that would overlap one is hidden once the ladder has a
    // width (the facts row under it carries every number).
    const levels = [];
    if (stop !== null) levels.push({ x: stop, name: "stop", cls: "stop" });
    levels.push({ x: p.entry, name: "entry", cls: "entry" });
    targets.slice().reverse().forEach((x, i) => levels.push({ x, name: `T${i + 1}`, cls: "target" }));
    const labs = levels.map((lv) => {
      const tick = el("div", "ladder-tick " + lv.cls);
      tick.style.left = `${at(lv.x)}%`;
      const lab = el("div", "ladder-lab " + lv.cls);
      lab.append(el("span", "ladder-name", lv.name), el("span", null, F.price(lv.x)));
      box.append(tick, place(lab, lv.x));
      return lab;
    });
    new ResizeObserver(() => {
      const kept = [];
      for (const lab of labs) {
        lab.hidden = false;
        const r = lab.getBoundingClientRect();
        lab.hidden = kept.some((q) => r.left < q.right + 4 && r.right > q.left - 4);
        if (!lab.hidden) kept.push(r);
      }
    }).observe(box);
    const dot = el("div", "ladder-dot");
    dot.style.left = `${at(p.mark)}%`;
    const mark = el("div", "ladder-lab mark");
    mark.append(el("span", "ladder-name", "mark"), el("span", null, F.price(p.mark)));
    box.append(dot, place(mark, p.mark));
    box.setAttribute("role", "img");
    box.setAttribute("aria-label", `mark ${F.price(p.mark)}, entry ${F.price(p.entry)}` +
      (stop !== null ? `, stop ${F.price(stop)}` : ", no stop") +
      (targets.length ? `, targets ${targets.map(F.price).join(" and ")}` : ""));
    return box;
  }

  // points: [{y, value, label}] in time order. One series, so no legend: the card's title names it.
  function spark(points) {
    const box = el("div", "spark");
    if (points.length < 2) { box.append(el("p", "muted", "Not enough closed trades for a curve yet.")); return box; }
    const ys = points.map((p) => p.y);
    const lo = Math.min(0, ...ys), hi = Math.max(0, ...ys);
    const X = (i) => (i / (points.length - 1)) * 100;
    const Y = (y) => 8 + (1 - (hi > lo ? (y - lo) / (hi - lo) : 0.5)) * 84;
    const line = points.map((p, i) => `${i ? "L" : "M"}${X(i).toFixed(2)},${Y(p.y).toFixed(2)}`).join(" ");
    const s = svg("svg", { viewBox: "0 0 100 100", preserveAspectRatio: "none", "aria-hidden": "true" });
    s.append(svg("path", { class: "spark-area", d: `${line} L100,${Y(0).toFixed(2)} L0,${Y(0).toFixed(2)} Z` }),
      svg("line", { class: "spark-zero", x1: 0, x2: 100, y1: Y(0), y2: Y(0) }),
      svg("path", { class: "spark-line", d: line }));
    const cross = el("div", "spark-cross");
    const dot = el("div", "spark-dot");
    const tip = el("div", "spark-tip");
    tip.setAttribute("role", "status");
    box.append(s, cross, dot, tip);

    const show = (i, hover) => {
      const p = points[i];
      dot.style.left = `${X(i)}%`;
      dot.style.top = `${Y(p.y)}%`;
      box.classList.toggle("hover", hover);
      if (!hover) return;
      cross.style.left = `${X(i)}%`;
      tip.replaceChildren(el("strong", null, p.value), el("span", null, p.label));
      tip.style.left = `${X(i)}%`;
      tip.style.transform = `translate(${-X(i)}%, -100%)`;   // slides with the point, never leaves the chart
    };
    let idx = points.length - 1;
    const rest = () => { idx = points.length - 1; show(idx, false); };
    const move = (e) => {
      const r = box.getBoundingClientRect();
      idx = clamp(Math.round(((e.clientX - r.left) / r.width) * (points.length - 1)), 0, points.length - 1);
      show(idx, true);
    };
    box.tabIndex = 0;
    box.setAttribute("role", "img");
    box.setAttribute("aria-label", `${points.length - 1} closed trades, ending at ${points[points.length - 1].value}. ` +
      "Arrow keys step through the trades.");
    box.addEventListener("pointermove", move);
    box.addEventListener("pointerdown", move);
    box.addEventListener("pointerleave", rest);
    box.addEventListener("blur", rest);
    box.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      e.preventDefault();
      idx = clamp(idx + (e.key === "ArrowLeft" ? -1 : 1), 0, points.length - 1);
      show(idx, true);
    });
    rest();
    return box;
  }

  return { meter, ladder, spark };
})();
