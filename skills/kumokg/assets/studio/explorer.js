// Explorer: live graph canvas, search + PPR paths, floating timeline, layouts, provenance drawer, health, export.
import { api, el, $, toast, connectSSE, initTopbar, renderEvidence, triple, colorMap, classColor, fmtPct, PALETTE,
  popover, closePopover, clientLog, trapErrors, debounce, clsBadge } from "/assets/kumo.js";
import { GraphCanvas } from "/assets/graph-canvas.js";

trapErrors("explorer");

let view = null;
let doctorCard = null;
let mode = "local";
let colorBy = "label";
const hiddenLabels = new Set();
const hiddenMods = new Set();
let breakpoints = [];
let asOf = null;
let selectedAdapter = null;
let lastSearch = null;
let playTimer = null;
let reported = false;

// replaceChildren() stringifies arrays and nulls ("[object HTMLDivElement]", "null"); flatten and skip empties like el() does.
const fill = (node, ...children) => node.replaceChildren(...children.flat(Infinity).filter(c => c !== undefined && c !== null && c !== false));

const reducedMotion = new URLSearchParams(location.search).has("snapshot") || matchMedia("(prefers-reduced-motion: reduce)").matches;
const gc = new GraphCanvas($("#graph"), { onSelect: (s) => openDrawer(s), instant: reducedMotion });

function nodeColor(n) {
  if (colorBy === "community") return n.community >= 0 ? PALETTE[n.community % PALETTE.length] : "#9aa3b0";
  return classColor(n.label);
}

function isVisible(n) {
  if (hiddenLabels.has(n.label)) return false;
  if (hiddenMods.size && n.modalities.length && n.modalities.every(m => hiddenMods.has(m))) return false;
  return true;
}

// ------------------------------------------------------------------------------------------ data
async function loadGraph() {
  const params = new URLSearchParams();
  if (asOf) params.set("as_of", asOf);
  if ($("#superseded").checked) params.set("include_superseded", "1");
  view = await api(`/api/graph?${params}`);
  colorMap(view.labels);
  breakpoints = view.temporal_breakpoints;
  gc.setData(view.nodes, view.edges);
  gc.setColor(nodeColor);
  gc.setVisible(isVisible);
  const active = view.edges.filter(e => e.active !== false).length;
  const inactive = view.edges.length - active;
  $("#counts").textContent = `${view.nodes.length} nodes · ${active} active` + (inactive ? ` · ${inactive} superseded` : "");
  renderLegend();
  renderTimeline();
  if (lastSearch) runSearch(lastSearch, true);
  if (!reported) {
    reported = true;
    setTimeout(() => clientLog("explorer", "render", { stats: gc.labelStats() }), 1200);
  }
}

const reloadGraph = debounce(() => { loadGraph(); loadHealth(); }, 250);

function renderLegend() {
  const counts = {};
  view.nodes.forEach(n => { counts[n.label] = (counts[n.label] || 0) + 1; });
  const box = $("#labels");
  box.innerHTML = "";
  if (colorBy === "community") {
    const comms = {};
    view.nodes.forEach(n => { if (n.community >= 0) comms[n.community] = (comms[n.community] || 0) + 1; });
    Object.entries(comms).sort((a, b) => b[1] - a[1]).slice(0, 12).forEach(([c, n]) => box.append(el("div", { class: "legend-row" },
      el("span", { class: "swatch", style: { background: PALETTE[c % PALETTE.length] } }), `Community ${c}`, el("span", { class: "n" }, n))));
    box.append(el("div", { class: "tiny muted", style: { marginTop: "4px" } }, "Filter by class:"));
  }
  view.labels.forEach(l => box.append(el("label", { class: "legend-row check" },
    el("input", { type: "checkbox", checked: !hiddenLabels.has(l), onchange: (e) => { e.target.checked ? hiddenLabels.delete(l) : hiddenLabels.add(l); gc.setVisible(isVisible); } }),
    clsBadge(l), el("span", { class: "n" }, counts[l] || 0))));
  const mbox = $("#modalities");
  mbox.innerHTML = "";
  view.modalities.forEach(m => mbox.append(el("label", { class: "check small chip" },
    el("input", { type: "checkbox", checked: !hiddenMods.has(m), onchange: (e) => { e.target.checked ? hiddenMods.delete(m) : hiddenMods.add(m); gc.setVisible(isVisible); } }), m)));
}

