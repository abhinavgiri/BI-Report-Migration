"""Generate a Tableau workbook (.twb) from the IR.

Structure follows the Superstore.twb that ships with Tableau Desktop 2026.2
(docs/FORMAT_FINDINGS.md section 6) plus the loader requirements found by
bisecting it (section 7): format manifest + source-build, the explain-data
node, a <simple-id> on every sheet / dashboard / window, a <style/> in every
table, non-self-closing empty shelves, entity-escaped newlines, and no
hand-written dashboard windows.

Data strategy:
- Default (decision D7): the report's data is locked inside the Power BI model,
  so the workbook is backed by ONE flattened, header-only CSV whose columns are
  every field the visuals reference. Model measures become calculated fields -
  translated when the DAX subset allows, otherwise a labelled placeholder.
- With snapshots (decision D15): each visual whose query was evaluated in Power
  BI's own engine gets its OWN datasource backed by a CSV holding exactly that
  visual's rows, measures already computed at the visual's grain. Visuals
  without a snapshot fall back to the stub datasource.
"""
from __future__ import annotations

import csv
import re
import uuid
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field as dc_field
from pathlib import Path

from src.analyzers.compatibility import ReportAssessment, Verdict
from src.models.ir import (
    Aggregation, ChartType, FieldKind, FieldRef, Page, Report, Role, Visual,
)

WORKBOOK_VERSION = "18.1"
DS_NAME = "federated.bimigration"
CONN_NAME = "textscan.bimigration"
CSV_NAME = "model_stub.csv"
MEASURE_NAMES = "[:Measure Names]"
MEASURE_VALUES = "[Multiple Values]"

_DERIVATION = {Aggregation.SUM: ("Sum", "sum"), Aggregation.AVERAGE: ("Avg", "avg"),
               Aggregation.COUNT: ("Count", "cnt"), Aggregation.COUNT_DISTINCT: ("CountD", "ctd"),
               Aggregation.MIN: ("Min", "min"), Aggregation.MAX: ("Max", "max"),
               Aggregation.OTHER: ("Sum", "sum"), None: ("Sum", "sum")}
_AGG_CALL = re.compile(r"\b(SUM|AVG|MIN|MAX|COUNT|COUNTD|ATTR|MEDIAN)\s*\(", re.I)
_NUMERIC_FORMAT = re.compile(r"[0#]")
_NUMERIC_VALUE = re.compile(r"^-?\d+(\.\d+)?([eE][-+]?\d+)?$")

# Tableau 2026.2 refuses to load a workbook without this feature-flag node
# (verified: removing it from the shipped Superstore.twb breaks the file).
_EXPLAIN_DATA_XML = """<_.fcp.ExplainData_AuthorControls.true...explain-data enabled-for-viewer="false" extreme-values-enabled-for-all="false">
  <explanation-types>
    <explanation-type enabled="true" type="number-of-records" />
    <explanation-type enabled="true" type="average-of-records" />
    <explanation-type enabled="true" type="aggregated-dimensions" />
    <explanation-type enabled="true" type="distribution-of-records" />
    <explanation-type enabled="true" type="unvisualized-measures" />
    <explanation-type enabled="true" type="extreme-values" />
    <_.fcp.ExplainData_NullValueExplanation.true...explanation-type enabled="true" type="null-value" />
    <explanation-type enabled="true" type="tvd-single-value" />
    <explanation-type enabled="true" type="ad-single-value" />
  </explanation-types>
</_.fcp.ExplainData_AuthorControls.true...explain-data>"""

# Declares the document-format features the file uses. Tableau 2026.2 refuses a
# workbook without this manifest, and likewise without a source-build attribute.
# WindowsPersistSimpleIdentifiers / SheetIdentifierTracking are why every sheet,
# dashboard and window must carry a <simple-id>.
_MANIFEST_XML = """<document-format-change-manifest>
  <_.fcp.AccessibleZoneTabOrder.true...AccessibleZoneTabOrder />
  <_.fcp.AnimationOnByDefault.true...AnimationOnByDefault />
  <AutoCreateAndUpdateDSDPhoneLayouts />
  <_.fcp.ExplainData_AuthorControls.true...ExplainData_AuthorControls />
  <_.fcp.ExplainData_NullValueExplanation.true...ExplainData_NullValueExplanation />
  <MapboxVectorStylesAndLayers />
  <_.fcp.MarkAnimation.true...MarkAnimation />
  <_.fcp.ObjectModelEncapsulateLegacy.true...ObjectModelEncapsulateLegacy />
  <_.fcp.ObjectModelTableType.true...ObjectModelTableType />
  <_.fcp.SchemaViewerObjectModel.true...SchemaViewerObjectModel />
  <SetMembershipControl />
  <SheetIdentifierTracking />
  <SortTagCleanup />
  <VizInTooltipHideWorksheet />
  <WindowsPersistSimpleIdentifiers />
  <WorksheetBackgroundTransparency />
  <ZoneFriendlyName />
</document-format-change-manifest>"""
SOURCE_BUILD = "0.0.0 (bi-migration-assistant)"

