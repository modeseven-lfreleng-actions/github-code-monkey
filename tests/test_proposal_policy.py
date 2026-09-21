# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Pure proposal rules: paths, modes, messages, trailers and PR text."""

from __future__ import annotations

import sys
import unittest
from importlib import import_module
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
policy = import_module("proposal_policy")

IDENTITY = policy.Identity(
    coauthor="Claude <noreply@anthropic.com>",
    bot_login="code-monkey[bot]",
    bot_email="1+code-monkey[bot]@users.noreply.github.com",
)
SIGN_OFF = (
    "Signed-off-by: code-monkey[bot] <1+code-monkey[bot]@users.noreply.github.com>"
)
COAUTHOR = "Co-authored-by: Claude <noreply@anthropic.com>"


class ParseTitleLimitTest(unittest.TestCase):
    """``parse_title_limit`` reads the gitlint section or falls back."""

    def test_default_without_file(self) -> None:
        """No .gitlint means the organisation default."""
        self.assertEqual(policy.parse_title_limit(None), policy.DEFAULT_TITLE_LIMIT)
        self.assertEqual(policy.parse_title_limit(""), 50)

    def test_reads_section(self) -> None:
        """The value under ``[title-max-length]`` wins, whitespace tolerated."""
        text = "[general]\nverbosity=3\n\n[title-max-length]\n  line-length = 72 \n"
        self.assertEqual(policy.parse_title_limit(text), 72)

    def test_unrelated_section_ignored(self) -> None:
        """A ``line-length`` under another section does not count."""
        text = "[body-max-line-length]\nline-length=100\n"
        self.assertEqual(policy.parse_title_limit(text), 50)

    def test_non_numeric_ignored(self) -> None:
        """A non-integer value falls back to the default."""
        text = "[title-max-length]\nline-length=lots\n"
        self.assertEqual(policy.parse_title_limit(text), 50)


class SafePathTest(unittest.TestCase):
    """``safe_path`` keeps changes inside the tree and out of .git."""

    def test_rejects(self) -> None:
        """Escapes, absolute paths, git metadata and odd separators fail."""
        for bad in (
            "",
            "/abs",
            "a/../b",
            "../x",
            ".git/config",
            "a/.git/hooks",
            "a//b",
            "a\\b",
            "./x",
            "a/",
            "a\0b",
        ):
            with self.subTest(bad=bad):
                self.assertFalse(policy.safe_path(bad))

    def test_accepts(self) -> None:
        """Ordinary nested paths, including dot-directories, pass."""
        for good in ("src/x.py", ".github/workflows/ci.yaml", "a.b", ".gitignore"):
            with self.subTest(good=good):
                self.assertTrue(policy.safe_path(good))


class ProtectedTest(unittest.TestCase):
    """``protected`` names the files the agent must not touch."""

    def test_protected_paths(self) -> None:
        """Exact names and licence prefixes are protected."""
        for path in (
            "AGENTS.md",
            "REUSE.toml",
            ".gitlint",
            "LICENSE",
            "LICENSE.txt",
            "LICENSES/Apache-2.0.txt",
        ):
            with self.subTest(path=path):
                self.assertTrue(policy.protected(path))

    def test_unprotected_paths(self) -> None:
        """Prefix matching applies to the path start only."""
        for path in ("docs/LICENSE-notes.md", "docs/AGENTS.md", "src/agents.md"):
            with self.subTest(path=path):
                self.assertFalse(policy.protected(path))


class CheckChangeModeTest(unittest.TestCase):
    """``check_change_mode`` refuses anything the API cannot replay."""

    def test_rejections_name_the_path(self) -> None:
        """Each unreplayable change raises a Rejection mentioning the path."""
        cases = (
            ("T", "100644", "120000", "type change"),
            ("A", "000000", "120000", "symlink"),
            ("A", "000000", "160000", "submodule"),
            ("A", "000000", "100755", "executable"),
            ("M", "100755", "100755", "executable"),
            ("M", "100755", "100644", "mode change"),
            ("A", "000000", "040000", "unsupported mode"),
        )
        for status, old, new, fragment in cases:
            with (
                self.subTest(status=status, new=new),
                self.assertRaises(policy.Rejection) as caught,
            ):
                policy.check_change_mode("bin/tool", status, old, new)
            self.assertIn("bin/tool", str(caught.exception))
            self.assertIn(fragment, str(caught.exception))

    def test_regular_changes_pass(self) -> None:
        """Additions and modifications of regular files are fine."""
        policy.check_change_mode("a.py", "A", "000000", "100644")
        policy.check_change_mode("a.py", "M", "100644", "100644")


