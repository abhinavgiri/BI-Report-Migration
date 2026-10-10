# Developer Handbook

For someone who can read a bit of Python but has never seen this project.
By the end you should be able to open any file and know what it is for, find
the file responsible for a bug, and judge whether a change belongs there.

Companion documents: [CODEBASE_GUIDE.md](CODEBASE_GUIDE.md) (one-line summary per
file, two audiences), [FORMAT_FINDINGS.md](FORMAT_FINDINGS.md) (the evidence behind
every format decision), [../PROJECT_PLAN.md](../PROJECT_PLAN.md) (milestones and decisions).

---

## 1. What the tool does, in sixty seconds

A company has reports in **Power BI**. They want them in **Tableau**. Doing that by
hand means opening every chart, writing down which fields it uses, and rebuilding it.

This tool does the reading for you:

1. You give it a `.pbix` file (a Power BI report).
2. It works out every page, every chart, which data each chart shows, and which filters apply.
3. It judges each chart: *Tableau can do this exactly* / *roughly* / *a person must rebuild it* / *no equivalent exists*.
4. It writes a **Tableau workbook** (`.twb`) with the charts it can build, fields already on the right shelves, pages laid out as dashboards.
5. Optionally it asks Power BI for the actual numbers so the charts are not empty.

It is an **assistant**, not a magic converter. The guiding rule of the whole codebase:

> **Never invent anything.** If we cannot know something, say so — never guess and
> present the guess as fact.

That rule is why the code has so many "reasons", "notes" and "problems" lists.

---

## 2. The one idea that explains the whole design

Imagine translating a book from Japanese to Portuguese. You could learn
"Japanese→Portuguese". But then Japanese→French is another whole job, and
Japanese→German another.

Instead you translate **Japanese → a neutral notebook**, then **notebook → Portuguese**.
Now adding French means writing one new half, not a whole new translator.

This project's neutral notebook is the **IR — Intermediate Representation**, in
`src/models/ir.py`. It describes a report in words that belong to neither vendor:
pages, visuals, fields, filters, measures.

```
Power BI files ──► parsers ──► IR ──► analyzer  ──► the assessment report
                   (know only          │
                    Power BI)          └──► generator ──► the Tableau workbook
                                            (knows only Tableau)
```

**The parsers never mention Tableau. The generator never mentions Power BI.**
There is even a test that enforces this — `tests/test_ir.py::test_ir_contains_no_vendor_vocabulary`
reads the source of `ir.py` and fails if the words "tableau" or "pbix" appear in it.

If you remember only one thing: **everything flows through the IR.** When you are
lost, ask "is this before the IR or after it?" — that tells you which half to look in.

---

## 3. Tech stack — what is installed and why

Runtime dependencies are tiny on purpose, so a colleague needs only Python.

| Thing | What it is | Why this project uses it |
|---|---|---|
| **Python 3.10+** | the language | Everything is Python except the browser page |
| **FastAPI** | a web-server library | Turns Python functions into web addresses (`/api/analyze`) |
| **uvicorn** | the actual web server | Runs FastAPI and listens on `http://127.0.0.1:8765` |
| **python-multipart** | file-upload support | Lets the browser send a `.pbix` to the server |
| **pytest** | test runner | `python -m pytest` runs all 141 tests |
| **httpx** | HTTP client | Used *by tests only*, to call the API without a browser |
| **openpyxl** | writes `.xlsx` | Builds the tracker and guide spreadsheets |
| **HTML + CSS + JavaScript** | the web page | Plain, no React/Vue — nothing to compile or install |

**From Python's standard library** (already there, nothing to install), and these carry
most of the real work:

