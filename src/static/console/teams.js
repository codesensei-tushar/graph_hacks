// /console/teams: the company's teams, their people and what each runs (GET /console/api/teams),
// and, for owners and admins, "Import a team file": paste or upload the TOML, check it (POST
// .../teams/import with check: true, which writes nothing), read the preview or the problems,
// then confirm the merge with the same text. Problems are the API's own sentences about the
// file just sent, shown as text.
(() => {
  "use strict";

  const C = window.Console;
  const { el, $ } = C;
  const bannerBox = $("#banner");
  const list = $("#teams");
  const area = $("#team-file");
  const checkButton = $("#check");
  const preview = $("#preview");
  const LIMIT = 256 * 1024;
  let me = null;
  let known = [];
  let checked = null; // the exact text the preview describes

  const KIND_ORDER = ["service", "system", "process", "product", "account", "policy", "playbook"];
  const kindPlural = (kind, n) => (n === 1 ? kind : `${kind}s`);

  // --- The teams --------------------------------------------------------------------------

  function teamCard(team) {
    const item = el("li", { class: "team-card", id: `team-${team.id}` });
    const head = el("div", { class: "team-head" });
    head.append(el("h3", {}, team.name));
    const people = team.members.length;
    head.append(el("span", { class: "team-counts" }, `${C.plural(people, "person", "people")} · ${C.plural(team.runs.length, "thing", "things")} it runs`));
    item.append(head);
    if (team.summary) item.append(el("p", { class: "team-summary" }, team.summary));

    const body = el("div", { class: "team-body" });
    const who = el("div");
    who.append(el("h4", {}, "People"));
    const members = el("ul", { class: "team-people" });
    for (const member of team.members) {
      const row = el("li");
      row.append(el("span", { class: "name" }, member.name));
      if (member.role) row.append(el("span", { class: "dim" }, member.role));
      members.append(row);
    }
    if (!people) members.append(el("li", { class: "dim" }, "No one yet."));
    who.append(members);

    const what = el("div");
    what.append(el("h4", {}, "Runs"));
    const runs = el("ul", { class: "team-runs" });
    const sorted = [...team.runs].sort((a, b) => KIND_ORDER.indexOf(a.kind) - KIND_ORDER.indexOf(b.kind) || a.name.localeCompare(b.name));
    for (const thing of sorted) {
      const row = el("li");
      const line = el("span", { class: "name" });
      line.append(thing.name, " ", el("span", { class: "kind-chip" }, thing.kind));
      row.append(line);
      row.append(thing.owners.length
        ? el("span", { class: "dim" }, `owned by ${thing.owners.map((o) => o.name).join(", ")}`)
        : el("span", { class: "no-owner" }, "no owner yet"));
      runs.append(row);
    }
    if (!team.runs.length) runs.append(el("li", { class: "dim" }, "Nothing yet."));
    what.append(runs);
    body.append(who, what);
    item.append(body);
    return item;
  }

  function empty() {
    const box = el("li", { class: "empty" });
    box.append(el("h2", {}, "No teams yet"));
    box.append(el("p", {}, C.isAdmin(me)
      ? "Add your first team, so answers know who owns what. Import its team file above; a team of three takes a few minutes to write."
      : "Once an owner or admin imports a team file, its people and what it runs show up here and in your answers."));
    return box;
  }

  async function load(highlight) {
    try {
      const data = await C.api("GET", "/console/api/teams");
      known = (data && data.teams) || [];
      list.removeAttribute("aria-busy");
      list.replaceChildren(...(known.length ? known.map(teamCard) : [empty()]));
      if (highlight) {
        const card = document.getElementById(`team-${highlight}`);
        if (card) { card.classList.add("just-added"); card.scrollIntoView({ block: "center" }); }
      }
    } catch (error) {
      if (error.status === 401) return;
      list.removeAttribute("aria-busy");
      const box = el("li", { class: "empty" });
      box.append(el("h2", {}, "The teams could not be loaded"), el("p", {}, error.message));
      const retry = el("button", { class: "btn btn-ghost", type: "button" }, "Try again");
      retry.addEventListener("click", () => load());
      const actions = el("div", { class: "actions" });
      actions.append(retry);
      box.append(actions);
      list.replaceChildren(box);
    }
  }

  // --- Import ---------------------------------------------------------------------------

  function show(tone, ...nodes) {
    preview.className = `preview ${tone}`;
    preview.replaceChildren(...nodes);
    preview.hidden = false;
  }

  function counts(result) {
    const chips = el("ul", { class: "chips preview-counts", "aria-label": "What the file adds" });
    const add = (text) => chips.append(el("li", { class: "kind-chip" }, text));
    add(C.plural(result.counts.people, "person", "people"));
    const runs = result.counts.runs || {};
    for (const kind of [...KIND_ORDER, ...Object.keys(runs).filter((k) => !KIND_ORDER.includes(k))]) {
      if (runs[kind]) add(`${C.fmt(runs[kind])} ${kindPlural(kind, runs[kind])}`);
    }
    if (result.counts.decisions) add(C.plural(result.counts.decisions, "decision", "decisions"));
    return chips;
  }

  function ready(result) {
    const exists = known.some((team) => team.id === result.team);
    const lead = el("p", { class: "preview-lead" });
    lead.append(el("b", {}, "Ready to import. "), exists
      ? "This team is already here: importing updates it and deletes nothing."
      : "Nothing has been written yet.");
    const confirm = el("button", { class: "btn", type: "button", id: "confirm-import" }, `Import ${result.contents.split(":")[0]}`);
    confirm.addEventListener("click", () => merge(confirm));
    const edit = el("button", { class: "link-btn", type: "button" }, "Edit the file");
    edit.addEventListener("click", () => { preview.hidden = true; area.focus(); });
    const actions = el("div", { class: "actions" });
    actions.append(confirm, edit);
    show("ok", lead, el("p", { class: "preview-contents mono" }, result.contents), counts(result), actions);
  }

  function refused(problems) {
    const items = el("ol", { class: "problems" });
    for (const problem of problems) items.append(el("li", {}, String(problem)));
    const lead = el("p", { class: "preview-lead" });
    lead.append(el("b", {}, problems.length === 1 ? "One thing to fix. " : `${problems.length} things to fix. `), "Nothing was written. Fix the file and check it again.");
    show("error", lead, items);
  }

  function failed(error) {
    if (error.status === 413 || error.code === "too_large") show("error", el("p", { class: "preview-lead" }, `The file is over ${Math.round(LIMIT / 1024)} KB. Split it into one file per team.`));
    else if (error.status !== 401) show("error", el("p", { class: "preview-lead" }, error.message));
  }

  // The import route answers a refusal with the file's problems, which C.api's sentence drops;
  // this call reads them itself, with the same session headers.
  async function post(check) {
    const session = await C.whoami();
    const response = await fetch("/console/api/teams/import", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "application/json", "X-CSRF-Token": session.csrf },
      body: JSON.stringify({ file: checked, check }),
    });
    if (response.status === 401) { location.assign("/signin?expired=1"); throw new C.ApiError("Signed out.", 401); }
    let data = null;
    try { data = await response.json(); } catch (error) { data = null; }
    if (response.status === 400 && data && data.error === "refused" && Array.isArray(data.problems)) return { problems: data.problems };
    if (!response.ok) {
      const code = data && (data.error || data.detail);
      const message = response.status === 403 ? "Only owners and admins can import a team." : response.status === 415 ? "The file could not be sent. Reload and try again." : response.status === 400 ? "That file could not be read. Check it is the team file's text." : "Company Brain could not do that just now. Try again in a minute.";
      throw new C.ApiError(message, response.status, code || "");
    }
    return data;
  }

  async function check() {
    checked = area.value;
    if (new Blob([checked]).size > LIMIT) { failed({ status: 413 }); return; }
    checkButton.disabled = true;
    checkButton.setAttribute("aria-busy", "true");
    checkButton.textContent = "Checking…";
    try {
      const result = await post(true);
      if (result.problems) refused(result.problems); else ready(result);
    } catch (error) {
      failed(error);
    } finally {
      checkButton.removeAttribute("aria-busy");
      checkButton.textContent = "Check the file";
      checkButton.disabled = !area.value.trim();
    }
  }

  async function merge(button) {
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.textContent = "Importing…";
    try {
      const result = await post(false);
      if (result.problems) { refused(result.problems); return; }
      preview.hidden = true;
      area.value = "";
      checked = null;
      checkButton.disabled = true;
      const made = result.nodes_created ? `${C.plural(result.nodes_created, "new node", "new nodes")} and ${C.plural(result.edges_created, "new link", "new links")}` : "nothing new; what was there is up to date";
      C.banner(bannerBox, `Imported ${result.contents}. ${made[0].toUpperCase()}${made.slice(1)}.`, "ok");
      await load(result.team);
    } catch (error) {
      failed(error);
    }
  }

  function watchImport() {
    $("#import").hidden = false;
    area.addEventListener("input", () => {
      checkButton.disabled = !area.value.trim();
      if (checked !== null && area.value !== checked) preview.hidden = true;
    });
    $("#import-form").addEventListener("submit", (event) => { event.preventDefault(); if (area.value.trim()) check(); });
    $("#team-upload").addEventListener("change", (event) => {
      const file = event.target.files && event.target.files[0];
      if (!file) return;
      if (file.size > LIMIT) { failed({ status: 413 }); return; }
      const reader = new FileReader();
      reader.addEventListener("load", () => {
        area.value = String(reader.result || "");
        checkButton.disabled = !area.value.trim();
        preview.hidden = true;
        check();
      });
      reader.readAsText(file);
      event.target.value = "";
    });
  }

  async function start() {
    try { me = await C.boot(); } catch (error) { return; }
    if (C.isAdmin(me)) watchImport();
    load();
  }

  start();
})();
