# Correlate — Implementation Backlog

Suggestions from a review/brainstorm session (2026-07-20), grouped by theme. Not
sequenced into epics like a build-from-scratch project — these are additive features
on top of the existing platform, so pick items independently based on priority.

**Point scale:** 1 = few hours · 2 = half day · 3 = 1 day · 5 = 2–3 days · 8 = ~1 week.

---

## Theme A — Generative AI capabilities (reset 2026-07-30 — implementation starting)

*Everything currently labeled "AI" in the app (the "Generative AI" clustering engine,
Smart Search, Find Similar) is embeddings — semantic search/clustering via
sentence-transformers, not text generation. There was no LLM API call anywhere in
the codebase, and A.1/A.4-equivalent features below were done via a manual copy/paste
bridge (build a prompt server-side → user pastes into their own Copilot chat → pastes
the answer back to save it) — that blocker is now resolved.*

**Provider: `copilot-http-bridge`** (`C:\Users\user\Documents\GitHub\bgmaruthi13\copilot-http-bridge`)
— a small VS Code extension exposing the user's signed-in GitHub Copilot chat model
over plain HTTP (`POST /ask {"prompt": "..."}` → `{"reply": "..."}`) while its
Extension Development Host window is running. Confirmed live 2026-07-30 —
`curl -X POST http://127.0.0.1:3939/ask -d '{"prompt": "reply with exactly the word:
pong"}'` returned `{"reply":"pong"}`, HTTP 200. Loopback-only is the right mode here —
the Django dev server runs on the same machine.

Real caveats, still true, worth re-reading before scaling this beyond prototyping:
no authentication on the bridge itself; only live while that VS Code window stays
open, not an always-on service; a Copilot seat is licensed as a coding assistant
inside an IDE, and routing arbitrary app traffic through it programmatically is a
different use case worth checking against license terms before real usage volume;
one seat won't hold up under multi-user production traffic the way a paid API tier
would. Treat this as the prototyping path to prove out value cheaply, not the
assumed-permanent production answer.

Every story below is the same shape: assemble structured context already in the DB →
one prompt via the bridge → a short piece of generated text → stored on a field,
shown in the UI. All read-only narrative generation — nothing here has the LLM take
an action or see data not already surfaced elsewhere in the app.

