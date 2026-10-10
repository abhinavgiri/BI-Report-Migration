"""Read the semantic model (tables, columns, measures, relationships) and run queries.

The model inside a .pbix is an Analysis Services backup blob that cannot be
parsed safely. But while Power BI Desktop has a file open it runs a local
Analysis Services instance, and that instance answers the standard TMSCHEMA
DMV queries with complete metadata, including full DAX - and evaluates DAX
queries, which is how data snapshots are taken.

Layers, kept apart so the pure ones are testable without Desktop running:

- ``parse_dmv(rows)``: DMV result rows (plain dicts) -> ``DataModel``. Pure.
- ``run_queries(port, queries)``: any DMV/DAX queries -> rows, through a
  PowerShell helper using the ADOMD client that ships inside Power BI Desktop.
- ``fetch_dmv`` / ``read_model`` / ``find_port_for``: convenience on top.

Everything is opt-in: if Desktop is closed, ``read_model`` raises
``ModelUnavailable`` and callers carry on with an empty ``DataModel``.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from src.models.ir import Column, DataModel, Measure, Relationship

DMVS = ("TMSCHEMA_TABLES", "TMSCHEMA_COLUMNS", "TMSCHEMA_MEASURES", "TMSCHEMA_RELATIONSHIPS")

# TOM DataType enum (Microsoft.AnalysisServices.Tabular.DataType).
DATA_TYPES = {1: "automatic", 2: "string", 6: "int64", 8: "double", 9: "datetime",
              10: "decimal", 11: "boolean", 17: "binary", 19: "unknown", 20: "variant"}

# TMSCHEMA_COLUMNS.Type: 1 data, 2 calculated, 3 row-number, 4 calculated-table column.
COLUMN_DATA, COLUMN_CALCULATED, COLUMN_ROWNUMBER, COLUMN_CALCTABLE = 1, 2, 3, 4


class ModelUnavailable(Exception):
    """Power BI Desktop is not running with a model we can reach."""


def parse_dmv(rows: dict[str, list[dict]], source: str = "dmv") -> DataModel:
    """Build a DataModel from raw DMV rows keyed by DMV name."""
    tables = {t["ID"]: t["Name"] for t in rows.get("TMSCHEMA_TABLES", [])}
    model = DataModel(tables=sorted(tables.values()), source=source)

    col_name: dict[int, str] = {}
    for c in rows.get("TMSCHEMA_COLUMNS", []):
        name = c.get("ExplicitName") or c.get("InferredName") or ""
        col_name[c["ID"]] = name
        if c.get("Type") == COLUMN_ROWNUMBER:
            continue
        model.columns.append(Column(
            table=tables.get(c["TableID"], "?"), name=name,
            data_type=DATA_TYPES.get(c.get("DataType"), str(c.get("DataType"))),
            calculated_expression=(c.get("Expression") or None) if c.get("Type") == COLUMN_CALCULATED else None,
            hidden=bool(c.get("IsHidden")),
        ))

    for m in rows.get("TMSCHEMA_MEASURES", []):
        model.measures.append(Measure(
            table=tables.get(m["TableID"], "?"), name=m["Name"],
            expression=(m.get("Expression") or "").strip(),
            format_string=m.get("FormatString") or None, hidden=bool(m.get("IsHidden")),
        ))

    for r in rows.get("TMSCHEMA_RELATIONSHIPS", []):
        model.relationships.append(Relationship(
            from_table=tables.get(r["FromTableID"], "?"), from_column=col_name.get(r["FromColumnID"], "?"),
            to_table=tables.get(r["ToTableID"], "?"), to_column=col_name.get(r["ToColumnID"], "?"),
            active=bool(r.get("IsActive", True)),
            many_to_one=(r.get("FromCardinality") == 2 and r.get("ToCardinality") == 1),
        ))
    model.columns.sort(key=lambda c: (c.table, c.name))
    model.measures.sort(key=lambda m: (m.table, m.name))
    return model


# --- live instance ----------------------------------------------------------

def workspace_root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Power BI Desktop" / "AnalysisServicesWorkspaces"


def discover_ports(root: Path | None = None) -> list[int]:
    """Ports of running Desktop instances, from each workspace's msmdsrv.port.txt (UTF-16LE)."""
    root = root or workspace_root()
    ports = []
    if not root.is_dir():
        return ports
    for port_file in root.glob("*/Data/msmdsrv.port.txt"):
        try:
            text = port_file.read_bytes().decode("utf-16-le").strip("\ufeff \r\n\x00")
            ports.append(int(text))
        except (ValueError, UnicodeDecodeError):
            continue
    return sorted(ports)


PBI_BIN = Path(r"C:\Program Files\Microsoft Power BI Desktop\bin")

