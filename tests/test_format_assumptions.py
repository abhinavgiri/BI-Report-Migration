"""Milestone 0 verification.

Every claim in docs/FORMAT_FINDINGS.md is asserted here. If a finding was wrong,
or Microsoft changes the format, these fail loudly instead of the parser
silently producing nonsense later.
"""
import json
import zipfile
from collections import Counter

import pytest


def read_layout_bytes(pbix_path):
    with zipfile.ZipFile(pbix_path) as z:
        return z.read("Report/Layout")


def read_layout(pbix_path):
    return json.loads(read_layout_bytes(pbix_path).decode("utf-16-le"))


# --- section 1: the container --------------------------------------------

def test_pbix_is_a_zip_container(retail_pbix, regional_pbix):
    for p in (retail_pbix, regional_pbix):
        assert zipfile.is_zipfile(p), p.name + " is not a ZIP"


def test_both_files_contain_layout_and_datamodel(retail_pbix, regional_pbix):
    for p in (retail_pbix, regional_pbix):
        with zipfile.ZipFile(p) as z:
            names = set(z.namelist())
        assert "Report/Layout" in names
        assert "DataModel" in names


def test_optional_parts_differ_between_vintages(retail_pbix, regional_pbix):
    """Justifies tolerant parsing: the two files do NOT carry the same parts.

    History: the Retail file originally carried a DataMashup part; after being
    re-saved by Power BI Desktop 2.149 on 2026-09-12 it no longer does. Only the
    parts common to every vintage are asserted as present.
    """
    with zipfile.ZipFile(retail_pbix) as z:
        retail = set(z.namelist())
    with zipfile.ZipFile(regional_pbix) as z:
        regional = set(z.namelist())
    assert retail ^ regional, "expected the two vintages to carry different part sets"
    assert "Connections" in regional and "Connections" not in retail
    assert {"Report/Layout", "DataModel", "Version"} <= retail & regional


# --- section 2: Report/Layout encoding -----------------------------------

def test_layout_is_utf16le_json_without_bom(retail_pbix, regional_pbix):
    for p in (retail_pbix, regional_pbix):
        raw = read_layout_bytes(p)
        assert raw[:2] == b"{\x00", p.name + " did not start with UTF-16LE '{'"
        doc = json.loads(raw.decode("utf-16-le"))
        assert isinstance(doc, dict)
        assert "sections" in doc


def test_layout_is_not_utf8(retail_pbix):
    """Guards against a future 'just use utf-8' regression."""
    raw = read_layout_bytes(retail_pbix)
    with pytest.raises((UnicodeDecodeError, json.JSONDecodeError)):
        json.loads(raw.decode("utf-8"))


def test_top_level_keys_are_not_guaranteed(retail_pbix, regional_pbix):
    """'id' / 'reportId' exist in one file but not the other -> must use .get()."""
    retail = read_layout(retail_pbix)
    regional = read_layout(regional_pbix)
    assert "reportId" in regional and "reportId" not in retail
    assert "id" in regional and "id" not in retail


def all_containers(doc):
    for sec in doc["sections"]:
        for vc in sec.get("visualContainers", []):
            yield sec, vc


def test_config_and_query_are_json_encoded_strings(retail_pbix):
    """Nested JSON-in-string: these fields need a SECOND json.loads.

    Note section[0] of Retail is an 'Info' cover page with no queries at all,
    so this must scan every section rather than just the first.
    """
    doc = read_layout(retail_pbix)
    assert isinstance(doc["sections"][0]["config"], str)

    with_query = [vc for _, vc in all_containers(doc) if "query" in vc]
    assert with_query, "expected at least one container carrying a query"
    for field in ("config", "query"):
        value = with_query[0][field]
        assert isinstance(value, str), field + " should be a JSON string"
        assert isinstance(json.loads(value), dict)


def test_position_is_canonical_in_config_layouts(retail_pbix, regional_pbix):
    """config.layouts[0].position exists on EVERY container in both files.

    Top-level x/y is optional: the original Retail file had none at all, while
    the copy re-saved by Desktop 2.149 (and Regional) carry a rounded mirror of
    the nested value. The parser must therefore prefer the nested value.
    """
    for pbix in (retail_pbix, regional_pbix):
        doc = read_layout(pbix)
        for _, vc in all_containers(doc):
            position = json.loads(vc["config"])["layouts"][0]["position"]
            assert {"x", "y", "width", "height"} <= set(position)
            if "x" in vc:                       # when mirrored, it must agree
                assert abs(position["x"] - vc["x"]) < 0.01


