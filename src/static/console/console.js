// Company Brain console: what every console page shares. Plain JS, no build step, no inline code
// (the console runs under script-src 'self'). Every piece of text from the API is inserted with
// textContent; markup is built from constants only. The pages read only /console/api (spec:
// "Console JSON API"); a 401 anywhere goes to /signin, and a page that needs a company goes to
// /console/start while the signed-in workspace has none.
(() => {
  "use strict";

  const el = (tag, attrs = {}, text) => {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value === false || value === null || value === undefined) continue;
      node.setAttribute(key, value === true ? "" : value);
    }
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const $ = (selector, root = document) => root.querySelector(selector);

  // --- The tools a company can connect ---------------------------------------------------------

  const SOURCES = {
    jira: {
      name: "Jira", mark: "J", noun: "project", nouns: "projects",
      blurb: "Issues, their status, who opened them and who has them, from the projects you choose.",
      picks: true,
    },
    hubspot: {
      name: "HubSpot", mark: "H", noun: "portal", nouns: "portals",
      blurb: "Companies, deals and their owners, read-only, from your HubSpot portal.",
      picks: false,
    },
  };
  const sourceOf = (connection) => SOURCES[connection.source] || { name: connection.source, mark: (connection.source || "?")[0].toUpperCase(), noun: "container", nouns: "containers", blurb: "", picks: false };

  // A site address reads as its host ("quillfern.atlassian.net"); the disconnect confirm asks for it.
  const siteName = (connection) => (connection.site || connection.account || "").replace(/^https?:\/\//, "").replace(/\/+$/, "");

  // --- Numbers and times -------------------------------------------------------------------------

  const number = new Intl.NumberFormat("en-GB");
  const fmt = (n) => number.format(n || 0);
  const plural = (n, one, many) => `${fmt(n)} ${n === 1 ? one : many}`;
  const dateTime = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });

  function ago(iso) {
    const at = Date.parse(iso);
    if (!iso || Number.isNaN(at)) return "";
    const seconds = Math.max(0, Math.round((Date.now() - at) / 1000));
    if (seconds < 45) return "just now";
    const minutes = Math.round(seconds / 60);
    if (minutes < 60) return `${minutes} min ago`;
    const hours = Math.round(minutes / 60);
    if (hours < 36) return `${hours} h ago`;
    const days = Math.round(hours / 24);
    return `${days} ${days === 1 ? "day" : "days"} ago`;
  }
  const when = (iso) => (iso && !Number.isNaN(Date.parse(iso)) ? dateTime.format(new Date(iso)) : "");

  // --- Status: one reading of a connection, shared by the cards and the detail page --------------

  // tone: ok | busy | warn | error. ``syncing`` keeps the 10-second poll going.
  function statusOf(connection) {
    const containers = connection.containers || [];
    const done = containers.filter((c) => c.backfilled).length;
    if (connection.status === "removing") return { tone: "busy", text: "Disconnecting…", syncing: true };
    if (connection.status === "connecting") {
      const site = connection.source === "jira" && !connection.site;
      return { tone: "warn", text: site ? "Choose a site" : "Finish setup", syncing: false, setup: true };
    }
    if (connection.status === "error" || connection.last_error || containers.some((c) => c.last_error)) {
      return { tone: "error", text: "Needs attention", syncing: false };
    }
    if (!containers.length) return { tone: "warn", text: `Choose ${sourceOf(connection).nouns}`, syncing: false, setup: true };
    if (done < containers.length) {
      const text = containers.length > 1 ? `Backfilling ${done}/${containers.length}` : "Backfilling";
      return { tone: "busy", text, syncing: true, progress: done / containers.length };
    }
    const synced = ago(connection.last_sync_at);
    return { tone: "ok", text: synced ? `Up to date · ${synced}` : "Up to date", syncing: false };
  }

  function pill(status) {
    const node = el("span", { class: `pill status-pill ${status.tone}` });
    node.append(el("span", { class: `sdot ${status.tone}`, "aria-hidden": "true" }), status.text);
    return node;
  }

  function tile(source, large = false) {
    const meta = SOURCES[source] || { mark: (source || "?")[0].toUpperCase() };
    return el("span", { class: `tile ${source}${large ? " lg" : ""}`, "aria-hidden": "true" }, meta.mark);
  }

  // Where a connection's card leads: its next setup step, or its page.
  function linkFor(connection) {
    const id = encodeURIComponent(connection.id);
    if (connection.status === "connecting" && connection.source === "jira" && !connection.site) return `/console/connect/jira/site?id=${id}`;
    if (statusOf(connection).setup && sourceOf(connection).picks) return `/console/connectors/${id}/projects`;
    return `/console/connectors/${id}`;
  }

  // --- The API -----------------------------------------------------------------------------------

  class ApiError extends Error {
    constructor(message, status, code = "") {
      super(message);
      this.status = status;
      this.code = code;
    }
  }

  // The API answers a refusal with a fixed code, as {"error": code} or {"detail": code}. A page
  // shows a fixed sentence per code or status, never what a server or a vendor said; a page that
  // knows a code better (the invite code, say) reads error.code itself.
  const codeOf = (data) => (data && typeof (data.error || data.detail) === "string" ? data.error || data.detail : "");
  const SENTENCES = {
    bad_csrf_token: "This page is out of date. Reload it and try again.",
    admin_only: "Only owners and admins can do that.",
    owner_only: "Only the company's owner can do that.",
    slack_install_pending: "Finish adding Company Brain to Slack first.",
    no_org: "Set up your company first.",
  };
  function sentence(status, data) {
    const code = codeOf(data);
    if (SENTENCES[code]) return SENTENCES[code];
    if (status === 400) return "That was not accepted. Check it and try again.";
    if (status === 403) return SENTENCES.admin_only;
    if (status === 404) return "That is no longer here. It may have been disconnected.";
    if (status === 409) return "Someone changed this at the same time. Reload and try again.";
    if (status === 429) return "Too many requests from this browser. Try again in a minute.";
    return "Company Brain could not do that just now. Try again in a minute.";
  }

  let session = null;

  // A refusal that means "this session has no usable company yet" goes to the setup steps.
  const SETUP_CODES = new Set(["no_org", "slack_install_pending"]);

  async function api(method, path, body) {
    const headers = { Accept: "application/json" };
    if (method !== "GET") {
      const me = await whoami();
      headers["Content-Type"] = "application/json";
      headers["X-CSRF-Token"] = me.csrf;
    }
    let response;
    try {
      response = await fetch(path, { method, headers, credentials: "same-origin", body: body === undefined ? undefined : JSON.stringify(body) });
    } catch (error) {
      throw new ApiError("Company Brain could not be reached. Check your connection and try again.", 0);
    }
    if (response.status === 401) {
      location.assign("/signin?expired=1");
      throw new ApiError("Signed out.", 401);
    }
    let data = null;
    try { data = await response.json(); } catch (error) { data = null; }
    if (!response.ok) {
      const code = codeOf(data);
      if (response.status === 403 && SETUP_CODES.has(code) && location.pathname !== "/console/start") location.assign("/console/start");
      throw new ApiError(sentence(response.status, data), response.status, code);
    }
    return data;
  }

  // A response that carries the session again (POST /orgs rotates it, with a new CSRF token).
  const remember = (me) => { session = Promise.resolve(me); return me; };

  // GET /console/api/me, once per page.
  function whoami() {
    if (!session) {
      session = fetch("/console/api/me", { headers: { Accept: "application/json" }, credentials: "same-origin" }).then(async (response) => {
        if (response.status === 401) {
          location.assign("/signin?expired=1");
          throw new ApiError("Signed out.", 401);
        }
        if (!response.ok) throw new ApiError(sentence(response.status, null), response.status);
        return response.json();
      });
    }
    return session;
  }

  const isAdmin = (me) => me && (me.role === "owner" || me.role === "admin");

  // The header: the company's name, Sign out, and which page this is. A page that needs a company
  // (every page but /console/start) goes to the setup steps while the workspace has none.
  async function boot({ needsOrg = true } = {}) {
    const me = await whoami();
    // No company yet, or one whose Slack install has not confirmed its creator (role "pending").
    if (needsOrg && (!me.org || me.role === "pending")) {
      location.replace("/console/start");
      throw new ApiError("No company yet.", 0);
    }
    // With no company yet there is nothing to show on the other pages.
    if (!me.org || me.role === "pending") for (const link of document.querySelectorAll(".top nav a")) link.hidden = true;
    const org = $("#top-org");
    if (org && me.org) {
      org.textContent = me.org.name;
      org.title = me.person ? `${me.org.name} · signed in as ${me.person.name}` : me.org.name;
      org.hidden = false;
    }
    const out = $("#sign-out");
    if (out) {
      out.hidden = false;
      out.addEventListener("click", async () => {
        out.setAttribute("aria-busy", "true");
        try {
          await fetch("/auth/logout", { method: "POST", credentials: "same-origin", headers: { "X-CSRF-Token": me.csrf } });
        } finally {
          location.assign("/signin?signed_out=1");
        }
      });
    }
    return me;
  }

  // --- Small pieces of UI --------------------------------------------------------------------

  function banner(node, text, tone = "") {
    node.className = `banner${tone ? ` ${tone}` : ""}`;
    node.replaceChildren(el("span", { class: "grow" }, text));
    node.hidden = !text;
    return node;
  }

  // A page reads its one-off notice (?connected=, ?error=) once, then drops it from the address,
  // so a reload does not say it again.
  function takeParam(name) {
    const params = new URLSearchParams(location.search);
    const value = params.get(name);
    if (value === null) return null;
    params.delete(name);
    const rest = params.toString();
    history.replaceState(history.state, "", `${location.pathname}${rest ? `?${rest}` : ""}${location.hash}`);
    return value;
  }

  // What the OAuth callbacks (package D) send back here, as fixed sentences.
  const NOTICES = {
    oauth_denied: ["Access was not granted, so nothing was connected.", "error"],
    oauth_state: ["That connection attempt expired. Start it again.", "error"],
    account_claimed: ["That Jira site or HubSpot portal is already connected to another company in Company Brain.", "error"],
    oauth_failed: ["The connection did not complete. Try again in a minute.", "error"],
  };
  const notice = (code) => NOTICES[code] || NOTICES.oauth_failed;

  // One click: ask the API where the tool's consent screen is, then go there. A same-site path
  // (the fixtures) or an https address only; anything else is refused.
  async function connect(source, button, onError) {
    const label = button.textContent;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.textContent = `Opening ${(SOURCES[source] || { name: source }).name}…`;
    try {
      const data = await api("POST", `/console/api/connectors/${encodeURIComponent(source)}/connect`);
      const target = data && typeof data.authorize_url === "string" ? data.authorize_url : "";
      if (!/^https:\/\//.test(target) && !/^\/(?!\/)/.test(target)) throw new ApiError(sentence(500), 500);
      location.assign(target);
    } catch (error) {
      button.disabled = false;
      button.removeAttribute("aria-busy");
      button.textContent = label;
      if (error.status !== 401) onError(error.message);
    }
  }

  // Every 10 seconds while ``step`` says there is more to watch, and never while the tab is hidden.
  // ``step`` returns true to keep going. The note names when it last checked.
  function poll(step, note, every = 10000) {
    let timer = null;
    let stopped = false;
    const say = (text) => { if (note) note.textContent = text; };
    async function tick() {
      timer = null;
      if (stopped) return;
      if (document.hidden) return;
      let more = false;
      try { more = await step(); } catch (error) { more = error.status !== 401; }
      if (more && !stopped) {
        say("Syncing · checking every 10 seconds");
        timer = setTimeout(tick, every);
      } else {
        say("");
      }
    }
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden && !timer && !stopped) tick();
    });
    return {
      start(active) {
        if (timer) clearTimeout(timer);
        timer = null;
        if (active) { say("Syncing · checking every 10 seconds"); timer = setTimeout(tick, every); } else say("");
      },
      stop() { stopped = true; if (timer) clearTimeout(timer); say(""); },
    };
  }

  window.Console = { el, $, SOURCES, sourceOf, siteName, fmt, plural, ago, when, statusOf, pill, tile, linkFor, api, whoami, remember, isAdmin, boot, banner, takeParam, notice, connect, poll, ApiError };
})();