# PowerShell helper: loads the ADOMD client shipped with Desktop (its dependencies
# must be resolved from the same folder), runs every query in the JSON file
# {name: text} and writes <name>.json (rows) or <name>.error.txt per query.
_PS_SCRIPT = r'''
param([int]$Port, [string]$OutDir, [string]$Bin, [string]$QueriesFile)
$script:tried = @{}
[System.AppDomain]::CurrentDomain.add_AssemblyResolve([System.ResolveEventHandler]{
  param($sender,$e); $s=($e.Name -split ',')[0]
  if ($script:tried.ContainsKey($s)) { return $null }; $script:tried[$s]=$true
  $c = Join-Path $Bin ($s + ".dll"); if (Test-Path $c) { return [System.Reflection.Assembly]::LoadFrom($c) }; return $null
})
$asm = [System.Reflection.Assembly]::LoadFrom((Join-Path $Bin "Microsoft.PowerBI.AdomdClient.dll"))
$t = $asm.GetType("Microsoft.AnalysisServices.AdomdClient.AdomdConnection")
$conn = [System.Activator]::CreateInstance($t, @("Data Source=localhost:$Port"))
$conn.Open()
$utf8 = New-Object System.Text.UTF8Encoding $false
$queries = Get-Content -Raw -Encoding UTF8 $QueriesFile | ConvertFrom-Json
foreach ($p in $queries.PSObject.Properties) {
  $name = $p.Name
  try {
    $cmd = $conn.CreateCommand(); $cmd.CommandText = $p.Value
    $rd = $cmd.ExecuteReader(); $rows = New-Object System.Collections.ArrayList
    $n = $rd.FieldCount; $names = @(); for ($i=0;$i -lt $n;$i++){ $names += $rd.GetName($i) }
    while ($rd.Read()) {
      $o = [ordered]@{}
      for ($i=0;$i -lt $n;$i++){
        $v = $rd.GetValue($i)
        if ($v -is [System.DBNull]) { $v = $null }
        elseif ($v -is [DateTime]) { $v = $v.ToString("yyyy-MM-dd HH:mm:ss") }
        $o[$names[$i]] = $v
      }
      [void]$rows.Add([PSCustomObject]$o)
    }
    $rd.Close()
    $json = if ($rows.Count -eq 0) { "[]" } elseif ($rows.Count -eq 1) { "[" + ($rows[0] | ConvertTo-Json -Depth 4 -Compress) + "]" } else { $rows | ConvertTo-Json -Depth 4 -Compress }
    [System.IO.File]::WriteAllText((Join-Path $OutDir ($name + ".json")), $json, $utf8)
  } catch {
    [System.IO.File]::WriteAllText((Join-Path $OutDir ($name + ".error.txt")), $_.Exception.Message, $utf8)
  }
}
$conn.Close()
'''


def run_queries(port: int, queries: dict[str, str], bin_dir: Path = PBI_BIN,
                timeout: int = 300) -> tuple[dict[str, list[dict]], dict[str, str]]:
    """Run DMV / DAX queries against the instance on ``port``.

    Returns ``(rows_by_name, error_by_name)``: a query that the engine rejects
    lands in the second dict with the engine's message; the rest still succeed.
    """
    if not (bin_dir / "Microsoft.PowerBI.AdomdClient.dll").is_file():
        raise ModelUnavailable(f"ADOMD client not found under {bin_dir}")
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "query.ps1"
        script.write_text(_PS_SCRIPT, encoding="utf-8")
        qfile = Path(tmp) / "queries.json"
        qfile.write_text(json.dumps(queries), encoding="utf-8")
        cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
               "-File", str(script), "-Port", str(port), "-OutDir", tmp, "-Bin", str(bin_dir),
               "-QueriesFile", str(qfile)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise ModelUnavailable(f"queries on port {port} timed out") from e
        if proc.returncode != 0:
            raise ModelUnavailable(f"queries on port {port} failed: {proc.stderr.strip()[:400]}")
        rows, errors = {}, {}
        for name in queries:
            f = Path(tmp) / f"{name}.json"
            e = Path(tmp) / f"{name}.error.txt"
            if f.is_file():
                data = json.loads(f.read_text(encoding="utf-8"))
                rows[name] = data if isinstance(data, list) else [data]
            elif e.is_file():
                errors[name] = e.read_text(encoding="utf-8").strip()
            else:
                errors[name] = "no result returned (is Power BI Desktop still open?)"
        if not rows and errors and len(errors) == len(queries):
            first = next(iter(errors.values()))
            raise ModelUnavailable(f"every query failed on port {port}: {first[:300]}")
        return rows, errors


def fetch_dmv(port: int, bin_dir: Path = PBI_BIN, timeout: int = 120) -> dict[str, list[dict]]:
    rows, errors = run_queries(port, {d: f"SELECT * FROM $SYSTEM.{d}" for d in DMVS}, bin_dir, timeout)
    return {d: rows.get(d, []) for d in DMVS}


def read_model(port: int | None = None, expect_tables: list[str] | None = None) -> DataModel:
    """Read the model from a running Power BI Desktop instance.

    With several instances open, ``expect_tables`` (the tables the report uses)
    picks the one whose model matches; otherwise the last discovered port is used.
    """
    if port is None:
        port = find_port_for(expect_tables)
    return parse_dmv(fetch_dmv(port), source=f"Power BI Desktop live instance, localhost:{port}")


def find_port_for(expect_tables: list[str] | None = None) -> int:
    ports = discover_ports()
    if not ports:
        raise ModelUnavailable("no running Power BI Desktop instance found "
                               "(open the .pbix in Desktop and re-run)")
    if not expect_tables or len(ports) == 1:
        return ports[-1]
    wanted = set(expect_tables)
    seen = []
    for p in ports:
        try:
            names = {t["Name"] for t in run_queries(p, {"t": "SELECT * FROM $SYSTEM.TMSCHEMA_TABLES"})[0].get("t", [])}
        except ModelUnavailable:
            continue
        if wanted & names:
            return p
        seen.append((p, sorted(names)[:4]))
    windows = ", ".join(("a window with no report loaded" if not names else "a window with tables " + ", ".join(names))
                        for _, names in seen) or "none"
    raise ModelUnavailable("Power BI Desktop is open, but none of its windows has this report loaded "
                           f"(open: {windows}). Open this .pbix in Power BI Desktop, wait for it to load, then run again.")


def load_dmv_dir(path: str | Path) -> dict[str, list[dict]]:
    """Load DMV JSON files from a folder (used for fixtures and offline runs)."""
    rows = {}
    for d in DMVS:
        f = Path(path) / f"{d}.json"
        if f.is_file():
            data = json.loads(f.read_text(encoding="utf-8-sig"))
            rows[d] = data if isinstance(data, list) else [data]
    return rows
