#!/usr/bin/env python3
"""Check the canonical vendoring bundle without another checkout/network."""

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def check():
    lock = json.loads((ROOT / "contracts/storage-admission-v1.lock.json").read_text())
    if lock["version"] != 1 or lock["canonical_repository"] != "deepregatta/forecast-tiles":
        raise ValueError("unexpected storage contract provenance")
    for item in lock["artifacts"]:
        if hashlib.sha256((ROOT / item["path"]).read_bytes()).hexdigest() != item["sha256"]:
            raise ValueError(f"storage admission drift: {item['path']}")
    print(f"PASS storage admission v1: {len(lock['artifacts'])} pinned canonical artifacts")


if __name__ == "__main__":
    check()
