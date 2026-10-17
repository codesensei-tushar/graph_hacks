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
  const kindOf = (label) => (label || "").replace(/([a-z])([A-Z])/g, "$1 $2").replace(/^./, (c) => c.toUpperCase());
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
    const names = { person: "People", service: "Services", knowledge: "Documents, tickets, decisions, pull requests, chat", other: "Teams, repos, channels" };
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

  function drawPath(nodes, edges) {
    const byId = new Map(nodes.map((node) => [node.id, node]));
    const { columns, column, row, ins, outs } = layout(nodes, edges);
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
    const svg = svgEl("svg", {
      viewBox: `0 0 ${totalWidth} ${totalHeight}`,
      role: "img",
      style: `width:100%;height:auto;max-width:${totalWidth}px;min-width:${Math.min(totalWidth, 560)}px`,
    });
    const caption = svgEl("title");
    caption.textContent = `The path: ${edges.length} edges over ${nodes.length} nodes`;
    svg.append(caption);
    const defs = svgEl("defs");
    const marker = svgEl("marker", { id: "arrow", viewBox: "0 0 10 10", refX: "9", refY: "5", markerWidth: "9", markerHeight: "9", markerUnits: "userSpaceOnUse", orient: "auto-start-reverse" });
    marker.append(svgEl("path", { d: "M0,0 L10,5 L0,10 z", class: "arrow" }));
    defs.append(marker);
    svg.append(defs);

    const edgeLayer = svgEl("g");
    const nodeLayer = svgEl("g");
    svg.append(edgeLayer, nodeLayer);

    const edgeEls = [];
    const labels = [];
    for (const edge of edges) {
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
      group.append(svgEl("path", { d: `M${p0} C${p1} ${p2} ${p3}`, "marker-end": "url(#arrow)" }));
      const t = labelX === null ? 0.5 : tAtX(p0, p1, p2, p3, labelX);
      const point = bezierAt(p0, p1, p2, p3, t);
      const label = svgEl("text", { "text-anchor": "middle" });
      label.textContent = edge.rel;
      group.append(label);
      labels.push({ label, x: point[0], y: point[1] - 4 });
      edgeLayer.append(group);
      edgeEls.push(group);
    }
    spreadLabels(labels);

    const nodeEls = new Map();
    for (const node of nodes) {
      const b = box(node.id);
      const color = FAMILY_COLOR[familyOf(node.label)];
      const group = svgEl("g", { class: "node", transform: `translate(${b.x},${b.y})` });
      group.append(svgEl("rect", { width: b.w, height: b.h, rx: 8, ry: 8, fill: `color-mix(in oklab, ${color} 16%, var(--surface))`, stroke: color }));
      const name = svgEl("text", { class: "name", x: b.w / 2, y: node.label ? 17 : 24, "text-anchor": "middle" });
      name.textContent = clip(node.name, Math.floor((b.w - 16) / 7.4));
      group.append(name);
      if (node.label) {
        const kind = svgEl("text", { class: "kind", x: b.w / 2, y: 31, "text-anchor": "middle" });
        kind.textContent = kindOf(node.label);
        group.append(kind);
      }
      const tip = svgEl("title");
      tip.textContent = node.label ? `${node.name} (${kindOf(node.label)})` : node.name;
      group.append(tip);
      nodeLayer.append(group);
      nodeEls.set(node.id, group);
    }
    return { svg, edgeEls, nodeEls };
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

  // The heading, drawing, legend and the `A -[REL]-> B` lines, with each line and its edge
  // highlighted together on hover. ``meta`` names the tool and depth when there is one.
  function renderPath(container, nodes, edges, meta = {}) {
    if (!edges.length) return;
    const heading = el("h3", {}, `${meta.title || "Path"} `);
    const info = el("span", { class: "meta" });
    info.append("(");
    if (meta.tool) info.append(el("code", {}, meta.tool), ", ");
    if (meta.depth !== null && meta.depth !== undefined) info.append(`depth ${meta.depth}, `);
    info.append(`${edges.length} edge${edges.length === 1 ? "" : "s"})`);
    heading.append(info);
    container.append(heading);

    const canvas = el("div", { class: "canvas" });
    const { svg, edgeEls, nodeEls } = drawPath(nodes, edges);
    canvas.append(svg);
    container.append(canvas);
    container.append(legend(nodes));

    const names = new Map(nodes.map((node) => [node.id, node.name]));
    const list = el("ul", { class: "steps" });
    edges.forEach((edge, index) => {
      const item = el("li", {}, `${names.get(edge.source)} -[${edge.rel}]-> ${names.get(edge.target)}`);
      const hot = (on) => {
        item.classList.toggle("hot", on);
        edgeEls[index].classList.toggle("hot", on);
        nodeEls.get(edge.source).classList.toggle("hot", on);
        nodeEls.get(edge.target).classList.toggle("hot", on);
      };
      item.addEventListener("mouseenter", () => hot(true));
      item.addEventListener("mouseleave", () => hot(false));
      edgeEls[index].addEventListener("mouseenter", () => hot(true));
      edgeEls[index].addEventListener("mouseleave", () => hot(false));
      list.append(item);
    });
    container.append(list);
  }

  window.BrainGraph = { el, renderPath, SCOPE_LABEL };
})();