_TEXT_NODE = re.compile(r">([^<]*)<")


def _simple_id(parent) -> None:
    """Every worksheet, dashboard and window carries one; Tableau will not load without it."""
    ET.SubElement(parent, "simple-id", {"uuid": "{" + str(uuid.uuid4()).upper() + "}"})


def _escape_text_newlines(xml: str) -> str:
    """Tableau only accepts newlines inside text nodes as ``&#10;`` / ``&#13;`` entities.

    ElementTree writes them literally (it escapes them in attributes only), which
    makes an otherwise valid workbook fail to load. Verified by round-tripping
    Superstore.twb through ElementTree: it fails until this repair is applied.
    """
    def fix(m):
        t = m.group(1)
        if t.strip() and ("\n" in t or "\r" in t):
            t = t.replace("\r", "&#13;").replace("\n", "&#10;")
        return ">" + t + "<"
    xml = _TEXT_NODE.sub(fix, xml)
    # Tableau's loader also rejects self-closing empty shelves; it writes <rows></rows>.
    return xml.replace("<rows />", "<rows></rows>").replace("<cols />", "<cols></cols>")


def _safe(text: str) -> str:
    return re.sub(r"[^\w .\-()%&']", "_", text).strip() or "field"


_PBI_STANDARD_FORMAT = re.compile(r"^[A-Za-z]\d*$")


def tableau_number_format(pbi: str | None) -> str | None:
    r"""Map a Power BI (.NET) numeric format string to a Tableau ``default-format``.

    Power BI writes .NET custom numeric formats, in up to three
    ``positive;negative;zero`` sections::

        \$#,0;(\$#,0);\$#,0
        "₹" #,0.00;"₹" -#,0.00;"₹" #,0.00
        0.00 %;-0.00 %;0.00 %

    Tableau writes a one-letter type followed by a pattern - verified against
    Tableau's own Superstore workbook, which contains ``c"$"#,##0``, ``p0.00%``
    and ``n#,##0.0"%"``.

    Only the positive section is mapped; Tableau derives negatives itself.
    Returns ``None`` when the string carries no numeric opinion (the standard
    codes ``G``/``D``, or a date pattern), leaving Tableau's default in place -
    guessing there would invent formatting the report never asked for.
    """
    if not pbi:
        return None
    text = pbi.strip()
    if not text or _PBI_STANDARD_FORMAT.match(text):        # 'G' general, 'D' date
        return None

    positive = text.split(";")[0]
    literals: list[str] = []            # quoted runs and \-escapes: currency symbols
    body_chars: list[str] = []          # the digit placeholders themselves
    i = 0
    while i < len(positive):
        ch = positive[i]
        if ch == '"':
            end = positive.find('"', i + 1)
            if end == -1:
                break
            literals.append(positive[i + 1:end])
            i = end + 1
        elif ch == "\\" and i + 1 < len(positive):
            literals.append(positive[i + 1])
            i += 2
        else:
            body_chars.append(ch)
            i += 1
    body = "".join(body_chars)
    if not re.search(r"[0#]", body):                        # a date pattern or pure text
        return None

    # keep '0' (forced digit) and '#' (optional digit) apart: '#,0.###' means
    # "up to three decimals", not "always three"
    decimals = "".join(c for c in body.split(".", 1)[1] if c in "0#") if "." in body else ""
    if "%" in body:                                         # unescaped % scales by 100
        return "p" + ("0." + decimals if decimals else "0") + "%"
    digits = ("#,##0" if "," in body else "0") + ("." + decimals if decimals else "")
    currency = next((lit.strip() for lit in literals if lit.strip() and lit.strip() != "%"), None)
    return f'c"{currency}"{digits}' if currency else "n" + digits


# --- field registry ----------------------------------------------------------

@dataclass
class TField:
    """A Tableau column derived from an IR field."""
    name: str                       # internal name without brackets, e.g. "Sales_Total"
    caption: str
    datatype: str                   # string | real | integer | date | datetime | boolean
    role: str                       # dimension | measure
    kind: str                       # nominal | quantitative | ordinal
    formula: str | None = None      # calculated field
    is_placeholder: bool = False
    is_aggregate_formula: bool = False
    number_format: str | None = None    # Tableau default-format, from the PBI format string

    @property
    def ref(self) -> str:
        return f"[{self.name}]"


