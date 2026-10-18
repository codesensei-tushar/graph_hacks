// Company Brain: the risk_map grid, one row per owned thing, in the order the tool ranked them.
// Plain JS, no build step, no network: it lays out the answer's "grid" (columns, rows, scoring).
// Every piece of text is inserted with textContent.
(() => {
  "use strict";

  const { el } = window.BrainGraph;
  const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
  const sr = (text) => el("span", { class: "sr" }, text);
  const CAPTION = "Where risk concentrates, highest first";
  const NOT_RANKED = "accounts are not in the dependency graph";
  const RANK_TITLE = "PageRank over DEPENDS_ON, the same call load_bearing uses";

  // A null is a muted dash with a reason, never a value that reads as fine.
  function dash(cell, why) {
    cell.classList.add("na");
    cell.title = why;
    cell.append(el("span", { "aria-hidden": "true" }, "–"), sr(why));
    return cell;
  }

  // The name asks the follow-up a reader wants next: what stops with a service or system,
  // who to talk to about anything else.
  function thingCell(row, ask) {
    const cell = el("th", { scope: "row", class: "thing" });
    const owners = row.owners || [];
    const ownedBy = owners.length ? `Owned by ${owners.join(", ")}${(row.owner_teams || []).length ? ` (${row.owner_teams.join(", ")})` : ""}` : "No owner";
    if (ask) {
      const question = ["service", "system"].includes(row.kind) ? `what breaks if ${row.name} goes down` : `who owns ${row.name}, and who else has worked on it`;
      const button = el("button", { type: "button", class: "ask", title: `${ownedBy}. Ask: ${question}` }, row.name);
      button.addEventListener("click", () => ask(question));
      cell.append(button);
    } else {
      cell.append(el("b", { title: ownedBy }, row.name));
    }
    const sub = el("span", { class: "sub" }, [row.kind === "service" ? "" : row.kind, row.team || "no team"].filter(Boolean).join(" · "));
    if (!owners.length) sub.append(el("span", { class: "muted" }, " · no owner"));
    cell.append(sub);
    return cell;
  }

  // "hot":"high" (dependents) draws a bar against the grid's largest; "hot":"low" (people) is
  // hot at none or one, warm at two, plain from three, with the names as its title.
  function countCell(column, row, max) {
    const value = row[column.key];
    const names = Array.isArray(value) ? value : null;
    const n = names ? names.length : Number(value) || 0;
    const cell = el("td", { class: "count" });
    const number = el("span", { class: n ? "n" : "n zero" }, String(n));
    cell.append(number);
    if (column.hot === "low") {
      number.className = `n pill${n <= 1 ? " hot" : n === 2 ? " warm" : ""}`;
      if (names) {
        const who = names.length ? names.join(", ") : "nobody";
        cell.title = who;
        number.setAttribute("aria-label", `${plural(n, "person", "people")}: ${who}`);
      }
    } else if (column.hot === "high") {
      const track = el("span", { class: "bar", "aria-hidden": "true" });
      const fill = el("span");
      fill.style.width = `${max ? Math.round((n / max) * 100) : 0}%`;
      track.append(fill);
      cell.append(track);
    }
    return cell;
  }

  // The algorithm's own number, for reference: two decimals, no heat.
  function scoreCell(column, row) {
    const value = row[column.key];
    const cell = el("td", { class: "score" });
    if (value === null || value === undefined) return dash(cell, NOT_RANKED);
    cell.textContent = Number(value).toFixed(2);
    return cell;
  }

  // Raised: a red square, read as "yes". Clear: an empty cell. Null: does not apply.
  function flagCell(column, row) {
    const value = (row.flags || {})[column.key];
    const cell = el("td", { class: "flag" });
    if (value === null || value === undefined) return dash(cell, "does not apply");
    if (!value) {
      cell.append(sr("no"));
      return cell;
    }
    cell.classList.add("on");
    cell.append(el("span", { class: "mark", "aria-hidden": "true" }), sr("yes"));
    return cell;
  }

  function cellFor(column, row, max) {
    if (column.type === "flag") return flagCell(column, row);
    if (column.type === "count") return countCell(column, row, max[column.key]);
    if (column.type === "score") return scoreCell(column, row);
    const value = row[column.key];
    return value === null || value === undefined ? dash(el("td"), "not known") : el("td", {}, String(value));
  }

  const sizeOf = (value) => (Array.isArray(value) ? value.length : Number(value) || 0);

  // On a phone the grid scrolls sideways inside its frame, never the page; the Thing column
  // stays put, and a fade at the right edge says there is more. PageRank, the algorithm's own
  // number, goes last, so the risk signals fill the first screen of a phone.
  function render(container, grid, opts = {}) {
    const columns = [...(grid.columns || [])].sort((a, b) => (a.type === "score") - (b.type === "score"));
    const rows = grid.rows || [];
    if (!rows.length) return;

    const table = el("table", { class: "riskmap" });
    table.append(el("caption", { class: "sr" }, CAPTION));
    const head = el("tr");
    head.append(el("th", { scope: "col", class: "thing" }, "Thing"));
    for (const column of columns) {
      const th = el("th", { scope: "col", class: column.type }, column.label);
      if (column.type === "score") th.title = RANK_TITLE;
      head.append(th);
    }
    const thead = el("thead");
    thead.append(head);
    table.append(thead);

    const max = {};
    for (const column of columns) {
      if (column.type === "count") max[column.key] = Math.max(0, ...rows.map((row) => sizeOf(row[column.key])));
    }
    const body = el("tbody");
    for (const row of rows) {
      const line = el("tr", row.score ? {} : { class: "calm" });
      line.append(thingCell(row, opts.ask));
      for (const column of columns) line.append(cellFor(column, row, max));
      body.append(line);
    }
    table.append(body);

    // The caption is read with the table; its visible copy is a heading like the path's, above the
    // scroller, so it stays in view while the grid scrolls.
    const heading = el("h3", { class: "riskmap-head", "aria-hidden": "true" }, `${CAPTION} `);
    const info = el("span", { class: "meta" });
    info.append("(", el("code", {}, "risk_map"), `, ${plural(rows.length, "thing", "things")})`);
    heading.append(info);
    const scroller = el("div", { class: "riskmap-scroll", tabindex: "0", role: "region", "aria-label": "Risk map, scrolls sideways" });
    scroller.append(table);
    const pane = el("div", { class: "riskmap-pane" });
    pane.append(scroller, el("span", { class: "riskmap-cue", "aria-hidden": "true" }, "scroll →"));
    const frame = el("div", { class: "riskmap-frame" });
    frame.append(pane);
    container.append(heading, frame);
    if (grid.scoring) container.append(el("p", { class: "riskmap-note" }, `Ranked by ${grid.scoring.replace(/ x /g, " × ")}.`));

    const showEdges = () => {
      const rest = scroller.scrollWidth - scroller.clientWidth;
      pane.classList.toggle("more-left", rest > 1 && scroller.scrollLeft > 1);
      pane.classList.toggle("more-right", rest > 1 && scroller.scrollLeft < rest - 1);
    };
    scroller.addEventListener("scroll", showEdges, { passive: true });
    if ("ResizeObserver" in window) new ResizeObserver(showEdges).observe(scroller);
    else showEdges();
  }

  window.RiskMap = { render };
})();
