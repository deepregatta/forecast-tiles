"""Per-attempt regional evidence, kept outside immutable forecast runs.

Missing/failed reports never establish a successful canary. Only the confirmed
publisher result establishes publication; a scratch run is recorded separately.
Reports contain public source identities and aggregate resource measurements,
never error messages, destination paths, credentials or account identifiers.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import struct
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RegionalAttempt:
    def __init__(self, path: str, layer: str, requested: str | None, dry_run: bool):
        self.path = Path(path)
        self.started = self.changed = time.perf_counter()
        self.phase = "configuration"
        self.source_metrics: dict = {}
        self.data = {
            "schema_version": 1,
            "layer": layer,
            "requested_cycle": requested,
            "cycle": None,
            "run_id": None,
            "started_at": now(),
            "destination": "scratch" if dry_run else "r2",
            "pointer": "latest-regional.json",
            "outcome": "failed",
            "failure_category": None,
            "pointer_commit_confirmed": None,
            "last_success_before": None,
            "phases_s": {},
            "source": self.source_metrics,
            "output": None,
            "github": {
                name.lower(): os.environ.get(name)
                for name in (
                    "GITHUB_RUN_ID",
                    "GITHUB_RUN_ATTEMPT",
                    "GITHUB_SHA",
                    "GITHUB_EVENT_NAME",
                )
            },
        }

    def update(self, phase: str | None = None, **fields) -> None:
        if phase is not None:
            timer = time.perf_counter()
            times = self.data["phases_s"]
            times[self.phase] = times.get(self.phase, 0) + timer - self.changed
            self.phase, self.changed = phase, timer
        self.data.update(fields)

    def outcome(self, name: str, category: str | None = None, error: BaseException | None = None):
        self.data.update(outcome=name, failure_category=category)
        if error is not None:
            self.data["error_type"] = type(error).__name__

    def tiles(self, tiles: list[tuple[str, bytes]]) -> None:
        largest_decoded = largest_inflated = 0
        for _, gz in tiles:
            with gzip.GzipFile(fileobj=io.BytesIO(gz)) as stream:
                prefix = stream.read(8)
                header_bytes = struct.unpack("<I", prefix[4:8])[0]
                header = json.loads(stream.read(header_bytes))
            variables = header["variables"]
            largest_decoded = max(
                largest_decoded,
                sum(v["byte_length"] // (2 if v["dtype"] == "i16" else 1) * 4 for v in variables),
            )
            largest_inflated = max(
                largest_inflated,
                (8 + header_bytes + 3) // 4 * 4
                + sum((v["byte_length"] + 3) // 4 * 4 for v in variables),
            )
        self.data["output"] = {
            "tiles": len(tiles),
            "gzip_bytes": sum(len(gz) for _, gz in tiles),
            "largest_gzip_bytes": max((len(gz) for _, gz in tiles), default=0),
            "largest_decoded_bytes": largest_decoded,
            "largest_inflated_bytes": largest_inflated,
        }

    def finish(self, exit_code: int) -> None:
        failed_phase = self.phase
        self.update("finished", exit_code=exit_code, finished_at=now())
        self.data["duration_s"] = time.perf_counter() - self.started
        if exit_code and self.data["failure_category"] is None:
            self.data["failure_category"] = failed_phase
        # ru_maxrss is KiB on the Ubuntu ingestion runners. Do not silently
        # interpret another platform's resource units as equivalent evidence.
        if sys.platform == "linux":
            import resource

            self.data["peak_process_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    def write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", dir=self.path.parent, delete=False) as stream:
            temp = Path(stream.name)
            try:
                json.dump(self.data, stream, indent=1, allow_nan=False)
                stream.write("\n")
            except BaseException:
                temp.unlink(missing_ok=True)
                raise
        try:
            temp.replace(self.path)
        finally:
            temp.unlink(missing_ok=True)
