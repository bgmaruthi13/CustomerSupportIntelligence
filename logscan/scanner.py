"""Streaming log-file scanner — the piece that makes 100GB+ files possible to
scan at all. Everything else in this app that touches PII (tickets.ingestion,
tickets.pii_detection's scan_dataframe_for_pii) reads its whole input into a
pandas DataFrame first; that's fine for a ticket export, impossible for a
log file two to three orders of magnitude larger. This module never holds
more than one chunk of raw bytes and one batch of pending findings in memory
at a time, no matter how large the source file is.

Two public entrypoints share one core loop (_scan_one_file):
  scan_stream()    — the original single-file path (LogSource source_type
                      'path'/'upload'): one file, one job, job finalized when
                      done.
  scan_directory() — source_type 'directory': many files under one job. Each
                      file is scanned with the exact same _scan_one_file() as
                      the single-file path (same chunk/carry-over correctness,
                      same batched writes), the job's totals accumulate
                      across files, and the job is finalized once at the end
                      rather than after each file.
"""

import fnmatch
import os

from django.db import transaction
from django.utils import timezone

from tickets.pii_detection import detect_pii

from logscan.models import LogPIIFinding, LogSourceFile

CHUNK_BYTES = 8 * 1024 * 1024  # 8MB per read — large enough to keep I/O calls
                                 # infrequent over a 100GB file, small enough
                                 # to never be a meaningful memory footprint.
BATCH_SIZE = 500                # findings buffered before a bulk_create flush
PROGRESS_EVERY_CHUNKS = 4       # persist job progress roughly every 32MB read,
                                 # not every line — a multi-hour scan needs a
                                 # live status without a DB write per line.


def _scan_one_file(fileobj, job, file_path, start_offset, scan_phones, chunk_bytes, batch_size):
    """Core streaming loop over ONE file object, starting at start_offset.
    Adds to job.bytes_scanned/lines_scanned/findings_count (reads job's
    current values as the starting point, rather than resetting them) so
    scan_directory() can call this once per file within a single job and get
    correct totals across every file, not just the last one scanned. Does
    NOT touch job.status/finished_at — finalizing the job is the caller's
    job, once, after every file it intends to scan in this run is done.

    Line-boundary handling: chunk reads don't align with line boundaries, so
    a line's bytes can be split across two chunks. `carry` holds whatever
    incomplete trailing segment a chunk ends on; it's prepended to the next
    chunk before splitting on b"\\n" again, so a PII value that happens to
    straddle a chunk boundary is never silently cut in half and missed —
    the actual condition this function's test coverage targets.

    Returns the final byte offset reached within THIS file (not cumulative
    across files — the caller persists this as this file's own resume point).
    """
    fileobj.seek(start_offset)
    pos = start_offset
    file_line_number = 0  # per-file, for LogPIIFinding.line_number — NOT the same as job.lines_scanned, which is cumulative across files
    pending = []
    carry = b""
    chunks_since_progress = 0

    base_bytes = job.bytes_scanned
    base_lines = job.lines_scanned
    base_findings = job.findings_count
    file_findings = 0

    def flush():
        if pending:
            with transaction.atomic():
                LogPIIFinding.objects.bulk_create(pending, batch_size=batch_size)
            pending.clear()

    def scan_line(raw_line, line_start):
        nonlocal file_findings
        text = raw_line.decode("utf-8", errors="replace")
        for f in detect_pii(text, include_phone=scan_phones):
            pending.append(LogPIIFinding(
                job=job, source=job.source, file_path=file_path,
                line_number=file_line_number, byte_offset=line_start,
                pii_type=f["pii_type"], confidence=f["confidence"],
                masked_preview=f["masked_preview"],
            ))
            file_findings += 1
        if len(pending) >= batch_size:
            flush()

    while True:
        chunk = fileobj.read(chunk_bytes)
        if not chunk:
            break

        data = carry + chunk
        lines = data.split(b"\n")
        carry = lines.pop()  # last element: complete only if data ended on b"\n"

        for raw_line in lines:
            file_line_number += 1
            line_start = pos
            pos += len(raw_line) + 1  # +1 for the newline just consumed
            scan_line(raw_line, line_start)

        chunks_since_progress += 1
        if chunks_since_progress >= PROGRESS_EVERY_CHUNKS:
            job.bytes_scanned = base_bytes + (pos - start_offset)
            job.lines_scanned = base_lines + file_line_number
            job.findings_count = base_findings + file_findings
            job.save(update_fields=["bytes_scanned", "lines_scanned", "findings_count"])
            chunks_since_progress = 0

    if carry:
        file_line_number += 1
        line_start = pos
        pos += len(carry)
        scan_line(carry, line_start)

    flush()
    job.bytes_scanned = base_bytes + (pos - start_offset)
    job.lines_scanned = base_lines + file_line_number
    job.findings_count = base_findings + file_findings
    job.save(update_fields=["bytes_scanned", "lines_scanned", "findings_count"])

    return pos


