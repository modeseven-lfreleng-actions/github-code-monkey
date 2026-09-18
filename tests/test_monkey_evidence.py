# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Evidence verification and bounded acceptance of proposal files."""

from __future__ import annotations

import hashlib
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from importlib import import_module
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
evidence = import_module("monkey_evidence")

SELECTION = b'{"schema": 1, "issues": []}\n'
GUIDANCE = b"# AGENTS\n\nGuidance.\n"


def digest(content: bytes) -> str:
    """Hex SHA-256 of some bytes."""
    return hashlib.sha256(content).hexdigest()


class EvidenceDirectory(unittest.TestCase):
    """Base class providing a temporary directory per test."""

    def setUp(self) -> None:
        """Create the scratch directory."""
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.directory = Path(holder.name)

    def write_evidence(self) -> None:
        """Lay down a correct selection.json and agents.md pair."""
        (self.directory / "selection.json").write_bytes(SELECTION)
        (self.directory / "agents.md").write_bytes(GUIDANCE)


class VerifyTest(EvidenceDirectory):
    """``verify`` authenticates both evidence files."""

    def test_correct_digests_pass(self) -> None:
        """Matching digests, in either case, verify silently."""
        self.write_evidence()
        evidence.verify(self.directory, digest(SELECTION), digest(GUIDANCE))
        evidence.verify(
            self.directory, digest(SELECTION).upper(), digest(GUIDANCE).upper()
        )

    def test_mismatch_fails(self) -> None:
        """A wrong digest on either file names that file."""
        self.write_evidence()
        wrong = digest(b"other")
        with self.assertRaisesRegex(ValueError, "mismatch for selection.json"):
            evidence.verify(self.directory, wrong, digest(GUIDANCE))
        with self.assertRaisesRegex(ValueError, "mismatch for agents.md"):
            evidence.verify(self.directory, digest(SELECTION), wrong)

    def test_non_hex_digest_fails(self) -> None:
        """A digest that is not 64 hex characters is refused before reading."""
        self.write_evidence()
        for bad in ("", "abc", "g" * 64, digest(SELECTION) + "0"):
            with (
                self.subTest(bad=bad),
                self.assertRaisesRegex(ValueError, "64 hex digits"),
            ):
                evidence.verify(self.directory, bad, digest(GUIDANCE))

    def test_missing_file_fails(self) -> None:
        """A missing evidence file is an OSError, not a silent pass."""
        (self.directory / "selection.json").write_bytes(SELECTION)
        with self.assertRaises(OSError):
            evidence.verify(self.directory, digest(SELECTION), digest(GUIDANCE))

    def test_oversize_file_fails(self) -> None:
        """A file over the limit fails even when its digest would match."""
        big = b"x" * 64
        (self.directory / "selection.json").write_bytes(SELECTION)
        (self.directory / "agents.md").write_bytes(big)
        with (
            patch.object(evidence, "MAX_GUIDANCE_BYTES", 32),
            self.assertRaisesRegex(ValueError, "exceeds the 32-byte limit"),
        ):
            evidence.verify(self.directory, digest(SELECTION), digest(big))

    def test_exact_limit_passes(self) -> None:
        """A file exactly at the limit is accepted."""
        exact = b"y" * 32
        (self.directory / "selection.json").write_bytes(SELECTION)
        (self.directory / "agents.md").write_bytes(exact)
        with patch.object(evidence, "MAX_GUIDANCE_BYTES", 32):
            evidence.verify(self.directory, digest(SELECTION), digest(exact))

    def test_symlink_refused(self) -> None:
        """agents.md as a symlink to correct content is still refused."""
        (self.directory / "selection.json").write_bytes(SELECTION)
        target = self.directory / "real.md"
        target.write_bytes(GUIDANCE)
        os.symlink(target, self.directory / "agents.md")
        with self.assertRaises((OSError, ValueError)):
            evidence.verify(self.directory, digest(SELECTION), digest(GUIDANCE))

    def test_fifo_refused(self) -> None:
        """agents.md as a FIFO is refused without blocking."""
        (self.directory / "selection.json").write_bytes(SELECTION)
        os.mkfifo(self.directory / "agents.md")
        with self.assertRaisesRegex(ValueError, "regular non-symlink file"):
            evidence.verify(self.directory, digest(SELECTION), digest(GUIDANCE))


