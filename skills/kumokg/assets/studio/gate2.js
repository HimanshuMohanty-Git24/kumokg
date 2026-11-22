// Gate 2: full-height review workbench with an evidence inspector, contradiction resolver and keyboard workflow.
import { api, el, $, toast, connectSSE, initTopbar, renderEvidence, triple, debounce, reasonTags, clsBadge, shortDoc, copy,
  clientLog, trapErrors } from "/assets/kumo.js";

trapErrors("gate2");

const TABS = [
  { id: "conflict", label: "Conflicts" },
  { id: "review", label: "Needs review" },
  { id: "auto", label: "Auto-verified" },
  { id: "quarantine", label: "Quarantined" },
];
let data = null;
let tab = "conflict";
let sel = 0;
const local = new Map(); // proposal_id -> {decision, edit}
const detailCache = new Map();
let visible = [];
let inspectToken = 0;
let seqOpen = null;

const toPred = (s) => (s || "").replace(/[^A-Za-z0-9]+/g, "_").replace(/^_+|_+$/g, "").toUpperCase();
const ICON = { approve: "✓", reject: "✕", conflict: "!", superseded: "⇢", none: "" };
const ICON_TITLE = { approve: "approved", reject: "rejected", conflict: "unresolved functional conflict", superseded: "committed, later superseded (history)", none: "undecided" };

function savedDecision(p) { return p.decision ? p.decision.decision : null; }

function stateOf(p) {
  const l = local.get(p.proposal_id);
  if (l) return { kind: l.decision === "approve" ? "approve" : "reject", unsaved: true };
  if (p.open_conflict) return { kind: "conflict" };
  if (p.graph_status === "superseded") return { kind: "superseded" };
  const d = savedDecision(p);
  if (d === "reject") return { kind: "reject" };
  if (d === "approve" || d === "resolved" || p.graph_status === "approved") return { kind: "approve" };
  return { kind: "none" };
}

// A contradiction settled in the graph (kept value or temporal sequence) no longer reads as an open conflict.
function reasonsOf(p) {
  return p.open_conflict ? p.reasons : p.reasons.map(r => (/contradicts another value/i.test(r) ? "conflict resolved in the graph" : r));
}

function filtered() {
  const t = $("#f-text").value.trim().toLowerCase();
  const pred = $("#f-pred").value, doc = $("#f-doc").value, mod = $("#f-mod").value;
  const minc = parseFloat($("#f-conf").value);
  return data.proposals.filter(p => p.bucket === tab
    && (!pred || p.predicate === pred) && (!doc || p.docs.includes(doc)) && (!mod || p.modalities.includes(mod))
    && p.confidence >= minc
    && (!t || `${p.source.name} ${p.predicate} ${p.target.name} ${p.quote}`.toLowerCase().includes(t)));
}

// ------------------------------------------------------------------------------------------ header
function renderProgress() {
  const total = data.proposals.length;
  const decided = data.decided;
  const unsaved = local.size;
  const bar = $("#progress");
  bar.querySelector("i").style.width = `${total ? (decided / total) * 100 : 0}%`;
  const b = bar.querySelector("b");
  b.style.left = `${total ? (decided / total) * 100 : 0}%`;
  b.style.width = `${total ? (Math.min(unsaved, total - decided) / total) * 100 : 0}%`;
  $("#progress-label").replaceChildren(el("b", {}, `${decided} / ${total}`), " proposals decided",
    el("span", { class: "sep-dot" }, unsaved ? el("span", { style: { color: "var(--warn)" } }, `${unsaved} unsaved`) : "0 unsaved"));
  const open = data.contradictions.filter(c => !c.resolved).length;
  $("#progress-caption").textContent = `from ${data.summary.candidates} raw mentions · auto-approve ≥ ${Number(data.threshold).toFixed(2)}` +
    (open ? ` · ${open} open conflict${open > 1 ? "s" : ""}` : "");
}

