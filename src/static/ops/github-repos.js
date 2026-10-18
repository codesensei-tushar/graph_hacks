// The operator page's public repos: add by address, watch the backfill, sync again, remove.
// Starts once github-app.js has the token (its "githubapp:ready" event), and calls through it.
(() => {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const el = (tag, attrs = {}, text) => {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    if (text !== undefined) node.textContent = text;
    return node;
  };
  let call = null;
  let banner = () => {};
  let timer = null;

  const org = () => encodeURIComponent($("#public-org").value.trim() || "quillfern");
  const PHASES = { start: "Starting", issues: "Reading issues", pulls: "Reading pull requests", done: "Up to date", stopped: "Stopped" };

  function row(repo) {
    const item = el("li", { class: "repo-row" });
    const head = el("p");
    head.append(el("b", {}, repo.full_name), ` · ${repo.status === "removing" ? "Removing" : PHASES[repo.phase] || repo.status}`);
    item.append(head);
    const depth = repo.backfill_limit === 0 ? "all" : String(repo.backfill_limit);
    item.append(el("p", { class: "small" }, `${repo.pull_requests} pull requests, ${repo.issues} issues in the graph · depth ${depth} · ${repo.scope}`));
    if (repo.last_error) item.append(el("p", { class: "small" }, repo.last_error));
    if (repo.status !== "removing") {
      const sync = el("button", { class: "btn btn-ghost btn-sm", type: "button" }, "Sync again");
      sync.addEventListener("click", () => act("POST", `/ops/github/public-repos/${encodeURIComponent(repo.id)}/sync?org=${org()}`, {}));
      const remove = el("button", { class: "btn btn-danger btn-sm", type: "button" }, "Remove");
      remove.addEventListener("click", () => {
        if (window.confirm(`Remove ${repo.full_name} and everything it brought into the graph?`)) act("DELETE", `/ops/github/public-repos/${encodeURIComponent(repo.id)}?org=${org()}`);
      });
      item.append(sync, document.createTextNode(" "), remove);
    }
    return item;
  }

  async function act(method, path, body) {
    try {
      await call(method, path, body);
      await load();
    } catch (error) {
      banner(error.message, "error");
    }
  }

  async function load() {
    if (timer) clearTimeout(timer);
    const data = await call("GET", `/ops/github/public-repos?org=${org()}`);
    const repos = (data && data.repos) || [];
    $("#public-list").replaceChildren(...repos.map(row));
    if (repos.some((repo) => repo.status === "backfilling" || repo.status === "removing")) timer = setTimeout(() => load().catch(() => {}), 10000);
  }

  document.addEventListener("githubapp:ready", (event) => {
    call = event.detail.call;
    banner = event.detail.banner;
    $("#public").hidden = false;
    load().catch((error) => banner(error.message, "error"));
  });

  $("#public-refresh").addEventListener("click", () => load().catch((error) => banner(error.message, "error")));

  $("#public-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const go = $("#public-go");
    go.disabled = true;
    banner("");
    try {
      const limit = Number.parseInt($("#public-limit").value, 10);
      const repo = await call("POST", `/ops/github/public-repos?org=${org()}`, { repo: $("#public-url").value, backfill: Number.isNaN(limit) || limit === 0 ? "all" : limit });
      banner(`Added ${repo.full_name}. The backfill has started.`, "ok");
      $("#public-url").value = "";
      await load();
    } catch (error) {
      banner(error.message, "error");
    } finally {
      go.disabled = false;
    }
  });
})();
