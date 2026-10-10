"""Milestone 12: per-visual DAX snapshots are built correctly and results map back to columns."""
import pytest

from src.extractors.data_extract import (
    ExtractColumn, ExtractPlan, dax_literal, filter_arguments, normalise_rows, plan_for_visual,
)
from src.models.ir import (
    Aggregation, ChartType, Condition, ConditionOp, FieldKind, FieldRef, Filter, FilterScope, Page, Visual,
)
from src.parsers.layout_parser import parse_pbix


@pytest.mark.parametrize("value, expected", [
    ("Same Store", '"Same Store"'), ('say "hi"', '"say ""hi"""'), (5, "5"), (2.5, "2.5"),
    (True, "TRUE()"), (None, "BLANK()"),
])
def test_dax_literal(value, expected):
    assert dax_literal(value) == expected


def _f(table, name, kind=FieldKind.COLUMN, agg=None):
    return FieldRef(table, name, kind, aggregation=agg)


def _flt(op, field, values=None, children=None):
    return Filter(scope=FilterScope.VISUAL, field=field,
                  condition=Condition(op, field=field, values=values or [], children=children or []))


def test_filter_arguments_cover_the_supported_predicates():
    col = _f("Store", "Store Type")
    notes = []
    args = filter_arguments([
        _flt(ConditionOp.IN, col, ["A", "B"]),
        _flt(ConditionOp.EQ, col, ["Same Store"]),
        _flt(ConditionOp.NOT_IN, col, [None]),
        _flt(ConditionOp.GT, _f("Sales", "Amount"), [10]),
        Filter(scope=FilterScope.VISUAL, field=col, condition=Condition(ConditionOp.AND, children=[
            Condition(ConditionOp.LE, field=col, values=[3]), Condition(ConditionOp.NE, field=col, values=["x"])])),
    ], "visual", notes)
    assert args == [
        "TREATAS({\"A\", \"B\"}, 'Store'[Store Type])",
        "TREATAS({\"Same Store\"}, 'Store'[Store Type])",
        "FILTER(ALL('Store'[Store Type]), NOT ('Store'[Store Type] IN {BLANK()}))",
        "FILTER(ALL('Sales'[Amount]), 'Sales'[Amount] > 10)",
        "FILTER(ALL('Store'[Store Type]), 'Store'[Store Type] <= 3)",
        "FILTER(ALL('Store'[Store Type]), 'Store'[Store Type] <> \"x\")",
    ]
    assert notes == []


def test_unsupported_filters_are_noted_not_guessed():
    notes = []
    measure = _f("Sales", "Total", FieldKind.MEASURE)
    args = filter_arguments([_flt(ConditionOp.GT, measure, [0]),
                             Filter(scope=FilterScope.PAGE, field=_f("T", "C"),
                                    condition=Condition(ConditionOp.UNKNOWN, field=_f("T", "C")))], "page", notes)
    assert args == [] and len(notes) == 2


def test_plan_for_a_real_retail_column_chart(retail_pbix):
    report = parse_pbix(retail_pbix)
    page = report.pages[1]
    v = next(v for v in page.visuals if any(f.name == "Total Sales Variance" for f in v.fields)
             and v.source_visual_type == "clusteredColumnChart")
    plan = plan_for_visual(page, v, report.filters)
    assert plan.dax == ("EVALUATE SUMMARIZECOLUMNS('Time'[FiscalMonth], 'District'[DM], "
                        "\"Sales_Total Sales Variance\", [Total Sales Variance])")
    assert [c.name for c in plan.columns] == ["Time_FiscalMonth", "District_DM", "Sales_Total Sales Variance"]
    assert plan.columns[-1].role == "measure" and plan.columns[0].role == "dimension"


def test_plan_applies_visual_filter_and_aggregations(retail_pbix):
    report = parse_pbix(retail_pbix)
    page, v = next((p, v) for p in report.pages for v in p.visuals if any(f.condition for f in v.filters))
    plan = plan_for_visual(page, v, report.filters)
    assert "TREATAS({\"Same Store\"}, 'Store'[Store Type])" in plan.dax


def test_plan_card_with_only_a_measure():
    page = Page(id="p", name="P", ordinal=0)
    v = Visual(id="v", chart_type=ChartType.CARD, source_visual_type="card",
               source_roles={"Values": [_f("Store", "Total Stores", FieldKind.MEASURE)]})
    plan = plan_for_visual(page, v, [])
    assert plan.dax == 'EVALUATE SUMMARIZECOLUMNS("Store_Total Stores", [Total Stores])'


def test_plan_inline_aggregation_uses_dax_function():
    page = Page(id="p", name="P", ordinal=0)
    v = Visual(id="v", chart_type=ChartType.BAR, source_visual_type="barChart",
               source_roles={"Category": [_f("Opp", "Status")],
                             "Y": [_f("Opp", "Value", FieldKind.AGGREGATION, Aggregation.AVERAGE)]})
    plan = plan_for_visual(page, v, [])
    assert plan.dax == "EVALUATE SUMMARIZECOLUMNS('Opp'[Status], \"Opp_Value\", AVERAGE('Opp'[Value]))"


def test_unbound_visual_has_no_plan():
    assert plan_for_visual(Page(id="p", name="P", ordinal=0),
                           Visual(id="t", chart_type=ChartType.TEXT, source_visual_type="textbox"), []) is None


def test_normalise_rows_maps_headers_dates_and_nulls():
    plan = ExtractPlan(key="k", dax="", columns=[
        ExtractColumn("Time[FiscalMonth]", "Time_FiscalMonth", "Time.FiscalMonth", "string", "dimension", "nominal"),
        ExtractColumn("[Sales_Total]", "Sales_Total", "Total", "real", "measure", "quantitative"),
        ExtractColumn("Time[Date]", "Time_Date", "Time.Date", "string", "dimension", "nominal"),
    ])
    rows = normalise_rows(plan, [
        {"Time[FiscalMonth]": "Jan", "[Sales_Total]": 1234.5, "Time[Date]": "/Date(1420070400000)/"},
        {"Time[FiscalMonth]": "Feb", "[Sales_Total]": None, "Time[Date]": "2015-02-01 00:00:00"},
        {"Time[FiscalMonth]": "Mar", "[Sales_Total]": 10.0, "Time[Date]": None},
    ])
    assert rows == [["Jan", "1234.5", "2015-01-01 00:00:00"], ["Feb", "", "2015-02-01 00:00:00"], ["Mar", "10", ""]]
    assert normalise_rows(plan, []) == []
