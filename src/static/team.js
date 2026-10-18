// Company Brain: one team's page. Reads /api/team/<id>; every piece of text goes in with textContent.
(() => {
  "use strict";

  const { el, renderPath, SCOPE_LABEL } = window.BrainGraph;
  const $ = (selector) => document.querySelector(selector);
  const teamId = decodeURIComponent(location.pathname.replace(/\/+$/, "").split("/").pop() || "");
  const count = (n, one, many) => `${n} ${n === 1 ? one : many}`;

  async function load() {
    try {
      const response = await fetch(`/api/team/${encodeURIComponent(teamId)}`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      render(data);
    } catch (error) {
      $("#team-name").textContent = "Team not found";
      $("#team-line").textContent = `${error.message}. The graph does not guess: pick a team from the list on the home page.`;
    }
  }

  function render(data) {
    document.title = `${data.name} · Company Brain`;
    $("#team-name").textContent = data.name;
    const things = data.owns.reduce((sum, group) => sum + group.items.length, 0);
    const parts = [data.company ? `A team at ${data.company}` : "A team", count(data.members.length, "person", "people"), `${count(things, "thing", "things")} it runs`];
    $("#team-line").textContent = `${parts.join(" · ")}.${data.summary ? ` ${data.summary}` : ""}`;

    const members = $("#members");
    for (const member of data.members) {
      const card = el("li");
      card.append(el("b", {}, member.name));
      if (member.role) card.append(el("p", {}, member.role));
      card.append(el("p", { class: "owns-line" }, member.owns.length ? `Owns ${member.owns.join(", ")}` : "Owns nothing directly"));
      members.append(card);
    }
    if (!data.members.length) members.append(el("li", {}, "No one in the graph is a member of this team yet."));

    const owns = $("#owns");
    for (const group of data.owns) {
      owns.append(el("h3", { class: "kind-title" }, `${group.title} (${group.items.length})`));
      const described = group.items.some((item) => item.summary);
      const table = el("table", { class: "tools" });
      const head = el("tr");
      for (const label of described ? ["Name", group.owner_label, "What it is"] : ["Name", group.owner_label]) head.append(el("th", {}, label));
      const thead = el("thead");
      thead.append(head);
      table.append(thead);
      const body = el("tbody");
      for (const item of group.items) {
        const row = el("tr");
        row.append(el("td", { class: "name" }, item.name));
        row.append(el("td", {}, item.owners.join(", ") || "no owner in the graph"));
        if (described) row.append(el("td", {}, item.summary || ""));
        body.append(row);
      }
      table.append(body);
      owns.append(table);
    }
    if (!data.owns.length) owns.append(el("p", { class: "note" }, "This team runs nothing in the graph yet."));

    const decisions = $("#decisions");
    for (const decision of data.decisions) {
      const item = el("li");
      const title = el("p", { class: "decision-title" });
      title.append(el("b", {}, decision.name));
      if (decision.decided_on) title.append(` · decided ${decision.decided_on}`);
      item.append(title);
      if (decision.summary) item.append(el("p", {}, decision.summary));
      const facts = [];
      if (decision.proposed_by.length) facts.push(`Proposed by ${decision.proposed_by.join(", ")}`);
      if (decision.about.length) facts.push(`About ${decision.about.join(", ")}`);
      if (facts.length) item.append(el("p", { class: "meta-line" }, `${facts.join(". ")}.`));
      if (decision.replaced) {
        item.append(el("p", { class: "replaced" }, `Replaced “${decision.replaced}”${decision.replaced_on ? `, decided ${decision.replaced_on}` : ""}.`));
      }
      decisions.append(item);
    }
    if (!data.decisions.length) decisions.append(el("li", {}, "No decision about what this team runs is in the graph yet."));

    const tickets = $("#tickets");
    for (const ticket of data.tickets) tickets.append(el("li", {}, `${ticket.id}: ${ticket.title} (about ${ticket.about})`));
    if (!data.tickets.length) $("#tickets-block").hidden = true;

    const memories = $("#memories");
    for (const memory of data.memories) {
      const learned = memory.learned_at ? `, learned ${memory.learned_at}` : "";
      memories.append(el("li", {}, `“${memory.text}” — ${memory.author}, about ${memory.about}${learned}`));
    }
    if (!data.memories.length) memories.append(el("li", { class: "note" }, "Nothing said in a public channel mentions what this team runs yet."));

    renderPath($("#graph"), data.nodes || [], data.edges || [], { title: "Team graph" });

    const chips = $("#team-chips");
    for (const question of data.questions || []) {
      const link = el("a", { class: "chip", href: `/?q=${encodeURIComponent(question)}` }, question);
      const item = el("li");
      item.append(link);
      chips.append(item);
    }
    if (!chips.children.length) $("#ask-team").hidden = true;

    const read = (data.scopes || []).map((scope) => SCOPE_LABEL[scope] || scope).join(", ");
    $("#team-footer").textContent = `Read from: ${read}. Every line on this page is an edge in the graph.`;
    $("#team-body").hidden = false;
    // A link to a section (/team/sales#runs) lands on it once the sections exist.
    const target = location.hash && document.getElementById(decodeURIComponent(location.hash.slice(1)));
    if (target) target.scrollIntoView();
  }

  load();
})();
