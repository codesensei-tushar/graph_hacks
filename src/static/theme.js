// Company Brain: the stored theme, stamped before the first paint. Loaded in <head>, before the
// stylesheets, so a page never flashes the other theme; a file and not an inline script, so a page
// served with a strict CSP (script-src 'self') runs it too. shell.js keeps the toggle.
try { var t = localStorage.getItem("brain-theme"); if (t === "light" || t === "dark") document.documentElement.dataset.theme = t; } catch (e) {}
