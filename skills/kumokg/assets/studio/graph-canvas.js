// KumoKG Explorer renderer: zero-dependency Canvas 2D graph engine.
// Curved quadratic edges with end arrowheads, collision-free pill labels, label-aware force physics,
// ego-graph focus, Force / Radial / Clusters layouts with animated transitions, HiDPI, pan / zoom / drag.
// The geometry helpers are pure and exported so they can be unit-tested without a browser.

const GOLDEN = Math.PI * (3 - Math.sqrt(5));

export function rectsOverlap(a, b, pad = 0) {
  return a.x < b.x + b.w + pad && b.x < a.x + a.w + pad && a.y < b.y + b.h + pad && b.y < a.y + a.h + pad;
}

export function countOverlaps(rects, pad = 0) {
  let n = 0;
  for (let i = 0; i < rects.length; i++) for (let j = i + 1; j < rects.length; j++) if (rectsOverlap(rects[i], rects[j], pad)) n++;
  return n;
}

// Greedy, priority-ordered label placement. Each item: {id, w, h, priority, anchors: [{x, y}, ...]} (top-left corners
// in preference order). Placed labels never overlap each other (with `pad`); obstacles (node discs) are avoided when an
// anchor allows it. Returns the placed rectangles.
export function placeLabels(items, obstacles = [], pad = 2) {
  const cell = 64;
  const grid = new Map();
  const ogrid = new Map();
  const keys = (r) => {
    const out = [];
    for (let gx = Math.floor(r.x / cell); gx <= Math.floor((r.x + r.w) / cell); gx++)
      for (let gy = Math.floor(r.y / cell); gy <= Math.floor((r.y + r.h) / cell); gy++) out.push(gx + "," + gy);
    return out;
  };
  for (const o of obstacles) for (const k of keys(o)) { if (!ogrid.has(k)) ogrid.set(k, []); ogrid.get(k).push(o); }
  const hits = (g, r, p, own) => {
    for (const k of keys(r)) {
      const lst = g.get(k);
      if (lst) for (const o of lst) if (o.id !== own && rectsOverlap(r, o, p)) return true;
    }
    return false;
  };
  const placed = [];
  const order = [...items].sort((a, b) => b.priority - a.priority || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
  for (const it of order) {
    let chosen = null;
    let fallback = null;
    for (const a of it.anchors) {
      const r = { id: it.id, x: a.x, y: a.y, w: it.w, h: it.h };
      if (hits(grid, r, pad, null)) continue;
      if (!hits(ogrid, r, 0, it.owner || it.id)) { chosen = r; break; }
      if (!fallback) fallback = r;
    }
    const r = chosen || fallback;
    if (!r) continue;
    placed.push(r);
    for (const k of keys(r)) { if (!grid.has(k)) grid.set(k, []); grid.get(k).push(r); }
  }
  return placed;
}

// Concentric rings around `centerId` by BFS distance; rings grow so neighbours keep >= `spacing` px apart.
export function radialLayout(nodes, edges, centerId, gap = 150, spacing = 120) {
  const adj = new Map(nodes.map(n => [n.id, []]));
  for (const e of edges) if (adj.has(e.source) && adj.has(e.target)) { adj.get(e.source).push(e.target); adj.get(e.target).push(e.source); }
  const byDeg = [...nodes].sort((a, b) => (b.degree || 0) - (a.degree || 0) || (a.id < b.id ? -1 : 1));
  const center = centerId && adj.has(centerId) ? centerId : (byDeg[0] && byDeg[0].id);
  const pos = new Map();
  if (!center) return pos;
  const level = new Map([[center, 0]]);
  const parent = new Map();
  const queue = [center];
  while (queue.length) {
    const u = queue.shift();
    for (const v of [...adj.get(u)].sort()) if (!level.has(v)) { level.set(v, level.get(u) + 1); parent.set(v, u); queue.push(v); }
  }
  const maxL = Math.max(0, ...level.values());
  for (const n of nodes) if (!level.has(n.id)) level.set(n.id, maxL + 1);
  const rings = new Map();
  for (const n of nodes) { const l = level.get(n.id); if (!rings.has(l)) rings.set(l, []); rings.get(l).push(n.id); }
  const angle = new Map([[center, 0]]);
  pos.set(center, { x: 0, y: 0 });
  let prevR = 0;
  const radii = [];
  for (const l of [...rings.keys()].sort((a, b) => a - b)) {
    if (l === 0) continue;
    const ids = rings.get(l);
    ids.sort((a, b) => (angle.get(parent.get(a)) ?? 99) - (angle.get(parent.get(b)) ?? 99) || (a < b ? -1 : 1));
    const r = Math.max(prevR + gap, (ids.length * spacing) / (2 * Math.PI));
    prevR = r;
    ids.forEach((id, i) => {
      const a = (i / ids.length) * Math.PI * 2 - Math.PI / 2 + l * 0.3;
      angle.set(id, a);
      pos.set(id, { x: Math.cos(a) * r, y: Math.sin(a) * r });
    });
    radii.push(r);
  }
  pos.center = center;
  pos.radii = radii;
  return pos;
}

// Community islands: members on a phyllotaxis spiral, islands packed outwards on a spiral without overlap.
export function clusterLayout(nodes, spacing = 64, gap = 70) {
  const groups = new Map();
  for (const n of nodes) {
    const c = n.community === undefined || n.community === null ? -1 : n.community;
    if (!groups.has(c)) groups.set(c, []);
    groups.get(c).push(n);
  }
  const ordered = [...groups.entries()].sort((a, b) => b[1].length - a[1].length || a[0] - b[0]);
  const pos = new Map();
  const islands = [];
  for (const [, members] of ordered) {
    members.sort((a, b) => (b.degree || 0) - (a.degree || 0) || (a.id < b.id ? -1 : 1));
    const R = spacing * 0.9 * Math.sqrt(members.length) + spacing * 0.6;
    let cx = 0, cy = 0;
    if (islands.length) {
      for (let t = 0.6; t < 400; t += 0.12) {
        const rr = 26 * t;
        const x = Math.cos(t) * rr, y = Math.sin(t) * rr;
        if (islands.every(p => Math.hypot(p.x - x, p.y - y) > p.R + R + gap)) { cx = x; cy = y; break; }
      }
    }
    islands.push({ x: cx, y: cy, R });
    members.forEach((n, k) => {
      const r = spacing * 0.9 * Math.sqrt(k);
      pos.set(n.id, { x: cx + Math.cos(k * GOLDEN) * r, y: cy + Math.sin(k * GOLDEN) * r });
    });
  }
  return pos;
}

function cssVar(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

function bez(ax, ay, cx, cy, bx, by, t) {
  const u = 1 - t;
  return { x: u * u * ax + 2 * u * t * cx + t * t * bx, y: u * u * ay + 2 * u * t * cy + t * t * by };
}

function bezTangent(ax, ay, cx, cy, bx, by, t) {
  return { x: 2 * (1 - t) * (cx - ax) + 2 * t * (bx - cx), y: 2 * (1 - t) * (cy - ay) + 2 * t * (by - cy) };
}

function segDist(p, a, b) {
  const dx = b.x - a.x, dy = b.y - a.y;
  const t = Math.max(0, Math.min(1, ((p.x - a.x) * dx + (p.y - a.y) * dy) / (dx * dx + dy * dy || 1)));
  return Math.hypot(p.x - (a.x + t * dx), p.y - (a.y + t * dy));
}

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

export class GraphCanvas {
  constructor(canvas, { onSelect, onHover, instant = false } = {}) {
    this.canvas = canvas;
    this.instant = instant;  // reduced motion: layout changes apply without tweening
    this.ctx = canvas.getContext("2d");
    this.onSelect = onSelect || (() => {});
    this.onHover = onHover || (() => {});
    this.nodes = [];
    this.edges = [];
    this.byId = new Map();
    this.adj = new Map();
    this.view = { k: 1, x: 0, y: 0 };
    this.alpha = 0;
    this.layout = "force";
    this.targets = null;
    this.hover = null;
    this.selected = null;
    this.highlight = { nodes: new Set(), edges: new Set(), path: new Set() };
    this.edgeLabels = false;
    this.egoLabelMax = 12;  // a focused hub with more incident edges lists predicates in the drawer, not on the canvas
    this.drag = null;
    this.colorOf = () => "#6f7d8f";
    this.visible = () => true;
    this.running = false;
    this.placed = [];
    this.labelCounts = { nodes: 0, edges: 0, candidates: 0 };
    this._bind();
    new ResizeObserver(() => this.resize()).observe(canvas);
    this.resize();
  }

  // ------------------------------------------------------------------ data
  resize() {
    const r = this.canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const first = !this.w || this.w <= 1;
    this.w = Math.max(1, r.width);
    this.h = Math.max(1, r.height);
    this.canvas.width = Math.round(this.w * dpr);
    this.canvas.height = Math.round(this.h * dpr);
    this.dpr = dpr;
    if (first && this.w > 1) {
      if (this.nodes.length) this.fit();
      else { this.view.x = this.w / 2; this.view.y = this.h / 2; }
    }
    this.draw();
  }

  setData(nodes, edges) {
    const old = this.byId;
    const ctx = this.ctx;
    ctx.save();
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.font = `550 12px ${cssVar("--sans", "sans-serif")}`;
    this.byId = new Map();
    const n = nodes.length || 1;
    this.nodes = nodes.map((d, i) => {
      const prev = old.get(d.id);
      const a = (i / n) * Math.PI * 2 * 7.3;
      const rad = 40 + 12 * Math.sqrt(i);
      const node = { ...d, x: prev ? prev.x : Math.cos(a) * rad, y: prev ? prev.y : Math.sin(a) * rad, vx: 0, vy: 0, fixed: prev ? prev.fixed : false };
      node.r = 5 + Math.sqrt(d.degree || 0) * 2.3;
      node.tw = ctx.measureText(d.name).width;
      this.byId.set(d.id, node);
      return node;
    });
    ctx.restore();
    this.edges = edges.filter(e => this.byId.has(e.source) && this.byId.has(e.target))
      .map(e => ({ ...e, s: this.byId.get(e.source), t: this.byId.get(e.target) }));
    this.adj = new Map(this.nodes.map(nd => [nd.id, new Set()]));
    for (const e of this.edges) { this.adj.get(e.source).add(e.target); this.adj.get(e.target).add(e.source); }
    const pairs = new Map();
    for (const e of this.edges) {
      const key = e.source < e.target ? `${e.source}|${e.target}` : `${e.target}|${e.source}`;
      if (!pairs.has(key)) pairs.set(key, []);
      pairs.get(key).push(e);
    }
    for (const list of pairs.values()) {
      list.sort((a, b) => (a.id < b.id ? -1 : 1));
      list.forEach((e, i) => {
        e.lane = i - (list.length - 1) / 2;
        e.reversed = e.source > e.target;
      });
    }
    if (this.selected && !this.byId.has(this.selected.id) && !this.edges.find(e => e.id === this.selected.id)) this.selected = null;
    if (!old.size) {
      this.alpha = 1;
      for (let i = 0; i < 450 && this.alpha > 0.02; i++) this._tick();
      this.fit();
      this.reheat(0.04);
    } else if (this.layout !== "force") {
      this.setLayout(this.layout, { animate: false });
    } else {
      this.reheat(0.5);
    }
  }

  setColor(fn) { this.colorOf = fn; this.draw(); }
  setVisible(fn) { this.visible = fn; if (this.layout === "force") this.reheat(0.3); else this.draw(); }
  setHighlight(h) { this.highlight = { nodes: new Set(h.nodes || []), edges: new Set(h.edges || []), path: new Set(h.path || []) }; this.draw(); }
  setEdgeLabels(on) { this.edgeLabels = !!on; this.draw(); }
  select(item) { this.selected = item; this.draw(); }

  focus(id) {
    const n = this.byId.get(id);
    if (!n) return;
    this.view.x = this.w / 2 - n.x * this.view.k;
    this.view.y = this.h / 2 - n.y * this.view.k;
    this.selected = { kind: "node", id };
    this.draw();
  }

  // ------------------------------------------------------------------ layouts
  setLayout(mode, { animate = true } = {}) {
    this.layout = mode;
    const vis = this.nodes.filter(n => this.visible(n));
    if (mode === "force") {
      this.targets = null;
      for (const n of this.nodes) n.fixed = false;
      this.reheat(0.6);
      return;
    }
    const center = this.selected && this.selected.kind === "node" ? this.selected.id : null;
    const visEdges = this.edges.filter(e => this.visible(e.s) && this.visible(e.t));
    this.targets = mode === "radial" ? radialLayout(vis, visEdges, center) : clusterLayout(vis);
    if (!animate || this.instant) {
      for (const n of this.nodes) { const t = this.targets.get(n.id); if (t) { n.x = t.x; n.y = t.y; } }
      this.fit();
      return;
    }
    this._fitAfterTween = true;
    this.reheat(0.3);
  }

  reheat(a = 0.6) {
    this.alpha = Math.max(this.alpha, a);
    if (!this.running) { this.running = true; requestAnimationFrame(() => this._loop()); }
  }

  _loop() {
    let moving = false;
    if (this.targets) {
      for (const n of this.nodes) {
        if (this.drag && this.drag.node === n) continue;
        const t = this.targets.get(n.id);
        if (!t) continue;
        const dx = t.x - n.x, dy = t.y - n.y;
        if (Math.abs(dx) + Math.abs(dy) > 0.4) { n.x += dx * 0.18; n.y += dy * 0.18; moving = true; }
      }
      if (!moving && this._fitAfterTween) { this._fitAfterTween = false; this.fit(); }
    } else {
      for (let i = 0; i < 2; i++) this._tick();
      moving = this.alpha > 0.004;
    }
    this.draw();
    if (moving || this.drag) requestAnimationFrame(() => this._loop());
    else this.running = false;
  }

  _tick() {
    const vis = this.nodes.filter(n => this.visible(n));
    const a = this.alpha;
    const rep = 1700;
    const lh = 22;
    for (let i = 0; i < vis.length; i++) {
      const p = vis[i];
      for (let j = i + 1; j < vis.length; j++) {
        const q = vis[j];
        let dx = q.x - p.x, dy = q.y - p.y;
        let d2 = dx * dx + dy * dy;
        if (d2 < 0.01) { dx = ((i * 7 + j) % 5 - 2) * 0.05 + 0.01; dy = ((i + j * 3) % 5 - 2) * 0.05 + 0.01; d2 = dx * dx + dy * dy; }
        if (d2 < 360000) {
          const d = Math.sqrt(d2);
          const f = (rep * a) / d2;
          p.vx -= (dx / d) * f; p.vy -= (dy / d) * f; q.vx += (dx / d) * f; q.vy += (dy / d) * f;
        }
        // label-aware collision: keep the label pills (drawn under each node) from overlapping
        const ox = (p.tw + q.tw) / 2 + 22 - Math.abs(dx);
        const oy = lh + Math.max(p.r, q.r) + 8 - Math.abs(dy);
        if (ox > 0 && oy > 0) {
          if (oy < ox) { const s = Math.sign(dy) || 1; p.vy -= s * oy * 0.12 * a; q.vy += s * oy * 0.12 * a; }
          else { const s = Math.sign(dx) || 1; p.vx -= s * ox * 0.06 * a; q.vx += s * ox * 0.06 * a; }
        }
      }
    }
    for (const e of this.edges) {
      if (!this.visible(e.s) || !this.visible(e.t)) continue;
      const dx = e.t.x - e.s.x, dy = e.t.y - e.s.y;
      const d = Math.sqrt(dx * dx + dy * dy) || 0.01;
      const L = 110 + (e.s.r + e.t.r) + Math.min(80, (e.s.tw + e.t.tw) * 0.12);
      const f = (d - L) * 0.03 * a;
      const fx = (dx / d) * f, fy = (dy / d) * f;
      e.s.vx += fx; e.s.vy += fy; e.t.vx -= fx; e.t.vy -= fy;
    }
    const cent = new Map();
    for (const n of vis) {
      if (n.community === undefined || n.community < 0) continue;
      const c = cent.get(n.community) || { x: 0, y: 0, n: 0 };
      c.x += n.x; c.y += n.y; c.n += 1; cent.set(n.community, c);
    }
    // aspect-aware gravity: wide canvases get a wide layout instead of a circle in the middle
    const gx = 0.0035 * (this.w > 1 && this.h > 1 ? Math.min(1, (this.h / this.w) * 0.9) : 1);
    for (const n of vis) {
      n.vx -= n.x * gx * a; n.vy -= n.y * 0.0035 * a;
      const c = cent.get(n.community);
      if (c && c.n > 1) { n.vx += (c.x / c.n - n.x) * 0.005 * a; n.vy += (c.y / c.n - n.y) * 0.005 * a; }
      if (n.fixed || (this.drag && this.drag.node === n)) { n.vx = 0; n.vy = 0; continue; }
      n.vx *= 0.8; n.vy *= 0.8;
      n.x += Math.max(-30, Math.min(30, n.vx));
      n.y += Math.max(-30, Math.min(30, n.vy));
    }
    this.alpha *= 0.986;
  }

  // ------------------------------------------------------------------ view
  fit(pad = 70) {
    const vis = this.nodes.filter(n => this.visible(n));
    if (!vis.length || this.w <= 1) return;
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const n of vis) {
      x0 = Math.min(x0, n.x - n.tw / 2 - 10); x1 = Math.max(x1, n.x + n.tw / 2 + 10);
      y0 = Math.min(y0, n.y - n.r); y1 = Math.max(y1, n.y + n.r + 26);
    }
    const k = Math.min(1.8, Math.max(0.12, Math.min((this.w - pad * 2) / Math.max(1, x1 - x0), (this.h - pad * 2 - 60) / Math.max(1, y1 - y0))));
    this.view.k = k;
    this.view.x = this.w / 2 - ((x0 + x1) / 2) * k;
    this.view.y = (this.h - 50) / 2 - ((y0 + y1) / 2) * k;
    this.draw();
  }

  zoom(f, cx = this.w / 2, cy = this.h / 2) {
    const k2 = Math.min(6, Math.max(0.08, this.view.k * f));
    const wx = (cx - this.view.x) / this.view.k, wy = (cy - this.view.y) / this.view.k;
    this.view.k = k2;
    this.view.x = cx - wx * k2;
    this.view.y = cy - wy * k2;
    this.draw();
  }

  _r(n) { return n.r * Math.max(1, 0.8 / this.view.k); }

  _toWorld(sx, sy) { return { x: (sx - this.view.x) / this.view.k, y: (sy - this.view.y) / this.view.k }; }
  _toScreen(x, y) { return { x: x * this.view.k + this.view.x, y: y * this.view.k + this.view.y }; }

  _geom(e) {
    const ax = e.s.x, ay = e.s.y, bx = e.t.x, by = e.t.y;
    const dx = bx - ax, dy = by - ay;
    const d = Math.hypot(dx, dy) || 1;
    let off = 0.07 * d + e.lane * 30;
    if (e.reversed) off = -off;
    const nx = -dy / d, ny = dx / d;
    return { ax, ay, bx, by, cx: (ax + bx) / 2 + nx * off, cy: (ay + by) / 2 + ny * off, d };
  }

  _hitNode(sx, sy) {
    const p = this._toWorld(sx, sy);
    let best = null, bd = Infinity;
    for (const n of this.nodes) {
      if (!this.visible(n)) continue;
      const d = Math.hypot(n.x - p.x, n.y - p.y);
      if (d < this._r(n) + 5 / this.view.k && d < bd) { best = n; bd = d; }
    }
    return best;
  }

  _hitEdge(sx, sy) {
    const p = this._toWorld(sx, sy);
    const tol = 5 / this.view.k;
    for (const e of this.edges) {
      if (!this.visible(e.s) || !this.visible(e.t)) continue;
      const g = this._geom(e);
      let prev = bez(g.ax, g.ay, g.cx, g.cy, g.bx, g.by, 0);
      for (let i = 1; i <= 14; i++) {
        const q = bez(g.ax, g.ay, g.cx, g.cy, g.bx, g.by, i / 14);
        if (segDist(p, prev, q) < tol) return e;
        prev = q;
      }
    }
    return null;
  }

  _bind() {
    const c = this.canvas;
    c.addEventListener("pointerdown", (ev) => {
      c.setPointerCapture(ev.pointerId);
      const n = this._hitNode(ev.offsetX, ev.offsetY);
      this.drag = n ? { node: n, moved: false, sx: ev.offsetX, sy: ev.offsetY } : { pan: true, moved: false, sx: ev.offsetX, sy: ev.offsetY, vx: this.view.x, vy: this.view.y };
      c.classList.add("dragging");
    });
    c.addEventListener("pointermove", (ev) => {
      if (this.drag) {
        const dx = ev.offsetX - this.drag.sx, dy = ev.offsetY - this.drag.sy;
        if (Math.abs(dx) + Math.abs(dy) > 3) this.drag.moved = true;
        if (this.drag.node && this.drag.moved) {
          const p = this._toWorld(ev.offsetX, ev.offsetY);
          this.drag.node.x = p.x; this.drag.node.y = p.y; this.drag.node.fixed = true;
          if (this.targets) this.targets.set(this.drag.node.id, { x: p.x, y: p.y });
          this.reheat(this.layout === "force" ? 0.12 : 0.01);
        } else if (this.drag.pan) {
          this.view.x = this.drag.vx + dx; this.view.y = this.drag.vy + dy; this.draw();
        }
        return;
      }
      const n = this._hitNode(ev.offsetX, ev.offsetY);
      const hv = n ? { kind: "node", id: n.id } : (() => { const e = this._hitEdge(ev.offsetX, ev.offsetY); return e ? { kind: "edge", id: e.id } : null; })();
      if ((hv && hv.id) !== (this.hover && this.hover.id)) { this.hover = hv; this.onHover(hv); this.draw(); }
      c.style.cursor = hv ? "pointer" : "grab";
    });
    c.addEventListener("pointerleave", () => { if (this.hover) { this.hover = null; this.draw(); } });
    c.addEventListener("pointerup", (ev) => {
      const d = this.drag;
      this.drag = null;
      c.classList.remove("dragging");
      if (d && !d.moved) {
        const n = this._hitNode(ev.offsetX, ev.offsetY);
        const e = n ? null : this._hitEdge(ev.offsetX, ev.offsetY);
        this.selected = n ? { kind: "node", id: n.id } : e ? { kind: "edge", id: e.id } : null;
        this.onSelect(this.selected);
        this.draw();
      }
    });
    c.addEventListener("dblclick", (ev) => {
      const n = this._hitNode(ev.offsetX, ev.offsetY);
      if (n) { n.fixed = false; this.reheat(0.3); } else this.fit();
    });
    c.addEventListener("wheel", (ev) => { ev.preventDefault(); this.zoom(Math.exp(-ev.deltaY * 0.0015), ev.offsetX, ev.offsetY); }, { passive: false });
  }

  labelStats() {
    return { drawn: this.placed.length, overlaps: countOverlaps(this.placed), nodeLabels: this.labelCounts.nodes,
      edgeLabels: this.labelCounts.edges, candidates: this.labelCounts.candidates, nodes: this.nodes.length, edges: this.edges.length };
  }

  // ------------------------------------------------------------------ drawing
  draw() {
    const ctx = this.ctx;
    if (!ctx || !this.w) return;
    const C = {
      edge: cssVar("--border-strong", "#bfc7d4"), text: cssVar("--text", "#18202d"), muted: cssVar("--muted", "#7b8596"),
      panel: cssVar("--panel", "#fff"), accent: cssVar("--accent", "#2b59c3"), violet: cssVar("--violet", "#6b4bc1"),
      labelBg: cssVar("--label-bg", "rgba(13,17,23,0.86)"), border: cssVar("--border", "#d8dde6"),
      sans: cssVar("--sans", "sans-serif"), mono: cssVar("--mono", "monospace"),
    };
    const dpr = this.dpr || 1;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, this.w, this.h);
    const k = this.view.k;
    const focus = this.hover || this.selected;
    let ego = null;
    let egoEdges = new Set();
    if (focus && focus.kind === "node" && this.adj.has(focus.id)) {
      ego = new Set([focus.id, ...this.adj.get(focus.id)]);
      for (const e of this.edges) if (e.source === focus.id || e.target === focus.id) egoEdges.add(e.id);
    } else if (focus && focus.kind === "edge") {
      const e = this.edges.find(x => x.id === focus.id);
      if (e) { ego = new Set([e.source, e.target]); egoEdges = new Set([e.id]); }
    }
    const hubFocus = !!(focus && focus.kind === "node" && egoEdges.size > this.egoLabelMax);
    const hl = this.highlight;
    const anyHl = hl.nodes.size || hl.edges.size || hl.path.size;
    const nodeAlpha = (n) => {
      if (ego) return ego.has(n.id) ? 1 : 0.15;
      if (anyHl) return hl.nodes.has(n.id) ? 1 : 0.22;
      return (n.degree || 0) === 0 ? 0.4 : 1;
    };

    ctx.save();
    ctx.translate(this.view.x, this.view.y);
    ctx.scale(k, k);
    const hub = this.layout === "radial" && this.targets && this.targets.radii ? this.byId.get(this.targets.center) : null;
    if (hub) {
      ctx.globalAlpha = 0.9;
      ctx.strokeStyle = C.edge;
      ctx.lineWidth = 1 / k;
      ctx.setLineDash([3 / k, 5 / k]);
      for (const r of this.targets.radii) { ctx.beginPath(); ctx.arc(hub.x, hub.y, r, 0, Math.PI * 2); ctx.stroke(); }
      ctx.setLineDash([]);
    }
    const edgeLabelCands = [];
    for (const e of this.edges) {
      if (!this.visible(e.s) || !this.visible(e.t)) continue;
      const g = this._geom(e);
      const onPath = hl.path.has(e.id);
      const isHl = onPath || hl.edges.has(e.id);
      const inEgo = egoEdges.has(e.id);
      let alpha = 0.7;
      if (ego) alpha = inEgo ? 0.95 : 0.15;
      else if (anyHl) alpha = isHl ? 0.95 : 0.15;
      const inactive = e.active === false || (e.active === undefined && e.status === "superseded");
      const color = inactive ? C.violet : onPath || isHl ? C.accent : inEgo ? C.text : C.edge;
      ctx.globalAlpha = alpha;
      ctx.strokeStyle = color;
      ctx.lineWidth = (onPath ? 2.6 : isHl || inEgo ? 1.8 : 1.1) / Math.sqrt(k);
      ctx.setLineDash(inactive ? [6 / k, 5 / k] : []);
      ctx.beginPath();
      ctx.moveTo(g.ax, g.ay);
      ctx.quadraticCurveTo(g.cx, g.cy, g.bx, g.by);
      ctx.stroke();
      ctx.setLineDash([]);
      // arrowhead where the curve meets the target disc
      const tEnd = Math.max(0.5, 1 - (this._r(e.t) + 2.5 / k) / Math.max(1, g.d));
      const tip = bez(g.ax, g.ay, g.cx, g.cy, g.bx, g.by, tEnd);
      const tan = bezTangent(g.ax, g.ay, g.cx, g.cy, g.bx, g.by, tEnd);
      const ang = Math.atan2(tan.y, tan.x);
      const sz = (isHl || inEgo ? 7.5 : 6) / Math.sqrt(k);
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.moveTo(tip.x, tip.y);
      ctx.lineTo(tip.x - sz * Math.cos(ang - 0.42), tip.y - sz * Math.sin(ang - 0.42));
      ctx.lineTo(tip.x - sz * Math.cos(ang + 0.42), tip.y - sz * Math.sin(ang + 0.42));
      ctx.closePath();
      ctx.fill();
      const autoLabel = onPath || (inEgo && !hubFocus);
      if (autoLabel || (this.edgeLabels && alpha > 0.5)) {
        const mid = bez(g.ax, g.ay, g.cx, g.cy, g.bx, g.by, 0.5);
        // focus/path predicates outrank ordinary node names; optional edge labels never displace a node name
        edgeLabelCands.push({ e, mid, priority: autoLabel ? 850000 : -1000 + (e.confidence || 0) * 100, color: onPath || isHl ? C.accent : C.muted });
      }
    }
    for (const n of this.nodes) {
      if (!this.visible(n)) continue;
      const alpha = nodeAlpha(n);
      ctx.globalAlpha = alpha;
      ctx.beginPath();
      ctx.arc(n.x, n.y, this._r(n), 0, Math.PI * 2);
      ctx.fillStyle = this.colorOf(n);
      ctx.fill();
      ctx.lineWidth = 1.5 / Math.sqrt(k);
      ctx.strokeStyle = C.panel;
      ctx.stroke();
      if ((n.degree || 0) === 0) {
        ctx.setLineDash([3 / k, 3 / k]);
        ctx.beginPath();
        ctx.arc(n.x, n.y, this._r(n) + 3 / Math.sqrt(k), 0, Math.PI * 2);
        ctx.strokeStyle = C.muted;
        ctx.lineWidth = 1 / Math.sqrt(k);
        ctx.stroke();
        ctx.setLineDash([]);
      }
      const sel = this.selected && this.selected.kind === "node" && this.selected.id === n.id;
      if (sel || hl.nodes.has(n.id) || (this.hover && this.hover.id === n.id)) {
        ctx.beginPath();
        ctx.arc(n.x, n.y, this._r(n) + 3.5 / Math.sqrt(k), 0, Math.PI * 2);
        ctx.strokeStyle = sel ? C.text : C.accent;
        ctx.lineWidth = 2 / Math.sqrt(k);
        ctx.stroke();
      }
    }
    ctx.restore();
    ctx.globalAlpha = 1;

    // ---- screen-space labels with collision-free placement
    const items = [];
    const obstacles = [];
    const labelH = 18;
    for (const n of this.nodes) {
      if (!this.visible(n)) continue;
      const s = this._toScreen(n.x, n.y);
      const rs = Math.max(3, this._r(n) * k);
      obstacles.push({ id: "n:" + n.id, x: s.x - rs, y: s.y - rs, w: rs * 2, h: rs * 2 });
      const alpha = nodeAlpha(n);
      if (alpha < 0.3) continue;
      const w = n.tw + 14;
      const sel = this.selected && this.selected.kind === "node" && this.selected.id === n.id;
      const hov = this.hover && this.hover.id === n.id;
      let pr = (n.degree || 0) * 100 + n.r;
      if (sel) pr = 1e6; else if (hov) pr = 9e5; else if (ego && ego.has(n.id)) pr = 8e5 + pr; else if (hl.nodes.has(n.id)) pr = 7e5 + pr;
      if ((n.degree || 0) === 0 && !sel && !hov) pr -= 1000;
      items.push({ id: "n:" + n.id, owner: "n:" + n.id, w, h: labelH, priority: pr, node: n, alpha,
        anchors: [{ x: s.x - w / 2, y: s.y + rs + 4 }, { x: s.x - w / 2, y: s.y - rs - 4 - labelH },
          { x: s.x + rs + 5, y: s.y - labelH / 2 }, { x: s.x - rs - 5 - w, y: s.y - labelH / 2 }] });
    }
    ctx.font = `500 10.5px ${C.mono}`;
    for (const c of edgeLabelCands) {
      const s = this._toScreen(c.mid.x, c.mid.y);
      const w = ctx.measureText(c.e.type).width + 10;
      const h = 15;
      items.push({ id: "e:" + c.e.id, w, h, priority: c.priority, edge: c, anchors: [{ x: s.x - w / 2, y: s.y - h / 2 },
        { x: s.x - w / 2, y: s.y - h - 2 }, { x: s.x - w / 2, y: s.y + 2 }] });
    }
    const placed = placeLabels(items, obstacles, 2);
    const byId = new Map(items.map(i => [i.id, i]));
    let nNodes = 0, nEdges = 0;
    for (const r of placed) {
      const it = byId.get(r.id);
      if (it.node) {
        nNodes++;
        ctx.globalAlpha = it.alpha;
        roundRect(ctx, r.x, r.y, r.w, r.h, 5);
        ctx.fillStyle = C.labelBg;
        ctx.fill();
        ctx.strokeStyle = C.border;
        ctx.lineWidth = 1;
        ctx.stroke();
        ctx.font = `${it.priority >= 8e5 ? 650 : 550} 12px ${C.sans}`;
        ctx.fillStyle = C.text;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText(it.node.name, r.x + r.w / 2, r.y + r.h / 2 + 0.5);
      } else {
        nEdges++;
        ctx.globalAlpha = 0.95;
        roundRect(ctx, r.x, r.y, r.w, r.h, 4);
        ctx.fillStyle = C.labelBg;
        ctx.fill();
        ctx.font = `500 10.5px ${C.mono}`;
        ctx.fillStyle = it.edge.color;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText(it.edge.e.type, r.x + r.w / 2, r.y + r.h / 2 + 0.5);
      }
    }
    ctx.globalAlpha = 1;
    this.placed = placed;
    this.labelCounts = { nodes: nNodes, edges: nEdges, candidates: items.length };
  }
}
