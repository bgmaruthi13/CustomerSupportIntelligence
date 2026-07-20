# Correlate — Implementation Backlog

Suggestions from a review/brainstorm session (2026-07-20), grouped by theme. Not
sequenced into epics like a build-from-scratch project — these are additive features
on top of the existing platform, so pick items independently based on priority.

**Point scale:** 1 = few hours · 2 = half day · 3 = 1 day · 5 = 2–3 days · 8 = ~1 week.

---

## Theme A — Generative AI capabilities

*Everything currently labeled "AI" in the app (the "Generative AI" clustering engine,
Ask Correlate, Find Similar) is embeddings — semantic search/clustering via
sentence-transformers, not text generation. There is no LLM API call anywhere in the
codebase; "Copilot prompt" builds a text block for a human to manually paste into an
external tool. All items below need one shared building block first: an actual LLM
API integration wired into `clustering/pipelines.py` (Claude is a natural fit given
the existing Python/Django stack — no new infra beyond an API key).*

| # | Story | Pts |
|---|---|---|
| A.0 | LLM API integration foundation (client wiring, config/API key handling, error/timeout handling, cost guardrails) | 3 |
| A.1 | Auto-generated cluster summaries — one-line plain-English problem statement from a sample of ticket titles/descriptions, stored on `Cluster.ai_summary` (new field, sibling to existing `trend_reasoning`) | 3 |
| A.2 | AI-drafted root cause + resolution — extends existing `resolution_notes`/`resolution_source` (add an `ai_suggested` choice alongside `manual`/`copilot_assisted`); analyst reviews/edits the draft instead of starting blank | 5 |
| A.3 | Ask Correlate synthesis step — turn nearest-neighbor ticket retrieval into an actual RAG answer ("recurred 12 times this quarter, mostly EU, usually resolved by...") instead of stopping at a result list | 5 |
| A.4 | Anomaly explanation — when a cluster's trend flips to "rising," summarize *why* from a sample of the recent-window tickets (shared vendor, outage window, version number) instead of just the count-comparison sentence | 3 |
| A.5 | Executive brief narrative generation — LLM turns the KPI snapshot (see Theme B) into 1–2 paragraphs of prose instead of a table of numbers | 2 |

**Recommended starting point:** A.0 → A.1 (smallest surface area — one field, one call
site, no new UI flow — and immediately makes the existing cluster list more readable).

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

## Notes

- None of the above is scheduled or committed — this is a menu, not a roadmap.
- A.2 and B.1 both touch `Cluster`/resolution UX — if both are picked up, sequence
  B.1 (cost data) before A.2 (AI resolution drafts) so the AI-drafted resolution can
  reference cost impact if useful.
- C.2 and C.4 both need a "wait for usage history" runway before they're buildable —
  flag them as later-quarter items even if prioritized now.
