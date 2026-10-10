"""FastAPI server: a thin JSON API over ``src.pipeline`` plus the static UI.

Endpoints
    GET  /                         the app
    GET  /api/health               versions, Power BI Desktop ports, Tableau presence
    GET  /api/inputs               .pbix files in input/ and input/uploads/
    POST /api/analyze              run the pipeline (upload or pick an input)
    GET  /api/runs                 previous runs (output/*/result.json)
    GET  /api/runs/{id}            one run in full
    DELETE /api/runs/{id}          remove that run's output folder
    GET  /api/runs/{id}/files/{p}  download an output file
    GET  /api/runs/{id}/bundle     everything for that run as a zip

Nothing here contains migration logic; it only validates paths and calls the
pipeline, so the CLI and the UI can never disagree.
"""
from __future__ import annotations

import argparse
import io
import json
import platform
import re
import shutil
import threading
import time
import webbrowser
import zipfile
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from src.parsers.model_reader import ModelUnavailable, discover_ports
from src.parsers.pbix_reader import PbixError
from src.pipeline import run_pipeline

ROOT = Path(__file__).resolve().parents[2]
INPUT_DIR = ROOT / "input"
UPLOAD_DIR = INPUT_DIR / "uploads"
OUTPUT_DIR = ROOT / "output"
STATIC_DIR = Path(__file__).parent / "static"
VERSION = "1.0.0"

app = FastAPI(title="BI Migration Assistant", version=VERSION, docs_url="/api/docs", redoc_url=None)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

_SAFE = re.compile(r"[^\w .\-()]")


def _safe_name(name: str) -> str:
    return _SAFE.sub("_", Path(name).name).strip() or "report"


def _under(base: Path, candidate: Path) -> Path:
    resolved = candidate.resolve()
    if base.resolve() not in resolved.parents and resolved != base.resolve():
        raise HTTPException(400, "path escapes its folder")
    return resolved


def _asset_token() -> str:
    """Changes whenever any static file changes, so browsers cannot serve a stale UI."""
    newest = max((f.stat().st_mtime_ns for f in STATIC_DIR.glob("*.*")), default=0)
    return f"{VERSION}-{newest % 100_000_000:08d}"


@app.get("/", response_class=HTMLResponse)
def index():
    """Serve the page with cache-busted asset URLs and no-cache on the HTML itself."""
    token = _asset_token()
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    html = re.sub(r'(/static/[\w.-]+\.(?:css|js))(?=")', rf"\1?v={token}", html)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache, must-revalidate"})


@app.get("/api/health")
def health():
    return {
        "ok": True, "version": VERSION, "python": platform.python_version(),
        "desktop_ports": discover_ports(),
        "tableau_installed": Path(r"C:\Program Files\Tableau").is_dir(),
        "input_dir": str(INPUT_DIR), "output_dir": str(OUTPUT_DIR),
    }


@app.get("/api/inputs")
def inputs():
    out = []
    for folder, uploaded in ((INPUT_DIR, False), (UPLOAD_DIR, True)):
        if not folder.is_dir():
            continue
        for f in sorted(folder.glob("*.pbix")):
            st = f.stat()
            out.append({"name": f.name, "rel": f.relative_to(INPUT_DIR).as_posix(), "size": st.st_size,
                        "modified": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                        "uploaded": uploaded})
    return out


