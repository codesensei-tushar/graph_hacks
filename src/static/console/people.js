// /console/people (owners and admins): Jira and HubSpot accounts linked to no person
// (GET /console/api/people/unlinked), each with its source, how many items it touched, one-click
// "Link to <name>" for each suggestion with the reason, and a picker for anyone else. Linking
// is POST /console/api/people/link {external_user_id, person_id}; a linked row stays, saying who
// it is linked to, with Change to link it again. No email or domain is ever shown: the API
// sends none.
(() => {
  "use strict";

  const C = window.Console;
  const { el, $ } = C;
  const bannerBox = $("#banner");
  const list = $("#people");
  const filter = $("#people-filter");
  const REASONS = { same_name: "same name", same_email_domain: "same email domain" };
  let people = [];
  let left = 0;

  function countLine() {
    $("#people-count").replaceChildren(left
      ? `${C.plural(left, "account", "accounts")} to link`
      : "Every account on this page is linked");
  }

  function picker(account, row) {
    const box = el("div", { class: "person-pick" });
    const id = `pick-${account.id.replace(/[^a-z0-9]/gi, "-")}`;
    box.append(el("label", { class: "sr", for: id }, `Person to link ${account.name} to`));
    const select = el("select", { class: "input", id });
    select.append(el("option", { value: "" }, "Choose a person…"));
    for (const person of people) select.append(el("option", { value: person.id }, person.name));
    const go = el("button", { class: "btn btn-sm", type: "button", disabled: "" }, "Link");
    select.addEventListener("change", () => { go.disabled = !select.value; });
    go.addEventListener("click", () => link(account, select.value, row, go));
    box.append(select, go);
    return box;
  }

  function linked(row, account, person, note) {
    row.classList.add("is-linked");
    const done = el("div", { class: "person-done" });
    done.append(el("span", { class: "sdot ok", "aria-hidden": "true" }), note || "Linked to ", el("b", {}, person ? person.name : ""));
    const change = el("button", { class: "link-btn", type: "button" }, "Change");
    change.addEventListener("click", () => {
      row.classList.remove("is-linked");
      row.querySelector(".person-done").remove();
      row.querySelector(".person-actions").hidden = false;
    });
    if (person) done.append(change);
    row.querySelector(".person-actions").hidden = true;
    row.append(done);
  }

  async function link(account, personId, row, button) {
    const label = button.textContent;
    for (const control of row.querySelectorAll("button, select")) control.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.textContent = "Linking…";
    const wasLinked = row.classList.contains("was-linked");
    try {
      const result = await C.api("POST", "/console/api/people/link", { external_user_id: account.id, person_id: personId });
      if (!wasLinked) { left -= 1; row.classList.add("was-linked"); }
      countLine();
      linked(row, account, result.person);
      C.banner(bannerBox, `${account.name} is now ${result.person.name}. Their ${C.sourceOf(account).name} work counts as theirs.`, "ok");
    } catch (error) {
      if (error.status === 401) return;
      if (error.status === 409) {
        if (!wasLinked) { left -= 1; row.classList.add("was-linked"); }
        countLine();
        linked(row, account, null, "Already linked to a person by email; nothing changed.");
      } else {
        C.banner(bannerBox, error.status === 404 ? "That account or person is no longer there. Reload to see the current list." : error.message, "error");
      }
    } finally {
      button.removeAttribute("aria-busy");
      button.textContent = label;
      for (const control of row.querySelectorAll("button, select")) control.disabled = false;
      const select = row.querySelector("select");
      if (select) row.querySelector(".person-pick .btn").disabled = !select.value;
    }
  }

  function row(account) {
    const item = el("li", { class: "person-row" });
    item.dataset.search = account.name.toLowerCase();
    const who = el("div", { class: "person-who" });
    const top = el("div", { class: "person-name" });
    top.append(C.tile(account.source), el("b", {}, account.name));
    who.append(top);
    const meta = el("p", { class: "person-meta" });
    meta.append(el("span", { class: `source-badge ${account.source}` }, C.sourceOf(account).name), C.plural(account.items || 0, "item", "items"));
    who.append(meta);
    item.append(who);

    const actions = el("div", { class: "person-actions" });
    const suggestions = account.suggestions || [];
    if (suggestions.length) {
      const offers = el("ul", { class: "suggestions", "aria-label": `Suggested people for ${account.name}` });
      for (const suggestion of suggestions) {
        const offer = el("li");
        const button = el("button", { class: "btn btn-ghost btn-sm", type: "button" }, `Link to ${suggestion.name}`);
        button.addEventListener("click", () => link(account, suggestion.person_id, item, button));
        offer.append(button, el("span", { class: "reason" }, REASONS[suggestion.reason] || "suggested"));
        offers.append(offer);
      }
      actions.append(offers);
      const other = el("button", { class: "link-btn other", type: "button" }, "Someone else…");
      other.addEventListener("click", () => { other.replaceWith(picker(account, item)); item.querySelector("select").focus(); });
      actions.append(other);
    } else {
      actions.append(el("p", { class: "reason none" }, "No likely match."), picker(account, item));
    }
    item.append(actions);
    return item;
  }

  function applyFilter() {
    const words = filter.value.toLowerCase().split(/\s+/).filter(Boolean);
    let shown = 0;
    for (const item of list.children) {
      const match = words.every((word) => (item.dataset.search || "").includes(word));
      item.hidden = !match;
      if (match) shown += 1;
    }
    const none = $("#people-none");
    none.hidden = shown > 0;
    if (!shown) none.textContent = `No account matches “${filter.value.trim()}”.`;
  }

  function empty() {
    const box = el("li", { class: "empty" });
    box.append(el("h2", {}, "Everyone is linked"));
    box.append(el("p", {}, "Every Jira and HubSpot account Company Brain has read belongs to a person. New accounts show up here after a sync if their email is hidden."));
    const actions = el("div", { class: "actions" });
    actions.append(el("a", { class: "btn btn-ghost", href: "/console" }, "Back to Connections"));
    box.append(actions);
    return box;
  }

  function failed(message) {
    const box = el("li", { class: "empty" });
    box.append(el("h2", {}, "The accounts could not be loaded"), el("p", {}, message));
    const retry = el("button", { class: "btn btn-ghost", type: "button" }, "Try again");
    retry.addEventListener("click", () => location.reload());
    const actions = el("div", { class: "actions" });
    actions.append(retry);
    box.append(actions);
    list.replaceChildren(box);
  }

  async function start() {
    let me;
    try { me = await C.boot(); } catch (error) { return; }
    if (!C.isAdmin(me)) {
      list.removeAttribute("aria-busy");
      failed("Only owners and admins can link people. Ask one of them.");
      list.querySelector(".actions").remove();
      return;
    }
    try {
      const data = (await C.api("GET", "/console/api/people/unlinked")) || {};
      const accounts = data.unlinked || [];
      people = [...(data.people || [])].sort((a, b) => a.name.localeCompare(b.name));
      list.removeAttribute("aria-busy");
      if (!accounts.length) { list.replaceChildren(empty()); return; }
      left = accounts.length;
      list.replaceChildren(...accounts.map(row));
      countLine();
      $("#people-bar").hidden = false;
      filter.addEventListener("input", applyFilter);
    } catch (error) {
      list.removeAttribute("aria-busy");
      if (error.status !== 401) failed(error.message);
    }
  }

  start();
})();