function renderTabs() {
  const counts = {};
  for (const p of data.proposals) counts[p.bucket] = (counts[p.bucket] || 0) + 1;
  $("#tabs").replaceChildren(...TABS.map(t => el("button", { class: tab === t.id ? "on" : "", role: "tab", "aria-selected": String(tab === t.id),
    onclick: () => { tab = t.id; sel = 0; seqOpen = null; renderAll(); } }, t.label, " ", el("span", { class: "count" }, counts[t.id] || 0))));
  $("#bulk").disabled = tab === "quarantine" || tab === "conflict";
}

// ------------------------------------------------------------------------------------------ queue
function statusBox(p) {
  const s = stateOf(p);
  return el("div", { class: `st ${s.kind}${s.unsaved ? " unsaved" : ""}`, title: ICON_TITLE[s.kind] + (s.unsaved ? " (unsaved)" : "") }, ICON[s.kind]);
}

function row(p) {
  const l = local.get(p.proposal_id);
  const pred = l && l.edit && l.edit.predicate ? l.edit.predicate : p.predicate;
  const flipped = l && l.edit && l.edit.flip;
  return el("div", { class: "item", role: "option", "data-id": p.proposal_id, onclick: () => { sel = visible.indexOf(p); seqOpen = null; markSel(p); inspect(p); } },
    statusBox(p),
    el("div", { style: { minWidth: 0 } },
      triple(flipped ? p.target.name : p.source.name, pred, flipped ? p.source.name : p.target.name),
      el("div", { class: "q" }, p.quote ? `“${p.quote.replace(/\s+/g, " ").replace(/(?:\*\s*){3,}/g, "⁂ ")}”` : ""),
      el("div", { class: "meta" },
        reasonTags(reasonsOf(p)).slice(0, 3),
        p.valid_from ? el("span", { class: "tag accent" }, `from ${p.valid_from.replace("T", " ")}`) : null,
        el("span", { class: "chip" }, `${p.evidence_count} ev · ${p.docs.length} src`),
        p.modalities.filter(m => m !== "text").map(m => el("span", { class: "chip" }, m)))),
    el("div", { style: { textAlign: "right" } }, el("div", { class: "mono small" }, p.confidence.toFixed(2)),
      el("div", { class: "conf" }, el("i", { style: { width: `${Math.round(p.confidence * 100)}%` } }))));
}

function renderList() {
  visible = filtered();
  if (sel >= visible.length) sel = Math.max(0, visible.length - 1);
  const list = $("#list");
  list.innerHTML = "";
  if (tab === "conflict" && data.contradictions.length) {
    const grouped = new Set();
    for (const c of data.contradictions) {
      const ids = new Set(c.values.map(v => v.ref));
      const rows = visible.filter(p => ids.has(p.proposal_id));
      if (!rows.length) continue;
      rows.forEach(p => grouped.add(p.proposal_id));
      list.append(el("div", { class: "group-head" + (c.resolved ? " done" : "") },
        el("b", {}, c.source_name), el("span", { class: "pred" }, c.predicate),
        el("span", { class: "muted" }, `${c.values.length} values · ${c.modalities.join(" vs ")}`), el("span", { class: "spacer" }),
        c.resolved ? el("span", { class: "tag ok" }, "Resolved") : el("span", { class: "tag warn" }, "Functional conflict")));
      rows.forEach(p => list.append(row(p)));
    }
    visible.filter(p => !grouped.has(p.proposal_id)).forEach(p => list.append(row(p)));
  } else {
    visible.forEach(p => list.append(row(p)));
  }
  if (!visible.length) list.append(el("div", { class: "empty" }, tab === "quarantine" ? "Nothing quarantined — no ungrounded claims." : "Nothing in this tab matches the filters."));
  const cur = visible[sel];
  markSel(cur);
  if (cur) inspect(cur);
  else $("#inspector").replaceChildren(el("div", { class: "empty" }, "Select a proposal to inspect its evidence."));
}

function markSel(cur) {
  document.querySelectorAll(".item").forEach(n => n.classList.toggle("sel", !!cur && n.dataset.id === cur.proposal_id));
  const n = cur && document.querySelector(`.item[data-id="${cur.proposal_id}"]`);
  if (n) n.scrollIntoView({ block: "nearest" });
}

