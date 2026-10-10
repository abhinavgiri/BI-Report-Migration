# Cross-Platform BI Migration Assistant — Project Plan

**Direction:** Power BI (`.pbix`) → Tableau (`.twb`)
**Language:** Python 3.13, standard library at runtime.
**Principle:** every format claim is verified against real files — the two `.pbix`
samples in `input/` and the `Superstore.twb` shipped with Tableau 2026.2.
See [docs/FORMAT_FINDINGS.md](docs/FORMAT_FINDINGS.md).

## Architecture

```
 .pbix ──► parsers ──► IR (models) ──► analyzers ──► assessment.md
                         │                    │
 Power BI Desktop ───────┘  (live DMV)        └──► translators ──► generators ──► .twb + data/

   src/pipeline.run_pipeline() wraps the above; src/cli and src/web (FastAPI) both call it
```

The **IR** is the point of the project. Parsers know only Power BI; generators know
only Tableau; neither imports the other. Adding Looker later = one new generator.

## Milestones — all complete

| # | Milestone | Status | Verification |
|---|---|---|---|
| 0 | Scaffold, format discovery | ✅ | 15 assumption tests; two wrong assumptions caught and corrected |
| 1 | `PbixReader` | ✅ | 11 tests; both files, both vintages, error paths |
| 2 | IR dataclasses | ✅ | 6 tests incl. a no-vendor-vocabulary guard |
| 3 | Layout → IR parser | ✅ | 24 tests; Retail 24/16, Regional 937/55/45, every filter predicate mapped |
| 4 | Compatibility analyzer + report | ✅ | 7 tests; Retail 16/16 migratable, Regional 6 unsupported named |
| 5 | Tableau `.twb` generator | ✅ | 11 tests **and both workbooks open in Tableau Desktop 2026.2 (`rk=ok`)** |
| 6 | CLI end-to-end | ✅ | 4 tests; one command → assessment.md, summary.json, .twb, csv |
| 7 | Regional Sales stress test | ✅ | 47 sheets / 9 dashboards generated, nothing dropped silently |
| 8 | Live model extraction (opt-in) | ✅ | 6 tests on real DMV fixtures; 15 measures with full DAX |
| 9 | DAX translator (named subset) | ✅ | 18 tests; Regional splits 4 translated / 11 refused with reasons |
| 10 | Two-audience codebase guide | ✅ | CODEBASE_GUIDE.xlsx + docs/CODEBASE_GUIDE.md from one source |
| 11 | Web UI + `run_app.bat` launcher | ✅ | 15 web tests; UI driven end-to-end in a browser; launcher verified from a clean copy |

**Total: 181 tests, all against the real files.**

## How Milestone 5 was verified

Screen access to Tableau was not granted, so verification used Tableau's own
log: a harness launches `tableau.exe <twb>`, watches the window title, and greps
`My Tableau Repository\Logs` for the error dialog or `load-workbook … rk=ok`.
The first workbook failed with a masked error. ~60 bisect variants — stripping
the shipped Superstore.twb *down* and grafting our XML *in* — isolated nine
loader requirements (findings §7). Both generated workbooks now load cleanly.

## Decisions

| ID | Decision | Why |
|---|---|---|
| D1 | Work from `.pbix` only | Report layer is in `Report/Layout`; model layer comes from Desktop's live AS instance; Microsoft says PBIX→PBIP cannot be automated |
| D2 | Never scrape DAX from the ABF blob | Compressed; fragments only; would emit confidently wrong calculations |
| D3 | Retail first, Regional as stress test | 24 vs 937 containers |
| D4 | Analyzer before generator | Assessment is useful even where generation cannot be |
| D5 | Translate only a named DAX subset; refuse loudly otherwise | Rule 8 — no plausible-but-wrong output |
| D6 | Validate `.twb` against Superstore.twb, then open in Tableau | Rule 5 — never invent the target format either |
| D7 | One flattened, header-only CSV datasource | Data is locked in the model; every shelf assignment is preserved; user swaps in a real extract |
| D8 | Pies emitted as bars, downgraded to *approximate* | Superstore has no pie worksheet to copy from |
| D9 | Model extraction is opt-in (`--model live` / `--model-dir`) | It needs Power BI Desktop open; the tool must degrade gracefully |
| D10 | Emit dashboard windows **with a viewpoint per sheet**, and hide every worksheet | Only the page tabs show, matching Power BI; an empty viewpoints block is what made earlier attempts fail |
| D11 | Verify Tableau loading via its log, not the screen | Screen control was declined; the log carries the pass/fail signal |
| D12 | Web app is a thin layer over one shared `run_pipeline()` | CLI and UI cannot disagree; the server only validates paths and maps errors |
| D13 | `run_app.bat` creates `.venv` and installs libraries keyed on a requirements hash | Collaborators have only Python; second launches are instant |
| D14 | Vanilla HTML/CSS/JS, no build step | Nothing beyond Python to install |
| D15 | Snapshots are per visual (its own `SUMMARIZECOLUMNS`), one datasource each | Exact numbers incl. non-additive measures; no join logic; visuals without a snapshot keep the stub |
| D17 | Ship a `.twbx` as the primary download | The bare `.twb` cannot find its CSVs; the package carries both |
| D18 | Hide every dashboard-placed worksheet | 35 sheet tabs buried the 4 page tabs; dashboard windows keep the file loadable |
| D20 | Declare each CSV's schema on its `<relation>` | A one-column snapshot has no comma, and Tableau splits the header on spaces |
| D19 | Snapshot values, not the IR, decide a column's Tableau type | Power BI cards hold formatted text; aggregating text is an error |
| D16 | Snapshots reproduce Power BI's filters by default; `none`/`visual` are explicit options | Faithfulness first; empty-in-Power-BI stays empty and is explained |
| D21 | Map the report's own format strings to Tableau `default-format` | A dashboard of raw floats does not read as a dashboard; the format is in `dataTransforms.selects[].format` |
| D22 | Emit `<computed-sort>` from `Query.OrderBy`, and only when both ends resolve | "Top 10 by revenue" sorted alphabetically is visibly wrong; an unresolved sort would break the sheet |
| D23 | Colour marks from the report's embedded theme, or not at all | The palette is real data; a palette we picked would be invention (rule 5) |
| D24 | Rebuild text boxes as dashboard text zones, with a backdrop behind light text | The page headings are the labels that make a dashboard readable; white-on-white would lose them |
| D25 | `<zoom type="entire-view"/>` on every dashboard viewpoint | Otherwise each sheet keeps its natural size and is clipped inside its zone |

## Known limits / future work

- Filters are parsed into the IR, listed in the assessment and applied to snapshots,
  but are not yet written into the `.twb` as Tableau filter objects.
- Slicers become a note ("dashboard filter"), not a quick-filter object.
- Maps are emitted as Circle marks with the location on Detail; geocoding is manual.
- The DAX subset excludes `CALCULATE`, iterators, `VAR`, time intelligence.
- PBIR (`definition/` folder) reader not yet written; the parser interface allows it.
- Per-visual colour overrides are not carried: the sample files set none
  (`dataPoint` objects absent), so only the report-level theme palette is available.
- A report naming a **built-in** theme ships no palette (the Retail sample names
  "Concourse"); those workbooks keep Tableau's default colours.
- Power BI's banner/background *shapes* are not carried; a light heading gets the
  theme's primary colour as a backdrop instead of the original shape.