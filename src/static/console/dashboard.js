// /console: one card per connection with its status pill, and the tools to add, each one click.
// While anything is backfilling or being removed the page checks again every 10 seconds.
(() => {
  "use strict";

  const C = window.Console;
  const { el, $ } = C;
  const list = $("#connections");
  const bannerBox = $("#banner");
  const addSection = $("#add");
  let me = null;
  let watcher = null;
  let slackCard = null; // the workspace card, once /console/api/slack answers

  const slackFirst = () => (slackCard ? [slackCard] : []);

  // The Slack workspace: whether the app is in, where it lives, how many channels it reads, when
  // it last heard anything, and Reinstall. A console without that route simply has no card.
  function slackItem(slack) {
    const box = el("div", { class: "conn-card slack-card-c" });
    const head = el("div", { class: "conn-head" });
    const names = el("span", { class: "grow" });
    names.append(el("b", {}, "Slack"), el("span", { class: "conn-site", title: slack.team_name || "" }, slack.team_name || "Your workspace"));
    head.append(C.tile("slack"), names);
    box.append(head);
    const state = el("div", { class: "conn-state" });
    const channels = slack.channels || [];
    const stats = el("p", { class: "conn-stats" });
    const count = el("span");
    count.append(el("b", {}, C.fmt(channels.length)), ` ${channels.length === 1 ? "channel" : "channels"}`);
    stats.append(count);
    const private_ = channels.filter((c) => c.private).length;
    if (private_) stats.append(el("span", {}, `${C.fmt(private_)} private`));
    state.append(C.pill(slack.bot_installed ? { tone: "ok", text: "App installed" } : { tone: "warn", text: "App not installed" }), stats);
    box.append(state);
    const facts = el("p", { class: "conn-note" });
    facts.append(slack.home_channel ? `Home channel #${slack.home_channel.name}` : "No home channel yet");
    facts.append(slack.last_event_at ? ` · last heard ${C.ago(slack.last_event_at)}` : " · nothing heard yet");
    box.append(facts);
    const foot = el("div", { class: "conn-foot" });
    foot.append(el("span", {}, "Everyone in the workspace can ask in Slack."));
    const url = typeof slack.install_url === "string" && (/^https:\/\//.test(slack.install_url) || /^\/(?!\/)/.test(slack.install_url)) ? slack.install_url : "";
    if (C.isAdmin(me) && url) foot.append(el("a", { class: "go", href: url }, slack.bot_installed ? "Reinstall →" : "Install →"));
    box.append(foot);
    const item = el("li");
    item.append(box);
    return item;
  }

  async function loadSlack() {
    try {
      slackCard = slackItem(await C.api("GET", "/console/api/slack"));
      if (!list.hasAttribute("aria-busy") && list.firstElementChild !== slackCard) list.prepend(slackCard);
    } catch (error) {
      slackCard = null;
    }
  }

  // --- Notices carried back in the address -------------------------------------------------------

  function notices() {
    const connected = C.takeParam("connected");
    const removed = C.takeParam("removed");
    const error = C.takeParam("error");
    if (error) C.banner(bannerBox, ...C.notice(error));
    else if (connected) C.banner(bannerBox, `${(C.SOURCES[connected] || { name: "The tool" }).name} is connected. The first sync has started.`, "ok");
    else if (removed) C.banner(bannerBox, "Disconnected. Everything it brought into the graph is being deleted.", "ok");
  }

  // --- Cards ---------------------------------------------------------------------------------

  function card(connection) {
    const source = C.sourceOf(connection);
    const status = C.statusOf(connection);
    const link = el("a", { class: "conn-card", href: C.linkFor(connection) });

    const head = el("div", { class: "conn-head" });
    const names = el("span", { class: "grow" });
    const site = C.siteName(connection);
    names.append(el("b", {}, source.name), el("span", { class: "conn-site", title: site }, site || "Site not chosen yet"));
    head.append(C.tile(connection.source), names);
    link.append(head);

    const containers = connection.containers || [];
    const stats = el("p", { class: "conn-stats" });
    const stat = (n, one, many) => {
      const span = el("span");
      span.append(el("b", {}, C.fmt(n)), ` ${n === 1 ? one : many}`);
      return span;
    };
    stats.append(stat(connection.items || 0, "item", "items"), stat(containers.length, source.noun, source.nouns));
    const state = el("div", { class: "conn-state" });
    state.append(C.pill(status), stats);
    link.append(state);

    if (status.progress !== undefined) {
      const bar = el("div", { class: "bar", role: "progressbar", "aria-label": "Backfill", "aria-valuemin": "0", "aria-valuemax": String(containers.length), "aria-valuenow": String(containers.filter((c) => c.backfilled).length) });
      const fill = el("span");
      // A CSSOM property, not a style attribute: the CSP allows this one.
      fill.style.width = `${Math.round(status.progress * 100)}%`;
      bar.append(fill);
      link.append(bar);
    }
    if (status.tone === "error") link.append(el("p", { class: "conn-error" }, "The last sync failed. Open it to see what happened and sync again."));
    link.append(el("p", { class: "conn-note" }, "Everyone in your Slack workspace can ask about this."));

    const foot = el("div", { class: "conn-foot" });
    foot.append(el("span", {}, connection.created_by ? `Added by ${connection.created_by}` : `Added ${C.ago(connection.created_at)}`));
    foot.append(el("span", { class: "go" }, status.setup ? "Finish setup →" : "Manage →"));
    link.append(foot);

    const item = el("li");
    item.append(link);
    return item;
  }

  // --- Tools to add ---------------------------------------------------------------------------

  function connectButton(source, connections) {
    const meta = C.SOURCES[source];
    const has = connections.some((c) => c.source === source);
    const label = has ? `Connect another ${meta.noun === "project" ? "site" : meta.noun}` : `Connect ${meta.name}`;
    const button = el("button", { class: has ? "btn btn-ghost" : "btn", type: "button" }, label);
    button.addEventListener("click", () => C.connect(source, button, (message) => C.banner(bannerBox, message, "error")));
    return button;
  }

  function connectors(target, connections) {
    target.replaceChildren();
    for (const source of Object.keys(C.SOURCES)) {
      const meta = C.SOURCES[source];
      const item = el("li");
      const head = el("div", { class: "conn-head" });
      const names = el("span", { class: "grow" });
      names.append(el("b", {}, meta.name));
      head.append(C.tile(source), names);
      item.append(head, el("p", {}, meta.blurb));
      if (C.isAdmin(me)) item.append(connectButton(source, connections));
      else item.append(el("p", { class: "small" }, "An owner or admin of your company can connect it."));
      target.append(item);
    }
  }

  // --- States --------------------------------------------------------------------------------

  function empty() {
    addSection.hidden = true;
    const box = el("li", { class: "empty" });
    box.append(el("h2", {}, "No tools connected yet"));
    box.append(el("p", {}, C.isAdmin(me)
      ? "Connect Jira or HubSpot and Company Brain starts reading it into your graph. Nothing is ever written back."
      : "Once an owner or admin connects Jira or HubSpot, what it holds shows up here and in your answers."));
    if (C.isAdmin(me)) {
      const actions = el("div", { class: "actions" });
      for (const source of Object.keys(C.SOURCES)) actions.append(connectButton(source, []));
      box.append(actions);
    }
    list.replaceChildren(...slackFirst(), box);
  }

  function failed(message) {
    addSection.hidden = true;
    const box = el("li", { class: "empty" });
    box.append(el("h2", {}, "Your connections could not be loaded"));
    box.append(el("p", {}, message));
    const actions = el("div", { class: "actions" });
    const retry = el("button", { class: "btn btn-ghost", type: "button" }, "Try again");
    retry.addEventListener("click", () => { list.setAttribute("aria-busy", "true"); load(); });
    actions.append(retry);
    box.append(actions);
    list.replaceChildren(box);
  }

  function render(connections) {
    list.removeAttribute("aria-busy");
    if (!connections.length) { empty(); return false; }
    list.replaceChildren(...slackFirst(), ...connections.map(card));
    connectors($("#connectors"), connections);
    if (!C.isAdmin(me)) $("#add-sub").textContent = "These are the tools Company Brain can read. Ask an owner or admin of your company to connect one.";
    addSection.hidden = false;
    return connections.some((c) => C.statusOf(c).syncing);
  }

  async function load() {
    try {
      const connections = await C.api("GET", "/console/api/connections");
      const syncing = render(connections || []);
      watcher.start(syncing);
      return syncing;
    } catch (error) {
      if (error.status !== 401) failed(error.message);
      return false;
    }
  }

  async function start() {
    try {
      me = await C.boot();
    } catch (error) {
      if (error.status && error.status !== 401) failed(error.message);
      return;
    }
    $("#org-name").textContent = me.org.name;
    notices();
    watcher = C.poll(async () => {
      const connections = await C.api("GET", "/console/api/connections");
      return render(connections || []);
    }, $("#poll-note"));
    loadSlack();
    nudgeTeams();
    load();
  }

  // No team yet: answers cannot say who owns what, so the page says where to fix that.
  async function nudgeTeams() {
    try {
      const data = await C.api("GET", "/console/api/teams");
      if ((data && data.teams || []).length) return;
    } catch (error) {
      return;
    }
    if (!C.isAdmin(me)) {
      $("#teams-nudge-sub").textContent = "An owner or admin of your company can add them on the Teams page.";
      $("#teams-nudge-go").textContent = "See Teams";
    }
    $("#teams-nudge").hidden = false;
  }

  start();
})();