// ------------------------------------------------------------------------------------------ health pill
async function loadHealth() {
  try {
    doctorCard = await api("/api/doctor");
    const d = doctorCard;
    const score = Number(d.score);
    const cls = score >= 95 ? "ok" : score >= 80 ? "warn" : "bad";
    $("#health-pill").replaceChildren(`${d.nodes} nodes · ${d.edges.total} edges · Health `,
      el("span", { class: `score ${cls}` }, score.toFixed(score % 1 ? 1 : 0)), " ▾");
  } catch (e) {
    $("#health-pill").textContent = "health unavailable";
  }
}

function healthPopover() {
  const d = doctorCard;
  if (!d) return;
  const metric = (k, v, cls = "") => el("div", { class: "metric" }, el("span", {}, k), el("span", { class: `v ${cls}` }, v));
  const g = d.grounding.pct, s = d.schema_compliance.pct;
  const content = el("div", {},
    el("div", { class: "row", style: { marginBottom: "6px" } }, el("h3", {}, "Graph health scorecard"), el("span", { class: "spacer" }),
      el("span", { class: "mono small muted" }, d.graph_hash)),
    metric("Health score", Number(d.score).toFixed(1), d.score >= 95 ? "ok" : "warn"),
    metric("Quote grounding (committed edges)", fmtPct(g), g >= 100 ? "ok" : g >= 90 ? "warn" : "bad"),
    metric("Schema compliance", fmtPct(s), s >= 100 ? "ok" : "warn"),
    metric("Contradictions", d.contradiction_count, d.contradiction_count ? "bad" : "ok"),
    metric("Orphan nodes · dangling edges", `${d.orphan_nodes.length} · ${d.dangling_edges.length}`, d.orphan_nodes.length || d.dangling_edges.length ? "warn" : "ok"),
    metric("Communities", d.communities),
    metric("Edges active · superseded", `${d.edges.active} · ${d.edges.superseded}`),
    metric("Review queue (undecided)", d.review_queue.undecided_proposals, d.review_queue.undecided_proposals ? "warn" : ""),
    d.contradictions.length ? el("div", { style: { marginTop: "8px" } }, el("h3", {}, "Open contradictions"),
      d.contradictions.slice(0, 4).map(c => el("div", { class: "item small" }, el("b", {}, c.source_name), " ", el("span", { class: "pred" }, c.predicate),
        el("span", { class: "muted" }, " · " + c.values.map(v => v.target_name || v.value).join(" vs ")),
        el("div", {}, el("a", { href: "/gate2" }, "Resolve at Gate 2 →"))))) : null);
  popover($("#health-pill"), content, { align: "end" });
}

// ------------------------------------------------------------------------------------------ timeline
function fmtPoint(t) {
  const [date, time] = String(t).split("T");
  return { date, time: time ? time.slice(0, 5) : null };
}

function renderTimeline() {
  const track = $("#track");
  track.querySelectorAll(".mk").forEach(m => m.remove());
  const points = [...breakpoints, null];
  const idx = asOf === null ? points.length - 1 : Math.max(0, breakpoints.indexOf(asOf));
  const sameDate = breakpoints.length && breakpoints.every(b => fmtPoint(b).date === fmtPoint(breakpoints[0]).date);
  points.forEach((p, i) => {
    const left = points.length === 1 ? 50 : 4 + (i / (points.length - 1)) * 92;
    const f = p ? fmtPoint(p) : null;
    const label = p === null ? "Now" : sameDate && f.time ? f.time : f.date + (f.time ? ` ${f.time}` : "");
    track.append(el("button", { type: "button", class: `mk${i === idx ? " on" : ""}${i < idx ? " past" : ""}`, style: { left: `${left}%` },
      title: p === null ? "Current state" : `as of ${p}`, onclick: () => setAsOf(p) }, el("span", {}, label)));
  });
  $("#rail-fill").style.width = points.length === 1 ? "100%" : `${(idx / (points.length - 1)) * 100}%`;
  const f = asOf ? fmtPoint(asOf) : null;
  $("#asof-label").textContent = asOf ? `Time travel · ${f.time || f.date}` : "Time travel · now";
  $("#asof-date").textContent = sameDate && breakpoints.length ? fmtPoint(breakpoints[0]).date : (asOf ? f.date : `${breakpoints.length} change points`);
}

function setAsOf(p) {
  asOf = p;
  renderTimeline();
  loadGraph();
}

