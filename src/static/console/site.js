// /console/connect/jira/site?id=<connection id>: after Jira's consent screen, when the account sees
// more than one site. Lists GET .../sites, saves the choice with POST .../site {cloud_id}, then
// goes on to choose projects. Without ?id it takes the Jira connection still waiting for a site.
(() => {
  "use strict";

  const C = window.Console;
  const { el, $ } = C;
  const bannerBox = $("#banner");
  const list = $("#sites");
  const go = $("#site-go");
  let id = new URLSearchParams(location.search).get("id") || "";

  function render(sites) {
    list.removeAttribute("aria-busy");
    list.replaceChildren();
    sites.forEach((site, index) => {
      const item = el("li");
      const label = el("label", { class: "choice" });
      const radio = el("input", { type: "radio", name: "site", value: site.cloud_id });
      radio.addEventListener("change", () => { go.disabled = false; });
      if (sites.length === 1 && index === 0) { radio.checked = true; go.disabled = false; }
      const text = el("span", { class: "txt" });
      text.append(el("b", {}, site.name), el("span", {}, (site.url || "").replace(/^https?:\/\//, "")));
      label.append(radio, C.tile("jira"), text);
      item.append(label);
      list.append(item);
    });
    if (!sites.length) {
      const empty = el("li", { class: "empty" });
      empty.append(el("h2", {}, "No Jira site found"), el("p", {}, "The account you connected cannot see a Jira Cloud site. Connect again with an account that can."));
      list.append(empty);
    }
  }

  async function start() {
    let me;
    try { me = await C.boot(); } catch (error) { return; }
    if (!C.isAdmin(me)) {
      list.removeAttribute("aria-busy");
      list.replaceChildren();
      C.banner(bannerBox, "Only owners and admins can connect a tool.", "error");
      return;
    }
    try {
      if (!id) {
        const waiting = ((await C.api("GET", "/console/api/connections")) || []).find((c) => c.source === "jira" && c.status === "connecting" && !c.site);
        if (!waiting) { location.replace("/console"); return; }
        id = waiting.id;
      }
      render((await C.api("GET", `/console/api/connections/${encodeURIComponent(id)}/sites`)) || []);
    } catch (error) {
      list.removeAttribute("aria-busy");
      list.replaceChildren();
      if (error.status !== 401) C.banner(bannerBox, error.message, "error");
      return;
    }
    $("#site-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const picked = list.querySelector("input[name=site]:checked");
      if (!picked) return;
      go.disabled = true;
      go.setAttribute("aria-busy", "true");
      go.textContent = "Saving…";
      try {
        await C.api("POST", `/console/api/connections/${encodeURIComponent(id)}/site`, { cloud_id: picked.value });
        location.assign(`/console/connectors/${encodeURIComponent(id)}/projects`);
      } catch (error) {
        go.disabled = false;
        go.removeAttribute("aria-busy");
        go.textContent = "Continue";
        if (error.status !== 401) C.banner(bannerBox, error.message, "error");
      }
    });
  }

  start();
})();
