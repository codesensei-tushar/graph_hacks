// Company Brain page: ask the graph, draw the path it walked. Plain JS, no build step.
// Every piece of answer text is inserted with textContent; markup is built from constants only.
// First load makes four calls (tools, stats, sample, teams); the proof waits until it is scrolled to.
(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const form = $("#ask-form");
  const input = $("#q");
  const button = $("#ask-btn");
  const answerBox = $("#answer");
  const answerQ = $("#answer-q");
  const statusLine = $("#status");
  const out = $("#out");
  const chips = $("#chips");

  const { el } = window.BrainGraph;
  const VISIBLE_CHIPS = 3;
  const CACHE_MS = 60 * 1000;
  const TOO_MANY = "Too many questions from this address; try again in a minute.";
  const reducedMotion = () => window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;

  // One GET, read as JSON. A 429 becomes the calm sentence the brief asks for.
  async function getJson(url) {
    const response = await fetch(url, { headers: { Accept: "application/json" } });
    if (response.status === 429) throw new Error(TOO_MANY);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    return data;
  }

  // A question chip (answer.js); by default it asks from the page.
  const chip = (question, onClick, extra = "") => BrainAnswer.chip(question, onClick || (() => askFromPage(question)), extra);

  // --- Catalogue: example chips, the graph line, the tool list --------------------------------

  let company = null;
  let catalogue = null;
  // The example chips that answer on this graph; a refusal with no near name offers the first two.
  // ``toolsReady`` settles once they are in, for a refusal that arrives first (a deep link).
  let examplesAnswered = [];
  // Every example chip /api/ask/tools offers, the refusal included, for the tour's honesty check.
  let examplesOffered = [];
  let toolsReady = Promise.resolve();
  let teamIds = null;
  // The stats' label counts once they arrive; {} when that call failed.
  let counted = null;
  // The whole graph's node and edge counts, and the hero sample's, for the line above the sample.
  let totals = null;
  let sampled = null;

  async function loadTools() {
    try {
      const data = await getJson("/api/ask/tools");
      company = data.company || null;
      catalogue = { company, name: data.company_name || null };
      renderChips(data.examples || []);
      renderGraphLine();
      renderTools(data.tools || [], data.writes || []);
      $("#onboard-slack").hidden = !(data.writes || []).includes("onboard_team");
      renderBeat();
    } catch (error) {
      chips.replaceChildren();
      $("#tool-count").textContent = "";
      $("#tool-list").replaceChildren(el("li", { class: "note" }, `The catalogue could not be loaded. ${error.message}`));
    }
  }

  // The last example names something the graph does not hold (ask_api.EXAMPLES says so); it
  // belongs with the refusal card, so every chip in the hero answers with a path.
  function renderChips(examples) {
    const answered = examples.slice(0, -1);
    examplesAnswered = answered;
    examplesOffered = examples.slice();
    const refusal = examples[examples.length - 1];
    chips.replaceChildren();
    const hidden = [];
    answered.forEach((question, index) => {
      const item = chip(question);
      if (index >= VISIBLE_CHIPS) { item.hidden = true; hidden.push(item); }
      chips.append(item);
    });
    if (hidden.length) {
      const more = chip(`${hidden.length} more`, () => {
        for (const item of hidden) item.hidden = false;
        more.remove();
        hidden[0].querySelector("button").focus();
      }, " more");
      more.querySelector("button").setAttribute("aria-label", `Show ${hidden.length} more example questions`);
      chips.append(more);
    }
    if (refusal) {
      const ask = $("#refusal-ask");
      ask.textContent = refusal;
      ask.addEventListener("click", () => askFromPage(refusal));
      $("#refusal-try").hidden = false;
    }
  }

  // The graph line names only what the live graph holds, in the brief's order: a plain seed
  // has no pull requests until GitHub adds them. Chat is a seeded Chat thread or a memory.
  const DEMO_KINDS = [[["Person"], "people"], [["Service"], "services"], [["Decision"], "decisions"], [["Ticket"], "tickets"], [["PullRequest"], "pull requests"], [["Memory", "Chat"], "chat"]];
  const OWN_KINDS = [[["Service"], "services"], [["Decision"], "decisions"], [["PullRequest"], "pull requests"], [["Memory", "Chat"], "public chat"]];
  const listed = (words) => (words.length < 2 ? words.join("") : `${words.slice(0, -1).join(", ")} and ${words[words.length - 1]}`);

  function renderGraphLine() {
    if (!catalogue || counted === null) return;
    const own = catalogue.company === "company-brain";
    const words = (own ? OWN_KINDS : DEMO_KINDS).filter(([labels]) => labels.some((label) => counted[label] > 0)).map(([, word]) => word);
    const what = words.length ? `: its ${own ? "own " : ""}${listed(words)}` : "";
    const line = $("#graph-line");
    if (own) line.textContent = `This page reads the live graph behind this project${what}.`;
    else if (catalogue.name) line.textContent = `This page reads the live graph of ${catalogue.name}, a synthetic company built for the demo${what}.`;
  }

  // The catalogue for builders: one card per read-only tool, read live from /api/ask/tools, with
  // its first sentence and its arguments. Each card has an id, so a proof row can link to it.
  // The architecture diagram takes its counts from the same call.
  function renderTools(tools, writes) {
    const reads = plural(tools.length, "read-only tool", "read-only tools");
    $("#tool-count").replaceChildren(el("b", {}, String(tools.length)), reads.slice(String(tools.length).length));
    const archReads = $("#arch-reads");
    if (archReads) archReads.textContent = reads;
    const archWrites = $("#arch-writes");
    if (archWrites && writes.length) archWrites.textContent = `${plural(writes.length, "write tool", "write tools")}`;
    const list = $("#tool-list");
    list.replaceChildren();
    for (const tool of tools) {
      const first = tool.description.split(/(?<=\.)\s/)[0];
      const card = el("li", { class: "tool-card", id: `tool-${tool.name}`, title: tool.description });
      card.dataset.search = `${tool.name} ${tool.name.replace(/_/g, " ")} ${tool.description} ${(tool.args || []).join(" ")}`.toLowerCase();
      card.append(el("code", { class: "tool-name" }, tool.name), el("p", { class: "tool-does" }, first));
      const args = el("p", { class: "tool-args" });
      if ((tool.args || []).length) {
        args.append(el("span", { class: "label" }, "args"));
        for (const arg of tool.args) args.append(el("code", {}, arg));
      } else {
        args.append(el("span", { class: "label" }, "no arguments"));
      }
      card.append(args);
      list.append(card);
    }
    const note = $("#tool-writes");
    if (writes.length) {
      note.replaceChildren(`${plural(writes.length, "more tool writes", "more tools write")} the graph: `);
      writes.forEach((name, index) => note.append(index ? ", " : "", el("code", {}, name)));
      note.append(". They run in Slack only, after someone approves a card that names the target, and are never served over MCP.");
      note.hidden = false;
    }
    watchFilter(tools.length);
  }

  function watchFilter(total) {
    const input = $("#tool-filter");
    const count = $("#tool-count");
    const full = count.cloneNode(true);
    input.disabled = false;
    input.addEventListener("input", () => {
      const words = input.value.toLowerCase().split(/\s+/).filter(Boolean);
      let shown = 0;
      for (const card of $("#tool-list").children) {
        const match = words.every((word) => (card.dataset.search || "").includes(word));
        card.hidden = !match;
        if (match) shown += 1;
      }
      $("#tool-empty").hidden = shown > 0;
      if (!words.length) count.replaceChildren(...full.cloneNode(true).childNodes);
      else count.replaceChildren(el("b", {}, String(shown)), ` of ${plural(total, "read-only tool", "read-only tools")}`);
    });
  }

  // How it works: the diagram shows one way in at a time when a button asks it to.
  function watchRoutes() {
    const figure = $("#arch");
    const group = $("#arch-routes");
    if (!figure || !group) return;
    group.hidden = false;
    for (const button of group.querySelectorAll("button")) {
      button.addEventListener("click", () => {
        figure.dataset.route = button.dataset.route;
        for (const other of group.querySelectorAll("button")) other.setAttribute("aria-pressed", String(other === button));
      });
    }
  }

  // For builders: the snippets name this server, tabs pick one, and each has a copy button.
  function watchQuickstart() {
    const box = $("#quickstart");
    if (!box) return;
    for (const span of box.querySelectorAll(".origin")) span.textContent = location.origin;
    const tabs = [...box.querySelectorAll('[role="tab"]')];
    const select = (tab, focus) => {
      for (const other of tabs) {
        const on = other === tab;
        other.setAttribute("aria-selected", String(on));
        other.tabIndex = on ? 0 : -1;
        document.getElementById(other.getAttribute("aria-controls")).hidden = !on;
      }
      if (focus) tab.focus();
    };
    tabs.forEach((tab, index) => {
      tab.addEventListener("click", () => select(tab, false));
      tab.addEventListener("keydown", (event) => {
        const step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
        if (event.key === "Home") { select(tabs[0], true); event.preventDefault(); }
        else if (event.key === "End") { select(tabs[tabs.length - 1], true); event.preventDefault(); }
        else if (step) { select(tabs[(index + step + tabs.length) % tabs.length], true); event.preventDefault(); }
      });
    });
    box.querySelector('[role="tablist"]').hidden = false;
    select(tabs[0], false);
    for (const button of box.querySelectorAll(".copy")) {
      let shown = null;
      button.addEventListener("click", async () => {
        const copied = await BrainAnswer.copyOut(button.parentElement.querySelector("code").textContent, button);
        button.textContent = copied ? "Copied" : "Not copied";
        button.classList.toggle("copied", copied);
        clearTimeout(shown);
        shown = setTimeout(() => { button.textContent = "Copy"; button.classList.remove("copied"); }, 1600);
      });
    }
  }

  // The bus-factor chips ask live only where the answer exists: Arjun on the demo company,
  // Priya once its Sales team is in the graph.
  function renderBeat() {
    if (company !== "quillfern" || teamIds === null) return;
    const list = $("#beat-chips");
    list.replaceChildren(chip("what if arjun mehta leaves"));
    if (teamIds.includes("sales")) list.append(chip("what if priya shah leaves"));
    list.hidden = false;
  }

  // --- Live counts ------------------------------------------------------------------------------

  async function loadStats() {
    const row = $("#stats");
    try {
      const data = await getJson("/api/graph/stats");
      const labels = data.labels || {};
      counted = labels;
      totals = { nodes: data.nodes, edges: data.edges };
      renderGraphLine();
      renderSampleCount();
      for (const cell of row.querySelectorAll("dd[data-stat]")) {
        const key = cell.dataset.stat;
        const value = key === "edges" ? data.edges : labels[key] || 0;
        cell.textContent = String(value);
        const term = cell.parentElement.querySelector("dt");
        if (value === 1 && term.dataset.one) term.textContent = term.dataset.one;
        cell.parentElement.classList.remove("sk");
      }
      if (data.company_name) $("#live-name").textContent = `${data.company_name} · live sample of the graph`;
    } catch (error) {
      counted = {};
      renderGraphLine();
      row.dataset.state = "failed";
      row.setAttribute("aria-hidden", "true");
    }
  }

  // --- The hero's live slice ------------------------------------------------------------------

  // "26 of 28 nodes · 58 of 62 edges": the sample reads as part of the graph the stats below
  // count, not as a second count that disagrees with them.
  function renderSampleCount() {
    if (!sampled) return;
    const part = (shown, total, one, many) => (total > shown ? `${shown} of ${plural(total, one, many)}` : plural(shown, one, many));
    const count = $("#live-count");
    count.replaceChildren(part(sampled.nodes, totals && totals.nodes, "node", "nodes"));
    count.append(el("span", { class: "count-edges" }, ` · ${part(sampled.edges, totals && totals.edges, "edge", "edges")}`));
  }

  async function loadSample() {
    const stage = $("#live-stage");
    const wait = $("#live-wait");
    try {
      const data = await getJson("/api/graph/sample");
      const mounted = window.BrainHero.mount(stage, data, { ticker: $("#live-ticker"), onAsk: askFromPage });
      if (!mounted) {
        wait.textContent = "Nothing public is in the graph yet, so there is nothing to draw.";
        return;
      }
      sampled = { nodes: mounted.nodes, edges: mounted.edges };
      renderSampleCount();
      window.BrainHero.legend($("#live-legend"), data.nodes);
    } catch (error) {
      wait.textContent = `The graph could not be drawn. ${error.message}`;
    }
  }

  // --- Asking ------------------------------------------------------------------------------------

  // The server caches answers for a minute; the page keeps them as long, so asking the same
  // question again costs no request.
  const answered = new Map();

  // A shared link asks while the hero is still loading: the chips, the graph line, the live graph
  // and the stats can all grow above the answer after it has been scrolled to. Until the reader
  // scrolls, clicks or presses a key, the answer is scrolled back under the header whenever it moves.
  const READER_MOVES = ["wheel", "touchstart", "pointerdown", "keydown"];
  let pinned = null;

  function unpin() {
    if (!pinned) return;
    if (pinned.watcher) pinned.watcher.disconnect();
    for (const type of READER_MOVES) window.removeEventListener(type, unpin, true);
    pinned = null;
  }

  function pinAnswer() {
    unpin();
    const top = () => answerBox.getBoundingClientRect().top + window.scrollY;
    const scroll = () => answerBox.scrollIntoView({ behavior: reducedMotion() ? "auto" : "smooth", block: "start" });
    let at = top();
    scroll();
    const follow = () => {
      const now = top();
      if (Math.abs(now - at) < 1) return;
      at = now;
      scroll();
    };
    const watcher = "ResizeObserver" in window ? new ResizeObserver(follow) : null;
    if (watcher) watcher.observe(document.body);
    for (const type of READER_MOVES) window.addEventListener(type, unpin, { capture: true, passive: true });
    pinned = { follow, watcher };
  }

  function askFromPage(question) {
    input.value = question;
    ask(question, true);
  }

  // --- The walk ----------------------------------------------------------------------------------

  // Every question asked from the page is a step in the browser's history, so Back and Forward move
  // between answers and a shared link still opens on its one question. ``walk`` mirrors that history
  // for the "Your walk" row: each step's question and the answer it showed, and which step is on
  // screen. A deep link starts the walk with replaceState, never a second entry; asking after
  // stepping back drops the steps ahead, as the browser does. The walk lives in memory only, so a
  // reload starts a new one. Each history entry's state carries its question and its step.
  const walk = { steps: [], at: -1, scroll: false };
  const SHOWN_STEPS = 5;
  const emptyAnswer = Array.from(out.childNodes, (node) => node.cloneNode(true));

  function stepTo(q, how, step) {
    const address = `?q=${encodeURIComponent(q)}`;
    if (how === "back") {
      // Only after a reload: the entry outlived the walk, which starts again here.
      walk.steps = [{ q }];
      walk.at = 0;
    } else if (how === "start") {
      walk.steps = [{ q }];
      walk.at = 0;
      history.replaceState({ q, step: 0 }, "", address);
    } else if (walk.at >= 0 && walk.steps[walk.at].q === q) {
      history.replaceState({ q, step: walk.at }, "", address);
    } else {
      walk.steps = walk.steps.slice(0, walk.at + 1).concat({ q });
      walk.at = walk.steps.length - 1;
      history.pushState({ q, step: walk.at }, "", address);
    }
    return walk.steps[walk.at];
  }

  // Back to the page as it loaded, before anything was asked.
  function showEmpty() {
    walk.at = -1;
    input.value = "";
    answerQ.textContent = "";
    statusLine.textContent = "";
    statusLine.classList.remove("error");
    out.replaceChildren(...emptyAnswer.map((node) => node.cloneNode(true)));
  }

  // Back and Forward show the answer the step showed, from memory: no new ask and no pinning. A
  // step from the "Your walk" row also brings the answer into view.
  window.addEventListener("popstate", (event) => {
    const state = event.state || {};
    const q = state.q || new URLSearchParams(location.search).get("q");
    const scroll = walk.scroll;
    walk.scroll = false;
    if (!q) { showEmpty(); return; }
    input.value = q;
    const entry = walk.steps[state.step];
    if (!entry || entry.q !== q || !entry.data) { ask(q, false, "back", state.step); return; }
    unpin();
    walk.at = state.step;
    answerQ.textContent = q;
    statusLine.textContent = "";
    statusLine.classList.remove("error");
    render(entry.data, q);
    if (scroll) answerBox.scrollIntoView({ behavior: reducedMotion() ? "auto" : "smooth", block: "start" });
  });

  // The steps before the one on screen, most recent last, each a button that goes back to it
  // through the browser's history; the one on screen closes the row, as plain text.
  function walkRow() {
    if (walk.at < 1) return null;
    const first = Math.max(0, walk.at - SHOWN_STEPS + 1);
    const row = el("nav", { class: "next walk", "aria-label": "Your walk" });
    row.append(el("p", { class: "next-label", "aria-hidden": "true" }, "Your walk"));
    const list = el("ol", { class: "walk-steps" });
    walk.steps.slice(first, walk.at + 1).forEach(({ q }, index) => {
      const step = first + index;
      const item = el("li");
      if (step === walk.at) {
        item.setAttribute("aria-current", "step");
        item.append(el("span", { class: "walk-here", title: q }, q));
      } else {
        const back = el("button", { type: "button", class: "walk-step", title: q }, q);
        back.addEventListener("click", () => { walk.scroll = true; history.go(step - walk.at); });
        item.append(back);
      }
      list.append(item);
    });
    row.append(list);
    return row;
  }

  async function ask(question, scroll, how = "ask", step = 0) {
    const q = question.trim();
    if (!q) return;
    button.disabled = true;
    answerQ.textContent = q;
    statusLine.textContent = "Walking the graph…";
    statusLine.classList.remove("error");
    const entry = stepTo(q, how, step);
    if (scroll) pinAnswer();
    try {
      const hit = answered.get(q);
      const fresh = hit && Date.now() - hit.at < CACHE_MS;
      const data = fresh ? hit.data : await getJson(`/api/ask?q=${encodeURIComponent(q)}`);
      if (!fresh) answered.set(q, { at: Date.now(), data });
      entry.data = data;
      render(data, q);
      return data;
    } catch (error) {
      out.replaceChildren();
      statusLine.textContent = error.message === TOO_MANY ? TOO_MANY : `No answer: ${error.message}`;
      statusLine.classList.add("error");
    } finally {
      button.disabled = false;
      if (scroll && pinned) pinned.follow();
    }
  }

  // The answer itself is drawn by answer.js, shared with the console's ask page. The public page
  // copies its own address (/?q=...), offers the example chips after a refusal with no near name,
  // and adds "Your walk" after "Ask next".
  function render(data, question) {
    statusLine.textContent = "";
    BrainAnswer.render(out, data, question, {
      onAsk: askFromPage,
      linkFor: (q) => new URL(`/?q=${encodeURIComponent(q)}`, location.origin).href,
      examples: () => examplesAnswered,
      examplesReady: toolsReady,
      isCurrent: (q) => answerQ.textContent === q,
      trail: walkRow,
    });
  }

  // --- Teams ---------------------------------------------------------------------------------------

  async function loadTeams() {
    const list = $("#team-cards");
    try {
      const data = await getJson("/api/teams");
      const teams = data.teams || [];
      teamIds = teams.map((team) => team.id);
      list.replaceChildren();
      for (const team of teams) {
        const link = el("a", { href: `/team/${encodeURIComponent(team.id)}` });
        link.append(el("b", {}, team.name));
        link.append(el("span", { class: "counts" }, `${plural(team.members, "person", "people")} · ${plural(team.owns, "thing", "things")}`));
        const kinds = el("span", { class: "kinds" });
        for (const kind of team.kinds) kinds.append(el("span", { class: "kind-chip" }, kind));
        link.append(kinds);
        const item = el("li");
        item.append(link);
        list.append(item);
      }
      if (!teams.length) list.append(el("li", { class: "note" }, "No team is in the graph yet."));
      renderBeat();
    } catch (error) {
      list.replaceChildren(el("li", { class: "note" }, `The teams could not be loaded. ${error.message}`));
    }
  }

  // --- Proof: the same questions with the graph switched off --------------------------------------

  async function loadProof() {
    const status = $("#proof-status");
    const body = $("#proof-body");
    status.textContent = "Running the questions twice…";
    try {
      const data = await getJson("/api/ask/proof");
      $("#proof-line").replaceChildren(el("b", {}, `${data.collapsed} of ${data.total}`), " answers disappear without the graph.");
      $("#proof-refusal").textContent = refusalSentence(data.rows || []);
      status.hidden = true;
      body.replaceChildren();
      const outcome = (side) => (side.found ? plural(side.steps, "edge", "edges") : "refused");
      for (const row of data.rows || []) {
        const line = el("tr", { class: row.collapsed ? "" : "kept" });
        const question = el("td", { class: "q", "data-label": "Question" });
        const askIt = el("button", { type: "button", class: "proof-ask", title: `Ask: ${row.question}` }, row.question);
        askIt.addEventListener("click", () => askFromPage(row.question));
        question.append(askIt);
        line.append(question);
        const toolCell = el("td", { class: "name", "data-label": "Tool" });
        const toolLink = el("a", { href: `#tool-${row.tool}`, title: `${row.tool} in the catalogue` });
        toolLink.append(el("code", {}, row.tool));
        toolCell.append(toolLink);
        line.append(toolCell);
        line.append(el("td", { "data-label": "With the graph" }, outcome(row.with_graph)));
        line.append(el("td", { "data-label": "Without" }, outcome(row.without_graph)));
        line.append(el("td", { class: "verdict", "data-label": "Result" }, row.verdict));
        body.append(line);
      }
    } catch (error) {
      status.textContent = `The proof could not be loaded. ${error.message}`;
      $("#proof-line").replaceChildren();
      body.replaceChildren();
    }
  }

  // Said only when the report bears it out: every row that did not collapse refused both ways.
  function refusalSentence(rows) {
    const kept = rows.filter((row) => !row.collapsed);
    if (!kept.length) return "Every one of them collapses.";
    if (kept.length === rows.length && kept.every((row) => !row.with_graph.found)) return "Nothing collapses because nothing answers yet: this graph holds none of what the questions name.";
    if (kept.every((row) => row.verdict === "refused both ways")) {
      const lead = kept.length === 1 ? "The one that does not is a refusal." : `The ${kept.length} that do not are refusals.`;
      return `${lead} A question about something the graph does not know is refused either way, which is the point.`;
    }
    return `${plural(kept.length, "question answers", "questions answer")} the same without the graph: ${kept.map((row) => row.question).join("; ")}.`;
  }

  // While the hero's dark top is under the header, the header goes transparent with light ink;
  // once it leaves, the header is solid again. A deep link pins the answer, so it lands solid.
  function watchHero() {
    const sky = $(".hero-sky");
    if (!sky || !("IntersectionObserver" in window)) return;
    const root = document.documentElement;
    const header = () => parseFloat(getComputedStyle(root).getPropertyValue("--header")) || 64;
    let observer = null;
    const watch = () => {
      if (observer) observer.disconnect();
      observer = new IntersectionObserver((entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) root.dataset.overHero = "";
          else delete root.dataset.overHero;
        }
      }, { rootMargin: `-${header()}px 0px 0px 0px` });
      observer.observe(sky);
    };
    watch();
    window.matchMedia("(max-width: 760px)").addEventListener("change", watch);
  }

  function watchProof() {
    if (!("IntersectionObserver" in window)) { loadProof(); return; }
    const observer = new IntersectionObserver((entries) => {
      if (!entries.some((entry) => entry.isIntersecting)) return;
      observer.disconnect();
      loadProof();
    }, { rootMargin: "120px 0px" });
    observer.observe($("#proof"));
  }

  // --- Wiring ------------------------------------------------------------------------------------

  form.addEventListener("submit", (event) => { event.preventDefault(); ask(input.value, true); });
  toolsReady = loadTools();
  loadStats();
  loadSample();
  loadTeams();
  watchProof();
  watchHero();
  watchRoutes();
  watchQuickstart();
  const initial = new URLSearchParams(location.search).get("q");
  if (initial) { input.value = initial; ask(initial, true, "start"); }

  // The guided tour (tour.js) asks through the page, so each step's answer, walk, cache and address
  // behave as a click's: ``ask`` settles with the answer once it is drawn (nothing when it failed),
  // ``offered`` once the chips are in.
  window.BrainPage = {
    ask: (question) => { input.value = question; return ask(question, true, "start"); },
    offered: () => toolsReady.then(() => examplesOffered.slice()),
  };
})();
