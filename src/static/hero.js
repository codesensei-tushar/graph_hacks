// Company Brain: the hero's live slice of the graph, from /api/graph/sample.
// A deterministic force layout (the same sample always gives the same picture) and a few
// bounded walks: pulses travel real edges in their true direction, fanning out hop by hop.
// The walks run only while the panel is in view and the tab is visible, stop for good after
// a few cycles or at the first interaction, and are drawn once, still, under reduced motion.
// Plain JS, no build step. Every piece of text goes in with textContent.
(() => {
  "use strict";

  const { el, svgEl, familyOf, kindOf, questionsFor } = window.BrainGraph;

  const CYCLES = 4;
  const TRAVEL = 760; // ms for a pulse to cross one edge
  const HOP = 980; // ms from one hop's start to the next
  const HOLD = 1700; // ms the finished walk stays lit before the next one
  const FADE = 520;
  const START = 900; // let the entrance settle first
  const MAX_DEPTH = 3;
  const MAX_FAN = 3;
  const FAMILY_NAME = { person: "People", service: "What teams run", knowledge: "Knowledge", other: "Teams and the rest" };

  const reducedMotion = () => window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const ease = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);
  const clip = (text, max) => (text.length <= max ? text : `${text.slice(0, max - 1)}…`);

  // --- Layout --------------------------------------------------------------------------------
  // Fruchterman-Reingold from a phyllotaxis start, hubs in the middle. No randomness anywhere,
  // so a given sample always settles the same way.

  function forceLayout(nodes, links, aspect) {
    const n = nodes.length;
    const x = new Float64Array(n);
    const y = new Float64Array(n);
    const degree = new Float64Array(n);
    for (const [a, b] of links) { degree[a] += 1; degree[b] += 1; }
    const order = nodes.map((_, i) => i).sort((a, b) => degree[b] - degree[a] || (nodes[a].id < nodes[b].id ? -1 : 1));
    order.forEach((i, rank) => {
      const r = 0.06 * Math.sqrt(rank + 0.5);
      const t = rank * 2.399963;
      x[i] = aspect / 2 + r * Math.cos(t) * aspect;
      y[i] = 0.5 + r * Math.sin(t);
    });
    const k = 0.95 * Math.sqrt(aspect / Math.max(n, 1));
    const k2 = k * k;
    const dx = new Float64Array(n);
    const dy = new Float64Array(n);
    let temp = aspect / 8;
    const ITER = 420;
    for (let it = 0; it < ITER; it += 1) {
      dx.fill(0);
      dy.fill(0);
      for (let i = 0; i < n; i += 1) {
        for (let j = i + 1; j < n; j += 1) {
          let ux = x[i] - x[j];
          let uy = y[i] - y[j];
          let d = Math.sqrt(ux * ux + uy * uy);
          if (d < 1e-6) { ux = 1e-3 * (i - j); uy = 1e-3; d = Math.sqrt(ux * ux + uy * uy); }
          const f = k2 / d;
          ux /= d; uy /= d;
          dx[i] += ux * f; dy[i] += uy * f;
          dx[j] -= ux * f; dy[j] -= uy * f;
        }
      }
      for (const [a, b] of links) {
        let ux = x[a] - x[b];
        let uy = y[a] - y[b];
        const d = Math.sqrt(ux * ux + uy * uy) || 1e-6;
        const f = (d * d) / k;
        ux /= d; uy /= d;
        dx[a] -= ux * f; dy[a] -= uy * f;
        dx[b] += ux * f; dy[b] += uy * f;
      }
      for (let i = 0; i < n; i += 1) {
        // Gravity keeps loose pieces near the middle; stretched sideways to fill the frame.
        dx[i] += (aspect / 2 - x[i]) * (1.1 / aspect);
        dy[i] += (0.5 - y[i]) * 1.6;
        const d = Math.sqrt(dx[i] * dx[i] + dy[i] * dy[i]) || 1e-6;
        const step = Math.min(d, temp);
        x[i] += (dx[i] / d) * step;
        y[i] += (dy[i] / d) * step;
      }
      temp = Math.max(aspect / 400, temp * 0.986);
    }
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    for (let i = 0; i < n; i += 1) {
      minX = Math.min(minX, x[i]); maxX = Math.max(maxX, x[i]);
      minY = Math.min(minY, y[i]); maxY = Math.max(maxY, y[i]);
    }
    const spanX = maxX - minX || 1;
    const spanY = maxY - minY || 1;
    return nodes.map((_, i) => [(x[i] - minX) / spanX, (y[i] - minY) / spanY]);
  }

  // --- Shapes --------------------------------------------------------------------------------
  // Shape and colour both carry the family, so neither is the only signal.

  function shape(family, r) {
    if (family === "person") return svgEl("circle", { class: "shape", r: r.toFixed(1) });
    if (family === "service") {
      const s = r * 1.7;
      return svgEl("rect", { class: "shape", x: (-s / 2).toFixed(1), y: (-s / 2).toFixed(1), width: s.toFixed(1), height: s.toFixed(1), rx: 2.5 });
    }
    if (family === "knowledge") {
      const s = r * 1.3;
      return svgEl("path", { class: "shape", d: `M0 ${-s}L${s} 0L0 ${s}L${-s} 0Z` });
    }
    const s = r * 1.15;
    const pts = [0, 1, 2, 3, 4, 5].map((i) => {
      const a = (Math.PI / 3) * i;
      return `${(s * Math.cos(a)).toFixed(1)},${(s * Math.sin(a)).toFixed(1)}`;
    });
    return svgEl("polygon", { class: "shape", points: pts.join(" ") });
  }

  function legendItem(family) {
    const item = el("li");
    const icon = svgEl("svg", { class: "sh lg", viewBox: "-8 -8 16 16", "aria-hidden": "true" });
    const g = svgEl("g", { class: `n ${family}` });
    g.append(shape(family, 5));
    icon.append(g);
    item.append(icon, FAMILY_NAME[family]);
    return item;
  }

  // --- Walks ---------------------------------------------------------------------------------
  // A walk starts at one node and follows outgoing edges only, so every pulse moves the way
  // its edge points. Each hop takes up to MAX_FAN unvisited targets, busiest first.

  function planWalk(seed, out, degree) {
    const hops = [];
    const seen = new Set([seed]);
    let frontier = [seed];
    for (let depth = 0; depth < MAX_DEPTH && frontier.length; depth += 1) {
      const hop = [];
      for (const id of frontier) {
        for (const edge of out.get(id) || []) {
          if (hop.length >= MAX_FAN) break;
          if (seen.has(edge.target)) continue;
          seen.add(edge.target);
          hop.push(edge);
        }
      }
      if (!hop.length) break;
      hops.push(hop);
      frontier = hop.map((edge) => edge.target);
    }
    return { seed, hops, size: hops.reduce((sum, hop) => sum + hop.length, 0), degree: degree.get(seed) || 0 };
  }

  // The richest walks first, preferring a different family of starting node each cycle.
  function pickWalks(nodes, out, degree, count) {
    const byId = new Map(nodes.map((node) => [node.id, node]));
    const walks = nodes
      .map((node) => planWalk(node.id, out, degree))
      .filter((walk) => walk.hops.length >= 2)
      .sort((a, b) => b.size - a.size || b.degree - a.degree || (a.seed < b.seed ? -1 : 1));
    const picked = [];
    const families = new Set();
    for (const walk of walks) {
      const family = familyOf(byId.get(walk.seed).label);
      if (families.has(family)) continue;
      families.add(family);
      picked.push(walk);
      if (picked.length === count) return picked;
    }
    for (const walk of walks) {
      if (picked.length === count) break;
      if (!picked.includes(walk)) picked.push(walk);
    }
    return picked;
  }

  // --- The panel -----------------------------------------------------------------------------

  function mount(stage, data, options = {}) {
    const ticker = options.ticker;
    const raw = Array.isArray(data.nodes) ? data.nodes : [];
    const nodes = raw.filter((node) => node && node.id !== undefined && node.name).map((node) => ({ ...node, id: String(node.id) }));
    const ids = new Set(nodes.map((node) => node.id));
    const seenPairs = new Set();
    const edges = [];
    for (const edge of data.edges || []) {
      const source = String(edge.source ?? edge.from);
      const target = String(edge.target ?? edge.to);
      if (!ids.has(source) || !ids.has(target) || source === target) continue;
      const key = `${source}|${edge.rel}|${target}`;
      if (seenPairs.has(key)) continue;
      seenPairs.add(key);
      edges.push({ source, target, rel: edge.rel });
    }
    if (!nodes.length) return null;

    const index = new Map(nodes.map((node, i) => [node.id, i]));
    const degree = new Map(nodes.map((node) => [node.id, 0]));
    const out = new Map(nodes.map((node) => [node.id, []]));
    const near = new Map(nodes.map((node) => [node.id, new Set()]));
    const pairs = new Set();
    const links = [];
    for (const edge of edges) {
      degree.set(edge.source, degree.get(edge.source) + 1);
      degree.set(edge.target, degree.get(edge.target) + 1);
      out.get(edge.source).push(edge);
      near.get(edge.source).add(edge.target);
      near.get(edge.target).add(edge.source);
      const pair = [edge.source, edge.target].sort().join("|");
      if (!pairs.has(pair)) { pairs.add(pair); links.push([index.get(edge.source), index.get(edge.target)]); }
    }
    for (const list of out.values()) list.sort((a, b) => degree.get(b.target) - degree.get(a.target) || (a.target < b.target ? -1 : 1));

    const box = () => ({ w: stage.clientWidth || 640, h: stage.clientHeight || 460 });
    const first = box();
    const unit = forceLayout(nodes, links, first.w / first.h);
    const radius = (id) => 4 + Math.min(6, Math.sqrt(degree.get(id)) * 1.4);

    const svg = svgEl("svg", { class: "lg", role: "img", "aria-labelledby": "lg-title lg-desc" });
    const title = svgEl("title", { id: "lg-title" });
    title.textContent = `A live sample of the graph: ${nodes.length} nodes and ${edges.length} edges`;
    const desc = svgEl("desc", { id: "lg-desc" });
    desc.textContent = "Each shape is a node from the public scope; each line is an edge between them. Pulses walk real edges in the direction they point.";
    svg.append(title, desc);
    const defs = svgEl("defs");
    const marker = svgEl("marker", { id: "lg-arrow", viewBox: "0 0 10 10", refX: "8.5", refY: "5", markerWidth: "7", markerHeight: "7", markerUnits: "userSpaceOnUse", orient: "auto" });
    marker.append(svgEl("path", { d: "M0,0 L10,5 L0,10 z", class: "head" }));
    defs.append(marker);
    svg.append(defs);
    const edgeLayer = svgEl("g");
    const walkLayer = svgEl("g");
    const nodeLayer = svgEl("g");
    const pulseLayer = svgEl("g");
    svg.append(edgeLayer, walkLayer, nodeLayer, pulseLayer);

    const edgeEls = edges.map((edge) => {
      const line = svgEl("line", { class: "e" });
      edgeLayer.append(line);
      return line;
    });
    const nodeEls = new Map();
    for (const node of nodes) {
      const family = familyOf(node.label);
      // A node is askable when the shared mapping has a question for it (graph.js, questionsFor).
      const askable = questionsFor(node).length > 0;
      const group = svgEl("g", { class: `n ${family}${askable ? " askable" : ""}` });
      group.append(svgEl("circle", { class: "ring", r: (radius(node.id) + 4.5).toFixed(1) }));
      group.append(shape(family, radius(node.id)));
      const label = svgEl("text", { class: "lbl quiet", y: "3.6" });
      label.textContent = clip(node.name, 22);
      group.append(label);
      nodeLayer.append(group);
      nodeEls.set(node.id, { group, label, askable });
    }

    // Pixel positions for the current size. Labels go right of a node, or left near the right
    // edge; the busiest nodes keep theirs, and any label that would overlap one is held back.
    let pos = new Map();
    function place() {
      const { w, h } = box();
      const padX = Math.min(64, w * 0.09);
      const padY = Math.min(40, h * 0.09);
      pos = new Map(nodes.map((node, i) => [node.id, [padX + unit[i][0] * (w - 2 * padX), padY + unit[i][1] * (h - 2 * padY)]]));
      svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
      edges.forEach((edge, i) => {
        const [x1, y1] = pos.get(edge.source);
        const [x2, y2] = pos.get(edge.target);
        edgeEls[i].setAttribute("x1", x1.toFixed(1));
        edgeEls[i].setAttribute("y1", y1.toFixed(1));
        edgeEls[i].setAttribute("x2", x2.toFixed(1));
        edgeEls[i].setAttribute("y2", y2.toFixed(1));
      });
      // A label goes right of its node, or left; it is held back when both sides would cover
      // another label or another node's shape. Nodes with a label are painted last, on top.
      const taken = [];
      const shapes = nodes.map((node) => {
        const [x, y] = pos.get(node.id);
        const r = radius(node.id) + 2;
        return { id: node.id, x0: x - r, x1: x + r, y0: y - r, y1: y + r };
      });
      const hits = (rect, id) => taken.some((o) => rect.x0 < o.x1 && rect.x1 > o.x0 && rect.y0 < o.y1 && rect.y1 > o.y0)
        || shapes.some((o) => o.id !== id && rect.x0 < o.x1 && rect.x1 > o.x0 && rect.y0 < o.y1 && rect.y1 > o.y0);
      const ranked = [...nodes].sort((a, b) => degree.get(b.id) - degree.get(a.id) || (a.id < b.id ? -1 : 1));
      const budget = w < 480 ? 5 : 9;
      const labelled = [];
      ranked.forEach((node, rank) => {
        const { group, label } = nodeEls.get(node.id);
        const [x, y] = pos.get(node.id);
        group.setAttribute("transform", `translate(${x.toFixed(1)},${y.toFixed(1)})`);
        const r = radius(node.id) + 5;
        const width = label.textContent.length * 6.1;
        const sides = x + r + width > w - 6 ? [true] : x - r - width < 6 ? [false] : [false, true];
        let side = sides[0];
        let rect = null;
        for (const left of sides) {
          const candidate = { x0: left ? x - r - width : x + r, x1: left ? x - r : x + r + width, y0: y - 9, y1: y + 7 };
          if (!hits(candidate, node.id)) { side = left; rect = candidate; break; }
        }
        label.setAttribute("x", (side ? -r : r).toFixed(1));
        label.setAttribute("text-anchor", side ? "end" : "start");
        const shown = rank < budget && rect !== null;
        if (shown) { taken.push(rect); labelled.push(group); }
        label.classList.toggle("quiet", !shown);
      });
      for (const group of labelled) nodeLayer.append(group);
      if (walk) redrawWalk();
    }

    // --- Hover, focus and asking ---------------------------------------------------------
    const tip = el("div", { class: "tip", "aria-hidden": "true" });
    const said = el("p", { class: "sr", "aria-live": "polite" });
    let focused = null;

    function show(id) {
      clear();
      focused = id;
      const node = nodes[index.get(id)];
      svg.classList.add("focus");
      nodeEls.get(id).group.classList.add("on");
      nodeLayer.append(nodeEls.get(id).group);
      for (const other of near.get(id)) nodeEls.get(other).group.classList.add("near");
      edges.forEach((edge, i) => edgeEls[i].classList.toggle("near", edge.source === id || edge.target === id));
      const kind = node.kind && node.kind !== "service" ? ` · ${node.kind}` : "";
      const count = degree.get(id);
      tip.replaceChildren(el("b", {}, node.name), el("span", {}, `${kindOf(node.label)}${kind} · ${count} edge${count === 1 ? "" : "s"} here`));
      if (nodeEls.get(id).askable && options.onAsk) tip.append(el("em", {}, `Click to ask: ${questionsFor(node)[0]}`));
      const [x, y] = pos.get(id);
      const { w } = box();
      tip.classList.add("show");
      const tw = tip.offsetWidth;
      const th = tip.offsetHeight;
      const tx = clamp(x - tw / 2, 8, w - tw - 8);
      const ty = y - th - radius(id) - 12 < 8 ? y + radius(id) + 12 : y - th - radius(id) - 12;
      tip.style.transform = `translate(${tx.toFixed(0)}px, ${ty.toFixed(0)}px)`;
      said.textContent = `${node.name}, ${kindOf(node.label)}, ${count} edges`;
    }
    function clear() {
      focused = null;
      svg.classList.remove("focus");
      for (const { group } of nodeEls.values()) group.classList.remove("on", "near");
      for (const line of edgeEls) line.classList.remove("near");
      tip.classList.remove("show");
    }
    function ask(id) {
      const node = nodes[index.get(id)];
      if (nodeEls.get(id).askable && options.onAsk) options.onAsk(questionsFor(node)[0]);
    }

    for (const node of nodes) {
      const { group } = nodeEls.get(node.id);
      group.addEventListener("pointerenter", () => { stop(); show(node.id); });
      group.addEventListener("pointerleave", () => { if (focused === node.id) clear(); });
      group.addEventListener("click", (event) => {
        // On touch the first tap shows the node; a second tap on it asks.
        if (event.pointerType === "touch" && focused !== node.id) { stop(); show(node.id); return; }
        ask(node.id);
      });
    }

    // Keyboard: the panel is one tab stop; arrow keys move between nodes, busiest first.
    const keyOrder = [...nodes].sort((a, b) => degree.get(b.id) - degree.get(a.id) || (a.id < b.id ? -1 : 1)).map((node) => node.id);
    stage.tabIndex = 0;
    stage.setAttribute("aria-label", `${title.textContent}. Use the arrow keys to move between nodes; Enter asks about the one shown.`);
    stage.addEventListener("focus", () => { stop(); if (!focused) show(keyOrder[0]); });
    stage.addEventListener("blur", clear);
    stage.addEventListener("keydown", (event) => {
      const at = focused ? keyOrder.indexOf(focused) : -1;
      if (event.key === "ArrowRight" || event.key === "ArrowDown") { show(keyOrder[(at + 1) % keyOrder.length]); event.preventDefault(); }
      else if (event.key === "ArrowLeft" || event.key === "ArrowUp") { show(keyOrder[(at - 1 + keyOrder.length) % keyOrder.length]); event.preventDefault(); }
      else if (event.key === "Enter" && focused) { ask(focused); event.preventDefault(); }
      else if (event.key === "Escape") clear();
    });

    // --- The walks -------------------------------------------------------------------------
    const walks = pickWalks(nodes, out, degree, CYCLES);
    let walk = null; // { plan, parts: [{edge, glow, line, pulse, halo, len, hop}] }
    let clock = 0;
    let cycle = 0;
    let done = walks.length === 0;
    let stopped = false;
    let inView = true;
    let raf = 0;
    let last = null;

    function geometry(edge) {
      const [x1, y1] = pos.get(edge.source);
      const [x2, y2] = pos.get(edge.target);
      const len = Math.hypot(x2 - x1, y2 - y1) || 1;
      const ux = (x2 - x1) / len;
      const uy = (y2 - y1) / len;
      const a = radius(edge.source) + 2;
      const b = radius(edge.target) + 4;
      return { x1: x1 + ux * a, y1: y1 + uy * a, x2: x2 - ux * b, y2: y2 - uy * b, len: Math.max(1, len - a - b) };
    }

    function startWalk(plan) {
      walkLayer.replaceChildren();
      pulseLayer.replaceChildren();
      walkLayer.style.opacity = "1";
      for (const { group } of nodeEls.values()) group.classList.remove("reached");
      const parts = [];
      plan.hops.forEach((hop, h) => {
        for (const edge of hop) {
          const glow = svgEl("line", { class: "walked-glow" });
          const line = svgEl("line", { class: "walked" });
          const pulse = svgEl("circle", { class: "pulse", r: "2.6" });
          const halo = svgEl("circle", { class: "pulse-halo", r: "7" });
          walkLayer.append(glow, line);
          pulse.style.display = "none";
          halo.style.display = "none";
          pulseLayer.append(halo, pulse);
          parts.push({ edge, hop: h, glow, line, pulse, halo, t: -1 });
        }
      });
      walk = { plan, parts };
      nodeEls.get(plan.seed).group.classList.add("reached");
      redrawWalk();
    }

    function redrawWalk() {
      for (const part of walk.parts) {
        const g = geometry(part.edge);
        part.g = g;
        for (const line of [part.glow, part.line]) {
          line.setAttribute("x1", g.x1.toFixed(1));
          line.setAttribute("y1", g.y1.toFixed(1));
          line.setAttribute("x2", g.x2.toFixed(1));
          line.setAttribute("y2", g.y2.toFixed(1));
          line.setAttribute("stroke-dasharray", g.len.toFixed(1));
        }
        setProgress(part, part.t < 0 ? 0 : part.t);
      }
    }

    function setProgress(part, t) {
      part.t = t;
      const { g } = part;
      const offset = (g.len * (1 - ease(clamp(t, 0, 1)))).toFixed(1);
      part.glow.setAttribute("stroke-dashoffset", offset);
      part.line.setAttribute("stroke-dashoffset", offset);
      const moving = t > 0 && t < 1;
      part.pulse.style.display = moving ? "" : "none";
      part.halo.style.display = moving ? "" : "none";
      if (moving) {
        const e = ease(t);
        const px = (g.x1 + (g.x2 - g.x1) * e).toFixed(1);
        const py = (g.y1 + (g.y2 - g.y1) * e).toFixed(1);
        part.pulse.setAttribute("cx", px); part.pulse.setAttribute("cy", py);
        part.halo.setAttribute("cx", px); part.halo.setAttribute("cy", py);
      }
      if (t >= 1 && !part.arrived) {
        part.arrived = true;
        part.line.setAttribute("marker-end", "url(#lg-arrow)");
        nodeEls.get(part.edge.target).group.classList.add("reached");
      }
    }

    function say(hop) {
      if (!ticker) return;
      const parts = walk.parts.filter((part) => part.hop === hop);
      if (!parts.length) return;
      const { edge } = parts[0];
      const name = (id) => nodes[index.get(id)].name;
      ticker.replaceChildren(`hop ${hop + 1}  ${name(edge.source)} -[`, el("b", {}, edge.rel), `]-> ${name(edge.target)}${parts.length > 1 ? `  +${parts.length - 1}` : ""}`);
    }

    function summary() {
      if (!ticker || !walk) return;
      const name = nodes[index.get(walk.plan.seed)].name;
      ticker.replaceChildren("walked ", el("b", {}, `${walk.plan.size} edges`), ` out from ${name}`);
    }

    function finishWalk() {
      if (!walk) return;
      for (const part of walk.parts) setProgress(part, 1);
    }

    // One cycle: hops at HOP intervals, then HOLD, then a fade into the next cycle.
    function tick() {
      const t = clock - START;
      if (t < 0) return;
      const plan = walks[cycle];
      const length = plan.hops.length * HOP + HOLD;
      if (!walk || walk.plan !== plan) { startWalk(plan); say(0); }
      const local = t - cycleStart;
      for (const part of walk.parts) {
        const p = (local - part.hop * HOP) / TRAVEL;
        if (p > 0 && part.t < 1) setProgress(part, Math.min(1, p));
      }
      const hop = Math.floor(local / HOP);
      if (hop !== spoken && hop < plan.hops.length) { spoken = hop; say(hop); }
      if (local >= plan.hops.length * HOP && spoken !== "done") { spoken = "done"; summary(); }
      if (cycle === walks.length - 1 && local >= length - HOLD) { finish(); return; }
      if (local >= length) walkLayer.style.opacity = String(clamp(1 - (local - length) / FADE, 0, 1));
      if (local >= length + FADE) { cycle += 1; cycleStart = local + cycleStart; spoken = -1; walk = null; }
    }
    let cycleStart = 0;
    let spoken = -1;

    function finish() {
      done = true;
      finishWalk();
      walkLayer.style.opacity = "1";
      summary();
      if (raf) cancelAnimationFrame(raf);
      raf = 0;
    }

    function stop() {
      if (stopped) return;
      stopped = true;
      if (walk) finish();
      else done = true;
    }

    const running = () => !done && !stopped && inView && !document.hidden;
    function frame(now) {
      raf = 0;
      if (!running()) { last = null; return; }
      clock += last === null ? 16 : Math.min(64, now - last);
      last = now;
      tick();
      if (running()) raf = requestAnimationFrame(frame);
    }
    function wake() {
      if (!raf && running()) { last = null; raf = requestAnimationFrame(frame); }
    }

    stage.replaceChildren(svg, tip, said);
    place();

    if (reducedMotion()) {
      // Still: the first walk, drawn complete.
      if (walks.length) { startWalk(walks[0]); finishWalk(); summary(); }
      done = true;
    } else if (!done) {
      const io = "IntersectionObserver" in window ? new IntersectionObserver((entries) => {
        inView = entries[entries.length - 1].isIntersecting;
        wake();
      }, { threshold: 0.2 }) : null;
      if (io) io.observe(stage);
      document.addEventListener("visibilitychange", wake);
      const interrupt = () => stop();
      for (const type of ["pointerdown", "keydown", "wheel", "touchstart"]) window.addEventListener(type, interrupt, { once: true, passive: true });
      wake();
    }

    let resizeTimer = 0;
    window.addEventListener("resize", () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => { clear(); place(); }, 120);
    });

    return { nodes: nodes.length, edges: edges.length, stop };
  }

  function legend(list, nodes) {
    const present = new Set(nodes.map((node) => familyOf(node.label)));
    list.replaceChildren();
    for (const family of ["person", "service", "knowledge", "other"]) if (present.has(family)) list.append(legendItem(family));
  }

  window.BrainHero = { mount, legend };
})();