// ------------------------------------------------------------------------------------------ inspector
async function inspect(p) {
  const token = ++inspectToken;
  const box = $("#inspector");
  let detail = detailCache.get(p.proposal_id);
  if (!detail) {
    box.replaceChildren(el("div", { class: "empty" }, "Loading evidence…"));
    try { detail = (await api(`/api/proposal/${encodeURIComponent(p.proposal_id)}`)).proposal; } catch (e) { box.replaceChildren(el("div", { class: "empty" }, e.message)); return; }
    detailCache.set(p.proposal_id, detail);
  }
  if (token !== inspectToken) return;
  try {
    renderInspector(box, p, detail);
  } catch (err) {
    console.error(err);
    clientLog("gate2", "error", { level: "error", message: String(err.message), stack: err.stack });
    box.replaceChildren(el("div", { class: "empty" }, `Could not render the evidence inspector: ${err.message}`));
  }
}

// The committed edge's interval wins over the claim's once the proposal is in the graph.
function validity(p, ge) {
  const fmt = (x) => (x ? x.replace("T", " ") : null);
  const from = fmt(ge ? ge.valid_from : p.valid_from) || "—";
  const to = fmt(ge ? ge.valid_to : p.valid_to) || "open";
  return `${from} → ${to}` + (ge ? (ge.status === "superseded" ? " · superseded" : " · committed") : "");
}

function valueLabel(v) { return v.target_name || String(v.value); }

function valueState(v) {
  if (v.graph) {
    const when = `${(v.graph.valid_from || "—").replace("T", " ")} → ${v.graph.valid_to ? v.graph.valid_to.replace("T", " ") : "open"}`;
    return v.graph.status === "superseded" ? el("span", { class: "tag violet", title: when }, `superseded · ${when}`) : el("span", { class: "tag ok", title: when }, `committed · ${when}`);
  }
  if (v.decision === "resolved") return el("span", { class: "tag ok" }, "merged into sequence");
  if (v.decision === "reject") return el("span", { class: "tag bad" }, "rejected");
  if (v.decision === "approve") return el("span", { class: "tag ok" }, "approved, not committed");
  return el("span", { class: "tag neutral" }, "pending");
}

