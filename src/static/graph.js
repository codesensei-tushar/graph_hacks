// Company Brain: draw a path of nodes and edges, shared by the ask page and the team pages.
// Plain JS, no build step. Every piece of text is inserted with textContent.
(() => {
  "use strict";

  // Node families, so a reader can tell people from services from knowledge at a glance.
  // Identity is never color alone: every node also carries its name and its kind.
  const FAMILY = {
    Person: "person", SlackUser: "person", GitHubUser: "person",
    Service: "service",
    Document: "knowledge", Ticket: "knowledge", Decision: "knowledge", PullRequest: "knowledge", Chat: "knowledge", Memory: "knowledge",
  };
  const FAMILY_COLOR = { person: "var(--person)", service: "var(--service)", knowledge: "var(--knowledge)", other: "var(--other)" };
  const SCOPE_LABEL = { org_shared: "public channels" };

  const familyOf = (label) => FAMILY[label] || "other";
  // The line under a node name is lowercase like a Service's kind ("person", "pull request");
  // product names keep their case.
  const KIND_WORD = { SlackUser: "Slack user", GitHubUser: "GitHub user" };
  const kindOf = (label) => KIND_WORD[label] || (label || "").replace(/([a-z])([A-Z])/g, "$1 $2").toLowerCase();
  const el = (tag, attrs = {}, text) => {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const svgEl = (tag, attrs = {}) => {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    return node;
  };

  function legend(nodes) {
    const seen = new Set(nodes.map((node) => familyOf(node.label)));
    const list = el("ul", { class: "legend" });
    const names = { person: "People", service: "What teams run", knowledge: "Documents, tickets, decisions, pull requests, chat", other: "Teams, repos, channels" };
    for (const family of ["person", "service", "knowledge", "other"]) {
      if (!seen.has(family)) continue;
      const item = el("li");
      item.append(el("span", { class: "sw", style: `--c:${FAMILY_COLOR[family]}` }), names[family]);
      list.append(item);
    }
    return list;
  }

  // --- Drawing the path --------------------------------------------------------------------
  // A small layered layout in which every edge points left to right. A node's column is the
  // longest chain of edges leading into it; a node nothing points at moves right until it sits
  // just before the nearest thing it points at. Rows follow the neighbours (barycentre sweeps)
  // and every column is centred, so a hub sits level with what surrounds it.

  const NODE_H = 40;
  const ROW_H = 58;
  const COL_GAP = 112;
  const PAD = 22;
  const LABEL_GAP = 13;

  function layout(nodes, edges) {
    const ids = nodes.map((node) => node.id);
    const outs = new Map(ids.map((id) => [id, []]));
    const ins = new Map(ids.map((id) => [id, []]));
    for (const edge of edges) {
      if (edge.source === edge.target) continue;
      outs.get(edge.source).push(edge.target);
      ins.get(edge.target).push(edge.source);
    }
    const layer = new Map();
    const visiting = new Set();
    const depth = (id) => {
      if (layer.has(id)) return layer.get(id);
      if (visiting.has(id)) return -1; // an edge that closes a cycle does not push a column
      visiting.add(id);
      let best = 0;
      for (const parent of ins.get(id)) {
        const d = depth(parent);
        if (d >= 0) best = Math.max(best, d + 1);
      }
      visiting.delete(id);
      layer.set(id, best);
      return best;
    };
    ids.forEach(depth);
    for (const id of ids) {
      if (ins.get(id).length || !outs.get(id).length) continue;
      const nearest = Math.min(...outs.get(id).map((child) => layer.get(child)));
      if (nearest - 1 > layer.get(id)) layer.set(id, nearest - 1);
    }

    const count = Math.max(...layer.values()) + 1;
    const columns = Array.from({ length: count }, () => []);
    for (const id of ids) columns[layer.get(id)].push(id);
    const pos = new Map();
    const place = () => columns.forEach((col) => col.forEach((id, r) => pos.set(id, r - (col.length - 1) / 2)));
    const sweep = (order, side) => {
      for (const c of order) {
        const key = new Map(columns[c].map((id) => {
          const near = side.get(id).filter((other) => pos.has(other));
          return [id, near.length ? near.reduce((sum, other) => sum + pos.get(other), 0) / near.length : pos.get(id)];
        }));
        columns[c].sort((a, b) => key.get(a) - key.get(b));
        columns[c].forEach((id, r) => pos.set(id, r - (columns[c].length - 1) / 2));
      }
    };
    place();
    const down = [...columns.keys()].slice(1);
    const up = [...columns.keys()].reverse().slice(1);
    sweep(down, ins);
    sweep(up, outs);
    sweep(down, ins);
    const row = new Map();
    columns.forEach((col) => col.forEach((id, r) => row.set(id, r)));
    return { columns, column: (id) => layer.get(id), row: (id) => row.get(id), ins, outs };
  }

  // The walk: edges draw in the order the tool returned them, each growing from its source to
  // its target; a node fades in as it is first reached. CSS runs it from --d on each element,
  // so reduced motion (no animations) shows the finished drawing at once.
  const WALK_STAGGER = 90;
  const WALK_DRAW = 250;
  let drawings = 0;

  function walkTimes(edges) {
    const reached = new Map();
    const reach = (id, at) => { if (!reached.has(id) || reached.get(id) > at) reached.set(id, at); };
    const starts = edges.map((edge, i) => {
      const start = i * WALK_STAGGER;
      reach(edge.source, start);
      reach(edge.target, start + WALK_DRAW);
      return start;
    });
    return { starts, reached };
  }

  function drawPath(nodes, edges) {
    const byId = new Map(nodes.map((node) => [node.id, node]));
    const { columns, column, row, ins, outs } = layout(nodes, edges);
    const { starts, reached } = walkTimes(edges);
    const width = (id) => Math.min(200, Math.max(96, Math.round(byId.get(id).name.length * 7.4 + 26)));
    const colWidth = columns.map((ids) => Math.max(...ids.map(width)));
    const colX = [];
    let x = PAD;
    columns.forEach((ids, c) => { colX.push(x); x += colWidth[c] + COL_GAP; });
    const tallest = Math.max(...columns.map((ids) => ids.length));
    const sameColumnEdges = edges.some((edge) => column(edge.source) >= column(edge.target));
    const totalWidth = x - COL_GAP + PAD + (sameColumnEdges ? 60 : 0);
    const totalHeight = PAD * 2 + tallest * ROW_H - (ROW_H - NODE_H);

    const box = (id) => {
      const c = column(id);
      const w = width(id);
      const offset = ((tallest - columns[c].length) * ROW_H) / 2;
      return { x: colX[c] + (colWidth[c] - w) / 2, y: PAD + offset + row(id) * ROW_H, w, h: NODE_H };
    };

    // Scales down to fit the column on a wide screen; on a phone it keeps a readable size and scrolls.
    drawings += 1;
    const titleId = `path-title-${drawings}`;
    const descId = `path-desc-${drawings}`;
    const svg = svgEl("svg", {
      viewBox: `0 0 ${totalWidth} ${totalHeight}`,
      role: "group",
      "aria-labelledby": `${titleId} ${descId}`,
      style: `width:100%;height:auto;max-width:${totalWidth}px;min-width:${Math.min(totalWidth, 560)}px`,
    });
    const caption = svgEl("title", { id: titleId });
    caption.textContent = `The path: ${edges.length} edge${edges.length === 1 ? "" : "s"} over ${nodes.length} nodes`;
    const desc = svgEl("desc", { id: descId });
    desc.textContent = "Each box is a node, named with its label; each arrow is an edge in its true direction. The list of edges after the drawing reads the same path as text.";
    svg.append(caption, desc);

    const edgeLayer = svgEl("g");
    const nodeLayer = svgEl("g");
    svg.append(edgeLayer, nodeLayer);

    const edgeEls = [];
    const labels = [];
    edges.forEach((edge, index) => {
      const a = box(edge.source);
      const b = box(edge.target);
      const from = column(edge.source);
      const to = column(edge.target);
      let p0, p1, p2, p3, labelX;
      if (to > from) {
        p0 = [a.x + a.w, a.y + a.h / 2];
        p3 = [b.x, b.y + b.h / 2];
        const dx = Math.max(30, (p3[0] - p0[0]) / 2);
        p1 = [p0[0] + dx, p0[1]];
        p2 = [p3[0] - dx, p3[1]];
        // Converging edges are labelled near their source, diverging ones near their target,
        // so labels spread out where the edges do instead of piling up at the hub.
        const converging = ins.get(edge.target).length > 1 && outs.get(edge.source).length === 1;
        const diverging = outs.get(edge.source).length > 1 && ins.get(edge.target).length === 1;
        const gap = diverging ? to - 1 : from;
        const gapStart = colX[gap] + colWidth[gap];
        labelX = gapStart + COL_GAP * (converging ? 0.38 : diverging ? 0.62 : 0.5);
      } else {
        // Same column, or against the columns because of a cycle: bow out to the right.
        p0 = [a.x + a.w, a.y + a.h / 2];
        p3 = [b.x + b.w, b.y + b.h / 2];
        const right = Math.max(p0[0], p3[0]);
        const bulge = 46 + Math.abs(a.y - b.y) / 6;
        p1 = [right + bulge, p0[1]];
        p2 = [right + bulge, p3[1]];
        labelX = null;
      }
      const group = svgEl("g", { class: "edge" });
      group.style.setProperty("--d", `${starts[index]}ms`);
      // pathLength="1" lets the walk grow the line with a dash of length 1, whatever its real length.
      group.append(svgEl("path", { class: "line", d: `M${p0} C${p1} ${p2} ${p3}`, pathLength: "1" }));
      // The arrowhead sits on the curve's end tangent, so it always shows the edge's true direction.
      const angle = (Math.atan2(p3[1] - p2[1], p3[0] - p2[0]) * 180) / Math.PI;
      group.append(svgEl("path", { class: "arrow", d: "M-9,-4.5 L0,0 L-9,4.5 z", transform: `translate(${p3}) rotate(${angle.toFixed(1)})` }));
      const t = labelX === null ? 0.5 : tAtX(p0, p1, p2, p3, labelX);
      const point = bezierAt(p0, p1, p2, p3, t);
      const label = svgEl("text", { "text-anchor": "middle" });
      label.textContent = edge.rel;
      group.append(label);
      labels.push({ label, x: point[0], y: point[1] - 4 });
      edgeLayer.append(group);
      edgeEls.push(group);
    });
    spreadLabels(labels);

    const nodeEls = new Map();
    for (const node of nodes) {
      const b = box(node.id);
      const color = FAMILY_COLOR[familyOf(node.label)];
      // A Service says what it is (process, system, playbook); every other node names its label.
      const kind = node.label === "Service" && node.kind ? node.kind : node.label ? kindOf(node.label) : "";
      const group = svgEl("g", { class: "node", transform: `translate(${b.x},${b.y})`, tabindex: "0", "aria-label": kind ? `${node.name}, ${kind}` : node.name });
      const body = svgEl("g", { class: "body" });
      body.style.setProperty("--d", `${reached.get(node.id) || 0}ms`);
      body.append(svgEl("rect", { width: b.w, height: b.h, rx: 8, ry: 8, fill: `color-mix(in oklab, ${color} 16%, var(--surface))`, stroke: color }));
      const name = svgEl("text", { class: "name", x: b.w / 2, y: node.label ? 17 : 24, "text-anchor": "middle" });
      name.textContent = clip(node.name, Math.floor((b.w - 16) / 7.4));
      body.append(name);
      if (node.label) {
        const kindText = svgEl("text", { class: "kind", x: b.w / 2, y: 31, "text-anchor": "middle" });
        kindText.textContent = kind;
        body.append(kindText);
      }
      const tip = svgEl("title");
      tip.textContent = kind ? `${node.name} (${kind})` : node.name;
      group.append(body, tip);
      nodeLayer.append(group);
      nodeEls.set(node.id, group);
    }
    return { svg, edgeEls, nodeEls, starts };
  }

  // Labels that share a column gap are nudged apart vertically so none sits on another.
  function spreadLabels(labels) {
    const byGap = new Map();
    for (const item of labels) {
      const key = Math.round(item.x / 24);
      if (!byGap.has(key)) byGap.set(key, []);
      byGap.get(key).push(item);
    }
    for (const group of byGap.values()) {
      group.sort((a, b) => a.y - b.y);
      for (let i = 1; i < group.length; i += 1) {
        if (group[i].y - group[i - 1].y < LABEL_GAP) group[i].y = group[i - 1].y + LABEL_GAP;
      }
    }
    for (const item of labels) {
      item.label.setAttribute("x", item.x.toFixed(1));
      item.label.setAttribute("y", item.y.toFixed(1));
    }
  }

  const bezierAt = (p0, p1, p2, p3, t) => {
    const u = 1 - t;
    return [0, 1].map((i) => u * u * u * p0[i] + 3 * u * u * t * p1[i] + 3 * u * t * t * p2[i] + t * t * t * p3[i]);
  };
  // The parameter at which the curve reaches x. The curves here only move right, so x grows with t.
  const tAtX = (p0, p1, p2, p3, x) => {
    let lo = 0;
    let hi = 1;
    for (let i = 0; i < 24; i += 1) {
      const mid = (lo + hi) / 2;
      if (bezierAt(p0, p1, p2, p3, mid)[0] < x) lo = mid; else hi = mid;
    }
    return (lo + hi) / 2;
  };
  const clip = (text, max) => (text.length <= max ? text : `${text.slice(0, Math.max(1, max - 1))}…`);

  // The heading, drawing, legend and the `A -[REL]-> B` lines. The walk runs once on arrival
  // and again from the Replay button; hovering or focusing a node, an edge or a line lights
  // that part of the drawing and its lines together. ``meta`` names the tool and depth.
  function renderPath(container, nodes, edges, meta = {}) {
    if (!edges.length) return;
    const head = el("div", { class: "path-head" });
    const heading = el("h3", {}, `${meta.title || "Path"} `);
    const info = el("span", { class: "meta" });
    info.append("(");
    if (meta.tool) info.append(el("code", {}, meta.tool), ", ");
    if (meta.depth !== null && meta.depth !== undefined) info.append(`depth ${meta.depth}, `);
    info.append(`${edges.length} edge${edges.length === 1 ? "" : "s"})`);
    heading.append(info);
    const replay = el("button", { type: "button", class: "replay" }, "Replay the walk");
    head.append(heading, replay);
    container.append(head);

    const canvas = el("div", { class: "canvas go" });
    const { svg, edgeEls, nodeEls, starts } = drawPath(nodes, edges);
    canvas.append(svg);
    // On a phone the drawing keeps a readable size and scrolls sideways: a fade at each edge with
    // more beyond it, and a "scroll →" cue until the reader starts scrolling, say so.
    const frame = el("div", { class: "canvas-frame" });
    frame.append(canvas, el("span", { class: "scroll-cue", "aria-hidden": "true" }, "scroll →"));
    container.append(frame);
    const showEdges = () => {
      const rest = canvas.scrollWidth - canvas.clientWidth;
      frame.classList.toggle("more-left", rest > 1 && canvas.scrollLeft > 1);
      frame.classList.toggle("more-right", rest > 1 && canvas.scrollLeft < rest - 1);
    };
    canvas.addEventListener("scroll", showEdges, { passive: true });
    if ("ResizeObserver" in window) new ResizeObserver(showEdges).observe(canvas);
    else showEdges();
    container.append(legend(nodes));

    const names = new Map(nodes.map((node) => [node.id, node.name]));
    const list = el("ul", { class: "steps go" });
    const items = edges.map((edge, index) => {
      const item = el("li", {}, `${names.get(edge.source)} -[${edge.rel}]-> ${names.get(edge.target)}`);
      item.style.setProperty("--d", `${starts[index]}ms`);
      list.append(item);
      return item;
    });
    container.append(list);

    const light = (indexes, on, nodeId) => {
      for (const index of indexes) {
        items[index].classList.toggle("hot", on);
        edgeEls[index].classList.toggle("hot", on);
        nodeEls.get(edges[index].source).classList.toggle("hot", on);
        nodeEls.get(edges[index].target).classList.toggle("hot", on);
      }
      if (nodeId) nodeEls.get(nodeId).classList.toggle("hot", on);
    };
    edges.forEach((edge, index) => {
      for (const target of [items[index], edgeEls[index]]) {
        target.addEventListener("mouseenter", () => light([index], true));
        target.addEventListener("mouseleave", () => light([index], false));
      }
    });
    for (const [id, group] of nodeEls) {
      const touching = edges.map((edge, index) => (edge.source === id || edge.target === id ? index : -1)).filter((index) => index >= 0);
      for (const [on, off] of [["mouseenter", "mouseleave"], ["focus", "blur"]]) {
        group.addEventListener(on, () => light(touching, true, id));
        group.addEventListener(off, () => light(touching, false, id));
      }
    }

    replay.addEventListener("click", () => {
      for (const part of [canvas, list]) part.classList.remove("go");
      void canvas.offsetWidth; // restart the CSS animations
      for (const part of [canvas, list]) part.classList.add("go");
    });
  }

  window.BrainGraph = { el, svgEl, renderPath, familyOf, kindOf, FAMILY_COLOR, SCOPE_LABEL };
})();
