# Site design brief

The brief for rebuilding the public page at [brain.noobsurd.space](https://brain.noobsurd.space). It is the judges' first impression, so it has to look current and prove the product in the first screen. The page stays what it is today: static HTML, CSS and JS in `src/static/`, served by FastAPI, reading the same public routes. This brief changes how it looks and what it says, not what it does.

Short version: dark-first, one accent, and the live product as the hero: the ask box, real counts and a real slice of the graph. The single big piece of motion is the path being walked, edge by edge, from a real answer. Every number on the page comes from an API call, and no sentence claims more than main does.

The UI session (graph-hacks-34) builds it in five phases: the design system and hero; the ask section with the animated path; proof, every team, how it works, builders and footer; the team pages; then republishing the deck. Coder A adds `/api/graph/stats` and `/api/graph/sample` for the hero.

## 1. What leading product sites do now

Checked on 17 Oct 2026 by loading each home page in headless Chrome at 1440 px.

| Site | What it does | Take | Leave |
| --- | --- | --- | --- |
| [Linear](https://linear.app) | Near-black page. A two-line headline, left-aligned, with a muted subline. The real product UI sits directly under the headline as the hero. Further down, statements set in two tones (bright, then muted) and thin-bordered panels | The real product as the hero, not an illustration. Two-tone headlines. Restraint: one typeface, almost no colour | The customer logo wall |
| [Cursor](https://cursor.com) | A short headline, then the product working inside a window frame on a painted backdrop. Sections alternate text and a framed live panel | Frame the ask box and its path drawing like an app window, so the hero reads as a working tool | The painted backdrop; we have no art to match it |
| [Supabase](https://supabase.com) | Two-line headline with the second line in the brand green. Then a bento grid of feature cards, each with a small diagram of the feature | A bento grid for "How it works", one principle per card, each with a tiny real diagram (an edge line, a card preview, a refusal) | Feature sprawl; we have five principles, not twenty products |
| [Modal](https://modal.com) | Black, one neon accent on a single word of the headline, and an animated particle visual. Later sections show node-and-edge diagrams | One accent word in the headline. Node diagrams as the visual language | The decorative particle animation; ours must be a real graph from a real answer |
| [Vercel](https://vercel.com) | Black, a large centrepiece visual, a white pill as the primary button and a dark ghost button beside it. Product demos sit in framed panels | The button pair: a filled pill for the main action, a ghost button for the second | The event-style centrepiece |

What all five share, and what we copy: a near-black background with one accent, large but quiet sans headings, 1 px borders on slightly lifted panels, generous vertical space, and the product shown working. What we do not copy: logo walls, customer counts, testimonials and "trusted by" strips. We have none, and inventing them is against the rules and against the product's whole claim.

## 2. Positioning and page order, with the copy

The promise, in one line: **Ask how your company works. Every answer shows the path it walked.**

Sections in this order. The copy below is final unless main changes; numbers in `{braces}` are filled from the API at runtime, never typed in.

### 2.1 Hero

The whole first screen (between 640 and 940 px tall), on a curved horizon: navy at the top in both themes, a vivid indigo band behind the headline, then lavender to near-white (light) or deep indigo (dark) at the fold. The hero pulls up under the header, which is transparent with light ink while the hero's dark top is under it and solid once it leaves, so a deep link lands under a solid header.

- A centred 760 px block, left-aligned inside: the eyebrow `Graph Hacks · Track 03 · Company Brain on FalkorDB`; the headline in a light weight, **Ask how your company works.** then, muted, *Every answer shows the path it walked.*, with *path* and *walked* heavy, slanted and dotted-underlined; the composer; the example chips from `/api/ask/tools`.
- The composer is the centre of the page: a wide frosted card with the question on top, a "Public channels only" status pill (true: the page reads `org_shared`) and a round send button named "Ask". The product is the demo: asking renders the answer and the walk in 2.2.
- At the foot of the screen, bottom-right: **Take the 60-second tour** (2.1b), a ghost button; under 760 px it sits under the chips. Add **Watch the demo** only once the video link exists.
- The nav is sparse, uppercase and spread across the top, with **Graph** after **Ask**.

### 2.1b The 60-second tour

Show, don't describe: **Take the 60-second tour** plays the product, live. `tour.js` walks six steps. Each step asks its question through the page (`window.BrainPage`, the one hook in `app.js`), so the answer, its walk, the cache and the address behave as a click's. It lights up what the answer shows (the page dims around it), replays the walk, and says in one caption what to notice:

| Step | Asks | Lights up | On this graph |
| --- | --- | --- | --- |
| Every answer shows its path | `who owns billing, and who else has worked on it` | the drawing | who owns it, and how many edges the walk took |
| It says what it does not know | `who knows the payments gateway` | the refusal | the nearest real things |
| One person away from an outage | `what are our riskiest services` | the risk map | the first row: what depends on it, who knows it |
| Everything one person holds | `who is Arjun Mehta` | the profile grid | what they hold, and what rests on them alone |
| What changed this week | `what changed this week` | the digest | the count of changes (a quiet week widens to a month, and says so) |
| Take FalkorDB out | nothing | the proof table | the proof line ("5 of 6 capabilities collapse …") |

- **Live, not canned.** Under each caption, one line read off the step's own answer ("On this graph …"), with this graph's names and numbers. What that line names is marked in place once the step settles: the owner's `OWNS` edge and its line under the drawing (the drawing's own hover light), the Try instead row, the risk map's first row, the profile rows that rest on the person alone, and the proof line. A step's marks go when the step does.
- **Honest.** A step whose question `/api/ask/tools` does not offer as an example chip on the graph in use is skipped, so the tour never shows a dead end; the counter counts what is left.
- **Within the limit.** Five asks, at most eight (`MAX_ASKS`); a step seen again within a minute reads the page's cache.
- **Controls.** A card in a corner with the step counter, a dot per step, Back, Pause/Play, Next (Finish on the last) and Skip. It plays about nine seconds a step and stops at the last; it pauses itself when the tab is hidden or the reader scrolls the page. Esc leaves; the arrow keys step while the card has focus. Leaving keeps the answer on screen and gives focus back to the button. Finish leaves the last answer on screen under a closing card, "That's the tour", with **Start again** and **Ask your own** (which leaves and focuses the ask box).
- **Linkable.** `/?tour=1` starts it, `&step=N` at a step, and the address follows the tour, so a reload picks it up where it was. Leaving puts back the answer's own `?q=` address.
- **Accessible.** The card is a labelled, non-modal dialog whose caption is announced; the dots are named "Step N: …". Under reduced motion it never plays on its own, scrolls without smoothing and shows progress instead of a running bar.
- The recording view is untouched until the tour starts: the button sits beside How it works, and everything else is new classes in `tour.css`.

### 2.2 Live proof: ask the graph

- Eyebrow: `Live`. Heading: **Ask the graph behind this project.**
- Body: "This page reads the live graph of {company_name}, a synthetic company built for the demo: its {kinds}. Pick a question or write your own. The answer comes back with its path, drawn in the order it was walked." `{company_name}` comes from `/api/ask/tools`; today the live site serves Quillfern. `{kinds}` names, in this order, people, services, decisions, tickets, pull requests and chat, but only those `/api/graph/stats` counts above zero (chat is a `Memory` or a `Chat` node), so a plain seed with no pull requests does not claim any. If production is switched back to the Company Brain self-seed, the sentence becomes "the live graph behind this project: its own {kinds}", from services, decisions, pull requests and public chat.
- The answer to whatever was asked in the hero's box (max 300 characters, the server's cap), the drawing, then the `A -[REL]-> B` lines. On a deep link (`/?q=...`, which works today) the page asks once on load.
- Footer under each answer, from the response: "Tool `{tool}` · picked by keywords · read from public channels".
- Small print: "Public and read-only: public channels only, no model, answers cached for a minute."

#### Explore from any answer

Every answer is a place to go next, and every next click lands on an answer.

- **Ask next.** Up to three follow-up questions under every answer that was found, after the footer line ("Tool … · picked by … · read from …"), so the answer text, the drawing, its head and the footer's provenance stay where the recording frames them; a frame that is cut cuts the optional rows. They come from the tool and the path: after `who owns billing` they are what breaks if Billing Service goes down, what if Arjun Mehta (its owner) leaves, and what Billing Service depends on. The tool's own steps live in `answer.js`'s `NEXT`; the rest are the first question of each node drawn. The question just asked is never offered again.
- **Try instead.** A refusal keeps the server's sentence word for word and turns its nearest names into questions: "who knows the payments gateway" offers `who knows Billing Service` and `who knows API Gateway`.
- **Click a node.** Every node in an answer's drawing asks its first question on click, Enter or Space, and its tooltip says which ("Click to ask: what if Arjun Mehta leaves"). The hero graph asks the same question, from the same mapping. Team pages keep their drawings passive.
- **Walk with the keys.** On a focused node of any drawing, the arrow keys move along its edges: Right to what it points at, Left to what points at it, Up and Down to its neighbours in the column. Focus moves; nothing is asked until Enter.
- **Your walk.** Every question asked from the page is a step in the browser's history. Back and Forward show each step's answer again from memory, with no new ask and no scrolling, and a row after "Ask next" lists the earlier steps; a click goes back to one. A deep link starts the walk without adding an entry, and a reload starts a new walk.
- **A refusal with no near name** still offers two questions: the first example chips, which `/api/ask/tools` lists only when they answer on the graph in use.
- **The proof's questions** are buttons: each asks its question in the answer above.
- **Team pages** link each person, thing, decision and ticket to its first question on the ask page (`/?q=what if Arjun Mehta leaves`), from the same mapping. Their drawings stay passive.

One mapping, `BrainGraph.questionsFor(node)` in `graph.js` (its `QUESTIONS` table), says what to ask about a node by its label and, for a `Service`, its kind:

| Node | Questions |
| --- | --- |
| Service (service, system, process, product, policy, playbook) | `who knows {name}`, `what breaks if {name} goes down`, `what does {name} depend on` |
| Service, kind account | `who knows {name}` |
| Person | `what if {name} leaves` |
| Team | `who is on the {name} team` |
| Decision | `why did we {name, first letter lowercased}` |
| Ticket | `what is {name} about` |
| Document, or a name with no label | `who knows {name}` |

Pull requests, chats and memories offer nothing. Two tests hold the guarantee: `tests/unit/test_explore.py` routes every template to its tool and holds every `NEXT` step to a template, and `tests/integration/test_explore_questions.py` asks every question offered by every node of every example chip's answer and of the hero's sample, on a fresh Quillfern seed with and without its Sales team, and requires each to be found. A question that fails is fixed in the mapping, never in the test. Nothing is asked ahead: each click is one ask, inside the page's budget of about 60 counted calls a minute.

### 2.2b The graph behind every answer

- Eyebrow: `The graph`. Heading: **The graph behind every answer.**
- A live slice of the graph from `/api/graph/sample`, laid out deterministically (the same input gives the same picture), with node names on hover and focus; clicking a node asks about it. Pulses travel along real edges in their true direction (see 3.4). It is real data from the public scope, never a decorative fake graph.
- Under it, the live counts from `/api/graph/stats`, in mono with tabular numerals and reserved width: for example **{people}** people · **{services}** things teams run · **{decisions}** decisions · **{edges}** edges. Only counts the API returns; if the call fails, the row is not shown.

### 2.3 Why the graph matters

A full-width dark band in both themes, entered and left through blurred indigo waves; the proof table takes the band's own tokens.

- Heading: **Take FalkorDB out, and the answers go with it.**
- Body: "The same questions run twice: once against FalkorDB, once with every graph read returning nothing."
- Live line from `/api/ask/proof`: "**{collapsed} of {total}** capabilities collapse without the graph." Then: "The one that does not is a refusal. A question about something the graph does not know is refused either way, which is the point."
- The table from the same call: question, tool, with the graph, without, result. On mobile it becomes one card per question.

### 2.4 Every team

- Heading: **Not only engineering.**
- Body: "A `Service` node has a kind: a service, a system, a process, a product, an account, a policy or a playbook. A team's renewals process, the CRM it depends on and its pricing playbook answer the same questions as a billing service, in their own words: a process stalls, a playbook no longer applies."
- Team cards from `/api/teams`: the name, `{members}` people, `{owns}` things, and a chip per kind. Each card links to `/team/{id}`.
- Onboarding, beside or under the cards: "**One file, written for people who do not write code.** Import it and the team is in the graph. The import only merges and deletes nothing, and people Slack already knows are matched by email, never duplicated." Show 10 to 12 real lines from `docs/templates/team.example.toml` as a code panel, with a link to `docs/ONBOARDING-TEAMS.md`.
- The bus-factor beat: "**Ask what rests on one person.**" with a chip for `what if arjun mehta leaves`, which answers live today: "If Arjun Mehta leaves, 1 thing rests on them alone. Billing Service: Arjun Mehta is its only owner; next in line: Lena Fischer, then Tomas Reyes; 2 others depend on it." Once the Sales team is imported, *what if priya shah leaves* shows the cross-team case: the next in line for the pricing playbook, Tomas Reyes, is on Platform.

### 2.5 How it works

- Eyebrow: `How it works`. Heading: **How an answer is made.** Sub: "Three ways in, one fixed catalogue of tools, one graph. Every answer comes back with the path it walked."
- The architecture as one inline SVG, accurate to the code: three ways in (Slack: `@mention`, DM or thread; this page: `GET /api/ask`; MCP clients: `/mcp`, streamable HTTP), how each picks a tool (one model call with the keyword router as fallback; the keyword router; named by the caller), the tool catalogue (its read-only count filled from `/api/ask/tools`, never typed), the write tools that wait for an Approve card in Slack and are never served over MCP, and FalkorDB. Forward links are grey; the way back, carrying the answer and its path, is the walk's colour. Buttons (All, Slack, This page, MCP) fade everything off one route.
- Four steps in a row (a column on mobile):
  1. **Resolve.** "Names in the question are matched against the graph first. No match, no guess: it says what it does not know and names the nearest things it does."
  2. **Pick a tool.** "A fixed catalogue of hand-written, parameterised Cypher. In Slack a model picks the tool and fills in the arguments; it never writes a query."
  3. **Walk.** "The tool walks the graph, filtered to what the asker may read."
  4. **Show the path.** "The reply prints every edge it used, `A -[REL]-> B`. The explanation is the traversal, not a sentence written afterwards."
- A bento grid of five cards, each with a small real visual:
  - **It never writes Cypher.** The visual is a tool name with its argument schema.
  - **It refuses to guess.** The visual is the refusal line with its nearest names.
  - **Writes wait for a human.** "Filing a ticket or recording a decision shows a card that names the target before anything is written." The visual is a mock of the Approve card's text, labelled as the card's wording.
  - **Decisions stay current.** "A new decision supersedes the old one; both dates stay, and the answer quotes the message it came from." The visual is `New -[SUPERSEDES]-> Old`.
  - **Permissions are a graph query.** The visual is `(:SlackUser)-[:IS]->(:Person)<-[:IS]-(:GitHubUser)-[:CAN_READ]->(:Repo)`.
- The data model as one inline SVG in four colour families, the same grouping as the deck's slide 4: people and teams; what teams run; knowledge (documents, tickets, pull requests, decisions, chat); access (Slack users, GitHub users, channels, repos). Link to the README's "Graph data model".

### 2.6 For builders

A full-width dark band, like the proof.

- Eyebrow: `For builders`. Heading: **The same tools, over MCP.**
- Body: "Every read-only tool in the catalogue is served at `/mcp` over streamable HTTP, with the same schemas and the same cited results: the answer, the path, whether it was found, and the nearest names on a miss. Read-only, public scope, no token needed: about 60 calls a minute per address."
- A quick start in tabs, each with a copy button and this server's own address: Claude Code (`claude mcp add --transport http company-brain <origin>/mcp`, the README's command), any MCP client (the endpoint), Python (the README's client), and the ask API (`curl`). Without JavaScript all four show, stacked.
- The catalogue as a grid read live from `/api/ask/tools`: one card per read-only tool with its first sentence and its arguments, a filter box, and a line naming the tools that write (Slack only, after an Approve card; never over MCP). The proof's tool names link to their cards.
- Links: the README's data model and query list, local setup ("`docker compose up -d`, then two commands").

### 2.7 Footer

- "Built for Graph Hacks. Track 03, Company Brain (primary); also Track 01, Agents That Act on Connected Data, and Track 02, Agent Memory and Coordination."
- The AI disclosure, word for word from the README.
- "Synthetic data and this project's own only."
- Links: repository, README, blog and video when they exist, `/healthz` (status, deployed commit, FalkorDB; `/api/health` counts every node, system ones too, so it would disagree with the page's public counts).

### 2.8 Team pages

`/team/<id>` reads one team the way the ask page reads the company, and never shows an empty box.

- The head: the team's name, its people and what it runs, then one line from the page's own rows ("Payments depends on Platform (3 edges). Platform (1 edge) depends on Payments. 1 of the 2 things it runs rests on one person: Invoice Worker."). The other teams are linked above the name, so a reader can walk from team to team.
- People, then what the team runs grouped by kind.
- **Across teams**: the `DEPENDS_ON` edges into things other teams run, and from them, grouped by team, each team linked to its page. The team graph draws them, with the other team's `RUNS` edge.
- **Where risk sits**: the team's rows of the risk map, from the same query and ranking as the `risk_map` tool, drawn by the same grid as the ask page.
- Decisions that stand, open tickets, what was said, the team graph, and questions to ask.
- A section with no rows is not rendered; one line names them instead ("Not in the graph yet for this team: chat.").

## 3. Visual direction

### 3.1 Palette

Dark is the design target; light is fully supported. The node families keep their meaning everywhere: cobalt is people, orange is what teams run, green is knowledge, grey is everything else. The one accent is cyan, and it means "the walked path": the walk's glow, the primary button and focus rings. Its hue must stay clearly apart from the people colour, which is why people move from sky blue to cobalt.

| Token | Dark | Light |
| --- | --- | --- |
| `--bg` | `#08091A` | `#F6F7FB` |
| `--surface` | `#0F1122` | `#FFFFFF` |
| `--surface-2` | `#15182B` | `#EFF1F8` |
| `--line` | `rgba(200, 205, 255, 0.09)` | `rgba(18, 22, 48, 0.10)` |
| `--line-2` | `rgba(200, 205, 255, 0.16)` | `rgba(18, 22, 48, 0.18)` |
| `--ink` | `#EEF0FB` | `#11142A` |
| `--ink-2` | `#AAB0CF` | `#484D68` |
| `--ink-3` | `#8288AB` | `#62678A` |
| `--path` (accent) | `#2DD4E6` | `#0E8FA6` |
| `--horizon` | `#4B50E6` | `#5257E8` |
| `--horizon-glow` | `rgba(75, 80, 230, 0.44)` | `rgba(82, 87, 232, 0.26)` |
| `--horizon-haze` | `rgba(38, 43, 143, 0.45)` | `rgba(223, 225, 251, 0.85)` |
| `--person` | `#7C8CFF` | `#3B5BDB` |
| `--service` | `#F0703A` | `#D9541F` |
| `--knowledge` | `#2EC48C` | `#16946A` |
| `--other` | `#8B93A7` | `#807E77` |

Neutrals are cool and borders are alpha hairlines. The indigo horizon (`--horizon*`) glows behind the ask box only; it is never an accent. Text that sits over the glow uses `--ink-2` or stronger, because `--ink-3` drops under 4.5:1 at its peak.

The values are a starting point; the UI session may tune them.

Check every text pair against WCAG AA (4.5:1 for body, 3:1 for large text) and adjust the muted tones if a pair falls short. Panels are `--surface` with a 1 px `--line` border and a faint top highlight; no heavy shadows.

### 3.2 Type

- Geist Sans for headings and body. Weights are 400, 500 and 600 only, plus 420 for the hero headline, whose one heavy word (*path*, 600) carries a dotted underline because Geist has no italic. Section headings are 500.
- One type scale: `--fs-xs` 12 px, `--fs-sm` 13, `--fs-md` 15, `--fs-base` 16, `--fs-lg` 18 (the answer text), `--fs-xl` 24. Every font size in `style.css` is one of these, an `em` value or a heading clamp; `test_landing_page` checks it.
- Geist Mono for the graph's vocabulary: `A -[REL]-> B`, tool names, counts and eyebrow labels.
- Vendor both as `woff2` in `src/static/fonts/` (SIL Open Font License; add the licence file), about 115 KB together, preloaded. Use `font-display: swap` with a size-adjusted fallback so the swap does not shift the layout. When the deck is republished in phase 5, it moves to the same two faces.
- Scale: hero 56 to 64 px on desktop and 36 to 40 px at 390 px; section headings 32 to 40 px; body 16 to 18 px with a line height of 1.6.

### 3.3 Layout

- Content max width 1200 px and a 12-column grid. Sections 96 to 128 px apart on desktop, 64 px on mobile.
- The hero splits copy and live panel 5/7 on desktop and stacks on mobile.
- At 390 px: 16 px side gutters and no horizontal page scroll. The drawing scrolls inside its own frame, or lays out top to bottom. The proof table turns into cards.

### 3.4 Motion

- One signature motion: **the walk**. When an answer arrives, its edges draw in the order the tool returned them, each line growing from its source node to its target (stroke-dashoffset, about 250 ms per edge, staggered about 90 ms). Each node fades and scales from 0.96 to 1 as it is first reached. The matching `A -[REL]-> B` line lights up at the same moment. A small **Replay the walk** button runs it again.
- The hero's sample graph: pulses travel along real edges in their true direction, a few at a time. They run only while the hero is in view and the tab is visible, stop after a few cycles or at the first interaction, and never loop forever.
- Everything else is quiet: 150 to 200 ms fades and slides on hover, focus and section entry. No parallax, no scroll-jacking, no auto-playing video, no decorative animation that does not show a real edge.
- `prefers-reduced-motion: reduce` turns the walk into an instant draw and turns off every entrance animation.
- Animate only `transform`, `opacity` and SVG stroke properties, never layout properties.

### 3.5 The graph drawing

- Keep `graph.js`'s layered layout and its semantics: nodes as rounded rectangles in their family colour (a 16 % tint fill with a full-colour stroke), and edges as curves with arrowheads and relationship labels in mono.
- Add the walk (3.4), a hover state that highlights a node's edges and its path lines, and keyboard focus on nodes with the same highlight.
- The edge arrow always shows the true direction, as today. Do not straighten or reverse edges for looks.

### 3.6 Dark and light

Default to the operating system's setting, with a toggle in the header that remembers the choice in `localStorage`, wrapped in try/catch so a private window still renders. Keep the existing `data-theme` hooks, so `/team/{id}` follows the same theme.

### 3.7 Accessibility

- Every interactive element is reachable by keyboard and has a visible focus ring in the accent colour.
- The answer region is an `aria-live="polite"` region.
- The SVG drawing has a `<title>` and `<desc>`. The `A -[REL]-> B` list stays as the accessible text version of the path.
- "Ask next" and "Try instead" are buttons in a list named by its `aria-label`. A node that asks is named "{name}, {kind}. Click to ask: {question}" and asks on Enter or Space as well as on click. The arrow keys move focus along a drawing's edges.
- "Your walk" is a `nav` holding an ordered list; the step on screen carries `aria-current="step"`.
- Never use colour as the only signal: node labels name the label ("Service", "Person").
- Headings run in order, `h1` once. Landmarks: `header`, `main`, `footer`.
- Tap targets are at least 44 px at 390 px.

## 4. Hard constraints for the builder

1. **No build step and no new Python dependency.** Plain HTML, CSS and ES modules in `src/static/`. A JS library, if one is truly needed, is vendored into `src/static/vendor/` or loaded from cdnjs with an exact version at least two weeks old. Prefer none: the drawing and the walk are a few hundred lines of SVG.
2. **The same routes:** `/api/ask`, `/api/ask/tools`, `/api/ask/proof`, `/api/teams`, `/api/team/{id}` and the `/team/{id}` page, unchanged, with their caching and rate limits. The only new routes are coder A's `/api/graph/stats` and `/api/graph/sample`. Both read the public scope only (what a public channel reads), so no private-channel memory or private-repo pull request reaches the page, not even as a count. Both are cached and share the per-address limit.
3. **Request budget.** Questions, explorer walks and MCP calls are counted: about 60 a minute per address, with bursts of 30; a heavy one (a path, the risk map, an onboarding brief, a people ranking, a two-hop walk) counts as 5. Each of their graph queries stops at 2 seconds and the whole call at 4, so a busy address cannot hold the workers Slack and sync need. Answers are cached for 60 seconds, and the reads every visitor shares (tools, stats, sample, teams, proof, explorer starters) go out with `Cache-Control: public, max-age=60` and are not counted. First load makes four uncounted calls: tools, stats, sample and teams. The proof table loads when it scrolls into view. Nothing polls, retries in a loop, or asks more than one question without a click. A 429 shows a calm "Too many questions from this address; try again in a minute".
4. **Nothing calls the model from the public page.** It uses the keyword router, as now.
5. **No fabricated numbers, logos, testimonials or replies.** Every count comes from an API response. A quoted reply is either live or copied from a test that pins it, and labelled with where it comes from. No reference-project names anywhere.
6. **Copy matches main.** If a sentence in section 2 stops being true, change the sentence, not the facts.
7. **Performance.** No layout shift: reserve space for every async section with a skeleton of the final size. Page weight under about 340 KB before API JSON (300 KB until the risk grid, the explore affordances and the builders section, 320 KB until the tour), own JS under about 120 KB unminified (96 KB until the tour), at most two font files, no images over 150 KB. Aim for Lighthouse 90 or better on performance and accessibility, mobile profile.
8. **Works without JavaScript** for everything that is not live: the hero copy, how it works, for builders and the footer render as static HTML. Live sections say "This part needs JavaScript".
9. **Tests.** The existing page tests stay green, including the guard in `tests/unit/test_static_page.py`. New tests: every section in section 2 is present with its heading; no number in the HTML is hard-coded where section 2 says it comes from the API; the page makes no request outside the routes in point 2 and the vendored files; `/team/{id}` still renders.
10. **The team page** (`/team/{id}`) adopts the same tokens, type and drawing in the same PR or the next one, so the two pages read as one site.

## 5. Acceptance checklist

The research session reviews each UI PR after it merges, on a local run against the production self-seed and against a Quillfern replay. Headless Chrome at 1440 × 900 and 390 × 844, dark and light, plus a pass with reduced motion. Findings go straight to the UI session.

- [ ] The first screen at 1440 shows the promise, the ask box with its chips, the live counts and the sample graph, with no scrolling.
- [ ] The sample graph and the counts carry nothing from a private scope (checked against a graph with a private-channel memory and a private repo in it), and the pulses stop as section 3.4 says.
- [ ] At 390 there is no horizontal page scroll, the hero reads in one screen, and the drawing stays usable.
- [ ] The walk draws edges in the tool's order, the path lines light up with it, the replay works, and reduced motion draws instantly.
- [ ] Every number on the page (collapsed of total, tool count, team members and things) matches its API response.
- [ ] Every example chip answers with a path on the production self-seed; none of them is refused.
- [ ] The proof section loads only when scrolled to, and its sentence about the refusal is true for the current report.
- [ ] Team cards link to working `/team/{id}` pages in the same style.
- [ ] Section 2's copy is on the page as written, or the PR says what changed and why.
- [ ] Dark and light both pass AA on body text, and focus rings show on every control.
- [ ] No console errors, and no network requests outside the allowed routes and vendored files.
- [ ] First load makes four API calls or fewer, and asking twice within a minute serves the cached answer.
- [ ] No layout shift visible on load; skeletons match the final sizes.
- [ ] No fabricated numbers, logos or testimonials, and no reference-project names.
- [ ] `ruff` and the full suite green, with the new page tests.
</content>
</invoke>
