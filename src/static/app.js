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

  const { el, SCOPE_LABEL } = window.BrainGraph;
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

  function chip(question, onClick, extra = "") {
    const item = el("li");
    const node = el("button", { type: "button", class: `chip${extra}` }, question);
    node.addEventListener("click", onClick || (() => askFromPage(question)));
    item.append(node);
    return item;
  }

  // --- Catalogue: example chips, the graph line, the tool list --------------------------------

  let company = null;
  let catalogue = null;
  let teamIds = null;
  // The stats' label counts once they arrive; {} when that call failed.
  let counted = null;

  async function loadTools() {
    try {
      const data = await getJson("/api/ask/tools");
      company = data.company || null;
      catalogue = { company, name: data.company_name || null };
      renderChips(data.examples || []);
      renderGraphLine();
      renderTools(data.tools || []);
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

  function renderTools(tools) {
    const count = $("#tool-count");
    count.replaceChildren(el("b", {}, String(tools.length)), ` read-only tool${tools.length === 1 ? "" : "s"}`);
    const list = $("#tool-list");
    list.replaceChildren();
    for (const tool of tools) {
      const first = tool.description.split(/(?<=\.)\s/)[0];
      const item = el("li", { title: tool.description });
      item.append(el("code", {}, tool.name), el("span", {}, first));
      list.append(item);
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
      renderGraphLine();
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
      $("#live-count").textContent = `${plural(mounted.nodes, "node", "nodes")} · ${plural(mounted.edges, "edge", "edges")}`;
      window.BrainHero.legend($("#live-legend"), data.nodes);
    } catch (error) {
      wait.textContent = `The graph could not be drawn. ${error.message}`;
    }
  }

  // --- Asking ------------------------------------------------------------------------------------

  // The server caches answers for a minute; the page keeps them as long, so asking the same
  // question again costs no request.
  const answered = new Map();

  function askFromPage(question) {
    input.value = question;
    ask(question, true);
  }

  async function ask(question, scroll) {
    const q = question.trim();
    if (!q) return;
    button.disabled = true;
    answerQ.textContent = q;
    statusLine.textContent = "Walking the graph…";
    statusLine.classList.remove("error");
    history.replaceState(null, "", `?q=${encodeURIComponent(q)}`);
    if (scroll) answerBox.scrollIntoView({ behavior: reducedMotion() ? "auto" : "smooth", block: "start" });
    try {
      const hit = answered.get(q);
      const fresh = hit && Date.now() - hit.at < CACHE_MS;
      const data = fresh ? hit.data : await getJson(`/api/ask?q=${encodeURIComponent(q)}`);
      if (!fresh) answered.set(q, { at: Date.now(), data });
      render(data);
    } catch (error) {
      out.replaceChildren();
      statusLine.textContent = error.message === TOO_MANY ? TOO_MANY : `No answer: ${error.message}`;
      statusLine.classList.add("error");
    } finally {
      button.disabled = false;
    }
  }

  function render(data) {
    statusLine.textContent = "";
    out.replaceChildren();
    if (data.notice) out.append(el("p", { class: "notice" }, data.notice));
    out.append(el("p", { class: data.found ? "text" : "text refused" }, data.answer));

    BrainGraph.renderPath(out, data.nodes || [], data.edges || [], { tool: data.tool, depth: data.depth });

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
    footer.append("Tool ", el("code", {}, data.tool), ` · picked by ${picker} · read from ${read}`);
    out.append(footer);
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
      $("#proof-line").replaceChildren(el("b", {}, `${data.collapsed} of ${data.total}`), " capabilities collapse without the graph.");
      $("#proof-refusal").textContent = refusalSentence(data.rows || []);
      status.hidden = true;
      body.replaceChildren();
      const outcome = (side) => (side.found ? plural(side.steps, "edge", "edges") : "refused");
      for (const row of data.rows || []) {
        const line = el("tr", { class: row.collapsed ? "" : "kept" });
        line.append(el("td", { class: "q", "data-label": "Question" }, row.question));
        const toolCell = el("td", { class: "name", "data-label": "Tool" });
        toolCell.append(el("code", {}, row.tool));
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
  loadTools();
  loadStats();
  loadSample();
  loadTeams();
  watchProof();
  const initial = new URLSearchParams(location.search).get("q");
  if (initial) { input.value = initial; ask(initial, true); }
})();
