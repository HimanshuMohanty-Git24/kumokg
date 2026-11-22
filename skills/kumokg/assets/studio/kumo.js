// KumoKG Studio shared module: API + SSE helpers, DOM builder, class palette, evidence renderer, popovers, telemetry.

export async function api(path, opts = {}) {
  const init = { headers: { "Accept": "application/json" }, ...opts };
  if (opts.json !== undefined) {
    init.method = init.method || "POST";
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.json);
    delete init.json;
  }
  const res = await fetch(path, init);
  let data;
  try { data = await res.json(); } catch { data = { ok: false, error: `HTTP ${res.status}` }; }
  if (!res.ok || data.ok === false) {
    const err = new Error(data.error || `HTTP ${res.status}`);
    err.data = data;
    err.status = res.status;
    throw err;
  }
  return data;
}

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "style" && typeof v === "object") {
      for (const [sk, sv] of Object.entries(v)) {
        if (sk.startsWith("--")) node.style.setProperty(sk, sv);
        else node.style[sk] = sv;
      }
    } else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else if (k === "html") node.innerHTML = v;
    else if (v === true) node.setAttribute(k, "");
    else node.setAttribute(k, v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === undefined || c === null || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

export function toast(msg, bad = false, ms = 2800) {
  const t = el("div", { class: "toast" + (bad ? " bad" : ""), role: "status" }, msg);
  document.body.append(t);
  setTimeout(() => t.remove(), ms);
}

export function debounce(fn, ms = 200) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

// ------------------------------------------------------------------------------------------ class palette
export const PALETTE = ["#4c7be0", "#2f9e6e", "#d0574d", "#c2902a", "#8a63d2", "#1f9bb0", "#c45391", "#6f7d8f",
  "#7f9a2a", "#d07a2c", "#4f93c9", "#9b6a4a"];
const classColors = new Map();

export function setClasses(names) {
  [...new Set(names)].sort().forEach((n) => { if (!classColors.has(n)) classColors.set(n, PALETTE[classColors.size % PALETTE.length]); });
}

export function classColor(name) {
  if (!classColors.has(name)) classColors.set(name, PALETTE[classColors.size % PALETTE.length]);
  return classColors.get(name);
}

export function clsBadge(name) {
  return el("span", { class: "cls", style: { "--c": classColor(name) }, title: `class ${name}` }, name);
}

export function colorMap(keys) {
  setClasses(keys);
  return new Map([...keys].map(k => [k, classColor(k)]));
}

export function fmtPct(v) { return v === null || v === undefined ? "–" : `${Number(v).toFixed(v >= 99.95 || v === 0 ? 0 : 1)}%`; }

export function shortDoc(name, max = 22) {
  if (!name || name.length <= max) return name || "";
  const dot = name.lastIndexOf(".");
  const ext = dot > 0 ? name.slice(dot + 1) : "";
  return name.slice(0, Math.max(6, max - ext.length - 2)).trimEnd() + "…" + ext;
}

export function triple(src, pred, tgt, srcLabel, tgtLabel) {
  const ent = (name, label) => label ? el("span", { class: "row", style: { gap: "5px" } }, el("span", { class: "ent" }, name), clsBadge(label)) : el("span", { class: "ent" }, name);
  return el("div", { class: "triple" }, ent(src, srcLabel), el("span", { class: "arrow" }, "→"), el("span", { class: "pred" }, pred),
    el("span", { class: "arrow" }, "→"), ent(tgt, tgtLabel));
}

// ------------------------------------------------------------------------------------------ reason tags
const TAG_RULES = [
  [/conflict resolved/i, "Conflict resolved", "ok"],
  [/functional/i, "Functional conflict", "warn"],
  [/relation grammar|unknown class|not defined on/i, "Schema extension", "violet"],
  [/fuzzy|soft/i, "Fuzzy quote", "warn"],
  [/visual/i, "Visual check", "accent"],
  [/confidence ([\d.]+)/i, (m) => `Low confidence ${m[1]}`, "neutral"],
  [/entity resolution/i, "Ambiguous entity", "warn"],
  [/calibration: rejected/i, "Previously rejected", "bad"],
  [/direction flipped/i, "Auto-flipped", "accent"],
  [/some evidence dropped/i, "Partial evidence", "neutral"],
  [/self-loop/i, "Self-loop", "warn"],
  [/not found|ungrounded|no evidence|pronoun/i, "Ungrounded", "bad"],
  [/defined .* -> .*; direction looks reversed/i, "Reversed direction", "warn"],
];

export function reasonTag(reason) {
  for (const [rx, label, cls] of TAG_RULES) {
    const m = reason.match(rx);
    if (m) return el("span", { class: `tag ${cls}`, title: reason }, typeof label === "function" ? label(m) : label);
  }
  return el("span", { class: "tag neutral", title: reason }, reason.length > 28 ? reason.slice(0, 26) + "…" : reason);
}

export function reasonTags(reasons) {
  const seen = new Set();
  return reasons.map(reasonTag).filter(t => { const k = t.textContent; if (seen.has(k)) return false; seen.add(k); return true; });
}

// ------------------------------------------------------------------------------------------ quote formatting
const escRx = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

// Display-only formatting: collapse whitespace and turn Gutenberg "* * * *" runs into a quiet divider.
// The stored quote is never modified.
export function prettyText(text, surfaces = null) {
  const flat = (text || "").replace(/\s+/g, " ");
  const out = [];
  const parts = flat.split(/((?:\*\s*){3,})/);
  for (const part of parts) {
    if (/^(?:\*\s*){3,}$/.test(part)) { out.push(el("span", { class: "divider", "aria-label": "section break" }, "⁂")); continue; }
    out.push(...highlightEntities(part, surfaces));
  }
  return out;
}

export function highlightEntities(text, surfaces) {
  if (!surfaces || !text) return [text];
  const forms = [];
  for (const s of surfaces.source || []) forms.push([s, "ent-s"]);
  for (const t of surfaces.target || []) forms.push([t, "ent-o"]);
  if (!forms.length) return [text];
  forms.sort((a, b) => b[0].length - a[0].length);
  const rx = new RegExp(`(?<![A-Za-z0-9])(${forms.map(f => escRx(f[0]).replace(/\s+/g, "\\s+")).join("|")})(?![A-Za-z0-9])`, "gi");
  const lower = new Map(forms.map(([f, c]) => [f.toLowerCase().replace(/\s+/g, " "), c]));
  const out = [];
  let last = 0;
  for (const m of text.matchAll(rx)) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const cls = lower.get(m[0].toLowerCase().replace(/\s+/g, " ")) || "ent-s";
    out.push(el("span", { class: cls, title: cls === "ent-s" ? "subject" : "object" }, m[0]));
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

const KIND_LABEL = { text: "TEXT", table: "TABLE", json: "JSON", notebook: "NOTEBOOK", image: "IMAGE", web: "WEB" };

function checkChip(check) {
  if (!check) return null;
  const cls = { verified: "ok", soft_verified: "warn", needs_visual: "accent", rejected: "bad" }[check.status] || "";
  return el("span", { class: `chip ${cls}`, title: check.detail || "" }, check.status.replace("_", " "));
}

export function renderEvidence(ev, surfaces = null) {
  const head = el("div", { class: "ev-head" },
    el("span", { class: "chip" }, KIND_LABEL[ev.kind] || ev.kind),
    el("span", { title: ev.source_name || ev.doc_id }, shortDoc(ev.source_name || ev.doc_id, 30)),
    ev.page_no ? el("span", { class: "chip" }, `p. ${ev.page_no}`) : null,
    ev.chunk_id ? el("button", { class: "copy", title: `chunk ${ev.chunk_id} — click to copy`, onclick: () => copy(ev.chunk_id) }, "id") : null,
    ev.citation && ev.kind !== "text" ? el("span", { class: "muted small mono" }, ev.citation) : null,
    el("span", { class: "spacer" }), checkChip(ev.check));
  const body = el("div", { class: "ev-body" });
  if (ev.kind === "text" || ev.kind === "web") {
    if (ev.context) {
      body.append(el("div", { class: "quote" }, ev.context.before ? ["…", prettyText(ev.context.before, surfaces)] : "",
        el("mark", {}, prettyText(ev.context.match, surfaces)), ev.context.after ? [prettyText(ev.context.after, surfaces), "…"] : ""));
    } else {
      body.append(el("div", { class: "quote" }, el("mark", {}, prettyText(ev.verbatim_quote, surfaces))));
    }
    if (ev.url) body.append(el("div", { class: "muted small mono", style: { marginTop: "6px" } }, ev.url));
  } else if (ev.kind === "table") {
    body.append(el("div", { class: "pointer", style: { marginBottom: "8px" } },
      el("span", {}, ev.sheet_or_table), el("i", {}, "row"), el("span", {}, ev.row_index), el("i", {}, "col"), el("span", {}, ev.column_name)));
    if (ev.row) {
      const tbl = el("table", { class: "grid-cells" },
        el("tr", {}, ev.row.columns.map(c => el("th", {}, c))),
        el("tr", {}, ev.row.values.map((v, i) => el("td", { class: ev.row.columns[i] === ev.column_name ? "hit" : "" }, v.length > 60 ? v.slice(0, 60) + "…" : v))));
      body.append(el("div", { class: "scroll-x" }, tbl));
    } else {
      body.append(el("div", {}, el("mark", {}, String(ev.cell_value))));
    }
  } else if (ev.kind === "json") {
    const parts = (ev.path || ev.json_pointer.split("/").slice(1));
    body.append(el("div", { class: "pointer", style: { marginBottom: "8px" } }, el("i", {}, "$"),
      parts.map((p, i) => [el("span", {}, decodeURIComponent(p)), i < parts.length - 1 ? el("i", {}, "/") : null])));
    body.append(el("div", { class: "row" }, el("span", { class: "muted small" }, "value"), el("mark", { class: "mono" }, JSON.stringify(ev.raw_value))));
    if (ev.parent) body.append(el("pre", { class: "code", style: { marginTop: "8px" } }, ev.parent));
  } else if (ev.kind === "notebook") {
    body.append(el("div", { class: "pointer", style: { marginBottom: "8px" } }, el("span", {}, `cell ${ev.cell_index}`),
      el("span", {}, ev.cell_type), el("span", {}, `lines ${ev.line_span[0]}–${ev.line_span[1]}`)));
    if (ev.cell_source) {
      const lines = ev.cell_source.split("\n");
      const pre = el("pre", { class: "code" });
      lines.forEach((ln, i) => {
        const n = i + 1;
        const hit = n >= ev.line_span[0] && n <= ev.line_span[1];
        pre.append(hit ? el("span", { class: "hl" }, ln) : ln, i < lines.length - 1 ? "\n" : "");
      });
      body.append(pre);
    } else {
      body.append(el("div", { class: "quote" }, el("mark", {}, ev.verbatim_quote)));
    }
  } else if (ev.kind === "image") {
    const url = ev.media_url || `/media/${ev.image_path}`;
    const box = el("div", { class: "thumb" }, el("img", { src: url, alt: ev.element_description, loading: "lazy" }));
    if (ev.bbox_norm) {
      const [x0, y0, x1, y1] = ev.bbox_norm;
      box.append(el("div", { class: "bbox", style: { left: `${x0 * 100}%`, top: `${y0 * 100}%`, width: `${(x1 - x0) * 100}%`, height: `${(y1 - y0) * 100}%` } }));
    }
    body.append(box, el("div", { style: { marginTop: "6px" } }, el("span", { class: "muted small" }, "element: "), ev.element_description));
  }
  return el("div", { class: "ev" }, head, body);
}

export function copy(text) {
  try {
    navigator.clipboard.writeText(text).then(() => toast(`Copied ${text}`), () => toast(text));
  } catch {
    toast(text);
  }
}

// ------------------------------------------------------------------------------------------ popover
let openPop = null;

export function closePopover() {
  if (openPop) { openPop.remove(); openPop = null; }
}

export function popover(anchor, content, { align = "start" } = {}) {
  closePopover();
  const pop = el("div", { class: "popover", role: "dialog" }, content);
  document.body.append(pop);
  const r = anchor.getBoundingClientRect();
  const w = pop.offsetWidth;
  let left = align === "end" ? r.right - w : r.left;
  left = Math.max(12, Math.min(left, window.innerWidth - w - 12));
  pop.style.left = `${left}px`;
  pop.style.top = `${r.bottom + 8}px`;
  openPop = pop;
  setTimeout(() => {
    const away = (e) => {
      if (!pop.contains(e.target) && !anchor.contains(e.target)) { closePopover(); document.removeEventListener("mousedown", away); }
    };
    document.addEventListener("mousedown", away);
  }, 0);
  return pop;
}

document.addEventListener("keydown", (e) => { if (e.key === "Escape") closePopover(); });

// ------------------------------------------------------------------------------------------ SSE + topbar
export function connectSSE(badge, onEvent) {
  let es;
  let retry = 1000;
  const set = (state, text) => {
    if (!badge) return;
    badge.className = `badge ${state}`;
    badge.replaceChildren(el("span", { class: "dot" }), text);
  };
  const open = () => {
    es = new EventSource("/api/events");
    es.addEventListener("hello", () => { retry = 1000; set("live", "Live"); });
    const handler = (e) => {
      let data = {};
      try { data = JSON.parse(e.data); } catch { /* keep-alive comment */ }
      set("live flash", "Live · updated");
      setTimeout(() => badge && badge.classList.remove("flash"), 900);
      onEvent && onEvent(data);
    };
    ["graph_changed", "gate1_approved", "message"].forEach(t => es.addEventListener(t, handler));
    es.onerror = () => {
      set("down", "Reconnecting…");
      es.close();
      setTimeout(open, retry);
      retry = Math.min(retry * 2, 15000);
    };
  };
  if (new URLSearchParams(location.search).has("snapshot")) {
    set("", "Snapshot");  // static capture mode (headless screenshots): no long-lived stream
    return () => {};
  }
  set("", "Connecting…");
  open();
  return () => es && es.close();
}

export async function initTopbar(active) {
  document.querySelectorAll(".nav a").forEach(a => a.classList.toggle("active", a.dataset.page === active));
  try {
    const st = await api("/api/status");
    const chip = document.getElementById("ws-chip");
    if (chip) {
      chip.textContent = st.workspace_name || st.workspace;
      chip.title = `${st.workspace}\nphase ${st.phase} · ${st.mode} / ${st.tier}`;
    }
    setClasses(st.classes || []);
    return st;
  } catch (e) {
    return null;
  }
}

// ------------------------------------------------------------------------------------------ telemetry (local only)
export function clientLog(page, event, extra = {}) {
  try {
    fetch("/api/client-log", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ page, event, level: extra.level || "info", ...extra }) }).catch(() => {});
  } catch { /* never let telemetry break the page */ }
}

export function trapErrors(page) {
  window.addEventListener("error", (e) => clientLog(page, "error", { level: "error", message: String(e.message), stack: e.error && e.error.stack }));
  window.addEventListener("unhandledrejection", (e) => clientLog(page, "error", { level: "error", message: String(e.reason && e.reason.message || e.reason), stack: e.reason && e.reason.stack }));
}
