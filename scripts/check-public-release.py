from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from coderus.public_release import IGNORED_DIRECTORIES, scan_public_files


def candidate_files(root: Path, *, all_files: bool) -> list[Path]:
    if all_files:
        return sorted(
            path
            for path in root.rglob("*")
            if path.is_file() and not IGNORED_DIRECTORIES.intersection(path.parts)
        )
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        check=True,
        capture_output=True,
    )
    return sorted(root / item.decode("utf-8") for item in result.stdout.split(b"\0") if item)


def main() -> int:
    parser = argparse.ArgumentParser(description="Check files intended for a public release")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--all-files",
        action="store_true",
        help="scan every non-cache file instead of Git candidate files",
    )
    args = parser.parse_args()
    root = args.root.resolve()
    findings = scan_public_files(root, candidate_files(root, all_files=args.all_files))
    if findings:
        print("Public release scan failed:")
        for finding in sorted(set(findings)):
            print(f"- {finding}")
        return 1
    print("Public release scan passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
