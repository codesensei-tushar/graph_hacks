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
