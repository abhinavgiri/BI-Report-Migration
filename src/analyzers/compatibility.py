"""Compatibility analyzer: how well does each visual translate to the target?

Consumes the IR only. Produces a verdict per visual plus human-readable reasons,
and a report-level summary. It never writes output files; that is the
generator's job. Keeping the two apart means the assessment stays useful even
for visuals the generator cannot emit.

The capability matrix is a hand-written judgement of what Tableau can express
for each neutral chart type. It is deliberately conservative: "DIRECT" means a
one-to-one mark type with the same shelf semantics, nothing more.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from enum import Enum

from src.models.ir import (
    Aggregation, ChartType, ConditionOp, FieldKind, Report, Role, Visual,
)


class Verdict(Enum):
    DIRECT = "direct"           # same mark type, same shelf semantics
    APPROXIMATE = "approximate"  # can be built, but looks or behaves differently
    MANUAL = "manual"           # a person must rebuild it; we can only describe it
    UNSUPPORTED = "unsupported"  # no target equivalent exists
    DECORATIVE = "decorative"   # layout chrome, not a data visual


@dataclass(frozen=True)
class Capability:
    verdict: Verdict
    target_mark: str | None      # Tableau mark class, when there is one
    note: str


# The matrix. Order of verdict severity is used when a per-visual check downgrades.
CAPABILITY_MATRIX: dict[ChartType, Capability] = {
    ChartType.BAR: Capability(Verdict.DIRECT, "Bar", "Horizontal bar: dimension on rows, measure on columns."),
    ChartType.COLUMN: Capability(Verdict.DIRECT, "Bar", "Vertical bar: dimension on columns, measure on rows."),
    ChartType.LINE: Capability(Verdict.DIRECT, "Line", "Line mark; category on columns, measure on rows."),
    ChartType.AREA: Capability(Verdict.DIRECT, "Area", "Area mark; category on columns, measure on rows."),
    ChartType.SCATTER: Capability(Verdict.DIRECT, "Circle", "Circle mark; X and Y measures on columns/rows."),
    ChartType.PIE: Capability(Verdict.APPROXIMATE, "Bar",
                              "Emitted as a bar per category; switch the mark to Pie and move the measure to Angle."),
    ChartType.TABLE: Capability(Verdict.DIRECT, "Text", "Text table (crosstab) of the selected fields."),
    ChartType.COMBO: Capability(Verdict.APPROXIMATE, "Bar",
                                "Needs a dual-axis worksheet (bar + line); emitted as bars, line axis must be added."),
    ChartType.MATRIX: Capability(Verdict.APPROXIMATE, "Text",
                                 "Crosstab is buildable; Power BI expand/collapse and stepped layout differ."),
    ChartType.CARD: Capability(Verdict.APPROXIMATE, "Text",
                               "Single-value text worksheet (BAN); Power BI card formatting is not carried."),
    ChartType.KPI: Capability(Verdict.APPROXIMATE, "Text",
                              "Value can be shown; the trend/goal indicator needs a hand-written calculation."),
    ChartType.SLICER: Capability(Verdict.APPROXIMATE, None,
                                 "Tableau has no slicer worksheet; becomes a quick filter on the dashboard."),
    ChartType.MAP: Capability(Verdict.APPROXIMATE, "Circle",
                              "Requires the location field to be geocoded in Tableau; custom shape maps do not transfer."),
    ChartType.FUNNEL: Capability(Verdict.APPROXIMATE, "Bar",
                                 "Rendered as sorted horizontal bars; true funnel shape needs a manual build."),
    ChartType.TREEMAP: Capability(Verdict.DIRECT, "Square",
                                  "Square mark: dimension on Detail, measure on Size - Tableau's native treemap."),
    ChartType.WATERFALL: Capability(Verdict.MANUAL, None,
                                    "Tableau builds waterfalls from Gantt bars plus a RUNNING_SUM table "
                                    "calculation; that cannot be generated faithfully, so rebuild it by hand."),
    ChartType.RIBBON: Capability(Verdict.MANUAL, None, "No Tableau equivalent; rebuild as a bump/rank chart by hand."),
    ChartType.AI: Capability(Verdict.UNSUPPORTED, None,
                             "Key influencers / decomposition tree / Q&A have no Tableau counterpart."),
    ChartType.CUSTOM: Capability(Verdict.UNSUPPORTED, None, "Third-party custom visual; not portable."),
    ChartType.UNKNOWN: Capability(Verdict.UNSUPPORTED, None, "Visual type not recognised by the parser."),
    ChartType.TEXT: Capability(Verdict.DECORATIVE, None, "Text box; becomes a dashboard text object."),
    ChartType.SHAPE: Capability(Verdict.DECORATIVE, None, "Shape; becomes a dashboard layout element."),
    ChartType.IMAGE: Capability(Verdict.DECORATIVE, None, "Image; re-add as a dashboard image object."),
    ChartType.BUTTON: Capability(Verdict.DECORATIVE, None, "Navigation button; Tableau uses dashboard navigation objects."),
    ChartType.GROUP: Capability(Verdict.DECORATIVE, None, "Visual group; Tableau uses layout containers."),
}

_SEVERITY = [Verdict.DIRECT, Verdict.APPROXIMATE, Verdict.MANUAL, Verdict.UNSUPPORTED]


def _worse(a: Verdict, b: Verdict) -> Verdict:
    if a is Verdict.DECORATIVE or b is Verdict.DECORATIVE:
        return Verdict.DECORATIVE
    return a if _SEVERITY.index(a) >= _SEVERITY.index(b) else b


@dataclass
class Assessment:
    visual: Visual
    page: str
    verdict: Verdict
    target_mark: str | None
    reasons: list[str] = dc_field(default_factory=list)

    @property
    def migratable(self) -> bool:
        return self.verdict in (Verdict.DIRECT, Verdict.APPROXIMATE)


def assess_visual(visual: Visual, page_name: str) -> Assessment:
    cap = CAPABILITY_MATRIX[visual.chart_type]
    a = Assessment(visual=visual, page=page_name, verdict=cap.verdict,
                   target_mark=cap.target_mark, reasons=[cap.note])

    if cap.verdict is Verdict.DECORATIVE:
        return a

    # --- per-visual downgrades ------------------------------------------------
    if not visual.is_data_bound:
        a.verdict = Verdict.MANUAL
        a.reasons.append("No fields are bound to this visual, so there is nothing to place on shelves.")
        return a

    if visual.measures and not visual.dimensions and visual.chart_type not in (
            ChartType.CARD, ChartType.KPI, ChartType.TABLE, ChartType.MATRIX):
        a.verdict = _worse(a.verdict, Verdict.APPROXIMATE)
        a.reasons.append("Only measures, no category field: chart will be a single bar/point.")

    model_measures = [f for f in visual.fields if f.kind is FieldKind.MEASURE]
    if model_measures:
        names = ", ".join(sorted({f.qualified_name for f in model_measures}))
        a.reasons.append(f"Uses model measure(s) {names}: emitted as placeholder calculations "
                         "unless the model layer supplies their definitions.")

    unknown_aggs = [f for f in visual.fields if f.aggregation is Aggregation.OTHER]
    if unknown_aggs:
        a.verdict = _worse(a.verdict, Verdict.APPROXIMATE)
        a.reasons.append("An aggregation type was not recognised; defaulted to SUM.")

    name_only = [n for n in visual.notes if "resolved from name only" in n]
    if name_only:
        a.verdict = _worse(a.verdict, Verdict.APPROXIMATE)
        a.reasons.append("Some fields lacked metadata; column/measure kind was inferred from the name.")

    if visual.is_data_bound and not visual.has_query:
        a.reasons.append("Power BI builds this visual's query at runtime; fields were taken from its projections.")

    unmapped = [f for f in visual.filters if f.condition and _has_unknown(f.condition)]
    if unmapped:
        a.verdict = _worse(a.verdict, Verdict.APPROXIMATE)
        a.reasons.append(f"{len(unmapped)} visual filter(s) use a predicate we could not translate.")

    if visual.roles.get(Role.OTHER):
        a.reasons.append("Some fields sit on a role with no Tableau shelf equivalent and were placed on Detail.")

    return a


def _has_unknown(cond) -> bool:
    if cond.op is ConditionOp.UNKNOWN:
        return True
    return any(_has_unknown(c) for c in cond.children)


@dataclass
class ReportAssessment:
    report: Report
    assessments: list[Assessment]

    def counts(self) -> dict[Verdict, int]:
        out = {v: 0 for v in Verdict}
        for a in self.assessments:
            out[a.verdict] += 1
        return out

    @property
    def data_visuals(self) -> list[Assessment]:
        return [a for a in self.assessments if a.verdict is not Verdict.DECORATIVE]

    @property
    def migratable(self) -> list[Assessment]:
        return [a for a in self.assessments if a.migratable]

    @property
    def measures_referenced(self) -> list[str]:
        names = {f.qualified_name for a in self.assessments for f in a.visual.fields
                 if f.kind is FieldKind.MEASURE}
        return sorted(names)


def analyze(report: Report) -> ReportAssessment:
    out = [assess_visual(v, p.name) for p in report.pages for v in p.visuals]
    return ReportAssessment(report=report, assessments=out)


# --- rendering ---------------------------------------------------------------

def render_markdown(ra: ReportAssessment) -> str:
    r = ra.report
    counts = ra.counts()
    data = ra.data_visuals
    lines = [f"# Migration assessment: {r.name}", "",
             f"Source: `{r.source_path}`  ", f"Pages: {len(r.pages)}  ",
             f"Visual containers: {len(ra.assessments)} (data visuals: {len(data)}, "
             f"decorative: {counts[Verdict.DECORATIVE]})", "",
             "## Summary", "", "| Verdict | Count | Meaning |", "|---|---:|---|"]
    meaning = {
        Verdict.DIRECT: "same mark type and shelves in Tableau",
        Verdict.APPROXIMATE: "buildable, but looks or behaves differently",
        Verdict.MANUAL: "must be rebuilt by hand; described below",
        Verdict.UNSUPPORTED: "no Tableau equivalent",
        Verdict.DECORATIVE: "layout chrome, re-added as dashboard objects",
    }
    for v in Verdict:
        lines.append(f"| {v.value} | {counts[v]} | {meaning[v]} |")
    if data:
        pct = 100.0 * len(ra.migratable) / len(data)
        lines += ["", f"**{len(ra.migratable)} of {len(data)} data visuals ({pct:.0f}%) can be generated automatically.**"]

    if r.model.available:
        lines += ["", f"Model layer: available ({len(r.model.measures)} measures, "
                      f"{len(r.model.relationships)} relationships) from {r.model.source}."]
    else:
        lines += ["", "Model layer: **not available** - measure definitions were not supplied, so "
                      "measures are emitted as placeholder calculations. Open the file in Power BI "
                      "Desktop and re-run with model extraction to fill them in."]
    if ra.measures_referenced:
        lines += ["", "Model measures referenced by visuals:", ""]
        lines += [f"- `{m}`" for m in ra.measures_referenced]

    lines += ["", "## Visuals by page", ""]
    for p in r.pages:
        page_items = [a for a in ra.assessments if a.page == p.name and a.verdict is not Verdict.DECORATIVE]
        deco = sum(1 for a in ra.assessments if a.page == p.name and a.verdict is Verdict.DECORATIVE)
        lines += [f"### {p.name}", "",
                  f"{len(page_items)} data visual(s), {deco} decorative element(s).", ""]
        if not page_items:
            continue
        lines += ["| Visual | Type | Verdict | Mark | Fields | Notes |", "|---|---|---|---|---|---|"]
        for a in page_items:
            v = a.visual
            fields = "; ".join(f"{role.value}: " + ", ".join(_fmt(f) for f in refs)
                               for role, refs in v.roles.items())
            title = v.title or v.id
            notes = " ".join(a.reasons[1:]) if len(a.reasons) > 1 else a.reasons[0]
            lines.append(f"| {_cell(title)} | {v.source_visual_type} | **{a.verdict.value}** | "
                         f"{a.target_mark or '-'} | {_cell(fields)} | {_cell(notes)} |")
        lines.append("")

    filters = r.filters + [f for p in r.pages for f in p.filters]
    if filters:
        lines += ["## Report and page filters", ""]
        for f in filters:
            target = f.field.qualified_name if f.field else "?"
            state = describe_condition(f.condition) if f.condition else "no value applied"
            lines.append(f"- [{f.scope.value}] `{target}` - {state}")
        lines.append("")
    return "\n".join(lines)


def _fmt(f) -> str:
    if f.kind is FieldKind.AGGREGATION and f.aggregation:
        return f"{f.aggregation.value.upper()}({f.qualified_name})"
    tag = " (measure)" if f.kind is FieldKind.MEASURE else ""
    return f"{f.qualified_name}{tag}"


def _cell(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


def describe_condition(c) -> str:
    """Human-readable form of a Condition tree, e.g. ``Store.Store Type eq ['Same Store']``."""
    if c.children:
        inner = f" {c.op.value.upper()} ".join(describe_condition(ch) for ch in c.children)
        return f"({inner})" if c.op is not ConditionOp.NOT else f"NOT {inner}"
    fld = c.field.qualified_name if c.field else "?"
    return f"{fld} {c.op.value} {c.values}"
