// Company Brain: one answer, drawn. Shared by the public ask page (app.js) and the console's ask
// page, so both show the same text, walk, follow-ups and footer. Plain JS, no build step.
// Every piece of answer text is inserted with textContent; markup is built from constants only.
// The page that renders owns asking: it passes onAsk, and everything here that asks calls it.
(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const { el, SCOPE_LABEL } = window.BrainGraph;

  function chip(question, onClick, extra = "") {
    const item = el("li");
    const node = el("button", { type: "button", class: `chip${extra}` }, question);
    node.addEventListener("click", onClick);
    item.append(node);
    return item;
  }

  // The same blast_radius question at one hop, two hops and every hop, so the deeper run shows
  // what the first one left out. A hop phrase at the end is what the server reads as a depth
  // (algorithms._HOPS): the buttons swap it, and "all" drops it, with any closing "?".
  // NAMES_DEPTH mirrors algorithms.depth_in, so the pressed button is the depth asked for.
  const HOP_PHRASE = /(?:,?\s*\b(?:\d+|one|two|three|four|five|six)\s+hops?(?:\s+out)?)?[\s.?!]*$/i;
  const NAMES_DEPTH = /\b(?:(?:\d+|one|two|three|four|five|six)\s+hops?|depth\s+\d+|directly|direct dependents|immediate(?:ly)?|first hop)\b/i;
  const DEPTHS = [["1 hop", ", one hop", 1], ["2 hops", ", two hops out", 2], ["all", "", null]];

  function addDepthButtons(out, question, depth, onAsk) {
    const head = $(".path-head", out);
    if (!head) return;
    const base = question.replace(HOP_PHRASE, "");
    const asked = NAMES_DEPTH.test(question) ? depth : null;
    const group = el("div", { class: "depths", role: "group", "aria-label": "How many hops to follow" });
    for (const [label, phrase, hops] of DEPTHS) {
      const choice = el("button", { type: "button", "aria-pressed": String(hops === asked) }, label);
      choice.addEventListener("click", () => onAsk(`${base}${phrase}`));
      group.append(choice);
    }
    head.insertBefore(group, $(".replay", head));
  }

  // The answer's own address, the one a shared link opens on. "Copied" shows for a moment.
  // Without the async clipboard (a plain-http host) the older copy command is tried; if that
  // fails too the button says so, and the address bar already holds the same link.
  async function copyOut(text, from) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (error) {
      return copyText(text, from);
    }
  }

  function copyText(text, from) {
    const area = el("textarea", { readonly: "", "aria-hidden": "true" });
    // A property, not a style attribute: a strict CSP blocks the attribute and allows this.
    area.style.cssText = "position:fixed;top:0;left:0;opacity:0";
    area.value = text;
    document.body.append(area);
    area.select();
    let copied = false;
    try { copied = document.execCommand("copy"); } catch (error) { copied = false; }
    area.remove();
    from.focus();
    return copied;
  }

  function addCopyLink(out, link) {
    const head = $(".path-head", out);
    if (!head) return;
    const copy = el("button", { type: "button", class: "copy-link", title: link });
    copy.append(el("span", { class: "label" }, "Copy link"), el("span", { class: "done" }, "Copied"));
    let shown = null;
    copy.addEventListener("click", async () => {
      const copied = await copyOut(link, copy);
      copy.querySelector(".done").textContent = copied ? "Copied" : "Not copied";
      copy.classList.add("copied");
      clearTimeout(shown);
      shown = setTimeout(() => copy.classList.remove("copied"), 1600);
    });
    head.insertBefore(copy, $(".replay", head));
  }

  // --- What to ask next --------------------------------------------------------------------------

  // Up to three follow-ups after an answer, built from its own nodes and path: the tool's next
  // steps first, filled from the roles rolesOf reads, then the first question of each other node in
  // order. A filled step is offered only when it is one of BrainGraph.questionsFor's questions for
  // that node (in its words: a decision keeps its title's case), or names no node, so every chip is
  // a question the integration test has asked on a fresh seed (tests/integration/
  // test_explore_questions.py). {service} is what the answer is about, {owner} the person who OWNS
  // it, {team} the team that RUNS it, {decision} the first decision, {proposer} who PROPOSED it, and
  // {person} the first person.
  const NEXT = {
    who_owns: ["what breaks if {service} goes down", "what if {owner} leaves", "what does {service} depend on"],
    who_knows: ["what breaks if {service} goes down", "what if {owner} leaves", "what does {service} depend on"],
    blast_radius: ["who knows {service}", "which services are load-bearing"],
    decision_trace: ["who knows {service}", "what if {proposer} leaves"],
    if_person_leaves: ["who knows {service}", "what breaks if {service} goes down"],
    path_between: ["what if {person} leaves", "who knows {service}"],
    load_bearing: ["what breaks if {service} goes down", "who knows {service}"],
    onboarding_brief: ["what if {person} leaves", "what breaks if {service} goes down"],
    who_should_know: ["what breaks if {service} goes down", "what if {owner} leaves"],
    decision_pressure: ["why did we {decision}", "who knows {service}"],
    working_groups: ["who is on the {team} team", "what if {person} leaves"],
    reading_list: ["who knows {service}", "what breaks if {service} goes down"],
    risk_map: ["what if {owner} leaves", "what breaks if {service} goes down", "who is on the {team} team"],
    related_memories: ["who knows {service}", "what breaks if {service} goes down"],
    person_profile: ["what if {person} leaves", "what breaks if {service} goes down", "who knows {service}"],
  };
  const MAX_NEXT = 3;
  // The answer text names its subject first where the drawing does not start with it: "If Auth
  // Service goes down, ..." draws its dependents first, and load_bearing draws the edges into the top.
  const SUBJECT_IN_TEXT = new Set(["blast_radius", "load_bearing"]);
  // A decision's answer is about what the decision is ABOUT.
  const ABOUT_A_DECISION = new Set(["decision_trace", "decision_pressure"]);
  const spoken = (question) => question.toLowerCase().replace(/[\s?.!]+$/, "").replace(/\s+/g, " ").trim();

  function rolesOf(data) {
    const nodes = data.nodes || [];
    const edges = data.edges || [];
    const byId = new Map(nodes.map((node) => [node.id, node]));
    const labelOf = (id) => (byId.get(id) || {}).label;
    const services = nodes.filter((node) => node.label === "Service");
    let service = services[0];
    if (ABOUT_A_DECISION.has(data.tool)) {
      const about = edges.find((edge) => edge.rel === "ABOUT" && labelOf(edge.source) === "Decision" && labelOf(edge.target) === "Service");
      if (about) service = byId.get(about.target);
    } else if (SUBJECT_IN_TEXT.has(data.tool)) {
      const text = (data.answer || "").toLowerCase();
      const named = services.map((node) => [text.indexOf(node.name.toLowerCase()), node]).filter(([at]) => at >= 0);
      named.sort((a, b) => a[0] - b[0] || b[1].name.length - a[1].name.length);
      if (named.length) service = named[0][1];
    }
    const personWith = (rel, target) => {
      const edge = edges.find((e) => e.rel === rel && (!target || e.target === target.id) && labelOf(e.source) === "Person");
      return edge ? byId.get(edge.source) : undefined;
    };
    const runs = service && edges.find((edge) => edge.rel === "RUNS" && edge.target === service.id && labelOf(edge.source) === "Team");
    return {
      service,
      owner: service ? personWith("OWNS", service) : undefined,
      team: runs ? byId.get(runs.source) : nodes.find((node) => node.label === "Team"),
      decision: nodes.find((node) => node.label === "Decision"),
      proposer: personWith("PROPOSED"),
      person: nodes.find((node) => node.label === "Person"),
    };
  }

  function nextQuestions(data, asked) {
    const roles = rolesOf(data);
    const offered = [];
    const offer = (q) => {
      if (offered.length < MAX_NEXT && spoken(q) !== spoken(asked) && !offered.some((other) => spoken(other) === spoken(q))) offered.push(q);
    };
    for (const step of NEXT[data.tool] || []) {
      const slot = (step.match(/\{(\w+)\}/) || [])[1];
      if (!slot) { offer(step); continue; }
      const node = roles[slot];
      const wanted = node && spoken(step.replace(`{${slot}}`, () => node.name));
      const question = node && BrainGraph.questionsFor(node).find((q) => spoken(q) === wanted);
      if (question) offer(question);
    }
    // The rest come from the drawing, skipping what the answer is already about: a node the
    // question names, and the subject of a tool whose steps above already ask about it.
    const about = spoken(asked);
    const covered = NEXT[data.tool] ? roles.service : undefined;
    for (const node of data.nodes || []) {
      if (node === covered || about.includes(node.name.toLowerCase())) continue;
      const [first] = BrainGraph.questionsFor(node);
      if (first) offer(first);
    }
    return offered;
  }

  // A refusal offers its nearest names as questions. With no name near, it offers the first
  // examples the page passes (the public page: the example chips that answer on this graph).
  function insteadOf(data, asked, examplesAnswered) {
    const near = (data.candidates || []).map((name) => BrainGraph.questionsFor({ name })[0]);
    const offered = near.length ? near : examplesAnswered.filter((q) => spoken(q) !== spoken(asked));
    return offered.slice(0, near.length ? MAX_NEXT : 2);
  }

  // The examples came after the refusal: add its row in place, if that refusal is still on screen.
  function insteadLater(out, data, asked, opts) {
    const footer = $(".footer-line", out);
    if (!opts.isCurrent(asked) || !footer || $(".next-list", out)) return;
    const next = insteadOf(data, asked, opts.examples());
    if (next.length) out.insertBefore(chipRow("Try instead", next, opts.onAsk), $(".walk", out) || footer);
  }

  // "Ask next" after an answer, "Try instead" after a refusal: a labelled row of question chips.
  // Each chip asks through the page, so the address, the cache, Copy link and the depth buttons follow.
  function chipRow(label, questions, onAsk) {
    const row = el("div", { class: "next" });
    row.append(el("p", { class: "next-label", "aria-hidden": "true" }, label));
    const list = el("ul", { class: "next-list", "aria-label": label });
    for (const q of questions) list.append(chip(q, () => onAsk(q)));
    row.append(list);
    return row;
  }

  // Draws one answer into ``out``. ``opts``:
  //   onAsk(question)   asks a follow-up (chips, depth buttons, the drawing's nodes, the grid)
  //   linkFor(question) the address Copy link copies
  //   examples()        the questions a refusal with no near name offers
  //   examplesReady     a promise that settles once those are known (a refusal can arrive first)
  //   isCurrent(q)      whether q is still the answer on screen
  //   trail()           an optional row to add after "Ask next" (the public page's "Your walk")
  //   scopes            labels for the scopes the footer names, over graph.js's own
  function render(out, data, question, opts) {
    const onAsk = opts.onAsk;
    out.replaceChildren();
    if (data.notice) out.append(el("p", { class: "notice" }, data.notice));
    out.append(el("p", { class: data.found ? "text" : "text refused" }, data.answer));
    // risk_map and person_profile return the same grid shape; one table draws both.
    if (data.grid && ["risk_map", "person_profile"].includes(data.grid.kind) && window.RiskMap) RiskMap.render(out, data.grid, { ask: onAsk });

    BrainGraph.renderPath(out, data.nodes || [], data.edges || [], { tool: data.tool, depth: data.depth, onAsk });
    if (data.tool === "blast_radius" && data.found) addDepthButtons(out, question, data.depth, onAsk);
    addCopyLink(out, opts.linkFor(question));

    if (data.found && data.candidates && data.candidates.length) {
      out.append(el("p", { class: "note" }, `Also matched: ${data.candidates.join(", ")}. Ask again with that name to switch.`));
    }

    const labels = { ...SCOPE_LABEL, ...(opts.scopes || {}) };
    if (data.memories && data.memories.length) {
      out.append(el("h3", {}, "From conversations"));
      const list = el("ul", { class: "memories" });
      for (const memory of data.memories) {
        const where = labels[memory.scope] || memory.scope;
        const learned = memory.learned_at ? `, learned ${memory.learned_at}` : "";
        list.append(el("li", {}, `“${memory.text}” — ${memory.author}, ${where}${learned}`));
      }
      out.append(list);
    }

    // After the edge lines and before the footer, so the answer text, the drawing and its head
    // stay where the recording frames them. A refusal offers its nearest names as questions.
    const next = data.found ? nextQuestions(data, question) : insteadOf(data, question, opts.examples ? opts.examples() : []);
    if (next.length) out.append(chipRow(data.found ? "Ask next" : "Try instead", next, onAsk));
    else if (!data.found && opts.examplesReady) opts.examplesReady.then(() => insteadLater(out, data, question, opts));
    const trail = opts.trail ? opts.trail() : null;
    if (trail) out.append(trail);

    const picker = data.picked_by === "model" ? "the model" : data.picked_by === "caller" ? "you" : "keywords";
    const read = (data.scopes || []).map((scope) => labels[scope] || scope).join(", ");
    const footer = el("p", { class: "footer-line" });
    footer.append("Tool ", el("code", {}, data.tool), ` · picked by ${picker} · read from ${read}`);
    out.append(footer);
  }

  window.BrainAnswer = { render, chip, chipRow, copyOut, nextQuestions, insteadOf };
})();
