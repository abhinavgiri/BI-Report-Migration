"""The migration pipeline as one function, shared by the CLI and the web app.

    result = run_pipeline("report.pbix", out_dir="output/report", model_mode="live", data_mode="live")

Writes into ``out_dir``: assessment.md, summary.json, measures.json (when a model
was supplied), result.json (everything the web UI shows), ``<report>.twb`` and
``data/*.csv``. Returns the same structure as ``result.json``.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from src.analyzers.compatibility import (
    ReportAssessment, Verdict, analyze, describe_condition, render_markdown,
)
from src.extractors.data_extract import Extract, normalise_rows, plan_for_visual
from src.generators.twb_writer import _safe, generate
from dataclasses import replace

from src.models.ir import ChartType, FieldKind, FieldRef, Filter, Report, Visual
from src.parsers.layout_parser import parse_pbix
from src.parsers.model_reader import (
    ModelUnavailable, load_dmv_dir, parse_dmv, read_model, run_queries,
)
from src.translators.dax_to_tableau import translate_measures

_PORT = re.compile(r"localhost:(\d+)")


def tableau_field_name(table: str, name: str) -> str:
    """Must agree with the generator's naming so translated DAX resolves."""
    return f"[{_safe(f'{table}_{name}')}]"


def attach_model(report: Report, model_mode: str, model_port: int | None, model_dir: str | None) -> None:
    """Fill ``report.model``; raises ModelUnavailable when a requested source is unreachable.

    Also refuses a model that does not belong to this report (no table in common):
    Power BI Desktop serves whichever file is open, and translating measures from
    the wrong model would silently produce wrong calculations.
    """
    if model_dir:
        report.model = parse_dmv(load_dmv_dir(model_dir), source=f"DMV export folder {model_dir}")
    elif model_mode == "live":
        report.model = read_model(model_port, expect_tables=report.tables_referenced)
        if not report.model.available:
            raise ModelUnavailable(
                "Power BI Desktop is running but its model is empty - open the .pbix in Desktop "
                "(wait for it to finish loading) and retry.")
    else:
        return
    used, have = set(report.tables_referenced), set(report.model.tables)
    if used and have and not (used & have):
        raise ModelUnavailable(
            f"the model does not match this report: it has tables {sorted(have)[:5]} "
            f"but the report uses {sorted(used)[:5]}. Open '{report.name}' in Power BI Desktop and retry.")


def reconcile_fields(report: Report) -> int:
    """Re-classify fields that the layout only named (no metadata) using the model.

    ``Sales.Revenue Won`` parsed from a bare queryRef is guessed to be a column;
    if the model says it is a measure, the snapshot query must ask for
    ``[Revenue Won]`` instead of ``'Sales'[Revenue Won]``. Returns how many
    references were corrected.
    """
    m = report.model
    if not m.available:
        return 0
    measures = {(x.table, x.name) for x in m.measures}
    columns = {(x.table, x.name) for x in m.columns}
    fixed = 0
    for v in report.visuals:
        swap: dict[FieldRef, FieldRef] = {}
        for f in v.fields:
            key = (f.table, f.name)
            if f.kind is FieldKind.COLUMN and key in measures and key not in columns:
                swap[f] = replace(f, kind=FieldKind.MEASURE, aggregation=None, source_aggregation=None)
            elif f.kind is FieldKind.MEASURE and key in columns and key not in measures:
                swap[f] = replace(f, kind=FieldKind.COLUMN)
        if not swap:
            continue
        for mapping in (v.roles, v.source_roles):
            for refs in mapping.values():
                refs[:] = [swap.get(r, r) for r in refs]
        v.notes.append(f"{len(swap)} field(s) re-classified from the model: "
                       + ", ".join(f"{a.qualified_name} -> {b.kind.value}" for a, b in swap.items()))
        fixed += len(swap)
    return fixed


def live_port(report: Report) -> int | None:
    m = _PORT.search(report.model.source or "")
    return int(m.group(1)) if m else None


# --- data snapshots -------------------------------------------------------------

