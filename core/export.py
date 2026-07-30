"""Shared CSV export helper — one utility every results page's download button
calls, instead of each page hand-rolling its own HttpResponse/csv.writer setup.
Used by E.1-E.5 (see BACKLOG.md Theme E): Problem Clusters, Global Clustering,
Duplicate Candidates, Log PII Alerts, Log Patterns/Cross-Source Patterns."""

import csv

from django.http import HttpResponse


def csv_response(rows, headers, filename):
    """rows: iterable of iterables (already stringifiable values, in header order).
    headers: column header row. filename: without extension, ".csv" is appended.
    Streams via csv.writer directly into the response body — no intermediate
    file or in-memory buffer, so this scales the same way the rest of the app's
    export-adjacent code (e.g. logscan's streaming scanner) avoids loading
    everything into memory at once, even though a results-page row count is
    nowhere near log-file scale."""
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{filename}.csv"'
    writer = csv.writer(response)
    writer.writerow(headers)
    writer.writerows(rows)
    return response