def test_query_and_projections_do_not_imply_each_other(regional_pbix):
    """Both mismatch directions exist -> the parser must treat query as optional."""
    doc = read_layout(regional_pbix)
    bound_without_query = []
    query_without_bound = []
    for _, vc in all_containers(doc):
        single = json.loads(vc["config"]).get("singleVisual") or {}
        bound = bool(single.get("projections"))
        has_query = "query" in vc
        if bound and not has_query:
            bound_without_query.append(single.get("visualType"))
        if has_query and not bound:
            query_without_bound.append(single.get("visualType"))

    assert len(bound_without_query) == 7
    assert set(bound_without_query) == {
        "lineStackedColumnComboChart",
        "pivotTable",
        "keyDriversVisual",
        "qnaVisual",
    }
    assert query_without_bound == ["textbox", "textbox", "textbox"]


# --- section 2: measured inventory ---------------------------------------

def inventory(pbix):
    """Returns (doc, visualType counts, data-bound count, group count, total)."""
    doc = read_layout(pbix)
    types = Counter()
    bound = 0
    groups = 0
    total = 0
    for sec in doc["sections"]:
        for vc in sec.get("visualContainers", []):
            total += 1
            single = json.loads(vc["config"]).get("singleVisual")
            if single is None:
                groups += 1
                continue
            types[single.get("visualType")] += 1
            if single.get("projections"):
                bound += 1
    return doc, types, bound, groups, total


def test_retail_inventory_matches_findings(retail_pbix):
    doc, types, bound, groups, total = inventory(retail_pbix)
    assert total == 24
    assert bound == 16
    assert groups == 0
    assert types["clusteredColumnChart"] == 4
    assert types["textbox"] == 7


def test_regional_inventory_matches_findings(regional_pbix):
    doc, types, bound, groups, total = inventory(regional_pbix)
    assert len(doc["sections"]) == 11
    assert total == 937
    assert bound == 55
    assert groups == 45, "containers without singleVisual are visual groups"
    assert types["actionButton"] == 732, "most containers are navigation chrome"


def test_decorative_visuals_have_no_projections(regional_pbix):
    """textbox / shape / actionButton are real containers but carry no data."""
    doc = read_layout(regional_pbix)
    seen = 0
    for sec in doc["sections"]:
        for vc in sec.get("visualContainers", []):
            single = json.loads(vc["config"]).get("singleVisual") or {}
            if single.get("visualType") in ("textbox", "shape", "actionButton"):
                assert not single.get("projections")
                seen += 1
    assert seen > 700


# --- section 2: semantic query shape -------------------------------------

def test_semantic_query_exposes_column_vs_measure(retail_pbix):
    """The Column / Measure / Aggregation distinction is what drives translation."""
    doc = read_layout(retail_pbix)
    kinds = set()
    for sec in doc["sections"]:
        for vc in sec.get("visualContainers", []):
            if "query" not in vc:
                continue
            for cmd in json.loads(vc["query"]).get("Commands", []):
                shape = cmd.get("SemanticQueryDataShapeCommand")
                if not shape:
                    continue
                query = shape["Query"]
                assert "From" in query and "Select" in query
                for sel in query["Select"]:
                    kinds |= {k for k in sel if k != "Name"}
    assert "Column" in kinds
    assert "Measure" in kinds


# --- section 3: DataModel is opaque --------------------------------------

def test_datamodel_is_an_abf_backup_not_tmdl(retail_pbix, regional_pbix):
    for p in (retail_pbix, regional_pbix):
        with zipfile.ZipFile(p) as z:
            head = z.read("DataModel")[:200]
        text = head.decode("utf-16-le", "replace")
        assert "STREAM_STORAGE_SIGNATURE" in text or "backup" in text.lower(), (
            p.name + ": unexpected DataModel header -> " + repr(text[:80])
        )


def test_measure_dax_is_not_recoverable_from_datamodel(retail_pbix):
    """Documents WHY we require a TMDL export instead of scraping the blob.

    'Total Sales Variance' is referenced by name in Report/Layout, so the measure
    certainly exists in the model -- yet it cannot be located in the DataModel
    bytes in either plausible encoding, because the stream is compressed.
    """
    layout_text = read_layout_bytes(retail_pbix).decode("utf-16-le")
    assert "Total Sales Variance" in layout_text, "measure should appear in layout"

    with zipfile.ZipFile(retail_pbix) as z:
        blob = z.read("DataModel")

    assert blob.find(b"Total Sales Variance") == -1
    assert blob.find("Total Sales Variance".encode("utf-16-le")) == -1
