"""Milestone 12 follow-ups found by running the real engine on Regional Sales."""
from pathlib import Path

from src.extractors.data_extract import filter_arguments, plan_for_visual
from src.models.ir import (
    Condition, ConditionOp, DataModel, FieldKind, FieldRef, Filter, FilterScope, Measure, Column, Page, Visual,
    ChartType,
)
from src.parsers.layout_parser import parse_pbix
from src.pipeline import reconcile_fields, run_pipeline
from tests.test_pipeline_data import _fake_engine

FIXTURES = Path(__file__).parent / "fixtures" / "dmv"


def test_hierarchy_levels_parse_to_their_column(regional_pbix):
    """Accounts / Street Hierarchy / State or Province must become Accounts.State or Province."""
    report = parse_pbix(regional_pbix)
    refs = {f for v in report.visuals for f in v.fields if f.table == "Accounts"}
    names = {f.name for f in refs}
    assert "State or Province" in names
    assert not any("Street Hierarchy." in n for n in names)


def _col(table, name):
    return FieldRef(table, name, FieldKind.COLUMN)


def test_empty_in_filter_is_skipped_with_a_note():
    notes = []
    f = Filter(scope=FilterScope.PAGE, field=_col("T", "C"), condition=Condition(ConditionOp.IN, field=_col("T", "C"), values=[]))
    assert filter_arguments([f], "page", notes) == []
    assert notes and "no values" in notes[0]


def test_not_and_blank_predicates():
    notes = []
    c = _col("Opportunity Calendar", "RELATIVE MONTH")
    tree = Condition(ConditionOp.AND, children=[
        Condition(ConditionOp.GE, field=c, values=[-6]),
        Condition(ConditionOp.NOT, children=[Condition(ConditionOp.EQ, field=c, values=[None])]),
    ])
    args = filter_arguments([Filter(scope=FilterScope.PAGE, field=c, condition=tree)], "page", notes)
    assert args == [
        "FILTER(ALL('Opportunity Calendar'[RELATIVE MONTH]), 'Opportunity Calendar'[RELATIVE MONTH] >= -6)",
        "FILTER(ALL('Opportunity Calendar'[RELATIVE MONTH]), NOT (ISBLANK('Opportunity Calendar'[RELATIVE MONTH])))",
    ]
    assert notes == []


def test_measure_filters_are_named_in_the_note():
    notes = []
    m = FieldRef("Owners", "Rev Goal", FieldKind.MEASURE)
    filter_arguments([Filter(scope=FilterScope.PAGE, field=m, condition=Condition(ConditionOp.GT, field=m, values=[0]))], "page", notes)
    assert notes == ["page filter on measure Owners.Rev Goal not applied (needs the visual's full evaluation context)"]


def test_snapshot_filter_modes(retail_pbix):
    report = parse_pbix(retail_pbix)
    page, v = next((p, v) for p in report.pages for v in p.visuals if any(f.condition for f in v.filters))
    page.filters.append(Filter(scope=FilterScope.PAGE, field=_col("Time", "FiscalYear"),
                               condition=Condition(ConditionOp.EQ, field=_col("Time", "FiscalYear"), values=["FY2016"])))
    all_ = plan_for_visual(page, v, report.filters, "all")
    vis = plan_for_visual(page, v, report.filters, "visual")
    none = plan_for_visual(page, v, report.filters, "none")
    assert "FY2016" in all_.dax and "Same Store" in all_.dax
    assert "FY2016" not in vis.dax and "Same Store" in vis.dax
    assert "TREATAS" not in none.dax and none.filters_applied == []


def test_reconcile_uses_the_model_to_fix_name_only_fields():
    v = Visual(id="v", chart_type=ChartType.CARD, source_visual_type="card",
               source_roles={"Values": [_col("Opportunities", "Revenue Won")]})
    v.roles = {}
    page = Page(id="p", name="P", ordinal=0, visuals=[v])
    from src.models.ir import Report
    r = Report(name="r", source_path="x", pages=[page])
    r.model = DataModel(tables=["Opportunities"], measures=[Measure("Opportunities", "Revenue Won", "1")],
                        columns=[Column("Opportunities", "Value")])
    assert reconcile_fields(r) == 1
    assert v.fields[0].kind is FieldKind.MEASURE
    assert plan_for_visual(page, v, []).dax == 'EVALUATE SUMMARIZECOLUMNS("Opportunities_Revenue Won", [Revenue Won])'


def test_regional_snapshot_queries_use_measures_from_the_model(regional_pbix, tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr("src.pipeline.run_queries", _fake_engine(captured))
    monkeypatch.setattr("src.pipeline.live_port", lambda report: 1)
    r = run_pipeline(regional_pbix, out_dir=tmp_path, model_dir=str(FIXTURES), data_mode="live")
    queries = captured["queries"].values()
    assert not any("'Opportunities'[Revenue Won]" in q for q in queries), "a measure must never be asked for as a column"
    assert any("[Revenue Won]" in q for q in queries)
    assert not any("Street Hierarchy." in q for q in queries)
    empties = [p for p in r["data"]["problems"] if p["kind"] == "empty"]
    assert empties == []          # the fake engine always returns rows
