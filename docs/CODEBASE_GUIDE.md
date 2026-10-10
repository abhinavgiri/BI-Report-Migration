# Codebase guide

Two explanations of every file: one for anyone, one for analysts and developers.
For the concepts, the tech stack and a bug-to-file map, read
[DEVELOPER_HANDBOOK.md](DEVELOPER_HANDBOOK.md) alongside this.
The same content lives in `CODEBASE_GUIDE.xlsx`.

## How it flows

| Step | In plain English | In code |
|---|---|---|
| 1. Open | Unzip the .pbix and read the report layout. | `PbixReader.layout()` |
| 2. Describe | Write every page, chart, field and filter in neutral terms. | `layout_parser.parse_pbix() -> ir.Report` |
| 3. (Optional) Ask the engine | If Power BI Desktop is open, fetch measures, columns, relationships with full DAX. | `model_reader.read_model() -> ir.DataModel` |
| 4. Assess | Give every chart a verdict and a reason. | `compatibility.analyze() -> ReportAssessment` |
| 5. Translate | Rewrite the simple formulas; refuse the rest loudly. | `dax_to_tableau.translate_measures()` |
| 6. Build | Emit the Tableau workbook, dashboards and the data stub. | `twb_writer.generate()` |
| 7. Report | Write assessment.md, summary.json, result.json, measures.json. | `pipeline.run_pipeline()` |
| 8. Show | Serve the same result in the local web app (or print it in the CLI). | `src.web.app / src.cli` |

## Part 1 - for anyone

**`src/parsers/pbix_reader.py`**  
Opens the Power BI file. A .pbix is secretly a ZIP folder, like a compressed archive. This file unzips it and pulls out the one part we need - the report layout - and turns it from computer bytes into readable text. It is the only place that touches the raw file.

**`src/parsers/layout_parser.py`**  
Reads the report layout and writes down, in our own words, what is on every page: which charts exist, where they sit, what data they show and which filters apply. It knows Power BI's vocabulary so nothing else has to.

**`src/parsers/model_reader.py`**  
Gets the 'brains' of the report - the business formulas (measures), the calculated columns and how tables link together. These are hidden in a locked box inside the file, but while Power BI Desktop has the file open it runs a little database we can ask politely. This file asks it.

**`src/models/ir.py`**  
The shared language. Instead of talking 'Power BI' or 'Tableau', every part of the tool describes a report in neutral terms: pages, visuals, fields, filters, measures. Think of it as the translator's notebook that both sides can read.

**`src/analyzers/compatibility.py`**  
The honest assessor. For every chart it says: 'Tableau can do this directly', 'roughly', 'a person must rebuild it', or 'no equivalent exists' - and explains why. It also writes the readable report.

**`src/translators/dax_to_tableau.py`**  
Rewrites simple Power BI formulas into Tableau formulas. It only handles the easy, safe kinds and refuses everything else out loud - a wrong formula is worse than no formula.

**`src/extractors/data_extract.py`**  
Asks Power BI itself for the numbers behind each chart. For every visual it writes one question in Power BI's query language (group by these fields, apply these filters, compute these measures) and turns the answer into a small table Tableau can draw. No guessing: the numbers are Power BI's own.

**`src/generators/twb_writer.py`**  
Builds the Tableau file. It creates one worksheet per chart with the right fields on the right shelves, one dashboard per page with charts in the same positions, and a spreadsheet-style data stub so the workbook opens. Real data is plugged in afterwards by the user.

**`src/cli.py`**  
The one command you run. Give it a .pbix and it produces the assessment, the Tableau workbook and a summary. Optional switches let it read the formulas from a running Power BI Desktop.

**`src/pipeline.py`**  
The one recipe both the command line and the website follow: open the file, (optionally) fetch the formulas, judge every chart, translate what is safe, build the Tableau workbook, write the reports. Because there is only one recipe, the website can never disagree with the command line.

**`src/web/app.py`**  
The local website's engine. It shows the page, lists the sample files, accepts a dropped .pbix, runs the recipe, remembers previous runs and hands back the files to download. It contains no migration logic at all.

**`src/web/static/index.html + styles.css`**  
The look of the website: an iOS-style glass design with four colour themes (Emerald, Sunset, Ocean, Sakura), light and dark mode, and a button to calm the animation.

**`src/web/static/app.js`**  
What makes the page work: drag-and-drop, the Run button, the progress steps, the result tiles and tables, downloads and the list of previous runs. Every number it shows was produced by the recipe.

**`src/web/static/backgrounds.js`**  
The living backgrounds: falling sakura petals, ocean waves with bubbles, a slow sunset with clouds, and fireflies among leaves for Emerald. They stay faint so the data stays readable.

