// Company Brain page: ask the graph, draw the path it walked. Plain JS, no build step.
// Every piece of answer text is inserted with textContent; markup is built from constants only.
(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const demoSteps = $("#demo-steps");
  const form = $("#ask-form");
  const input = $("#q");
  const button = $("#ask-btn");
  const answerBox = $("#answer");
  const statusLine = $("#status");
  const out = $("#out");
  const chips = $("#chips");
  const toolsBody = $("#tools-body");
  const companyLine = $("#company");

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

  // --- Catalogue and example questions ---------------------------------------------------

  async function loadTools() {
    try {
      const response = await fetch("/api/ask/tools");
      const data = await response.json();
      renderChips(data.examples || []);
      renderDemo(data.demo || []);
      renderTools(data.tools || []);
      if (data.company) companyLine.textContent = `Seeded company: ${data.company}.`;
    } catch (error) {
      toolsBody.replaceChildren(el("tr", {}, "")).firstChild.append(el("td", { colspan: "3", class: "note" }, "The catalogue could not be loaded."));
    }
  }

  function renderChips(examples) {
    chips.replaceChildren();
    for (const question of examples) {
      const chip = el("button", { type: "button", class: "chip" }, question);
      chip.addEventListener("click", () => { input.value = question; ask(question); });
      const item = el("li");
      item.append(chip);
      chips.append(item);
    }
  }

  function renderDemo(steps) {
    if (!steps.length) return;
    demoSteps.replaceChildren();
    for (const step of steps) {
      const item = el("li");
      item.append(el("code", {}, step.prompt));
      item.append(el("span", { class: "exp" }, step.expect));
      demoSteps.append(item);
    }
  }

  function renderTools(tools) {
    toolsBody.replaceChildren();
    for (const tool of tools) {
      const row = el("tr");
      const nameCell = el("td", { class: "name" });
      nameCell.append(el("code", {}, tool.name));
      row.append(nameCell);
      row.append(el("td", {}, tool.description));
      row.append(el("td", { class: "mono" }, tool.args.join(", ")));
      toolsBody.append(row);
    }
  }

  // --- Asking ------------------------------------------------------------------------------

  async function ask(question) {
    const q = question.trim();
    if (!q) return;
    button.disabled = true;
    answerBox.classList.add("show");
    statusLine.textContent = "Walking the graph…";
    statusLine.classList.remove("error");
    out.replaceChildren();
    history.replaceState(null, "", `?q=${encodeURIComponent(q)}#ask`);
    try {
      const response = await fetch(`/api/ask?q=${encodeURIComponent(q)}`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      render(data);
    } catch (error) {
      statusLine.textContent = `No answer: ${error.message}`;
      statusLine.classList.add("error");
    } finally {
      button.disabled = false;
    }
  }

  function render(data) {
    statusLine.textContent = "";
    out.replaceChildren();
    out.append(el("p", { class: "text" }, data.answer));

    const edges = data.edges || [];
    if (edges.length) {
      const heading = el("h3", {}, "Path ");
      const meta = el("span", { class: "meta" });
      meta.append("(", el("code", {}, data.tool));
      if (data.depth !== null && data.depth !== undefined) meta.append(`, depth ${data.depth}`);
      meta.append(`, ${edges.length} edge${edges.length === 1 ? "" : "s"})`);
      heading.append(meta);
      out.append(heading);

      const canvas = el("div", { class: "canvas" });
      const { svg, edgeEls, nodeEls } = drawPath(data.nodes || [], edges);
      canvas.append(svg);
      out.append(canvas);
      out.append(legend(data.nodes || []));

      const names = new Map((data.nodes || []).map((node) => [node.id, node.name]));
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
      out.append(list);
    }

    if (data.found && data.candidates && data.candidates.length) {
      out.append(el("p", { class: "note" }, `Also matched: ${data.candidates.join(", ")}. Ask again with that name to switch.`));
    }

    if (data.memories && data.memories.length) {
      out.append(el("h3", {}, "From conversations"));
      const list = el("ul", { class: "memories" });
      for (const memory of data.memories) {
        const where = SCOPE_LABEL[memory.scope] || memory.scope;
        const learned = memory.learned_at ? `, learned ${memory.learned_at}` : "";
        list.append(el("li", {}, `“${memory.text}” — ${memory.author}, ${where}${learned}`));
      }
      out.append(list);
    }

    const picker = data.picked_by === "model" ? "the model" : data.picked_by === "caller" ? "you" : "keywords";
    const read = (data.scopes || []).map((scope) => SCOPE_LABEL[scope] || scope).join(", ");
    const footer = el("p", { class: "footer-line" });
    footer.append("Tool ", el("code", {}, data.tool), `, picked by ${picker}. Read from: ${read}`);
    out.append(footer);
  }

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
  // Columns follow the arrows: a node reached along an outgoing edge sits one column to the
  // right of the node it was reached from, one reached against an edge sits to the left.
  // The walk starts from the node with the most edges, which is the thing the question named.

  const NODE_H = 40;
  const ROW_H = 60;
  const COL_GAP = 84;
  const PAD = 22;

  function layout(nodes, edges) {
    const degree = new Map(nodes.map((node) => [node.id, 0]));
    for (const edge of edges) { degree.set(edge.source, degree.get(edge.source) + 1); degree.set(edge.target, degree.get(edge.target) + 1); }
    const column = new Map();
    const order = [];
    const unvisited = new Set(nodes.map((node) => node.id));
    while (unvisited.size) {
      let start = null;
      for (const id of unvisited) if (start === null || degree.get(id) > degree.get(start)) start = id;
      column.set(start, 0);
      unvisited.delete(start);
      const queue = [start];
      while (queue.length) {
        const id = queue.shift();
        order.push(id);
        for (const edge of edges) {
          if (edge.source === id && unvisited.has(edge.target)) { unvisited.delete(edge.target); column.set(edge.target, column.get(id) + 1); queue.push(edge.target); }
          if (edge.target === id && unvisited.has(edge.source)) { unvisited.delete(edge.source); column.set(edge.source, column.get(id) - 1); queue.push(edge.source); }
        }
      }
    }
    const distinct = [...new Set(column.values())].sort((a, b) => a - b);
    const index = new Map(distinct.map((value, i) => [value, i]));
    const columns = distinct.map(() => []);
    for (const id of order) columns[index.get(column.get(id))].push(id);

    // Rows: keep each node near the rows of the neighbours already placed to its left.
    const row = new Map();
    columns.forEach((ids, c) => {
      if (c > 0) {
        const key = (id) => {
          const rows = [];
          for (const edge of edges) {
            const other = edge.source === id ? edge.target : edge.target === id ? edge.source : null;
            if (other !== null && row.has(other) && index.get(column.get(other)) < c) rows.push(row.get(other));
          }
          return rows.length ? rows.reduce((a, b) => a + b, 0) / rows.length : order.indexOf(id);
        };
        ids.sort((a, b) => key(a) - key(b) || order.indexOf(a) - order.indexOf(b));
      }
      ids.forEach((id, r) => row.set(id, r));
    });
    return { columns, column: (id) => index.get(column.get(id)), row: (id) => row.get(id) };
  }

  function drawPath(nodes, edges) {
    const byId = new Map(nodes.map((node) => [node.id, node]));
    const { columns, column, row } = layout(nodes, edges);
    const width = (id) => Math.min(220, Math.max(96, Math.round(byId.get(id).name.length * 7.4 + 26)));
    const colWidth = columns.map((ids) => Math.max(...ids.map(width)));
    const colX = [];
    let x = PAD;
    columns.forEach((ids, c) => { colX.push(x); x += colWidth[c] + COL_GAP; });
    const totalWidth = x - COL_GAP + PAD;
    const totalHeight = PAD * 2 + Math.max(...columns.map((ids) => ids.length)) * ROW_H - (ROW_H - NODE_H);

    const box = (id) => {
      const c = column(id);
      const w = width(id);
      return { x: colX[c] + (colWidth[c] - w) / 2, y: PAD + row(id) * ROW_H, w, h: NODE_H };
    };

    const svg = svgEl("svg", { viewBox: `0 0 ${totalWidth} ${totalHeight}`, width: totalWidth, height: totalHeight, role: "img" });
    svg.append(svgEl("title")).lastChild.textContent = `The path: ${edges.length} edges over ${nodes.length} nodes`;
    const defs = svgEl("defs");
    const marker = svgEl("marker", { id: "arrow", viewBox: "0 0 10 10", refX: "9", refY: "5", markerWidth: "9", markerHeight: "9", markerUnits: "userSpaceOnUse", orient: "auto-start-reverse" });
    marker.append(svgEl("path", { d: "M0,0 L10,5 L0,10 z", class: "arrow" }));
    defs.append(marker);
    svg.append(defs);

    const edgeLayer = svgEl("g");
    const nodeLayer = svgEl("g");
    svg.append(edgeLayer, nodeLayer);

    const edgeEls = [];
    for (const edge of edges) {
      const a = box(edge.source);
      const b = box(edge.target);
      let d, mid;
      if (column(edge.target) > column(edge.source)) {
        const p0 = [a.x + a.w, a.y + a.h / 2], p3 = [b.x, b.y + b.h / 2];
        const dx = Math.max(30, (p3[0] - p0[0]) / 2);
        const p1 = [p0[0] + dx, p0[1]], p2 = [p3[0] - dx, p3[1]];
        d = `M${p0} C${p1} ${p2} ${p3}`;
        mid = bezierMid(p0, p1, p2, p3);
      } else if (column(edge.target) < column(edge.source)) {
        const p0 = [a.x, a.y + a.h / 2], p3 = [b.x + b.w, b.y + b.h / 2];
        const dx = Math.max(30, (p0[0] - p3[0]) / 2);
        const p1 = [p0[0] - dx, p0[1]], p2 = [p3[0] + dx, p3[1]];
        d = `M${p0} C${p1} ${p2} ${p3}`;
        mid = bezierMid(p0, p1, p2, p3);
      } else {
        const p0 = [a.x + a.w, a.y + a.h / 2], p3 = [b.x + b.w, b.y + b.h / 2];
        const bulge = 46 + Math.abs(a.y - b.y) / 6;
        const p1 = [p0[0] + bulge, p0[1]], p2 = [p3[0] + bulge, p3[1]];
        d = `M${p0} C${p1} ${p2} ${p3}`;
        mid = bezierMid(p0, p1, p2, p3);
      }
      const group = svgEl("g", { class: "edge" });
      group.append(svgEl("path", { d, "marker-end": "url(#arrow)" }));
      const label = svgEl("text", { x: mid[0], y: mid[1] - 5, "text-anchor": "middle" });
      label.textContent = edge.rel;
      group.append(label);
      edgeLayer.append(group);
      edgeEls.push(group);
    }

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
      group.append(svgEl("title")).lastChild.textContent = node.label ? `${node.name} (${kindOf(node.label)})` : node.name;
      nodeLayer.append(group);
      nodeEls.set(node.id, group);
    }
    return { svg, edgeEls, nodeEls };
  }

  const bezierMid = (p0, p1, p2, p3) => [0, 1].map((i) => (p0[i] + 3 * p1[i] + 3 * p2[i] + p3[i]) / 8);
  const clip = (text, max) => (text.length <= max ? text : `${text.slice(0, Math.max(1, max - 1))}…`);

  // --- Proof: the same questions with the graph switched off --------------------------------

  const proofStatus = $("#proof-status");
  const proofTable = $("#proof-table");
  const proofBody = $("#proof-body");

  async function loadProof() {
    try {
      const response = await fetch("/api/ask/proof");
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "The proof did not run.");
      proofStatus.textContent = `${data.collapsed} of ${data.total} capabilities collapse without FalkorDB.`;
      proofBody.replaceChildren();
      for (const row of data.rows || []) {
        const line = el("tr");
        line.append(el("td", {}, row.question));
        const toolCell = el("td", { class: "name" });
        toolCell.append(el("code", {}, row.tool));
        line.append(toolCell);
        line.append(el("td", {}, row.with_graph.found ? `${row.with_graph.steps} edges` : "refused"));
        line.append(el("td", {}, row.without_graph.found ? `${row.without_graph.steps} edges` : "refused"));
        line.append(el("td", {}, row.verdict));
        proofBody.append(line);
      }
      proofTable.hidden = false;
    } catch (error) {
      proofStatus.textContent = "The proof could not be loaded.";
    }
  }

  // --- Wiring ------------------------------------------------------------------------------

  form.addEventListener("submit", (event) => { event.preventDefault(); ask(input.value); });
  loadTools();
  loadProof();
  const initial = new URLSearchParams(location.search).get("q");
  if (initial) { input.value = initial; ask(initial); }
})();
