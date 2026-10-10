"""The presentation details we carry across: number formats, sort, colour, headings.

Everything asserted here is data the .pbix genuinely contains. Where a report does
not carry something - Retail names a built-in theme that is not embedded - the test
asserts that we leave it alone rather than substitute our own choice.
"""
import xml.etree.ElementTree as ET

import pytest

from src.analyzers.compatibility import analyze
from src.generators.twb_writer import generate, tableau_number_format
from src.parsers.layout_parser import parse_pbix


# --- number formats ----------------------------------------------------------

@pytest.mark.parametrize("pbi, expected", [
    (r"\$#,0;(\$#,0);\$#,0", 'c"$"#,##0'),
    (r"\$#,0.00;(\$#,0.00);\$#,0.00", 'c"$"#,##0.00'),
    ('"₹" #,0.00;"₹" -#,0.00;"₹" #,0.00', 'c"₹"#,##0.00'),
    ("0.00 %;-0.00 %;0.00 %", "p0.00%"),
    ("0.0%;-0.0%;0.0%", "p0.0%"),
    ("#,0%;-#,0%;#,0%", "p0%"),
    ("#,0", "n#,##0"),
    ("0", "n0"),
    ("0.00", "n0.00"),
    # '#' is an OPTIONAL digit: it must not become a forced zero
    (r"\$#,0.###;(x);(y)", 'c"$"#,##0.###'),
    # no numeric opinion -> leave Tableau's default alone
    ("G", None), ("D", None), ("d", None), ("dd/mm/yyyy", None), ("", None), (None, None),
])
def test_number_format_mapping(pbi, expected):
    assert tableau_number_format(pbi) == expected


def test_real_reports_get_number_formats(retail_pbix, tmp_path):
    report = parse_pbix(retail_pbix)
    g = generate(report, analyze(report), tmp_path)
    root = ET.parse(g.twb_path).getroot()
    formats = [c.get("default-format") for c in root.iter("column") if c.get("default-format")]
    assert formats, "the Retail report carries currency formats; none reached the workbook"
    assert any(f.startswith('c"$"') for f in formats), formats[:5]


# --- sort --------------------------------------------------------------------

def test_order_by_is_parsed(retail_pbix):
    report = parse_pbix(retail_pbix)
    sorted_visuals = [v for v in report.visuals if v.sort]
    assert len(sorted_visuals) >= 5
    assert any(s.descending for v in sorted_visuals for s in v.sort)


def test_computed_sort_only_references_declared_columns(retail_pbix, tmp_path):
    """A sort pointing at a column the worksheet never declares breaks the sheet."""
    report = parse_pbix(retail_pbix)
    g = generate(report, analyze(report), tmp_path)
    root = ET.parse(g.twb_path).getroot()
    checked = 0
    for ws in root.find("worksheets"):
        declared = {c.get("name") for c in ws.iter("column-instance")}
        for cs in ws.iter("computed-sort"):
            checked += 1
            for attr in ("column", "using"):
                local = "[" + cs.get(attr).split(".[", 1)[-1]
                assert local in declared, f"{ws.get('name')}: {attr}={local} not declared"
    assert checked, "no computed-sort emitted for a report that has OrderBy"


# --- theme / colour ----------------------------------------------------------

def test_theme_palette_read_when_embedded(regional_pbix):
    report = parse_pbix(regional_pbix)
    assert report.theme_name == "Fidex"
    assert report.palette and all(c.startswith("#") and len(c) == 7 for c in report.palette)


def test_no_palette_invented_when_the_file_ships_none(retail_pbix, tmp_path):
    """Retail names the built-in theme "Concourse", which is not embedded.

    We must not colour its marks with a palette the report never chose.
    """
    report = parse_pbix(retail_pbix)
    assert report.palette == []
    g = generate(report, analyze(report), tmp_path)
    root = ET.parse(g.twb_path).getroot()
    assert not [f for f in root.iter("format") if f.get("attr") == "mark-color"]


def test_palette_colours_marks_when_the_file_ships_one(regional_pbix, tmp_path):
    report = parse_pbix(regional_pbix)
    g = generate(report, analyze(report), tmp_path)
    root = ET.parse(g.twb_path).getroot()
    colours = {f.get("value") for f in root.iter("format") if f.get("attr") == "mark-color"}
    assert colours and colours <= set(report.palette)


# --- headings and fit --------------------------------------------------------

def test_text_boxes_become_dashboard_zones(regional_pbix, tmp_path):
    report = parse_pbix(regional_pbix)
    g = generate(report, analyze(report), tmp_path)
    root = ET.parse(g.twb_path).getroot()
    zones = [z for z in root.iter("zone") if z.get("type-v2") == "text"]
    assert len(zones) >= 10
    assert all(z.find("formatted-text/run") is not None for z in zones)


def test_light_headings_get_a_backdrop(regional_pbix, tmp_path):
    """White heading text must not land invisibly on a white dashboard."""
    report = parse_pbix(regional_pbix)
    g = generate(report, analyze(report), tmp_path)
    root = ET.parse(g.twb_path).getroot()
    for z in root.iter("zone"):
        if z.get("type-v2") != "text":
            continue
        runs = z.findall("formatted-text/run")
        backdrop = [f.get("value") for f in z.iter("format") if f.get("attr") == "background-color"]
        for r in runs:
            if r.get("fontcolor", "").lower() in ("#ffffff", "#fefefe"):
                assert backdrop, f"white text with no backdrop: {r.text!r}"


def test_every_dashboard_sheet_fits_its_zone(regional_pbix, tmp_path):
    report = parse_pbix(regional_pbix)
    g = generate(report, analyze(report), tmp_path)
    root = ET.parse(g.twb_path).getroot()
    viewpoints = list(root.iter("viewpoint"))
    assert viewpoints
    assert all(v.find("zoom") is not None and v.find("zoom").get("type") == "entire-view"
               for v in viewpoints)
