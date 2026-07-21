"""CLI entrypoint for FlowShift diagnostics."""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import flowshift_diagnostics as diag


def build_parser():
    parser = argparse.ArgumentParser(description="Print a FlowShift diagnostics report")
    parser.add_argument("--json", action="store_true", help="print JSON instead of the readable report")
    parser.add_argument("--timeout", type=float, default=1.5, help="local control socket timeout in seconds")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    payload = diag.collect_diagnostics(timeout=args.timeout)
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        snapshot = payload.get("snapshot") or payload.get("diagnostics") or {}
        print(payload.get("report") or diag.format_diagnostics_report(snapshot))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