| Module | Used for | Seen in |
|---|---|---|
| `zipfile` | a `.pbix` is secretly a ZIP | `pbix_reader.py` |
| `json` | reading Power BI's report definition | `layout_parser.py`, everywhere |
| `xml.etree.ElementTree` | building the Tableau file | `twb_writer.py` |
| `csv` | writing the data tables | `twb_writer.py` |
| `re` (regular expressions) | finding patterns in text | `dax_to_tableau.py`, `twb_writer.py` |
| `dataclasses` | short way to define data types | `ir.py` and most modules |
| `enum` | fixed lists of choices | `ir.py`, `compatibility.py` |
| `subprocess` | running PowerShell to talk to Power BI | `model_reader.py` |
| `pathlib` | file paths that work on any OS | everywhere |

---

## 4. The data structures you must know

Almost every "data structure" here is a **dataclass** — Python shorthand for
"a thing with named fields".

```python
@dataclass
class Placement:
    x: float = 0.0
    y: float = 0.0
```

That gives you `Placement(x=10, y=20)`, printing, and `==` comparison for free.
The other structure is an **Enum** — a fixed list of allowed values, so a typo
becomes an error instead of a silent bug:

```python
class Verdict(Enum):
    DIRECT = "direct"
    APPROXIMATE = "approximate"
    ...
```

### The IR types (`src/models/ir.py`, 263 lines)

Read this file first. It is the vocabulary for everything else.

| Type | Plain English | Key fields |
|---|---|---|
| `Report` | one whole Power BI file | `name`, `pages`, `filters`, `model` |
| `Page` | one tab in the report | `name`, `visuals`, `filters`, `width`/`height` |
| `Visual` | one chart, text box or button | `chart_type`, `placement`, `roles`, `filters`, `notes` |
| `FieldRef` | "this chart uses Sales.Amount" | `table`, `name`, `kind`, `aggregation` |
| `Filter` | "only show Region = West" | `scope`, `field`, `condition` |
| `Condition` | the actual test, possibly nested | `op`, `values`, `children` |
| `Placement` | where the chart sits on the page | `x`, `y`, `width`, `height` |
| `DataModel` | the report's formulas and tables | `tables`, `columns`, `measures`, `relationships` |
| `Measure` | a business formula written in DAX | `table`, `name`, `expression` |
| `Relationship` | "Orders links to Customers" | `from_table`, `from_column`, `to_table`, `to_column` |

Four enums give the fixed vocabularies: `ChartType` (bar, line, pie, map, kpi…),
`FieldKind` (column / measure / aggregation), `Aggregation` (sum, average, min…),
`Role` (axis, legend, value, rows, columns, detail).

**Two subtleties worth understanding**, because bugs hide here:

- `FieldRef` is `@dataclass(frozen=True)` — it cannot be modified after creation.
  That makes it **hashable**, so it can go in a `set` or be a `dict` key. This is
  how `Visual.fields` removes duplicates when the same field is on two shelves.