@dataclass
class FieldRegistry:
    """The Tableau columns of one datasource, named consistently from IR fields."""
    translations: dict[tuple[str, str], str] = dc_field(default_factory=dict)
    data_mode: bool = False          # snapshot: measures are plain numeric columns
    fields: dict[str, TField] = dc_field(default_factory=dict)
    _by_key: dict[tuple[str, str, str], str] = dc_field(default_factory=dict)

    def _unique(self, base: str) -> str:
        name, n = base, 2
        while name in self.fields:
            name, n = f"{base} {n}", n + 1
        return name

    def base_column(self, f: FieldRef) -> TField:
        """The underlying CSV column for a COLUMN or AGGREGATION field."""
        key = ("col", f.table, f.name)
        if key in self._by_key:
            return self.fields[self._by_key[key]]
        numeric = f.kind is FieldKind.AGGREGATION or (
            f.format_string and _NUMERIC_FORMAT.search(f.format_string) and f.kind is not FieldKind.COLUMN)
        tf = TField(name=self._unique(_safe(f"{f.table}_{f.name}")), caption=f.qualified_name,
                    datatype="real" if numeric else "string",
                    role="measure" if numeric else "dimension",
                    kind="quantitative" if numeric else "nominal",
                    number_format=tableau_number_format(f.format_string))
        self.fields[tf.name] = tf
        self._by_key[key] = tf.name
        return tf

    def measure(self, f: FieldRef) -> TField:
        key = ("meas", f.table, f.name)
        if key in self._by_key:
            return self.fields[self._by_key[key]]
        if self.data_mode:
            formula, placeholder = None, False        # the value came from Power BI itself
        else:
            formula = self.translations.get((f.table, f.name))
            placeholder = formula is None
            if placeholder:
                formula = (f"// Power BI measure {f.qualified_name}: definition not available - "
                           f"replace this placeholder\n0")
        tf = TField(name=self._unique(_safe(f"{f.table}_{f.name}")), caption=f.display_name or f.name,
                    datatype="real", role="measure", kind="quantitative", formula=formula,
                    is_placeholder=placeholder,
                    number_format=tableau_number_format(f.format_string))
        self.fields[tf.name] = tf
        self._by_key[key] = tf.name
        return tf

    def finalize_aggregates(self) -> None:
        """A calculated field is aggregate if it calls an aggregate or references one."""
        def is_agg(tf: TField, seen=()) -> bool:
            if tf.formula is None or tf.name in seen:
                return False
            if _AGG_CALL.search(tf.formula):
                return True
            for other in self.fields.values():
                if other.formula is not None and other.ref in tf.formula and other is not tf:
                    if is_agg(other, seen + (tf.name,)):
                        return True
            return False
        for tf in self.fields.values():
            if tf.formula is not None:
                tf.is_aggregate_formula = is_agg(tf)

    def instance(self, f: FieldRef) -> tuple[str, TField, str, str]:
        """(instance name like 'sum:Sales_Total:qk', field, derivation, type).

        The TField's role wins: a snapshot may prove that a "measure" is really
        formatted text (a Power BI card showing "Rs 470.75 Cr"), and aggregating
        text is an error in Tableau.
        """
        tf = self.measure(f) if f.kind is FieldKind.MEASURE else self.base_column(f)
        if tf.role == "dimension":
            return f"none:{tf.name}:nk", tf, "None", "nominal"
        if tf.formula is not None and tf.is_aggregate_formula:
            return f"usr:{tf.name}:qk", tf, "User", "quantitative"
        deriv, short = _DERIVATION.get(f.aggregation, ("Sum", "sum"))
        return f"{short}:{tf.name}:qk", tf, deriv, "quantitative"

    def retype_from_rows(self, rows: list[list], row_columns: list[str]) -> list[str]:
        """Let the snapshot's actual values decide each column's type. Returns retyped names."""
        index = {name: i for i, name in enumerate(row_columns)}
        changed = []
        for tf in self.fields.values():
            i = index.get(tf.name)
            if i is None or tf.formula is not None:
                continue
            values = [str(r[i]).strip() for r in rows if i < len(r) and str(r[i]).strip() != ""]
            numeric = bool(values) and all(_NUMERIC_VALUE.match(v) for v in values)
            role = "measure" if numeric else "dimension"
            if role != tf.role:
                changed.append(tf.name)
            tf.role, tf.datatype, tf.kind = (
                ("measure", "real", "quantitative") if numeric else ("dimension", "string", "nominal"))
        return changed

    @property
    def base_fields(self) -> list[TField]:
        return [tf for tf in self.fields.values() if tf.formula is None]


@dataclass
class Datasource:
    """One Tableau datasource: a registry plus the CSV that backs it."""
    name: str
    conn: str
    caption: str
    csv_name: str
    registry: FieldRegistry
    rows: list[list] | None = None          # None = header-only stub
    row_columns: list[str] | None = None    # column names matching ``rows``


# --- XML builders --------------------------------------------------------------

def _column_el(parent, tf: TField):
    attrs = {"caption": tf.caption, "datatype": tf.datatype, "name": tf.ref, "role": tf.role, "type": tf.kind}
    # A snapshot can prove a "measure" is really formatted text (a card showing
    # "Rs 470.75 Cr"); a numeric format on a string column is meaningless.
    if tf.number_format and tf.role == "measure":
        attrs["default-format"] = tf.number_format
    el = ET.SubElement(parent, "column", attrs)
    if tf.formula is not None:
        ET.SubElement(el, "calculation", {"class": "tableau", "formula": tf.formula})
    return el


