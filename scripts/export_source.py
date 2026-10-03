#!/usr/bin/env python3
"""Export an allowlisted, checked ZIP. Does not rewrite the source project."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys
import zipfile

sys.dont_write_bytecode = True
from check_source import report
from source_policy import inspect_file, source_paths


def export(root: Path, destination: Path) -> int:
    root = root.resolve()
    destination = destination.resolve()
    export_root = (root / ".local" / "exports").resolve()
    if not root.is_dir():
        report(Path("."), "project-directory-missing")
        return 2
    # A custom archive may be outside the project or inside this runtime folder.
    # Never allow the command to overwrite or add a source file.
    if destination.is_relative_to(root) and not destination.is_relative_to(export_root):
        report(Path("."), "output-must-be-outside-source-or-in-local-exports")
        return 2
    if destination.suffix.lower() != ".zip" or destination.exists():
        report(Path("."), "output-must-be-new-zip")
        return 2
    contents = []
    failed = False
    for relative in source_paths(root):
        rules, data = inspect_file(root, relative)
        for rule in sorted(rules):
            report(relative, rule)
            failed = True
        if not rules and data is not None:
            contents.append((relative, data))
    if failed:
        return 1
    if not contents:
        report(Path("."), "empty-source-export")
        return 2
    created = False
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as stream:
            created = True
            with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for relative, data in contents:
                    info = zipfile.ZipInfo(relative.as_posix(), date_time=(2026, 1, 1, 0, 0, 0))
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.create_system = 3
                    mode = 0o755 if relative.suffix in {".sh", ".command"} else 0o644
                    info.external_attr = (0o100000 | mode) << 16
                    archive.writestr(info, data)
    except OSError:
        if created:
            destination.unlink(missing_ok=True)
        report(Path("."), "archive-write-failed")
        return 2
    print(f"Exported {len(contents)} source files: {destination}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, help="New .zip path. Default: PROJECT/.local/exports/source-UTC_TIMESTAMP.zip")
    args = parser.parse_args()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = args.output or args.root / ".local" / "exports" / f"source-{timestamp}.zip"
    return export(args.root, destination)


if __name__ == "__main__":
    raise SystemExit(main())
