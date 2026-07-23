# Correlate — Implementation Backlog

Suggestions from a review/brainstorm session (2026-07-20), grouped by theme. Not
sequenced into epics like a build-from-scratch project — these are additive features
on top of the existing platform, so pick items independently based on priority.

**Point scale:** 1 = few hours · 2 = half day · 3 = 1 day · 5 = 2–3 days · 8 = ~1 week.

---

## Theme A — Generative AI capabilities

*Everything currently labeled "AI" in the app (the "Generative AI" clustering engine,
Smart Search, Find Similar) is embeddings — semantic search/clustering via
sentence-transformers, not text generation. There is no LLM API call anywhere in the
codebase. No LLM API key is available in this environment — A.1 and A.4 below were
implemented via the existing copy/paste bridge pattern instead (build a prompt
server-side → user pastes into their own Copilot chat → pastes the answer back to
save it), same mechanism `resolution_notes`/`_build_copilot_prompt` already used. A.2,
A.3, and A.5 don't fit that pattern (A.2 is already effectively covered by the
existing Resolution card; A.3/A.5 need to run unattended, which the copy/paste bridge
can't do) — they still need either a real LLM API key or a callable proxy like GitHub
Models before they're buildable.*

| # | Story | Pts | Status |
|---|---|---|---|
| A.0 | LLM API integration foundation (client wiring, config/API key handling, error/timeout handling, cost guardrails) | 3 | Not started — no API key available |
| A.1 | Auto-generated cluster summaries — one-line plain-English problem statement, stored on `Cluster.ai_summary` | 3 | **Done** — copy/paste bridge (Copy Prompt → paste into Copilot → save), shown on cluster list + detail |
| A.2 | AI-drafted root cause + resolution | 5 | Already covered — pre-existing Resolution card (`resolution_notes`/`copilot_assisted`) is this same pattern |
| A.3 | Smart Search synthesis step (RAG answer, not just retrieval) | 5 | Not started — needs an LLM API (unattended, doesn't fit copy/paste) |
| A.4 | Trend/anomaly explanation — hypothesis for *why* a cluster is rising/falling, stored on `Cluster.ai_trend_explanation` | 3 | **Done** — same copy/paste bridge, sample is the cluster's most-recent tickets; hidden when trend is "stable" |
| A.5 | Executive brief narrative generation | 2 | Not started — needs an LLM API + Theme B's KPI snapshot first |

**Next up:** A.0 (LLM API integration) is the blocker for A.3/A.5 — see the
conversation note above on GitHub Models as a callable option that doesn't need a
separate Anthropic/OpenAI subscription.

---

## Theme B — Management / ROI-facing reporting

*What actually gets read in a leadership review, vs. what a data scientist would want
to see day to day.*

| # | Story | Pts |
|---|---|---|
| B.1 | Cost-per-ticket setting (project or queue level) + dollar-impact framing on every cluster/dashboard number ("~$14,100/quarter if left unaddressed") | 3 |
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
| C.1 | Replace `compute_trend()`'s fixed ±25% early/late-third heuristic with a proper statistical test (Mann-Kendall or a Poisson rate-ratio test between windows) — same UX, but the "rising/falling" label would carry a p-value instead of being sensitive to single-ticket noise on small clusters | 3 |
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
| D.0 | Benchmark: install `presidio-analyzer`, confirm NLP-engine-disabled mode is actually as fast as the current regex-only `detect_pii()` on a real test file — go/no-go gate for D.1 | 2 | Not started |
| D.1 | Replace `detect_pii()`'s hand-rolled email/card/IBAN/phone/account regex with Presidio's equivalent regex-based recognizers (NLP engine disabled) — same speed, broader coverage (adds national-ID formats), one less thing to hand-maintain. Remap onto existing `PII_TYPES`/`LogPIIFinding`/`PIIFinding` schema | 5 | Not started — blocked on D.0 |
| D.2 | Add `PERSON`/`LOCATION` detection as a new opt-in-per-source toggle (`use_name_location_detection`, off by default — same pattern as `scan_phone_numbers`), backed by Presidio's NLP engine + `en_core_web_sm` (13MB — smallest spaCy model; upgrade to `en_core_web_md` later only if `sm`'s accuracy proves too weak in practice) | 5 | Not started — blocked on D.1 |
| D.3 | Re-verify: chunk-boundary correctness, synthetic planted-value tests, and the full end-to-end browser verification `logscan` was originally built with — against the new engine, not assumed carried over | 3 | Not started — blocked on D.1/D.2 |

**Recommended starting point:** D.0 — cheap (a couple hours), and the honest
go/no-go gate for the rest: if disabling Presidio's NLP engine doesn't actually
match current regex-only speed, D.1 needs a different design (e.g. keep the current
hand-rolled detectors and use Presidio only for D.2's `PERSON`/`LOCATION` toggle,
rather than replacing everything).

---

## Notes

- None of the above is scheduled or committed — this is a menu, not a roadmap.
- A.2 and B.1 both touch `Cluster`/resolution UX — if both are picked up, sequence
  B.1 (cost data) before A.2 (AI resolution drafts) so the AI-drafted resolution can
  reference cost impact if useful.
- C.2 and C.4 both need a "wait for usage history" runway before they're buildable —
  flag them as later-quarter items even if prioritized now.