class SplitMessageTest(unittest.TestCase):
    """``split_message`` separates headline, body and trailer block."""

    def test_full_split(self) -> None:
        """Headline, body and trailers come apart; trailing blanks vanish."""
        message = (
            "Fix(x): Do a thing\n\nFirst paragraph.\n\nSecond one.\n\n"
            "Co-authored-by: A <a@x>\nSigned-off-by: B <b@x>\n\n\n"
        )
        headline, body, trailers = policy.split_message(message)
        self.assertEqual(headline, "Fix(x): Do a thing")
        self.assertEqual(body, ["First paragraph.", "", "Second one."])
        self.assertEqual(
            trailers, ["Co-authored-by: A <a@x>", "Signed-off-by: B <b@x>"]
        )

    def test_headline_only(self) -> None:
        """A bare subject has no body and no trailers."""
        self.assertEqual(policy.split_message("Fix: X\n"), ("Fix: X", [], []))

    def test_trailers_only(self) -> None:
        """A subject followed directly by trailers has an empty body."""
        headline, body, trailers = policy.split_message(
            "Fix: X\n\nSigned-off-by: B <b@x>"
        )
        self.assertEqual((headline, body), ("Fix: X", []))
        self.assertEqual(trailers, ["Signed-off-by: B <b@x>"])

    def test_missing_blank_line_rejected(self) -> None:
        """Body text directly under the subject is a rejection."""
        with self.assertRaises(policy.Rejection):
            policy.split_message("Fix: X\nbody right away\n")

    def test_trailer_glued_to_subject_rejected(self) -> None:
        """A trailer directly under the subject is not a trailer; it is a rejection."""
        with self.assertRaisesRegex(policy.Rejection, "blank line"):
            policy.split_message(f"Fix: X\n{SIGN_OFF}\n")

    def test_body_line_looking_like_trailer_inside_body(self) -> None:
        """Only the final block counts as trailers; a colon line mid-body stays."""
        headline, body, trailers = policy.split_message(
            "Fix: X\n\nNote: this stays\n\nMore text.\n"
        )
        self.assertEqual(headline, "Fix: X")
        self.assertEqual(body, ["Note: this stays", "", "More text."])
        self.assertEqual(trailers, [])


class CheckHeadlineTest(unittest.TestCase):
    """``check_headline`` applies the subject rules."""

    def test_every_type_passes(self) -> None:
        """Each allowed type passes bare, with scope, and with a bang."""
        for kind in policy.COMMIT_TYPES:
            for headline in (
                f"{kind}: Do something",
                f"{kind}(scope): Do something",
                f"{kind}(scope)!: Do something",
                f"{kind}!: Do something",
            ):
                with self.subTest(headline=headline):
                    policy.check_headline(headline, 50)

    def test_lowercase_type_fails(self) -> None:
        """A lowercase type lacks the required capitalisation."""
        with self.assertRaisesRegex(policy.Rejection, "capitalised"):
            policy.check_headline("fix: lower", 50)

    def test_unknown_type_fails(self) -> None:
        """A type outside the list is rejected."""
        with self.assertRaises(policy.Rejection):
            policy.check_headline("Bugfix: nope", 50)

    def test_missing_space_fails(self) -> None:
        """The colon must be followed by a space and text."""
        with self.assertRaises(policy.Rejection):
            policy.check_headline("Fix:nope", 50)
        with self.assertRaises(policy.Rejection):
            policy.check_headline("Fix: ", 50)

    def test_over_limit_fails(self) -> None:
        """A subject over the limit is rejected; at the limit passes."""
        exact = "Fix: " + "x" * 45
        self.assertEqual(len(exact), 50)
        policy.check_headline(exact, 50)
        with self.assertRaisesRegex(policy.Rejection, "exceeds 50"):
            policy.check_headline(exact + "y", 50)

    def test_trailing_punctuation_fails(self) -> None:
        """A subject ending in punctuation is rejected."""
        for mark in ".;:!?,":
            with self.subTest(mark=mark), self.assertRaises(policy.Rejection):
                policy.check_headline(f"Fix: Thing{mark}", 50)


