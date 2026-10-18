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

  async function ask(question, scroll) {
    const q = question.trim();
    if (!q) return;
    button.disabled = true;
    answerQ.textContent = q;
    statusLine.textContent = "Walking the graph…";
    statusLine.classList.remove("error");
    history.replaceState(null, "", `?q=${encodeURIComponent(q)}`);
    if (scroll) pinAnswer();
    try {
      const hit = answered.get(q);
      const fresh = hit && Date.now() - hit.at < CACHE_MS;
      const data = fresh ? hit.data : await getJson(`/api/ask?q=${encodeURIComponent(q)}`);
      if (!fresh) answered.set(q, { at: Date.now(), data });
      render(data, q);
    } catch (error) {
      out.replaceChildren();
      statusLine.textContent = error.message === TOO_MANY ? TOO_MANY : `No answer: ${error.message}`;
      statusLine.classList.add("error");
    } finally {
      button.disabled = false;
      if (scroll && pinned) pinned.follow();
    }
  }

  // The same blast_radius question at one hop, two hops and every hop, so the deeper run shows
  // what the first one left out. A hop phrase at the end is what the server reads as a depth
  // (algorithms._HOPS): the buttons swap it, and "all" drops it, with any closing "?".
  // NAMES_DEPTH mirrors algorithms.depth_in, so the pressed button is the depth asked for.
  const HOP_PHRASE = /(?:,?\s*\b(?:\d+|one|two|three|four|five|six)\s+hops?(?:\s+out)?)?[\s.?!]*$/i;
  const NAMES_DEPTH = /\b(?:(?:\d+|one|two|three|four|five|six)\s+hops?|depth\s+\d+|directly|direct dependents|immediate(?:ly)?|first hop)\b/i;
  const DEPTHS = [["1 hop", ", one hop", 1], ["2 hops", ", two hops out", 2], ["all", "", null]];

  function addDepthButtons(question, depth) {
    const head = $(".path-head", out);
    if (!head) return;
    const base = question.replace(HOP_PHRASE, "");
    const asked = NAMES_DEPTH.test(question) ? depth : null;
    const group = el("div", { class: "depths", role: "group", "aria-label": "How many hops to follow" });
    for (const [label, phrase, hops] of DEPTHS) {
      const choice = el("button", { type: "button", "aria-pressed": String(hops === asked) }, label);
      choice.addEventListener("click", () => askFromPage(`${base}${phrase}`));
      group.append(choice);
    }
    head.insertBefore(group, $(".replay", head));
  }

  // The answer's own address, the one a shared link opens on (/?q=...). "Copied" shows for a
  // moment. Without the async clipboard (a plain-http host) the older copy command is tried;
  // if that fails too the button says so, and the address bar already holds the same link.
  function copyText(text, from) {
    const area = el("textarea", { readonly: "", "aria-hidden": "true", style: "position:fixed;top:0;left:0;opacity:0" });
    area.value = text;
    document.body.append(area);
    area.select();
    let copied = false;
    try { copied = document.execCommand("copy"); } catch (error) { copied = false; }
    area.remove();
    from.focus();
    return copied;
  }

  function addCopyLink(question) {
    const head = $(".path-head", out);
    if (!head) return;
    const link = new URL(`/?q=${encodeURIComponent(question)}`, location.origin).href;
    const copy = el("button", { type: "button", class: "copy-link", title: link });
    copy.append(el("span", { class: "label" }, "Copy link"), el("span", { class: "done" }, "Copied"));
    let shown = null;
    copy.addEventListener("click", async () => {
      let copied = false;
      try {
        await navigator.clipboard.writeText(link);
        copied = true;
      } catch (error) {
        copied = copyText(link, copy);
      }
      copy.querySelector(".done").textContent = copied ? "Copied" : "Not copied";
      copy.classList.add("copied");
      clearTimeout(shown);
      shown = setTimeout(() => copy.classList.remove("copied"), 1600);
    });
    head.insertBefore(copy, $(".replay", head));
  }

  function render(data, question) {
    statusLine.textContent = "";
    out.replaceChildren();
    if (data.notice) out.append(el("p", { class: "notice" }, data.notice));
    out.append(el("p", { class: data.found ? "text" : "text refused" }, data.answer));
    // risk_map and person_profile return the same grid shape; one table draws both.
    if (data.grid && ["risk_map", "person_profile"].includes(data.grid.kind) && window.RiskMap) RiskMap.render(out, data.grid, { ask: askFromPage });

    BrainGraph.renderPath(out, data.nodes || [], data.edges || [], { tool: data.tool, depth: data.depth });
    if (data.tool === "blast_radius" && data.found) addDepthButtons(question, data.depth);
    addCopyLink(question);

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
  loadTools();
  loadStats();
  loadSample();
  loadTeams();
  watchProof();
  watchHero();
  const initial = new URLSearchParams(location.search).get("q");
  if (initial) { input.value = initial; ask(initial, true); }
})();