function resolverCard(p, c) {
  const others = c.values.filter(v => v.ref !== p.proposal_id);
  const order = c.suggested_order && c.suggested_order.length === 2 ? [...c.suggested_order] : c.values.map(v => v.ref).slice(0, 2);
  const card = el("div", { class: "resolver" + (c.resolved ? " done" : "") });
  card.append(el("div", { class: "resolver-head" },
    c.resolved ? el("span", { class: "tag ok" }, "Resolved") : el("span", { class: "tag warn" }, "Functional conflict"),
    el("b", {}, c.source_name), el("span", { class: "pred" }, c.predicate),
    el("span", { class: "muted small" }, c.kind === "functional_property" ? "has two values for a single-valued property" : "can hold only one value at a time")));
  card.append(el("table", {}, el("tbody", {}, c.values.map(v => el("tr", { class: v.ref === p.proposal_id ? "current" : "" },
    el("td", {}, el("b", {}, valueLabel(v)), v.ref === p.proposal_id ? el("div", { class: "tiny muted" }, "viewing") : null),
    el("td", {}, valueState(v)),
    el("td", { class: "small muted" }, (v.modalities || []).join(", "), v.docs && v.docs.length ? el("div", { class: "tiny", title: v.docs.map(d => data.docs[d] || d).join(", ") }, shortDoc(data.docs[v.docs[0]] || v.docs[0], 26)) : null))))));
  const resolve = async (body, label) => {
    try {
      const res = await api("/api/contradictions/resolve", { json: { contradiction_id: c.contradiction_id, ...body } });
      toast(`${label}: ${res.resolved ? "contradiction resolved" : "updated"}`, false, 4000);
      seqOpen = null;
      await reload(p.proposal_id);
    } catch (e) {
      toast(`Could not resolve: ${e.message}`, true, 6000);
    }
  };
  if (c.resolved) {
    const sequenced = c.values.some(v => v.graph && v.graph.status === "superseded");
    const mine = c.values.find(v => v.ref === p.proposal_id);
    const attach = mine && !mine.decision && mine.graph && mine.graph.status === "approved"
      && c.suggested_switch_at && c.suggested_order && c.suggested_order.length === 2;
    card.append(el("div", { class: "resolver-actions" },
      el("span", { class: "muted small" }, sequenced ? "The graph holds these values as a bi-temporal sequence." : "Only one value is still live in the graph."),
      attach ? el("button", { onclick: () => resolve({ action: "sequence", order: c.suggested_order, switch_at: c.suggested_switch_at }, "Evidence attached") },
        "Attach this evidence to the committed edge") : null));
  } else {
    const actions = el("div", { class: "resolver-actions" },
      el("button", { class: "ok", onclick: () => resolve({ action: "keep", keep: p.proposal_id }, "Kept this value") }, "Keep only this"),
      others.length === 1 ? el("button", { onclick: () => resolve({ action: "keep", keep: others[0].ref }, "Kept the other value") }, `Keep other (${valueLabel(others[0])})`) : null,
      c.kind === "functional_relation" && c.values.length === 2 ? el("button", { onclick: () => { seqOpen = seqOpen === c.contradiction_id ? null : c.contradiction_id; renderInspector($("#inspector"), p, detailCache.get(p.proposal_id)); } },
        "Keep both as temporal sequence (supersede)") : null);
    card.append(actions);
    if (seqOpen === c.contradiction_id) {
      const byRef = new Map(c.values.map(v => [v.ref, v]));
      const state = { order };
      const firstLbl = el("b", {}, valueLabel(byRef.get(state.order[0])));
      const secondLbl = el("b", {}, valueLabel(byRef.get(state.order[1])));
      const at = el("input", { class: "mono", style: { width: "170px" }, value: c.suggested_switch_at || "", placeholder: "YYYY-MM-DD[THH:MM]", "aria-label": "switch time" });
      card.append(el("div", { class: "seq-form" }, "Earlier", firstLbl,
        el("button", { class: "small ghost", title: "Swap order", onclick: () => { state.order.reverse(); firstLbl.textContent = valueLabel(byRef.get(state.order[0])); secondLbl.textContent = valueLabel(byRef.get(state.order[1])); } }, "⇄"),
        "then", secondLbl, "from", at,
        el("button", { class: "primary small", onclick: () => {
          if (!at.value.trim()) { toast("Enter when the later value starts (e.g. 1862-07-04T14:30)", true); return; }
          resolve({ action: "sequence", order: state.order, switch_at: at.value.trim() }, "Temporal sequence");
        } }, "Apply supersede")));
    }
  }
  return card;
}

