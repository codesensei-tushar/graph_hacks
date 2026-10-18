// Company Brain: what every page shares. The theme toggle follows the OS until it is clicked,
// then remembers the choice; storage can throw in a private window, so the page renders without it.
(() => {
  "use strict";

  const root = document.documentElement;
  const button = document.getElementById("theme-toggle");
  if (!button) return;
  const dark = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  const current = () => root.dataset.theme || (dark && dark.matches ? "dark" : "light");

  function label() {
    const next = current() === "dark" ? "light" : "dark";
    button.setAttribute("aria-label", `Switch to the ${next} theme`);
  }

  button.addEventListener("click", () => {
    const next = current() === "dark" ? "light" : "dark";
    root.dataset.theme = next;
    try { localStorage.setItem("brain-theme", next); } catch (error) { /* a private window keeps it for this page only */ }
    label();
  });
  if (dark && dark.addEventListener) dark.addEventListener("change", label);
  label();
})();

// Phones: the section links fold behind the Menu button render() adds; a link, Escape or a tap outside closes it.
(() => {
  const top = document.querySelector(".top");
  const toggle = document.getElementById("nav-toggle");
  if (!top || !toggle) return;
  const set = (open) => { top.toggleAttribute("data-nav-open", open); toggle.setAttribute("aria-expanded", String(open)); };
  toggle.addEventListener("click", () => set(!top.hasAttribute("data-nav-open")));
  top.addEventListener("click", (event) => { if (event.target.closest("nav a")) set(false); });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") set(false); });
  document.addEventListener("click", (event) => { if (!top.contains(event.target)) set(false); });
})();