**`run_app.bat`**  
The double-click launcher. Finds Python, creates a private environment on first run, installs the libraries automatically (again only when requirements.txt changes), starts the app and opens your browser.

**`tests/test_web.py`**  
Checks the website's engine: pages load, samples are listed, analysing works via upload and via the sample list, downloads and the zip work, bad requests are refused politely.

**`tests/conftest.py`**  
Tells the tests where the two real Power BI files live. The tests use the actual files, not toy examples.

**`tests/test_format_assumptions.py`**  
A written-down list of everything we proved about the file format, as tests. If Microsoft changes the format, these fail first and tell us exactly what moved.

**`tests/test_pbix_reader.py`**  
Checks the file opener.

**`tests/test_ir.py`**  
Checks the shared language stays neutral and convenient.

**`tests/test_layout_parser.py`**  
Checks the layout reader against the real reports.

**`tests/test_compatibility.py`**  
Checks every chart gets a verdict.

**`tests/test_twb_writer.py`**  
Checks the Tableau file is internally consistent.

**`tests/test_model_reader.py`**  
Checks the formula reader using data captured from the real Power BI engine.

**`tests/test_dax_to_tableau.py`**  
Checks the formula translator says yes to the safe cases and no to the rest.

**`tests/test_cli.py`**  
Checks the whole thing works end to end with one command.

**`tests/fixtures/dmv/*.json`**  
Snapshots of what Power BI's engine answered when we asked for the model. Lets tests run without Power BI open.

**`input/*.pbix`**  
The two Power BI reports we migrate. Never modified; hashes recorded.

**`output/<report>/assessment.md`**  
The readable migration report: what moves, what needs a person, and why.

**`output/<report>/<report>.twb`**  
The Tableau workbook. Open it in Tableau Desktop.

**`output/<report>/data/model_stub.csv`**  
An empty spreadsheet with the right column names so Tableau has something to connect to.

**`output/<report>/summary.json`**  
Machine-readable version of the numbers printed on screen.

**`output/<report>/measures.json`**  
Every business formula, its Tableau translation, or the reason it could not be translated.

**`docs/FORMAT_FINDINGS.md`**  
Everything we learned about the file formats, with proof.

**`PROJECT_PLAN.md`**  
The plan and what is done.

**`PROJECT_TRACKER.xlsx`**  
The living tracker: milestones, architecture, findings, environment, decisions, risks.

**`README.md / requirements.txt / pytest.ini / .gitignore`**  
How to install and run; what to ignore.

## Part 2 - for analysts and developers