function renderInspector(box, p, detail) {
  const s = stateOf(p);
  const l = local.get(p.proposal_id) || {};
  const edit = l.edit || (p.decision && p.decision.edit) || {};
  const flipped = !!edit.flip;
  const predInput = el("input", { class: "mono", id: "edit-pred", value: edit.predicate || p.predicate, style: { width: "220px" }, "aria-label": "predicate",
    onkeydown: (e) => { if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); setDecision(p, "approve", { predicate: toPred(e.target.value) }); e.target.blur(); } } });
  const decisionText = l.decision ? `${l.decision} (unsaved — press Enter to commit)` : p.decision ? `${p.decision.decision} · saved ${String(p.decision.decided_at || "").replace("T", " ").replace("Z", "")}` : "undecided";
  box.replaceChildren(
    el("div", { class: "insp-head" },
      el("div", { class: "row wrap" }, el("div", { class: `st ${s.kind}`, title: ICON_TITLE[s.kind] }, ICON[s.kind]),
        el("span", { class: "chip" }, p.bucket), el("span", { class: "chip" }, p.best_verdict.replace("_", " ")),
        el("span", { class: "spacer" }), el("button", { class: "copy", title: `${p.proposal_id} — click to copy`, onclick: () => copy(p.proposal_id) }, p.proposal_id)),
      triple(flipped ? p.target.name : p.source.name, edit.predicate || p.predicate, flipped ? p.source.name : p.target.name,
        flipped ? p.target.label : p.source.label, flipped ? p.source.label : p.target.label)),
    el("div", { class: "insp-body" },
      el("dl", { class: "kv" },
        el("dt", {}, "confidence"), el("dd", { class: "mono" }, p.confidence.toFixed(3)),
        el("dt", {}, "validity"), el("dd", { class: "mono" }, validity(p, detail.graph_edge)),
        el("dt", {}, "sources"), el("dd", {}, p.docs.map(d => el("span", { class: "chip", title: data.docs[d] || d, style: { marginRight: "4px" } }, shortDoc(data.docs[d] || d, 26)))),
        el("dt", {}, "extractors"), el("dd", { class: "mono" }, p.extractors.join(", ")),
        el("dt", {}, "ontology"), el("dd", {}, p.ontology.status === "ok" ? el("span", { class: "tag ok" }, "In grammar") : el("span", { class: "tag violet", title: p.ontology.detail || "" }, p.ontology.status.replace("_", " ")))),
      p.reasons.length ? el("div", { class: "row wrap" }, reasonTags(reasonsOf(p))) : null,
      (detail.contradictions || []).map(c => resolverCard(p, c)),
      el("div", { class: "action-bar" },
        el("button", { class: "ok solid", onclick: () => setDecision(p, "approve") }, "Approve ", el("kbd", {}, "A")),
        el("button", { class: "bad", onclick: () => setDecision(p, "reject") }, "Reject ", el("kbd", {}, "R")),
        el("span", { class: "spacer" }), el("span", { class: "small muted" }, decisionText)),
      el("div", { class: "edit-row" }, el("span", {}, "Edit"), el("label", { for: "edit-pred" }, "predicate"), predInput,
        el("button", { class: "small", onclick: () => setDecision(p, "approve", { predicate: toPred(predInput.value) }) }, "Apply"),
        el("button", { class: "small", onclick: () => flip(p) }, flipped ? "Unflip" : "Flip direction"), el("kbd", {}, "E")),
      el("h3", {}, `Evidence (${detail.evidence.length})`),
      detail.evidence.map(ev => renderEvidence(ev, detail.surfaces))));
}

// ------------------------------------------------------------------------------------------ decisions
function setDecision(p, decision, edit = {}) {
  const prev = local.get(p.proposal_id) || {};
  const merged = { ...(prev.edit || {}), ...edit };
  if (merged.predicate === p.predicate) delete merged.predicate;
  local.set(p.proposal_id, { decision, edit: decision === "approve" ? merged : {} });
  renderProgress();
  const node = document.querySelector(`.item[data-id="${p.proposal_id}"]`);
  if (node) node.replaceWith(row(p));
  if (sel < visible.length - 1) sel += 1;
  markSel(visible[sel]);
  if (visible[sel]) inspect(visible[sel]);
}

function flip(p) {
  const d = local.get(p.proposal_id) || { decision: "approve", edit: {} };
  local.set(p.proposal_id, { decision: "approve", edit: { ...(d.edit || {}), flip: !(d.edit && d.edit.flip) } });
  renderList();
  renderProgress();
}

async function commit() {
  const decisions = [...local.entries()].map(([proposal_id, d]) => ({ proposal_id, decision: d.decision, edit: d.edit }));
  if (!decisions.length && !confirm("No new decisions. Commit the already saved decisions again?")) return;
  $("#commit").disabled = true;
  try {
    const res = await api("/api/edges/decide", { json: { decisions, commit: true } });
    local.clear();
    const c = res.commit;
    const rules = Object.values(res.decisions.calibration_rules_added).reduce((a, b) => a + b, 0);
    toast(`Committed: +${c.edges_added} edges, ${c.edges_merged} merged, +${c.nodes_added} nodes · ${rules} calibration rules`, false, 5000);
    await reload();
  } catch (e) {
    toast(`Commit failed: ${e.message}`, true, 6000);
  } finally {
    $("#commit").disabled = false;
  }
}

