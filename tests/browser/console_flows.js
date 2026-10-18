// The console's flows in a real browser, against the fake API. Not part of CI (no browser there).
// Run a server with CONSOLE_ENABLED=1 CONSOLE_FIXTURES=1, then, with playwright-core installed
// anywhere on NODE_PATH and CHROME pointing at a Chromium:
//   node tests/browser/console_flows.js http://localhost:8000 <dir for screenshots>
// Fails loudly when a step does not land where it should. The harness bypasses the page's CSP
// only for its own probes (waitForFunction); the pages themselves run under it.
const { chromium } = require("playwright-core");
const assert = require("assert");
const [base, shots] = process.argv.slice(2);

(async () => {
  const browser = await chromium.launch({ executablePath: process.env.CHROME });
  const errors = [];
  const fresh = async (scenario, next, opts = {}) => {
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 }, reducedMotion: "reduce", colorScheme: opts.theme || "light", bypassCSP: true });
    const page = await ctx.newPage();
    page.on("pageerror", (e) => errors.push(`${scenario}: ${e}`));
    page.on("console", (m) => { if (m.type() === "error" && !/503|401|400|403|409/.test(m.text())) errors.push(`${scenario}: ${m.text()}`); });
    await page.goto(`${base}/console/_fixture/${scenario}?next=${encodeURIComponent(next)}`);
    return { ctx, page };
  };
  const step = (name) => console.log(`ok  ${name}`);

  // 1. Start: a wrong invite code is refused under its field; the right one opens step 2.
  {
    const { ctx, page } = await fresh("new", "/console/start");
    await page.waitForSelector("#step-org[data-state=current]");
    await page.fill("#org-name-input", "Acme Robotics");
    await page.fill("#invite-input", "WRONG");
    await page.click("#org-submit");
    await page.waitForSelector("#invite-error:not(:empty)");
    assert.match(await page.textContent("#invite-error"), /not valid/);
    step("start: bad invite code shows under its field");
    await page.fill("#invite-input", "beta-quill-2026");
    await page.click("#org-submit");
    await page.waitForSelector("#step-slack[data-state=current]");
    assert.equal(await page.getAttribute("#step-org", "data-state"), "done");
    assert.equal(await page.textContent("#top-org"), "Acme Robotics");
    await page.screenshot({ path: `${shots}/start-step2-light-desktop.png`, fullPage: true });
    step("start: company created (pending), step 2 open");
    await page.click("#step-slack .btn-slack");
    await page.waitForSelector("#step-tool[data-state=current]");
    assert.equal(await page.getAttribute("#step-slack", "data-state"), "done");
    await page.click("#step-tool .btn >> text=Connect HubSpot");
    await page.waitForURL(/\/console\/connectors\/hubspot/);
    await page.waitForSelector("#banner.ok");
    step("start: Slack install confirms the owner; Connect HubSpot lands on its page");
    await ctx.close();
  }

  // 2. Connect Jira from an empty console: site, projects, save, detail with backfill.
  {
    const { ctx, page } = await fresh("empty", "/console");
    await page.waitForSelector(".empty .btn");
    await page.click(".empty .btn >> text=Connect Jira");
    await page.waitForURL(/\/console\/connect\/jira\/site\?id=/);
    await page.waitForSelector("input[name=site]");
    assert.equal(await page.isDisabled("#site-go"), true);
    await page.check("input[name=site] >> nth=0");
    await page.click("#site-go");
    await page.waitForURL(/\/projects$/);
    await page.waitForSelector("#pick-list input[type=checkbox]");
    assert.equal(await page.isDisabled("#save"), true);
    await page.fill("#pick-filter", "pay");
    assert.equal(await page.locator("#pick-list li:not([hidden])").count(), 1);
    await page.click("#pick-all");
    await page.fill("#pick-filter", "");
    await page.check("#pick-list li:has-text('Billing') input");
    assert.match(await page.textContent("#pick-count"), /2 of 40 selected/);
    await page.click("#save");
    await page.waitForURL(/\/console\/connectors\/jira%3A[^/]+$|\/console\/connectors\/jira:[^/]+$/);
    await page.waitForSelector("#containers tr");
    assert.match(await page.textContent("#banner"), /Connected/);
    assert.equal(await page.locator("#containers tr").count(), 2);
    step("connect jira: site, filter, select, save, detail");
    await ctx.close();
  }

  // 3. Detail: polling moves the backfill on; the pill ends Up to date.
  {
    const { ctx, page } = await fresh("default", "/console/connectors/jira:01JB7Q2K9W");
    await page.waitForSelector("#detail-pill .status-pill");
    const first = await page.textContent("#detail-pill");
    assert.match(first, /Backfilling \d+\/35/);
    assert.match(await page.textContent("#poll-note"), /every 10 seconds/);
    await page.waitForFunction((before) => document.querySelector("#detail-pill").textContent !== before, first, { timeout: 25000 });
    step(`detail: poll moved "${first}" to "${await page.textContent("#detail-pill")}"`);
    await ctx.close();
  }

  // 4. Needs attention, Sync now clears it.
  {
    const { ctx, page } = await fresh("attention", "/console/connectors/hubspot:01JB7Q8M3R");
    await page.waitForSelector("#banner.error");
    assert.equal(await page.isHidden("#choose-projects"), true);
    await page.click("#sync-now");
    await page.waitForFunction(() => /Up to date/.test(document.querySelector("#detail-pill").textContent));
    assert.match(await page.textContent("#banner"), /Sync started/);
    step("attention: sync now clears the failure");
    await ctx.close();
  }

  // 5. Disconnect: armed only by the exact site name; ends on the dashboard with a notice.
  {
    const { ctx, page } = await fresh("default", "/console/connectors/hubspot:01JB7Q8M3R");
    await page.waitForSelector("#disconnect-open:visible");
    await page.click("#disconnect-open");
    await page.fill("#confirm-input", "quillfern");
    assert.equal(await page.isDisabled("#confirm-go"), true);
    await page.fill("#confirm-input", "Quillfern.com");
    assert.equal(await page.isDisabled("#confirm-go"), false);
    await page.click("#confirm-go");
    await page.waitForSelector("#detail-pill >> text=Disconnecting");
    step("disconnect: typed name arms it; status Disconnecting");
    await page.waitForURL(/\/console(\?removed=1)?$/, { timeout: 40000 });
    await page.waitForSelector("#banner.ok");
    assert.match(await page.textContent("#banner"), /Disconnected/);
    assert.equal(await page.locator("a.conn-card").count(), 1);
    assert.match(await page.textContent(".slack-card-c"), /App installed/);
    step("disconnect: back on Connections with one card left");
    await ctx.close();
  }

  // 6. Member: no Connect, no Sync, no Disconnect.
  {
    const { ctx, page } = await fresh("member", "/console/connectors/jira:01JB7Q2K9W");
    await page.waitForSelector("#member-note:visible");
    assert.equal(await page.isHidden("#detail-actions"), true);
    assert.equal(await page.isHidden("#danger"), true);
    step("member: actions hidden");
    await ctx.close();
  }

  // 7. Signed out: every console page goes to /signin.
  {
    const { ctx, page } = await fresh("signedout", "/console");
    await page.waitForURL(/\/signin\?expired=1$/);
    step("signed out: redirected to sign in");
    await ctx.close();
  }

  // 8. No company: /console goes to the setup steps.
  {
    const { ctx, page } = await fresh("new", "/console");
    await page.waitForURL(/\/console\/start$/);
    step("no company: /console goes to /console/start");
    await ctx.close();
  }

  // 9. Ask: a question draws the shared answer; a follow-up asks again and becomes the address.
  {
    const { ctx, page } = await fresh("member", "/console/ask");
    await page.waitForSelector("#ask-mode-text >> text=keywords");
    await page.click("#starters [data-start='who owns ']");
    assert.equal(await page.inputValue("#q"), "who owns ");
    await page.type("#q", "billing");
    await page.press("#q", "Enter");
    await page.waitForSelector("#out .footer-line");
    assert.match(page.url(), /\/console\/ask\?q=who%20owns%20billing$/);
    assert.match(await page.textContent("#out .footer-line"), /who_owns/);
    assert.ok(await page.locator("#out .canvas svg").count());
    const next = page.locator("#out .next-list button").first();
    const asked = await next.textContent();
    await next.click();
    await page.waitForFunction((q) => document.querySelector("#answer-q").textContent === q && document.querySelector("#out .footer-line"), asked);
    assert.match(decodeURIComponent(page.url()), new RegExp(asked.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
    await page.goBack();
    await page.waitForFunction(() => document.querySelector("#answer-q").textContent === "who owns billing");
    step(`ask: answer, Ask next ("${asked}"), Back`);
    await ctx.close();
  }

  // 10. Teams: the Connections nudge, a refused file's problems, then upload, check and import.
  {
    const { ctx, page } = await fresh("empty", "/console");
    await page.waitForSelector("#teams-nudge:visible");
    await page.click("#teams-nudge-go");
    await page.waitForURL(/\/console\/teams$/);
    await page.waitForSelector("#teams .empty");
    await page.fill("#team-file", "[[runs]]\nname = \"Gizmo\"\nkind = \"gizmo\"\n");
    await page.click("#check");
    await page.waitForSelector("#preview.error .problems li");
    assert.ok((await page.locator("#preview .problems li").count()) >= 2);
    step("teams: a refused file lists its problems, nothing written");
    await page.setInputFiles("#team-upload", require("path").join(__dirname, "..", "..", "examples", "teams", "quillfern-sales.toml"));
    await page.waitForSelector("#preview.ok #confirm-import");
    assert.match(await page.textContent("#preview"), /Sales: 3 people/);
    await page.click("#confirm-import");
    await page.waitForSelector("#team-sales.just-added");
    assert.match(await page.textContent("#banner"), /Imported Sales/);
    assert.equal(await page.inputValue("#team-file"), "");
    step("teams: upload, check, import; the new team is on the list");
    await ctx.close();
  }

  // 11. People: the card's nudge, one-click suggestion, the picker, the 409, and the empty state.
  {
    const { ctx, page } = await fresh("default", "/console");
    await page.waitForSelector(".conn-card .conn-nudge");
    assert.match(await page.textContent(".conn-card .conn-nudge"), /6 accounts are not linked/);
    await page.goto(`${base}/console/connectors/jira:01JB7Q2K9W`);
    await page.waitForSelector("#unlinked-note:visible");
    await page.click("#unlinked-note a");
    await page.waitForURL(/\/console\/people$/);
    await page.waitForSelector(".person-row");
    assert.match(await page.textContent("#people-count"), /8 accounts to link/);
    const first = page.locator(".person-row").first();
    await first.locator(".suggestions button").first().click();
    await first.locator(".person-done").waitFor();
    assert.match(await first.textContent(".person-done"), /Linked to Arjun Mehta/);
    assert.match(await page.textContent("#people-count"), /7 accounts to link/);
    const none = page.locator(".person-row[data-search=\"contractor (qa)\"]");
    await none.locator("select").selectOption({ label: "Noor Haddad" });
    await none.locator(".person-pick .btn").click();
    await none.locator(".person-done").waitFor();
    const byEmail = page.locator(".person-row[data-search=\"tomas reyes\"]");
    await byEmail.locator(".suggestions button").first().click();
    await byEmail.locator(".person-done").waitFor();
    assert.match(await byEmail.textContent(".person-done"), /Already linked/);
    await page.fill("#people-filter", "dev");
    assert.equal(await page.locator(".person-row:not([hidden])").count(), 1);
    step("people: nudge, suggestion, picker, already linked, filter");
    await page.goto(`${base}/console/_fixture/linked?next=/console/people`);
    await page.waitForSelector(".people-list .empty");
    assert.match(await page.textContent(".people-list .empty"), /Everyone is linked/);
    await page.goto(`${base}/console/_fixture/member?next=/console/teams`);
    await page.waitForSelector("#teams .team-card");
    assert.equal(await page.isHidden(".top nav a[data-nav=people]"), true);
    step("people: everyone linked; members do not see People");
    await ctx.close();
  }

  await browser.close();
  if (errors.length) { console.log("ERRORS:\n" + errors.join("\n")); process.exit(1); }
  console.log("all flows passed");
})().catch((e) => { console.error(e); process.exit(1); });
