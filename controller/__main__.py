"""CLI.

  python -m controller run    --url http://127.0.0.1:8700 [--db state/controller.db]
  python -m controller status [--db ...]                       loop health, alerts, credit burn
  python -m controller view   [--db ...] [--workflow W]        the supervisor screen
  python -m controller why    --workflow W --at "D3 14:00"     why was W configured like this at that time
  python -m controller customer --person P                     what one person heard, and on which version
"""
import argparse
import logging
import os
import sys

DEFAULT_DB = os.environ.get("CONTROLLER_DB", "state/controller.db")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="controller")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--url", required=True)
    r.add_argument("--db", default=DEFAULT_DB)
    r.add_argument("-v", "--verbose", action="store_true")
    for name in ("status", "view", "why", "customer"):
        p = sub.add_parser(name)
        p.add_argument("--db", default=DEFAULT_DB)
        if name in ("view", "why"):
            p.add_argument("--workflow")
        if name == "why":
            p.add_argument("--at")
        if name == "customer":
            p.add_argument("--person", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "run":
        logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                            format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        from .loop import Controller
        Controller(a.url, a.db).run()
        return
    from . import report
    if a.cmd == "status":
        print(report.status(a.db))
    elif a.cmd == "view":
        print(report.view(a.db, a.workflow))
    elif a.cmd == "why":
        if not a.workflow:
            sys.exit("--workflow is required")
        print(report.why(a.db, a.workflow, a.at))
    elif a.cmd == "customer":
        print(report.customer(a.db, a.person))


if __name__ == "__main__":
    main()
