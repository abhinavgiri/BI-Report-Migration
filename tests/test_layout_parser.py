"""Milestone 3: Report/Layout -> IR, checked against the measured inventories."""
from collections import Counter

import pytest

from src.models.ir import (
    Aggregation, ChartType, ConditionOp, FieldKind, FilterScope, Role,
)
from src.parsers.layout_parser import (
    field_from_queryref, parse_condition, parse_literal, parse_pbix,
)


@pytest.fixture(scope="module")
def retail(retail_pbix):
    return parse_pbix(retail_pbix)


@pytest.fixture(scope="module")
def regional(regional_pbix):
    return parse_pbix(regional_pbix)


# --- literals & helpers ----------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("'Same Store'", "Same Store"),
    ("'O''Brien'", "O'Brien"),
    ("0L", 0), ("-12L", -12),
    ("0M", 0.0), ("2.5M", 2.5),
    ("true", True), ("false", False),
    ("null", None),
    ("datetime'2024-01-01T00:00:00'", "2024-01-01T00:00:00"),
])
def test_parse_literal(raw, expected):
    assert parse_literal(raw) == expected


def test_field_from_queryref_handles_aggregate_and_bare():
    agg = field_from_queryref("Sum(Sales.Amount)")
    assert (agg.table, agg.name, agg.kind, agg.aggregation) == ("Sales", "Amount", FieldKind.AGGREGATION, Aggregation.SUM)
    bare = field_from_queryref("District.DM")
    assert (bare.table, bare.name) == ("District", "DM")


# --- Retail Analysis (Phase 1 target) --------------------------------------

def test_retail_page_and_visual_counts(retail):
    assert [p.name for p in retail.pages] == ["Info", "Overview", "District Monthly Sales", "New Stores"]
    assert len(retail.visuals) == 24
    assert sum(1 for v in retail.visuals if v.is_data_bound) == 16


def test_retail_chart_types(retail):
    kinds = Counter(v.chart_type for v in retail.visuals)
    assert kinds[ChartType.COLUMN] == 5          # 4 clustered + 1 plain column
    assert kinds[ChartType.TEXT] == 7
    assert kinds[ChartType.UNKNOWN] == 0 and kinds[ChartType.CUSTOM] == 0


def test_retail_column_chart_fields_by_role(retail):
    v = next(v for v in retail.visuals
             if v.source_visual_type == "clusteredColumnChart"
             and any(f.name == "Total Sales Variance" for f in v.fields))
    axis = v.roles[Role.AXIS]
    assert axis[0].table == "Time" and axis[0].name == "FiscalMonth" and axis[0].kind is FieldKind.COLUMN
    assert v.roles[Role.LEGEND][0].qualified_name == "District.DM"
    val = v.roles[Role.VALUE][0]
    assert val.kind is FieldKind.MEASURE and val.qualified_name == "Sales.Total Sales Variance"
    assert v.has_query


def test_retail_fields_carry_display_metadata(retail):
    card = next(v for v in retail.visuals if v.chart_type is ChartType.CARD)
    f = card.roles[Role.VALUE][0]
    assert f.display_name == "Total Stores"
    assert f.format_string == "0"
    assert f.query_name == "Store.Total Stores"


def test_retail_positions_come_from_config_layouts(retail):
    """Retail has no top-level x/y, so a zero position would mean we read the wrong place."""
    bound = [v for v in retail.visuals if v.is_data_bound]
    assert all(v.placement.width > 0 and v.placement.height > 0 for v in bound)
    assert retail.pages[1].width == 1280 and retail.pages[1].height == 720


def test_retail_page_filters(retail):
    overview = retail.pages[1]
    assert len(overview.filters) == 6
    assert all(f.scope is FilterScope.PAGE for f in overview.filters)
    assert overview.filters[0].field.qualified_name == "District.District"
    assert overview.filters[0].condition is None       # a slot with no value applied


def test_retail_visual_filter_with_condition(retail):
    flt = next(f for v in retail.visuals for f in v.filters if f.condition)
    assert flt.field.qualified_name == "Store.Store Type"
    assert flt.condition.op is ConditionOp.EQ
    assert flt.condition.values == ["Same Store"]


def test_retail_tables_referenced(retail):
    assert set(retail.tables_referenced) >= {"Sales", "Store", "District", "Time"}


# --- Regional Sales (Phase 2 stress test) ----------------------------------

def test_regional_survives_and_matches_inventory(regional):
    assert len(regional.pages) == 11
    assert len(regional.visuals) == 937
    assert sum(1 for v in regional.visuals if v.is_data_bound) == 55
    assert sum(1 for v in regional.visuals if v.chart_type is ChartType.GROUP) == 45
    assert sum(1 for v in regional.visuals if v.chart_type is ChartType.BUTTON) == 732


def test_regional_bound_without_query_is_noted_not_dropped(regional):
    noted = [v for v in regional.visuals if v.is_data_bound and not v.has_query]
    assert len(noted) == 7
    assert all(any("no query" in n for n in v.notes) for v in noted)
    assert {v.source_visual_type for v in noted} == {
        "lineStackedColumnComboChart", "pivotTable", "keyDriversVisual", "qnaVisual"}


def test_regional_custom_visual_is_flagged(regional):
    custom = [v for v in regional.visuals if v.chart_type is ChartType.CUSTOM]
    assert len(custom) == 1 and custom[0].source_visual_type.startswith("FlowVisual_")


def test_regional_group_membership(regional):
    members = [v for v in regional.visuals if v.group]
    assert members, "visuals inside groups should record their parent"
    groups = {v.id for v in regional.visuals if v.chart_type is ChartType.GROUP}
    assert all(v.group in groups for v in members)


def test_regional_condition_kinds_all_mapped(regional):
    ops = Counter()
    def walk(c):
        ops[c.op] += 1
        for ch in c.children:
            walk(ch)
    for f in regional.filters + [f for p in regional.pages for f in p.filters] \
             + [f for v in regional.visuals for f in v.filters]:
        if f.condition:
            walk(f.condition)
    assert ops[ConditionOp.UNKNOWN] == 0, dict(ops)
    assert ops[ConditionOp.IN] and ops[ConditionOp.AND] and ops[ConditionOp.NOT_IN]


def test_parse_condition_not_in():
    cond = {"Not": {"Expression": {"In": {
        "Expressions": [{"Column": {"Expression": {"SourceRef": {"Source": "c"}}, "Property": "Campaign"}}],
        "Values": [[{"Literal": {"Value": "null"}}]]}}}}
    c = parse_condition(cond, {"c": "Campaigns"})
    assert c.op is ConditionOp.NOT_IN and c.field.qualified_name == "Campaigns.Campaign" and c.values == [None]
