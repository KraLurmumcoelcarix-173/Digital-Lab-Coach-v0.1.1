"""
Build the student release zip (dist/DigitalLabCoach.zip).

Ships everything a student or instructor needs at runtime and nothing
else: no tests, no git metadata, no virtualenv, no caches. Sample
circuits, manifests, and the official-test defaults are runtime data and
stay in. Upload the result as a GitHub release ASSET with this exact
filename so the README's Download button URL
(<repo>/releases/latest/download/DigitalLabCoach.zip) stays stable
across versions.

Usage:  uv run python scripts/make_release_zip.py [--out dist]
"""

from __future__ import annotations

import argparse
import re
import zipfile
from pathlib import Path

INCLUDE_DIRS = ["dlc", "prompts", "proxy", "docs", "data"]
INCLUDE_FILES = [
    "README.md", "LICENSE", "pyproject.toml", "uv.lock",
    ".python-version", ".env.example",
    "START_HERE.bat", "start.sh", "UNINSTALL.bat", "uninstall.sh",
]
EXCLUDE_DIR_NAMES = {"__pycache__", ".pytest_cache", ".git", ".venv",
                     "tests","screenshots"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo"}


def _version(root: Path) -> str:
    m = re.search(r'^version\s*=\s*"([^"]+)"',
                  (root / "pyproject.toml").read_text(encoding="utf-8"),
                  re.MULTILINE)
    return m.group(1) if m else "0.0.0"


def _want(path: Path) -> bool:
    if any(part in EXCLUDE_DIR_NAMES for part in path.parts):
        return False
    return path.suffix not in EXCLUDE_SUFFIXES


def build(root: Path, out_dir: Path) -> Path:
    version = _version(root)
    prefix = f"DigitalLabCoach-{version}/"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "DigitalLabCoach.zip"
    files: list[Path] = []
    for name in INCLUDE_FILES:
        p = root / name
        if p.is_file():
            files.append(p)
    for d in INCLUDE_DIRS:
        for p in sorted((root / d).rglob("*")):
            if p.is_file() and _want(p.relative_to(root)):
                files.append(p)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in files:
            rel = p.relative_to(root).as_posix()
            info = zipfile.ZipInfo(prefix + rel)
            info.compress_type = zipfile.ZIP_DEFLATED
            mode = 0o755 if p.suffix == ".sh" else 0o644
            info.external_attr = mode << 16
            z.writestr(info, p.read_bytes())
    print(f"wrote {out}  ({out.stat().st_size / 1e6:.1f} MB, "
          f"{len(files)} files, top folder {prefix})")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="dist")
    args = ap.parse_args()
    root = Path(__file__).resolve().parent.parent
    build(root, Path(args.out))