| File | Lines | What it does | Key functions / ideas |
|---|---:|---|---|
| `src/parsers/pbix_reader.py` | 109 | Thin wrapper over zipfile. Validates the container, exposes parts, decodes Report/Layout (UTF-16LE JSON, no BOM) and caches it. Also owns load_json_string(), the helper for Power BI's double-encoded JSON-in-string fields. Raises PbixError on anything that is not a usable report. | PbixReader.layout() - the single entry point everything else uses. decode_text_part() - the encoding detection; ask yourself why UTF-8 would silently produce garbage here. |
| `src/parsers/layout_parser.py` | 353 | Walks sections -> visualContainers, decodes config/query/filters/dataTransforms, maps visualType -> ChartType, projection roles -> Role, SemanticQuery Column/Measure/Aggregation -> FieldRef, and filter predicates (In/Comparison/And/Not) -> Condition trees. Position is taken from config.layouts[0].position with top-level x/y as fallback. Field metadata is resolved from three sources in priority order. | parse_visual() - the heart. _field_index() - why dataTransforms beats query beats name-only. _aliases() - subquery aliases, the bug the Regional file caught. VISUAL_TYPE_MAP / ROLE_MAP - the vocabulary tables; every entry was observed in a real file. |
| `src/parsers/model_reader.py` | 232 | Two layers: parse_dmv() is pure (DMV rows -> DataModel) and tested on captured fixtures; fetch_dmv() shells out to PowerShell, loads Microsoft.PowerBI.AdomdClient.dll from Desktop's own bin folder with an AssemblyResolve hook, runs TMSCHEMA_* DMVs and returns JSON. discover_ports() reads msmdsrv.port.txt (UTF-16LE). Raises ModelUnavailable when Desktop is closed. | parse_dmv() - Type=3 row-number columns are skipped; Type=2 carries DAX. Why the live route was chosen over scraping the ABF blob (see FORMAT_FINDINGS section 3). |
| `src/models/ir.py` | 263 | Vendor-neutral dataclasses and enums: Report/Page/Visual/FieldRef/Filter/Condition plus DataModel (Measure/Column/Relationship). Frozen FieldRef so it hashes. Source values are preserved in source_* fields; unknowns become OTHER/UNKNOWN. A test asserts no vendor vocabulary leaks in. | Visual.fields / dimensions / measures - how downstream code asks questions. Role vs source_roles - neutral vs provenance. This is the interview centrepiece: parsers only produce it, generators only consume it. |
| `src/analyzers/compatibility.py` | 263 | CAPABILITY_MATRIX maps ChartType -> (Verdict, Tableau mark, note). assess_visual() applies per-visual downgrades (unbound, unknown aggregation, name-only fields, untranslatable filter). analyze() -> ReportAssessment with counts; render_markdown() writes assessment.md. Never writes workbooks - that separation keeps the assessment useful even when generation cannot. | The matrix is a judgement call - be ready to defend each row. _worse() - severity ordering. Why DECORATIVE is its own verdict rather than 'unsupported'. |
| `src/translators/dax_to_tableau.py` | 224 | Tokenizer + recursive-descent parser for a named DAX subset (arithmetic, comparisons, aggregates over a column, DIVIDE, IF, BLANK, measure/column refs). Emits Tableau calc text. Anything outside the subset raises UnsupportedDax naming the token. translate_measures() returns per-measure success/error. | Grammar: logical > comparison > additive > term > unary > primary. Why CALCULATE/FILTER/SUMX are refused (filter/row context has no textual Tableau equivalent). On Regional: 4 of 15 translate. |
| `src/extractors/data_extract.py` | 217 | Pure module: plan_for_visual() builds EVALUATE SUMMARIZECOLUMNS(dims, filters, "alias", [Measure]/AGG(col)) from the IR; filter_arguments() maps Condition trees to TREATAS/FILTER(ALL()) terms (IN, comparisons, NOT, blank), noting what it cannot express (measure filters); normalise_rows() maps engine headers/dates/nulls onto the plan's columns. The engine call lives in model_reader.run_queries. | Why per visual rather than one flat table: non-additive measures. filters='all|visual|none'. |
| `src/generators/twb_writer.py` | 550 | Emits .twb XML via ElementTree following Superstore.twb: one federated datasource over a textscan CSV (header-only), columns from a FieldRegistry (base columns + calculated measures), column-instances with derivations (sum/avg/none/usr), worksheets with rows/cols/encodings by chart type, Measure Names for multi-measure text tables, dashboards with 0-100000 zones, worksheet windows. Emits the loader requirements found by bisect: format manifest + source-build, explain-data node, simple-ids, style per table, non-self-closing empty shelves, entity-escaped newlines; no dashboard windows. With snapshots, each visual gets its own datasource + CSV (data_mode registry: measures are plain columns). Verified: opens in Tableau 2026.2. | FieldRegistry.instance() - how a FieldRef becomes '[sum:Sales_Total:qk]'. _plan() - the shelf rules per ChartType. finalize_aggregates() - why a translated measure uses 'usr' derivation. Decision D7 (flattened stub datasource). |
| `src/cli.py` | 72 | argparse front-end: parse -> optional model (--model live / --model-dir) -> analyze -> translate measures -> generate. Writes assessment.md, summary.json, measures.json, <name>.twb, data/model_stub.csv. Exit codes 0/1/2. | run() - the pipeline in 40 lines; every stage is one function call. _tableau_name() must agree with the generator's naming or translated DAX would reference missing fields. |
| `src/pipeline.py` | 287 | run_pipeline(pbix, out_dir, model_mode, model_port, model_dir, generate_twb) -> dict. Orchestrates parse -> attach_model -> analyze -> translate_measures -> generate and serialises everything (pages, visuals with verdicts/roles/filters/sheet, measures with DAX + translation or refusal, workbook manifest, files) to result.json; also writes summary.json / measures.json / assessment.md. Raises PbixError / ModelUnavailable. | build_result() is the single source of truth for what the UI shows; sheet_by_visual is keyed by page AND visual id because Power BI reuses container names on every page. |
| `src/web/app.py` | 213 | FastAPI + uvicorn. GET / (static UI), /api/health (Python version, Power BI Desktop ports, Tableau present), /api/inputs, POST /api/analyze (multipart upload or input_rel; model_mode none|live|dir; runs the pipeline in a threadpool), /api/runs, /api/runs/{id}, /api/runs/{id}/files/{path}, /api/runs/{id}/bundle (zip). Path safety via _under(); 400/404/409 mapped from PbixError / missing / ModelUnavailable. main() opens the browser. | _under() - why every path is resolved against its base folder. Errors are HTTP codes, never silent. |
| `src/web/static/index.html + styles.css` | - | Single page; CSS variables per [data-theme] x [data-mode]; verdict colours as tokens; responsive grid; prefers-reduced-motion honoured; conic-gradient donut; no framework. | Theme tokens live in one place (:root + [data-theme]/[data-mode]) - add a theme by adding four variables. |
| `src/web/static/app.js` | - | Vanilla JS module: theme persistence (localStorage), form state, FormData POST to /api/analyze, stepper animation while awaiting, renderers for tiles/donut/legend, visuals-by-page table with expandable detail rows, measures (DAX vs Tableau / refusal), filters, skipped, files; run history loading. | show(result) - the one function that maps result.json to the screen. |
| `src/web/static/backgrounds.js` | - | Canvas scenes keyed by theme; each has init/step/draw; DPR-aware resize; paused when the tab is hidden; static frame under reduced motion; colours adapt to light/dark. | Backgrounds.set(theme, mode) and Backgrounds.reduce(flag) are the whole public surface. |
| `run_app.bat` | - | Locates py/python; creates .venv if missing; hashes requirements.txt with certutil and re-installs when the hash differs from .venv/.requirements.sha256; runs python -m src.web --open --port 8765; pauses on error so the message stays visible. | The hash stamp is what makes second launches instant. |
| `tests/test_web.py` | 122 | FastAPI TestClient: index/static, health, inputs, analyze (sample + offline model + upload round-trip), sheet-per-page regression, runs/files/bundle, path-escape and bad-input codes, live-model 409. | test_bad_requests - the path-escape case is the security test. |
| `tests/conftest.py` | 30 | Session-scoped fixtures returning the input/*.pbix paths; skips cleanly if a file is missing. | Why real files as fixtures: the parser cannot drift from reality. |
| `tests/test_format_assumptions.py` | 244 | 15 assertions covering container parts, UTF-16LE, JSON-in-string, measured inventories (937/55, 24/16), position location, query/projection independence, ABF opacity. Updated once when Retail was re-saved. | inventory() - the container walk. The two tests that failed on first run and corrected assumptions. |
| `tests/test_pbix_reader.py` | 77 | 11 tests: both files, caching, error paths, encoding detection. | - |
| `tests/test_ir.py` | 59 | Hashability, dedup across roles, table collection, no-vendor-vocabulary guard. | test_ir_contains_no_vendor_vocabulary |
| `tests/test_layout_parser.py` | 156 | Retail: pages, counts, roles, metadata, positions, filters. Regional: 937/55/45, no-query visuals noted, custom visual flagged, groups, every filter predicate mapped. | test_regional_condition_kinds_all_mapped |
| `tests/test_compatibility.py` | 73 | Matrix completeness, downgrade rules, Retail 16/16 migratable, Regional counts, Markdown content. | - |
| `tests/test_twb_writer.py` | 132 | Well-formed XML, element order, every shelf field declared, CSV header == base columns, placeholder vs translated measures, dashboard zones in bounds, Regional generates. | test_every_shelf_field_is_declared |
| `tests/test_model_reader.py` | 66 | Fixture-driven: 10 tables, 69 columns (row-numbers excluded), 15 measures with DAX intact, 8 relationships, UTF-16 port file, clear error when Desktop is closed. | - |
| `tests/test_dax_to_tableau.py` | 62 | 11 supported forms, 6 refusals with reasons, and the real Regional measures splitting 4 ok / 11 refused. | test_real_regional_measures_split_honestly |
| `tests/test_cli.py` | 50 | Retail end-to-end artefacts; Regional with offline model translates measures; exit codes 1 and 2. | - |
| `tests/fixtures/dmv/*.json` | - | Raw TMSCHEMA_* DMV rows captured from localhost:52961 with Regional Sales open (2026-09-12). | These are real, not hand-written. |
| `input/*.pbix` | - | Retail Analysis (Phase 1 target; re-saved by Desktop 2.149 on 2026-09-12) and Regional Sales (stress test). | - |
| `output/<report>/assessment.md` | - | render_markdown() output: summary table, per-page visual table with fields by role, filters. | - |
| `output/<report>/<report>.twb` | - | Generated XML; 14 sheets / 3 dashboards for Retail, 47 / 9 for Regional. | - |
| `output/<report>/data/model_stub.csv` | - | Header-only CSV; columns = every base field referenced. Replace with a real extract. | - |
| `output/<report>/summary.json` | - | Counts, verdicts, model status, workbook manifest. | - |
| `output/<report>/measures.json` | - | Written only when a model was supplied. | - |
| `docs/FORMAT_FINDINGS.md` | 316 | Empirical notes: container, layout, model blob, corrections found by tests, Tableau format from Superstore. | Section 5 - the corrections. |
| `PROJECT_PLAN.md` | 78 | Architecture sketch, milestone table with verification, decisions. | - |
| `PROJECT_TRACKER.xlsx` | - | Status dropdowns roll up on the Overview sheet. | - |
| `README.md / requirements.txt / pytest.ini / .gitignore` | - | Standard-library runtime; pytest + openpyxl for dev; tests run from the project root. | - |
