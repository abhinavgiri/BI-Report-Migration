"""Milestone 8: DMV rows -> DataModel.

The fixture folder holds the real TMSCHEMA output captured from Power BI
Desktop's live instance with Regional Sales open (2026-09-12). No hand-written
model data.
"""
from pathlib import Path

import pytest

from src.parsers.model_reader import (
    ModelUnavailable, discover_ports, load_dmv_dir, parse_dmv, read_model,
)

FIXTURES = Path(__file__).parent / "fixtures" / "dmv"


@pytest.fixture(scope="module")
def model():
    return parse_dmv(load_dmv_dir(FIXTURES), source="fixture")


def test_tables(model):
    assert len(model.tables) == 10
    assert "Opportunities" in model.tables and "Opportunity Calendar" in model.tables
    assert model.available


def test_columns_exclude_row_numbers(model):
    assert len(model.columns) == 79 - 10
    calc = [c for c in model.columns if c.calculated_expression]
    assert len(calc) == 5
    weeks = next(c for c in calc if c.name == "Weeks Open")
    assert weeks.table == "Opportunities" and "DATEDIFF" in weeks.calculated_expression


def test_measures_have_full_dax(model):
    assert len(model.measures) == 15
    won = next(m for m in model.measures if m.name == "Revenue Won")
    assert won.table == "Opportunities"
    assert won.expression.startswith("CALCULATE(")
    assert 'Opportunities[Status] = "Won"' in won.expression
    assert won.format_string.startswith("\\$#,0.00")
    pipeline = next(m for m in model.measures if m.name == "Opportunity Count In Pipeline")
    assert "--" in pipeline.expression, "DAX comments must survive"


def test_relationships(model):
    assert len(model.relationships) == 8
    r = next(r for r in model.relationships if r.from_column == "CloseDate")
    assert (r.from_table, r.to_table, r.to_column) == ("Opportunities", "Opportunity Calendar", "Date")
    assert r.active and r.many_to_one


def test_discover_ports_reads_utf16(tmp_path):
    d = tmp_path / "AnalysisServicesWorkspace_x" / "Data"
    d.mkdir(parents=True)
    (d / "msmdsrv.port.txt").write_bytes("52961".encode("utf-16-le"))
    assert discover_ports(tmp_path) == [52961]
    assert discover_ports(tmp_path / "missing") == []


def test_read_model_without_desktop_raises_clearly(monkeypatch):
    monkeypatch.setattr("src.parsers.model_reader.discover_ports", lambda: [])
    with pytest.raises(ModelUnavailable, match="open the .pbix in Desktop"):
        read_model()


def test_wrong_report_open_explains_itself_in_plain_english(monkeypatch):
    """Selecting report A while Desktop has report B open must say so readably."""
    import src.parsers.model_reader as mr
    monkeypatch.setattr(mr, "discover_ports", lambda: [49379, 62338])
    monkeypatch.setattr(mr, "run_queries", lambda port, q, *a, **k: (
        {"t": [] if port == 49379 else [{"Name": "Accounts"}, {"Name": "Campaigns"}]}, {}))
    with pytest.raises(ModelUnavailable) as e:
        mr.find_port_for(["Sales", "Store"])
    msg = str(e.value)
    assert "none of its windows has this report loaded" in msg
    assert "a window with no report loaded" in msg and "Accounts" in msg
    assert "[(" not in msg, "no raw Python tuples in a user-facing message"


def test_matching_window_is_chosen_among_several(monkeypatch):
    import src.parsers.model_reader as mr
    monkeypatch.setattr(mr, "discover_ports", lambda: [1, 2])
    monkeypatch.setattr(mr, "run_queries", lambda port, q, *a, **k: (
        {"t": [{"Name": "Sales"}] if port == 2 else [{"Name": "Other"}]}, {}))
    assert mr.find_port_for(["Sales", "Store"]) == 2
