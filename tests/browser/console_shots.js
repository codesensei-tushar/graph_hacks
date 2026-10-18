// Screenshots of console pages. Not part of CI. Usage, with a fixture server running:
//   node tests/browser/console_shots.js <base> <outdir> "name|/path[|dialog|filter|top]" ...
// e.g. "console|/console/_fixture/default?next=/console".
// Each page in light and dark, at 1440x900 and 390x844, full page. Fails on console errors
// (CSP violations included) and on horizontal page scroll at phone width.
const { chromium } = require("playwright-core");
const fs = require("fs");
const [base, outdir, ...pages] = process.argv.slice(2);
fs.mkdirSync(outdir, { recursive: true });
const SIZES = { desktop: { width: 1440, height: 900 }, phone: { width: 390, height: 844 } };
(async () => {
  const browser = await chromium.launch({ executablePath: process.env.CHROME });
  const problems = [];
  for (const spec of pages) {
    const [name, path, action] = spec.split("|");
    for (const theme of ["light", "dark"]) {
      for (const [size, viewport] of Object.entries(SIZES)) {
        const ctx = await browser.newContext({ viewport, colorScheme: theme, reducedMotion: "reduce", deviceScaleFactor: 1, bypassCSP: true });
        const page = await ctx.newPage();
        page.on("console", (m) => { if (m.type() === "error") problems.push(`${name} ${theme} ${size}: ${m.text()}`); });
        page.on("pageerror", (e) => problems.push(`${name} ${theme} ${size}: ${e}`));
        await page.goto(base + path, { waitUntil: "networkidle" });
        await page.waitForTimeout(700);
        if (action === "dialog") {
          await page.click("#disconnect-open");
          await page.fill("#confirm-input", "quillfern.atlass");
          await page.waitForTimeout(200);
        } else if (action === "preview") {
          await page.setInputFiles("#team-upload", require("path").join(__dirname, "..", "..", "examples", "teams", "quillfern-sales.toml"));
          await page.waitForSelector("#preview.ok");
        } else if (action === "problems") {
          await page.fill("#team-file", "[team]\nname = \"Support\"\n\n[[members]]\nname = \"Alex Kim\"\n\n[[runs]]\nname = \"Ticket triage\"\nkind = \"workflow\"\nowner = \"Alex\"\n");
          await page.click("#check");
          await page.waitForSelector("#preview.error");
        } else if (action === "filter") {
          await page.fill("#pick-filter", "pay");
          await page.waitForTimeout(200);
        }
        await page.evaluate(() => window.scrollTo(0, 0));
        await page.waitForTimeout(100);
        const wide = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
        if (wide > 0) problems.push(`${name} ${theme} ${size}: horizontal scroll ${wide}px`);
        const file = `${outdir}/${name}-${theme}-${size}.png`;
        await page.screenshot({ path: file, fullPage: !["dialog", "top"].includes(action) });
        console.log(file);
        await ctx.close();
      }
    }
  }
  fs.writeFileSync(`${outdir}/problems.txt`, problems.join("\n"));
  if (problems.length) console.log("PROBLEMS:\n" + problems.join("\n"));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
