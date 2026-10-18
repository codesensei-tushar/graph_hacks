// /console/connectors/<id>/projects: the filterable project picker. It lists what the connection's
// account can see (GET .../available), ticks what is already synced, and saves the choice with
// PUT .../containers. A project already synced matches by its container id, which ends in
// ":container:<external_id>" (Install.container_id).
(() => {
  "use strict";

  const C = window.Console;
  const { el, $ } = C;
  const id = decodeURIComponent(location.pathname.split("/")[3] || "");
  const detail = `/console/connectors/${encodeURIComponent(id)}`;
  const bannerBox = $("#banner");
  const list = $("#pick-list");
  const filter = $("#pick-filter");
  const save = $("#save");
  let source = C.SOURCES.jira;
  let total = 0;
  let saved = new Set();

  const boxes = () => [...list.querySelectorAll("input[type=checkbox]")];
  const chosen = () => boxes().filter((box) => box.checked).map((box) => box.value);

  function counts() {
    const picked = chosen();
    const shown = [...list.children].filter((item) => !item.hidden).length;
    const words = filter.value.trim() ? `${C.fmt(shown)} shown · ` : "";
    $("#pick-count").textContent = `${words}${C.fmt(picked.length)} of ${C.fmt(total)} selected`;
    const changed = picked.length !== saved.size || picked.some((value) => !saved.has(value));
    save.disabled = !picked.length || !changed;
    $("#save-note").textContent = !picked.length
      ? `Choose at least one ${source.noun}.`
      : changed
        ? `${C.plural(picked.length, source.noun, source.nouns)} chosen. Saving starts reading the new ones.`
        : "No changes.";
  }

  function applyFilter() {
    const words = filter.value.toLowerCase().split(/\s+/).filter(Boolean);
    let shown = 0;
    for (const item of list.children) {
      const match = words.every((word) => item.dataset.search.includes(word));
      item.hidden = !match;
      if (match) shown += 1;
    }
    const empty = $("#pick-empty");
    empty.hidden = shown > 0;
    if (!shown) empty.textContent = `No ${source.noun} matches “${filter.value.trim()}”.`;
    counts();
  }

  function render(available) {
    list.removeAttribute("aria-busy");
    list.replaceChildren();
    total = available.length;
    // Already synced first, then by name, so what is on comes into view first.
    const sorted = [...available].sort((a, b) => Number(saved.has(String(b.external_id))) - Number(saved.has(String(a.external_id))) || a.name.localeCompare(b.name));
    for (const project of sorted) {
      const value = String(project.external_id);
      const item = el("li");
      item.dataset.search = `${project.name} ${project.key}`.toLowerCase();
      const label = el("label", { class: "pick" });
      const box = el("input", { type: "checkbox", value });
      box.checked = saved.has(value);
      box.addEventListener("change", counts);
      label.append(box, el("span", { class: "name" }, project.name));
      if (project.key && project.key !== project.name) label.append(el("span", { class: "key" }, project.key));
      item.append(label);
      list.append(item);
    }
    for (const control of [filter, $("#pick-all"), $("#pick-none")]) control.disabled = !total;
    if (!total) {
      const empty = $("#pick-empty");
      empty.textContent = `The account that connected ${source.name} cannot see any ${source.nouns}. Reconnect with an account that can, then come back here.`;
      empty.hidden = false;
    }
    counts();
  }

  function failed(message) {
    list.removeAttribute("aria-busy");
    list.replaceChildren();
    const empty = $("#pick-empty");
    empty.replaceChildren(el("span", {}, `${message} `));
    const retry = el("button", { class: "link-btn", type: "button" }, "Try again");
    retry.addEventListener("click", () => location.reload());
    empty.append(retry);
    empty.hidden = false;
  }

  function watch() {
    filter.addEventListener("input", applyFilter);
    $("#pick-all").addEventListener("click", () => {
      for (const item of list.children) if (!item.hidden) item.querySelector("input").checked = true;
      counts();
    });
    $("#pick-none").addEventListener("click", () => {
      for (const box of boxes()) box.checked = false;
      counts();
    });
    save.addEventListener("click", async () => {
      save.disabled = true;
      save.setAttribute("aria-busy", "true");
      save.textContent = "Saving…";
      try {
        await C.api("PUT", `/console/api/connections/${encodeURIComponent(id)}/containers`, { selected: chosen() });
        location.assign(`${detail}?connected=1`);
      } catch (error) {
        save.removeAttribute("aria-busy");
        save.textContent = "Save and sync";
        counts();
        if (error.status !== 401) C.banner(bannerBox, error.message, "error");
      }
    });
  }

  async function start() {
    let me;
    try { me = await C.boot(); } catch (error) { return; }
    $("#back-link").href = detail;
    $("#cancel-link").href = detail;
    if (!C.isAdmin(me)) {
      C.banner(bannerBox, "Only owners and admins can choose what a tool syncs.", "error");
      failed("Ask an owner or admin of your company.");
      return;
    }
    watch();
    try {
      const connections = (await C.api("GET", "/console/api/connections")) || [];
      const connection = connections.find((c) => c.id === id);
      if (!connection) throw new C.ApiError("This connection was not found. It may have been disconnected.", 404);
      source = C.sourceOf(connection);
      $("#back-link").textContent = `← ${source.name}`;
      const title = `Choose ${source.nouns}`;
      $("#page-title").textContent = title;
      document.title = `${title} · Company Brain`;
      list.setAttribute("aria-label", source.nouns[0].toUpperCase() + source.nouns.slice(1));
      filter.placeholder = "Filter by name or key";
      $("#pick-filter").previousElementSibling.textContent = `Filter ${source.nouns}`;
      const site = C.siteName(connection);
      $("#picker-lede").textContent = `Company Brain reads ${source.nouns === "projects" ? "issues" : "records"} from the ${source.nouns} you choose${site ? ` on ${site}` : ""}, read-only. You can change the choice later.`;
      saved = new Set((connection.containers || []).map((c) => String(c.id).split(":container:").pop()));
      render((await C.api("GET", `/console/api/connections/${encodeURIComponent(id)}/available`)) || []);
    } catch (error) {
      if (error.status !== 401) failed(error.message);
    }
  }

  start();
})();