def _datasource_el(parent, ds: Datasource):
    el = ET.SubElement(parent, "datasource", {"caption": ds.caption, "inline": "true",
                                              "name": ds.name, "version": WORKBOOK_VERSION})
    conn = ET.SubElement(el, "connection", {"class": "federated"})
    ncs = ET.SubElement(conn, "named-connections")
    nc = ET.SubElement(ncs, "named-connection", {"caption": ds.caption, "name": ds.conn})
    ET.SubElement(nc, "connection", {"class": "textscan", "directory": "data", "filename": ds.csv_name,
                                     "password": "", "server": ""})
    rel = ET.SubElement(conn, "relation", {"connection": ds.conn, "name": ds.csv_name,
                                           "table": f"[{ds.csv_name}]", "type": "table"})
    # Declare the schema, or Tableau GUESSES the delimiter. A single-column snapshot
    # (a card holding one value) contains no comma at all, and Tableau then splits on
    # spaces - turning "_Measures_Total Revenue Display" into three phantom fields.
    schema = ET.SubElement(rel, "columns", {"character-set": "UTF-8", "header": "yes",
                                            "locale": "en_US", "separator": ","})
    for ordinal, tf in enumerate(ds.registry.base_fields):
        ET.SubElement(schema, "column", {"datatype": tf.datatype, "name": tf.name,
                                         "ordinal": str(ordinal)})
    cols = ET.SubElement(conn, "cols")
    for tf in ds.registry.base_fields:
        ET.SubElement(cols, "map", {"key": tf.ref, "value": f"[{ds.csv_name}].{tf.ref}"})
    ET.SubElement(el, "column", {"datatype": "string", "name": MEASURE_NAMES, "role": "dimension", "type": "nominal"})
    for tf in ds.registry.fields.values():
        _column_el(el, tf)
    return el


@dataclass
class Shelves:
    rows: list[str] = dc_field(default_factory=list)
    cols: list[str] = dc_field(default_factory=list)
    color: str | None = None
    size: str | None = None
    text: list[str] = dc_field(default_factory=list)
    detail: list[str] = dc_field(default_factory=list)
    mark: str = "Automatic"
    sorts: list[tuple[str, str, str]] = dc_field(default_factory=list)   # (column, direction, using)
    mark_color: str | None = None                                        # solid colour, no legend
    color_map: list[tuple[str, str]] = dc_field(default_factory=list)    # (member, colour) for a legend
    labels: bool = False                                                 # show mark labels


_SAFE_MEMBER = re.compile(r"^[A-Za-z0-9 ,.&'()/+-]+$")


def _is_light(colour: str) -> bool:
    """Perceived luminance test, so we never put pale text on a pale dashboard."""
    try:
        r, g, b = (int(colour[i:i + 2], 16) for i in (1, 3, 5))
    except (ValueError, IndexError):
        return False
    return (0.299 * r + 0.587 * g + 0.114 * b) > 160


def _members(ds: Datasource, tf_name: str) -> list[str]:
    """Distinct values of one snapshot column, in the order they first appear."""
    if not ds.rows or not ds.row_columns or tf_name not in ds.row_columns:
        return []
    i = ds.row_columns.index(tf_name)
    seen: list[str] = []
    for row in ds.rows:
        if i >= len(row):
            continue
        value = str(row[i]).strip()
        if value and value not in seen:
            seen.append(value)
    return seen


