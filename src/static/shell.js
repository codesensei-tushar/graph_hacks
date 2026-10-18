// The one header (data-shell, data-here): adds the nav from NAV, Sign in, the console's org and Sign out.
(() => {
  "use strict";

  const NAV = {
    site: [["ask", "Ask", "/#ask"], ["graph", "Graph", "/#graph"], ["explore", "Explore", "/explore"], ["proof", "Proof", "/#proof"], ["teams", "Teams", "/#teams"], ["how", "How it works", "/#how"], ["builders", "Builders", "/#builders"]],
    console: [["connections", "Connections", "/console"], ["ask", "Ask", "/console/ask"]],
  };
  const el = (tag, attrs, text) => {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    if (text) node.textContent = text;
    return node;
  };

  const top = document.querySelector("header.top[data-shell]");
  const actions = top && top.querySelector(".top-actions");
  if (actions) {
    const set = top.dataset.shell === "console" ? "console" : "site";
    const here = top.dataset.here === "team" ? "teams" : top.dataset.here;
    const nav = el("nav", { "aria-label": set === "console" ? "Console" : "Sections" });
    for (const [key, label, href] of NAV[set]) {
      const link = el("a", { href, "data-nav": key }, label);
      if (key === here) link.setAttribute("aria-current", "page");
      nav.append(link);
    }
    actions.before(nav);
    const lead = [];
    if (set === "console") {
      lead.push(el("span", { class: "top-org", id: "top-org", hidden: "" }), el("button", { class: "top-signin", id: "sign-out", type: "button", hidden: "" }, "Sign out"));
    } else if ((document.querySelector('meta[name="brain-console"]') || {}).content === "on" && here !== "signin") {
      lead.push(el("a", { class: "top-signin", href: "/signin" }, "Sign in"));
    }
    actions.prepend(...lead);
  }

  const root = document.documentElement;
  const button = document.getElementById("theme-toggle");
  if (!button) return;
  const dark = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  const current = () => root.dataset.theme || (dark && dark.matches ? "dark" : "light");
  const label = () => button.setAttribute("aria-label", `Switch to the ${current() === "dark" ? "light" : "dark"} theme`);
  button.addEventListener("click", () => {
    const next = current() === "dark" ? "light" : "dark";
    root.dataset.theme = next;
    try { localStorage.setItem("brain-theme", next); } catch (error) { /* private window */ }
    label();
  });
  if (dark && dark.addEventListener) dark.addEventListener("change", label);
  label();
})();
