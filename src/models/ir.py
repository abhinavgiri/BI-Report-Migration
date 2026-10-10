"""Intermediate Representation (IR) of a BI report.

This is the heart of the project. Everything upstream (Power BI parsers) produces
these objects; everything downstream (analyzers, Tableau generators) consumes
them. Neither side ever sees the other's file format.

Design rules:
- Plain dataclasses, no behaviour beyond small conveniences.
- Vendor-neutral vocabulary. Where a source concept has no neutral equivalent,
  the raw source value is kept in a ``source_*`` field for provenance and the
  neutral field is set to an ``OTHER``/``UNKNOWN`` member.
- Nothing here is required: parsers fill what they can find; consumers must
  tolerate empty lists and ``None``.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from enum import Enum


class ChartType(Enum):
    """Neutral visual categories. The generator decides how each is drawn."""
    BAR = "bar"                 # horizontal bars
    COLUMN = "column"           # vertical bars
    LINE = "line"
    AREA = "area"
    COMBO = "combo"             # line + column
    PIE = "pie"
    SCATTER = "scatter"
    TABLE = "table"
    MATRIX = "matrix"           # pivot / crosstab
    CARD = "card"               # single number
    KPI = "kpi"
    SLICER = "slicer"           # interactive filter control
    MAP = "map"
    FUNNEL = "funnel"
    RIBBON = "ribbon"
    TREEMAP = "treemap"
    WATERFALL = "waterfall"
    TEXT = "text"               # textbox
    SHAPE = "shape"
    IMAGE = "image"
    BUTTON = "button"
    GROUP = "group"             # container of other visuals
    AI = "ai"                   # key drivers, decomposition tree, Q&A
    CUSTOM = "custom"           # third-party visual
    UNKNOWN = "unknown"


class FieldKind(Enum):
    COLUMN = "column"           # a raw attribute / dimension
    MEASURE = "measure"         # a model-defined calculation
    AGGREGATION = "aggregation"  # an inline aggregate over a column


class Aggregation(Enum):
    SUM = "sum"
    AVERAGE = "average"
    COUNT = "count"
    COUNT_DISTINCT = "count_distinct"
    MIN = "min"
    MAX = "max"
    OTHER = "other"


class Role(Enum):
    """Where a field sits on the visual. Vendor-neutral shelf names."""
    AXIS = "axis"               # category / x
    LEGEND = "legend"           # series / colour
    VALUE = "value"             # y / values / size
    ROWS = "rows"               # matrix rows
    COLUMNS = "columns"         # matrix columns
    DETAIL = "detail"           # tooltip / detail
    OTHER = "other"


@dataclass(frozen=True)
class FieldRef:
    """A reference to a model field as used by a visual or filter."""
    table: str
    name: str
    kind: FieldKind
    aggregation: Aggregation | None = None
    source_aggregation: int | None = None   # raw enum int for provenance
    display_name: str | None = None
    format_string: str | None = None
    query_name: str | None = None           # source's own identifier, e.g. "Sales.Total Sales"

    @property
    def is_measure_like(self) -> bool:
        return self.kind in (FieldKind.MEASURE, FieldKind.AGGREGATION)

    @property
    def qualified_name(self) -> str:
        return f"{self.table}.{self.name}"


@dataclass(frozen=True)
class Sort:
    """How the source ordered one visual's categories.

    This belongs to the visual, not to the field, because it is a presentation
    choice: "the top 10 clinics *by revenue*, highest first".
    """
    field: FieldRef                 # the field the categories are ordered BY
    descending: bool = False


@dataclass(frozen=True)
class TextRun:
    """A styled span of literal text, as written on a text box."""
    text: str
    size_pt: float | None = None
    bold: bool = False
    color: str | None = None            # "#rrggbb"


class ConditionOp(Enum):
    IN = "in"
    NOT_IN = "not_in"
    EQ = "eq"
    NE = "ne"
    GT = "gt"
    GE = "ge"
    LT = "lt"
    LE = "le"
    AND = "and"
    OR = "or"
    NOT = "not"
    UNKNOWN = "unknown"


@dataclass
class Condition:
    """A filter predicate tree. Leaves have a field and values; AND/OR/NOT have children."""
    op: ConditionOp
    field: FieldRef | None = None
    values: list = dc_field(default_factory=list)
    children: list["Condition"] = dc_field(default_factory=list)
    source: dict | None = None              # raw predicate for anything we could not map


class FilterScope(Enum):
    REPORT = "report"
    PAGE = "page"
    VISUAL = "visual"


@dataclass
class Filter:
    scope: FilterScope
    field: FieldRef | None
    condition: Condition | None = None      # None = a filter slot with no applied value
    name: str | None = None
    source_type: str | None = None          # e.g. "Categorical", "Advanced"


@dataclass
class Placement:
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0
    z: float = 0.0


@dataclass
class Visual:
    id: str
    chart_type: ChartType
    source_visual_type: str
    placement: Placement = dc_field(default_factory=Placement)
    title: str | None = None
    roles: dict[Role, list[FieldRef]] = dc_field(default_factory=dict)
    source_roles: dict[str, list[FieldRef]] = dc_field(default_factory=dict)
    filters: list[Filter] = dc_field(default_factory=list)
    sort: list[Sort] = dc_field(default_factory=list)
    text_runs: list[TextRun] = dc_field(default_factory=list)   # literal text, for text boxes
    show_labels: bool | None = None         # None = the source expressed no opinion
    group: str | None = None                # parent group id, if inside a visual group
    has_query: bool = False
    notes: list[str] = dc_field(default_factory=list)   # parser remarks, e.g. "no query"

    @property
    def fields(self) -> list[FieldRef]:
        seen, out = set(), []
        for refs in self.source_roles.values():
            for r in refs:
                if r not in seen:
                    seen.add(r)
                    out.append(r)
        return out

    @property
    def is_data_bound(self) -> bool:
        return bool(self.source_roles)

    @property
    def dimensions(self) -> list[FieldRef]:
        return [f for f in self.fields if f.kind is FieldKind.COLUMN]

    @property
    def measures(self) -> list[FieldRef]:
        return [f for f in self.fields if f.is_measure_like]


@dataclass
class Page:
    id: str
    name: str
    ordinal: int
    width: float = 0.0
    height: float = 0.0
    visuals: list[Visual] = dc_field(default_factory=list)
    filters: list[Filter] = dc_field(default_factory=list)
    hidden: bool = False

    @property
    def data_visuals(self) -> list[Visual]:
        return [v for v in self.visuals if v.is_data_bound]


@dataclass
class Measure:
    """A model measure with its definition. Only available via the model layer."""
    table: str
    name: str
    expression: str
    format_string: str | None = None
    hidden: bool = False


@dataclass
class Column:
    table: str
    name: str
    data_type: str | None = None
    calculated_expression: str | None = None    # DAX, for calculated columns
    hidden: bool = False


@dataclass
class Relationship:
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    active: bool = True
    many_to_one: bool = True


@dataclass
class DataModel:
    """Semantic model facts. Empty when only the report file bytes were available."""
    tables: list[str] = dc_field(default_factory=list)
    columns: list[Column] = dc_field(default_factory=list)
    measures: list[Measure] = dc_field(default_factory=list)
    relationships: list[Relationship] = dc_field(default_factory=list)
    source: str | None = None               # where it came from, for the report

    @property
    def available(self) -> bool:
        return bool(self.tables or self.measures)


@dataclass
class Report:
    name: str
    source_path: str
    pages: list[Page] = dc_field(default_factory=list)
    filters: list[Filter] = dc_field(default_factory=list)
    model: DataModel = dc_field(default_factory=DataModel)
    source_format: str = "unknown"
    palette: list[str] = dc_field(default_factory=list)     # the report's own series colours
    theme_name: str | None = None

    @property
    def visuals(self) -> list[Visual]:
        return [v for p in self.pages for v in p.visuals]

    @property
    def tables_referenced(self) -> list[str]:
        seen = set()
        for v in self.visuals:
            for f in v.fields:
                seen.add(f.table)
        for flt in self.filters + [f for p in self.pages for f in p.filters]:
            if flt.field:
                seen.add(flt.field.table)
        return sorted(seen)