| # | Story | Pts | Status |
|---|---|---|---|
| A.0 | LLM bridge client foundation — `core/llm_bridge.py`: POST to the bridge, base URL configurable via env var (defaults to `http://127.0.0.1:3939`), timeout + clear "bridge unreachable" error (same honest-failure pattern the app already uses for the embedding model path, not a silent fallback) | 2 | **Done** — `ask()`/`bridge_status()`. Verified against the real running bridge (happy path, empty-prompt validation) and the actual failure mode this exists to catch (unreachable bridge → clear `LLMBridgeUnavailable`, not a silent/empty result) |
| A.0b | (follow-up, user-requested) Make the bridge URL live-configurable in-app instead of env-var-only — new `SiteSettings.llm_bridge_url` field + a "✨ Copilot HTTP Bridge" card on Clustering Settings, mirroring the existing Embedding Model path pattern exactly. Resolution order: DB setting → `LLM_BRIDGE_URL` env var → default. Deliberately does NOT auto-check connectivity on page load (would fire a real LLM call every time the settings page opens) — a separate "Test Connection" button does one on-demand round-trip instead | 1 | **Done** — verified via Django test client (blank DB setting correctly falls back; saving via the real view immediately changes what `_bridge_url()`/`ask()` resolve to, not just on next restart; pointing it at an unreachable address makes the very next `ask()` fail specifically against that new address) and a real live browser click on "Test Connection" ("Reachable at http://127.0.0.1:3939"). Cleanup restored the setting to blank afterward |
| A.1 | Cluster summaries — upgrade `Cluster.ai_summary` from the manual copy/paste flow to an automatic call through A.0, using the existing `_build_summary_prompt` | 2 | **Done** — new "Generate with Copilot Bridge" button alongside the existing manual flow (kept as fallback, not replaced). Verified via Django test client (real bridge call through the real view saved a real summary; simulated bridge-down left `ai_summary` untouched, no crash) and a real live browser click — genuinely correct AI-generated summary now showing on cluster 77: "The cluster represents a high volume of requests for updates to firewall rules for a new vendor IP." |
| A.2 | Trend explanation — same upgrade for `Cluster.ai_trend_explanation`, using the existing `_build_trend_explanation_prompt` | 2 | **Done** — same "Generate with Copilot Bridge" pattern as A.1, manual flow kept as fallback. Verified via Django test client (real bridge call saved a coherent hypothesis on a real Rising cluster; simulated bridge-down left the field untouched) and a real live browser click on cluster 102 ("Timeout / Job / Batch") |
| A.3 | Project-level context in A.1/A.2's prompts — project name/domain, other active clusters in the same project (today's prompts are cluster-only, no project framing) | 1 | **Done** — shared `_project_context_lines()` helper (project name+domain, top 4 other active clusters by recurring_count), prepended to both prompts. Verified via a standalone script (project name/domain present, correctly omits the "other clusters" header when there are none) and a real printed prompt confirming natural-reading output |
| A.4 | Root cause + resolution draft — automatic call through A.0 to pre-fill the existing Resolution card (`resolution_notes`), still fully editable/overridable — a starting draft, not a replacement for analyst judgment | 2 | **Done** — deliberately different UX than A.1/A.2: "Generate Draft" renders the result into the textarea for review, does NOT auto-save to the DB (unlike A.1/A.2's direct-save) — the analyst still clicks "Save Resolution Notes" themselves. Also added project context (A.3) to this prompt for consistency. Verified via Django test client (draft appears in the response/textarea, `resolution_notes` in the DB provably untouched by the generate step, bridge-down leaves DB untouched too) and a real live browser click |
| A.5 | Duplicate-pair explanation — one sentence on *why* a flagged pair on the Duplicate Candidates page (E.3) looks like a duplicate, beyond just the similarity %, to help a reviewer decide Confirm vs. Not a Duplicate | 1 | **Done** — new `DuplicateCandidate.ai_explanation` field (migration 0015) + per-pair "✨ Explain" button, auto-saves directly (unlike A.4 — this is a read-only reviewer aid, not something edited afterward). No real pairs existed in dev (same as E.3's verification) — verified via Django test client against a directly-created pair: real coherent explanation generated and saved, bridge-down correctly left the field untouched, cleanup confirmed |
| A.6 | Log pattern narrative (logscan) — turn a Find Patterns cluster's keywords/example line into a plain-English "what's likely happening" sentence, same summarization idea as A.1 applied to log clusters instead of ticket clusters | 2 | **Done** — new `LogPatternCluster.ai_narrative` field (migration 0005) + per-pattern "✨ Explain" button on the Patterns page, auto-saves directly like A.1. Verified via Django test client against a real pattern from the earlier sample-data seeding ("Info / Ip / Num", 24 recurring lines): real, accurate narrative generated ("multiple connection attempts... timing out after several retries, indicating potential network issues") correctly interpreting the actual underlying template; bridge-down left the field untouched; cleanup restored the original value |
| A.7 | Smart Search synthesis — one synthesized answer above the retrieved ticket list (classic RAG: read the top 5-10 results, write a synthesized answer), not just a ranked list | 3 | **Done, with one honest verification gap.** Opt-in via `?synthesize=1` on an existing search (not automatic on every query) — reads the top 8 results' titles + each match's cluster `resolution_notes` when it has one (the real fix history A.4 drafts into, surfaced back at search time), ephemeral (not persisted, recomputed per request). No embedding model is configured in this dev environment at all (pre-existing — `Project.objects...` has none set up, unrelated to A.7), so the real `_run_search()` retrieval path couldn't be driven end-to-end. Verified everything up to that boundary instead: the live page correctly still shows its pre-existing "Embedding model not configured" state with the new UI copy, no regression; and `_build_search_synthesis_prompt` + a real bridge call against realistic synthetic result objects produced a genuinely good, correctly-hedged synthesized answer (used the known resolution for 2/3 tickets, correctly flagged the third as possibly a different cause). Re-verify against real retrieval once an embedding model is configured. |
| A.8 | Executive brief narrative generation | 2 | Not started — still blocked on Theme B's KPISnapshot (B.2), independent of the LLM provider question |
| A.9 | (user-requested "wow factor" for a leadership demo) Dashboard "✨ Narrate This" — one click reads the same numbers already on the dashboard (ticket count, top clusters, trends, cost impact) and writes a 3-4 sentence live status briefing, the way an analyst would open a status update out loud | 1 | **Done** — opt-in via `?narrate=1` (never automatic), ephemeral like A.7 (not persisted). Verified via Django test client (real bridge call produced a genuinely good, accurate briefing off real project data: 2,578 tickets, correct top-cluster names/counts/trends; bridge-down showed a clear error, no broken panel) and a real live browser click |
| A.10 | (user-requested "wow factor" for a leadership demo) Cross-Source Patterns incident narrative — turn a correlation group's member patterns (different sources, overlapping in time) into one causally-ordered incident story instead of a raw table, e.g. "Auth service degraded, cascading into checkout failures downstream" | 1 | **Done** — new `LogPatternCorrelation.ai_incident_narrative` field (migration 0006) + "✨ Narrate Incident" button per group, auto-saves directly. Verified via Django test client against the real correlation group from earlier E.5 verification (2 sources, 4 members): genuinely impressive, correctly time-ordered real result — "Inventory Service experiencing a database connection pool exhaustion... may have led to the subsequent inventory lookup timeouts in the Checkout Service... continued to process orders... despite the earlier inventory issues." Bridge-down left the field untouched. Confirmed live via a real browser click; left in place as a real demo-ready result |

**Recommended build order:** A.0 (foundation) → A.1/A.2 (cheapest possible proof the
bridge works in real product code, since the prompts already exist and just need a
real call instead of copy/paste) → A.3 (small follow-on) → A.5/A.6 (same pattern,
new surfaces, still small) → A.4 (needs care: pre-filling a field a human then edits
is a different UX than read-only narrative text) → A.7 (the biggest one — new
retrieval+synthesis logic, not just automating an existing prompt) → A.8 (blocked
on B.2 regardless of provider).

**Fallback provider research (2026-07-24) — superseded by the `copilot-http-bridge`
decision above for now, kept here for if/when this needs to graduate to a paid API**
(e.g. the bridge's caveats become a real problem, or multi-user reliability is
needed): Azure OpenAI needs no special "AI license" beyond a normal Azure subscription plus
an Azure OpenAI resource provisioned in-portal (regional/model access gating has
loosened over time but should be re-checked live, not assumed from memory) — it's
consumption-based, billed per token, not a seat license. For a solo/small-team
project, Azure's provisioning overhead (subscription, resource creation, possible
regional gating) may cost more setup time than it saves versus going straight to
the Anthropic or OpenAI API directly (API key + billing, live in minutes) — Azure
mainly earns its keep if there's already Azure spend/compliance requirements to
consolidate onto. **Cost is not the constraint at this project's scale**: against
the current 52 non-noise clusters, a full summary+trend-explanation run costs well
under $1 on a cheap-tier model (GPT-4o-mini/Claude Haiku class, ~$0.15–1/M input,
~$0.60–5/M output) and only a few dollars even on a frontier-tier model — even
re-run daily for a month this stays single-digit dollars. **Estimated effort once a
provider/key is chosen: ~2–3 days** (A.0's client foundation ~1 day, A.6's
project-context prompt extension ~0.5–1 day, verification ~0.5 day) — not counting
any lead time an org's own Azure access-request process might add.

---

## Theme B — Management / ROI-facing reporting

*What actually gets read in a leadership review, vs. what a data scientist would want
to see day to day.*

| # | Story | Pts |
|---|---|---|
| B.1 | Cost-per-ticket setting (project or queue level) + dollar-impact framing on every cluster/dashboard number ("~$14,100/quarter if left unaddressed") | 3 | **Done** — project-level (`Project.cost_per_ticket`/`cost_currency_symbol`, new Cost Impact settings card). Dollar figure is an honest trailing-90-day run-rate (recent tickets × cost, scaled to a quarter), never a speculative forecast — 0/unset hides all figures rather than showing a misleading $0. Wired into Problem Clusters list (new column), cluster detail (callout), and a new dashboard KPI tile (summed across Problem Candidates). Verified via Django test client (pure-function edge cases, real settings-form round-trip, column/callout/tile appear and disappear correctly) and live browser (all 3 surfaces showed real computed figures — $2,805/qtr dashboard tile, $204/qtr on a real cluster — then reset back to the project's original cost_per_ticket=0 state, confirmed via direct DB read) |
| B.2 | `KPISnapshot` model + scheduled job to trend `manual_effort_pct`/`avg_confidence` over time instead of a live-only snapshot on the How It Works page | 5 |
| B.3 | Scheduled executive brief (weekly email/PDF: top 5 rising problem clusters + cost estimate, PII/compliance findings count, coverage trend) — needs a task scheduler (no Celery/cron in the stack today; smallest addition is probably Windows Task Scheduler + a management command, matching the existing `deploy/windows/` deployment model) | 5 |
| B.4 | Ranked "fix this first" list — single score combining confidence + recurring_count + trend + (once B.1 exists) cost, surfaced as literally 5 items instead of a sortable cluster table | 3 |
| B.5 | Compliance risk rollup on the PII report — one-line per-project summary ("3 high-confidence findings in unmapped columns — no PII imported to date") instead of a findings table only | 2 |
| B.6 | Cross-project/queue benchmarking — extend Global Clustering's cross-project intersection view to rank queues/teams by recurring-problem volume and confidence | 5 |

**Recommended starting point:** B.1 (small, additive, no new pipeline — display-layer
calculation on top of existing cluster/dashboard data) — makes every other number in
the app land differently in a management conversation.

---

## Theme C — Statistical rigor (data science)

*Where the platform's current heuristics would benefit from being actual tested
statistics instead of fixed-cutoff rules of thumb.*

| # | Story | Pts |
|---|---|---|
| C.1 | Replace `compute_trend()`'s fixed ±25% early/late-third heuristic with a proper statistical test (Mann-Kendall or a Poisson rate-ratio test between windows) — same UX, but the "rising/falling" label would carry a p-value instead of being sensitive to single-ticket noise on small clusters | 3 | **Done** — implemented as an exact binomial test (`scipy.stats.binomtest`), the correct conditional form of a Poisson rate-ratio test since the early/late windows are equal length. Same function signature, same callers unchanged. Verified via a standalone script (the exact "old heuristic says rising, new test correctly says stable" noise case, plus large-shift and balanced-split cases) and a real end-to-end re-run of Traditional ML against the live 2578-ticket dataset — found a real cluster (26 vs 14 split, p=0.081) that the old ±25% rule would have flagged "Rising" but the new test correctly calls "Stable"; confirmed rendering live in the UI (tooltip shows the p-value reasoning) |
| C.2 | Calibrate `compute_confidence()`'s weighted formula against outcome data — once enough `resolution_notes`/`is_problem_candidate` history exists, fit a logistic regression (size, density, recency, keyword entropy → "was this actioned") to replace the hand-picked weights with a validated probability | 8 |
| C.3 | Clustering quality evaluation harness — small human-labeled eval set ("these ticket pairs are/aren't the same problem") to compute silhouette/ARI across granularity presets, replacing the current fixed `min_cluster_size` constants with data-justified choices | 5 |
| C.4 | Learn `DuplicateCandidate` thresholds from confirm/dismiss feedback (`status`, `reviewed_by` are already collected) instead of the current fixed cosine-similarity + timing/reporter/app heuristic cutoffs | 5 |
| C.5 | Cycle-time / SLA analytics — needs an optional `resolved_at` field (mappable at ingestion, like `created_at`) to unlock time-to-resolution distributions and survival analysis on how long a cluster stays "hot" | 5 |
| C.6 | Multivariate EDA — chi-square test of independence / simple decision-tree segmentation across fields (e.g. "70% of failures are Application X in Country Y"), vs. today's independent univariate breakdowns | 3 |

**Recommended starting point:** C.1 — contained entirely to `clustering/scoring.py`,
no migrations, fixes something currently misleading users today.

---

## Theme D — Log scanning: broader PII detection via Presidio

*The `logscan` app's detection (`tickets.pii_detection.detect_pii()`, reused as-is)
covers email/phone/card/IBAN/account-number/address — email/card/IBAN via regex +
checksum (Luhn, mod-97), account-number/address as explicitly best-effort heuristics.
Two real gaps: no name detection at all, and address detection is a weak
postal-code-plus-keyword guess. Microsoft's
[Presidio](https://github.com/microsoft/presidio) closes both — regex+checksum
recognizers for the same structured types (email/card/IBAN/phone) plus dozens of
national-ID formats (SSN, Aadhaar, NHS number, etc.), and real NER-based `PERSON`/
`LOCATION` detection via a spaCy model.*

*The key design constraint carried over from `logscan`'s original build: a 100GB
file scan can't pay per-line NER inference cost by default, the same reason phone
detection (`scan_phone_numbers`) is opt-in today. Presidio's NLP engine can reportedly
be disabled to run only its regex-based recognizers (no spaCy load, no inference cost)
— **this needs to be benchmarked for real before committing to the design**, not
just trusted from docs, the same rigor the rest of `logscan` was verified with.*

| # | Story | Pts | Status |
|---|---|---|---|
| D.0 | Benchmark: install `presidio-analyzer`, confirm NLP-engine-disabled mode is actually as fast as the current regex-only `detect_pii()` on a real test file — go/no-go gate for D.1 | 2 | **Done — NO-GO.** Benchmarked on a realistic 100,000-line synthetic log (~0.5% PII density, matching logscan's `include_phone=False` call path) against `presidio-analyzer` 2.2.364 with its NLP engine genuinely disabled (`spacy.blank("en")` — tokenizer only, zero trained weights/inference, confirmed via a 0.35s load time). Current `detect_pii()`: 45,852 lines/sec. Presidio (NLP disabled): 3,570 lines/sec — **12.84x slower**, and found *fewer* matches on the same input (273 vs 369) despite entities scoped to only EMAIL_ADDRESS/CREDIT_CARD/IBAN_CODE. The claim that disabling Presidio's NLP engine gets back to regex-only speed does not hold — its `AnalyzerEngine.analyze()` per-call overhead (recognizer registry dispatch, span/context handling) dominates even with no model inference happening. |
| D.1 | Replace `detect_pii()`'s hand-rolled email/card/IBAN/phone/account regex with Presidio's equivalent regex-based recognizers (NLP engine disabled) — same speed, broader coverage (adds national-ID formats), one less thing to hand-maintain. Remap onto existing `PII_TYPES`/`LogPIIFinding`/`PIIFinding` schema | 5 | **Blocked — D.0 was a NO-GO.** Not proceeding: replacing the current detector would make every scan ~13x slower for a 100GB-scale tool built specifically to avoid that kind of per-line cost, with no accuracy upside on the formats already covered. |
| D.2 | Add `PERSON`/`LOCATION` detection as a new opt-in-per-source toggle (`use_name_location_detection`, off by default — same pattern as `scan_phone_numbers`), backed by Presidio's NLP engine + `en_core_web_sm` (13MB — smallest spaCy model; upgrade to `en_core_web_md` later only if `sm`'s accuracy proves too weak in practice) | 5 | Not blocked by D.0/D.1's outcome — this was always going to need Presidio's real NLP engine (name/location detection has no realistic regex substitute) and was always meant to be opt-in-per-source specifically because of the inference cost, same reasoning `scan_phone_numbers` already uses. Genuinely still viable as a standalone opt-in feature; just no longer bundled with a D.1 replacement of the fast-path detectors. |
| D.3 | Re-verify: chunk-boundary correctness, synthetic planted-value tests, and the full end-to-end browser verification `logscan` was originally built with — against the new engine, not assumed carried over | 3 | Blocked on D.2, if D.2 is picked up later |

**Outcome of this pass:** D.0's benchmark was the honest gate BACKLOG.md asked
for, and it says no — keep the current hand-rolled `detect_pii()` as the
always-on fast path. D.2 (opt-in name/location detection) is unaffected by this
and remains a real, separately-viable feature if there's appetite for it later —
it was never going to be free either way.

---

## Theme E — Download/export results from every results page

*User-requested (2026-07-24): every page that shows computed results — clusters,
duplicates, PII findings, discovered log patterns, cross-source correlations — is
currently view-only. No CSV/JSON export exists anywhere in the app today (`tickets`
has file *upload* for ticket import, not export). Someone reviewing clusters or
findings outside the app — pasting into a spreadsheet, attaching to an incident
ticket, feeding a management report — has no way to get the data out except
copy-pasting the rendered table.*

| # | Story | Pts | Status |
|---|---|---|---|
| E.0 | Shared CSV export helper (e.g. `core/export.py::csv_response(rows, headers, filename)`) — one utility every page's download button calls, instead of N one-off `HttpResponse`/`csv.writer` implementations | 2 | **Done** — verified via standalone script: correct headers, comma/quote escaping (RFC4180 via `csv.writer`), empty-rows handling |
| E.1 | Download on Problem Clusters (`clustering/list.html`, `detail.html`) — cluster summary CSV (name, keywords, recurring_count, confidence, trend) and a per-cluster CSV of its member tickets | 3 | **Done** — verified via Django test client against real dev data (engine-filtered summary export, per-cluster member export row-count matches actual member count) and live browser (both buttons present, correct URLs, 200 OK) |
| E.2 | Download on Global Clustering (`global_clustering.html`, `global_cluster_detail.html`) — same shape as E.1, across-project | 2 | **Done** — no real GlobalCluster data exists in dev (needs 2+ owned projects to run; only 1 project exists), so verified via Django test client with minimal directly-created GlobalCluster/GlobalClusterMember rows (engine filter correctly excludes wrong-engine cluster, member export row count exact, project name present) — cleanup explicitly re-verified (0 rows remain) after the earlier E.4 orphaned-fixture lesson. Live browser: page renders cleanly, download button correctly hidden when there's nothing to download |
| E.3 | Download on Duplicate Candidates (`duplicates.html`) — candidate pairs + status/reviewed_by | 1 | **Done** — exports ALL statuses (pending/confirmed/dismissed), not just the on-screen pending-only table. Verified via Django test client (both statuses present, correct headers, cleanup re-verified) plus a real found-and-fixed bug: the download link was initially only reachable from the "has pending pairs" branch, invisible whenever pending_count was 0 even with confirmed/dismissed pairs to export — added it to the empty-pending-state branch too (gated on reviewed_count) |
| E.4 | Download on Log PII Alerts (`logscan/findings.html`) — **masked previews only, never the raw matched value**, same guarantee the page and `LogPIIFinding` model already enforce; this is a redaction-preserving export, not a new exposure surface | 2 | **Done** — verified via Django test client (unfiltered + type-filtered + source-filtered downloads, masked values only) and live browser (button present with correct filter-preserving URL, 200 OK) |
| E.5 | Download on Log Patterns + Cross-Source Patterns (`logscan/patterns.html`, `correlations.html`) — cluster/correlation summary rows, `example_line` already redacted before it's ever saved so no extra care needed there | 2 | **Done** — Cross-Source Patterns export flattens one row per (correlation group, member cluster) pair. Verified via Django test client against real existing data from the earlier feature build (5/5 pattern rows, 4/4 flattened correlation-member rows, exact match) — read-only, no fixtures created. Live browser: both buttons present with correct URLs |

**Recommended starting point:** E.0 then E.4 — the helper is small and unblocks
everything else, and Log PII Alerts is the page most likely to actually get
exported for a compliance/incident report, so it's worth doing early. Explicitly
re-verify E.4's redaction guarantee holds through the export path (not just the
on-screen render) before calling it done — the whole point of `masked_preview` is
that raw values never leave the scanner, and a CSV export is a new path capable of
being downloaded and shared, unlike a browser table.

---

## Notes

- None of the above is scheduled or committed — this is a menu, not a roadmap.
- A.2 and B.1 both touch `Cluster`/resolution UX — if both are picked up, sequence
  B.1 (cost data) before A.2 (AI resolution drafts) so the AI-drafted resolution can
  reference cost impact if useful.
- C.2 and C.4 both need a "wait for usage history" runway before they're buildable —
  flag them as later-quarter items even if prioritized now.
