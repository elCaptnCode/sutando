#!/usr/bin/env python3
"""`--assert-head` refuses to let a review be posted against a moved head.

A preflight runs before you compose; the head can move while you write. The
gap is longest for the findings worth the most care, which is the wrong way
round. Peer report: a finding posted 29 seconds after the fix that answered it,
with the verified sha correctly quoted in the comment body — naming the head is
not the same as re-reading it.

Run: python3 tests/review-preflight-assert-head.test.py
"""
import importlib.util
import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("_rp", REPO / "scripts" / "review-preflight.py")
rp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rp)

HEAD = "f89fec9ef92f45455fd025f35773b0bff9ff6474"


def _run(argv, head):
    """main() with current_head pinned; returns (rc, stdout, stderr)."""
    real = rp.current_head
    rp.current_head = lambda pr, runner=None, repo=None: head
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            rc = rp.main(argv)
    finally:
        rp.current_head = real
    return rc, out.getvalue(), err.getvalue()


class AssertHead(unittest.TestCase):
    def test_unchanged_head_passes(self):
        rc, out, _ = _run(["3774", "--assert-head", HEAD], HEAD)
        self.assertEqual(rc, 0)
        self.assertIn("unchanged", out)

    def test_a_prefix_of_the_head_is_accepted(self):
        """Short shas are what a person actually copies out of a previous run."""
        rc, out, _ = _run(["3774", "--assert-head", HEAD[:9]], HEAD)
        self.assertEqual(rc, 0)
        self.assertIn("unchanged", out)

    def test_a_TOO_SHORT_sha_is_refused_not_quietly_accepted(self):
        """The prefix compare is what makes short shas usable, and it is also what
        would let `-​-assert-head f` match five sixteenths of all heads. A guard
        whose weak input silently passes is the failure this guard exists for."""
        for short in ("f", "f89", "f89fec"):
            rc, _, err = _run(["3774", "--assert-head", short], HEAD)
            self.assertEqual(rc, 2, f"{short!r} must be a usage error")
            self.assertIn("too short", err)

    def test_seven_characters_is_accepted(self):
        """The boundary itself, so the cutoff cannot drift without a test failing."""
        rc, out, _ = _run(["3774", "--assert-head", HEAD[:7]], HEAD)
        self.assertEqual(rc, 0)
        self.assertIn("unchanged", out)

    def test_MOVED_head_fails(self):
        rc, _, err = _run(["3774", "--assert-head", "637d3d37b"], HEAD)
        self.assertEqual(rc, 3)
        self.assertIn("MOVED", err)

    def test_UNREADABLE_head_fails_rather_than_passing(self):
        """The failure that matters: not-read must never read as not-moved."""
        rc, _, err = _run(["3774", "--assert-head", HEAD], None)
        self.assertEqual(rc, 3)
        self.assertIn("could not read", err)

    def test_missing_pr_number_is_a_usage_error_not_a_pass(self):
        rc, _, err = _run(["--assert-head", HEAD], HEAD)
        self.assertEqual(rc, 2)
        self.assertIn("needs the PR number", err)

    def test_without_the_flag_the_guide_still_renders(self):
        """The flag is additive: the normal preflight path must be untouched."""
        rc, out, _ = _run(["3774"], HEAD)
        self.assertEqual(rc, 0)
        self.assertIn("Lessons", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
