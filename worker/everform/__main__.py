"""Command line: python -m everform <command>

  sync      fetch the gallery, process new/changed images, publish if the portrait changed
  rebuild   re-derive the fusion sums from cached faces (no re-download) and publish
  history   regenerate the reconstructed film-by-film timeline
  status    print the current counts from the published manifest
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from . import config


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="everform", description="Claudia Everform worker")
    p.add_argument("command", choices=["sync", "rebuild", "history", "status"])
    p.add_argument("--mode", choices=["canon", "archive", "all"], default="all")
    p.add_argument("--force", action="store_true", help="publish a new version even if inputs are unchanged")
    p.add_argument("--limit", type=int, default=None, help="analyze at most N new images this run")
    p.add_argument("--history", choices=["auto", "always", "never"], default="auto")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    modes = list(config.MODES) if a.mode == "all" else [a.mode]

    from .sync import rebuild, regenerate_history, run_sync

    if a.command == "sync":
        out = run_sync(modes, force=a.force, limit=a.limit, history=a.history)
        print(json.dumps(out, indent=1))
        failed = [m for m, r in out["modes"].items() if r.get("outcome") == "rejected"]
        return 2 if failed else 0
    if a.command == "rebuild":
        print(json.dumps(rebuild(modes), indent=1))
        return 0
    if a.command == "history":
        regenerate_history(modes)
        return 0
    if a.command == "status":
        from .publish import load_manifest
        m = load_manifest()
        for mode, e in m.get("modes", {}).items():
            print(mode, e.get("current"), json.dumps(e.get("counts")))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
