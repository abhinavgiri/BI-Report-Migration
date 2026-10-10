"""Milestone 12: the pipeline wires snapshots through to per-visual datasources.

The engine is replaced by a fake so the test is hermetic; the DAX text it
receives is asserted so the real engine would get the same queries.
"""
import csv
import xml.etree.ElementTree as ET

from src.models.ir import DataModel
from src.pipeline import run_pipeline


def _fake_engine(captured):
    def run_queries(port, queries, *a, **k):
        captured["port"] = port
        captured["queries"] = queries
        rows, errors = {}, {}
        for name, dax in queries.items():
            if "'District'[DM]" in dax:              # pretend the engine rejects one visual
                errors[name] = "The column 'District'[DM] is ambiguous"
                continue
            # one row per query: every column the plan asked for gets a value
            cols = _columns_of(dax)
            rows[name] = [{c: (3.5 if c.startswith("[") else "A") for c in cols},
                          {c: (1.5 if c.startswith("[") else "B") for c in cols}]
        return rows, errors
    return run_queries


def _columns_of(dax):
    """Header names the engine would return for a SUMMARIZECOLUMNS query."""
    body = dax[len("EVALUATE SUMMARIZECOLUMNS("):-1]
    out, depth, cur = [], 0, ""
    for ch in body:
        if ch in "({[":
            depth += 1
        elif ch in ")}]":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur.strip()); cur = ""
        else:
            cur += ch
    out.append(cur.strip())
    cols, i = [], 0
    while i < len(out):
        a = out[i]
        if a.startswith('"'):                        # "alias", expr
            cols.append("[" + a.strip('"') + "]"); i += 2
        elif a.startswith("'"):                      # 'Table'[Col]
            cols.append(a.replace("'", "", 2) if a.count("'") == 2 else a); i += 1
        else:
            i += 1                                   # filter argument
    return cols


def test_live_snapshots_become_per_visual_datasources(retail_pbix, tmp_path, monkeypatch):
    captured = {}
    tables = ["Sales", "Store", "District", "Time", "Item"]
    monkeypatch.setattr("src.pipeline.read_model",
                        lambda port=None, expect_tables=None: DataModel(tables=tables, source="Power BI Desktop live instance, localhost:4242"))
    monkeypatch.setattr("src.pipeline.run_queries", _fake_engine(captured))

    r = run_pipeline(retail_pbix, out_dir=tmp_path, data_mode="live")

    assert captured["port"] == 4242
    assert all(q.startswith("EVALUATE SUMMARIZECOLUMNS(") for q in captured["queries"].values())
    assert len(captured["queries"]) == 14                  # every worksheet-to-be
    rejected = sum(1 for q in captured["queries"].values() if "'District'[DM]" in q)
    good = 14 - rejected
    assert 0 < rejected < 14
    d = r["data"]
    assert d["mode"] == "live" and d["sheets"] == good
    assert d["rows"] == good * 2
    assert len(d["problems"]) == rejected and {p["kind"] for p in d["problems"]} == {"query"}
    assert all("ambiguous" in p["error"] for p in d["problems"])

    root = ET.parse(tmp_path / r["workbook"]["twb"]).getroot()
    ds_names = [d.get("name") for d in root.find("datasources")]
    assert ds_names[0] == "federated.bimigration" and len(ds_names) == 1 + good
    csvs = sorted((tmp_path / "data").glob("v*_*.csv"))
    assert len(csvs) == good
    with open(csvs[0], newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    assert len(rows) == 3 and rows[1] != rows[2]
    # the UI can show where each sheet's numbers came from
    with_rows = [v for p in r["pages_detail"] for v in p["visuals"] if v["snapshot_rows"]]
    assert len(with_rows) == good and all(v["snapshot_dax"] for v in with_rows)


def test_data_live_without_desktop_is_a_clear_error(retail_pbix, tmp_path, monkeypatch):
    from src.parsers.model_reader import ModelUnavailable
    monkeypatch.setattr("src.pipeline.read_model",
                        lambda port=None, expect_tables=None: (_ for _ in ()).throw(ModelUnavailable("closed")))
    try:
        run_pipeline(retail_pbix, out_dir=tmp_path, data_mode="live")
    except ModelUnavailable as e:
        assert "closed" in str(e)
    else:
        raise AssertionError("expected ModelUnavailable")
