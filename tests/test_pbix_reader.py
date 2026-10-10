"""Milestone 1: PbixReader."""
import json
import zipfile

import pytest

from src.parsers.pbix_reader import (
    LAYOUT_PART,
    PbixError,
    PbixReader,
    decode_text_part,
    load_json_string,
)


def test_opens_both_real_files(retail_pbix, regional_pbix):
    assert len(PbixReader(retail_pbix).layout()["sections"]) == 4
    assert len(PbixReader(regional_pbix).layout()["sections"]) == 11


def test_layout_is_cached(retail_pbix):
    r = PbixReader(retail_pbix)
    assert r.layout() is r.layout()


def test_parts_differ_between_files(retail_pbix, regional_pbix):
    retail, regional = PbixReader(retail_pbix), PbixReader(regional_pbix)
    assert retail.has_data_model() and regional.has_data_model()
    assert regional.has_part("Connections") and not retail.has_part("Connections")
    assert retail.parts != regional.parts


def test_version_is_read_as_text(regional_pbix):
    assert PbixReader(regional_pbix).version() == "1.25"


def test_missing_file_raises(tmp_path):
    with pytest.raises(PbixError, match="not found"):
        PbixReader(tmp_path / "nope.pbix")


def test_non_zip_raises(tmp_path):
    bad = tmp_path / "bad.pbix"
    bad.write_bytes(b"this is not a zip")
    with pytest.raises(PbixError, match="not a ZIP"):
        PbixReader(bad)


def test_zip_without_layout_raises(tmp_path):
    bad = tmp_path / "empty.pbix"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("Version", "1.0")
    with pytest.raises(PbixError, match=LAYOUT_PART):
        PbixReader(bad)


def test_read_unknown_part_raises(retail_pbix):
    with pytest.raises(PbixError, match="no part named"):
        PbixReader(retail_pbix).read_bytes("does/not/exist")


def test_decode_detects_utf16le_without_bom():
    raw = '{"a": 1}'.encode("utf-16-le")
    assert json.loads(decode_text_part(raw)) == {"a": 1}


def test_decode_handles_bom_and_utf8():
    assert decode_text_part("é".encode("utf-16")) == "é"      # BOM-prefixed
    assert decode_text_part("é".encode("utf-8")) == "é"


def test_load_json_string_round_trips_and_passes_through():
    assert load_json_string('{"x": [1]}') == {"x": [1]}
    assert load_json_string({"already": "parsed"}) == {"already": "parsed"}
    assert load_json_string(None) is None
    with pytest.raises(TypeError):
        load_json_string(42)
