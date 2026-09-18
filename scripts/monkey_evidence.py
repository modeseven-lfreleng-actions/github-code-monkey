# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Verify trusted evidence and accept bounded files from an untrusted proposal.

``verify --directory DIR --selection-sha256 HEX --guidance-sha256 HEX``
checks the exact bytes of ``selection.json`` and ``agents.md`` against
digests the select job published as job outputs, never against values
found in the downloaded artifact.

``accept --directory UNTRUSTED --output ACCEPTED`` copies the three
files the publisher reads from an author artifact, each bounded and
required to be a regular non-symlink file. Anything else in the
directory is ignored: never import from it, never execute from it.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
from pathlib import Path

MAX_SELECTION_BYTES = 16 * 1024 * 1024
MAX_GUIDANCE_BYTES = 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_BUNDLE_BYTES = 32 * 1024 * 1024
MAX_USAGE_BYTES = 1024 * 1024
SHA256_RE = re.compile(r"[0-9a-fA-F]{64}")

ACCEPTED_FILES: tuple[tuple[str, int, bool], ...] = (
    ("manifest.json", MAX_MANIFEST_BYTES, True),
    ("changes.bundle", MAX_BUNDLE_BYTES, False),
    ("usage.json", MAX_USAGE_BYTES, False),
)


def read_regular(path: Path, limit: int) -> bytes:
    """Read bounded bytes from a regular file without following a final symlink."""
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        # NONBLOCK lets fstat reject a FIFO without waiting for a writer.
        # Check the opened descriptor, not a prior stat that could race.
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        with os.fdopen(os.open(path.name, flags, dir_fd=directory_fd), "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"{path.name} must be a regular non-symlink file")
            if info.st_size > limit:
                raise ValueError(f"{path.name} exceeds the {limit}-byte limit")
            content = source.read(limit + 1)
            if len(content) > limit:
                raise ValueError(f"{path.name} exceeds the {limit}-byte limit")
            return content
    finally:
        os.close(directory_fd)


def verify(directory: Path, selection_sha256: str, guidance_sha256: str) -> None:
    """Authenticate the trusted evidence files against independently held hashes."""
    for name, digest, limit in (
        ("selection.json", selection_sha256, MAX_SELECTION_BYTES),
        ("agents.md", guidance_sha256, MAX_GUIDANCE_BYTES),
    ):
        if not SHA256_RE.fullmatch(digest):
            raise ValueError(f"trusted SHA-256 for {name} must contain 64 hex digits")
        content = read_regular(directory / name, limit)
        if hashlib.sha256(content).hexdigest() != digest.lower():
            raise ValueError(f"SHA-256 mismatch for {name}")


def accept(directory: Path, output: Path) -> list[str]:
    """Copy the bounded proposal files into a trusted directory; return their names."""
    output.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for name, limit, required in ACCEPTED_FILES:
        source = directory / name
        if not source.exists() and not source.is_symlink():
            if required:
                raise ValueError(f"proposal is missing {name}")
            continue
        content = read_regular(source, limit)
        (output / name).write_bytes(content)
        copied.append(name)
    return copied


def main(argv: list[str] | None = None) -> None:
    """Dispatch the verify and accept commands."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    verification = commands.add_parser("verify", help="check trusted evidence digests")
    verification.add_argument("--directory", type=Path, required=True)
    verification.add_argument("--selection-sha256", required=True)
    verification.add_argument("--guidance-sha256", required=True)
    acceptance = commands.add_parser("accept", help="copy bounded proposal files")
    acceptance.add_argument("--directory", type=Path, required=True)
    acceptance.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            verify(args.directory, args.selection_sha256, args.guidance_sha256)
        else:
            copied = accept(args.directory, args.output)
            print(f"accepted: {', '.join(copied)}")
    except (OSError, ValueError) as exc:
        message = ascii(str(exc)).replace("::", ": :").replace("##[", "# #[")
        parser.exit(1, f"evidence: {message}\n")


if __name__ == "__main__":
    main()