def _plan(visual: Visual, ds: Datasource, mark: str | None, palette: list[str] | None = None) -> Shelves:
    """Decide which field goes on which shelf for this chart type."""
    reg = ds.registry

    def q(f):
        inst, _, _, _ = reg.instance(f)
        return f"[{ds.name}].[{inst}]"
    roles = visual.roles
    src = visual.source_roles
    dims = [q(f) for f in roles.get(Role.AXIS, [])]
    rows_role = [q(f) for f in roles.get(Role.ROWS, [])]
    cols_role = [q(f) for f in roles.get(Role.COLUMNS, [])]
    legend = [q(f) for f in roles.get(Role.LEGEND, [])]
    values = [q(f) for f in roles.get(Role.VALUE, []) if f.is_measure_like or reg.base_column(f).role == "measure"]
    detail = [q(f) for f in roles.get(Role.DETAIL, []) + roles.get(Role.OTHER, [])]
    s = Shelves(mark=mark or "Automatic", detail=detail, color=legend[0] if legend else None)
    ct = visual.chart_type

    if ct is ChartType.SCATTER:
        x = [q(f) for f in src.get("X", [])]
        y = [q(f) for f in src.get("Y", [])]
        s.cols, s.rows = x[:1], y[:1]
        size = [q(f) for f in src.get("Size", [])]
        s.size = size[0] if size else None
        s.detail = [q(f) for f in src.get("Category", [])] + detail
        s.mark = "Circle"
    elif ct in (ChartType.TABLE, ChartType.MATRIX, ChartType.CARD, ChartType.KPI):
        s.rows = rows_role or dims
        s.cols = cols_role
        # cards often hold pre-formatted text, so show every value field, numeric or not
        s.text = [q(f) for f in roles.get(Role.VALUE, [])]
        s.mark = "Automatic"
    elif ct is ChartType.TREEMAP:
        # Tableau's treemap: no shelves, dimension on Detail, measure on Size (and Colour).
        s.cols, s.rows = [], []
        s.detail = dims + detail
        s.size = values[0] if values else None
        s.color = s.color or (values[0] if values else None)
        s.mark = "Square"
    elif ct is ChartType.BAR:                       # horizontal: dimension on rows
        s.rows, s.cols = dims + rows_role, values
    else:                                           # column / line / area / combo / pie / funnel / map
        s.cols, s.rows = dims, values
        if ct is ChartType.MAP:
            s.cols, s.rows, s.detail = [], [], dims + detail
            s.size = values[0] if values else None
            s.mark = "Circle"

    # Power BI's OrderBy names the measure that ranks the categories ("top 10
    # clinics BY revenue"). Tableau needs both ends to be column-instances this
    # worksheet actually declares, so anything that does not resolve is dropped
    # rather than guessed - some OrderBy expressions reference fields the visual
    # never projects.
    declared = {reg.instance(f)[0] for f in visual.fields}
    categorical = [c for c in (s.rows + s.cols + s.detail) if c.endswith(":nk]")]
    taken: set[str] = set()
    for srt in visual.sort:
        if not any(srt.field.table == f.table and srt.field.name == f.name for f in visual.fields):
            continue
        inst, _, _, kind = reg.instance(srt.field)
        if kind != "quantitative" or inst not in declared:
            continue
        using = f"[{ds.name}].[{inst}]"
        target = next((c for c in categorical if c not in taken and c != using), None)
        if target is None:
            continue
        taken.add(target)
        s.sorts.append((target, "DESC" if srt.descending else "ASC", using))

    s.labels = visual.show_labels is True

    # Colour comes from the report's own theme, or not at all. A visual with no
    # legend is one series, which the source paints in the theme's first colour;
    # a visual with a legend gets one theme colour per member, in order - the
    # same rule the source applies.
    if palette:
        if s.color and s.color.endswith(":nk]"):
            tf_name = s.color.split(".[", 1)[-1].rstrip("]").split(":")[1]
            members = _members(ds, tf_name)
            if members and all(_SAFE_MEMBER.match(m) for m in members):
                s.color_map = [(m, palette[i % len(palette)]) for i, m in enumerate(members)]
        elif not s.color:
            s.mark_color = palette[0]
    return s


def _bucket(member: str):
    el = ET.Element("bucket")
    el.text = f'"{member}"'
    return el


def _join(items: list[str], sep: str) -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return "(" + f" {sep} ".join(items) + ")"


def _worksheet_el(parent, name: str, visual: Visual, ds: Datasource, shelves: Shelves):
    reg = ds.registry
    ws = ET.SubElement(parent, "worksheet", {"name": name})
    if visual.title:
        lo = ET.SubElement(ws, "layout-options")
        t = ET.SubElement(lo, "title")
        ft = ET.SubElement(t, "formatted-text")
        ET.SubElement(ft, "run").text = visual.title
    table = ET.SubElement(ws, "table")
    view = ET.SubElement(table, "view")
    dss = ET.SubElement(view, "datasources")
    ET.SubElement(dss, "datasource", {"caption": ds.caption, "name": ds.name})
    deps = ET.SubElement(view, "datasource-dependencies", {"datasource": ds.name})

    used: dict[str, tuple[TField, str, str]] = {}
    for f in visual.fields:
        inst, tf, deriv, kind = reg.instance(f)
        used[inst] = (tf, deriv, kind)
    multi_measure = len(shelves.text) > 1
    if multi_measure:
        ET.SubElement(deps, "column", {"datatype": "string", "name": MEASURE_NAMES,
                                       "role": "dimension", "type": "nominal"})
    for tf in {tf.name: tf for tf, _, _ in used.values()}.values():
        _column_el(deps, tf)
    for inst, (tf, deriv, kind) in used.items():
        ET.SubElement(deps, "column-instance", {"column": tf.ref, "derivation": deriv,
                                                "name": f"[{inst}]", "pivot": "key", "type": kind})
    if multi_measure:
        flt = ET.SubElement(view, "filter", {"class": "categorical", "column": f"[{ds.name}].{MEASURE_NAMES}"})
        gf = ET.SubElement(flt, "groupfilter", {"function": "union"})
        for m in shelves.text:
            ET.SubElement(gf, "groupfilter", {"function": "member", "level": MEASURE_NAMES, "member": f'"{m}"'})
    for column, direction, using in shelves.sorts:
        ET.SubElement(view, "computed-sort", {"column": column, "direction": direction, "using": using})
    ET.SubElement(view, "aggregation", {"value": "true"})
    # <style> must exist or Tableau refuses the file (findings section 7); it is
    # also where mark colour and labels live.
    style = ET.SubElement(table, "style")
    if shelves.mark_color or shelves.labels or shelves.color_map:
        rule = ET.SubElement(style, "style-rule", {"element": "mark"})
        if shelves.color_map:
            enc = ET.SubElement(rule, "encoding", {"attr": "color", "field": shelves.color,
                                                   "type": "palette"})
            for member, colour in shelves.color_map:
                ET.SubElement(enc, "map", {"to": colour}).append(_bucket(member))
        elif shelves.mark_color:
            ET.SubElement(rule, "format", {"attr": "mark-color", "value": shelves.mark_color})
        if shelves.labels:
            ET.SubElement(rule, "format", {"attr": "mark-labels-show", "value": "true"})
            ET.SubElement(rule, "format", {"attr": "mark-labels-cull", "value": "true"})

    panes = ET.SubElement(table, "panes")
    pane = ET.SubElement(panes, "pane")
    pv = ET.SubElement(pane, "view")
    ET.SubElement(pv, "breakdown", {"value": "auto"})
    ET.SubElement(pane, "mark", {"class": shelves.mark})
    enc = ET.SubElement(pane, "encodings")
    if shelves.color:
        ET.SubElement(enc, "color", {"column": shelves.color})
    if shelves.size:
        ET.SubElement(enc, "size", {"column": shelves.size})
    for d in shelves.detail:
        ET.SubElement(enc, "lod", {"column": d})
    if multi_measure:
        ET.SubElement(enc, "text", {"column": f"[{ds.name}].{MEASURE_VALUES}"})
    elif shelves.text:
        ET.SubElement(enc, "text", {"column": shelves.text[0]})

    cols = list(shelves.cols) + ([f"[{ds.name}].{MEASURE_NAMES}"] if multi_measure else [])
    ET.SubElement(table, "rows").text = _join(shelves.rows, "+" if shelves.rows and all(":qk" in r for r in shelves.rows) else "/")
    ET.SubElement(table, "cols").text = _join(cols, "+" if cols and all(":qk" in c for c in cols) else "/")
    _simple_id(ws)
    return ws