@app.post("/api/analyze")
async def analyze(file: UploadFile | None = File(None), input_rel: str | None = Form(None),
                  model_mode: str = Form("none"), model_dir: str | None = Form(None),
                  model_port: int | None = Form(None), generate: bool = Form(True),
                  data_mode: str = Form("none"), snapshot_filters: str = Form("all")):
    if file is not None and file.filename:
        name = _safe_name(file.filename)
        if not name.lower().endswith(".pbix"):
            raise HTTPException(400, "only .pbix files are accepted")
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        pbix = UPLOAD_DIR / name
        pbix.write_bytes(await file.read())
    elif input_rel:
        pbix = _under(INPUT_DIR, INPUT_DIR / input_rel)
        if not pbix.is_file():
            raise HTTPException(404, f"input not found: {input_rel}")
    else:
        raise HTTPException(400, "upload a .pbix or choose one from the input folder")

    if model_mode not in ("none", "live", "dir"):
        raise HTTPException(400, "model_mode must be none, live or dir")
    if model_mode == "dir" and not (model_dir and Path(model_dir).is_dir()):
        raise HTTPException(400, "model_dir must be an existing folder of TMSCHEMA_*.json files")
    if data_mode not in ("none", "live"):
        raise HTTPException(400, "data_mode must be none or live")
    if snapshot_filters not in ("all", "visual", "none"):
        raise HTTPException(400, "snapshot_filters must be all, visual or none")

    out_dir = OUTPUT_DIR / _safe_name(pbix.stem)
    try:
        result = await run_in_threadpool(
            run_pipeline, pbix, out_dir, "live" if model_mode == "live" else "none",
            model_port, model_dir if model_mode == "dir" else None, generate, data_mode, snapshot_filters)
    except PbixError as e:
        raise HTTPException(400, str(e))
    except ModelUnavailable as e:
        raise HTTPException(409, f"model unavailable: {e}")
    return result


def _run_dirs():
    if not OUTPUT_DIR.is_dir():
        return []
    dirs = [d for d in OUTPUT_DIR.iterdir() if d.is_dir() and (d / "result.json").is_file()]
    return sorted(dirs, key=lambda d: (d / "result.json").stat().st_mtime, reverse=True)


@app.get("/api/runs")
def runs():
    out = []
    for d in _run_dirs():
        try:
            r = json.loads((d / "result.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({
            "run_id": d.name, "report": r.get("report"), "pages": r.get("pages"),
            "containers": r.get("containers"), "data_visuals": r.get("data_visuals"),
            "verdicts": r.get("verdicts"), "migratable_pct": r.get("migratable_pct"),
            "model_available": (r.get("model") or {}).get("available", False),
            "has_workbook": bool(r.get("workbook")),
            "modified": datetime.fromtimestamp((d / "result.json").stat().st_mtime).isoformat(timespec="seconds"),
        })
    return out


def _run_dir(run_id: str) -> Path:
    d = _under(OUTPUT_DIR, OUTPUT_DIR / run_id)
    if not (d / "result.json").is_file():
        raise HTTPException(404, f"no such run: {run_id}")
    return d


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str):
    return json.loads((_run_dir(run_id) / "result.json").read_text(encoding="utf-8"))


@app.get("/api/runs/{run_id}/files/{path:path}")
def run_file(run_id: str, path: str):
    d = _run_dir(run_id)
    f = _under(d, d / path)
    if not f.is_file():
        raise HTTPException(404, f"no such file: {path}")
    return FileResponse(f, filename=f.name)


@app.delete("/api/runs/{run_id}")
def delete_run(run_id: str):
    """Remove one run's output folder. Only ever deletes inside output/."""
    d = _run_dir(run_id)
    shutil.rmtree(d)
    return {"deleted": run_id}


@app.get("/api/runs/{run_id}/bundle")
def run_bundle(run_id: str):
    d = _run_dir(run_id)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(d.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(d).as_posix())
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{d.name} - tableau migration.zip"'})


# --- entry point ---------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    import uvicorn

    ap = argparse.ArgumentParser(prog="python -m src.web", description="Run the BI Migration Assistant web UI.")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--open", dest="open_browser", action="store_true", default=True)
    g.add_argument("--no-open", dest="open_browser", action="store_false")
    args = ap.parse_args(argv)

    url = f"http://{args.host}:{args.port}"
    if args.open_browser:
        def _open():
            time.sleep(1.2)
            webbrowser.open(url)
        threading.Thread(target=_open, daemon=True).start()
    print(f"BI Migration Assistant -> {url}   (Ctrl+C to stop)")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0
