"""Command line: .pbix in -> assessment + Tableau workbook out.

    python -m src.cli "input/Retail Analysis Sample PBIX.pbix"
    python -m src.cli report.pbix --out output/report --model live
    python -m src.cli report.pbix --model live --data live      # snapshots from the open report
    python -m src.cli report.pbix --model-dir tests/fixtures/dmv

Exit codes: 0 success, 1 bad input, 2 model/data requested but unavailable.
The same pipeline powers the web app (``python -m src.web``).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.parsers.model_reader import ModelUnavailable
from src.parsers.pbix_reader import PbixError
from src.pipeline import run_pipeline


def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bi-migrate", description=__doc__.split("\n\n")[0])
    ap.add_argument("pbix", help="Power BI .pbix file")
    ap.add_argument("--out", help="output folder (default: output/<report name>)")
    ap.add_argument("--model", choices=["none", "live"], default="none",
                    help="'live' reads measures/relationships from the running Power BI Desktop")
    ap.add_argument("--model-port", type=int, help="explicit Analysis Services port (with --model live)")
    ap.add_argument("--model-dir", help="folder of TMSCHEMA_*.json DMV exports (offline model)")
    ap.add_argument("--data", choices=["none", "live"], default="none",
                    help="'live' evaluates every visual's query in the open Power BI Desktop and "
                         "ships the rows with the workbook (implies --model live)")
    ap.add_argument("--snapshot-filters", choices=["all", "visual", "none"], default="all",
                    help="which Power BI filters to apply when snapshotting: all (what Power BI shows), "
                         "only each visual's own, or none (raw grain)")
    ap.add_argument("--no-twb", action="store_true", help="assessment only, skip workbook generation")
    args = ap.parse_args(argv)

    try:
        r = run_pipeline(args.pbix, out_dir=args.out, model_mode=args.model, model_port=args.model_port,
                         model_dir=args.model_dir, generate_twb=not args.no_twb, data_mode=args.data,
                         snapshot_filters=args.snapshot_filters)
    except PbixError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except ModelUnavailable as e:
        print(f"error: model requested but unavailable: {e}", file=sys.stderr)
        return 2

    v = r["verdicts"]
    print(f"{r['report']}: {r['pages']} pages, {r['containers']} containers, {r['data_visuals']} data visuals")
    print(f"  direct {v['direct']} | approximate {v['approximate']} | manual {v['manual']} "
          f"| unsupported {v['unsupported']} | decorative {v['decorative']}")
    m = r["model"]
    if m["available"]:
        print(f"  model: {m['measures']} measures, {m['measures_translated']} translated to Tableau")
    else:
        print("  model: not requested (use --model live with the file open in Power BI Desktop)")
    d = r["data"]
    if d["mode"] == "live":
        print(f"  data: {d['sheets']} sheets with snapshots, {d['rows']} rows, {len(d['problems'])} problem(s)")
        for p in d["problems"][:5]:
            print(f"    - {p['page']} / {p['visual']}: {p['error'][:120]}")
    if r["workbook"]:
        print(f"  workbook: {Path(r['out_dir']) / r['workbook']['twb']} "
              f"({len(r['workbook']['worksheets'])} sheets, {len(r['workbook']['dashboards'])} dashboards)")
    print(f"  assessment: {Path(r['out_dir']) / 'assessment.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(run())