def _zone_style(zone, **formats) -> None:
    zs = ET.SubElement(zone, "zone-style")
    base = {"border-color": "#d7d7d7", "border-style": "none", "border-width": "0", "margin": "4"}
    for attr, value in {**base, **formats}.items():
        ET.SubElement(zs, "format", {"attr": attr, "value": value})


def _text_zone(parent, visual: Visual, attrs: dict, palette: list[str]) -> None:
    """A source text box, rebuilt as a Tableau text zone.

    These hold the page headings. The source often paints them white because a
    coloured banner shape sits behind them - and that shape is not something we
    carry over, so white text would land invisibly on a white dashboard. When the
    text is light we put the theme's own primary colour behind it instead; with no
    theme to draw on we drop the colour and let Tableau use its readable default.
    """
    zone = ET.SubElement(parent, "zone", {**attrs, "type-v2": "text"})
    ft = ET.SubElement(zone, "formatted-text")
    light = any(r.color and _is_light(r.color) for r in visual.text_runs)
    banner = palette[0] if (light and palette) else None
    for r in visual.text_runs:
        run_attrs: dict[str, str] = {}
        if r.size_pt:
            run_attrs["fontsize"] = str(int(round(r.size_pt)))
        if r.bold:
            run_attrs["bold"] = "true"
        if r.color and (banner or not _is_light(r.color)):
            run_attrs["fontcolor"] = r.color
        ET.SubElement(ft, "run", run_attrs).text = r.text
    _zone_style(zone, **({"background-color": banner} if banner else {}))


def _dashboard_el(parent, page: Page, placed: list[tuple[Visual, str]], palette: list[str] | None = None):
    """One dashboard per page; zones use Tableau's 0-100000 relative coordinates."""
    palette = palette or []
    W, H = max(page.width, 1.0), max(page.height, 1.0)
    db = ET.SubElement(parent, "dashboard", {"name": page.name})
    ET.SubElement(db, "size", {"maxheight": str(int(H)), "maxwidth": str(int(W)),
                               "minheight": str(int(H)), "minwidth": str(int(W)),
                               "sizing-mode": "fixed"})
    zones = ET.SubElement(db, "zones")
    root = ET.SubElement(zones, "zone", {"h": "100000", "id": "1", "type-v2": "layout-basic",
                                         "w": "100000", "x": "0", "y": "0"})

    def box(p) -> dict:
        return {"h": str(max(int(100000 * p.height / H), 1)),
                "w": str(max(int(100000 * p.width / W), 1)),
                "x": str(int(100000 * p.x / W)), "y": str(int(100000 * p.y / H))}

    zid = 2
    for visual, ws_name in placed:
        zone = ET.SubElement(root, "zone", {**box(visual.placement), "id": str(zid), "name": ws_name})
        ET.SubElement(zone, "layout-cache", {"type-h": "fixed", "type-w": "fixed"})
        _zone_style(zone, **{"background-color": "#ffffff", "padding": "2"})
        zid += 1
    # the headings the source wrote on the page, in their own places
    for visual in page.visuals:
        if visual.chart_type is ChartType.TEXT and visual.text_runs:
            _text_zone(root, visual, {**box(visual.placement), "id": str(zid)}, palette)
            zid += 1
    _simple_id(db)
    return db