class CheckBodyTest(unittest.TestCase):
    """``check_body`` wraps at 72 unless a URL is present."""

    def test_long_line_fails(self) -> None:
        """A 73-character line without a URL is rejected."""
        with self.assertRaises(policy.Rejection):
            policy.check_body(["", "x" * 73])

    def test_limit_passes(self) -> None:
        """Exactly 72 characters is fine."""
        policy.check_body(["", "x" * 72])

    def test_url_line_exempt(self) -> None:
        """A long line carrying a URL passes."""
        policy.check_body(["See https://example.com/" + "a" * 80])
        policy.check_body(["See http://example.com/" + "a" * 80])


class LogSafeTest(unittest.TestCase):
    """``log_safe`` neutralises workflow-command markers and line breaks."""

    def test_markers_and_newlines(self) -> None:
        """A spoofed annotation flattens to harmless text."""
        hostile = "fine\n::error::spoofed\r\n##[group]x\n::stop-commands::t"
        safe = policy.log_safe(hostile)
        self.assertNotIn("\n", safe)
        self.assertNotIn("::", safe)
        self.assertNotIn("##[", safe)
        self.assertIn("spoofed", safe)


class CommentSafeTest(unittest.TestCase):
    """``comment_safe`` keeps an outcome comment to one quiet line."""

    def test_flattens_and_defuses_mentions(self) -> None:
        """Newlines collapse and an @-mention no longer pings."""
        safe = policy.comment_safe("first\n\n## heading\n@alice please")
        self.assertNotIn("\n", safe)
        self.assertNotIn("@alice", safe)
        self.assertIn("alice", safe)


class CoauthorForTest(unittest.TestCase):
    """``coauthor_for`` maps a model prefix to its trailer identity."""

    MAPPING: dict[str, object] = {
        "claude-": "Claude <noreply@anthropic.com>",
        "gpt-": "ChatGPT <chatgpt@openai.com>",
        "broken-": 42,
    }

    def test_prefix_mapping(self) -> None:
        """The first matching prefix wins."""
        self.assertEqual(
            policy.coauthor_for("claude-opus-5", self.MAPPING),
            "Claude <noreply@anthropic.com>",
        )
        self.assertEqual(
            policy.coauthor_for("gpt-5", self.MAPPING), "ChatGPT <chatgpt@openai.com>"
        )

    def test_unknown_model_raises(self) -> None:
        """No mapping is an operational failure, not a rejection."""
        with self.assertRaises(policy.PublishError):
            policy.coauthor_for("gemini-3", self.MAPPING)

    def test_non_string_trailer_ignored(self) -> None:
        """A malformed mapping value is skipped rather than returned."""
        with self.assertRaises(policy.PublishError):
            policy.coauthor_for("broken-1", self.MAPPING)


class ComposeTrailersTest(unittest.TestCase):
    """``compose_trailers`` closes the block with co-author and sign-off."""

    def test_appends_both_when_absent(self) -> None:
        """An empty block gains both trailers in order."""
        self.assertEqual(policy.compose_trailers([], IDENTITY), [COAUTHOR, SIGN_OFF])

    def test_no_duplicate_coauthor(self) -> None:
        """An existing co-author with the same address is kept, not doubled."""
        existing = "Co-authored-by: Claude Opus <noreply@anthropic.com>"
        self.assertEqual(
            policy.compose_trailers([existing], IDENTITY), [existing, SIGN_OFF]
        )

    def test_similar_address_does_not_suppress_model_trailer(self) -> None:
        """Only an exact bracketed address counts as the model already present."""
        lookalike = "Co-authored-by: Someone <xnoreply@anthropic.com>"
        self.assertEqual(
            policy.compose_trailers([lookalike], IDENTITY),
            [lookalike, COAUTHOR, SIGN_OFF],
        )

    def test_address_match_is_case_insensitive(self) -> None:
        """Mail addresses compare case-insensitively."""
        existing = "Co-authored-by: Claude <NoReply@Anthropic.com>"
        self.assertEqual(
            policy.compose_trailers([existing], IDENTITY), [existing, SIGN_OFF]
        )

    def test_other_coauthor_kept_and_model_added(self) -> None:
        """A human co-author is kept and the model's trailer still added."""
        human = "Co-authored-by: Human <human@example.com>"
        self.assertEqual(
            policy.compose_trailers([human], IDENTITY), [human, COAUTHOR, SIGN_OFF]
        )

    def test_bot_sign_off_moved_to_end(self) -> None:
        """A sign-off the agent already added moves after the co-author."""
        self.assertEqual(
            policy.compose_trailers([SIGN_OFF, "Issue-ID: X-1"], IDENTITY),
            ["Issue-ID: X-1", COAUTHOR, SIGN_OFF],
        )

    def test_other_trailers_preserved(self) -> None:
        """Unrelated trailers survive in order; another sign-off stays."""
        other = "Signed-off-by: Human <human@example.com>"
        self.assertEqual(
            policy.compose_trailers(["Issue-ID: X-1", other], IDENTITY),
            ["Issue-ID: X-1", other, COAUTHOR, SIGN_OFF],
        )


