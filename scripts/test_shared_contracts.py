"""Exercise actual committed pins and mismatched copies without siblings/network."""

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/check_shared_contracts.py"
LOCK = "contracts/shared-contracts.lock.json"


class ContractDriftTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="shared-contract-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.lock = json.loads((ROOT / LOCK).read_text())
        for row in self.lock["artifacts"]:
            target = self.root / row["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / row["path"], target)
        self.write_lock()
        target = self.root / SCRIPT.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SCRIPT, target)
        self.script = target

    def write_lock(self):
        target = self.root / LOCK
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.lock))

    def run_check(self):
        return subprocess.run(
            [sys.executable, str(self.script), "--root", str(self.root)],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )

    def assert_drift(self, result, row):
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        for text in (row["path"], row["canonical_repository"], "producer", "consumer", "Refresh"):
            self.assertIn(text, result.stderr)

    def test_committed_pins_pass_in_an_isolated_checkout(self):
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"{len(self.lock['artifacts'])} pinned artifacts", result.stdout)

    def test_each_schema_or_fixture_change_fails_with_owners_and_remedy(self):
        for row in self.lock["artifacts"]:
            with self.subTest(path=row["path"]):
                path = self.root / row["path"]
                original = path.read_bytes()
                if path.name.endswith(".schema.json"):
                    # Silent weakening: the old producer outputs still validate.
                    doc = json.loads(original)
                    doc["required"] = []
                    path.write_text(json.dumps(doc))
                elif path.suffix == ".json":
                    doc = json.loads(original)
                    doc["schema_version"] = 99
                    path.write_text(json.dumps(doc))
                else:
                    changed = bytearray(original)
                    changed[-1] ^= 1
                    path.write_bytes(changed)
                self.assert_drift(self.run_check(), row)
                path.write_bytes(original)

    def test_updating_only_digest_does_not_hide_a_stale_canonical_revision(self):
        row = next(r for r in self.lock["artifacts"] if r["representation"] == "exact")
        path = self.root / row["path"]
        path.write_bytes(path.read_bytes() + b"\n")
        row["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.write_lock()
        self.assert_drift(self.run_check(), row)

    def test_changed_canonical_digest_is_refused_for_every_representation(self):
        for row in self.lock["artifacts"]:
            with self.subTest(path=row["path"]):
                original = row["sha256"]
                row["sha256"] = "0" * 64
                self.write_lock()
                self.assert_drift(self.run_check(), row)
                row["sha256"] = original
        self.write_lock()

    def test_raw_fixture_cannot_diverge_even_with_an_updated_local_digest(self):
        rows = [r for r in self.lock["artifacts"] if r["representation"] == "gunzip"]
        if not rows:
            self.skipTest("this checkout vendors only exact representations")
        row = rows[0]
        path = self.root / row["path"]
        path.write_bytes(path.read_bytes() + b"changed payload")
        row["vendored_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.write_lock()
        result = self.run_check()
        self.assert_drift(result, row)
        self.assertIn("gunzip representation", result.stderr)

    def test_missing_artifact_fails_with_owners_and_remedy(self):
        row = self.lock["artifacts"][0]
        (self.root / row["path"]).unlink()
        self.assert_drift(self.run_check(), row)

    def test_deleted_or_duplicate_pin_cannot_reduce_the_checked_inventory(self):
        original = list(self.lock["artifacts"])
        for rows in (original[1:], original + [original[0]]):
            self.lock["artifacts"] = rows
            self.write_lock()
            result = self.run_check()
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("inventory", result.stderr)

    def test_unknown_pin_format_is_refused(self):
        self.lock["pin_format"] = 99
        self.write_lock()
        result = self.run_check()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("pin_format", result.stderr)


if __name__ == "__main__":
    unittest.main()
