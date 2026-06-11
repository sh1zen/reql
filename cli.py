#!/usr/bin/env python
"""Repository-local launcher for the REQL command line interface."""
from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"


def _add_src_to_path() -> None:
    src = str(SRC)
    if src not in sys.path:
        sys.path.insert(0, src)
    elif sys.path[0:1] != [src]:
        sys.path.remove(src)
        sys.path.insert(0, src)

    current = os.environ.get("PYTHONPATH")
    if current:
        paths = current.split(os.pathsep)
        if src not in paths:
            os.environ["PYTHONPATH"] = os.pathsep.join([src, current])
    else:
        os.environ["PYTHONPATH"] = src


def main(argv: list[str] | None = None) -> int:
    _add_src_to_path()

    from memory.cli import main as cli_main

    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