function togglePlay() {
  if (playTimer) { clearInterval(playTimer); playTimer = null; $("#play").textContent = "▶"; return; }
  const points = [...breakpoints, null];
  let i = asOf === null ? 0 : breakpoints.indexOf(asOf) + 1;
  if (asOf === null) setAsOf(points[0]);
  $("#play").textContent = "❚❚";
  playTimer = setInterval(() => {
    if (i >= points.length) { togglePlay(); return; }
    setAsOf(points[i]);
    i += 1;
  }, 1400);
}

// ------------------------------------------------------------------------------------------ search
async function runSearch(q, quiet = false) {
  lastSearch = q;
  const params = new URLSearchParams({ q, mode, max_tokens: "1500" });
  if (asOf) params.set("as_of", asOf);
  const box = $("#results");
  if (!quiet) box.replaceChildren(el("div", { class: "muted small" }, "Searching…"));
  try {
    const r = await api(`/api/search?${params}`);
    gc.setHighlight({ nodes: r.nodes.map(n => n.id).concat(r.seeds.map(s => s.id)), edges: r.edges.map(e => e.id), path: r.path_edges || [] });
    const names = new Map(view.nodes.map(n => [n.id, n.name]));
    fill(box,
      el("div", { class: "row small muted" }, el("span", { class: "chip" }, r.mode), `${r.edges.length} facts · ~${r.tokens_estimate} tokens · ${r.latency_ms} ms`),
      (r.paths || []).length ? el("div", { class: "stack", style: { gap: "4px" } }, el("h3", {}, "Multi-hop paths"),
        r.paths.slice(0, 4).map(p => el("div", { class: "result mono small", onclick: () => gc.focus(p[p.length - 1]) }, p.map(id => names.get(id) || id).join(" → ")))) : null,
      el("h3", {}, "Facts"),
      r.edges.slice(0, 12).map(e => el("div", { class: "result", onclick: () => { gc.select({ kind: "edge", id: e.id }); openDrawer({ kind: "edge", id: e.id }); } },
        triple(e.source_name, e.type, e.target_name), e.quote ? el("div", { class: "muted small", style: { marginTop: "3px" } }, `“${e.quote.replace(/\s+/g, " ")}”`) : null,
        el("div", { class: "mono tiny muted" }, e.citation || ""))),
      !r.edges.length ? el("div", { class: "muted small" }, "No grounded facts matched.") : null);
  } catch (e) {
    box.replaceChildren(el("div", { class: "small", style: { color: "var(--bad)" } }, e.message));
  }
}

// ------------------------------------------------------------------------------------------ drawer
const isEmpty = (v) => v === null || v === undefined || v === "" || (Array.isArray(v) && !v.length)
  || (typeof v === "object" && !Array.isArray(v) && !Object.keys(v).length);

async function openDrawer(sel) {
  if (!sel) { closeDrawer(); return; }
  const drawer = $("#drawer");
  drawer.classList.add("open");
  drawer.setAttribute("aria-hidden", "false");
  const body = $("#drawer-body");
  body.replaceChildren(el("div", { class: "muted" }, "Loading provenance…"));
  try {
    const d = await api(`/api/evidence/${encodeURIComponent(sel.id)}`);
    $("#drawer-title").textContent = d.kind === "node" ? d.name : d.type;
    const names = new Map(view.nodes.map(n => [n.id, n]));
    const head = d.kind === "node"
      ? el("dl", { class: "kv" }, el("dt", {}, "class"), el("dd", {}, clsBadge(d.label)),
          d.aliases && d.aliases.length ? [el("dt", {}, "aliases"), el("dd", {}, d.aliases.join(", "))] : null,
          Object.entries(d.properties || {}).filter(([, v]) => !isEmpty(v))
            .map(([k, v]) => [el("dt", {}, k), el("dd", { class: "mono small" }, typeof v === "object" ? JSON.stringify(v) : String(v))]),
          el("dt", {}, "id"), el("dd", { class: "mono small" }, d.id))
      : el("div", { class: "stack" }, triple(d.source_name, d.type, d.target_name, names.get(d.source)?.label, names.get(d.target)?.label),
          el("dl", { class: "kv" }, el("dt", {}, "status"), el("dd", {}, el("span", { class: `tag ${d.status === "approved" ? "ok" : "violet"}` }, d.status)),
            el("dt", {}, "valid"), el("dd", { class: "mono small" }, `${d.valid_from || "—"} → ${d.valid_to || "open"}`),
            el("dt", {}, "confidence"), el("dd", { class: "mono small" }, Number(d.confidence).toFixed(3)),
            d.superseded_by ? [el("dt", {}, "superseded by"), el("dd", {}, el("a", { href: "#", onclick: (e) => { e.preventDefault(); openDrawer({ kind: "edge", id: d.superseded_by }); } }, d.superseded_by))] : null,
            el("dt", {}, "id"), el("dd", { class: "mono small" }, d.id)));
    const incident = d.kind === "node" ? view.edges.filter(e => e.source === d.id || e.target === d.id) : [];
    const neighbours = incident.slice(0, 60);
    fill(body, head,
      neighbours.length ? el("div", { class: "stack", style: { gap: "5px" } },
        el("h3", {}, neighbours.length < incident.length ? `Relations (${neighbours.length} of ${incident.length})` : `Relations (${incident.length})`),
        incident.length > gc.egoLabelMax ? el("div", { class: "muted small" }, "Hub node: predicates are listed here instead of on the canvas.") : null,
        neighbours.map(e => el("div", { class: "result", onclick: () => { gc.select({ kind: "edge", id: e.id }); openDrawer({ kind: "edge", id: e.id }); } },
          triple(names.get(e.source)?.name || e.source, e.type, names.get(e.target)?.name || e.target)))) : null,
      el("h3", {}, `Evidence (${d.evidence.length})`),
      d.evidence.length ? d.evidence.map(ev => renderEvidence(ev, d.surfaces)) : el("div", { class: "muted small" }, "No evidence recorded."),
      d.history && d.history.length ? el("div", { class: "stack", style: { gap: "4px" } }, el("h3", {}, "Change history"),
        d.history.slice().reverse().map(h => el("div", { class: "small" }, el("span", { class: "mono muted" }, h.ts.replace("T", " ").replace("Z", "")), " · ",
          el("b", {}, h.op), " · ", h.author, h.message ? el("div", { class: "muted" }, h.message) : null))) : null);
  } catch (e) {
    body.replaceChildren(el("div", { class: "muted" }, e.message));
  }
}

