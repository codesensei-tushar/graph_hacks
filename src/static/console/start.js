// /console/start: three steps for a new company. 1, its name and an invite code (POST
// /console/api/orgs); 2, the Slack app (me.slack_connected); 3, a first tool. Each step that is
// done says so; the first that is not is open.
(() => {
  "use strict";

  const C = window.Console;
  const { el, $ } = C;
  const bannerBox = $("#banner");
  const steps = { org: $("#step-org"), slack: $("#step-slack"), tool: $("#step-tool") };
  let me = null;

  function show(connections) {
    // A new company's creator is "pending" until the Slack install confirms them; then owner.
    const pending = me.role === "pending";
    const done = { org: Boolean(me.org), slack: Boolean(me.org && me.slack_connected && !pending), tool: connections.length > 0 };
    let open = false;
    for (const name of ["org", "slack", "tool"]) {
      const state = done[name] ? "done" : open ? "todo" : "current";
      if (state === "current") open = true;
      steps[name].dataset.state = state;
      steps[name].toggleAttribute("aria-current", state === "current");
    }
    $("#setup").removeAttribute("aria-busy");
    if (me.org) {
      $("#org-done").textContent = pending
        ? `${me.org.name} is created. Adding the Slack app confirms you as its owner.`
        : `${me.org.name} is set up, with you as its ${me.role || "member"}.`;
    }
    if (done.tool) {
      const line = $("#tool-done");
      const names = [...new Set(connections.map((c) => C.sourceOf(c).name))].join(" and ");
      line.replaceChildren(`${names} connected. `, el("a", { href: "/console" }, "Open the console →"));
    }
    if (done.org && done.slack && done.tool) C.banner(bannerBox, "You are set up. Your tools sync in the background.", "ok");
    tools();
  }

  function tools() {
    const list = $("#connectors");
    list.replaceChildren();
    for (const source of Object.keys(C.SOURCES)) {
      const meta = C.SOURCES[source];
      const item = el("li");
      const head = el("div", { class: "conn-head" });
      const names = el("span", { class: "grow" });
      names.append(el("b", {}, meta.name));
      head.append(C.tile(source), names);
      item.append(head, el("p", {}, meta.blurb));
      if (C.isAdmin(me)) {
        const button = el("button", { class: "btn", type: "button" }, `Connect ${meta.name}`);
        button.addEventListener("click", () => C.connect(source, button, (message) => C.banner(bannerBox, message, "error")));
        item.append(button);
      } else {
        item.append(el("p", { class: "small" }, "An owner or admin can connect it."));
      }
      list.append(item);
    }
  }

  async function connections() {
    if (!me.org || me.role === "pending") return [];
    try { return (await C.api("GET", "/console/api/connections")) || []; } catch (error) { return []; }
  }

  // Step 1. The invite code's own error sits under its field; anything else is a banner.
  function watchForm() {
    const form = $("#org-form");
    const name = $("#org-name-input");
    const code = $("#invite-input");
    const fieldError = $("#invite-error");
    const submit = $("#org-submit");
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      fieldError.textContent = "";
      code.removeAttribute("aria-invalid");
      name.removeAttribute("aria-invalid");
      C.banner(bannerBox, "");
      if (!name.value.trim()) { name.setAttribute("aria-invalid", "true"); name.focus(); return; }
      if (!code.value.trim()) {
        code.setAttribute("aria-invalid", "true");
        fieldError.textContent = "Enter your invite code.";
        code.focus();
        return;
      }
      submit.disabled = true;
      submit.setAttribute("aria-busy", "true");
      submit.textContent = "Creating…";
      try {
        // The answer is the whole session again, rotated, with a new CSRF token.
        me = C.remember(await C.api("POST", "/console/api/orgs", { name: name.value.trim(), invite_code: code.value.trim() }));
        $("#top-org").textContent = me.org.name;
        $("#top-org").hidden = false;
        show(await connections());
        $("#step-slack h2").focus();
      } catch (error) {
        if (error.code === "invalid_invite") {
          code.setAttribute("aria-invalid", "true");
          fieldError.textContent = "That invite code is not valid, or it has already been used.";
          code.focus();
        } else if (error.code === "bad_name") {
          name.setAttribute("aria-invalid", "true");
          C.banner(bannerBox, "Give the company a name of at least 2 characters, in plain text.", "error");
          name.focus();
        } else if (error.code === "already_in_org") {
          C.banner(bannerBox, "Your Slack workspace already belongs to a company. Reload to continue its setup.", "error");
        } else if (error.status !== 401) {
          C.banner(bannerBox, error.message, "error");
        }
      } finally {

        submit.disabled = false;
        submit.removeAttribute("aria-busy");
        submit.textContent = "Create company";
      }
    });
  }

  async function start() {
    try {
      me = await C.boot({ needsOrg: false });
    } catch (error) {
      if (error.status && error.status !== 401) C.banner(bannerBox, error.message, "error");
      return;
    }
    $("#step-slack h2").tabIndex = -1;
    watchForm();
    $("#slack-recheck").addEventListener("click", () => location.reload());
    const error = C.takeParam("error");
    if (error) C.banner(bannerBox, ...C.notice(error));
    show(await connections());
  }

  start();
})();
