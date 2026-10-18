// /console/github: the GitHub accounts this company connected (each with its repos, sync and
// disconnect), Connect GitHub, and public repos read by address. Every text from the API goes in
// with textContent. While anything is backfilling or being removed the page checks every 10 seconds.
(() => {
  "use strict";

  const C = window.Console;
  const { el, $ } = C;
  const bannerBox = $("#banner");
  let me = null;
  let watcher = null;

  const say = (text, tone = "") => C.banner(bannerBox, text, tone);
  const fail = (error) => { if (error.status !== 401) say(error.message, "error"); };
  const safeHttps = (url) => (typeof url === "string" && /^https:\/\/github\.com\//.test(url) ? url : "");
  const busy = (status) => status === "backfilling" || status === "waiting" || status === "removing";

  function repoStatus(repo) {
    if (repo.status === "removing") return { tone: "busy", text: "Removing…" };
    if (repo.last_error && repo.status !== "ready") return { tone: "error", text: "Needs attention" };
    if (repo.status === "stopped") return { tone: "warn", text: "Stopped" };
    if (repo.status === "ready") return { tone: "ok", text: "Up to date" };
    return { tone: "busy", text: "Backfilling" };
  }

  function stats(repo) {
    const line = el("p", { class: "conn-stats" });
    for (const [n, one, many] of [[repo.pull_requests, "pull request", "pull requests"], [repo.issues, "issue", "issues"]]) {
      const span = el("span");
      span.append(el("b", {}, C.fmt(n)), ` ${n === 1 ? one : many}`);
      line.append(span);
    }
    return line;
  }

  function repoRow(repo) {
    const row = el("div", { class: "conn-state" });
    const name = el("span", { class: "grow" });
    name.append(el("b", {}, repo.full_name), el("span", { class: "conn-site" }, repo.private ? " · private: collaborators, in a DM" : " · public: everyone"));
    row.append(name, C.pill(repoStatus(repo)), stats(repo));
    const box = el("div");
    box.append(row);
    if (repo.last_error) box.append(el("p", { class: "conn-error" }, repo.last_error));
    return box;
  }

  async function act(button, method, path, body, done) {
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    try {
      const data = await C.api(method, path, body);
      if (done) done(data);
      await load();
    } catch (error) {
      fail(error);
    } finally {
      button.disabled = false;
      button.removeAttribute("aria-busy");
    }
  }

  function installationCard(item) {
    const box = el("div", { class: "conn-card" });
    const head = el("div", { class: "conn-head" });
    const names = el("span", { class: "grow" });
    names.append(el("b", {}, item.account || `Installation ${item.id}`), el("span", { class: "conn-site" }, item.account_type === "Organization" ? "GitHub organization" : "GitHub account"));
    head.append(C.tile("github"), names);
    head.append(C.pill(item.status === "removing" ? { tone: "busy", text: "Disconnecting…" } : item.suspended ? { tone: "warn", text: "Suspended on GitHub" } : { tone: "ok", text: "Connected" }));
    box.append(head);
    if (!item.repos.length) box.append(el("p", { class: "conn-note" }, item.status === "removing" ? "Deleting what it brought in." : "Listing its repos…"));
    for (const repo of item.repos) box.append(repoRow(repo));
    const depth = item.backfill_limit === 0 ? "every pull request" : `the newest ${C.fmt(item.backfill_limit)} pull requests`;
    box.append(el("p", { class: "conn-note" }, `Reads ${depth} of each repo, then keeps up through GitHub's webhooks.`));
    const foot = el("div", { class: "conn-foot" });
    const manage = safeHttps(item.manage_url);
    if (manage) foot.append(el("a", { href: manage, rel: "noopener" }, "Choose repos on GitHub →"));
    if (C.isAdmin(me) && item.status !== "removing") {
      const sync = el("button", { class: "btn btn-ghost btn-sm", type: "button" }, "Sync now");
      sync.addEventListener("click", () => act(sync, "POST", `/console/api/github/installations/${encodeURIComponent(item.id)}/sync`, {}, () => say("Syncing again from the start.", "ok")));
      const drop = el("button", { class: "btn btn-danger btn-sm", type: "button" }, "Disconnect");
      drop.addEventListener("click", () => {
        if (!window.confirm(`Disconnect ${item.account || "this GitHub account"}? Everything it brought into your graph is deleted. The app stays installed on GitHub until you remove it there.`)) return;
        act(drop, "DELETE", `/console/api/github/installations/${encodeURIComponent(item.id)}`, undefined, (data) => {
          const url = safeHttps(data && data.uninstall_url);
          say(url ? `Disconnected. Remove the app on GitHub too: ${url}` : "Disconnected.", "ok");
        });
      });
      foot.append(sync, drop);
    }
    box.append(foot);
    const li = el("li");
    li.append(box);
    return li;
  }

  function publicCard(repo) {
    const box = el("div", { class: "conn-card" });
    const head = el("div", { class: "conn-head" });
    const names = el("span", { class: "grow" });
    names.append(el("b", {}, repo.full_name), el("span", { class: "conn-site" }, "Public repo · everyone reads it"));
    head.append(C.tile("github"), names, C.pill(repoStatus(repo)));
    box.append(head, stats(repo));
    if (repo.last_error) box.append(el("p", { class: "conn-error" }, repo.last_error));
    const foot = el("div", { class: "conn-foot" });
    foot.append(el("span", {}, repo.backfill_limit === 0 ? "Every pull request" : `Newest ${C.fmt(repo.backfill_limit)} pull requests`));
    if (C.isAdmin(me) && repo.status !== "removing") {
      const sync = el("button", { class: "btn btn-ghost btn-sm", type: "button" }, "Sync now");
      sync.addEventListener("click", () => act(sync, "POST", `/console/api/github/public-repos/${encodeURIComponent(repo.id)}/sync`, {}));
      const drop = el("button", { class: "btn btn-danger btn-sm", type: "button" }, "Remove");
      drop.addEventListener("click", () => {
        if (window.confirm(`Remove ${repo.full_name} and everything it brought into your graph?`)) act(drop, "DELETE", `/console/api/github/public-repos/${encodeURIComponent(repo.id)}`);
      });
      foot.append(sync, drop);
    }
    box.append(foot);
    const li = el("li");
    li.append(box);
    return li;
  }

  async function load() {
    const [github, publicRepos] = await Promise.all([C.api("GET", "/console/api/github"), C.api("GET", "/console/api/github/public-repos")]);
    const items = (github && github.installations) || [];
    const list = $("#installations");
    list.removeAttribute("aria-busy");
    list.replaceChildren(...(items.length ? items.map(installationCard) : [el("li", { class: "empty" }, "No GitHub account connected yet.")]));
    const repos = (publicRepos && publicRepos.repos) || [];
    $("#public-repos").replaceChildren(...repos.map(publicCard));
    const app = (github && github.app) || {};
    $("#app-note").hidden = Boolean(app.available);
    $("#connect-actions").hidden = !(C.isAdmin(me) && app.connect);
    $("#connect").textContent = items.length ? "Connect another GitHub account" : "Connect GitHub";
    const watching = items.some((item) => item.status === "removing" || item.repos.some((repo) => busy(repo.status))) || repos.some((repo) => busy(repo.status));
    watcher.start(watching);
    return watching;
  }

  async function connect(button) {
    button.disabled = true;
    button.textContent = "Opening GitHub…";
    try {
      const data = await C.api("POST", "/console/api/github/connect");
      const target = data && typeof data.install_url === "string" ? data.install_url : "";
      if (!/^https:\/\/github\.com\/apps\//.test(target)) throw new C.ApiError("Company Brain could not do that just now. Try again in a minute.", 500);
      location.assign(target);
    } catch (error) {
      button.disabled = false;
      button.textContent = "Connect GitHub";
      fail(error);
    }
  }

  function notices() {
    const error = C.takeParam("error");
    const connected = C.takeParam("connected");
    const updated = C.takeParam("updated");
    if (error) say(...C.notice(error));
    else if (connected) say("GitHub is connected. Its repos are being read now.", "ok");
    else if (updated) say("GitHub has the new repo selection; the change arrives here within a minute.", "ok");
  }

  async function start() {
    try {
      me = await C.boot();
    } catch (error) {
      fail(error);
      return;
    }
    notices();
    $("#member-note").hidden = C.isAdmin(me);
    $("#public-form").hidden = !C.isAdmin(me);
    watcher = C.poll(load, $("#poll-note"));
    $("#connect").addEventListener("click", (event) => connect(event.currentTarget));
    $("#public-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const go = $("#public-go");
      const depth = Number.parseInt($("#public-depth").value, 10);
      await act(go, "POST", "/console/api/github/public-repos", { repo: $("#public-repo").value, backfill: Number.isNaN(depth) || depth === 0 ? "all" : depth }, (repo) => {
        say(`Added ${repo.full_name}. Reading it now.`, "ok");
        $("#public-repo").value = "";
      });
    });
    try {
      await load();
    } catch (error) {
      fail(error);
    }
  }

  start();
})();
