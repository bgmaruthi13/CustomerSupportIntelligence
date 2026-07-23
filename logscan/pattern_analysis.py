"""Unsupervised log pattern discovery — the log-analysis equivalent of
clustering.pipelines.run_traditional_ml, but for a plain list of log-line
strings instead of a Ticket queryset. Deliberately bounded in scope (reads
only the last `PATTERN_ANALYSIS_MAX_BYTES` of a file, not the whole thing) -
logscan.scanner's chunked streaming exists to make a 100GB *scan* possible;
this is a fast, small, synchronous *analysis* pass over a recent slice, a
genuinely different problem with a genuinely simpler I/O approach.

Phase 1 (this module): pattern/template discovery only - group recurring log
lines and name/rank them, same shape as Problem Clusters for tickets. No
trend/anomaly detection yet (needs per-line timestamp parsing, which varies
too much by log format to attempt safely here) - that's an explicit later
phase, not attempted in this pass.

Known limitation, found during testing rather than assumed away: a window of
near-*identical* lines (almost no vocabulary diversity once shared boilerplate
is IDF-downweighted and any one-off unique tokens are min_df-filtered out) can
come out entirely as noise instead of one dominant cluster - TF-IDF's own
term-weighting naturally thins the signal in that specific degenerate case.
Verified this doesn't happen on realistic multi-template log windows (distinct
templates cluster cleanly); a genuinely single-template, low-diversity window
is the edge case worth revisiting if it shows up in practice (e.g. skip the
SVD reduction step when the vocabulary is already small).
"""

import os

import numpy as np
from django.db import transaction

from clustering.pipelines import DEFAULT_GRANULARITY, GRANULARITY_PRESETS, _min_cluster_size
from clustering.scoring import compute_confidence
from clustering.text_utils import cluster_title, top_keywords
from tickets.pii_detection import redact_pii

from logscan.models import LogPatternCluster
from logscan.pattern_text import normalize_log_line
from logscan.sourcefiles import source_file_label

PATTERN_ANALYSIS_MAX_BYTES = 5 * 1024 * 1024  # 5MB tail read - bounded by design, not a streaming scan
MIN_LINES_TO_CLUSTER = 20


class PatternAnalysisResult:
    def __init__(self, ran, message, cluster_count=0):
        self.ran = ran
        self.message = message
        self.cluster_count = cluster_count


def read_tail(path, max_bytes=PATTERN_ANALYSIS_MAX_BYTES):
    """One bounded read from the end of `path` — not the chunked/carry-over
    streaming logscan.scanner uses, which solves a different problem (scanning
    every byte of a 100GB file exhaustively). Pattern analysis only ever wants
    a recent slice, so a single seek-and-read is all this needs. Drops a
    possibly-partial first line if the read didn't start at byte 0."""
    size = os.path.getsize(path)
    start = max(0, size - max_bytes)
    with open(path, "rb") as f:
        f.seek(start)
        data = f.read()
    if start > 0:
        nl = data.find(b"\n")
        if nl != -1:
            data = data[nl + 1:]
    text = data.decode("utf-8", errors="replace")
    # rstrip("\r") handles CRLF-terminated files (common for Windows-authored
    # logs) without needing universal-newline text-mode reading, which would
    # complicate the byte-offset math a bounded binary read otherwise avoids.
    return [line.rstrip("\r") for line in text.split("\n") if line.strip()]