class ComposeMessageTest(unittest.TestCase):
    """``compose_message`` returns the headline and the composed body."""

    def test_with_body(self) -> None:
        """Body text, a blank line, then the trailer block.

        The separator line under the subject must not leak into the body:
        createCommitOnBranch adds its own blank line between headline and
        body, so a leading newline would produce two.
        """
        message = "Fix(readme): Correct typo\n\nExplain why.\nSecond line.\n"
        headline, body = policy.compose_message(message, IDENTITY, 50)
        self.assertEqual(headline, "Fix(readme): Correct typo")
        self.assertEqual(body, f"Explain why.\nSecond line.\n\n{COAUTHOR}\n{SIGN_OFF}")

    def test_without_body(self) -> None:
        """No body means the trailer block alone."""
        headline, body = policy.compose_message("Docs: Note\n", IDENTITY, 50)
        self.assertEqual(headline, "Docs: Note")
        self.assertEqual(body, f"{COAUTHOR}\n{SIGN_OFF}")

    def test_existing_trailers_folded(self) -> None:
        """An agent-added sign-off is repositioned after the co-author."""
        message = f"Fix: X\n\nWhy.\n\n{SIGN_OFF}\n"
        _, body = policy.compose_message(message, IDENTITY, 50)
        self.assertEqual(body, f"Why.\n\n{COAUTHOR}\n{SIGN_OFF}")

    def test_rejections_propagate(self) -> None:
        """Headline and body rules apply through compose_message."""
        with self.assertRaises(policy.Rejection):
            policy.compose_message("fix: lower\n", IDENTITY, 50)
        with self.assertRaises(policy.Rejection):
            policy.compose_message("Fix: X\n\n" + "y" * 80 + "\n", IDENTITY, 50)


class CheckPullRequestTextTest(unittest.TestCase):
    """``check_pull_request_text`` validates the manifest's title and body."""

    def test_missing_title_or_body(self) -> None:
        """Absent, blank or non-string title or body are rejected."""
        for title, body in (
            (None, "Closes #7"),
            ("  ", "Closes #7"),
            ("T", None),
            ("T", ""),
            (7, "Closes #7"),
            ("T", ["x"]),
        ):
            with (
                self.subTest(title=title, body=body),
                self.assertRaises(policy.Rejection),
            ):
                policy.check_pull_request_text(
                    title, body, repository="owner/repo", issue=7, single_headline=None
                )

    def test_single_commit_title_must_match(self) -> None:
        """One commit means the title equals its subject, after stripping."""
        title, body = policy.check_pull_request_text(
            "  Fix: X ",
            "Closes #7\n\n",
            repository="owner/repo",
            issue=7,
            single_headline="Fix: X",
        )
        self.assertEqual((title, body), ("Fix: X", "Closes #7"))
        with self.assertRaisesRegex(policy.Rejection, "must equal the subject"):
            policy.check_pull_request_text(
                "Fix: Y",
                "Closes #7",
                repository="owner/repo",
                issue=7,
                single_headline="Fix: X",
            )

    def test_multi_commit_any_title(self) -> None:
        """Without a single headline the title is free."""
        title, _ = policy.check_pull_request_text(
            "Anything goes",
            "Fixes #7",
            repository="owner/repo",
            issue=7,
            single_headline=None,
        )
        self.assertEqual(title, "Anything goes")

    def test_closing_keywords(self) -> None:
        """Closes, Fixes and Resolves, any case, bare or for this repository."""
        for body in (
            "Closes #7",
            "text\nfixes #7\nmore",
            "  RESOLVES owner/repo#7",
            "Resolves owner.name/re-po#7.",
        ):
            with self.subTest(body=body):
                repository = "owner.name/re-po" if "re-po" in body else "owner/repo"
                policy.check_pull_request_text(
                    "T", body, repository=repository, issue=7, single_headline=None
                )

    def test_closing_keyword_inside_code_does_not_count(self) -> None:
        """A closing line in a fence or code span closes nothing on GitHub."""
        for body in (
            "Example:\n\n```text\nCloses #7\n```\n",
            "Example:\n\n~~~\nCloses #7\n~~~\n",
            "Write `Closes #7` at the end.",
        ):
            with (
                self.subTest(body=body),
                self.assertRaisesRegex(policy.Rejection, "Closes #7"),
            ):
                policy.check_pull_request_text(
                    "T", body, repository="owner/repo", issue=7, single_headline=None
                )
        policy.check_pull_request_text(
            "T",
            "```text\nexample\n```\n\nCloses #7\n",
            repository="owner/repo",
            issue=7,
            single_headline=None,
        )

    def test_closing_directive_stays_on_one_line_outside_code(self) -> None:
        """A keyword split from its reference, or indented as code, closes nothing."""
        for body in ("Closes\n#7", "    Closes #7", "Closes\t\n#7"):
            with (
                self.subTest(body=body),
                self.assertRaisesRegex(policy.Rejection, "Closes #7"),
            ):
                policy.check_pull_request_text(
                    "T", body, repository="owner/repo", issue=7, single_headline=None
                )
        policy.check_pull_request_text(
            "T", "   Closes #7", repository="owner/repo", issue=7, single_headline=None
        )

    def test_closing_reference_to_another_repository_rejects(self) -> None:
        """A qualified reference must name the selected repository."""
        with self.assertRaisesRegex(policy.Rejection, "Closes #7"):
            policy.check_pull_request_text(
                "T",
                "Closes unrelated/repo#7",
                repository="owner/repo",
                issue=7,
                single_headline=None,
            )

    def test_closing_line_must_start_line(self) -> None:
        """The keyword must open the line and the number must be exact."""
        for body in (
            "This closes #7",
            "Closes #70",
            "Closes #8",
            "Closes 7",
            "Closes: #7",
        ):
            with (
                self.subTest(body=body),
                self.assertRaisesRegex(policy.Rejection, "Closes #7"),
            ):
                policy.check_pull_request_text(
                    "T", body, repository="owner/repo", issue=7, single_headline=None
                )

    def test_title_length(self) -> None:
        """A title over 256 characters is rejected."""
        with self.assertRaisesRegex(policy.Rejection, "exceeds 256"):
            policy.check_pull_request_text(
                "x" * 257,
                "Closes #7",
                repository="owner/repo",
                issue=7,
                single_headline=None,
            )
        policy.check_pull_request_text(
            "x" * 256,
            "Closes #7",
            repository="owner/repo",
            issue=7,
            single_headline=None,
        )


