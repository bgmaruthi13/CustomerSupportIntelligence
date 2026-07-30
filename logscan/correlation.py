"""Cross-source pattern correlation — the multi-project/multi-tool answer to
Find Patterns being scoped to one file at a time. Groups non-noise
LogPatternCluster rows from DIFFERENT LogSources in the same Project whose
time ranges overlap within a configurable window, so "these different
tools/services all logged something unusual around 14:05" surfaces as one
finding instead of N separate per-source pattern lists nobody would think to
compare by hand.

Deliberately reuses LogPatternCluster rows already produced by
pattern_analysis.analyze_patterns() — this module never re-reads a log file
or re-runs TF-IDF/HDBSCAN; it only compares the first_line_at/last_line_at
range each cluster already recorded, from whichever of its own member lines
had a parseable ISO-8601 timestamp (pattern_text.extract_timestamp). A
cluster with no parseable timestamp in any of its lines has no time range
and is silently excluded from correlation — it isn't an error, just nothing
to compare.

Known simplification, disclosed rather than hidden: grouping uses
transitive overlap (if A overlaps B and B overlaps C, all three land in one
group even if A and C don't themselves overlap) — the same chaining
behavior density-based clustering (HDBSCAN) already exhibits elsewhere in
this app, and a reasonable first pass for "a spread-out incident touched
several sources across a few minutes." Revisit if it proves too eager in
practice.
"""

from datetime import timedelta
from itertools import combinations

from django.db import transaction

from logscan.models import LogPatternCluster, LogPatternCorrelation, LogPatternCorrelationMember

DEFAULT_WINDOW_MINUTES = 10


class CorrelationResult:
    def __init__(self, ran, message, group_count=0):
        self.ran = ran
        self.message = message
        self.group_count = group_count


def _latest_clusters_per_source(project):
    """Non-noise clusters with a usable time range, restricted to each
    source's MOST RECENT analyze_patterns() run — a source may have been
    re-analyzed several times, and only the latest run's clusters are
    current (analyze_patterns wipes-and-rebuilds per source on every run, so
    stale rows from a prior run shouldn't normally still be present, but
    this guards against comparing two different runs of the same source
    were that invariant ever violated)."""
    clusters = list(
        LogPatternCluster.objects
        .filter(source__project=project, is_noise=False,
                first_line_at__isnull=False, last_line_at__isnull=False)
        .select_related("source")
        .order_by("source_id", "-created_at")
    )
    latest_run_at = {}
    for c in clusters:
        if c.source_id not in latest_run_at:
            latest_run_at[c.source_id] = c.created_at
    return [c for c in clusters if c.created_at == latest_run_at[c.source_id]]


def _ranges_overlap(a_start, a_end, b_start, b_end, window):
    latest_start = max(a_start, b_start)
    earliest_end = min(a_end, b_end)
    if latest_start <= earliest_end:
        return True
    return (latest_start - earliest_end) <= window


def correlate_project(project, window_minutes=DEFAULT_WINDOW_MINUTES):
    """Wipes and rebuilds this project's LogPatternCorrelation rows from the
    current set of eligible clusters (see _latest_clusters_per_source).
    Requires clusters with timestamps from at least 2 distinct sources to
    find anything — callers should point the user at running Find Patterns
    on more sources first when this comes back empty for that reason."""
    window = timedelta(minutes=window_minutes)
    clusters = _latest_clusters_per_source(project)

    distinct_sources = {c.source_id for c in clusters}
    if len(distinct_sources) < 2:
        return CorrelationResult(
            False,
            f"Need clusters with timestamps from at least 2 sources to correlate — found timestamped clusters from {len(distinct_sources)}. Run Find Patterns on more path/upload sources first.",
        )

    parent = list(range(len(clusters)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    for i, j in combinations(range(len(clusters)), 2):
        a, b = clusters[i], clusters[j]
        if a.source_id == b.source_id:
            continue  # correlation is about DIFFERENT sources agreeing on a window
        if _ranges_overlap(a.first_line_at, a.last_line_at, b.first_line_at, b.last_line_at, window):
            union(i, j)

    groups = {}
    for idx in range(len(clusters)):
        groups.setdefault(find(idx), []).append(clusters[idx])

    multi_source_groups = [g for g in groups.values() if len({c.source_id for c in g}) >= 2]

    with transaction.atomic():
        LogPatternCorrelation.objects.filter(project=project).delete()
        for group in multi_source_groups:
            starts = [c.first_line_at for c in group]
            ends = [c.last_line_at for c in group]
            correlation = LogPatternCorrelation.objects.create(
                project=project, window_minutes=window_minutes,
                overlap_start=min(starts), overlap_end=max(ends),
                source_count=len({c.source_id for c in group}),
            )
            LogPatternCorrelationMember.objects.bulk_create([
                LogPatternCorrelationMember(correlation=correlation, cluster=c) for c in group
            ])

    count = len(multi_source_groups)
    return CorrelationResult(
        True,
        f"Found {count} cross-source correlation{'s' if count != 1 else ''} across {len(clusters)} eligible clusters from {len(distinct_sources)} sources.",
        count,
    )
