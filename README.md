# Cross-Platform BI Migration Assistant

Reads a Power BI report (`.pbix`), assesses how well each visual can move to
Tableau, and generates a Tableau workbook (`.twb`) with every migratable visual
already placed on the right shelves and every page laid out as a dashboard.

It is deliberately an **assistant**, not a magic converter: it tells you exactly
what it could do, what it approximated, and what a person still has to rebuild.

```
 .pbix ──► parsers ──► IR ──► analyzer ──► assessment.md
                         │                       │
 (Power BI Desktop) ─────┘        DAX subset ──► generator ──► <report>.twb + data/
```

## Quick start

**Easiest:** double-click **`run_app.bat`**. It finds Python, creates a private
environment, installs the libraries (first run only), starts the local web app
and opens your browser at http://127.0.0.1:8765. Nothing leaves your machine.

The web UI: drop one `.pbix` or many at once (they run one after another), or pick a sample, optionally read measures from a
running Power BI Desktop, run, and browse the verdict per visual, the DAX
translations, filters, skipped items and downloads. Four themes (Emerald, Sunset,
Ocean, Sakura), light/dark, animated backgrounds you can switch off.

From a terminal:

```bash
python -m venv .venv
.venv/Scripts/activate            # Windows
pip install -r requirements.txt
python -m pytest                  # 181 tests against the real sample files
python -m src.web                 # the web app
python -m src.cli "input/Retail Analysis Sample PBIX.pbix"   # or the CLI
```

Output lands in `output/<report name>/`:

| File | What it is |
|---|---|
| `assessment.md` | Human-readable migration report: verdict + reason per visual |
| `<report>.twbx` | **Packaged workbook - open this one**: it carries the data inside |
| `<report>.twb` | The same workbook without data; needs the `data/` folder beside it |
| `data/model_stub.csv` | Header-only CSV the workbook connects to; replace with a real extract |
| `summary.json` | The same numbers, machine-readable |
| `measures.json` | Each measure's DAX, its Tableau translation, or why it was refused (model runs only) |

### Seeing real charts: data snapshots

The workbook's tables are empty by default (the rows live inside the `.pbix`
model, which we never scrape). With the report **open in Power BI Desktop**, tick
**Include data snapshots** (or `--data live`): every visual's own query is
evaluated by Power BI's engine and shipped as a small CSV, so Tableau draws the
same numbers - measures included. Filters default to what Power BI applies;
`--snapshot-filters none` takes the raw grain instead.

### Getting the measures too

The DAX definitions are locked inside the `.pbix` in a compressed engine
backup. While the file is **open in Power BI Desktop**, its local Analysis
Services instance will answer for them:

```bash
python -m src.cli "input/Regional Sales Sample.pbix" --model live
```

Or from a saved DMV export folder (the tests ship one):

```bash
python -m src.cli "input/Regional Sales Sample.pbix" --model-dir tests/fixtures/dmv
```

## What it produced on the samples

| Report | Pages | Containers | Data visuals | Direct | Approx. | Manual | Unsupported | Sheets |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Retail Analysis | 4 | 24 | 16 | 9 | 7 | 0 | 0 | 14 |
| Regional Sales | 11 | 937 | 55 | 22 | 26 | 1 | 6 | 47 |

Regional Sales with the model attached: 15 measures read, **4 translated**, 11
refused with the reason named (`CALCULATE`, `AVERAGEX`, `VAR`, ...).

Both generated workbooks **open in Tableau Desktop 2026.2** (`load-workbook rk=ok`).
The loader requirements that took ~60 bisect runs to find are in
[docs/FORMAT_FINDINGS.md](docs/FORMAT_FINDINGS.md) section 7.

## Documents

- [PROJECT_PLAN.md](PROJECT_PLAN.md) - milestones, architecture, decisions
- [PROJECT_TRACKER.xlsx](PROJECT_TRACKER.xlsx) - the living tracker
- [docs/FORMAT_FINDINGS.md](docs/FORMAT_FINDINGS.md) - every format fact, with the evidence
- [docs/DEVELOPER_HANDBOOK.md](docs/DEVELOPER_HANDBOOK.md) - **start here if you are new**: the ideas, the
  tech stack, every function, a bug-to-file map, and an honest list of what could be better
- [docs/CODEBASE_GUIDE.md](docs/CODEBASE_GUIDE.md) / [CODEBASE_GUIDE.xlsx](CODEBASE_GUIDE.xlsx) - what each file does, for two audiences

## Principles

1. Never invent a file format - every construct was observed in a real `.pbix`
   or in the `Superstore.twb` that ships with Tableau.
2. Never hide a failure - untranslatable DAX becomes a labelled placeholder,
   unsupported visuals are listed, nothing is silently dropped.
3. The IR is the product - parsers know only Power BI, generators know only
   Tableau, and neither imports the other.
