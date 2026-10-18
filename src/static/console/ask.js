// /console/ask: the public ask page's answer (answer.js), asked with the signed-in person's own
// scopes through POST /console/api/ask. Keyword routing for every member; the model picks the tool
// for owners and admins, within the company's daily cap. ?q= asks on load, and each question asked
// here becomes the page's address, so Copy link and a reload land on the same answer.
(() => {
  "use strict";

  const C = window.Console;
  const { $ } = C;
  const form = $("#ask-form");
  const input = $("#q");
  const button = $("#ask-btn");
  const answerQ = $("#answer-q");
  const statusLine = $("#status");
  const out = $("#out");
  const TOO_MANY = "Too many questions just now. Try again in a minute. Model-picked answers are capped at 200 a day per company.";

  // The footer and the memories name the scopes read in words. A private channel or a connected
  // tool is one kind of place, however many were read.
  const SCOPES = { org_shared: "public channels", org_internal: "your company's internal channels" };
  function scopeWords(scope) {
    if (SCOPES[scope]) return SCOPES[scope];
    if (/^channel:/.test(scope)) return "private channels you are in";
    if (/^connector_/.test(scope)) return "connected tools";
    return scope;
  }
  function worded(data) {
    const scopes = [...new Set((data.scopes || []).map(scopeWords))];
    const memories = (data.memories || []).map((memory) => ({ ...memory, scope: scopeWords(memory.scope) }));
    return { ...data, scopes, memories };
  }

  let asking = 0;

  async function ask(question, how = "push") {
    const q = question.trim();
    if (!q) { input.focus(); return; }
    const ticket = ++asking;
    input.value = q;
    button.disabled = true;
    answerQ.textContent = q;
    statusLine.textContent = "Walking the graph…";
    statusLine.classList.remove("error");
    const address = `/console/ask?q=${encodeURIComponent(q)}`;
    if (how === "push" && `${location.pathname}${location.search}` !== address) history.pushState({ q }, "", address);
    else if (how === "replace") history.replaceState({ q }, "", address);
    try {
      const data = await C.api("POST", "/console/api/ask", { q });
      if (ticket !== asking) return;
      statusLine.textContent = "";
      BrainAnswer.render(out, worded(data), q, {
        onAsk: (next) => { ask(next); out.closest(".answer").scrollIntoView({ block: "start" }); },
        linkFor: (link) => new URL(`/console/ask?q=${encodeURIComponent(link)}`, location.origin).href,
        examples: () => [],
        isCurrent: (current) => answerQ.textContent === current,
        scopes: {},
      });
    } catch (error) {
      if (ticket !== asking || error.status === 401) return;
      out.replaceChildren();
      statusLine.textContent = error.status === 429 ? TOO_MANY : `No answer: ${error.message}`;
      statusLine.classList.add("error");
    } finally {
      if (ticket === asking) button.disabled = false;
    }
  }

  async function start() {
    let me;
    try { me = await C.boot(); } catch (error) { return; }
    $("#org-name").textContent = me.org.name;
    $("#ask-mode-text").textContent = C.isAdmin(me) ? "Your access · the model picks the tool" : "Your access · picked by keywords";
    $("#ask-mode").title = C.isAdmin(me)
      ? "Owners and admins: one model call picks the tool, within your company's daily cap; keywords otherwise."
      : "The keyword router picks the tool from your question's words.";
    form.addEventListener("submit", (event) => { event.preventDefault(); ask(input.value); });
    // The question shapes fill the box and put the cursor where the name goes.
    for (const chip of document.querySelectorAll("#starters [data-start]")) {
      chip.addEventListener("click", () => {
        input.value = chip.dataset.start;
        const at = chip.dataset.caret ? Number(chip.dataset.caret) : input.value.length;
        input.focus();
        input.setSelectionRange(at, at);
      });
    }
    window.addEventListener("popstate", (event) => {
      const q = (event.state && event.state.q) || new URLSearchParams(location.search).get("q");
      if (q) ask(q, "none");
    });
    const initial = new URLSearchParams(location.search).get("q");
    if (initial) ask(initial, "replace");
    else input.focus();
  }

  start();
})();
