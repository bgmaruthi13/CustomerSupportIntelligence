from collections import Counter
from datetime import timedelta

from django.utils import timezone

PROBLEM_RECURRING_THRESHOLD = 5
PROBLEM_CONFIDENCE_THRESHOLD = 65


def compute_confidence(cluster_size, max_cluster_size, avg_membership_strength, recency_fraction,
                        weight_size=0.40, weight_density=0.35, weight_recency=0.25, floor=30, cap=97):
    """Weighted formula: cluster size (recurrence) + density (membership strength) + recency.

    Weights are normalized here, not validated at save time — whatever three numbers a
    project has configured (see Project.confidence_weight_*), they're divided by their
    own sum before being applied, so the formula always stays correctly bounded within
    [floor, cap] regardless of what was entered (e.g. "50/30/20" or "1/1/1" both just
    work). Floor is deliberately low by default (30) — a cluster with weak
    size/density/recency signal should be allowed to show as low-confidence rather than
    being inflated to "moderate".
    """
    total_weight = weight_size + weight_density + weight_recency
    if total_weight <= 0:
        weight_size, weight_density, weight_recency, total_weight = 0.40, 0.35, 0.25, 1.0
    w_size, w_density, w_recency = weight_size / total_weight, weight_density / total_weight, weight_recency / total_weight

    size_score = cluster_size / max_cluster_size if max_cluster_size else 0
    score = w_size * size_score + w_density * avg_membership_strength + w_recency * recency_fraction
    confidence = floor + score * (cap - floor)
    return round(min(cap, max(floor, confidence)))


def compute_trend(dates):
    """Compares ticket recurrence in the most-recent third of the date range vs. the
    earliest third of that same range (the middle third is deliberately excluded — it's
    the transition period and including it would dilute the early/late contrast).

    Returns (trend, reasoning) — reasoning is a plain-English sentence citing the actual
    counts and date windows behind the label, so "Rising" isn't just an unexplained badge.
    """
    if len(dates) < 4:
        return "stable", "Fewer than 4 tickets in this cluster — not enough spread over time to call a trend."

    sorted_dates = sorted(dates)
    span = sorted_dates[-1] - sorted_dates[0]
    if span.total_seconds() <= 0:
        return "stable", "All tickets in this cluster occurred at the same time, so there's no time spread to compare."

    third = span / 3
    early_cutoff = sorted_dates[0] + third
    late_cutoff = sorted_dates[-1] - third
    early_count = sum(1 for d in sorted_dates if d <= early_cutoff)
    late_count = sum(1 for d in sorted_dates if d >= late_cutoff)

    early_window = f"{sorted_dates[0]:%d %b %Y}–{early_cutoff:%d %b %Y}"
    late_window = f"{late_cutoff:%d %b %Y}–{sorted_dates[-1]:%d %b %Y}"
    basis = f"{late_count} tickets in the most recent third of this cluster's timeline ({late_window}) vs {early_count} in the earliest third ({early_window})"

    if late_count > early_count * 1.25:
        return "rising", f"{basis} — recurrence is accelerating (>25% more in the recent window)."
    if late_count < early_count * 0.75:
        return "falling", f"{basis} — recurrence is easing off (>25% fewer in the recent window)."
    return "stable", f"{basis} — volume is holding steady (within 25% either way)."


def compute_problem_thresholds(cluster_stats):
    """Derives recurring-count and confidence thresholds from this run's own
    distribution instead of using fixed constants.

    Fixed thresholds work for a handful of clusters but break down at scale — on a
    20k-ticket dataset every cluster cleared the old bar (recurring>=5, confidence>=65),
    flagging 96% of clusters as "problem candidates" and making the label meaningless.
    Percentile-based thresholds keep "candidate" meaning "stands out from this run's
    peers" regardless of dataset size, while falling back to the fixed baseline for
    small runs where percentiles would be noisy.

    cluster_stats: list of (recurring_count, confidence) for non-noise clusters.
    """
    if not cluster_stats:
        return PROBLEM_RECURRING_THRESHOLD, PROBLEM_CONFIDENCE_THRESHOLD

    def percentile(values, p):
        s = sorted(values)
        idx = min(len(s) - 1, int(len(s) * p))
        return s[idx]

    recurring_values = [r for r, _ in cluster_stats]
    confidence_values = [c for _, c in cluster_stats]
    recurring_threshold = max(PROBLEM_RECURRING_THRESHOLD, percentile(recurring_values, 0.75))
    confidence_threshold = max(PROBLEM_CONFIDENCE_THRESHOLD, percentile(confidence_values, 0.5))
    return recurring_threshold, confidence_threshold


def is_problem_candidate(recurring_count, confidence, trend, recurring_threshold, confidence_threshold):
    if recurring_count < recurring_threshold:
        return False
    if confidence < confidence_threshold:
        return False
    return trend in ("rising", "stable")


def most_common(values, default="Unspecified"):
    values = [v for v in values if v]
    if not values:
        return default
    return Counter(values).most_common(1)[0][0]


def most_common_multi(value_lists, default="Unspecified"):
    """Like most_common, but each input is itself a list of values (e.g. a ticket's
    offering_list) — every element counts toward its own tally, so a ticket tagged with
    two offerings contributes to both instead of being counted as one combined string."""
    exploded = [v for values in value_lists for v in values if v]
    if not exploded:
        return default
    return Counter(exploded).most_common(1)[0][0]


def recency_fraction(dates, window_days=90):
    if not dates:
        return 0.0
    cutoff = timezone.now() - timedelta(days=window_days)
    recent = sum(1 for d in dates if d >= cutoff)
    return recent / len(dates)
