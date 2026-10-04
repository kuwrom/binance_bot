// Every desk command as a form (generated from /api/commands), in the Commands tab or in a sheet opened from a
// card. A write shows what it will send and asks once more, in the page, before it runs. Also the reports and docs.
"use strict";

const Cmd = (() => {
  const el = R.el;

  function input(f) {
    let e;
    if (f.kind === "bool") {
      e = el("select");
      for (const [v, t] of [["", `default (${f.default})`], ["true", "true"], ["false", "false"]]) {
        const o = el("option", null, t); o.value = v; e.append(o);
      }
    } else if (f.kind === "choice") {
      e = el("select");
      if (!f.required) { const o = el("option", null, `default (${f.default ?? "none"})`); o.value = ""; e.append(o); }
      for (const c of f.choices) { const o = el("option", null, String(c)); o.value = String(c); e.append(o); }
    } else if (f.kind === "json") {
      e = el("textarea");
      e.rows = 4;
      e.placeholder = f.type.startsWith("dict") ? "JSON, or KEY=VALUE lines" : "JSON";
    } else {
      e = el("input");
      e.type = f.kind === "int" || f.kind === "float" ? "number" : "text";
      if (f.kind === "float") e.step = "any";
      if (e.type === "number") e.inputMode = "decimal";
      if (f.kind === "list") e.placeholder = "values separated by commas or spaces";
      if (!f.required && f.default !== null && f.kind !== "list") e.placeholder = `default: ${f.default}`;
    }
    e.name = f.name;
    e.required = f.required;
    return e;
  }

  // compact (a sheet): the label and the help; the full form also names the parameter and its type
  function field(f, id, value, compact) {
    const row = el("div", "field");
    const lab = el("label", null, F.label(f.name) + (f.required ? " *" : ""));
    const inp = input(f);
    inp.id = lab.htmlFor = id;
    if (value !== undefined) inp.value = String(value);
    row.append(lab, inp);
    const note = (compact ? [f.help] : [f.name, f.type, f.help]).filter(Boolean).join(" · ");
    if (note) row.append(el("small", "muted", note));
    return row;
  }

  // The form for one command. opts: prefill {name: value}, compact (a sheet: optional fields folded, short help),
  // out (where results go), onDone(res) after a run that wasn't refused.
  function form(c, opts = {}) {
    const prefill = opts.prefill || {};
    const box = el("div", "cmd-box");
    const head = el("div", "result-head");
    head.append(el("h2", null, opts.title || c.key));
    if (c.write) head.append(el("span", "pill bad", "write"));
    if (c.weight && !opts.compact) head.append(el("span", "pill", c.weight));
    box.append(head);
    if (opts.compact) {
      if (c.help) box.append(el("p", "muted", c.help));
    } else if (c.doc) {
      const d = el("div", "md doc");
      d.innerHTML = MD.render(c.doc);
      box.append(d);
    }

    const f = el("form", "fields");
    const uid = Math.random().toString(36).slice(2, 7);
    const main = c.fields.filter((x) => x.required || x.name in prefill);
    const rest = c.fields.filter((x) => !main.includes(x));
    for (const x of main) f.append(field(x, `f-${uid}-${x.name}`, prefill[x.name], opts.compact));
    if (rest.length) {
      const more = el("details", "optional");
      more.open = !opts.compact;
      more.append(el("summary", null, `Optional (${rest.length})`));
      const inner = el("div", "fields");
      for (const x of rest) inner.append(field(x, `f-${uid}-${x.name}`, undefined, opts.compact));
      more.append(inner);
      f.append(more);
    }
    const go = el("button", c.write ? "danger" : "primary", c.write ? `Review ${c.key}` : `Run ${c.key}`);
    go.type = "submit";
    const confirmBox = el("div", "confirm");
    confirmBox.hidden = true;
    const out = opts.out || el("div", "cmd-out");
    f.append(go, confirmBox);
    box.append(f);
    if (!opts.out) box.append(out);

    const values = () => {
      const args = {};
      for (const x of c.fields) {
        const v = f.elements[x.name].value;
        if (v !== "") args[x.name] = v;
      }
      return args;
    };
    const send = async (args) => {
      go.disabled = true;
      const wait = el("p", "muted running", `running ${c.key}…`);
      out.prepend(wait);
      try {
        const res = await run(c.key, args);
        wait.remove();
        out.prepend(R.result(c.key, res, { write: c.write }));
        const r = res && res.result;
        if (opts.onDone && !(R.isObj(r) && r.ok === false)) opts.onDone(res);
      } catch (err) {
        wait.remove();
        out.prepend(R.result(c.key, { error: err.message }));
      } finally { go.disabled = false; }
    };
    f.addEventListener("submit", (e) => {
      e.preventDefault();
      const args = values();
      if (!c.write) { send(args); return; }
      confirmBox.replaceChildren(el("strong", null, `Run ${c.key}? It changes the desk.`), R.value(args, "", 0));
      const row = el("div", "confirm-row");
      const yes = el("button", "danger", `Run ${c.key}`);
      const no = el("button", "ghost", "Cancel");
      yes.type = no.type = "button";
      const close = () => { confirmBox.hidden = true; go.hidden = false; };
      yes.addEventListener("click", () => { close(); send(args); });
      no.addEventListener("click", close);
      row.append(yes, no);
      confirmBox.append(row);
      confirmBox.hidden = false;
      go.hidden = true;
      yes.focus();
    });
    return box;
  }

  // ------------------------------------------------------------------ the sheet (a command opened from a card)
  function open(key, opts = {}) {
    const c = state.byKey[key];
    const sheet = $("#sheet");
    if (!c || !sheet) return;
    $("#sheet-body").replaceChildren(form(c, { compact: true, ...opts }));
    if (!sheet.open) sheet.showModal();
  }

  // ------------------------------------------------------------------ the Commands tab
  function renderList() {
    const q = $("#cmd-search").value.trim().toLowerCase();
    const groups = {};
    for (const c of state.commands) {
      if (q && !(c.key.toLowerCase().includes(q) || c.help.toLowerCase().includes(q))) continue;
      (groups[c.group] = groups[c.group] || []).push(c);
    }
    const box = $("#cmd-groups");
    box.replaceChildren();
    for (const [g, cmds] of Object.entries(groups)) {
      box.append(el("h4", null, g));
      for (const c of cmds) {
        const b = el("button", "cmd" + (state.current === c.key ? " active" : ""));
        b.append(el("span", null, c.key));
        if (c.write) b.append(el("span", "pill bad", "write"));
        b.title = c.help;
        b.addEventListener("click", () => select(c.key));
        box.append(b);
      }
    }
    if (!box.childElementCount) box.append(el("p", "muted", "No command matches."));
  }

  function select(key, prefill) {
    const c = state.byKey[key];
    if (!c) return;
    state.current = key;
    renderList();
    const card = el("div", "card");
    card.append(form(c, { prefill, out: $("#cmd-result") }));
    $("#cmd-form").replaceChildren(card);
    $("#tab-commands").classList.add("picked");
  }

  // ------------------------------------------------------------------ reports (the Trades tab, advanced)
  async function report(key) {
    const args = key === "audit" ? { since: "24h" } : {};
    const out = $("#report-result");
    out.replaceChildren(el("p", "muted", `running ${key}…`));
    try { out.replaceChildren(R.result(key, await run(key, args))); }
    catch (err) { out.replaceChildren(R.result(key, { error: err.message })); }
  }

  // ------------------------------------------------------------------ docs
  async function loadDocs() {
    const docs = await api("/api/docs");
    const render = () => {
      const q = $("#doc-search").value.trim().toLowerCase();
      const ul = $("#doc-list");
      ul.replaceChildren();
      for (const d of docs.filter((x) => x.toLowerCase().includes(q))) {
        const li = el("li");
        const a = el("button", "link", d);
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
      view.prepend(el("div", "muted", path));
      $("#tab-docs").classList.add("picked");
      window.scrollTo(0, 0);
    } catch (err) { view.replaceChildren(el("p", "error", err.message)); }
  }

  function init() {
    $("#cmd-search").addEventListener("input", renderList);
    $$("[data-preset]").forEach((b) => b.addEventListener("click", () => report(b.dataset.preset)));
    $$("[data-back]").forEach((b) => b.addEventListener("click", () => b.closest(".tab-page").classList.remove("picked")));
    $("#sheet-close").addEventListener("click", () => $("#sheet").close());
    $("#sheet").addEventListener("click", (e) => { if (e.target === $("#sheet")) $("#sheet").close(); });
  }

  return { init, open, select, renderList, loadDocs };
})();