function closeDrawer() {
  $("#drawer").classList.remove("open");
  $("#drawer").setAttribute("aria-hidden", "true");
  gc.select(null);
}

// ------------------------------------------------------------------------------------------ export
async function openExport() {
  $("#export-modal").classList.remove("hidden");
  const grid = $("#adapters");
  try {
    const { adapters } = await api("/api/adapters");
    grid.replaceChildren(...adapters.map(a => el("div", { class: "adapter" + (selectedAdapter === a.name ? " on" : ""), tabindex: "0", role: "button",
      onclick: () => { selectedAdapter = a.name; openExport(); }, onkeydown: (e) => { if (e.key === "Enter") { selectedAdapter = a.name; openExport(); } } },
      el("b", {}, a.name), el("span", { class: "small" }, a.title), el("span", { class: "muted tiny" }, a.formats.join(", ")))));
    $("#pg-mode").disabled = selectedAdapter !== "postgres";
    $("#nep-mode").disabled = selectedAdapter !== "neptune";
  } catch (e) {
    grid.replaceChildren(el("div", { class: "muted" }, e.message));
  }
}

async function runExport() {
  if (!selectedAdapter) return toast("Pick a target first");
  const out = $("#export-result");
  out.replaceChildren(el("div", { class: "muted small" }, `Exporting to ${selectedAdapter}…`));
  const options = {};
  if (selectedAdapter === "postgres") options.pg_mode = $("#pg-mode").value;
  if (selectedAdapter === "neptune") options.neptune_mode = $("#nep-mode").value;
  try {
    const r = await api("/api/export", { json: { target: selectedAdapter, provenance_mode: $("#prov").value, options } });
    out.replaceChildren(renderExport(r));
  } catch (e) {
    out.replaceChildren(e.data && e.data.parity ? renderExport(e.data) : el("div", { class: "small", style: { color: "var(--bad)" } }, e.message));
  }
}

function renderExport(r) {
  const p = r.parity;
  return el("div", { class: "stack" },
    el("div", { class: "row wrap" }, el("span", { class: `tag ${p.ok ? "ok" : "bad"}` }, p.ok ? "Parity verified" : "Parity failed"),
      el("span", { class: "mono small" }, `nodes ${p.nodes_found}/${p.nodes_expected} · edges ${p.edges_found}/${p.edges_expected} · dangling ${p.dangling}`),
      el("span", { class: "mono tiny muted" }, r.out_dir)),
    p.errors && p.errors.length ? el("div", { class: "small", style: { color: "var(--bad)" } }, p.errors.join("; ")) : null,
    el("table", { class: "data" }, el("thead", {}, el("tr", {}, el("th", {}, "file"), el("th", {}, "bytes"), el("th", {}, "sha256"))),
      el("tbody", {}, r.files.map(f => el("tr", {}, el("td", { class: "mono small" }, f.path), el("td", { class: "mono small" }, f.bytes), el("td", { class: "mono tiny muted" }, f.sha256.slice(0, 16) + "…"))))));
}

