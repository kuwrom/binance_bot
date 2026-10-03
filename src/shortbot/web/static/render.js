// Renders any command result as DOM: objects as key/value tables, lists of objects as tables, long or Markdown
// text through MD, chart paths as images. Built with textContent (never innerHTML) except MD's escaped output.
"use strict";

const R = (() => {
  const el = (tag, cls, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  };

  const isObj = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
  const short = (v) => {
    const s = JSON.stringify(v);
    return s.length > 160 ? s.slice(0, 157) + "…" : s;
  };

  function scalar(v, key) {
    if (v === null || v === undefined) return el("span", "muted", "—");
    if (typeof v === "boolean") return el("span", v ? "pill ok" : "pill bad", String(v));
    if (typeof v === "number") return el("span", "num" + (/pnl|_r$|^r$|realized|unrealized/i.test(key || "") ?
      (v > 0 ? " pos" : v < 0 ? " neg" : "") : ""), Number.isInteger(v) ? String(v) : String(+v.toFixed(6)));
    const s = String(v);
    if (/\.png$/.test(s) && key === "path") {
      const box = el("div", "chart");
      const img = el("img");
      img.src = "/api/png?path=" + encodeURIComponent(s);
      img.alt = s;
      box.append(img, el("div", "muted", s));
      return box;
    }
    if (s.includes("\n") || /^#{1,3} |\*\*|^- /m.test(s)) {
      const d = el("div", "md");
      d.innerHTML = MD.render(s);
      return d;
    }
    return el("span", null, s);
  }

  function table(rows, depth) {
    const cols = [];
    for (const r of rows.slice(0, 200)) for (const k of Object.keys(r)) if (!cols.includes(k)) cols.push(k);
    const wrap = el("div", "scroll");
    const t = el("table");
    const hr = el("tr");
    for (const c of cols.slice(0, 16)) hr.append(el("th", null, c));
    t.append(el("thead"));
    t.tHead.append(hr);
    const tb = el("tbody");
    for (const r of rows) {
      const tr = el("tr");
      for (const c of cols.slice(0, 16)) {
        const td = el("td");
        const v = r[c];
        if (isObj(v) || Array.isArray(v)) {
          if (depth < 1 && JSON.stringify(v).length > 160) {
            const d = el("details");
            d.append(el("summary", null, short(v)), value(v, c, depth + 1));
            td.append(d);
          } else td.append(el("code", null, short(v)));
        } else td.append(scalar(v, c));
        tr.append(td);
      }
      tb.append(tr);
    }
    t.append(tb);
    wrap.append(t);
    if (cols.length > 16) wrap.append(el("div", "muted", `${cols.length - 16} more columns: see Raw JSON`));
    return wrap;
  }

  function value(v, key, depth = 0) {
    if (Array.isArray(v)) {
      if (!v.length) return el("span", "muted", "none");
      if (v.every(isObj)) return table(v, depth);
      if (v.every((x) => !isObj(x) && !Array.isArray(x))) {
        const ul = el("ul", "plain");
        for (const x of v) { const li = el("li"); li.append(scalar(x, key)); ul.append(li); }
        return ul;
      }
      const ul = el("ul", "plain");
      for (const x of v) { const li = el("li"); li.append(value(x, key, depth + 1)); ul.append(li); }
      return ul;
    }
    if (isObj(v)) {
      const keys = Object.keys(v);
      if (!keys.length) return el("span", "muted", "{}");
      if (depth > 3) return el("code", null, short(v));
      const t = el("table", "kv");
      for (const k of keys) {
        const tr = el("tr");
        const th = el("th", null, k);
        const td = el("td");
        td.append(value(v[k], k, depth + 1));
        tr.append(th, td);
        t.append(tr);
      }
      return t;
    }
    return scalar(v, key);
  }

  // A result card: title, status badge, rendered value, and the raw JSON one click away.
  function result(title, res, opts = {}) {
    const card = el("div", "card result");
    const head = el("div", "result-head");
    head.append(el("h3", null, title));
    const r = res && res.result !== undefined ? res.result : res;
    const failed = isObj(r) && r.ok === false;
    if (res && res.error) head.append(el("span", "pill bad", "error"));
    else if (failed) head.append(el("span", "pill bad", r.unknown ? "unknown: re-read positions" : "refused"));
    else if (opts.write) head.append(el("span", "pill ok", "done"));
    if (res && res.ms !== undefined) head.append(el("span", "muted", `${res.ms} ms`));
    card.append(head);
    if (res && res.stale_code) card.append(el("p", "warn", res.stale_code));
    if (res && res.error) card.append(el("p", "error", res.error));
    else {
      if (failed && r.error) card.append(el("p", "error", r.error));
      card.append(value(r, "", 0));
      const raw = el("details", "raw");
      raw.append(el("summary", null, "Raw JSON"), el("pre", null, JSON.stringify(r, null, 2)));
      card.append(raw);
    }
    return card;
  }

  return { el, value, result, isObj };
})();
