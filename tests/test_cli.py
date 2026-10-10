"""Milestone 6: one command produces every artefact."""
import json
from pathlib import Path

from src.cli import run

FIXTURES = Path(__file__).parent / "fixtures" / "dmv"


def test_end_to_end_retail(retail_pbix, tmp_path, capsys):
    rc = run([str(retail_pbix), "--out", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "4 pages, 24 containers, 16 data visuals" in out
    assert (tmp_path / "assessment.md").is_file()
    assert (tmp_path / "summary.json").is_file()
    assert (tmp_path / "Retail Analysis Sample PBIX.twb").is_file()
    assert (tmp_path / "data" / "model_stub.csv").is_file()
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["verdicts"]["unsupported"] == 0
    assert summary["model"]["available"] is False
    assert len(summary["workbook"]["worksheets"]) == 14


def test_offline_model_dir_translates_measures(regional_pbix, tmp_path):
    rc = run([str(regional_pbix), "--out", str(tmp_path), "--model-dir", str(FIXTURES)])
    assert rc == 0
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["model"]["available"] and summary["model"]["measures"] == 15
    assert 0 < summary["model"]["measures_translated"] < 15
    measures = json.loads((tmp_path / "measures.json").read_text(encoding="utf-8"))
    won = next(m for m in measures if m["measure"] == "Revenue Won")
    assert won["tableau"] is None and "CALCULATE" in won["error"]


def test_bad_input_exit_code(tmp_path):
    bad = tmp_path / "x.pbix"
    bad.write_bytes(b"nope")
    assert run([str(bad), "--out", str(tmp_path / "o")]) == 1


def test_mismatched_model_exit_code(retail_pbix, tmp_path):
    # Regional's DMV export attached to Retail: refused with exit code 2, never silently used
    assert run([str(retail_pbix), "--out", str(tmp_path), "--model-dir", str(FIXTURES)]) == 2


def test_live_model_unavailable_exit_code(retail_pbix, tmp_path, monkeypatch):
    monkeypatch.setattr("src.pipeline.read_model", lambda port=None, **kw: (_ for _ in ()).throw(
        __import__("src.parsers.model_reader", fromlist=["ModelUnavailable"]).ModelUnavailable("closed")))
    assert run([str(retail_pbix), "--out", str(tmp_path), "--model", "live"]) == 2