class AcceptTest(EvidenceDirectory):
    """``accept`` copies only the three bounded files."""

    def setUp(self) -> None:
        """Split the scratch directory into untrusted input and trusted output."""
        super().setUp()
        self.untrusted = self.directory / "untrusted"
        self.untrusted.mkdir()
        self.output = self.directory / "accepted" / "deep"

    def test_copies_all_three(self) -> None:
        """Every accepted file is copied byte for byte; nothing else is."""
        (self.untrusted / "manifest.json").write_bytes(b'{"outcome": "abstain"}')
        (self.untrusted / "changes.bundle").write_bytes(b"# v2 git bundle\n\x00")
        (self.untrusted / "usage.json").write_bytes(b"{}")
        (self.untrusted / "evil.py").write_bytes(b"import os\n")
        (self.untrusted / "sitecustomize.py").write_bytes(b"")
        copied = evidence.accept(self.untrusted, self.output)
        self.assertEqual(copied, ["manifest.json", "changes.bundle", "usage.json"])
        self.assertEqual(
            sorted(p.name for p in self.output.iterdir()),
            ["changes.bundle", "manifest.json", "usage.json"],
        )
        self.assertEqual(
            (self.output / "changes.bundle").read_bytes(), b"# v2 git bundle\n\x00"
        )
        self.assertFalse((self.output / "evil.py").exists())

    def test_missing_manifest_raises(self) -> None:
        """manifest.json is required."""
        (self.untrusted / "changes.bundle").write_bytes(b"x")
        with self.assertRaisesRegex(ValueError, "missing manifest.json"):
            evidence.accept(self.untrusted, self.output)

    def test_optional_files_may_be_absent(self) -> None:
        """A manifest alone is a complete acceptance."""
        (self.untrusted / "manifest.json").write_bytes(b"{}")
        self.assertEqual(
            evidence.accept(self.untrusted, self.output), ["manifest.json"]
        )
        self.assertEqual([p.name for p in self.output.iterdir()], ["manifest.json"])

    def test_symlinked_bundle_refused(self) -> None:
        """An optional file present as a symlink is an error, not skipped."""
        (self.untrusted / "manifest.json").write_bytes(b"{}")
        target = self.directory / "outside.bundle"
        target.write_bytes(b"secret")
        os.symlink(target, self.untrusted / "changes.bundle")
        with self.assertRaises((OSError, ValueError)):
            evidence.accept(self.untrusted, self.output)
        self.assertFalse((self.output / "changes.bundle").exists())

    def test_dangling_symlink_refused(self) -> None:
        """A dangling symlink for a required file is not treated as missing."""
        os.symlink(self.directory / "nowhere", self.untrusted / "manifest.json")
        with self.assertRaises(OSError):
            evidence.accept(self.untrusted, self.output)

    def test_oversize_bundle_refused(self) -> None:
        """A bundle over the cap is refused."""
        (self.untrusted / "manifest.json").write_bytes(b"{}")
        (self.untrusted / "changes.bundle").write_bytes(b"b" * 100)
        with (
            patch.object(
                evidence,
                "ACCEPTED_FILES",
                (("manifest.json", 1024, True), ("changes.bundle", 50, False)),
            ),
            self.assertRaisesRegex(ValueError, "exceeds the 50-byte limit"),
        ):
            evidence.accept(self.untrusted, self.output)


class MainTest(EvidenceDirectory):
    """The command line dispatches and reports failures with a prefix."""

    def test_verify_command(self) -> None:
        """``verify`` succeeds quietly with correct digests."""
        self.write_evidence()
        evidence.main(
            [
                "verify",
                "--directory",
                str(self.directory),
                "--selection-sha256",
                digest(SELECTION),
                "--guidance-sha256",
                digest(GUIDANCE),
            ]
        )

    def test_verify_failure_exits_one(self) -> None:
        """A mismatch exits 1 with the ``evidence:`` prefix on stderr."""
        self.write_evidence()
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
            evidence.main(
                [
                    "verify",
                    "--directory",
                    str(self.directory),
                    "--selection-sha256",
                    digest(b"nope"),
                    "--guidance-sha256",
                    digest(GUIDANCE),
                ]
            )
        self.assertEqual(caught.exception.code, 1)
        self.assertTrue(stderr.getvalue().startswith("evidence: "))
        self.assertIn("mismatch for selection.json", stderr.getvalue())

    def test_accept_command(self) -> None:
        """``accept`` copies and lists the files it took."""
        untrusted = self.directory / "u"
        untrusted.mkdir()
        (untrusted / "manifest.json").write_bytes(b"{}")
        (untrusted / "usage.json").write_bytes(b"{}")
        output = self.directory / "o"
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            evidence.main(
                ["accept", "--directory", str(untrusted), "--output", str(output)]
            )
        self.assertEqual(stdout.getvalue(), "accepted: manifest.json, usage.json\n")
        self.assertTrue((output / "usage.json").is_file())

    def test_accept_failure_exits_one(self) -> None:
        """A missing manifest exits 1 with the prefix."""
        untrusted = self.directory / "u"
        untrusted.mkdir()
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
            evidence.main(
                [
                    "accept",
                    "--directory",
                    str(untrusted),
                    "--output",
                    str(self.directory / "o"),
                ]
            )
        self.assertEqual(caught.exception.code, 1)
        self.assertIn(
            "evidence: 'proposal is missing manifest.json'", stderr.getvalue()
        )

    def test_missing_command_is_usage_error(self) -> None:
        """No subcommand is an argparse error (exit 2)."""
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            evidence.main([])
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
