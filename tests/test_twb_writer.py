"""Milestone 5: the generated .twb is well-formed and internally consistent.

Every field expression on a shelf must resolve to a column-instance declared in
that worksheet, and every base column must exist in the CSV header. Opening the
file in Tableau Desktop is the final, manual verification (see PROJECT_PLAN).
"""
import csv
import re
import xml.etree.ElementTree as ET

import pytest

from src.analyzers.compatibility import analyze
from src.generators.twb_writer import DS_NAME, generate
from src.parsers.layout_parser import parse_pbix

_FIELD_EXPR = re.compile(r"\[" + re.escape(DS_NAME) + r"\]\.\[([^\]]+)\]")


@pytest.fixture(scope="module")
def retail_out(retail_pbix, tmp_path_factory):
    report = parse_pbix(retail_pbix)
    out = tmp_path_factory.mktemp("twb")
    return report, generate(report, analyze(report), out)


def test_files_written(retail_out):
    _, g = retail_out
    assert g.twb_path.is_file() and g.csv_path.is_file()
    assert g.twb_path.suffix == ".twb"


def test_worksheet_and_dashboard_counts(retail_out):
    _, g = retail_out
    # 16 data visuals, of which 2 slicers become dashboard filters -> 14 worksheets
    assert len(g.worksheets) == 14
    assert [s[2] for s in g.skipped] == ["slicer becomes a dashboard filter"] * 2
    assert g.dashboards == ["Overview", "District Monthly Sales", "New Stores"]   # Info page has no data
    assert len(set(g.worksheets)) == len(g.worksheets), "sheet names must be unique"


def test_workbook_is_well_formed_and_ordered(retail_out):
    _, g = retail_out
    root = ET.parse(g.twb_path).getroot()
    assert root.tag == "workbook" and root.get("version") == "18.1"
    assert root.get("source-build"), "Tableau 2026.2 requires a source-build attribute"
    tags = [c.tag for c in root]
    assert tags[:6] == ["document-format-change-manifest", "preferences", "datasources",
                        "worksheets", "dashboards", "windows"]
    assert tags[6].startswith("_.fcp.ExplainData"), "Tableau 2026.2 requires the explain-data node"
    assert len(root.find("worksheets")) == 14
    assert len(root.find("dashboards")) == 3


def test_every_shelf_field_is_declared(retail_out):
    _, g = retail_out
    root = ET.parse(g.twb_path).getroot()
    ds_columns = {c.get("name") for c in root.find("datasources")[0].findall("column")}
    for ws in root.find("worksheets"):
        declared = {ci.get("name") for ci in ws.iter("column-instance")}
        declared |= {c.get("name") for c in ws.find("table/view/datasource-dependencies").findall("column")}
        text = ET.tostring(ws, encoding="unicode")
        for inst in _FIELD_EXPR.findall(text):
            assert f"[{inst}]" in declared, f"{ws.get('name')}: [{inst}] used but not declared"
        for ci in ws.iter("column-instance"):
            assert ci.get("column") in ds_columns, f"{ci.get('column')} missing from datasource"


def test_csv_header_matches_base_columns(retail_out):
    _, g = retail_out
    root = ET.parse(g.twb_path).getroot()
    ds = root.find("datasources")[0]
    base = {c.get("name")[1:-1] for c in ds.findall("column")
            if c.find("calculation") is None and not c.get("name").startswith("[:")}
    with open(g.csv_path, newline="", encoding="utf-8") as fh:
        header = next(csv.reader(fh))
    assert set(header) == base
    mapped = {m.get("key")[1:-1] for m in ds.find("connection/cols")}
    assert mapped == base


def test_measures_become_placeholder_calculations(retail_out):
    _, g = retail_out
    assert "Total Sales Variance" in g.placeholder_measures
    assert g.translated_measures == []
    root = ET.parse(g.twb_path).getroot()
    calc = [c for c in root.find("datasources")[0].findall("column") if c.find("calculation") is not None]
    assert calc and all("placeholder" in c.find("calculation").get("formula") for c in calc)


