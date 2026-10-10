"""Turn a decoded ``Report/Layout`` document into the vendor-neutral IR.

This is the only module that understands Power BI's report vocabulary
(``visualType`` names, projection roles, the semantic-query JSON). Everything it
knows was observed in the real sample files or cross-checked against the
Power BI visuals SDK enums; where a value is unrecognised it is preserved in a
``source_*`` field rather than guessed.

Key facts this module relies on (all verified, see docs/FORMAT_FINDINGS.md):
- ``config`` / ``query`` / ``filters`` / ``dataTransforms`` are JSON-in-string.
- position lives in ``config.layouts[0].position``; top-level x/y are optional.
- a container without ``singleVisual`` is a visual group.
- ``query`` is optional even for data-bound visuals, so field metadata is
  resolved from ``dataTransforms.selects`` first, then ``query.Select``, then
  the bare ``queryRef`` string as a last resort.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from src.models.ir import (
    Aggregation, ChartType, Condition, ConditionOp, FieldKind, FieldRef, Filter,
    FilterScope, Page, Placement, Report, Role, Sort, TextRun, Visual,
)
from src.parsers.pbix_reader import PbixReader, load_json_string

# --- vocabulary tables ----------------------------------------------------

# Power BI visualType -> neutral chart type. Names observed in the sample files.
VISUAL_TYPE_MAP: dict[str, ChartType] = {
    "barChart": ChartType.BAR,
    "clusteredBarChart": ChartType.BAR,
    "stackedBarChart": ChartType.BAR,
    "columnChart": ChartType.COLUMN,
    "clusteredColumnChart": ChartType.COLUMN,
    "stackedColumnChart": ChartType.COLUMN,
    "lineChart": ChartType.LINE,
    "areaChart": ChartType.AREA,
    "stackedAreaChart": ChartType.AREA,
    "lineStackedColumnComboChart": ChartType.COMBO,
    "lineClusteredColumnComboChart": ChartType.COMBO,
    "pieChart": ChartType.PIE,
    "donutChart": ChartType.PIE,
    "scatterChart": ChartType.SCATTER,
    "tableEx": ChartType.TABLE,
    "pivotTable": ChartType.MATRIX,
    "card": ChartType.CARD,
    "cardVisual": ChartType.CARD,        # the newer card visual (2023+)
    "multiRowCard": ChartType.CARD,
    "kpi": ChartType.KPI,
    "slicer": ChartType.SLICER,
    "map": ChartType.MAP,
    "filledMap": ChartType.MAP,
    "shapeMap": ChartType.MAP,
    "funnel": ChartType.FUNNEL,
    "ribbonChart": ChartType.RIBBON,
    "treemap": ChartType.TREEMAP,
    "waterfallChart": ChartType.WATERFALL,
    "textbox": ChartType.TEXT,
    "shape": ChartType.SHAPE,
    "image": ChartType.IMAGE,
    "actionButton": ChartType.BUTTON,
    "keyDriversVisual": ChartType.AI,
    "decompositionTreeVisual": ChartType.AI,
    "qnaVisual": ChartType.AI,
}

# Projection role -> neutral shelf. Roles observed across both sample files.
ROLE_MAP: dict[str, Role] = {
    "Category": Role.AXIS, "X": Role.AXIS,
    "Series": Role.LEGEND, "Legend": Role.LEGEND,
    "Y": Role.VALUE, "Y2": Role.VALUE, "Values": Role.VALUE, "Value": Role.VALUE,
    "Size": Role.VALUE, "Indicator": Role.VALUE, "Target": Role.VALUE, "Analyze": Role.VALUE,
    "Data": Role.VALUE,            # cardVisual's value slot
    "Group": Role.LEGEND,          # cardVisual / waterfall breakdown
    "Rows": Role.ROWS, "Columns": Role.COLUMNS,
    "Tooltips": Role.DETAIL, "Details": Role.DETAIL, "TrendLine": Role.DETAIL,
    "ExplainBy": Role.DETAIL, "Play": Role.DETAIL,
}

# Semantic-query ``Aggregation.Function`` int. 0/1/3 verified against the Name
# prefixes in the sample files ("Sum(", "Avg(", "Min("); the rest follow the
# Power BI visuals SDK ``QueryAggregateFunction`` enum.
AGGREGATION_MAP: dict[int, Aggregation] = {
    0: Aggregation.SUM, 1: Aggregation.AVERAGE, 2: Aggregation.COUNT,
    3: Aggregation.MIN, 4: Aggregation.MAX, 5: Aggregation.COUNT_DISTINCT,
}

# ``Comparison.ComparisonKind`` int, per the SDK ``QueryComparisonKind`` enum.
COMPARISON_MAP: dict[int, ConditionOp] = {
    0: ConditionOp.EQ, 1: ConditionOp.GT, 2: ConditionOp.GE,
    3: ConditionOp.LT, 4: ConditionOp.LE,
}

_LITERAL_INT = re.compile(r"^-?\d+L$")
_LITERAL_DEC = re.compile(r"^-?\d+(\.\d+)?[MD]$")


def parse_literal(value):
    """Decode a semantic-query literal: ``'text'``, ``12L``, ``0.5M``, ``true``, ``null``."""
    if not isinstance(value, str):
        return value
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    if value == "null":
        return None
    if value in ("true", "false"):
        return value == "true"
    if _LITERAL_INT.match(value):
        return int(value[:-1])
    if _LITERAL_DEC.match(value):
        return float(value[:-1])
    if value.startswith("datetime'") and value.endswith("'"):
        return value[len("datetime'"):-1]
    return value


# --- field references -----------------------------------------------------

def _aliases(query: dict | None) -> dict[str, str]:
    """Map ``From`` aliases to entity names.

    Entries are usually ``{"Name": "t", "Entity": "Time"}``. Regional Sales also
    contains ``{"Name": "q", "Expression": {"Subquery": {"Query": ...}}}``; such an
    alias resolves to the first entity of the inner query.
    """
    out: dict[str, str] = {}
    for f in (query or {}).get("From", []) or []:
        name = f.get("Name")
        if not name:
            continue
        if "Entity" in f:
            out[name] = f["Entity"]
        else:
            inner = (f.get("Expression") or {}).get("Subquery", {}).get("Query")
            out[name] = next(iter(_aliases(inner).values()), name) if inner else name
    return out


def _entity(expr: dict, aliases: dict[str, str]) -> str:
    ref = expr.get("Expression", {}).get("SourceRef", {})
    if "Entity" in ref:
        return ref["Entity"]
    return aliases.get(ref.get("Source"), ref.get("Source", "?"))


def parse_field_expr(expr: dict, aliases: dict[str, str], **meta) -> FieldRef | None:
    """``{"Column": ...}`` / ``{"Measure": ...}`` / ``{"Aggregation": ...}`` -> FieldRef."""
    if "Column" in expr:
        col = expr["Column"]
        return FieldRef(_entity(col, aliases), col["Property"], FieldKind.COLUMN, **meta)
    if "Measure" in expr:
        m = expr["Measure"]
        return FieldRef(_entity(m, aliases), m["Property"], FieldKind.MEASURE, **meta)
    if "HierarchyLevel" in expr:
        # {"HierarchyLevel": {"Expression": {"Hierarchy": {"Expression": {SourceRef}, "Hierarchy": "H"}}, "Level": "L"}}
        # A level normally carries the name of the column it is built on (verified on
        # Accounts / Street Hierarchy / State or Province in Regional Sales).
        lvl = expr["HierarchyLevel"]
        hier = (lvl.get("Expression") or {}).get("Hierarchy") or {}
        return FieldRef(_entity(hier, aliases), lvl.get("Level", "?"), FieldKind.COLUMN, **meta)
    if "Aggregation" in expr:
        agg = expr["Aggregation"]
        inner = parse_field_expr(agg["Expression"], aliases)
        if inner is None:
            return None
        fn = agg.get("Function")
        return FieldRef(inner.table, inner.name, FieldKind.AGGREGATION,
                        aggregation=AGGREGATION_MAP.get(fn, Aggregation.OTHER),
                        source_aggregation=fn, **meta)
    return None


_QUERYREF = re.compile(r"^(?:(?P<fn>\w+)\()?(?P<table>[^.()]+)\.(?P<name>[^()]+)\)?$")
_QUERYREF_FN = {"Sum": Aggregation.SUM, "Avg": Aggregation.AVERAGE, "Count": Aggregation.COUNT,
                "CountNonNull": Aggregation.COUNT_DISTINCT, "Min": Aggregation.MIN,
                "Max": Aggregation.MAX}


def field_from_queryref(query_ref: str) -> FieldRef:
    """Last resort: ``"Sales.Total"`` or ``"Sum(Sales.Amount)"`` with no metadata."""
    m = _QUERYREF.match(query_ref)
    if not m:
        return FieldRef("?", query_ref, FieldKind.COLUMN, query_name=query_ref)
    fn = m.group("fn")
    if fn:
        return FieldRef(m.group("table"), m.group("name"), FieldKind.AGGREGATION,
                        aggregation=_QUERYREF_FN.get(fn, Aggregation.OTHER), query_name=query_ref)
    # bare "Table.Name": cannot tell column from measure without metadata
    return FieldRef(m.group("table"), m.group("name"), FieldKind.COLUMN, query_name=query_ref)


def _field_index(container: dict) -> dict[str, FieldRef]:
    """Map every queryRef name a visual can use to a fully described FieldRef."""
    index: dict[str, FieldRef] = {}

    # 1. dataTransforms.selects: richest (display name, format, expr)
    dt = load_json_string(container.get("dataTransforms")) or {}
    for sel in dt.get("selects", []) or []:
        expr = sel.get("expr")
        if not expr:
            continue
        ref = parse_field_expr(expr, {}, display_name=sel.get("displayName"),
                               format_string=sel.get("format"), query_name=sel.get("queryName"))
        if ref and sel.get("queryName"):
            index[sel["queryName"]] = ref

    # 2. query.Select: expr only, but always consistent with the projections
    q = load_json_string(container.get("query")) or {}
    for cmd in q.get("Commands", []) or []:
        query = (cmd.get("SemanticQueryDataShapeCommand") or {}).get("Query") or {}
        aliases = _aliases(query)
        for sel in query.get("Select", []) or []:
            name = sel.get("Name")
            if name and name not in index:
                ref = parse_field_expr(sel, aliases, query_name=name)
                if ref:
                    index[name] = ref
    return index


def _sorts(container: dict, index: dict[str, FieldRef]) -> list[Sort]:
    """``Query.OrderBy`` -> IR sorts.

    Power BI records the author's sort on the visual's semantic query::

        "OrderBy": [{"Direction": 2,
                     "Expression": {"Measure": {"Expression": {"SourceRef": {"Source": "_"}},
                                                "Property": "Total Revenue"}}}]

    ``Direction`` is 1 = ascending, 2 = descending. Without this every category
    lands in Tableau alphabetically, so a "Top 10 by revenue" bar chart arrives
    sorted by name - visibly wrong next to the original.
    """
    out: list[Sort] = []
    query_json = load_json_string(container.get("query")) or {}
    for cmd in query_json.get("Commands", []) or []:
        query = (cmd.get("SemanticQueryDataShapeCommand") or {}).get("Query") or {}
        aliases = _aliases(query)
        for entry in query.get("OrderBy", []) or []:
            expr = entry.get("Expression")
            if not expr:
                continue
            ref = parse_field_expr(expr, aliases)
            if ref is None:
                continue
            # prefer the richer description the visual already resolved (display
            # name, format string), matching on table + name
            ref = next((v for v in index.values() if v.table == ref.table and v.name == ref.name), ref)
            out.append(Sort(field=ref, descending=entry.get("Direction") == 2))
    return out


# --- filters ---------------------------------------------------------------

def parse_condition(cond: dict, aliases: dict[str, str]) -> Condition:
    if "Comparison" in cond:
        c = cond["Comparison"]
        fld = parse_field_expr(c.get("Left", {}), aliases)
        raw = c.get("Right", {}).get("Literal", {}).get("Value")
        return Condition(COMPARISON_MAP.get(c.get("ComparisonKind"), ConditionOp.UNKNOWN),
                         field=fld, values=[parse_literal(raw)], source=cond)
    if "In" in cond:
        c = cond["In"]
        exprs = c.get("Expressions", [])
        fld = parse_field_expr(exprs[0], aliases) if exprs else None
        values = [parse_literal(v[0].get("Literal", {}).get("Value")) for v in c.get("Values", []) if v]
        return Condition(ConditionOp.IN, field=fld, values=values, source=cond)
    if "And" in cond or "Or" in cond:
        key = "And" if "And" in cond else "Or"
        c = cond[key]
        return Condition(ConditionOp.AND if key == "And" else ConditionOp.OR,
                         children=[parse_condition(c["Left"], aliases), parse_condition(c["Right"], aliases)],
                         source=cond)
    if "Not" in cond:
        inner = parse_condition(cond["Not"]["Expression"], aliases)
        if inner.op is ConditionOp.IN:                      # NOT IN is common enough to name
            return Condition(ConditionOp.NOT_IN, field=inner.field, values=inner.values, source=cond)
        return Condition(ConditionOp.NOT, children=[inner], source=cond)
    return Condition(ConditionOp.UNKNOWN, source=cond)


def parse_filters(raw, scope: FilterScope) -> list[Filter]:
    items = load_json_string(raw) or []
    out: list[Filter] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        fld = parse_field_expr(item.get("expression") or {}, {}) if item.get("expression") else None
        condition = None
        f = item.get("filter")
        if isinstance(f, dict) and f.get("Where"):
            aliases = _aliases(f)
            conds = [parse_condition(w["Condition"], aliases) for w in f["Where"] if "Condition" in w]
            condition = conds[0] if len(conds) == 1 else Condition(ConditionOp.AND, children=conds)
            if fld is None and condition.field is not None:
                fld = condition.field
        out.append(Filter(scope=scope, field=fld, condition=condition,
                          name=item.get("name"), source_type=item.get("type")))
    return out


# --- visuals and pages -----------------------------------------------------

def _placement(container: dict, config: dict) -> Placement:
    pos = None
    for layout in config.get("layouts") or []:
        if isinstance(layout, dict) and "position" in layout:
            pos = layout["position"]
            break
    if pos is None:                       # older files carry it at top level
        pos = container
    return Placement(x=float(pos.get("x", 0) or 0), y=float(pos.get("y", 0) or 0),
                     width=float(pos.get("width", 0) or 0), height=float(pos.get("height", 0) or 0),
                     z=float(pos.get("z", 0) or 0))


def _title(single: dict) -> str | None:
    """``vcObjects.title[0].properties.text.expr.Literal.Value`` when the author set one."""
    try:
        entries = single.get("vcObjects", {}).get("title") or []
        text = entries[0]["properties"]["text"]["expr"]["Literal"]["Value"]
        return parse_literal(text)
    except (KeyError, IndexError, TypeError):
        return None


_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")


def _text_runs(single: dict) -> list[TextRun]:
    """A text box's literal content, from ``objects.general[0].properties.paragraphs``.

    These carry the page headings and section captions - the words that make a
    dashboard readable. Dropping them leaves a grid of unexplained charts.
    """
    objects = single.get("objects") or {}
    entries = objects.get("general") or objects.get("text") or []
    runs: list[TextRun] = []
    for entry in entries:
        for para in ((entry.get("properties") or {}).get("paragraphs") or []):
            for run in para.get("textRuns") or []:
                text = run.get("value")
                if not isinstance(text, str) or not text.strip():
                    continue
                style = run.get("textStyle") or {}
                size = style.get("fontSize")          # e.g. "16pt"
                try:
                    size_pt = float(str(size).rstrip("ptPT")) if size else None
                except ValueError:
                    size_pt = None
                colour = style.get("color")
                runs.append(TextRun(
                    text=text,
                    size_pt=size_pt,
                    bold=str(style.get("fontWeight", "")).lower() == "bold"
                         or "semibold" in str(style.get("fontFamily", "")).lower(),
                    color=colour.lower() if isinstance(colour, str) and _HEX.match(colour) else None))
    return runs


def _show_labels(single: dict) -> bool | None:
    """``objects.labels[0].properties.show`` - whether the author turned data labels on."""
    entries = (single.get("objects") or {}).get("labels") or []
    for entry in entries:
        show = ((entry.get("properties") or {}).get("show") or {})
        value = (show.get("expr") or {}).get("Literal", {}).get("Value")
        if value is not None:
            return parse_literal(value) is True
        if isinstance(show, bool):
            return show
    return True if entries else None      # a labels object with only styling means "on"


def parse_visual(container: dict) -> Visual:
    config = load_json_string(container.get("config")) or {}
    vid = str(config.get("name") or container.get("id") or "")
    placement = _placement(container, config)
    group = config.get("parentGroupName")

    single = config.get("singleVisual")
    if single is None:
        gname = (config.get("singleVisualGroup") or {}).get("displayName")
        return Visual(id=vid, chart_type=ChartType.GROUP, source_visual_type="singleVisualGroup",
                      placement=placement, title=gname, group=group)

    vtype = single.get("visualType") or "unknown"
    chart = VISUAL_TYPE_MAP.get(vtype)
    if chart is None:
        chart = ChartType.CUSTOM if "_" in vtype else ChartType.UNKNOWN

    visual = Visual(id=vid, chart_type=chart, source_visual_type=vtype, placement=placement,
                    title=_title(single), group=group, has_query="query" in container)

    index = _field_index(container)
    for role, entries in (single.get("projections") or {}).items():
        refs: list[FieldRef] = []
        for entry in entries or []:
            qref = entry.get("queryRef") if isinstance(entry, dict) else None
            if not qref:
                continue
            ref = index.get(qref)
            if ref is None:
                ref = field_from_queryref(qref)
                visual.notes.append(f"field {qref!r} resolved from name only")
            refs.append(ref)
        if refs:
            visual.source_roles[role] = refs
            visual.roles.setdefault(ROLE_MAP.get(role, Role.OTHER), []).extend(refs)

    visual.sort = _sorts(container, index)
    visual.text_runs = _text_runs(single)
    visual.show_labels = _show_labels(single)
    if visual.is_data_bound and not visual.has_query:
        visual.notes.append("data-bound but carries no query (built at runtime)")
    visual.filters = parse_filters(container.get("filters"), FilterScope.VISUAL)
    return visual


def parse_page(section: dict, ordinal: int) -> Page:
    config = load_json_string(section.get("config")) or {}
    page = Page(id=str(section.get("name") or ordinal), name=section.get("displayName") or f"Page {ordinal + 1}",
                ordinal=ordinal, width=float(section.get("width") or 0), height=float(section.get("height") or 0),
                hidden=config.get("visibility") == 1)
    page.visuals = [parse_visual(vc) for vc in section.get("visualContainers", []) or []]
    page.filters = parse_filters(section.get("filters"), FilterScope.PAGE)
    return page


BASE_THEME_DIR = "Report/StaticResources/SharedResources/BaseThemes"
CUSTOM_THEME_DIR = "Report/StaticResources/RegisteredResources"


def _theme(reader: PbixReader, layout: dict) -> tuple[str | None, list[str]]:
    """The report's own series colours, if the file carries the theme it names.

    ``Report/Layout``'s config names a theme collection::

        {"baseTheme":   {"name": "CY25SU11"},
         "customTheme": {"name": "CustomTheme1234567890123456.json"}}

    A custom theme is embedded under RegisteredResources and wins; the base theme
    is embedded under BaseThemes. A report that names only a built-in theme (the
    Retail sample names "Concourse") ships no palette at all, and we return an
    empty list rather than substitute colours the report never chose.
    """
    collection = (load_json_string(layout.get("config")) or {}).get("themeCollection") or {}
    for key, folder in (("customTheme", CUSTOM_THEME_DIR), ("baseTheme", BASE_THEME_DIR)):
        named = (collection.get(key) or {}).get("name")
        if not named:
            continue
        part = f"{folder}/{named}" + ("" if named.lower().endswith(".json") else ".json")
        if not reader.has_part(part):
            continue
        try:
            data = json.loads(reader.read_bytes(part).decode("utf-8-sig"))
        except (ValueError, UnicodeDecodeError):
            continue
        colours = [c.lower() for c in (data.get("dataColors") or []) if _HEX.match(str(c))]
        if colours:
            return data.get("name") or named, colours
    return None, []


def parse_layout(layout: dict, name: str, source_path: str,
                 theme_name: str | None = None, palette: list[str] | None = None) -> Report:
    report = Report(name=name, source_path=source_path, source_format="pbix/Report/Layout",
                    theme_name=theme_name, palette=palette or [])
    report.pages = [parse_page(s, i) for i, s in enumerate(layout.get("sections", []) or [])]
    report.filters = parse_filters(layout.get("filters"), FilterScope.REPORT)
    return report


def parse_pbix(path) -> Report:
    reader = PbixReader(path)
    layout = reader.layout()
    theme_name, palette = _theme(reader, layout)
    return parse_layout(layout, Path(path).stem, str(path), theme_name, palette)
