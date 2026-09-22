#!/usr/bin/env python3
"""Run the Skill's complete local verification with the selected Python runtime."""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def snapshot(directory: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            relative = path.relative_to(ROOT).as_posix()
            result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def main() -> int:
    print(f"Python runtime: {sys.executable}", flush=True)
    print(f"Python version: {sys.version.split()[0]}", flush=True)

    print("[1/3] Compile", flush=True)
    subprocess.run(
        [sys.executable, "-m", "compileall", "-q", "scripts", "tests"],
        cwd=ROOT,
        check=True,
    )

    print("[2/3] Unit and end-to-end tests", flush=True)
    subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=ROOT,
        check=True,
    )

    print("[3/3] Example regeneration drift", flush=True)
    before = snapshot(ROOT / "examples")
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "regenerate_examples.py")],
        cwd=ROOT,
        check=True,
    )
    after = snapshot(ROOT / "examples")

    if before != after:
        changed = sorted(
            path for path in set(before) | set(after) if before.get(path) != after.get(path)
        )
        print("ERROR: regenerating examples changed committed outputs:", file=sys.stderr)
        for path in changed:
            print(f"  - {path}", file=sys.stderr)
        return 2

    print("Verification passed.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