def _dashboard_window_el(parent, name: str, sheets: list[str]):
    """A dashboard's own tab.

    Tableau rejects a dashboard window whose ``<viewpoints>`` is empty, but accepts
    one carrying a viewpoint per sheet on the dashboard - which is what lets us hide
    every worksheet and leave only the page tabs (verified in Tableau 2026.2).
    """
    win = ET.SubElement(parent, "window", {"class": "dashboard", "maximized": "true", "name": name})
    viewpoints = ET.SubElement(win, "viewpoints")
    for sheet in sheets:
        vp = ET.SubElement(viewpoints, "viewpoint", {"name": sheet})
        # without this each sheet keeps its natural size and is clipped by, or
        # floats inside, its dashboard zone
        ET.SubElement(vp, "zoom", {"type": "entire-view"})
    ET.SubElement(win, "active", {"id": "-1"})
    _simple_id(win)
    return win


def _window_el(parent, name: str, hidden: bool = False):
    attrs = {"class": "worksheet", "name": name}
    if hidden:
        attrs["hidden"] = "true"      # on a dashboard: Tableau shows the dashboard tab instead
    win = ET.SubElement(parent, "window", attrs)
    cards = ET.SubElement(win, "cards")
    left = ET.SubElement(cards, "edge", {"name": "left"})
    strip = ET.SubElement(left, "strip", {"size": "160"})
    for t in ("pages", "filters", "marks"):
        ET.SubElement(strip, "card", {"type": t})
    top = ET.SubElement(cards, "edge", {"name": "top"})
    for t in ("columns", "rows"):
        ET.SubElement(ET.SubElement(top, "strip", {"size": "2147483647"}), "card", {"type": t})
    _simple_id(win)
    return win


# --- entry point ---------------------------------------------------------------

@dataclass
class GeneratedWorkbook:
    twb_path: Path
    csv_path: Path                                      # the stub datasource's CSV
    worksheets: list[str] = dc_field(default_factory=list)
    dashboards: list[str] = dc_field(default_factory=list)
    datasources: list[str] = dc_field(default_factory=list)
    skipped: list[tuple[str, str, str]] = dc_field(default_factory=list)   # (page, visual, reason)
    placeholder_measures: list[str] = dc_field(default_factory=list)
    translated_measures: list[str] = dc_field(default_factory=list)
    sheet_by_visual: dict[str, str] = dc_field(default_factory=dict)   # 'page id/visual id' -> worksheet name
    data_sheets: list[str] = dc_field(default_factory=list)            # sheets backed by a snapshot
    hidden_sheets: list[str] = dc_field(default_factory=list)          # shown via their dashboard tab
    retyped: list[str] = dc_field(default_factory=list)                # columns the snapshot re-typed
    twbx_path: Path | None = None
    data_rows: int = 0


_BAD_SHEET = re.compile(r"[\[\]:/\\?*]")


def _sheet_name(page: Page, visual: Visual, taken: set[str]) -> str:
    label = visual.title or visual.source_visual_type
    base = _BAD_SHEET.sub("-", f"{page.name} - {label}")[:60].strip()
    name, n = base, 2
    while name in taken:
        name, n = f"{base} ({n})", n + 1
    taken.add(name)
    return name


def _write_csv(path: Path, ds: Datasource) -> int:
    header = [tf.name for tf in ds.registry.base_fields]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        if not ds.rows:
            return 0
        index = {name: i for i, name in enumerate(ds.row_columns or [])}
        for row in ds.rows:
            w.writerow([row[index[h]] if h in index and index[h] < len(row) else "" for h in header])
    return len(ds.rows)


