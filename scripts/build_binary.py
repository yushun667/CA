"""Build a native, self-contained CA directory with the current Python runtime.

Run on each target OS/architecture after installing requirements-build.txt.
PyInstaller is not a cross compiler. Distribute the entire dist/ca directory.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path, default=None)
    parser.add_argument("--work-dir", type=Path, default=None)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    work = (args.work_dir or root / "build" / "binary").resolve()
    dist = (args.dist_dir or root / "dist").resolve()
    work.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(root / "src"))
    try:
        from PyInstaller.__main__ import run
    except ImportError:
        parser.error("install build dependencies first: python -m pip install -r requirements-build.txt")
    run([
        "--noconfirm",
        "--clean",
        "--onedir",
        "--console",
        "--name=ca",
        f"--distpath={dist}",
        f"--workpath={work / 'work'}",
        f"--specpath={work}",
        f"--paths={root / 'src'}",
        "--collect-submodules=ca",
        "--collect-all=clang",
        "--collect-all=duckdb",
        "--hidden-import=_duckdb",
        "--copy-metadata=clang",
        "--copy-metadata=libclang",
        "--copy-metadata=duckdb",
        f"--add-data={root / 'src' / 'ca' / 'store' / 'schema.sql'}:ca/store",
        f"--add-data={root / 'src' / 'ca' / 'ui' / 'static'}:ca/ui/static",
        str(root / "scripts" / "frozen_entry.py"),
    ])
    executable = dist / "ca" / ("ca.exe" if sys.platform == "win32" else "ca")
    if not executable.is_file():
        raise RuntimeError(f"build did not produce {executable}")
    print(f"Built native CA bundle: {executable}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
