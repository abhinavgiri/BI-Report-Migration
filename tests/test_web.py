"""Milestone 11: the web API is a faithful, path-safe front to the pipeline."""
import io
import shutil
import zipfile

import pytest
from fastapi.testclient import TestClient

from src.web import app as webapp

client = TestClient(webapp.app)


def test_index_serves_the_app():
    r = client.get("/")
    assert r.status_code == 200 and "BI Migration Assistant" in r.text
    assert client.get("/static/app.js").status_code == 200


def test_health_reports_environment():
    h = client.get("/api/health").json()
    assert h["ok"] and "python" in h and isinstance(h["desktop_ports"], list)


def test_inputs_list_the_samples(retail_pbix, regional_pbix):
    names = {i["name"] for i in client.get("/api/inputs").json()}
    assert {retail_pbix.name, regional_pbix.name} <= names


def test_analyze_sample_then_download(retail_pbix):
    r = client.post("/api/analyze", data={"input_rel": retail_pbix.name, "model_mode": "none", "generate": "true"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["data_visuals"] == 16 and j["pages"] == 4 and j["migratable_pct"] == 100.0
    assert j["workbook"]["worksheets"] and len(j["pages_detail"]) == 4
    first_data = next(v for p in j["pages_detail"] for v in p["visuals"] if v["data_bound"])
    assert first_data["verdict"] in ("direct", "approximate") and first_data["roles"]
    # container ids repeat across pages, so each visual must map to a sheet named after ITS page
    for p in j["pages_detail"]:
        for v in p["visuals"]:
            if v["sheet"]:
                assert v["sheet"].startswith(p["name"] + " - "), (p["name"], v["sheet"])

    run_id = j["run_id"]
    assert any(x["run_id"] == run_id for x in client.get("/api/runs").json())
    assert client.get(f"/api/runs/{run_id}").json()["report"] == j["report"]

    twb = client.get(f"/api/runs/{run_id}/files/{j['workbook']['twb']}")
    assert twb.status_code == 200 and twb.content.startswith(b"<?xml")
    bundle = client.get(f"/api/runs/{run_id}/bundle")
    assert bundle.status_code == 200
    with zipfile.ZipFile(io.BytesIO(bundle.content)) as z:
        assert "assessment.md" in z.namelist() and "result.json" in z.namelist()


def test_analyze_with_offline_model_translates(regional_pbix):
    r = client.post("/api/analyze", data={"input_rel": regional_pbix.name, "model_mode": "dir",
                                          "model_dir": "tests/fixtures/dmv", "generate": "false"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["model"]["available"] and j["model"]["measures"] == 15
    assert 0 < j["model"]["measures_translated"] < 15
    assert j["workbook"] is None
    won = next(m for m in j["model"]["items"] if m["name"] == "Revenue Won")
    assert won["tableau"] is None and "CALCULATE" in won["error"]


def test_model_from_another_report_is_refused(retail_pbix):
    """The DMV fixture is Regional's model; attaching it to Retail must be refused, not silently used."""
    r = client.post("/api/analyze", data={"input_rel": retail_pbix.name, "model_mode": "dir",
                                          "model_dir": "tests/fixtures/dmv", "generate": "false"})
    assert r.status_code == 409 and "does not match" in r.json()["detail"]


def test_upload_roundtrip(retail_pbix):
    name = "zz_upload_test.pbix"
    try:
        r = client.post("/api/analyze", files={"file": (name, retail_pbix.read_bytes(), "application/octet-stream")},
                        data={"model_mode": "none", "generate": "false"})
        assert r.status_code == 200, r.text
        assert r.json()["data_visuals"] == 16
        assert (webapp.UPLOAD_DIR / name).is_file()
    finally:
        (webapp.UPLOAD_DIR / name).unlink(missing_ok=True)
        shutil.rmtree(webapp.OUTPUT_DIR / "zz_upload_test", ignore_errors=True)


@pytest.mark.parametrize("payload, status", [
    ({"model_mode": "none"}, 400),                                   # nothing chosen
    ({"input_rel": "..\\..\\etc\\passwd", "model_mode": "none"}, 400),  # path escape
    ({"input_rel": "missing.pbix", "model_mode": "none"}, 404),
    ({"input_rel": "Retail Analysis Sample PBIX.pbix", "model_mode": "dir", "model_dir": "nope"}, 400),
])
def test_bad_requests(payload, status):
    assert client.post("/api/analyze", data=payload).status_code == status


def test_non_pbix_upload_rejected():
    r = client.post("/api/analyze", files={"file": ("notes.txt", b"hello")}, data={"model_mode": "none"})
    assert r.status_code == 400


def test_live_model_empty_is_409(retail_pbix, monkeypatch):
    """Desktop open with no report loaded answers the DMVs with nothing; that must not pass as success."""
    from src.models.ir import DataModel
    monkeypatch.setattr("src.pipeline.read_model", lambda port=None, **kw: DataModel(source="live"))
    r = client.post("/api/analyze", data={"input_rel": retail_pbix.name, "model_mode": "live"})
    assert r.status_code == 409 and "empty" in r.json()["detail"]


def test_live_model_unavailable_is_409(retail_pbix, monkeypatch):
    from src.parsers.model_reader import ModelUnavailable
    monkeypatch.setattr("src.pipeline.read_model", lambda port=None, **kw: (_ for _ in ()).throw(ModelUnavailable("closed")))
    r = client.post("/api/analyze", data={"input_rel": retail_pbix.name, "model_mode": "live"})
    assert r.status_code == 409


def test_unknown_run_and_file_are_404():
    assert client.get("/api/runs/does-not-exist").status_code == 404
    run = client.get("/api/runs").json()[0]["run_id"]
    assert client.get(f"/api/runs/{run}/files/nope.txt").status_code == 404
    assert client.get(f"/api/runs/{run}/files/..%2F..%2Fpytest.ini").status_code in (400, 404)


def test_delete_run_removes_the_folder(retail_pbix, tmp_path, monkeypatch):
    # Point the app at a throwaway folder first. Without this the run id is the
    # report's own name, so the test would overwrite - and then DELETE - the real
    # output/ folder a user had just generated.
    monkeypatch.setattr(webapp, "OUTPUT_DIR", tmp_path)
    r = client.post("/api/analyze", data={"input_rel": retail_pbix.name, "model_mode": "none", "generate": "false"})
    run_id = r.json()["run_id"]
    assert (webapp.OUTPUT_DIR / run_id).is_dir()

    assert client.delete(f"/api/runs/{run_id}").json() == {"deleted": run_id}
    assert not (webapp.OUTPUT_DIR / run_id).exists()
    assert client.get(f"/api/runs/{run_id}").status_code == 404
    assert run_id not in {x["run_id"] for x in client.get("/api/runs").json()}


def test_delete_cannot_escape_the_output_folder():
    assert client.delete("/api/runs/..%2F..%2Fsrc").status_code in (400, 404)
    assert client.delete("/api/runs/nope").status_code == 404
    assert (webapp.ROOT / "src").is_dir()          # nothing outside output/ was touched


def test_index_cache_busts_static_assets():
    """Stale UI was a real bug: asset URLs must change whenever a static file changes."""
    import re as _re
    html = client.get("/").text
    assert "no-cache" in client.get("/").headers.get("cache-control", "")
    refs = _re.findall(r'/static/[\w.-]+\.(?:css|js)\?v=([\w.-]+)', html)
    assert len(refs) >= 2 and len(set(refs)) == 1, refs      # every asset, one token
    before = refs[0]

    js = webapp.STATIC_DIR / "app.js"
    original = js.read_bytes()
    try:
        js.write_bytes(original + b"\n// touch\n")
        after = _re.findall(r'/static/app\.js\?v=([\w.-]+)', client.get("/").text)[0]
        assert after != before, "editing a static file must change the token"
    finally:
        js.write_bytes(original)


def test_static_files_are_still_served_with_the_query_string():
    token = webapp._asset_token()
    assert client.get(f"/static/app.js?v={token}").status_code == 200
    assert client.get(f"/static/styles.css?v={token}").status_code == 200
