// /console/connectors/<id>: one connection. Sync now, Reconnect, choose projects, and Disconnect
// after typing the site's name. It reads GET /console/api/connections (the spec has no single-
// connection read) and checks again every 10 seconds while the connection is syncing.
(() => {
  "use strict";

  const C = window.Console;
  const { el, $ } = C;
  const id = decodeURIComponent(location.pathname.split("/")[3] || "");
  const bannerBox = $("#banner");
  const syncButton = $("#sync-now");
  let me = null;
  let current = null;
  let watcher = null;
  let syncAsked = 0; // checks left to watch after "Sync now"
  let leaving = false;

  // --- Drawing -----------------------------------------------------------------------------

  function meta(connection, source) {
    const list = $("#meta");
    list.replaceChildren();
    const row = (term, value) => {
      if (value === "" || value === null || value === undefined) return;
      const item = el("div");
      item.append(el("dt", {}, term), el("dd", {}, String(value)));
      list.append(item);
    };
    row("Items", C.fmt(connection.items));
    row(source.nouns[0].toUpperCase() + source.nouns.slice(1), C.fmt((connection.containers || []).length));
    row("Last sync", connection.last_sync_at ? `${C.ago(connection.last_sync_at)}` : "Not yet");
    if (!connection.site) row("Account", connection.account);
    row("Connected by", connection.created_by);
    row("Connected", C.when(connection.created_at));
    row("Access", connection.auth === "oauth" ? "OAuth, read-only" : connection.auth === "token" ? "API token, read-only" : "Server settings");
  }

  function containerRows(connection, source) {
    const body = $("#containers");
    body.replaceChildren();
    const containers = connection.containers || [];
    // The column shows once the API says who can read each container (its readers field).
    const visibility = containers.some((c) => "readers" in c);
    $("#vis-col").hidden = !visibility;
    $("#containers-title").textContent = source.nouns[0].toUpperCase() + source.nouns.slice(1);
    $("#containers-col").textContent = source.noun[0].toUpperCase() + source.noun.slice(1);
    const done = containers.filter((c) => c.backfilled).length;
    $("#containers-sub").textContent = containers.length
      ? `${C.fmt(done)} of ${C.plural(containers.length, source.noun, source.nouns)} fully read. New and changed items are picked up on every sync.`
      : `No ${source.nouns} chosen yet, so nothing is being read.`;
    if (!containers.length) {
      const row = el("tr");
      row.append(el("td", { colspan: "5", class: "dim" }, `Choose ${source.nouns} to start syncing.`));
      body.append(row);
      return;
    }
    // Errors first, then the ones still backfilling, then the rest, each by name.
    const rank = (c) => (c.last_error ? 0 : c.backfilled ? 2 : 1);
    for (const container of [...containers].sort((a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name))) {
      const row = el("tr");
      row.append(el("td", {}, container.name));
      const state = el("td", { class: "state" });
      const tone = container.last_error ? "error" : container.backfilled ? "ok" : "busy";
      state.append(el("span", { class: `sdot ${tone}`, "aria-hidden": "true" }), container.last_error ? "Failed" : container.backfilled ? "Up to date" : "Backfilling");
      // A container keeps at most its newest items (sync.MAX_ITEMS_PER_CONTAINER); say how many of how many.
      if (container.capped) {
        const of = Number.isFinite(container.available) ? ` of ${C.fmt(container.available)}` : "";
        state.append(el("span", { class: "dim" }, ` · keeping the newest ${C.fmt(container.items)}${of}`));
      }
      if (container.last_error) state.append(el("span", { class: "err" }, container.last_error));
      row.append(state);
      // Who can read what came from it: everyone in the company, or the N people the tool lets read it.
      if (visibility) {
        const readers = container.readers;
        row.append(el("td", { class: readers === null || readers === undefined ? "" : "restricted" }, readers === null || readers === undefined ? "Company-wide" : `Restricted: ${C.plural(readers, "reader", "readers")}`));
      }
      row.append(el("td", { class: "num" }, C.fmt(container.items)));
      row.append(el("td", { class: "dim when" }, container.last_fetch_at ? C.ago(container.last_fetch_at) : "Not yet"));
      body.append(row);
    }
  }

  function draw(connection) {
    current = connection;
    const source = C.sourceOf(connection);
    const status = C.statusOf(connection);
    document.title = `${source.name} · Company Brain`;
    $("#page-title").textContent = source.name;
    $("#detail-tile").replaceChildren(C.tile(connection.source, true));
    $("#detail-pill").replaceChildren(C.pill(status));
    const site = C.siteName(connection);
    $("#detail-site").textContent = site ? `Reading ${site}, read-only.` : "No site chosen yet.";

    const admin = C.isAdmin(me);
    const removing = connection.status === "removing";
    $("#detail-actions").hidden = !admin || removing;
    $("#member-note").hidden = admin;
    $("#danger").hidden = !admin || removing;
    const choose = $("#choose-projects");
    choose.hidden = !source.picks;
    choose.href = `/console/connectors/${encodeURIComponent(connection.id)}/projects`;
    choose.textContent = `Choose ${source.nouns}`;
    $("#disconnect-open").textContent = `Disconnect ${source.name}`;

    if (status.tone === "error") {
      const why = connection.last_error ? ` ${connection.last_error}` : "";
      C.banner(bannerBox, `The last sync failed.${why} Sync again, or reconnect if access was revoked.`, "error");
      bannerBox.dataset.failure = "";
    } else if ("failure" in bannerBox.dataset) {
      // The failure is fixed: its banner goes.
      delete bannerBox.dataset.failure;
      C.banner(bannerBox, "");
    }
    if (removing) {
      C.banner(bannerBox, "Disconnecting. Everything this tool brought into the graph is being deleted.", "");
    }

    meta(connection, source);
    containerRows(connection, source);
    $("#detail-body").hidden = false;
    return status.syncing;
  }

  function gone() {
    if (leaving) return;
    leaving = true;
    location.replace(current && current.status === "removing" ? "/console?removed=1" : "/console");
  }

  async function read() {
    const connections = (await C.api("GET", "/console/api/connections")) || [];
    const connection = connections.find((c) => c.id === id);
    if (!connection) {
      if (current) { gone(); return false; }
      throw new C.ApiError("This connection was not found. It may have been disconnected.", 404);
    }
    const syncing = draw(connection);
    if (syncAsked > 0) syncAsked -= 1;
    return syncing || syncAsked > 0;
  }

  // --- Actions -------------------------------------------------------------------------------

  async function busy(button, label, work) {
    const before = button.textContent;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.textContent = label;
    try { await work(); } finally {
      button.disabled = false;
      button.removeAttribute("aria-busy");
      button.textContent = before;
    }
  }

  function watchActions() {
    syncButton.addEventListener("click", () => busy(syncButton, "Starting…", async () => {
      try {
        await C.api("POST", `/console/api/connections/${encodeURIComponent(id)}/sync`);
        syncAsked = 3;
        watcher.start(true);
        await read();
        C.banner(bannerBox, "Sync started. This page updates as it runs.", "ok");
      } catch (error) {
        if (error.status !== 401) C.banner(bannerBox, error.message, "error");
      }
    }));
    const reconnect = $("#reconnect");
    reconnect.addEventListener("click", () => C.connect(current.source, reconnect, (message) => C.banner(bannerBox, message, "error")));
    watchDisconnect();
  }

  // Disconnect asks for the site's name, typed, before the button arms. Escape and Cancel close it.
  function watchDisconnect() {
    const dialog = $("#confirm");
    const input = $("#confirm-input");
    const go = $("#confirm-go");
    const error = $("#confirm-error");
    const expected = () => C.siteName(current) || C.sourceOf(current).name;
    const matches = () => input.value.trim().toLowerCase() === expected().toLowerCase();

    $("#disconnect-open").addEventListener("click", () => {
      const source = C.sourceOf(current);
      $("#confirm-title").textContent = `Disconnect ${source.name}?`;
      $("#confirm-text").textContent = `Company Brain stops reading ${expected()} and deletes the ${C.fmt(current.items)} items it brought into your graph, with its access tokens. This cannot be undone.`;
      $("#confirm-name").textContent = expected();
      input.value = "";
      error.textContent = "";
      go.disabled = true;
      if (typeof dialog.showModal === "function") dialog.showModal(); else dialog.setAttribute("open", "");
      input.focus();
    });
    input.addEventListener("input", () => { go.disabled = !matches(); error.textContent = ""; });
    $("#confirm-cancel").addEventListener("click", () => dialog.close());
    $("#confirm-form").addEventListener("submit", (event) => {
      event.preventDefault();
      if (!matches()) { error.textContent = "That does not match. Type it exactly as shown."; return; }
      busy(go, "Disconnecting…", async () => {
        try {
          await C.api("DELETE", `/console/api/connections/${encodeURIComponent(id)}`);
          dialog.close();
          current.status = "removing";
          draw(current);
          watcher.start(true);
        } catch (failure) {
          if (failure.status !== 401) error.textContent = failure.message;
        }
      });
      go.disabled = !matches();
    });
  }

  // --- Start ---------------------------------------------------------------------------------

  function failed(message, retry) {
    $("#page-title").textContent = "Connection";
    const box = el("div", { class: "empty" });
    box.append(el("h2", {}, retry ? "This connection could not be loaded" : "Not found"));
    box.append(el("p", {}, message));
    const actions = el("div", { class: "actions" });
    if (retry) {
      const again = el("button", { class: "btn btn-ghost", type: "button" }, "Try again");
      again.addEventListener("click", () => location.reload());
      actions.append(again);
    }
    actions.append(el("a", { class: retry ? "btn btn-ghost" : "btn", href: "/console" }, "Back to Connections"));
    box.append(actions);
    bannerBox.replaceWith(box);
  }

  async function start() {
    try {
      me = await C.boot();
    } catch (error) {
      return;
    }
    if (C.takeParam("connected")) C.banner(bannerBox, "Connected. The first sync has started; this page updates as it runs.", "ok");
    const error = C.takeParam("error");
    if (error) C.banner(bannerBox, ...C.notice(error));
    watcher = C.poll(read, $("#poll-note"));
    watchActions();
    try {
      watcher.start(await read());
    } catch (failure) {
      if (failure.status !== 401) failed(failure.message, failure.status !== 404);
    }
  }

  start();
})();
