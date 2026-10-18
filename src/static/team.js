// Company Brain: one team's page. Reads /api/team/<id>; every piece of text goes in with textContent.
(() => {
  "use strict";

  const { el, renderPath, SCOPE_LABEL } = window.BrainGraph;
  const $ = (selector) => document.querySelector(selector);
  const teamId = decodeURIComponent(location.pathname.replace(/\/+$/, "").split("/").pop() || "");
  const count = (n, one, many) => `${n} ${n === 1 ? one : many}`;
  const listed = (words) => (words.length < 2 ? words.join("") : `${words.slice(0, -1).join(", ")} and ${words[words.length - 1]}`);

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
    // The team in one line: who it leans on, who leans on it, and what rests on one person.
    if (data.glance) {
      $("#team-glance").textContent = data.glance;
      $("#team-glance").hidden = false;
    }
    // An empty section is not drawn as an empty box; one line says what the graph does not hold yet.
    if (data.empty && data.empty.length) {
      $("#team-missing").textContent = `Not in the graph yet for this team: ${listed(data.empty)}.`;
      $("#team-missing").hidden = false;
    }

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

    // Ties to other teams, both ways, one DEPENDS_ON edge per line; each team links to its page.
    const links = $("#team-links");
    const side = (title, teams) => {
      if (!teams.length) return;
      links.append(el("h3", { class: "kind-title" }, title));
      for (const team of teams) {
        const head = el("p", { class: "link-team" });
        head.append(el("a", { href: `/team/${encodeURIComponent(team.id)}` }, team.name), ` · ${count(team.pairs.length, "edge", "edges")}`);
        const lines = el("ul", { class: "plain edge-lines" });
        for (const [dependent, dependency] of team.pairs) lines.append(el("li", { class: "mono" }, `${dependent} -[DEPENDS_ON]-> ${dependency}`));
        links.append(head, lines);
      }
    };
    side("Depends on", data.depends_on || []);
    side("Depended on by", data.depended_on_by || []);
    if (!links.children.length) $("#links").hidden = true;

    // The team's rows of the risk map; a thing's name asks about it on the ask page.
    if (data.risk && data.risk.rows && data.risk.rows.length && window.RiskMap) {
      window.RiskMap.render($("#team-risk"), data.risk, { ask: (question) => { location.href = `/?q=${encodeURIComponent(question)}`; } });
    } else {
      $("#risk").hidden = true;
    }

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
    if (!data.decisions.length) $("#decisions-block").hidden = true;

    const tickets = $("#tickets");
    for (const ticket of data.tickets) tickets.append(el("li", {}, `${ticket.id}: ${ticket.title} (about ${ticket.about})`));
    if (!data.tickets.length) $("#tickets-block").hidden = true;

    const memories = $("#memories");
    for (const memory of data.memories) {
      const learned = memory.learned_at ? `, learned ${memory.learned_at}` : "";
      memories.append(el("li", {}, `“${memory.text}” — ${memory.author}, about ${memory.about}${learned}`));
    }
    if (!data.memories.length) $("#said").hidden = true;

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

  // The other teams, so a reader can go from one team to the next without the home page.
  async function loadSwitcher() {
    try {
      const response = await fetch("/api/teams");
      if (!response.ok) return;
      const teams = (await response.json()).teams || [];
      if (teams.length < 2) return;
      const holder = $("#team-switch");
      for (const team of teams) {
        holder.append(el("span", { class: "sep", "aria-hidden": "true" }, " · "));
        if (team.id === teamId) holder.append(el("span", { "aria-current": "page" }, team.name));
        else holder.append(el("a", { href: `/team/${encodeURIComponent(team.id)}` }, team.name));
      }
      holder.hidden = false;
    } catch {
      // The page reads fine without the switcher.
    }
  }

  load();
  loadSwitcher();
})();
