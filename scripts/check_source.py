#!/usr/bin/env python3
"""Offline release check: output relative filenames and rule names, never matches."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

sys.dont_write_bytecode = True
from source_policy import inspect_file, source_paths


def report(relative: Path, rule: str) -> None:
    print(json.dumps({"path": relative.as_posix(), "rule": rule}, ensure_ascii=True))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--tracked", action="store_true", help="Check every Git-index path, including forbidden files; use in CI.")
    args = parser.parse_args()
    root = args.root.resolve()
    if not root.is_dir():
        report(Path("."), "project-directory-missing")
        return 2
    if args.tracked:
        try:
            result = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True)
        except (OSError, subprocess.CalledProcessError):
            report(Path("."), "git-index-unavailable")
            return 2
        paths = [Path(name) for name in result.stdout.decode("utf-8", errors="surrogateescape").split("\0") if name]
    else:
        paths = source_paths(root)
    failed = False
    for relative in paths:
        rules, _ = inspect_file(root, relative)
        for rule in sorted(rules):
            report(relative, rule)
            failed = True
    if not failed:
        report(Path("."), "source-check-passed")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