def test_translated_measure_is_used_when_supplied(retail_pbix, tmp_path):
    report = parse_pbix(retail_pbix)
    g = generate(report, analyze(report), tmp_path,
                 translations={("Sales", "Total Sales Variance"): "SUM([Sales_This Year Sales]) - 1"})
    root = ET.parse(g.twb_path).getroot()
    col = next(c for c in root.find("datasources")[0].findall("column") if c.get("caption") == "Total Sales Variance")
    assert col.find("calculation").get("formula").startswith("SUM(")
    assert "Total Sales Variance" in g.translated_measures
    # an aggregate formula is referenced with the 'usr' derivation, not re-summed
    assert "[usr:Sales_Total Sales Variance:qk]" in ET.tostring(root, encoding="unicode")


def test_required_tableau_nodes_present(retail_out):
    """Each of these was proven required by bisecting the shipped Superstore.twb."""
    _, g = retail_out
    root = ET.parse(g.twb_path).getroot()
    for ws in root.find("worksheets"):
        assert ws.find("simple-id") is not None and ws.find("table/style") is not None
    for db in root.find("dashboards"):
        assert db.find("simple-id") is not None
    for win in root.find("windows"):
        assert win.find("simple-id") is not None
    text = g.twb_path.read_text(encoding="utf-8")
    assert "\n0\"" not in text and "&#10;0" in text, "newlines in calculations must be entity-escaped"


def test_dashboard_zones_are_within_bounds(retail_out):
    """Every zone is either a worksheet we generated or a text box we carried over."""
    _, g = retail_out
    root = ET.parse(g.twb_path).getroot()
    text_zones = 0
    for db in root.find("dashboards"):
        for z in db.find("zones")[0]:
            for k in ("x", "y", "w", "h"):
                assert 0 <= int(z.get(k)) <= 100000
            if z.get("type-v2") == "text":
                text_zones += 1
                assert z.find("formatted-text/run") is not None, "a text zone must carry its text"
                assert z.get("name") is None
            else:
                assert z.get("name") in g.worksheets
    assert text_zones, "the Retail report has 7 text boxes; its headings must survive"


def test_page_headings_carry_their_styling(retail_out):
    """The source's own font size and weight reach the dashboard."""
    _, g = retail_out
    root = ET.parse(g.twb_path).getroot()
    runs = [r for db in root.find("dashboards") for r in db.iter("run")]
    assert runs, "no text runs emitted"
    assert any(r.get("fontsize") for r in runs)
    assert any((r.text or "").strip() == "Store Sales Overview" for r in runs)


def test_regional_generates_without_error(regional_pbix, tmp_path):
    report = parse_pbix(regional_pbix)
    g = generate(report, analyze(report), tmp_path)
    assert len(g.worksheets) > 30
    assert any("no Tableau counterpart" in s[2] or "custom visual" in s[2].lower() for s in g.skipped)
    ET.parse(g.twb_path)      # well-formed


def test_treemap_uses_square_mark_with_size_not_shelves(tmp_path):
    """Tableau's treemap has nothing on rows/cols: dimension on Detail, measure on Size."""
    from src.models.ir import (
        Aggregation, ChartType, FieldKind, FieldRef, Page, Report, Role, Visual,
    )
    dim = FieldRef("Product", "Category", FieldKind.COLUMN)
    val = FieldRef("Sales", "Amount", FieldKind.AGGREGATION, aggregation=Aggregation.SUM)
    v = Visual(id="v", chart_type=ChartType.TREEMAP, source_visual_type="treemap",
               source_roles={"Group": [dim], "Values": [val]},
               roles={Role.LEGEND: [dim], Role.VALUE: [val]})
    report = Report(name="t", source_path="x", pages=[Page(id="p", name="P", ordinal=0, width=100, height=100, visuals=[v])])
    g = generate(report, analyze(report), tmp_path)
    ws = ET.parse(g.twb_path).getroot().find("worksheets")[0]
    assert ws.find("table/panes/pane/mark").get("class") == "Square"
    assert not (ws.find("table/rows").text or "") and not (ws.find("table/cols").text or "")
    assert ws.find("table/panes/pane/encodings/size") is not None


