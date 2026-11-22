// Gate 1: read-first ontology review with a smart Approve CTA.
import { api, el, $, toast, connectSSE, initTopbar, classColor, setClasses, clsBadge, highlightEntities, shortDoc, copy,
  popover, clientLog, trapErrors } from "/assets/kumo.js";

trapErrors("gate1");

const TYPES = ["string", "int", "float", "bool", "date"];
const NOISE = new Set(["Said", "Says", "Asked", "Replied", "Thought", "Cried", "Shouted", "Oh", "Well", "Two", "Five", "Seven", "Soo", "Beau", "Ootiful"]);
let data = null;
let ont = null;
let snapshot = "";
let approved = false;
let fingerprint = null;
let editRel = -1;
let editAlias = -1;
const selectedTerms = new Set();

const toPred = (s) => (s || "").replace(/([a-z0-9])([A-Z])/g, "$1_$2").replace(/[^A-Za-z0-9]+/g, "_").replace(/^_+|_+$/g, "").toUpperCase();
const toClass = (s) => (s || "").split(/[^A-Za-z0-9]+/).filter(Boolean).map(p => p[0].toUpperCase() + p.slice(1)).join("");
const classNames = () => ont.entity_classes.map(c => c.name);
const csv = (s) => (s || "").split(",").map(x => x.trim()).filter(Boolean);
const norm = (s) => (s || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();

// ------------------------------------------------------------------------------------------ dirty state + CTA
function changed() { renderCTA(); validate(); }

function isDirty() { return JSON.stringify(ont) !== snapshot; }

function renderCTA() {
  const status = $("#cta-status");
  const box = $("#cta");
  const problems = validate();
  const dirty = isDirty();
  if (approved && !dirty && !data.draft_newer) {
    status.className = "status-line ok";
    status.replaceChildren("Approved ✓", el("span", { class: "mono tiny muted", title: "ontology fingerprint" }, fingerprint || ""));
    box.replaceChildren(el("a", { class: "btn primary", href: "/gate2" }, "Proceed to Gate 2 →"));
    return;
  }
  status.className = "status-line" + (dirty ? " warn" : "");
  status.textContent = problems.length ? `⚠ ${problems[0]}` : dirty ? "Unsaved edits" : data.draft_newer ? "Draft changed since approval" : "Not approved yet";
  const label = approved ? "Save & Re-approve" : "Approve & Continue";
  box.replaceChildren(el("button", { class: "primary", id: "approve", disabled: problems.length > 0, onclick: approve }, label));
}

async function approve() {
  if (validate().length) return;
  const btn = $("#approve");
  if (btn) btn.disabled = true;
  try {
    const res = await api("/api/ontology/approve", { json: { ontology: ont } });
    snapshot = JSON.stringify(ont);
    approved = true;
    data.draft_newer = false;
    fingerprint = res.fingerprint;
    toast(`Ontology approved — ${res.classes} classes, ${res.relations} relations. Your agent continues automatically.`, false, 5000);
  } catch (e) {
    toast(`Approval failed: ${e.message}`, true, 6000);
  }
  renderCTA();
}

function validate() {
  const names = classNames();
  const problems = [];
  if (new Set(names).size !== names.length) problems.push("duplicate class names");
  for (const r of ont.relations) {
    if (!r.predicate) problems.push("a relation has an empty predicate");
    if (!names.includes(r.source_class) || !names.includes(r.target_class)) problems.push(`${r.source_class} → ${r.predicate} → ${r.target_class} uses a missing class`);
  }
  syncAliasCounts();
  return problems;
}

// ------------------------------------------------------------------------------------------ helpers
function classSelect(value, onChange, allowEmpty = false) {
  const s = el("select", { onchange: (e) => onChange(e.target.value) });
  if (allowEmpty) s.append(el("option", { value: "" }, "any class"));
  for (const n of classNames()) s.append(el("option", { value: n, selected: n === value }, n));
  if (value && !classNames().includes(value)) s.append(el("option", { value, selected: true }, `${value} (missing)`));
  return s;
}

function renameClass(oldName, newName) {
  if (!newName || oldName === newName) return;
  for (const r of ont.relations) {
    if (r.source_class === oldName) r.source_class = newName;
    if (r.target_class === oldName) r.target_class = newName;
  }
  for (const m of ont.structured_mappings) {
    if (m.entity_class === oldName) m.entity_class = newName;
    for (const fr of m.relations) if (fr.target_class === oldName) fr.target_class = newName;
    if (m.parent_relation && m.parent_relation.parent_class === oldName) m.parent_relation.parent_class = newName;
  }
  for (const a of ont.alias_rules) if (a.entity_class === oldName) a.entity_class = newName;
}

function isTrivial(rule) {
  const c = norm(rule.canonical);
  return (rule.aliases || []).every(a => norm(a) === c);
}

function knownTerms() {
  const m = new Map();
  for (const c of ont.entity_classes) for (const t of c.seed_terms || []) m.set(norm(t), c.name);
  for (const a of ont.alias_rules) {
    const cls = a.entity_class || m.get(norm(a.canonical));
    if (!cls) continue;
    m.set(norm(a.canonical), cls);
    for (const x of a.aliases || []) m.set(norm(x), cls);
  }
  return m;
}

// ------------------------------------------------------------------------------------------ sections
function renderAudit() {
  const a = data.audit || {};
  const strip = $("#audit-strip");
  const kinds = (a.active || []).map(k => el("span", { class: "kind" },
    el("b", {}, `${k.files} ${k.kind}`), " active",
    k.pages ? el("span", { class: "sep-dot" }, `${k.pages} pages`) : null,
    k.chunks ? el("span", { class: "sep-dot" }, `${k.chunks} chunks`) : null,
    k.needs_ocr_or_vlm ? el("span", { class: "sep-dot" }, el("span", { class: "tag warn" }, `${k.needs_ocr_or_vlm} need OCR / VLM`)) : null));
  const excluded = a.excluded_sources || [];
  const docs = data.docs || {};
  const pretty = (s) => Object.entries(docs).reduce((acc, [id, name]) => acc.split(id).join(name), s || "");
  strip.replaceChildren(el("h3", {}, "Sources"), ...(kinds.length ? kinds : [el("span", { class: "muted" }, "No active sources")]),
    excluded.length ? el("button", { class: "linkish small", onclick: (e) => popover(e.currentTarget,
      el("div", {}, el("h3", {}, `Excluded sources (${excluded.length})`), excluded.map(x => el("div", { class: "item" },
        el("div", { class: "row" }, el("b", {}, x.name), el("span", { class: "tag neutral" }, "excluded")),
        el("div", { class: "small", style: { marginTop: "3px" } }, pretty(x.reason)),
        x.detail ? el("div", { class: "tiny muted" }, x.detail) : null)))) }, `${excluded.length} excluded (details)`) : null);
}

// Trivial self-aliases are hidden by default, so every alias count on the page uses the same "shown (total)" figures.
function aliasCounts() {
  const total = ont.alias_rules.length;
  const hidden = $("#show-trivial").checked ? 0 : ont.alias_rules.filter(isTrivial).length;
  return { total, shown: total - hidden };
}

function syncAliasCounts() {
  const { total, shown } = aliasCounts();
  const aliasText = shown === total ? `${total} aliases` : `${shown} aliases shown (${total} total)`;
  $("#validation").textContent = `${ont.entity_classes.length} classes · ${ont.relations.length} relations · ${aliasText}`;
  const tab = $("#jump-alias-n");
  if (tab) {
    tab.textContent = shown;
    tab.parentElement.title = shown === total ? `${total} alias rules` : `${shown} shown · ${total} total (trivial self-aliases hidden)`;
  }
}

function renderJump() {
  const nav = $("#jump");
  const secs = [["sec-classes", "Classes", ont.entity_classes.length], ["sec-relations", "Relations", ont.relations.length]];
  if (ont.structured_mappings.length) secs.push(["sec-mappings", "Mappings", ont.structured_mappings.length]);
  secs.push(["sec-aliases", "Aliases", aliasCounts().shown], ["sec-terms", "Discovered", visibleTerms().length]);
  nav.replaceChildren(...secs.map(([id, label, n]) => el("a", { href: `#${id}`, "data-sec": id,
    onclick: (e) => { e.preventDefault(); document.getElementById(id).scrollIntoView({ behavior: "smooth", block: "start" }); } },
    label, el("span", { class: "n", id: id === "sec-aliases" ? "jump-alias-n" : null }, n))));
  syncAliasCounts();
}

function renderClasses() {
  const box = $("#classes");
  box.innerHTML = "";
  $("#class-count").textContent = `${ont.entity_classes.length} classes`;
  ont.entity_classes.forEach((c, ci) => {
    const color = classColor(c.name);
    const seeds = el("div", { class: "chips" },
      (c.seed_terms || []).map((t, ti) => el("span", { class: "seed", style: { "--c": color } }, t,
        el("button", { title: `Remove ${t}`, "aria-label": `Remove ${t}`, onclick: () => { c.seed_terms.splice(ti, 1); renderClasses(); changed(); } }, "×"))),
      el("input", { class: "chip-add", placeholder: "+ add", "aria-label": `Add seed term to ${c.name}`, onkeydown: (e) => {
        if (e.key === "Enter" || e.key === ",") {
          e.preventDefault();
          const v = e.target.value.trim();
          if (v && !(c.seed_terms || []).includes(v)) { c.seed_terms = [...(c.seed_terms || []), v]; renderClasses(); changed(); }
          else e.target.value = "";
        }
      } }));
    const props = Object.entries(c.properties || {});
    const propTable = props.length ? el("table", { class: "props-table" }, el("tbody", {}, props.map(([k, p]) => el("tr", {},
      el("td", {}, el("input", { class: "mono bare", value: k, "aria-label": "property name", onchange: (e) => {
        const nk = e.target.value.trim(); if (!nk || nk === k) return;
        const copyProps = {}; for (const [kk, vv] of Object.entries(c.properties)) copyProps[kk === k ? nk : kk] = vv; c.properties = copyProps; renderClasses(); changed();
      } })),
      el("td", {}, (() => { const s = el("select", { class: "mono", onchange: (e) => { p.type = e.target.value; changed(); } });
        TYPES.forEach(t => s.append(el("option", { value: t, selected: t === p.type }, t))); return s; })()),
      el("td", {}, el("label", { class: "check small" }, el("input", { type: "checkbox", class: "switch", checked: p.functional !== false,
        onchange: (e) => { p.functional = e.target.checked; changed(); } }), "functional")),
      el("td", { style: { textAlign: "right" } }, el("button", { class: "ghost small icon", title: "Remove property", onclick: () => { delete c.properties[k]; renderClasses(); changed(); } }, "✕")))))) :
      el("span", { class: "muted small" }, "No typed properties");
    const examples = (c.examples || []).slice(0, 3).map(ex => {
      const doc = (data.docs || {})[ex.doc_id] || ex.doc_id || "";
      return el("div", { class: "example", style: { "--c": color } },
        el("div", { class: "q" }, "“", highlightEntities((ex.quote || "").replace(/\s+/g, " ").replace(/(?:\*\s*){3,}/g, "⁂ "), { source: [ex.surface || ex.name] }), "”"),
        el("div", { class: "foot" }, el("span", { title: doc }, shortDoc(doc, 24)), ex.page_no ? el("span", { class: "sep-dot" }, `p. ${ex.page_no}`) : null,
          ex.chunk_id ? el("button", { class: "copy", title: `chunk ${ex.chunk_id} — click to copy`, onclick: () => copy(ex.chunk_id) }, "id") : null));
    });
    box.append(el("div", { class: "card class-card", style: { "--c": color } },
      el("div", { class: "class-head" }, el("span", { class: "swatch", style: { background: color } }),
        el("input", { class: "bare", value: c.name, "aria-label": "Class name", onchange: (e) => {
          const nn = toClass(e.target.value); renameClass(c.name, nn); c.name = nn; setClasses([nn]); render(); changed();
        } }),
        el("button", { class: "ghost small", title: "Delete class (and the relations that use it)", onclick: () => {
          if (!confirm(`Delete class ${c.name}?`)) return;
          ont.relations = ont.relations.filter(r => r.source_class !== c.name && r.target_class !== c.name);
          ont.entity_classes.splice(ci, 1); render(); changed();
        } }, "Delete")),
      el("textarea", { class: "bare", rows: 2, placeholder: "Describe this class", oninput: (e) => { c.description = e.target.value; changed(); } }, c.description || ""),
      el("div", { class: "stack", style: { gap: "6px" } }, el("h3", {}, `Seed terms (${(c.seed_terms || []).length})`), seeds),
      el("div", { class: "stack", style: { gap: "4px" } },
        el("div", { class: "row" }, el("h3", {}, "Properties"), el("span", { class: "spacer" }),
          el("button", { class: "ghost small", onclick: () => { c.properties = c.properties || {}; let n = "property"; let i = 1; while (c.properties[n]) n = `property_${i++}`; c.properties[n] = { type: "string", functional: true, description: "" }; renderClasses(); changed(); } }, "+ property")),
        propTable),
      el("div", { class: "stack", style: { gap: "8px" } }, el("h3", {}, `Grounded examples (${(c.examples || []).length})`),
        examples.length ? examples : el("span", { class: "muted small" }, "No grounded examples yet — add seed terms and re-run propose-ontology."))));
  });
}

function renderRelations() {
  const showCues = $("#show-cues").checked;
  $("#relations thead").replaceChildren(el("tr", {}, el("th", {}, "Source"), el("th", {}, "Predicate"), el("th", {}, "Target"), el("th", {}, "Flags"),
    showCues ? el("th", {}, "Cue phrases") : null, el("th", {})));
  $("#rel-count").textContent = `${ont.relations.length} relations`;
  const tb = $("#relations tbody");
  tb.innerHTML = "";
  ont.relations.forEach((r, i) => {
    if (i === editRel) {
      tb.append(el("tr", { class: "editing" },
        el("td", {}, classSelect(r.source_class, (v) => { r.source_class = v; changed(); })),
        el("td", {}, el("input", { class: "mono", value: r.predicate, "aria-label": "predicate", onchange: (e) => { r.predicate = toPred(e.target.value); e.target.value = r.predicate; changed(); } })),
        el("td", {}, classSelect(r.target_class, (v) => { r.target_class = v; changed(); })),
        el("td", {}, el("div", { class: "row" },
          el("label", { class: "check small" }, el("input", { type: "checkbox", class: "switch", checked: r.functional, onchange: (e) => { r.functional = e.target.checked; changed(); } }), "Functional"),
          el("label", { class: "check small" }, el("input", { type: "checkbox", class: "switch", checked: r.symmetric, onchange: (e) => { r.symmetric = e.target.checked; changed(); } }), "Symmetric"))),
        showCues ? el("td", {}, el("input", { value: (r.cue_phrases || []).join(", "), "aria-label": "cue phrases", onchange: (e) => { r.cue_phrases = csv(e.target.value); changed(); } })) : null,
        el("td", { class: "actions" }, el("div", { class: "row row-actions" },
          el("button", { class: "small primary", onclick: () => { editRel = -1; renderRelations(); } }, "Done"),
          el("button", { class: "ghost small icon", title: "Delete relation", onclick: () => { ont.relations.splice(i, 1); editRel = -1; render(); changed(); } }, "✕")))));
      return;
    }
    const flags = [r.functional ? el("span", { class: "flag on" }, "Functional") : null, r.symmetric ? el("span", { class: "flag on" }, "Symmetric") : null].filter(Boolean);
    tb.append(el("tr", { class: "view", onclick: () => { editRel = i; renderRelations(); } },
      el("td", {}, clsBadge(r.source_class)), el("td", {}, el("span", { class: "pred" }, r.predicate)), el("td", {}, clsBadge(r.target_class)),
      el("td", {}, flags.length ? el("div", { class: "row" }, flags) : el("span", { class: "muted small" }, "—")),
      showCues ? el("td", { class: "small muted" }, (r.cue_phrases || []).join(", ") || "—") : null,
      el("td", { class: "actions" }, el("div", { class: "row row-actions" },
        el("button", { class: "ghost small", onclick: (e) => { e.stopPropagation(); editRel = i; renderRelations(); } }, "Edit"),
        el("button", { class: "ghost small icon", title: "Delete relation", onclick: (e) => { e.stopPropagation(); ont.relations.splice(i, 1); render(); changed(); } }, "✕")))));
  });
}

function renderAliases() {
  const q = norm($("#alias-q").value);
  const cls = $("#alias-class").value;
  const showTrivial = $("#show-trivial").checked;
  const trivial = ont.alias_rules.filter(isTrivial).length;
  $("#trivial-label").textContent = `Show trivial (${trivial})`;
  const tb = $("#aliases tbody");
  tb.innerHTML = "";
  let shown = 0;
  ont.alias_rules.forEach((a, i) => {
    if (i !== editAlias) {
      if (!showTrivial && isTrivial(a)) return;
      if (cls && a.entity_class !== cls) return;
      if (q && !norm([a.canonical, ...(a.aliases || [])].join(" ")).includes(q)) return;
    }
    shown++;
    if (i === editAlias) {
      tb.append(el("tr", { class: "editing" },
        el("td", {}, el("input", { value: a.canonical, "aria-label": "canonical name", onchange: (e) => { a.canonical = e.target.value.trim(); changed(); } })),
        el("td", {}, classSelect(a.entity_class || "", (v) => { a.entity_class = v || null; changed(); }, true)),
        el("td", {}, el("input", { value: (a.aliases || []).join(", "), "aria-label": "aliases", onchange: (e) => { a.aliases = csv(e.target.value); changed(); } })),
        el("td", { class: "actions" }, el("div", { class: "row row-actions" },
          el("button", { class: "small primary", onclick: () => { editAlias = -1; renderAliases(); } }, "Done"),
          el("button", { class: "ghost small icon", title: "Delete alias rule", onclick: () => { ont.alias_rules.splice(i, 1); editAlias = -1; renderAliases(); changed(); } }, "✕")))));
      return;
    }
    const c = norm(a.canonical);
    const alts = (a.aliases || []).filter(x => norm(x) !== c);
    tb.append(el("tr", { class: "view", onclick: () => { editAlias = i; renderAliases(); } },
      el("td", {}, el("b", {}, a.canonical)), el("td", {}, a.entity_class ? clsBadge(a.entity_class) : el("span", { class: "muted small" }, "any")),
      el("td", {}, alts.length ? el("div", { class: "chips" }, alts.map(x => el("span", { class: "chip" }, x))) : el("span", { class: "muted small" }, "case variant only")),
      el("td", { class: "actions" }, el("div", { class: "row row-actions" },
        el("button", { class: "ghost small", onclick: (e) => { e.stopPropagation(); editAlias = i; renderAliases(); } }, "Edit"),
        el("button", { class: "ghost small icon", title: "Delete alias rule", onclick: (e) => { e.stopPropagation(); ont.alias_rules.splice(i, 1); renderAliases(); changed(); } }, "✕")))));
  });
  if (!shown) tb.append(el("tr", {}, el("td", { colspan: 4, class: "muted small" }, "No alias rules match.")));
  $("#alias-count").textContent = `${shown} of ${ont.alias_rules.length}`;
  syncAliasCounts();
}

function renderMappings() {
  const sec = $("#sec-mappings");
  sec.classList.toggle("hidden", !ont.structured_mappings.length);
  const box = $("#mappings");
  box.innerHTML = "";
  for (const m of ont.structured_mappings) {
    const pv = (data.mapping_preview || []).find(p => p.mapping_id === m.mapping_id) || { columns: [], rows: [] };
    const roles = {};
    if (m.id_field) roles[m.id_field] = ["id", "accent"];
    roles[m.name_field] = ["name", "accent"];
    for (const [k, t] of Object.entries(m.property_fields || {})) roles[k] = [`prop:${t}`, ""];
    for (const k of m.prose_fields || []) roles[k] = ["prose → text", "warn"];
    for (const r of m.relations || []) roles[r.field] = [`${r.predicate} → ${r.target_class}`, "ok"];
    const rels = (m.relations || []).map(r => el("tr", {},
      el("td", { class: "mono" }, r.field),
      el("td", {}, el("input", { class: "mono", value: r.predicate, onchange: (e) => { r.predicate = toPred(e.target.value); changed(); } })),
      el("td", {}, classSelect(r.target_class, (v) => { r.target_class = v; changed(); })),
      el("td", { class: "mono small muted" }, r.target_match + (r.valid_from_field ? ` · valid_from=${r.valid_from_field}` : ""))));
    box.append(el("div", { class: "card" },
      el("div", { class: "row wrap" }, el("span", { class: "chip accent" }, m.kind), el("b", { class: "mono" }, pv.source_name || m.source),
        el("span", { class: "mono muted" }, m.table), el("span", { class: "muted small" }, `${m.row_count} rows`), el("span", { class: "spacer" }),
        el("span", { class: "small muted" }, "entity class"), classSelect(m.entity_class, (v) => { m.entity_class = v; changed(); }),
        el("label", { class: "check small" }, el("input", { type: "checkbox", class: "switch", checked: m.enabled, onchange: (e) => { m.enabled = e.target.checked; changed(); } }), "enabled")),
      rels.length ? el("table", { class: "read" }, el("thead", {}, el("tr", {}, el("th", {}, "Field"), el("th", {}, "Predicate"), el("th", {}, "Target class"), el("th", {}, "Match"))), el("tbody", {}, rels)) : null,
      pv.columns.length ? el("div", { class: "scroll-x" }, el("table", { class: "grid-cells" },
        el("tr", {}, pv.columns.map(c => el("th", {}, c, roles[c] ? el("div", {}, el("span", { class: `chip ${roles[c][1]}` }, roles[c][0])) : el("div", { class: "muted" }, "ignored")))),
        pv.rows.map(r => el("tr", {}, pv.columns.map(c => el("td", {}, r[c] || "")))))) : null));
  }
}

function visibleTerms() {
  return (ont.discovered_terms || []).filter(t => !NOISE.has(t.term));
}

function renderTerms() {
  const box = $("#terms");
  box.innerHTML = "";
  const known = knownTerms();
  for (const t of visibleTerms()) {
    const cls = known.get(norm(t.term));
    if (cls) {
      box.append(el("span", { class: "term known", style: { "--c": classColor(cls) }, title: `already a ${cls} term` }, t.term, el("span", { class: "n" }, t.count)));
      continue;
    }
    const on = selectedTerms.has(t.term);
    box.append(el("button", { class: "term" + (on ? " sel" : ""), title: `${t.count} occurrences`, onclick: () => { on ? selectedTerms.delete(t.term) : selectedTerms.add(t.term); renderTerms(); } },
      t.term, el("span", { class: "n" }, t.count)));
  }
  if (!visibleTerms().length) box.append(el("span", { class: "muted" }, "No discovered terms (structured-only corpus)."));
  const sel = $("#term-class");
  const keep = sel.value;
  sel.innerHTML = "";
  classNames().forEach(n => sel.append(el("option", { value: n, selected: n === keep }, n)));
  $("#add-terms").textContent = selectedTerms.size ? `Add selected (${selectedTerms.size})` : "Add selected";
}

function fillAliasClassFilter() {
  const s = $("#alias-class");
  const keep = s.value;
  s.innerHTML = "";
  s.append(el("option", { value: "" }, "all classes"));
  classNames().forEach(n => s.append(el("option", { value: n, selected: n === keep }, n)));
}

function render() {
  $("#closed-world").checked = !!ont.closed_world;
  $("#namespace").value = ont.namespace;
  $("#prefix").value = ont.prefix;
  renderAudit(); renderClasses(); renderRelations(); renderMappings(); fillAliasClassFilter(); renderAliases(); renderTerms(); renderJump(); renderCTA();
}

async function load() {
  try {
    data = await api("/api/ontology");
  } catch (e) {
    $("#app").replaceChildren(el("div", { class: "panel empty" }, el("h2", {}, "No ontology draft yet"), el("p", {}, e.message),
      el("p", { class: "mono small" }, "kumokg sample --workspace <ws>  →  kumokg propose-ontology --workspace <ws> [--from draft.json]")));
    return;
  }
  ont = data.ontology;
  snapshot = JSON.stringify(ont);
  approved = !!data.approved;
  fingerprint = data.approved_fingerprint;
  setClasses(classNames());
  render();
  clientLog("gate1", "render", { stats: { classes: ont.entity_classes.length, relations: ont.relations.length } });
  const obs = new IntersectionObserver((entries) => {
    for (const en of entries) if (en.isIntersecting) document.querySelectorAll("#jump a").forEach(a => a.classList.toggle("on", a.dataset.sec === en.target.id));
  }, { rootMargin: "-130px 0px -60% 0px" });
  document.querySelectorAll("section[id^=sec-]").forEach(s => obs.observe(s));
}

$("#closed-world").addEventListener("change", (e) => { ont.closed_world = e.target.checked; changed(); });
$("#namespace").addEventListener("change", (e) => { ont.namespace = e.target.value.trim(); changed(); });
$("#prefix").addEventListener("change", (e) => { ont.prefix = e.target.value.trim(); changed(); });
$("#show-cues").addEventListener("change", renderRelations);
$("#show-trivial").addEventListener("change", renderAliases);
$("#alias-q").addEventListener("input", renderAliases);
$("#alias-class").addEventListener("change", renderAliases);
$("#add-class").addEventListener("click", () => {
  let n = "NewClass", i = 2; while (classNames().includes(n)) n = `NewClass${i++}`;
  ont.entity_classes.push({ name: n, description: "", properties: {}, examples: [], seed_terms: [] }); render(); changed();
});
$("#add-rel").addEventListener("click", () => {
  const c = classNames()[0] || "Entity";
  ont.relations.push({ source_class: c, predicate: "RELATED_TO", target_class: c, functional: false, symmetric: false, description: "", cue_phrases: [], inverse_cue_phrases: [] });
  editRel = ont.relations.length - 1; renderRelations(); renderJump(); changed();
});
$("#add-alias").addEventListener("click", () => {
  ont.alias_rules.unshift({ canonical: "", aliases: [], entity_class: null }); editAlias = 0; renderAliases(); changed();
});
$("#add-terms").addEventListener("click", () => {
  const cls = ont.entity_classes.find(c => c.name === $("#term-class").value);
  if (!cls || !selectedTerms.size) return toast("Select terms and a class first");
  cls.seed_terms = Array.from(new Set([...(cls.seed_terms || []), ...selectedTerms]));
  toast(`Added ${selectedTerms.size} seed terms to ${cls.name}`);
  selectedTerms.clear(); render(); changed();
});

initTopbar("gate1").then(load);
connectSSE($("#sse"), (ev) => { if (ev.type === "gate1_approved") toast("Gate 1 approved"); });
