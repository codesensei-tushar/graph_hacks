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

  const { el, SCOPE_LABEL } = window.BrainGraph;

  // --- Catalogue and example questions ---------------------------------------------------

  async function loadTools() {
    try {
      const response = await fetch("/api/ask/tools");
      const data = await response.json();
      renderChips(data.examples || []);
      document.getElementById("onboard").hidden = !(data.writes || []).includes("onboard_team");
      renderDemo(data.demo || []);
      renderTools(data.tools || []);
      if (data.company) companyLine.textContent = `Seeded company: ${data.company}.`;
    } catch (error) {
      const row = el("tr");
      row.append(el("td", { colspan: "3", class: "note" }, "The catalogue could not be loaded."));
      toolsBody.replaceChildren(row);
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
    history.replaceState(null, "", `?q=${encodeURIComponent(q)}`);
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
    if (data.notice) out.append(el("p", { class: "notice" }, data.notice));
    out.append(el("p", { class: "text" }, data.answer));

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
    footer.append("Tool ", el("code", {}, data.tool), `, picked by ${picker}. Read from: ${read}`);
    out.append(footer);
  }


  // --- Teams --------------------------------------------------------------------------------

  async function loadTeams() {
    const list = document.getElementById("team-cards");
    try {
      const response = await fetch("/api/teams");
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      list.replaceChildren();
      for (const team of data.teams || []) {
        const link = el("a", { href: `/team/${encodeURIComponent(team.id)}` });
        link.append(el("b", {}, team.name));
        const runs = `${team.owns} ${team.owns === 1 ? "thing" : "things"} it runs`;
        const kinds = team.kinds.length ? ` (${team.kinds.join(", ")})` : "";
        link.append(el("span", {}, `${team.members} ${team.members === 1 ? "person" : "people"} · ${runs}${kinds}`));
        const item = el("li");
        item.append(link);
        list.append(item);
      }
      if (!list.children.length) list.append(el("li", { class: "note" }, "No team is in the graph yet."));
    } catch (error) {
      list.replaceChildren(el("li", { class: "note" }, "The teams could not be loaded."));
    }
  }

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
  loadTeams();
  loadProof();
  const initial = new URLSearchParams(location.search).get("q");
  if (initial) { input.value = initial; ask(initial); }
})();