def _finalize_job(job):
    job.finished_at = timezone.now()
    job.status = "completed"
    job.save(update_fields=["finished_at", "status"])


def scan_stream(fileobj, job, scan_phones=False, file_path="", chunk_bytes=CHUNK_BYTES, batch_size=BATCH_SIZE):
    """Single-file entrypoint — LogSource source_type 'path' or 'upload'.
    `file_path` should be the source's own filename/path (see
    logscan.sourcefiles.source_file_label); left blank only for backward
    compatibility, not by design — every current caller passes it."""
    final_offset = _scan_one_file(fileobj, job, file_path, job.start_offset, scan_phones, chunk_bytes, batch_size)
    _finalize_job(job)
    return final_offset


def iter_directory_files(source):
    """Yields (relative_path, absolute_path) for every file under
    source.path matching source.file_pattern, recursing into subfolders iff
    source.recursive. Sorted for deterministic scan order — makes test
    assertions and log output reproducible, not load-bearing for correctness."""
    base = source.path
    if source.recursive:
        for root, _dirs, files in os.walk(base):
            for name in sorted(fnmatch.filter(files, source.file_pattern or "*")):
                abs_path = os.path.join(root, name)
                yield os.path.relpath(abs_path, base), abs_path
    else:
        try:
            entries = sorted(os.listdir(base))
        except OSError:
            return
        for name in fnmatch.filter(entries, source.file_pattern or "*"):
            abs_path = os.path.join(base, name)
            if os.path.isfile(abs_path):
                yield name, abs_path


def scan_directory(source, job, scan_phones=None, chunk_bytes=CHUNK_BYTES, batch_size=BATCH_SIZE):
    """Directory/NAS entrypoint — LogSource source_type 'directory'. Enumerates
    every matching file (see iter_directory_files), scans only the files
    that are new or have grown since their own LogSourceFile.last_scanned_offset
    (log rotation — a file shrinking below its stored offset — resets that
    file's offset to 0, same handling as tail_log_sources' single-file case),
    and finalizes the job once at the end rather than per file. One job
    therefore represents one whole pass over the directory, with totals
    summed across however many files actually had something new to scan.
    """
    if scan_phones is None:
        scan_phones = source.scan_phone_numbers

    for relative_path, absolute_path in iter_directory_files(source):
        file_row, _created = LogSourceFile.objects.get_or_create(source=source, relative_path=relative_path)
        file_row.save()  # bumps auto_now last_seen_at even when nothing else about the row changes this pass

        try:
            current_size = os.path.getsize(absolute_path)
        except OSError:
            continue  # vanished between listing and stat — skip this pass, next pass will just not see it either

        if current_size < file_row.last_scanned_offset:
            file_row.last_scanned_offset = 0
            file_row.save(update_fields=["last_scanned_offset"])

        if current_size <= file_row.last_scanned_offset:
            continue

        with open(absolute_path, "rb") as fileobj:
            final_offset = _scan_one_file(
                fileobj, job, relative_path, file_row.last_scanned_offset,
                scan_phones, chunk_bytes, batch_size,
            )
        file_row.last_scanned_offset = final_offset
        file_row.save(update_fields=["last_scanned_offset"])

    _finalize_job(job)
    return job.bytes_scanned
