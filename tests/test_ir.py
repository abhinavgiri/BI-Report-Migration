"""Milestone 2: the IR is vendor-neutral, hashable where needed, and convenient."""
import inspect

from src.models import ir
from src.models.ir import (
    Aggregation, ChartType, FieldKind, FieldRef, Page, Placement, Report, Role, Visual,
)


def _field(table, name, kind=FieldKind.COLUMN, agg=None):
    return FieldRef(table=table, name=name, kind=kind, aggregation=agg)


def test_fieldref_is_hashable_and_value_equal():
    a = _field("Sales", "Amount", FieldKind.AGGREGATION, Aggregation.SUM)
    b = _field("Sales", "Amount", FieldKind.AGGREGATION, Aggregation.SUM)
    assert a == b and hash(a) == hash(b)
    assert len({a, b}) == 1


def test_visual_fields_deduplicate_across_roles():
    dim = _field("Time", "Month")
    m = _field("Sales", "Total", FieldKind.MEASURE)
    v = Visual(id="v1", chart_type=ChartType.COLUMN, source_visual_type="columnChart",
               source_roles={"Category": [dim], "Y": [m], "Tooltips": [dim]})
    assert v.fields == [dim, m]
    assert v.dimensions == [dim]
    assert v.measures == [m]
    assert v.is_data_bound


def test_unbound_visual_is_not_data_bound():
    v = Visual(id="t", chart_type=ChartType.TEXT, source_visual_type="textbox")
    assert not v.is_data_bound and v.fields == []


def test_report_collects_tables_from_visuals_and_filters():
    v = Visual(id="v", chart_type=ChartType.BAR, source_visual_type="barChart",
               source_roles={"Y": [_field("Sales", "Total", FieldKind.MEASURE)]})
    page = Page(id="p", name="Overview", ordinal=0, visuals=[v],
                filters=[ir.Filter(scope=ir.FilterScope.PAGE, field=_field("District", "DM"))])
    r = Report(name="r", source_path="x.pbix", pages=[page])
    assert r.tables_referenced == ["District", "Sales"]
    assert r.visuals == [v]
    assert page.data_visuals == [v]
    assert not r.model.available


def test_ir_contains_no_vendor_vocabulary():
    """The IR must not mention either vendor by name in its public surface."""
    src = inspect.getsource(ir).lower()
    body = src.split('"""', 2)[2]          # skip the module docstring
    for word in ("tableau", "power bi", "pbix", "twb", "dax"):
        # DAX appears only in a comment explaining calculated_expression provenance
        assert body.count(word) <= (1 if word == "dax" else 0), word


def test_placement_defaults():
    assert Placement() == Placement(0.0, 0.0, 0.0, 0.0, 0.0)
