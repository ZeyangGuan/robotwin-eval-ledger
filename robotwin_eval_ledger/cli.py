import argparse
import json
import sys

from .demo import make_demo
from .ledger import LedgerError, load_run
from .report import write_comparison, write_summary


def main(argv=None):
    parser = argparse.ArgumentParser(description="Audit recorded evaluation denominators and sample sets. No simulator required.")
    parser.add_argument("--version", action="version", version="robotwin-eval-ledger 0.1.0")
    sub = parser.add_subparsers(dest="command", required=True)
    summary = sub.add_parser("summarize", help="merge one run's per-worker JSONL files")
    summary.add_argument("inputs", nargs="+", help="JSONL files or directories (nonrecursive)")
    summary.add_argument("--out", required=True, help="report directory; generated report files are replaced")
    comp = sub.add_parser("compare", help="compare two run directories or JSONL files")
    comp.add_argument("left")
    comp.add_argument("right")
    comp.add_argument("--out", required=True)
    demo = sub.add_parser("demo", help="generate synthetic 100-seed, crash/retry and multi-task examples")
    demo.add_argument("--out", default="demo-output", help="new or empty output directory")
    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            out = make_demo(args.out)
            print(f"Synthetic demo created: {out}/comparison/report.html")
            print("Both observed rates are 80%; evaluated sets differ (100 versus 80 seeds).")
            print(f"Per-task diagnostics: {out}/multitask-report/report.html")
        elif args.command == "summarize":
            result = write_summary(load_run(args.inputs), args.out)
            print(json.dumps({"run_id": result["run_id"], "latest_attempts": result["latest_attempts"],
                              "official": result["official"], "warnings": result["warnings"]}, indent=2))
            print(f"Report: {args.out}/report.html")
        else:
            result = write_comparison(load_run(args.left), load_run(args.right), args.out)
            print(json.dumps({k: v for k, v in result.items() if k != "rows"}, indent=2))
            print(f"Report: {args.out}/report.html")
    except (LedgerError, OSError) as exc:
        print(f"ledger error: {exc}", file=sys.stderr)
        return 2
    return 0