def analyze_patterns(source, max_bytes=PATTERN_ANALYSIS_MAX_BYTES):
    """Reads the last `max_bytes` of `source`'s file, clusters the (normalized)
    lines by shared vocabulary (TF-IDF + SVD + HDBSCAN — the same technique
    clustering.pipelines.run_traditional_ml uses for tickets), and replaces any
    existing LogPatternCluster rows for this (source, file_path) with the new
    results. path/upload sources only — see LogPatternCluster's docstring for
    why directory sources aren't supported yet (which file, or merge how, is a
    real design question left for a follow-on)."""
    from sklearn.decomposition import TruncatedSVD
    from sklearn.feature_extraction.text import TfidfVectorizer
    import hdbscan

    if source.source_type == "path":
        file_path = source.path
    elif source.source_type == "upload":
        file_path = source.uploaded_file.path if source.uploaded_file else ""
    else:
        return PatternAnalysisResult(False, "Pattern analysis isn't available for directory sources yet — pick a single file (path or upload) source.")

    if not file_path or not os.path.isfile(file_path):
        return PatternAnalysisResult(False, f"Could not read '{file_path}'.")

    raw_lines = read_tail(file_path, max_bytes)
    if len(raw_lines) < MIN_LINES_TO_CLUSTER:
        return PatternAnalysisResult(False, f"Need at least {MIN_LINES_TO_CLUSTER} lines in the analyzed window to cluster — found {len(raw_lines)}.")

    normalized_lines = [normalize_log_line(line) for line in raw_lines]

    preset = GRANULARITY_PRESETS[DEFAULT_GRANULARITY]
    # max_df is left at 1.0 (no upper-bound filtering) deliberately, unlike the
    # ticket pipeline's 0.6-0.95: for ticket descriptions, a word in nearly
    # every document really is generic boilerplate noise. For log lines it's
    # the opposite - near-universal shared vocabulary across a window IS the
    # template/pattern signal being looked for, not noise to discard. Filtering
    # it out the way ticket clustering does emptied the vocabulary entirely on
    # a highly-repetitive log window during testing.
    vectorizer = TfidfVectorizer(max_features=3000, ngram_range=(1, 2), min_df=2, max_df=1.0)
    try:
        tfidf_matrix = vectorizer.fit_transform(normalized_lines)
    except ValueError:
        return PatternAnalysisResult(False, "Log text in this window is too sparse/uniform to vectorize.")

    n_components = min(50, max(2, tfidf_matrix.shape[1] - 1, tfidf_matrix.shape[0] - 1))
    n_components = min(n_components, min(tfidf_matrix.shape) - 1) if min(tfidf_matrix.shape) > 1 else 2
    n_components = max(2, n_components)
    reduced = TruncatedSVD(n_components=n_components, random_state=42).fit_transform(tfidf_matrix)

    clusterer = hdbscan.HDBSCAN(
        min_cluster_size=_min_cluster_size(len(raw_lines), preset["min_cluster_size_floor"]),
        metric="euclidean", cluster_selection_method=preset["cluster_selection_method"],
    )
    labels = clusterer.fit_predict(reduced)
    strength = np.nan_to_num(clusterer.probabilities_, nan=0.5)

    labels_arr = np.asarray(labels)
    unique_labels = sorted(set(labels_arr.tolist()))
    max_size = max((int((labels_arr == lbl).sum()) for lbl in unique_labels if lbl != -1), default=1)

    file_label = source_file_label(source)
    to_create = []
    cluster_count = 0
    for lbl in unique_labels:
        idx = np.where(labels_arr == lbl)[0]
        is_noise = lbl == -1
        members_normalized = [normalized_lines[i] for i in idx]
        keywords = top_keywords(members_normalized, n=5)
        name = "Unclustered / Noise" if is_noise else cluster_title(keywords)

        avg_strength = float(np.mean([strength[i] for i in idx])) if len(idx) else 0.0
        # recency_fraction/weight_recency both zeroed — no per-line timestamp
        # parsing in this phase (see module docstring), so confidence is
        # size+density only; weights redistributed onto those two rather than
        # left diluted by a fixed-zero recency term.
        confidence = 0 if is_noise else compute_confidence(
            cluster_size=len(idx), max_cluster_size=max_size,
            avg_membership_strength=avg_strength, recency_fraction=0.0,
            weight_size=0.55, weight_density=0.45, weight_recency=0.0,
        )

        example_idx = idx[len(idx) // 2]
        example_line = redact_pii(raw_lines[example_idx])[:1000]

        to_create.append(LogPatternCluster(
            source=source, file_path=file_label, engine="traditional_ml",
            name=name, keywords=", ".join(keywords), example_line=example_line,
            recurring_count=len(idx), confidence=confidence, is_noise=is_noise,
            lines_analyzed=len(raw_lines),
        ))
        if not is_noise:
            cluster_count += 1

    with transaction.atomic():
        LogPatternCluster.objects.filter(source=source, file_path=file_label).delete()
        LogPatternCluster.objects.bulk_create(to_create)

    return PatternAnalysisResult(True, f"Found {cluster_count} pattern{'s' if cluster_count != 1 else ''} across {len(raw_lines)} recent lines.", cluster_count)