def snapshot_visuals(report: Report, assessment: ReportAssessment, port: int,
                     filters: str = "all") -> tuple[dict[str, Extract], list[dict]]:
    """Evaluate every migratable visual's query in Power BI's engine.

    Returns ``(snapshots by 'page id/visual id', problems)`` where problems lists
    the visuals whose query the engine rejected, with the engine's message.
    """
    verdicts = {id(a.visual): a for a in assessment.assessments}
    plans = {}
    labels = {}
    for page in report.pages:
        for v in page.visuals:
            a = verdicts[id(v)]
            if a.verdict is Verdict.DECORATIVE or not a.migratable or v.chart_type is ChartType.SLICER:
                continue
            plan = plan_for_visual(page, v, report.filters, filters)
            if plan is not None:
                plans[plan.key] = plan
                labels[plan.key] = {"page": page.name, "visual": v.title or v.id}
    if not plans:
        return {}, []
    names = {f"q{i}": key for i, key in enumerate(plans)}
    rows, errors = run_queries(port, {q: plans[key].dax for q, key in names.items()})
    snapshots, problems = {}, []
    for q, key in names.items():
        plan = plans[key]
        if q in rows:
            snapshots[key] = Extract(plan, normalise_rows(plan, rows[q]))
            for note in plan.notes:
                problems.append({**labels[key], "error": note, "kind": "filter"})
            if not rows[q]:
                why = ("; ".join(plan.filters_applied) if plan.filters_applied else "no filters")
                problems.append({**labels[key], "kind": "empty",
                                 "error": "Power BI returns no rows for this visual with its current filters "
                                          f"({why}) - it is empty in Power BI Desktop today as well."})
        else:
            problems.append({**labels[key], "error": errors.get(q, "no result"), "kind": "query", "dax": plan.dax})
    return snapshots, problems


# --- serialisation for result.json / the web UI ------------------------------

def _field(f) -> dict:
    return {"table": f.table, "name": f.name, "kind": f.kind.value,
            "aggregation": f.aggregation.value if f.aggregation else None,
            "display_name": f.display_name, "format": f.format_string,
            "label": (f"{f.aggregation.value.upper()}({f.qualified_name})"
                      if f.kind is FieldKind.AGGREGATION and f.aggregation else f.qualified_name)}


def _filter(flt: Filter) -> dict:
    return {"scope": flt.scope.value, "name": flt.name, "type": flt.source_type,
            "field": flt.field.qualified_name if flt.field else None,
            "condition": describe_condition(flt.condition) if flt.condition else None}


def _visual(v: Visual, assessment, sheet: str | None, snapshot: Extract | None) -> dict:
    return {
        "id": v.id, "title": v.title, "source_type": v.source_visual_type,
        "chart_type": v.chart_type.value, "verdict": assessment.verdict.value,
        "mark": assessment.target_mark, "reasons": assessment.reasons, "notes": v.notes,
        "has_query": v.has_query, "data_bound": v.is_data_bound, "sheet": sheet,
        "snapshot_rows": len(snapshot.rows) if snapshot else None,
        "snapshot_dax": snapshot.plan.dax if snapshot else None,
        "placement": {"x": v.placement.x, "y": v.placement.y,
                      "w": v.placement.width, "h": v.placement.height},
        "roles": {role.value: [_field(f) for f in refs] for role, refs in v.roles.items()},
        "source_roles": {role: [_field(f) for f in refs] for role, refs in v.source_roles.items()},
        "filters": [_filter(f) for f in v.filters],
    }


def build_result(report: Report, ra: ReportAssessment, translations: list, workbook, out_dir: Path,
                 elapsed_ms: int, snapshots: dict[str, Extract], data_mode: str, problems: list[dict]) -> dict:
    by_visual = {id(a.visual): a for a in ra.assessments}
    sheet_by_visual = workbook.sheet_by_visual if workbook else {}
    counts = ra.counts()
    data = ra.data_visuals
    pages = []
    for p in report.pages:
        vis = [_visual(v, by_visual[id(v)], sheet_by_visual.get(f"{p.id}/{v.id}"), snapshots.get(f"{p.id}/{v.id}"))
               for v in p.visuals]
        pages.append({
            "id": p.id, "name": p.name, "ordinal": p.ordinal, "width": p.width, "height": p.height,
            "data_visuals": sum(1 for v in vis if v["verdict"] != Verdict.DECORATIVE.value),
            "decorative": sum(1 for v in vis if v["verdict"] == Verdict.DECORATIVE.value),
            "visuals": vis, "filters": [_filter(f) for f in p.filters],
        })
    files = [{"name": f.relative_to(out_dir).as_posix(), "size": f.stat().st_size}
             for f in sorted(out_dir.rglob("*")) if f.is_file()]
    return {
        "report": report.name, "source_path": report.source_path, "run_id": out_dir.name,
        "out_dir": str(out_dir), "elapsed_ms": elapsed_ms,
        "pages": len(report.pages), "containers": len(report.visuals), "data_visuals": len(data),
        "verdicts": {v.value: n for v, n in counts.items()},
        "migratable": len(ra.migratable),
        "migratable_pct": round(100.0 * len(ra.migratable) / len(data), 1) if data else 0.0,
        "tables": report.tables_referenced,
        "measures_referenced": ra.measures_referenced,
        "model": {
            "available": report.model.available, "source": report.model.source,
            "tables": len(report.model.tables), "columns": len(report.model.columns),
            "measures": len(report.model.measures), "relationships": len(report.model.relationships),
            "measures_translated": sum(1 for t in translations if t.ok),
            "items": [{"table": t.measure.table, "name": t.measure.name, "dax": t.measure.expression,
                       "format": t.measure.format_string, "tableau": t.formula, "error": t.error}
                      for t in translations],
        },
        "data": {
            "mode": data_mode,
            "sheets": len(workbook.data_sheets) if workbook else len(snapshots),
            "rows": workbook.data_rows if workbook else sum(len(s.rows) for s in snapshots.values()),
            "problems": problems,
        },
        "workbook": None if workbook is None else {
            "twb": workbook.twb_path.name, "csv": workbook.csv_path.relative_to(out_dir).as_posix(),
            "twbx": workbook.twbx_path.name if workbook.twbx_path else None,
            "worksheets": workbook.worksheets, "dashboards": workbook.dashboards,
            "datasources": workbook.datasources, "data_sheets": workbook.data_sheets,
            "hidden_sheets": workbook.hidden_sheets, "retyped": workbook.retyped,
            "skipped": [{"page": p, "visual": v, "reason": r} for p, v, r in workbook.skipped],
            "placeholder_measures": workbook.placeholder_measures,
            "translated_measures": workbook.translated_measures,
        },
        "pages_detail": pages,
        "report_filters": [_filter(f) for f in report.filters],
        "files": files,
    }


