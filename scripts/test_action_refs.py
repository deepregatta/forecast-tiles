"""Verify update failures and annotated-tag peeling without network or credentials."""

import unittest
from unittest.mock import patch

from resolve_action_ref import resolve

SHA = "a" * 40
TAG_SHA = "b" * 40


class ActionRefsTests(unittest.TestCase):
    def check(self, responses, git_sha=SHA, **kwargs):
        with (
            patch("resolve_action_ref.api", side_effect=responses),
            patch("resolve_action_ref.git_ref", return_value=git_sha),
        ):
            return resolve("actions/checkout", "v4.4.0", **kwargs)

    def identity(self):
        return {"full_name": "actions/checkout", "archived": False}

    def ref(self, kind="commit", sha=SHA):
        return {"ref": "refs/tags/v4.4.0", "object": {"type": kind, "sha": sha}}

    def commit(self):
        return {"sha": SHA, "verification": {"verified": True, "reason": "valid"}}

    def test_lightweight_tag_verified_by_git_and_rest(self):
        result = self.check([self.identity(), self.ref(), self.commit()], expected_sha=SHA)
        self.assertEqual(result["sha"], SHA)

    def test_annotated_tag_returns_commit_not_tag_object(self):
        result = self.check(
            [
                self.identity(),
                self.ref("tag", TAG_SHA),
                {"object": {"type": "commit", "sha": SHA}},
                self.commit(),
            ]
        )
        self.assertEqual(result["sha"], SHA)

    def test_transfer_or_fork_identity_requires_review(self):
        with self.assertRaisesRegex(ValueError, "identity"):
            self.check([{"full_name": "other/checkout", "archived": False}])

    def test_archive_requires_review(self):
        with self.assertRaisesRegex(ValueError, "archived"):
            self.check([{"full_name": "actions/checkout", "archived": True}])

    def test_moving_or_missing_git_tag_refused(self):
        for git_sha in ("c" * 40, None):
            with self.subTest(git_sha=git_sha), self.assertRaisesRegex(ValueError, "disagree"):
                self.check([self.identity(), self.ref()], git_sha=git_sha)

    def test_non_commit_target_refused(self):
        with self.assertRaisesRegex(ValueError, "commit SHA"):
            self.check([self.identity(), self.ref("tree")])

    def test_cyclic_tag_refused(self):
        with self.assertRaisesRegex(ValueError, "cyclic"):
            self.check(
                [
                    self.identity(),
                    self.ref("tag", TAG_SHA),
                    {"object": self.ref("tag", TAG_SHA)["object"]},
                ]
            )

    def test_changed_reviewed_pin_refused(self):
        with self.assertRaisesRegex(ValueError, "reviewed pin"):
            self.check([self.identity(), self.ref(), self.commit()], expected_sha="c" * 40)

    def test_wrong_commit_response_refused(self):
        with self.assertRaisesRegex(ValueError, "upstream commit"):
            self.check([self.identity(), self.ref(), {"sha": "c" * 40}])

    def test_invalid_repository_tag_or_short_pin_refused_before_network(self):
        for args in (
            ("https://github.com/actions/checkout", "v4", None),
            ("actions/checkout", "main", None),
            ("actions/checkout", "v4", "abcdef"),
        ):
            with self.subTest(args=args), patch("resolve_action_ref.api") as read:
                with self.assertRaises(ValueError):
                    resolve(*args)
                read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
