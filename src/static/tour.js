// Company Brain: the 60-second tour. A scripted walk through the live page, shown, not described:
// each step really asks its question through the page (window.BrainPage, from app.js), lights up
// what the answer shows, replays the walk, and says in one caption what to notice. Next, Back,
// Pause and Skip; Esc leaves; /?tour=1 starts it (and &step=N at a step). A step whose question the
// live graph does not offer as an example chip is skipped, so the tour never shows a dead end.
// Plain JS, no build step; every piece of text goes in with textContent.
(() => {
  "use strict";

  const page = window.BrainPage;
  if (!page || !window.BrainGraph) return;
  const { el } = window.BrainGraph;
  const $ = (selector) => document.querySelector(selector);
  const reducedMotion = () => window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // Each step: the question it asks (an example chip; null asks nothing), what to light up (the
  // first selector that exists), its caption, and the live fact it reads off what it shows (FACTS).
  // One step per line, for tests/unit/test_tour.py.
  const STEPS = [
    { ask: "who owns billing, and who else has worked on it", spot: ["#out .canvas-frame", "#answer"], title: "Every answer shows its path", text: "The owner first, then everyone tied to Billing Service by a real edge: owns, wrote, reviewed, worked on. The drawing replays the walk in the order it was made.", fact: "owner" },
    { ask: "who knows the payments gateway", spot: ["#answer"], title: "It says what it does not know", text: "Nothing in the graph is called the payments gateway, so it refuses and names the nearest real things instead of guessing. Each one is a question away.", fact: "nearest" },
    { ask: "what are our riskiest services", spot: ["#out .riskmap-frame", "#answer"], title: "One person away from an outage", text: "The risk map ranks everything a team runs by what depends on it and the gaps it has: one person who knows it, no document, an owner on another team.", fact: "riskiest" },
    { ask: "who is Arjun Mehta", spot: ["#out .riskmap-frame", "#answer"], title: "Everything one person holds", text: "A person's profile: their role, what they own and know, what depends on each, and what would rest on no one if they left.", fact: "holds" },
    { ask: "what changed this week", spot: ["#out .text", "#answer"], title: "What changed this week", text: "A dated digest of decisions, pull requests, tickets and what was said, grouped by the thing it touched, newest first. A quiet week widens to a month, and says so.", fact: "changes" },
    { ask: null, spot: ["#proof-table", "#proof"], title: "Take FalkorDB out", text: "The same questions run twice: once on the graph, once with every graph read returning nothing. Without FalkorDB the answers collapse; a refusal stays a refusal.", fact: "proof" },
  ];
  // Time on a step once it is drawn, while playing: six steps in about a minute.
  const STEP_MS = 9000;
  // Asks one tour may make. A step seen again within a minute reads the page's own cache.
  const MAX_ASKS = 8;
  const CACHE_MS = 60 * 1000;
  // Long enough for the page's smooth scroll to land before the walk replays.
  const SETTLE_MS = 450;

  let tour = null;
  let card = null;
  let spot = null;

  // --- What each step shows on this graph -------------------------------------------------------

  // One line read off the live answer, under the caption: the step's claim, shown with this graph's
  // own names and numbers. Nothing is said when the answer does not carry what the line needs.
  const named = (names) => (names.length < 2 ? names.join("") : `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`);
  const firstSentence = (text) => ((text || "").replace(/\*/g, "").match(/^.*?[.!?](?=\s|$)/) || [""])[0];
  const FACTS = {
    owner(data) {
      const byId = new Map((data.nodes || []).map((node) => [node.id, node]));
      const owns = (data.edges || []).find((edge) => edge.rel === "OWNS");
      const edges = (data.edges || []).length;
      if (!owns || !byId.get(owns.source)) return "";
      return `${byId.get(owns.source).name} owns ${byId.get(owns.target).name}; the walk took ${edges} edge${edges === 1 ? "" : "s"}.`;
    },
    nearest(data) {
      const near = (data.candidates || []).slice(0, 3);
      return near.length ? `Nearest real things: ${named(near)}.` : "";
    },
    riskiest(data) {
      const top = data.grid && data.grid.rows && data.grid.rows[0];
      if (!top) return "";
      const people = (top.people || []).length;
      return `${top.name} comes first: ${top.dependents} thing${top.dependents === 1 ? "" : "s"} depend on it and ${people} ${people === 1 ? "person knows" : "people know"} it.`;
    },
    holds(data) {
      const rows = (data.grid && data.grid.rows) || [];
      if (!rows.length) return firstSentence(data.answer);
      const alone = rows.filter((row) => row.flags && row.flags.alone).map((row) => row.name);
      const held = `${rows.length} thing${rows.length === 1 ? "" : "s"} held`;
      return alone.length ? `${held}; ${named(alone)} ${alone.length === 1 ? "rests" : "rest"} on them alone.` : `${held}; none rests on them alone.`;
    },
    // Up to the sentence that counts the changes, so a quiet week that widened to a month says both.
    changes(data) {
      const sentences = (data.answer || "").replace(/\*/g, "").match(/[^.!?]+[.!?](?=\s|$)/g) || [];
      const counted = sentences.findIndex((sentence) => /\b\d+ changes?\b/.test(sentence));
      return counted < 0 ? firstSentence(data.answer) : sentences.slice(0, Math.min(counted, 1) + 1).join("").trim();
    },
    proof: () => ($("#proof-line") ? $("#proof-line").textContent.trim() : ""),
  };

  function factFor(step, data) {
    try {
      return step.fact && (data || step.fact === "proof") ? FACTS[step.fact](data || {}) : "";
    } catch (error) {
      return "";
    }
  }

  // The fact's subject, marked in place: the owner's OWNS edge and its line under the drawing (the
  // drawing's own hover light), the Try instead row, the risk map's first row, the rows that rest on
  // the person alone, and the proof's line. A step's marks go when the step does.
  const MARKS = {
    owner() {
      const edges = [...document.querySelectorAll("#out .canvas .edge")];
      const index = edges.findIndex((edge) => (edge.querySelector("text") || {}).textContent === "OWNS");
      return index < 0 ? [] : [[edges[index], "hot"], [document.querySelectorAll("#out .steps li")[index], "hot"]];
    },
    nearest: () => [[$("#out .next"), "tour-mark"]],
    riskiest: () => [[$("#out .riskmap tbody tr"), "tour-mark"]],
    holds: () => [...document.querySelectorAll("#out .riskmap tbody tr")].filter((row) => row.querySelector("td.flag.on")).map((row) => [row, "tour-mark"]),
    proof: () => [[$("#proof-line"), "tour-mark"]],
  };

  function mark(step) {
    unmark();
    tour.marks = (MARKS[step.fact] ? MARKS[step.fact]() : []).filter(([node]) => node);
    for (const [node, name] of tour.marks) node.classList.add(name);
  }

  function unmark() {
    if (!tour) return;
    for (const [node, name] of tour.marks || []) node.classList.remove(name);
    tour.marks = [];
  }

  // The proof loads once its band comes into view; its line is read when it has arrived.
  async function proofLine(token) {
    for (let waited = 0; waited < 4000 && tour && token === tour.token; waited += 200) {
      if ($("#proof-line") && $("#proof-line").textContent.trim()) return;
      await new Promise((done) => setTimeout(done, 200));
    }
  }

  // --- The card and the spotlight ---------------------------------------------------------------

  function mount() {
    spot = el("div", { class: "tour-spot", "aria-hidden": "true", hidden: "" });
    card = el("div", { class: "tour-card", role: "dialog", "aria-modal": "false", "aria-labelledby": "tour-title", "aria-describedby": "tour-text", tabindex: "-1" });
    const top = el("div", { class: "tour-top" });
    top.append(el("span", { class: "tour-count", id: "tour-count" }), button("tour-skip", "Skip tour", stop));
    const progress = el("div", { class: "tour-progress", "aria-hidden": "true" });
    progress.append(el("span", { class: "tour-fill" }));
    const controls = el("div", { class: "tour-controls" });
    controls.append(button("tour-back", "Back", back), button("tour-play", "Pause", toggle), button("tour-next", "Next", next));
    const dots = el("div", { class: "tour-dots", role: "group", "aria-label": "Tour steps" });
    tour.steps.forEach((step, index) => {
      const dot = el("button", { type: "button", class: "tour-dot", "aria-label": `Step ${index + 1}: ${step.title}` });
      dot.addEventListener("click", () => go(index));
      dots.append(dot);
    });
    controls.prepend(dots);
    // After the last step, the reader's next move.
    const end = el("div", { class: "tour-controls tour-end", hidden: "" });
    end.append(button("tour-again", "Start again", again), button("tour-next tour-own", "Ask your own", () => stop($("#q"))));
    const fact = el("p", { class: "tour-fact", id: "tour-fact", hidden: "" });
    // At phone width the card folds to this one line (step, title, Next, close) over the answer;
    // a tap on the title opens the whole card. Wider screens never show it.
    const line = el("div", { class: "tour-line" });
    const expand = el("button", { type: "button", class: "tour-expand", "aria-expanded": "false", "aria-controls": "tour-text" });
    expand.addEventListener("click", () => fold(!card.classList.contains("open")));
    line.append(expand, button("tour-line-next", "Next", next), button("tour-close", "×", stop));
    line.lastChild.setAttribute("aria-label", "Close the tour");
    card.append(line, top, el("p", { class: "tour-title", id: "tour-title" }), el("p", { class: "tour-text", id: "tour-text", "aria-live": "polite" }), fact, progress, controls, end);
    document.body.append(spot, card);
  }

  function fold(open) {
    card.classList.toggle("open", open);
    card.querySelector(".tour-expand").setAttribute("aria-expanded", String(open));
  }

  function button(name, label, onClick) {
    const node = el("button", { type: "button", class: name }, label);
    node.addEventListener("click", onClick);
    return node;
  }

  // The spotlight follows its target every frame while the tour runs, so it stays on the answer
  // while the page scrolls, the drawing settles or the window resizes. A target the page has
  // replaced (another question asked) loses the spotlight.
  function follow() {
    if (!tour) return;
    const target = tour.target;
    if (target && target.isConnected) {
      const box = target.getBoundingClientRect();
      spot.style.transform = `translate(${Math.round(box.left - 6)}px, ${Math.round(box.top - 6)}px)`;
      spot.style.width = `${Math.round(box.width + 12)}px`;
      spot.style.height = `${Math.round(box.height + 12)}px`;
      spot.hidden = false;
    } else {
      spot.hidden = true;
    }
    tour.frame = requestAnimationFrame(follow);
  }

  // --- Moving through the steps ------------------------------------------------------------------

  // The steps the live graph can show: an asking step only when its question is offered as a chip
  // (the refusal chip always is), the proof only when the page has it.
  async function stepsOffered() {
    const offered = await page.offered();
    return STEPS.filter((step) => (step.ask ? offered.includes(step.ask) : step.spot.some((selector) => $(selector))));
  }

  async function start(from, at = 0) {
    if (tour) return;
    // An Esc while the steps load (a slow graph) means no tour after all.
    let cancelled = false;
    const early = (event) => { if (event.key === "Escape") cancelled = true; };
    document.addEventListener("keydown", early, true);
    const steps = await stepsOffered();
    document.removeEventListener("keydown", early, true);
    if (!steps.length || tour || cancelled) return;
    tour = { steps, at: -1, playing: !reducedMotion(), timer: null, frame: null, target: null, token: 0, asks: 0, askedAt: new Map(), from };
    mount();
    document.addEventListener("keydown", onKey, true);
    for (const [where, type] of HOLDS) where.addEventListener(type, hold, { passive: true });
    card.focus({ preventScroll: true });
    tour.frame = requestAnimationFrame(follow);
    go(Math.min(Math.max(at, 0), steps.length - 1));
  }

  async function go(index) {
    clearTimeout(tour.timer);
    const token = ++tour.token;
    const step = tour.steps[index];
    tour.at = index;
    tour.target = null;
    caption(step);
    card.classList.add("busy");
    const data = step.ask ? await askOnce(step.ask) : null;
    // A later step, or leaving, wins over a slow answer.
    if (!tour || token !== tour.token) return;
    tour.target = step.spot.map((selector) => $(selector)).find(Boolean) || null;
    if (!step.ask && tour.target) tour.target.scrollIntoView({ behavior: reducedMotion() ? "auto" : "smooth", block: "center" });
    if (!step.ask) await proofLine(token);
    if (!tour || token !== tour.token) return;
    showFact(factFor(step, data));
    remember();
    setTimeout(() => { if (tour && token === tour.token) mark(step); }, reducedMotion() ? 0 : SETTLE_MS);
    setTimeout(() => {
      if (!tour || token !== tour.token) return;
      const replay = $("#out .replay");
      if (step.ask && replay && !reducedMotion()) replay.click();
      card.classList.remove("busy");
      schedule();
    }, reducedMotion() ? 0 : SETTLE_MS);
  }

  // The page asks (and draws) the question; a question asked by this tour within the minute is read
  // from the page's cache, and past MAX_ASKS the tour shows what is already there.
  async function askOnce(question) {
    const fresh = Date.now() - (tour.askedAt.get(question) || 0) < CACHE_MS;
    if (!fresh) {
      if (tour.asks >= MAX_ASKS) return null;
      tour.asks += 1;
      tour.askedAt.set(question, Date.now());
    }
    return (await page.ask(question)) || null;
  }

  function showFact(text) {
    const fact = $("#tour-fact");
    fact.replaceChildren();
    fact.hidden = !text;
    if (text) fact.append(el("span", { class: "tour-live" }, "On this graph"), ` ${text}`);
  }

  // The address follows the tour (/?tour=1&step=N), so a reload or a shared link picks it up at the
  // same step; the page's own history entry keeps its question in its state. Leaving puts back the
  // address of the answer on screen.
  function remember() {
    history.replaceState(history.state, "", `?tour=1&step=${tour.at + 1}`);
  }

  function forget() {
    const q = $("#answer-q").textContent;
    history.replaceState(history.state, "", q ? `?q=${encodeURIComponent(q)}` : location.pathname);
  }

  function caption(step) {
    const last = tour.at === tour.steps.length - 1;
    $("#tour-count").textContent = `Tour · ${tour.at + 1} of ${tour.steps.length}`;
    $("#tour-title").textContent = step.title;
    $("#tour-text").textContent = step.text;
    card.querySelector(".tour-back").disabled = tour.at === 0;
    card.querySelector(".tour-next").textContent = last ? "Finish" : "Next";
    card.querySelector(".tour-line-next").textContent = last ? "Finish" : "Next";
    card.querySelector(".tour-expand").textContent = `${tour.at + 1}/${tour.steps.length} · ${step.title}`;
    unmark();
    card.querySelectorAll(".tour-dot").forEach((dot, index) => {
      if (index === tour.at) dot.setAttribute("aria-current", "step");
      else dot.removeAttribute("aria-current");
    });
    showFact("");
    playLabel();
    fill(0);
  }

  // While playing, the bar fills over the step's time and the tour moves on; it stops at the last
  // step and waits for Finish. Under reduced motion it never plays and the bar shows how far along.
  function schedule() {
    clearTimeout(tour.timer);
    const last = tour.at === tour.steps.length - 1;
    if (tour.playing && !last) {
      fill(STEP_MS);
      tour.timer = setTimeout(next, STEP_MS);
    } else {
      fill(-1);
    }
  }

  function fill(ms) {
    const bar = card.querySelector(".tour-fill");
    bar.style.transition = "none";
    bar.style.width = ms < 0 ? `${((tour.at + 1) / tour.steps.length) * 100}%` : "0%";
    if (ms > 0 && !reducedMotion()) {
      void bar.offsetWidth; // start the fill from empty
      bar.style.transition = `width ${ms}ms linear`;
      bar.style.width = "100%";
    }
  }

  function playLabel() {
    const play = card.querySelector(".tour-play");
    play.textContent = tour.playing ? "Pause" : "Play";
    play.setAttribute("aria-pressed", String(!tour.playing));
    play.setAttribute("aria-label", tour.playing ? "Pause the tour" : "Play the tour");
  }

  function next() {
    if (!tour) return;
    if (tour.at + 1 < tour.steps.length) go(tour.at + 1);
    else finish();
  }

  function back() {
    if (tour && !tour.done && tour.at > 0) go(tour.at - 1);
  }

  function toggle() {
    tour.playing = !tour.playing;
    playLabel();
    if (!card.classList.contains("busy")) schedule();
  }

  // Autoplay never moves the page under a reader: a hidden tab, or a reader scrolling the page
  // themselves (a wheel or a touch outside the card), pauses it. The page's own smooth scroll fires
  // neither.
  function hold(event) {
    if (!tour || !tour.playing) return;
    if (event.type === "visibilitychange" ? !document.hidden : card.contains(event.target)) return;
    toggle();
  }
  const HOLDS = [[document, "visibilitychange"], [window, "wheel"], [window, "touchmove"]];

  // Esc leaves from anywhere; the arrow keys step while the card has focus.
  function onKey(event) {
    if (event.key === "Escape") {
      event.preventDefault();
      stop();
    } else if (card.contains(document.activeElement) && (event.key === "ArrowRight" || event.key === "ArrowLeft")) {
      event.preventDefault();
      if (tour.done) return;
      if (event.key === "ArrowRight") next();
      else back();
    }
  }

  // Leaving keeps the answer on screen and gives focus back to where the tour started; finishing
  // hands it to the ask box, for the reader's own question.
  function stop(focusTo) {
    if (!tour) return;
    clearTimeout(tour.timer);
    cancelAnimationFrame(tour.frame);
    document.removeEventListener("keydown", onKey, true);
    for (const [where, type] of HOLDS) where.removeEventListener(type, hold);
    unmark();
    forget();
    const returnTo = focusTo instanceof HTMLElement ? focusTo : tour.from;
    tour = null;
    card.remove();
    spot.remove();
    card = spot = null;
    if (returnTo && returnTo.isConnected) returnTo.focus({ preventScroll: true });
  }

  // The closing card: the tour's last answer stays on screen, and the reader picks what is next.
  function finish() {
    clearTimeout(tour.timer);
    tour.token += 1;
    tour.done = true;
    tour.target = null;
    unmark();
    $("#tour-count").textContent = "Tour · done";
    $("#tour-title").textContent = "That's the tour";
    $("#tour-text").textContent = "Every answer you saw came from the live graph, with the edges it walked. Ask your own question, or start again.";
    showFact("");
    card.querySelectorAll(".tour-dot").forEach((dot) => dot.removeAttribute("aria-current"));
    card.querySelector(".tour-fill").style.width = "100%";
    card.querySelector(".tour-controls:not(.tour-end)").hidden = true;
    card.querySelector(".tour-end").hidden = false;
    card.querySelector(".tour-expand").textContent = "Tour · done";
    card.querySelector(".tour-line-next").hidden = true;
    fold(true);
    card.querySelector(".tour-own").focus({ preventScroll: true });
  }

  function again() {
    tour.done = false;
    card.querySelector(".tour-controls:not(.tour-end)").hidden = false;
    card.querySelector(".tour-end").hidden = true;
    card.querySelector(".tour-line-next").hidden = false;
    card.querySelector(".tour-next").focus({ preventScroll: true });
    go(0);
  }

  // --- Starting -----------------------------------------------------------------------------------

  const startButton = $("#tour-start");
  if (startButton) {
    startButton.hidden = false;
    startButton.addEventListener("click", () => start(startButton));
  }
  const params = new URLSearchParams(location.search);
  if (params.get("tour") === "1") start(startButton, (parseInt(params.get("step"), 10) || 1) - 1);
})();