def generate(report: Report, assessment: ReportAssessment, out_dir: str | Path,
             translations: dict[tuple[str, str], str] | None = None,
             snapshots: dict | None = None) -> GeneratedWorkbook:
    """Write ``<report>.twb`` and its CSVs into ``out_dir``.

    ``snapshots`` maps ``"<page id>/<visual id>"`` to an object with ``.columns``
    (each having ``.name``) and ``.rows`` (lists aligned with the columns); such a
    visual gets its own datasource with real rows.
    """
    out_dir = Path(out_dir)
    (out_dir / "data").mkdir(parents=True, exist_ok=True)
    snapshots = snapshots or {}
    stub = Datasource(DS_NAME, CONN_NAME, f"{report.name} (flattened model stub)", CSV_NAME,
                      FieldRegistry(translations=dict(translations or {})))
    verdicts = {id(a.visual): a for a in assessment.assessments}
    result = GeneratedWorkbook(twb_path=out_dir / f"{_safe(report.name)}.twb", csv_path=out_dir / "data" / CSV_NAME)

    # pass 1: decide datasources and register fields, so every datasource is complete
    plan: list[tuple[Page, Visual, str | None, Datasource, str]] = []
    taken: set[str] = set()
    per_visual: list[Datasource] = []
    for page in report.pages:
        for v in page.visuals:
            a = verdicts[id(v)]
            if a.verdict is Verdict.DECORATIVE:
                continue
            if not a.migratable or v.chart_type is ChartType.SLICER:
                reason = "slicer becomes a dashboard filter" if v.chart_type is ChartType.SLICER else a.reasons[-1]
                result.skipped.append((page.name, v.title or v.id, reason))
                continue
            name = _sheet_name(page, v, taken)
            key = f"{page.id}/{v.id}"
            snap = snapshots.get(key)
            if snap is not None:
                n = len(per_visual) + 1
                ds = Datasource(f"federated.v{n}", f"textscan.v{n}", f"{name} (snapshot)",
                                f"v{n}_{_safe(name)[:40].strip()}.csv", FieldRegistry(data_mode=True),
                                rows=list(snap.rows), row_columns=[c.name for c in snap.columns])
                per_visual.append(ds)
                result.data_sheets.append(name)
            else:
                ds = stub
            for f in v.fields:
                ds.registry.instance(f)
            plan.append((page, v, a.target_mark, ds, name))
    stub.registry.finalize_aggregates()
    for ds in per_visual:                      # the data decides the types, not our guess
        result.retyped += ds.registry.retype_from_rows(ds.rows or [], ds.row_columns or [])
    stub_used = any(ds is stub for _, _, _, ds, _ in plan)

    wb = ET.Element("workbook", {"source-build": SOURCE_BUILD, "source-platform": "win",
                                 "version": WORKBOOK_VERSION,
                                 "xmlns:user": "http://www.tableausoftware.com/xml/user"})
    wb.append(ET.fromstring(_MANIFEST_XML))
    prefs = ET.SubElement(wb, "preferences")
    ET.SubElement(prefs, "preference", {"name": "ui.encoding.shelf.height", "value": "24"})
    ET.SubElement(prefs, "preference", {"name": "ui.shelf.height", "value": "26"})
    dss = ET.SubElement(wb, "datasources")
    for ds in ([stub] if stub_used or not per_visual else []) + per_visual:
        _datasource_el(dss, ds)
        result.datasources.append(ds.name)
    wss = ET.SubElement(wb, "worksheets")
    dbs = ET.SubElement(wb, "dashboards")
    wins = ET.SubElement(wb, "windows")

    by_page: dict[str, list[tuple[Visual, str]]] = {}
    for page, v, mark, ds, name in plan:
        _worksheet_el(wss, name, v, ds, _plan(v, ds, mark, report.palette))
        result.worksheets.append(name)
        result.sheet_by_visual[f"{page.id}/{v.id}"] = name    # ids repeat across pages
        by_page.setdefault(page.id, []).append((v, name))
    for page in report.pages:
        if page.id in by_page:
            _dashboard_el(dbs, page, by_page[page.id], report.palette)
            result.dashboards.append(page.name)
    # One tab per page, exactly like Power BI: every worksheet that sits on a dashboard
    # is hidden behind it. That needs the dashboard windows below - a workbook with no
    # visible window at all refuses to load (findings section 10).
    on_dashboard = {n for placed in by_page.values() for _, n in placed}
    for page in report.pages:
        placed = by_page.get(page.id)
        if placed:
            _dashboard_window_el(wins, page.name, [n for _, n in placed])
    for name in result.worksheets:
        _window_el(wins, name, hidden=name in on_dashboard)
    result.hidden_sheets = sorted(on_dashboard)

    for tf in stub.registry.fields.values():
        if tf.formula is not None:
            (result.placeholder_measures if tf.is_placeholder else result.translated_measures).append(tf.caption)

    wb.append(ET.fromstring(_EXPLAIN_DATA_XML))
    ET.indent(wb, space="  ")
    xml = ET.tostring(wb, encoding="unicode", xml_declaration=True)
    result.twb_path.write_text(_escape_text_newlines(xml), encoding="utf-8", newline="")

    if stub_used or not per_visual:
        _write_csv(result.csv_path, stub)
    for ds in per_visual:
        result.data_rows += _write_csv(out_dir / "data" / ds.csv_name, ds)

    result.twbx_path = _package_twbx(result.twb_path, out_dir / "data")
    return result


def _package_twbx(twb_path: Path, data_dir: Path) -> Path:
    r"""Bundle the workbook and its CSVs into a single .twbx.

    A .twbx is a ZIP holding the .twb at the root plus its data files under the
    same relative folder the connections name. Downloading only the .twb leaves
    Tableau hunting for ``data\model_stub.csv``; the .twbx carries everything,
    which is why it is the download we lead with.
    """
    twbx = twb_path.with_suffix(".twbx")
    with zipfile.ZipFile(twbx, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(twb_path, twb_path.name)
        for csv_file in sorted(data_dir.glob("*.csv")):
            z.write(csv_file, f"data/{csv_file.name}")
    return twbx
