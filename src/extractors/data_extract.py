"""Build one DAX query per visual and turn its result into a worksheet snapshot.

Why per visual: a Power BI visual is a query (group by its dimensions, evaluate
its measures, apply its filters). Running that exact query in Power BI's own
engine yields the numbers the visual shows - including measures we could never
translate - and one row per grain, so Tableau's SUM over the snapshot is the
same number. A single flattened table could not do that for non-additive
measures (ratios, averages, distinct counts).

This module is pure: it only builds DAX text and normalises result rows. The
engine call lives in ``src.parsers.model_reader.run_queries``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field
from datetime import datetime, timezone

from src.models.ir import (
    Aggregation, Condition, ConditionOp, FieldKind, FieldRef, Filter, Page, Visual,
)

AGG_DAX = {Aggregation.SUM: "SUM", Aggregation.AVERAGE: "AVERAGE", Aggregation.MIN: "MIN",
           Aggregation.MAX: "MAX", Aggregation.COUNT: "COUNTA",
           Aggregation.COUNT_DISTINCT: "DISTINCTCOUNT", Aggregation.OTHER: "SUM", None: "SUM"}

_COMPARISON = {ConditionOp.EQ: "=", ConditionOp.NE: "<>", ConditionOp.GT: ">",
               ConditionOp.GE: ">=", ConditionOp.LT: "<", ConditionOp.LE: "<="}
_PS_DATE = re.compile(r"^/Date\((-?\d+)\)/$")


def safe_name(text: str) -> str:
    """Same rule as the Tableau generator, so snapshot columns and shelves agree."""
    return re.sub(r"[^\w .\-()%&']", "_", text).strip() or "field"


def dax_literal(value) -> str:
    if value is None:
        return "BLANK()"
    if isinstance(value, bool):
        return "TRUE()" if value else "FALSE()"
    if isinstance(value, (int, float)):
        return repr(value)
    return '"' + str(value).replace('"', '""') + '"'


def colref(table: str, column: str) -> str:
    return "'" + table.replace("'", "''") + "'[" + column + "]"


@dataclass
class ExtractColumn:
    dax_name: str        # header as the engine returns it, e.g. "Time[FiscalMonth]" or "[Sales_Total]"
    name: str            # Tableau internal name (no brackets), e.g. "Time_FiscalMonth"
    caption: str
    datatype: str        # string | real
    role: str            # dimension | measure
    kind: str            # nominal | quantitative


@dataclass
class ExtractPlan:
    key: str                          # "<page id>/<visual id>"
    dax: str
    columns: list[ExtractColumn]
    notes: list[str] = dc_field(default_factory=list)
    filters_applied: list[str] = dc_field(default_factory=list)   # DAX filter terms, for explaining empty results


@dataclass
class Extract:
    plan: ExtractPlan
    rows: list[list]

    @property
    def columns(self) -> list[ExtractColumn]:
        return self.plan.columns


# --- filters ------------------------------------------------------------------

def _predicate(cond: Condition) -> tuple[str, str] | None:
    """A leaf condition as ``(column ref, DAX boolean)``; None when not expressible."""
    f = cond.field
    if f is None or f.kind is not FieldKind.COLUMN:
        return None
    col = colref(f.table, f.name)
    vals = cond.values
    if cond.op in (ConditionOp.IN, ConditionOp.NOT_IN):
        if not vals:
            return None
        expr = f"{col} IN {{{', '.join(dax_literal(v) for v in vals)}}}"
        return col, (f"NOT ({expr})" if cond.op is ConditionOp.NOT_IN else expr)
    if cond.op in _COMPARISON and vals:
        v = vals[0]
        if v is None and cond.op in (ConditionOp.EQ, ConditionOp.NE):
            return col, (f"ISBLANK({col})" if cond.op is ConditionOp.EQ else f"NOT ISBLANK({col})")
        return col, f"{col} {_COMPARISON[cond.op]} {dax_literal(v)}"
    return None


def _filter_terms(cond: Condition, notes: list[str], where: str) -> list[str]:
    """Translate a Condition tree into SUMMARIZECOLUMNS filter-table arguments."""
    if cond.op is ConditionOp.AND:
        out = []
        for c in cond.children:
            out += _filter_terms(c, notes, where)
        return out
    if cond.op is ConditionOp.NOT and len(cond.children) == 1:
        inner = _predicate(cond.children[0])
        if inner is not None:
            col, expr = inner
            return [f"FILTER(ALL({col}), NOT ({expr}))"]
    f = cond.field
    if f is None or f.kind is not FieldKind.COLUMN:
        what = f"measure {f.qualified_name}" if f is not None else f"'{cond.op.value}' predicate"
        notes.append(f"{where} filter on {what} not applied (needs the visual's full evaluation context)")
        return []
    if cond.op in (ConditionOp.IN, ConditionOp.NOT_IN) and not cond.values:
        notes.append(f"{where} filter on {f.qualified_name} has no values and was not applied")
        return []
    col = colref(f.table, f.name)
    if cond.op is ConditionOp.IN:
        return [f"TREATAS({{{', '.join(dax_literal(v) for v in cond.values)}}}, {col})"]
    if cond.op is ConditionOp.EQ and cond.values and cond.values[0] is not None:
        return [f"TREATAS({{{dax_literal(cond.values[0])}}}, {col})"]
    leaf = _predicate(cond)
    if leaf is not None:
        col, expr = leaf
        return [f"FILTER(ALL({col}), {expr})"]
    notes.append(f"{where} filter '{cond.op.value}' on {f.qualified_name} not applied")
    return []


def filter_arguments(filters: list[Filter], where: str, notes: list[str]) -> list[str]:
    out = []
    for flt in filters:
        if flt.condition is not None:
            out += _filter_terms(flt.condition, notes, where)
    return out


# --- planning -----------------------------------------------------------------

def plan_for_visual(page: Page, visual: Visual, report_filters: list[Filter],
                    filters: str = "all") -> ExtractPlan | None:
    """The DAX that reproduces this visual's data, or None if it has no fields.

    ``filters``: ``"all"`` applies report + page + visual filters (what Power BI
    shows), ``"visual"`` only the visual's own, ``"none"`` takes the raw grain.
    """
    fields = visual.fields
    dims = [f for f in fields if f.kind is FieldKind.COLUMN]
    meas = [f for f in fields if f.is_measure_like]
    if not dims and not meas:
        return None
    notes: list[str] = []
    columns: list[ExtractColumn] = []
    args: list[str] = []
    seen: set[str] = set()
    for f in dims:
        name = safe_name(f"{f.table}_{f.name}")
        if name in seen:
            continue
        seen.add(name)
        args.append(colref(f.table, f.name))
        columns.append(ExtractColumn(f"{f.table}[{f.name}]", name, f.qualified_name, "string", "dimension", "nominal"))
    terms: list[str] = []
    if filters == "all":
        terms += filter_arguments(report_filters, "report", notes) + filter_arguments(page.filters, "page", notes)
    if filters in ("all", "visual"):
        terms += filter_arguments(visual.filters, "visual", notes)
    args += terms
    filters = terms
    for f in meas:
        name = safe_name(f"{f.table}_{f.name}")
        if name in seen:
            continue
        seen.add(name)
        expr = f"[{f.name}]" if f.kind is FieldKind.MEASURE else f"{AGG_DAX.get(f.aggregation, 'SUM')}({colref(f.table, f.name)})"
        args += [dax_literal(name), expr]
        columns.append(ExtractColumn(f"[{name}]", name, f.display_name or f.name, "real", "measure", "quantitative"))
    dax = "EVALUATE SUMMARIZECOLUMNS(" + ", ".join(args) + ")"
    return ExtractPlan(key=f"{page.id}/{visual.id}", dax=dax, columns=columns, notes=notes, filters_applied=filters)


# --- results ------------------------------------------------------------------

def _cell(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value) if isinstance(value, float) and not value.is_integer() else str(int(value)) if isinstance(value, float) else str(value)
    if isinstance(value, str):
        m = _PS_DATE.match(value)
        if m:      # PowerShell 5.1 serialises DateTime as /Date(ms)/
            return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        return value
    return str(value)


def normalise_rows(plan: ExtractPlan, raw_rows: list[dict]) -> list[list]:
    """Map engine result rows (dicts keyed by DAX header) onto the plan's column order."""
    if not raw_rows:
        return []
    keys = list(raw_rows[0].keys())
    lower = {k.lower(): k for k in keys}
    resolved = []
    for col in plan.columns:
        k = col.dax_name if col.dax_name in raw_rows[0] else lower.get(col.dax_name.lower())
        if k is None:      # engine may drop the table prefix or the brackets
            bare = col.dax_name.strip("[]").split("[")[-1].rstrip("]").lower()
            k = next((orig for lo, orig in lower.items() if lo.strip("[]").split("[")[-1].rstrip("]") == bare), None)
        resolved.append(k)
    return [[_cell(r.get(k)) if k else "" for k in resolved] for r in raw_rows]
