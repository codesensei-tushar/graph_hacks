// The operator's GitHub App page. The admin token lives in this closure only; every call sends it as
// a bearer token to this server. Registration posts GitHub's manifest form (the page's CSP allows a
// form to github.com and nowhere else). Text from the server is inserted with textContent.
(() => {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const el = (tag, attrs = {}, text) => {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    if (text !== undefined) node.textContent = text;
    return node;
  };
  let token = "";

  function banner(text, tone = "") {
    const box = $("#banner");
    box.className = `banner${tone ? ` ${tone}` : ""}`;
    box.textContent = text;
    box.hidden = !text;
  }

  async function call(method, path, body) {
    const response = await fetch(path, {
      method,
      headers: { Accept: "application/json", Authorization: `Bearer ${token}`, ...(body ? { "Content-Type": "application/json" } : {}) },
      body: body ? JSON.stringify(body) : undefined,
      credentials: "omit",
    });
    let data = null;
    try { data = await response.json(); } catch (error) { data = null; }
    if (!response.ok) {
      const message = (data && typeof data.message === "string" && data.message) || (response.status === 403 ? "That token was not accepted." : "The server could not do that. Try again.");
      throw new Error(message);
    }
    return data;
  }

  function fact(list, name, value) {
    list.append(el("dt", {}, name), el("dd", {}, value || "—"));
  }

  async function refresh() {
    const data = await call("GET", "/ops/github/app/status");
    const list = $("#facts");
    list.replaceChildren();
    if (data.configured) {
      const app = data.app;
      fact(list, "Source", app.source === "env" ? "Environment variables (they win)" : "Registered from this page");
      fact(list, "App", app.name || app.slug || `#${app.app_id}`);
      fact(list, "App id", app.app_id);
      fact(list, "Owner", app.owner);
      fact(list, "User sign-in", app.user_sign_in ? "Ready" : "Missing client id or secret");
      if (app.install_url) {
        const dd = el("dd");
        dd.append(el("a", { href: app.install_url }, app.install_url));
        list.append(el("dt", {}, "Install link"), dd);
      }
    } else {
      fact(list, "App", "None yet");
    }
    $("#status").hidden = false;
    $("#register").hidden = Boolean(data.configured);
    // Other sections of the page (installations) start from here, with the same token.
    document.dispatchEvent(new CustomEvent("githubapp:ready", { detail: { call, banner, data } }));
  }

  $("#auth-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    token = $("#token").value.trim();
    banner("");
    try {
      await refresh();
      $("#token").value = "";
    } catch (error) {
      banner(error.message, "error");
    }
  });

  $("#register-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    banner("");
    const go = $("#register-go");
    go.disabled = true;
    try {
      const data = await call("POST", "/ops/github/app/manifest", { name: $("#name").value, owner_org: $("#owner").value });
      if (!/^https:\/\/github\.com\//.test(data.post_url)) throw new Error("The server answered an unexpected address.");
      const form = el("form", { method: "post", action: data.post_url });
      const field = el("input", { type: "hidden", name: "manifest" });
      field.value = data.manifest;
      form.append(field);
      document.body.append(form);
      form.submit();
    } catch (error) {
      go.disabled = false;
      banner(error.message, "error");
    }
  });
})();