// ------------------------------------------------------------------------------------------ wiring
$("#search-form").addEventListener("submit", (e) => { e.preventDefault(); const q = $("#q").value.trim(); if (q) runSearch(q); });
$("#mode").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  mode = b.dataset.mode;
  document.querySelectorAll("#mode button").forEach(x => x.classList.toggle("on", x === b));
  if (lastSearch) runSearch(lastSearch);
});
$("#clear-search").addEventListener("click", () => { lastSearch = null; $("#q").value = ""; $("#results").innerHTML = ""; gc.setHighlight({}); });
$("#colorby").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  colorBy = b.dataset.c;
  document.querySelectorAll("#colorby button").forEach(x => x.classList.toggle("on", x === b));
  gc.setColor(nodeColor); renderLegend();
});
$("#layout-mode").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  document.querySelectorAll("#layout-mode button").forEach(x => x.classList.toggle("on", x === b));
  gc.setLayout(b.dataset.l);
  setTimeout(() => clientLog("explorer", "layout", { stats: { layout: b.dataset.l, ...gc.labelStats() } }), 1500);
});
$("#edge-labels").addEventListener("click", (e) => {
  const on = e.currentTarget.getAttribute("aria-pressed") !== "true";
  e.currentTarget.setAttribute("aria-pressed", String(on));
  e.currentTarget.classList.toggle("primary", on);
  e.currentTarget.classList.toggle("ghost", !on);
  gc.setEdgeLabels(on);
});
$("#superseded").addEventListener("change", () => loadGraph());
$("#play").addEventListener("click", togglePlay);
$("#zoom-in").addEventListener("click", () => gc.zoom(1.25));
$("#zoom-out").addEventListener("click", () => gc.zoom(0.8));
$("#fit").addEventListener("click", () => gc.fit());
$("#close-drawer").addEventListener("click", closeDrawer);
$("#health-pill").addEventListener("click", healthPopover);
$("#open-export").addEventListener("click", openExport);
$("#close-export").addEventListener("click", () => $("#export-modal").classList.add("hidden"));
$("#export-modal").addEventListener("click", (e) => { if (e.target.id === "export-modal") $("#export-modal").classList.add("hidden"); });
$("#run-export").addEventListener("click", runExport);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { $("#export-modal").classList.add("hidden"); closeDrawer(); closePopover(); }
  if (e.key === "/" && document.activeElement?.tagName !== "INPUT") { e.preventDefault(); $("#q").focus(); }
});

// Deep links: /explorer?layout=radial|clusters&labels=1&as_of=<instant>&superseded=1&select=<node id>&q=<question>&mode=ppr
async function applyDeepLink() {
  const qp = new URLSearchParams(location.search);
  const layout = qp.get("layout");
  if (layout && ["force", "radial", "clusters"].includes(layout)) {
    const b = document.querySelector(`#layout-mode button[data-l="${layout}"]`);
    if (b) b.click();
  }
  if (qp.get("labels") === "1") $("#edge-labels").click();
  const select = qp.get("select");
  if (select && gc.byId.has(select)) { gc.select({ kind: "node", id: select }); openDrawer({ kind: "node", id: select }); }
  if (qp.get("mode") === "ppr") document.querySelector('#mode button[data-mode="ppr"]').click();
  if (qp.get("q")) { $("#q").value = qp.get("q"); runSearch(qp.get("q")); }
}

initTopbar("explorer").then(async () => {
  const qp = new URLSearchParams(location.search);
  if (qp.get("as_of")) asOf = qp.get("as_of");
  if (qp.get("superseded") === "1") $("#superseded").checked = true;
  loadHealth();
  try {
    await loadGraph();
    await applyDeepLink();
  } catch (e) {
    $("#graph").replaceWith(el("div", { class: "empty" }, el("h2", {}, "No committed graph yet"), el("p", {}, e.message),
      el("p", { class: "mono small" }, "Review proposals at Gate 2, then commit.")));
  }
});
connectSSE($("#sse"), (ev) => {
  if (ev.type === "graph_changed") { reloadGraph(); toast(`Graph updated (${ev.reason || "change"})`); }
});