def test_only_the_page_tabs_are_visible(retail_out):
    """The tab bar should mirror Power BI's pages, not expose 14 building blocks."""
    _, g = retail_out
    windows = ET.parse(g.twb_path).getroot().find("windows")
    visible = [w for w in windows if w.get("hidden") != "true"]
    assert [w.get("name") for w in visible] == g.dashboards
    assert all(w.get("class") == "dashboard" for w in visible)
    assert len([w for w in windows if w.get("hidden") == "true"]) == len(g.hidden_sheets) == len(g.worksheets)


def test_dashboard_windows_carry_a_viewpoint_per_sheet(retail_out):
    """Tableau rejects a dashboard window with an empty <viewpoints> block."""
    _, g = retail_out
    root = ET.parse(g.twb_path).getroot()
    zones = {db.get("name"): [z.get("name") for z in db.find("zones")[0] if z.get("name")]
             for db in root.find("dashboards")}
    for w in root.find("windows"):
        if w.get("class") != "dashboard":
            continue
        names = [v.get("name") for v in w.find("viewpoints")]
        assert names, "an empty viewpoints block makes the workbook unloadable"
        assert names == zones[w.get("name")]


def test_every_relation_declares_its_csv_schema(retail_out):
    """Without a separator, Tableau guesses - a one-column CSV has no comma to find."""
    _, g = retail_out
    root = ET.parse(g.twb_path).getroot()
    for ds in root.find("datasources"):
        schema = ds.find("connection/relation/columns")
        assert schema is not None, ds.get("caption")
        assert schema.get("separator") == "," and schema.get("header") == "yes"
        assert schema.get("character-set") == "UTF-8"
        declared = [c.get("name") for c in schema]
        mapped = [m.get("key")[1:-1] for m in ds.find("connection/cols")]
        assert declared == mapped, ds.get("caption")


def test_twbx_packages_workbook_and_data_together(retail_out):
    """Downloading only the .twb leaves Tableau hunting for data\model_stub.csv."""
    import zipfile
    _, g = retail_out
    assert g.twbx_path and g.twbx_path.is_file() and g.twbx_path.suffix == ".twbx"
    with zipfile.ZipFile(g.twbx_path) as z:
        names = z.namelist()
        assert g.twb_path.name in names
        csvs = [n for n in names if n.startswith("data/") and n.endswith(".csv")]
        assert csvs, "the package must carry the data the connections name"
        # every connection's file must actually be inside the package
        root = ET.fromstring(z.read(g.twb_path.name).decode("utf-8"))
        for c in root.iter("connection"):
            if c.get("class") == "textscan":
                assert f"{c.get('directory')}/{c.get('filename')}" in names, c.get("filename")


def test_snapshot_values_decide_column_types(tmp_path):
    """A Power BI card holding formatted text must not be declared numeric and SUMmed."""
    from dataclasses import dataclass, field as dcf
    from src.models.ir import ChartType, FieldKind, FieldRef, Page, Report, Role, Visual

    @dataclass
    class Col:
        name: str

    @dataclass
    class Snap:
        columns: list
        rows: list

    text_val = FieldRef("_Measures", "Total Revenue Display", FieldKind.MEASURE)
    v = Visual(id="v", chart_type=ChartType.CARD, source_visual_type="cardVisual",
               source_roles={"Data": [text_val]}, roles={Role.VALUE: [text_val]})
    page = Page(id="p", name="P", ordinal=0, width=100, height=100, visuals=[v])
    report = Report(name="t", source_path="x", pages=[page])
    snap = Snap(columns=[Col("_Measures_Total Revenue Display")], rows=[["₹470.75 Cr"]])
    g = generate(report, analyze(report), tmp_path, snapshots={"p/v": snap})

    assert g.retyped == ["_Measures_Total Revenue Display"]
    root = ET.parse(g.twb_path).getroot()
    col = next(c for c in root.find("datasources")[-1].findall("column") if "Display" in c.get("name"))
    assert col.get("datatype") == "string" and col.get("role") == "dimension"
    text = root.find("worksheets")[0].find(".//encodings/text")
    assert text is not None and ":nk]" in text.get("column"), "text card must not be aggregated"
