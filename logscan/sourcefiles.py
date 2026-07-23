"""Opens a LogSource's underlying file, with validation that turns a raw OS
error into an actionable message. Shared by all three trigger entrypoints
(run_log_scan, run_scheduled_scans, tail_log_sources) — they used to each
duplicate this open() call, which is how a directory path produced a bare
`[Errno 13] Permission denied` (Windows' error for opening a directory as a
file) instead of a message that says what's actually wrong.
"""

import os


class InvalidLogSource(Exception):
    pass


def open_source_file(source):
    """Returns an open, binary-mode file object for `source`, or raises
    InvalidLogSource with a message specific enough to fix without needing to
    decode an OS errno. Single-file sources only ('path'/'upload') — a
    'directory' source has no single file to open; callers must branch to
    logscan.scanner.scan_directory() before ever reaching this function.
    Calling it with a directory source anyway is a caller bug, not a
    configuration mistake, so it raises rather than silently doing nothing."""
    if source.source_type == "path":
        path = source.path
        if not path:
            raise InvalidLogSource("This source has source_type='path' but no path is set.")
        if not os.path.exists(path):
            raise InvalidLogSource(f"Path does not exist: '{path}'")
        if os.path.isdir(path):
            raise InvalidLogSource(
                f"'{path}' is a directory, not a file — point this source at the specific "
                "log file to scan (e.g. 'app.log'), not its containing folder. To scan every "
                "file in a folder, use source_type='directory' instead."
            )
        try:
            return open(path, "rb")
        except PermissionError as exc:
            raise InvalidLogSource(f"Permission denied reading '{path}' — check the file's access permissions: {exc}")
    elif source.source_type == "upload":
        if not source.uploaded_file:
            raise InvalidLogSource("This source has source_type='upload' but no uploaded_file is set.")
        return source.uploaded_file.open("rb")
    else:
        raise InvalidLogSource(
            f"open_source_file() was called on a '{source.source_type}' source — "
            "directory sources must be scanned with logscan.scanner.scan_directory(), not opened as a single file."
        )


def source_file_label(source):
    """The file_path label to stamp onto LogPIIFinding rows for a single-file
    source — its own filename, so the findings report never needs a special
    case distinguishing 'which file' from 'which source' between directory
    scans (many files) and single-file scans (exactly one)."""
    if source.source_type == "upload":
        return source.uploaded_file.name if source.uploaded_file else ""
    return os.path.basename(source.path) if source.path else ""
