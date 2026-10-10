"""Milestone 4: every visual gets a verdict and a reason; nothing is dropped."""
import pytest

from src.analyzers.compatibility import (
    CAPABILITY_MATRIX, Verdict, analyze, assess_visual, render_markdown,
)
from src.models.ir import ChartType, FieldKind, FieldRef, Visual
from src.parsers.layout_parser import parse_pbix


def test_matrix_covers_every_chart_type():
    assert set(CAPABILITY_MATRIX) == set(ChartType)


def test_unbound_data_visual_is_manual():
    v = Visual(id="v", chart_type=ChartType.BAR, source_visual_type="barChart")
    a = assess_visual(v, "p")
    assert a.verdict is Verdict.MANUAL and len(a.reasons) == 2


def test_measure_use_is_explained_but_not_downgraded():
    v = Visual(id="v", chart_type=ChartType.COLUMN, source_visual_type="columnChart",
               source_roles={"Category": [FieldRef("T", "Month", FieldKind.COLUMN)],
                             "Y": [FieldRef("S", "Total", FieldKind.MEASURE)]}, has_query=True)
    v.roles = {}  # roles map not needed for this check
    a = assess_visual(v, "p")
    assert a.verdict is Verdict.DIRECT
    assert any("S.Total" in r for r in a.reasons)


@pytest.fixture(scope="module")
def retail_assessment(retail_pbix):
    return analyze(parse_pbix(retail_pbix))


def test_retail_every_visual_assessed(retail_assessment):
    ra = retail_assessment
    assert len(ra.assessments) == 24
    counts = ra.counts()
    assert counts[Verdict.DECORATIVE] == 8            # 7 textboxes + 1 image
    assert counts[Verdict.UNSUPPORTED] == 0
    assert len(ra.data_visuals) == 16
    assert len(ra.migratable) == 16                    # all classic chart types


def test_retail_verdict_breakdown(retail_assessment):
    by_type = {}
    for a in retail_assessment.data_visuals:
        by_type.setdefault(a.visual.chart_type, set()).add(a.verdict)
    assert by_type[ChartType.COLUMN] == {Verdict.DIRECT}
    assert by_type[ChartType.LINE] == {Verdict.DIRECT}
    assert by_type[ChartType.CARD] == {Verdict.APPROXIMATE}
    assert by_type[ChartType.SLICER] == {Verdict.APPROXIMATE}
    assert by_type[ChartType.MAP] == {Verdict.APPROXIMATE}


def test_retail_markdown_report(retail_assessment):
    md = render_markdown(retail_assessment)
    assert md.startswith("# Migration assessment: Retail Analysis Sample PBIX")
    assert "16 of 16 data visuals (100%)" in md
    assert "### Overview" in md and "### New Stores" in md
    assert "Model layer: **not available**" in md
    assert "`Sales.Total Sales Variance`" in md


def test_regional_nothing_dropped(regional_pbix):
    ra = analyze(parse_pbix(regional_pbix))
    assert len(ra.assessments) == 937
    counts = ra.counts()
    assert counts[Verdict.UNSUPPORTED] == 6            # 3 keyDrivers + decomposition + qna + custom
    assert counts[Verdict.DECORATIVE] == 732 + 68 + 26 + 45 + 11
    md = render_markdown(ra)
    assert "Win/Loss Ratio Overview" in md


def test_modern_visual_types_are_mapped():
    """Types found in a real 2026 report: the newer card, treemap and waterfall."""
    from src.parsers.layout_parser import ROLE_MAP, VISUAL_TYPE_MAP
    from src.models.ir import Role
    assert VISUAL_TYPE_MAP["cardVisual"] is ChartType.CARD
    assert VISUAL_TYPE_MAP["treemap"] is ChartType.TREEMAP
    assert VISUAL_TYPE_MAP["waterfallChart"] is ChartType.WATERFALL
    assert ROLE_MAP["Data"] is Role.VALUE and ROLE_MAP["Group"] is Role.LEGEND


def test_treemap_is_direct_and_waterfall_is_manual():
    assert CAPABILITY_MATRIX[ChartType.TREEMAP].verdict is Verdict.DIRECT
    assert CAPABILITY_MATRIX[ChartType.TREEMAP].target_mark == "Square"
    assert CAPABILITY_MATRIX[ChartType.WATERFALL].verdict is Verdict.MANUAL
    assert "RUNNING_SUM" in CAPABILITY_MATRIX[ChartType.WATERFALL].note