- `Visual` keeps **both** `roles` (neutral: `Role.AXIS`) and `source_roles`
  (Power BI's own words: `"Category"`). Neutral for the generator, original for
  provenance and debugging. When something looks wrong on a shelf, compare the two.

### The one structure that is *not* a dataclass

`result.json` — the big dictionary `build_result()` returns and the web page
renders. It is a plain `dict`. That is the least safe part of the codebase
(see §9, improvement 2).

---

## 5. Ten programming concepts in this codebase, and where to see each

This is the section to read if you want to *learn* from the project rather than
just navigate it. Each concept links to the smallest real example.

**1. A file format is just bytes with rules.**
`.pbix` turns out to be a ZIP file with a different extension.
→ `pbix_reader.py::PbixReader.__init__` uses `zipfile.is_zipfile()` to prove it.

**2. Text encoding is not automatic.**
The report definition is stored as **UTF-16LE** (two bytes per character). Reading
it as the usual UTF-8 produces garbage, silently.
→ `pbix_reader.py::decode_text_part` looks at the first bytes and decides.

**3. Data can be double-wrapped.**
Power BI stores JSON *inside a JSON string*. You must parse twice.
→ `pbix_reader.py::load_json_string`, used all over `layout_parser.py`.

**4. Mapping one vocabulary to another with a lookup table.**
Instead of a hundred `if` statements, use a dictionary.
→ `layout_parser.py::VISUAL_TYPE_MAP` maps `"clusteredColumnChart" → ChartType.COLUMN`.
This is the simplest, most maintainable pattern in the project.

**5. Recursive functions for tree-shaped data.**
A filter can be `A AND (B OR NOT C)` — a tree. A function that calls itself handles
any depth.
→ `layout_parser.py::parse_condition` calls itself for `And` / `Or` / `Not`.
→ `compatibility.py::_has_unknown` walks the same tree to ask "is any part untranslatable?"

**6. Writing a small language parser (tokenizer + recursive descent).**
This is the "hardest" computer-science idea here, and it is worth reading.
Translating DAX to Tableau needs real understanding of the formula, so
`dax_to_tableau.py` does what real compilers do:
- **tokenize**: chop `SUM(Sales[Amount]) * 2` into pieces: `SUM`, `(`, `Sales[Amount]`, `)`, `*`, `2`
- **parse**: one function per precedence level, each calling the next —
  `logical → comparison → additive → term → unary → primary`.
  That chain is *why* `1 + 2 * 3` knows to multiply first.
→ `dax_to_tableau.py::Translator`, 11 small methods.

**7. Building XML programmatically.**
A `.twb` is an XML document. Rather than glue strings together (easy to break),
build a tree of elements.
→ `twb_writer.py::_worksheet_el` and friends use `ET.SubElement(...)`.

**8. A registry to keep names consistent.**
The same field appears in the datasource, in the worksheet and in the CSV header.
All three must agree exactly or the workbook breaks. So one object owns naming.
→ `twb_writer.py::FieldRegistry` — `base_column()`, `measure()`, `instance()`.

**9. Talking to another program.**
Power BI Desktop runs a hidden database. Python cannot speak its protocol, so the
code writes a PowerShell script, runs it with `subprocess`, and reads the JSON it
leaves behind.
→ `model_reader.py::run_queries` and the `_PS_SCRIPT` string above it.

**10. Separating decision from action.**
The analyzer decides *what can be migrated*; the generator decides *how to write it*.
They never call each other. That is why a failure to write `.twb` still leaves you a
useful assessment.
→ `compatibility.py` has no file-writing except its Markdown report; `twb_writer.py`
makes no judgements, it only reads `Verdict`.

---

## 6. The journey of one `.pbix`, function by function

This is the whole program in order. Follow it once and the codebase stops feeling large.

```
run_app.bat → src/web/app.py → src/pipeline.py::run_pipeline → everything else
```

| # | Step | Function | What happens |
|---|---|---|---|
| 1 | Open | `pbix_reader.PbixReader(path)` | Check it is a ZIP, list its parts |
| 2 | Decode | `.layout()` → `decode_text_part` | Pull out `Report/Layout`, decode UTF-16LE, parse JSON |
| 3 | Walk | `layout_parser.parse_layout` | For each page → `parse_page` → `parse_visual` |
| 4 | Identify | `parse_visual` | `VISUAL_TYPE_MAP` gives the neutral chart type; position from `config.layouts[0].position` |
| 5 | Fields | `_field_index` + `parse_field_expr` | Work out which fields the chart uses and whether each is a column, a measure or an aggregation |
| 6 | Filters | `parse_filters` → `parse_condition` | Turn Power BI's filter JSON into a `Condition` tree |
| 7 | *(optional)* Model | `model_reader.read_model` | Ask the running Power BI Desktop for tables, columns, measures (with DAX), relationships |
| 8 | *(optional)* Fix | `pipeline.reconcile_fields` | The layout sometimes only gives a name; the model says whether it is really a measure |
| 9 | Judge | `compatibility.analyze` → `assess_visual` | Look up `CAPABILITY_MATRIX`, then downgrade for per-chart problems; produce a `Verdict` + reasons |
| 10 | Translate | `dax_to_tableau.translate_measures` | Try to rewrite each DAX formula; refuse loudly if outside the supported subset |
| 11 | *(optional)* Data | `pipeline.snapshot_visuals` → `data_extract.plan_for_visual` | Build one DAX query per chart, run it in Power BI, keep the rows |
| 12 | Build | `twb_writer.generate` | Datasources, worksheets, dashboards, CSVs → the `.twb` file |
| 13 | Report | `pipeline.build_result` | Package everything into `result.json`; `render_markdown` writes `assessment.md` |
| 14 | Show | `web/static/app.js::show` | Draw the tiles, donut, tables and download links |

---

## 7. Every file, every function

`src/` totals **36 classes and 125 functions**. Regenerate this list any time with:

```bash
.venv/Scripts/python.exe tools/index_code.py
```

### `src/pipeline.py` (287 lines) — the recipe everything else follows

The most important file after `ir.py`. Both the CLI and the website call
`run_pipeline`, which is why they can never disagree.

| Function | Does |
|---|---|
| `run_pipeline(...)` | The 14 steps above, in order. Returns the big result dict |
| `attach_model(...)` | Fetches the model; **refuses a model from a different report** (no table in common) |
| `reconcile_fields(report)` | Uses the model to fix fields the layout only named |
| `snapshot_visuals(...)` | Asks Power BI to evaluate every chart's query |
| `build_result(...)` | Turns objects into the JSON the web page reads |
| `tableau_field_name`, `live_port`, `_field`, `_filter`, `_visual` | Small helpers |

### `src/parsers/` — everything that understands Power BI

**`pbix_reader.py` (109 lines)** — the only file that touches the raw file.
`PbixReader` (`has_part`, `read_bytes`, `read_text`, `version`, `has_data_model`, `layout`),
plus `decode_text_part`, `load_json_string`, and the `PbixError` exception.

**`layout_parser.py` (353 lines)** — the biggest translator of Power BI vocabulary.
`parse_pbix` → `parse_layout` → `parse_page` → `parse_visual`.
Supporting: `parse_field_expr`, `field_from_queryref`, `_field_index`, `_aliases`,
`_entity`, `parse_filters`, `parse_condition`, `parse_literal`, `_placement`, `_title`.
Lookup tables: `VISUAL_TYPE_MAP`, `ROLE_MAP`, `AGGREGATION_MAP`, `COMPARISON_MAP`.

**`model_reader.py` (234 lines)** — talks to the live Power BI Desktop.
`parse_dmv` (pure, testable), `run_queries` (the PowerShell bridge), `fetch_dmv`,
`read_model`, `find_port_for`, `discover_ports`, `workspace_root`, `load_dmv_dir`,
and `ModelUnavailable`.

### `src/models/ir.py` (263 lines) — the neutral vocabulary
No logic, only the types from §4 plus small conveniences: `Visual.fields`,
`.dimensions`, `.measures`, `.is_data_bound`; `Page.data_visuals`;
`Report.visuals`, `.tables_referenced`; `FieldRef.qualified_name`, `.is_measure_like`.

### `src/analyzers/compatibility.py` (263 lines) — the judge
`CAPABILITY_MATRIX` (one row per `ChartType`) plus `assess_visual`, `analyze`,
`_worse` (severity ordering), `_has_unknown`, `render_markdown`, `describe_condition`,
and the `Verdict` / `Capability` / `Assessment` / `ReportAssessment` types.

### `src/translators/dax_to_tableau.py` (224 lines) — the formula translator
`tokenize`, the `Translator` class (`translate`, `logical`, `comparison`, `additive`,
`term`, `unary`, `primary`, `call`, `peek`, `take`), `translate`, `translate_measures`,
`_split_colref`, and `UnsupportedDax`.

### `src/extractors/data_extract.py` (217 lines) — asks Power BI for the numbers
`plan_for_visual` (builds the DAX), `filter_arguments`/`_filter_terms`/`_predicate`
(filters → DAX), `normalise_rows` (engine answers → table rows), `dax_literal`,
`colref`, `safe_name`, `_cell`; types `ExtractColumn`, `ExtractPlan`, `Extract`.

### `src/generators/twb_writer.py` (550 lines) — writes the Tableau file
The largest file. `generate` is the entry point; `FieldRegistry` owns naming;
`_plan` decides which field goes on which shelf per chart type; `_datasource_el`,
`_worksheet_el`, `_dashboard_el`, `_window_el`, `_column_el` build the XML;
`_escape_text_newlines` and `_simple_id` satisfy Tableau's loader quirks;
`_write_csv` writes the data; `_sheet_name` keeps names unique.

### `src/web/` — the local website
**`app.py` (213 lines)**: `index`, `health`, `inputs`, `analyze`, `runs`, `run_detail`,
`run_file`, `run_bundle`, `main`, plus `_safe_name` and `_under` (path safety).
**`static/index.html` / `styles.css`**: the page and its four themes.
**`static/app.js` (253 lines)**: `run`, `show`, `renderVisuals`, `renderMeasures`,
`renderFilters`, `renderSkipped`, `renderFiles`, `loadRuns`, `loadHealth`, `initTheme`.
**`static/backgrounds.js` (161 lines)**: the four canvas animations.

### `src/cli.py` (72 lines) — the terminal version
One function, `run(argv)`: read the arguments, call `run_pipeline`, print a summary.

### `tests/` — 141 tests in 14 files
Named after what they protect: `test_format_assumptions.py` (every format fact),
`test_layout_parser.py`, `test_compatibility.py`, `test_twb_writer.py`,
`test_dax_to_tableau.py`, `test_data_extract.py`, `test_web.py`, and so on.
`conftest.py` points them at the two real `.pbix` files — **the tests use real
Power BI files, not toy examples**, which is why they catch format surprises.

---

## 8. Something is broken — which file do I open?

Work backwards from what you see. The pipeline order in §6 is the map: a symptom
almost always belongs to the *last* step that touched it.

| What you see | Most likely file | Then check |
|---|---|---|
| "not a ZIP container" / "has no Report/Layout" | `parsers/pbix_reader.py` | Is the file really a `.pbix`? A `.pbit` template has different parts |
| Garbled text, `UnicodeDecodeError` | `pbix_reader.py::decode_text_part` | Encoding detection; also set `PYTHONIOENCODING=utf-8` on Windows |
| Wrong number of pages/visuals | `layout_parser.py::parse_layout` / `parse_page` | Compare with `tests/test_format_assumptions.py`, which pins the real counts |
| A chart shows the wrong type, or `unknown` | `layout_parser.py::VISUAL_TYPE_MAP` | Add the missing `visualType` name |
| Fields missing or on the wrong shelf | `layout_parser.py::_field_index`, then `ROLE_MAP` | Compare `visual.roles` vs `visual.source_roles` in `result.json` |
| A field is called a column but is a measure | `pipeline.py::reconcile_fields` | Needs the model; without it the kind is a guess |
| Verdict looks too harsh or too kind | `analyzers/compatibility.py::CAPABILITY_MATRIX` | The per-visual downgrades in `assess_visual` |
| A measure was not translated | `translators/dax_to_tableau.py` | The refusal reason is in `measures.json` — it is usually correct to refuse |
| Tableau says "errors occurred while loading" | `generators/twb_writer.py` | `FORMAT_FINDINGS.md` §7 lists the nine loader requirements |
| Workbook opens but a chart is blank | Data, not structure | Run with snapshots; check `data/*.csv` has rows |
| Charts blank *with* snapshots | `extractors/data_extract.py` | `result.json` → `data.problems`; a visual empty in Power BI stays empty |
| "model unavailable" / "does not match this report" | `parsers/model_reader.py::find_port_for` | Is *this* report open in Power BI Desktop? |
| Web page says "Failed to fetch" | The server stopped | The `run_app.bat` window; restart it |
| A web request returns 400/404/409 | `web/app.py::analyze` | The status code tells you which check rejected it |
| Buttons/themes misbehave | `web/static/app.js` | Browser console (F12) |
| `.bat` fails on a colleague's machine | `run_app.bat` | Python version, then internet access for `pip` |

**Three habits that beat guessing:**

1. **Read `output/<report>/result.json`.** Everything the tool concluded is in there.
   Most "bugs" are visible in it before you touch any code.
2. **Run one test file**: `python -m pytest tests/test_layout_parser.py -v`.
   If parser tests pass but the workbook is wrong, the bug is *after* the IR.
3. **Search for the vocabulary**: `grep -rn "clusteredColumnChart" src/` finds every
   place a concept is handled.

---

## 9. Where this codebase could be better

Honest self-assessment. These are real, not hypothetical, and roughly in order of value.

**1. `twb_writer.py` is 550 lines doing three jobs.** It holds naming (`FieldRegistry`),
XML building (`_*_el`) and orchestration (`generate`). Splitting it into
`twb/naming.py`, `twb/xml.py`, `twb/build.py` would make each testable alone.
*This is the file most likely to be painful to change.*

**2. `result.json` is an untyped dictionary.** `build_result` returns a plain `dict`
with ~20 keys, and `app.js` reads them by name. A typo like `r.data_visual` fails
silently in the browser. A `@dataclass` (or a Pydantic model, which FastAPI supports
natively) would catch that and document the shape in one place.

**3. `run_pipeline` takes 8 parameters, passed positionally from `app.py`.** One
mis-ordered argument would be a nasty bug. An options dataclass —
`run_pipeline(pbix, RunOptions(model_mode="live", data_mode="live"))` — is safer
and reads better.

**4. No logging.** The code uses `print()` in the CLI and returns problems in lists.
Python's `logging` module would let you turn on detail when diagnosing without
changing code.

**5. The PowerShell bridge is the most fragile part.** `model_reader.run_queries`
writes a script, spawns PowerShell, and reads JSON files back. It works and it needs
no installation — that was the point — but it is slow (one process per batch) and
error handling depends on string matching. Alternatives worth weighing:
`pythonnet` to load the ADOMD library directly, or `pyadomd`. Both add an install
step, which is exactly what `run_app.bat` was designed to avoid, so the trade is real.

**6. Filters are analysed but never written into the `.twb`.** The biggest *functional*
gap. The `Condition` trees already exist in the IR; mapping them to Tableau's
`<filter class="categorical">` elements is the highest-value next feature.

**7. No golden-file test for the workbook.** Tests check structure (element order,
every shelf field declared). They would not notice a subtle formatting regression.
Storing one known-good `.twb` and diffing would catch more.

**8. `_safe()` could collide.** Two different fields whose names differ only by a
character that `_safe` strips would map to one column. `FieldRegistry._unique`
prevents a crash by appending a number, but the *pairing* to the CSV could drift.
A hash suffix would be stricter.

**9. The front-end is one 253-line IIFE.** Fine at this size and it keeps the
"no build step" promise, but past ~500 lines it should become ES modules.

**What is deliberately *not* a flaw:** the tests depending on real `.pbix` files
(that is what makes them trustworthy), the tiny DAX subset (refusing is correct),
and the empty data stub by default (the data genuinely is not in the file).

---

## 10. Running it

```bash
run_app.bat                    # easiest: sets everything up, opens the browser
python -m pytest               # 141 tests
python -m src.web              # the website only
python -m src.cli "input/Retail Analysis Sample PBIX.pbix"
python -m src.cli "input/Regional Sales Sample.pbix" --model live --data live
```

Outputs land in `output/<report name>/`: `assessment.md` (readable),
`result.json` (everything), `<report>.twb` (the workbook), `data/*.csv` (the rows),
`measures.json` (DAX translations and refusals).
