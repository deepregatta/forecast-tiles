#!/usr/bin/env python3
"""Check committed shared-contract pins offline; never refresh pins implicitly."""

import argparse
import gzip
import hashlib
import json
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "forecast-tiles"
LOCK = "contracts/shared-contracts.lock.json"
# Deliberate inventory changes require review alongside the pins, not deletion
# of a lock entry to silently stop checking a schema or consumer fixture.
EXPECTED = {
    "contracts/forecast-latest-regional.schema.json": (
        "passage",
        "contracts/forecast-latest-regional.schema.json",
        "PFT1/schema-1",
        "exact",
    ),
    "contracts/forecast-latest.schema.json": (
        "passage",
        "contracts/forecast-latest.schema.json",
        "PFT1/schema-1",
        "exact",
    ),
    "contracts/forecast-manifest.schema.json": (
        "passage",
        "contracts/forecast-manifest.schema.json",
        "PFT1/schema-1",
        "exact",
    ),
    "contracts/forecast-tile.schema.json": (
        "passage",
        "contracts/forecast-tile.schema.json",
        "PFT1/schema-1",
        "exact",
    ),
    "tests/fixtures/golden-N40W010-ensemble.bin.gz": (
        "forecast-tiles",
        "tests/fixtures/golden-N40W010-ensemble.bin.gz",
        "PFT1/schema-1",
        "exact",
    ),
    "tests/fixtures/golden-N40W010-ensemble.expected.json": (
        "forecast-tiles",
        "tests/fixtures/golden-N40W010-ensemble.expected.json",
        "PFT1/schema-1",
        "exact",
    ),
    "tests/fixtures/golden-N40W010-weather.bin.gz": (
        "forecast-tiles",
        "tests/fixtures/golden-N40W010-weather.bin.gz",
        "PFT1/schema-1",
        "exact",
    ),
    "tests/fixtures/golden-N40W010-weather.expected.json": (
        "forecast-tiles",
        "tests/fixtures/golden-N40W010-weather.expected.json",
        "PFT1/schema-1",
        "exact",
    ),
    "contracts/land-index-latest.schema.json": (
        "forecast-tiles",
        "contracts/land-index-latest.schema.json",
        "TLI1/schema-1",
        "exact",
    ),
    "contracts/land-index-manifest.schema.json": (
        "forecast-tiles",
        "contracts/land-index-manifest.schema.json",
        "TLI1/schema-1",
        "exact",
    ),
    "contracts/land-index-tile.schema.json": (
        "forecast-tiles",
        "contracts/land-index-tile.schema.json",
        "TLI1/schema-1",
        "exact",
    ),
    "tests/fixtures/land-index-raz/N48W005.bin.gz": (
        "forecast-tiles",
        "tests/fixtures/land-index-raz/N48W005.bin.gz",
        "TLI1/schema-1",
        "exact",
    ),
    "tests/fixtures/land-index-raz/expected.json": (
        "forecast-tiles",
        "tests/fixtures/land-index-raz/expected.json",
        "TLI1/schema-1",
        "exact",
    ),
    "tests/fixtures/land-index-raz/manifest.json": (
        "forecast-tiles",
        "tests/fixtures/land-index-raz/manifest.json",
        "TLI1/schema-1",
        "exact",
    ),
}


def git_blob(data):
    header = b"blob " + str(len(data)).encode() + b"\0"
    return "git-blob:" + hashlib.sha1(header + data, usedforsecurity=False).hexdigest()


def check(root):
    try:
        manifest = json.loads((root / LOCK).read_text())
        if manifest["pin_format"] != 1:
            raise ValueError("unsupported pin_format; expected 1")
        if manifest["repository"] != REPOSITORY:
            raise ValueError("pin repository does not match this checkout")
        rows = manifest["artifacts"]
        paths = [row["path"] for row in rows]
        if set(paths) != set(EXPECTED) or len(paths) != len(EXPECTED):
            raise ValueError("pin inventory is missing, duplicated or unexpected")
        errors = []
        for row in rows:
            path = row["path"]
            owner, canonical, contract, representation = EXPECTED[path]
            context = (
                f"producer forecast-tiles / consumer {','.join(row['consumers'])} / "
                f"checkout {REPOSITORY}: {path}; canonical {owner}/{canonical} "
                f"@ {row['canonical_revision']} ({contract})"
            )
            try:
                identity = (
                    row["canonical_repository"],
                    row["canonical_path"],
                    row["contract"],
                    row["representation"],
                )
                if identity != (owner, canonical, contract, representation):
                    raise ValueError("canonical identity/representation does not match inventory")
                consumers = (
                    ["passage", "tactician"] if contract.startswith("PFT1") else ["tactician"]
                )
                if row["producer"] != "forecast-tiles" or row["consumers"] != consumers:
                    raise ValueError("producer/consumer ownership does not match inventory")
                data = (root / path).read_bytes()
                digest = hashlib.sha256(data).hexdigest()
                expected_digest = row["sha256"]
                if representation == "exact":
                    if row["intentional_divergence"] is not None:
                        raise ValueError("exact vendor bytes must not declare divergence")
                    if git_blob(data) != row["canonical_revision"]:
                        raise ValueError(f"canonical revision drift: found {git_blob(data)}")
                else:
                    if not row["intentional_divergence"]:
                        raise ValueError("decompressed fixture needs an explicit divergence record")
                    canonical_data = (root / row["canonical_fixture"]).read_bytes()
                    if git_blob(canonical_data) != row["canonical_revision"]:
                        raise ValueError("stored producer fixture canonical revision drift")
                    if hashlib.sha256(canonical_data).hexdigest() != row["sha256"]:
                        raise ValueError("stored producer fixture SHA-256 drift")
                    if gzip.decompress(canonical_data) != data:
                        raise ValueError("gunzip representation differs from the producer fixture")
                    expected_digest = row["vendored_sha256"]
                if digest != expected_digest:
                    raise ValueError(f"SHA-256 drift: expected {expected_digest}, found {digest}")
            except (OSError, KeyError, TypeError, ValueError, EOFError, zlib.error) as error:
                errors.append(f"{context}: {error}.")
        return errors
    except (OSError, KeyError, TypeError, ValueError) as error:
        return [f"producer forecast-tiles / consumer {REPOSITORY}: invalid {LOCK}: {error}."]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=ROOT, help="isolated checkout for regression tests"
    )
    args = parser.parse_args()
    errors = check(args.root)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        print(
            "Refresh only after reviewing the named canonical artifact and version; "
            "copy the pinned bytes (or documented representation), update revision/digests "
            "together, and run producer/consumer contract tests. No automatic latest download.",
            file=sys.stderr,
        )
        return 1
    print(
        f"PASS {REPOSITORY}: {len(EXPECTED)} pinned artifacts; canonical revisions and SHA-256 agree."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