function fillFilters() {
  const preds = [...new Set(data.proposals.map(p => p.predicate))].sort();
  const mods = [...new Set(data.proposals.flatMap(p => p.modalities))].sort();
  const keep = (id, items, label) => {
    const s = $(id); const v = s.value; s.innerHTML = "";
    s.append(el("option", { value: "" }, label));
    items.forEach(([val, txt]) => s.append(el("option", { value: val, selected: val === v }, txt)));
  };
  keep("#f-pred", preds.map(p => [p, p]), "all predicates");
  keep("#f-doc", Object.entries(data.docs).filter(([id]) => data.proposals.some(p => p.docs.includes(id))).map(([id, n]) => [id, shortDoc(n, 28)]), "all sources");
  keep("#f-mod", mods.map(m => [m, m]), "all modalities");
}

function renderAll() { renderProgress(); renderTabs(); renderList(); }

async function reload(keepId = null) {
  detailCache.clear();
  data = await api("/api/candidates");
  fillFilters();
  if (keepId) {
    const p = data.proposals.find(x => x.proposal_id === keepId);
    if (p) tab = p.bucket;
  }
  renderProgress();
  renderTabs();
  visible = filtered();
  if (keepId) sel = Math.max(0, visible.findIndex(x => x.proposal_id === keepId));
  renderList();
}

async function load() {
  try {
    data = await api("/api/candidates");
  } catch (e) {
    $("#app").replaceChildren(el("div", { class: "panel empty" }, el("h2", {}, "Nothing to review yet"), el("p", {}, e.message),
      el("p", { class: "mono small" }, "kumokg extract | import-candidates  →  kumokg verify --workspace <ws>")));
    return;
  }
  if (!data.proposals.some(p => p.bucket === tab)) tab = TABS.find(t => data.proposals.some(p => p.bucket === t.id))?.id || "auto";
  fillFilters();
  renderAll();
  clientLog("gate2", "render", { stats: { proposals: data.proposals.length, decided: data.decided } });
}

document.addEventListener("keydown", (e) => {
  if (!data) return;
  const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName);
  if (typing) return;
  const p = visible[sel];
  const k = e.key.toLowerCase();
  if (k === "j") { sel = Math.min(visible.length - 1, sel + 1); seqOpen = null; markSel(visible[sel]); if (visible[sel]) inspect(visible[sel]); e.preventDefault(); }
  else if (k === "k") { sel = Math.max(0, sel - 1); seqOpen = null; markSel(visible[sel]); if (visible[sel]) inspect(visible[sel]); e.preventDefault(); }
  else if (k === "a" && p) { setDecision(p, "approve"); e.preventDefault(); }
  else if (k === "r" && p) { setDecision(p, "reject"); e.preventDefault(); }
  else if (k === "e" && p) { setTimeout(() => { const i = $("#edit-pred"); if (i) { i.focus(); i.select(); } }, 20); e.preventDefault(); }
  else if (e.key === "Enter") { commit(); e.preventDefault(); }
});

const rerender = debounce(() => { sel = 0; renderList(); }, 120);
["#f-text", "#f-pred", "#f-doc", "#f-mod"].forEach(id => $(id).addEventListener("input", rerender));
$("#f-conf").addEventListener("input", (e) => { $("#f-conf-v").textContent = Number(e.target.value).toFixed(2); rerender(); });
$("#bulk").addEventListener("click", () => {
  const items = filtered();
  items.forEach(p => local.set(p.proposal_id, { decision: "approve", edit: (local.get(p.proposal_id) || {}).edit || {} }));
  toast(`Marked ${items.length} proposals for approval — press Enter to commit`);
  renderAll();
});
$("#commit").addEventListener("click", commit);

initTopbar("gate2").then(load);
connectSSE($("#sse"), (ev) => { if (ev.type === "graph_changed" && !["gate2_commit", "contradiction_resolved"].includes(ev.reason)) toast("Graph changed elsewhere"); });