class PullRequestBodySizeTest(unittest.TestCase):
    """``check_pull_request_body_size`` refuses bodies GitHub would reject."""

    def test_limit(self) -> None:
        """At the limit passes; one over is a rejection."""
        policy.check_pull_request_body_size("x" * policy.MAX_PR_BODY)
        with self.assertRaisesRegex(policy.Rejection, "exceeds"):
            policy.check_pull_request_body_size("x" * (policy.MAX_PR_BODY + 1))


class ProvenanceBlockTest(unittest.TestCase):
    """``provenance_block`` records where the change came from."""

    def test_contents(self) -> None:
        """Issue, model, run, base and the commands table appear."""
        block = policy.provenance_block(
            model="claude-opus-5",
            run_url="https://example/run/1",
            issue_url="https://github.com/o/r/issues/7",
            base_sha="d" * 40,
            commands=[
                {"command": "uv run pytest | tee `log`", "exit_code": 0},
                {"command": "make", "exit_code": None},
            ],
        )
        self.assertIn("- Issue: https://github.com/o/r/issues/7", block)
        self.assertIn("- Model: `claude-opus-5` via GitHub Copilot CLI", block)
        self.assertIn("- Run: https://example/run/1", block)
        self.assertIn(f"- Base: `{'d' * 40}`", block)
        self.assertIn("| Command | Exit |", block)
        self.assertIn("| `uv run pytest \\| tee 'log'` | 0 |", block)
        self.assertIn("| `make` | ? |", block)
        self.assertTrue(block.startswith("\n---\n"))
        self.assertTrue(block.endswith("</details>\n"))

    def test_without_commands_or_run(self) -> None:
        """No commands means no table; an empty run URL renders n/a."""
        block = policy.provenance_block(
            model="m", run_url="", issue_url="u", base_sha="e" * 40, commands=[]
        )
        self.assertNotIn("| Command |", block)
        self.assertIn("- Run: n/a", block)


class IdentityTest(unittest.TestCase):
    """``Identity`` renders the sign-off trailer."""

    def test_sign_off(self) -> None:
        """The DCO trailer uses the login and email."""
        self.assertEqual(IDENTITY.sign_off, SIGN_OFF)


if __name__ == "__main__":
    unittest.main()