def run_pipeline(pbix: str | Path, out_dir: str | Path | None = None, model_mode: str = "none",
                 model_port: int | None = None, model_dir: str | None = None,
                 generate_twb: bool = True, data_mode: str = "none", snapshot_filters: str = "all") -> dict:
    """Parse -> (model) -> assess -> translate -> (snapshots) -> generate.

    ``data_mode="live"`` evaluates each visual's query in the running Power BI
    Desktop (it implies ``model_mode="live"``). Raises PbixError / ModelUnavailable.
    """
    started = time.perf_counter()
    report = parse_pbix(pbix)
    if data_mode == "live" and not model_dir:
        model_mode = "live"
    attach_model(report, model_mode, model_port, model_dir)
    reconcile_fields(report)

    out_dir = Path(out_dir) if out_dir else Path("output") / report.name
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in (out_dir / "data").glob("v*_*.csv"):     # stale snapshots from an earlier run
        old.unlink()

    assessment = analyze(report)
    (out_dir / "assessment.md").write_text(render_markdown(assessment), encoding="utf-8")

    translations = translate_measures(report.model.measures, tableau_field_name) if report.model.measures else []
    lookup = {(t.measure.table, t.measure.name): t.formula for t in translations if t.ok}
    if translations:
        (out_dir / "measures.json").write_text(json.dumps(
            [{"table": t.measure.table, "measure": t.measure.name, "dax": t.measure.expression,
              "tableau": t.formula, "error": t.error} for t in translations], indent=2), encoding="utf-8")

    snapshots, problems = {}, []
    if data_mode == "live":
        port = model_port or live_port(report)
        if port is None:
            raise ModelUnavailable("data snapshots need the report open in Power BI Desktop (live model)")
        snapshots, problems = snapshot_visuals(report, assessment, port, snapshot_filters)

    workbook = generate(report, assessment, out_dir, lookup, snapshots) if generate_twb else None

    elapsed = int((time.perf_counter() - started) * 1000)
    result = build_result(report, assessment, translations, workbook, out_dir, elapsed, snapshots, data_mode, problems)
    result["data"]["filters"] = snapshot_filters
    summary = {k: result[k] for k in ("report", "pages", "containers", "data_visuals", "verdicts", "data")}
    summary["model"] = {k: result["model"][k] for k in ("available", "source", "measures", "measures_translated")}
    if workbook is not None:
        summary["workbook"] = {"twb": str(workbook.twb_path), "csv": str(workbook.csv_path),
                               "worksheets": workbook.worksheets, "dashboards": workbook.dashboards,
                               "skipped": workbook.skipped,
                               "placeholder_measures": workbook.placeholder_measures,
                               "translated_measures": workbook.translated_measures}
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    if not any(f["name"] == "summary.json" for f in result["files"]):
        result["files"].append({"name": "summary.json", "size": (out_dir / "summary.json").stat().st_size})
    (out_dir / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


__all__ = ["run_pipeline", "attach_model", "tableau_field_name", "snapshot_visuals", "ModelUnavailable"]
