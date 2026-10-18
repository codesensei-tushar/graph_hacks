// Company Brain: the graph explorer at /explore. Start from any node, step to a neighbour by
// clicking it, follow one relationship with `neighbors`, and keep the walk as a breadcrumb.
// Plain JS, no build step; every piece of text goes in with textContent. The address is the
// current step (/explore?node=<id>, &depth=2, or &rel=<TYPE>&dir=<out|in|both>&hops=<n> for a
// followed relationship), so any step can be shared, and Back walks back.
(() => {
  "use strict";

  const G = window.BrainGraph;
  const $ = (id) => document.getElementById(id);
  const el = G.el;
  // What a click can step to: the labels explore resolves. Memories, repos and channels are leaves.
  const STEPPABLE = new Set(["Service", "Person", "Team", "Document", "Ticket", "Decision", "PullRequest"]);
  const EXPLORE_DEPTHS = [1, 2];
  const NEIGHBOR_DEPTHS = [1, 2, 3];
  const FOLLOW_HOPS = 2;
  const KIND_WORD = { PullRequest: "pull request" };

  const parts = {
    form: $("explorer-form"), input: $("explorer-input"), start: $("explorer-start"), starters: $("starters"),
    walk: $("walk"), walkList: $("walk-list"), stage: $("stage"), name: $("focus-name"), kind: $("focus-kind"),
    depths: $("depths"), answer: $("explorer-answer"), nearest: $("explorer-nearest"), graph: $("explorer-graph"),
    hint: $("explorer-hint"), ask: $("explorer-ask"), follows: $("explorer-follows"), edges: $("explorer-edges"),
    said: $("explorer-said"), memories: $("explorer-memories"),
  };

  // The walk: every step taken, and which one is on screen. A new step from an earlier one drops
  // the steps after it, as a browser's history does.
  let walk = [];
  let at = -1;
  let loading = 0;

  // --- Addresses -------------------------------------------------------------------------------

  function stepFromUrl() {
    const params = new URLSearchParams(location.search);
    const node = (params.get("node") || "").trim();
    if (!node) return null;
    const rel = (params.get("rel") || "").trim();
    if (rel) return { node, rel, dir: params.get("dir") || "both", hops: Number(params.get("hops")) || FOLLOW_HOPS };
    return { node, depth: Number(params.get("depth")) || 1 };
  }

  function urlFor(step) {
    const params = new URLSearchParams({ node: step.node });
    if (step.rel) {
      params.set("rel", step.rel);
      params.set("dir", step.dir);
      params.set("hops", String(step.hops));
    } else if (step.depth > 1) {
      params.set("depth", String(step.depth));
    }
    return `/explore?${params}`;
  }

  function apiFor(step) {
    const node = encodeURIComponent(step.node);
    if (step.rel) return `/api/explore/neighbors?node=${node}&relation=${encodeURIComponent(step.rel)}&direction=${encodeURIComponent(step.dir)}&depth=${step.hops}`;
    return `/api/explore?node=${node}&depth=${step.depth}`;
  }

  async function getJson(url) {
    let response;
    try {
      response = await fetch(url, { headers: { Accept: "application/json" } });
    } catch (error) {
      return { error: "The graph could not be reached. Check the connection and try again." };
    }
    let body = {};
    try { body = await response.json(); } catch (error) { body = {}; }
    if (response.status === 429) return { error: body.error || "Too many requests from this address; wait a minute and try again." };
    if (!response.ok) return { error: body.error || `The graph answered ${response.status}.` };
    return body;
  }

  // --- Walking ---------------------------------------------------------------------------------

  // Show one step. ``how`` is "push" for a new step, "replace" to fix the address of the current
  // one (a name typed resolves to an id), or "none" when Back or the breadcrumb chose it.
  async function go(step, how = "push") {
    const ticket = ++loading;
    busy(true);
    const data = await getJson(apiFor(step));
    if (ticket !== loading) return; // a later click won
    busy(false);
    if (data.error) {
      say(data.error, true);
      return;
    }
    render(data, step);
    if (!data.found || !data.focus) return;
    const settled = { ...step, node: data.focus.id, name: data.focus.name, label: data.focus.label, kind: data.focus.kind };
    if (how === "push") {
      walk = walk.slice(0, at + 1);
      const last = walk[walk.length - 1];
      if (last && urlFor(last) === urlFor(settled)) walk[walk.length - 1] = settled;
      else walk.push(settled);
      at = walk.length - 1;
      history.pushState({ walk, at }, "", urlFor(settled));
    } else if (how === "replace") {
      if (at < 0) { walk = [settled]; at = 0; } else walk[at] = settled;
      history.replaceState({ walk, at }, "", urlFor(settled));
    }
    drawWalk();
    document.title = `${settled.name} · Explore · Company Brain`;
  }

  function busy(on) {
    parts.stage.setAttribute("aria-busy", on ? "true" : "false");
    parts.stage.classList.toggle("loading", on);
    if (on) parts.answer.textContent = "Walking the graph…";
  }

  // Answers mark a decision's title as *title*, as Slack does; here it reads as bold, still inserted as text.
  function say(text, refused) {
    parts.answer.replaceChildren();
    String(text).split(/\*([^*\n]+)\*/).forEach((piece, index) => {
      if (piece) parts.answer.append(index % 2 ? el("strong", {}, piece) : piece);
    });
    parts.answer.classList.toggle("refused", Boolean(refused));
  }

  // --- Rendering -------------------------------------------------------------------------------

  function render(data, step) {
    const focus = data.focus;
    parts.name.textContent = focus ? focus.name : data.found ? "" : "Not in the graph";
    parts.kind.textContent = focus ? describe(focus, data) : "";
    say(data.answer || "", !data.found);
    drawNearest(data.candidates || [], data.found);
    drawDepths(data, step);
    drawGraph(data, step);
    drawAsk(focus);
    drawFollows(data);
    drawMemories(data.memories || []);
  }

  function describe(focus, data) {
    const kind = focus.label === "Service" && focus.kind ? focus.kind : KIND_WORD[focus.label] || (focus.label || "").toLowerCase();
    const edges = data.edges.length;
    const walked = data.relation ? `, following ${data.relation} ${data.direction === "out" ? "out" : data.direction === "in" ? "in" : "either way"}` : "";
    return `${kind}${walked} · ${edges} edge${edges === 1 ? "" : "s"}`;
  }

  function drawNearest(candidates, found) {
    parts.nearest.replaceChildren();
    parts.nearest.hidden = found || !candidates.length;
    for (const candidate of candidates) {
      const item = el("li");
      const button = el("button", { type: "button", class: "chip" }, candidate.name);
      button.addEventListener("click", () => go({ node: candidate.id, depth: 1 }));
      item.append(button);
      parts.nearest.append(item);
    }
  }

  function drawDepths(data, step) {
    parts.depths.replaceChildren();
    parts.depths.hidden = !data.found;
    if (!data.found) return;
    const options = step.rel ? NEIGHBOR_DEPTHS : EXPLORE_DEPTHS;
    const current = step.rel ? step.hops : step.depth;
    for (const value of options) {
      const button = el("button", { type: "button", "aria-pressed": String(value === current) }, `${value} hop${value === 1 ? "" : "s"}`);
      button.addEventListener("click", () => {
        if (value === current) return;
        // The same node looked at further: the current step changes, the walk does not grow.
        go(step.rel ? { ...step, node: data.focus.id, hops: value } : { ...step, node: data.focus.id, depth: value }, "replace");
      });
      parts.depths.append(button);
    }
  }

  function drawGraph(data, step) {
    parts.graph.replaceChildren();
    parts.edges.replaceChildren();
    parts.hint.hidden = true;
    if (!data.found) return;
    if (!data.edges.length) {
      parts.edges.append(el("p", { class: "explorer-sub" }, "Nothing you can read is linked to it."));
      return;
    }
    const title = step.rel ? `${data.relation} from ${data.focus.name}` : `Around ${data.focus.name}`;
    G.renderPath(parts.graph, data.nodes, data.edges, { title, tool: data.tool, depth: data.depth });
    // The edge lines sit in the side panel; moving the element keeps their hover links to the drawing.
    const steps = parts.graph.querySelector(".steps");
    if (steps) parts.edges.append(steps);
    stepFromNodes(data);
    parts.hint.hidden = false;
  }

  // renderPath draws one group per node, in the order of ``nodes``. Each neighbour that explore
  // can resolve becomes a step: click it, or focus it and press Enter or Space.
  function stepFromNodes(data) {
    const groups = parts.graph.querySelectorAll(".canvas .node");
    groups.forEach((group, index) => {
      const node = data.nodes[index];
      if (!node) return;
      if (node.id === data.focus.id) {
        group.classList.add("focus");
        group.setAttribute("aria-current", "true");
        return;
      }
      if (!STEPPABLE.has(node.label)) return;
      group.classList.add("steppable");
      group.setAttribute("role", "button");
      group.setAttribute("aria-label", `${group.getAttribute("aria-label") || node.name}. Step here`);
      const step = () => go({ node: node.id, depth: 1 });
      group.addEventListener("click", step);
      group.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          step();
        }
      });
    });
  }

  // The ask page's own questions for a node when graph.js offers them, else a plain who-owns or what-breaks.
  function questions(node) {
    if (typeof G.questionsFor === "function") {
      const offered = G.questionsFor(node);
      if (offered.length) return offered;
    }
    const lower = /^[A-Z][a-z]/.test(node.name) ? node.name[0].toLowerCase() + node.name.slice(1) : node.name;
    if (node.label === "Service") return [`who owns ${node.name}`, `what breaks if ${node.name} goes down`];
    if (node.label === "Person") return [`what if ${node.name} leaves`];
    if (node.label === "Team") return [`who is on the ${node.name} team`];
    if (node.label === "Decision") return [`why did we ${lower}`];
    return [`who knows ${node.name}`];
  }

  function drawAsk(focus) {
    parts.ask.replaceChildren();
    if (!focus) return;
    for (const question of questions(focus)) {
      const item = el("li");
      item.append(el("a", { class: "chip", href: `/?q=${encodeURIComponent(question)}` }, question));
      parts.ask.append(item);
    }
  }

  function drawFollows(data) {
    parts.follows.replaceChildren();
    const follows = data.found ? data.follows || [] : [];
    if (!follows.length) {
      parts.follows.append(el("li", { class: "explorer-sub" }, data.found ? "No relationship from here to follow." : "Find a node first."));
      return;
    }
    for (const follow of follows) {
      const item = el("li");
      const button = el("button", { type: "button", class: "follow" });
      const arrow = follow.direction === "out" ? "→" : "←";
      button.append(el("span", { class: "follow-words" }, follow.phrase), el("code", {}, `${arrow} ${follow.rel}`), el("span", { class: "follow-count" }, String(follow.count)));
      button.setAttribute("aria-label", `Follow ${follow.rel} ${follow.direction === "out" ? "out of" : "into"} ${data.focus.name}, ${follow.count} one hop away, ${FOLLOW_HOPS} hops`);
      button.addEventListener("click", () => go({ node: data.focus.id, rel: follow.rel, dir: follow.direction, hops: FOLLOW_HOPS }));
      item.append(button);
      parts.follows.append(item);
    }
  }

  function drawMemories(memories) {
    parts.memories.replaceChildren();
    parts.said.hidden = !memories.length;
    for (const memory of memories) {
      const item = el("li");
      item.append(el("span", { class: "said-day" }, memory.learned_at || ""), " ", memory.text);
      parts.memories.append(item);
    }
  }

  function drawWalk() {
    parts.walkList.replaceChildren();
    parts.walk.hidden = !walk.length;
    walk.forEach((step, index) => {
      const item = el("li");
      const label = step.rel ? `${step.name} · ${step.rel} ${step.dir === "out" ? "→" : step.dir === "in" ? "←" : "↔"}` : step.name;
      if (index === at) {
        item.append(el("span", { "aria-current": "step" }, label));
      } else {
        const link = el("a", { href: urlFor(step) }, label);
        link.addEventListener("click", (event) => {
          event.preventDefault();
          at = index;
          history.pushState({ walk, at }, "", urlFor(step));
          go(step, "none");
          drawWalk();
        });
        item.append(link);
      }
      parts.walkList.append(item);
    });
  }

  async function drawStarters() {
    const data = await getJson("/api/explore/start");
    const starters = data.starters || [];
    parts.starters.replaceChildren();
    parts.start.hidden = !starters.length;
    for (const node of starters) {
      const item = el("li");
      const button = el("button", { type: "button", class: "chip" }, node.name);
      button.addEventListener("click", () => go({ node: node.id, depth: 1 }));
      item.append(button);
      parts.starters.append(item);
    }
    return starters;
  }

  // --- Wiring ----------------------------------------------------------------------------------

  parts.form.addEventListener("submit", (event) => {
    event.preventDefault();
    const text = parts.input.value.trim();
    if (text) go({ node: text, depth: 1 });
  });

  window.addEventListener("popstate", (event) => {
    const saved = event.state;
    if (saved && Array.isArray(saved.walk)) {
      walk = saved.walk;
      at = saved.at;
    }
    const step = stepFromUrl();
    if (step) go(step, "none");
    drawWalk();
  });

  (async () => {
    const starters = await drawStarters();
    const first = stepFromUrl();
    if (first) {
      go(first, "replace");
    } else if (starters.length) {
      go({ node: starters[0].id, depth: 1 }, "replace");
    } else {
      busy(false);
      parts.name.textContent = "Nothing to explore yet";
      say("The graph has nothing a public channel can read yet.", true);
    }
  })();
})();
