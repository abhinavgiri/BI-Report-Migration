# Format Findings (empirical)

Everything here was measured from the two real `.pbix` files in `input/`,
not taken from blog posts. Re-runnable probes live in `tests/`.

## 1. `.pbix` is a ZIP (OPC) container

Observed parts:

| Part | Regional Sales | Retail Analysis | Notes |
|---|---|---|---|
| `Version` | yes (`1.25`) | yes | UTF-16LE text |
| `Report/Layout` | yes (6.2 MB) | yes (222 KB) | **UTF-16LE JSON, no BOM** |
| `DataModel` | yes (670 KB) | yes (13.5 MB) | ABF backup, see §3 |
| `DataMashup` | no | yes | Power Query M, separate container |
| `Connections` | yes | no | |
| `Report/LinguisticSchema` | yes | yes | Q&A synonyms |
| `Report/StaticResources/**` | yes | yes | themes, images, shapemaps |

Two different vintages → the reader must tolerate missing parts.

## 2. `Report/Layout` — parseable today

Encoding is **UTF-16LE with no BOM**. `json.loads(raw.decode("utf-16-le"))` works.

Top-level keys differ between files:

- Regional Sales: `id, theme, sections, pods, config, reportId, filters,
  resourcePackages, layoutOptimization, publicCustomVisuals`
- Retail Analysis: `theme, resourcePackages, sections, config,
  layoutOptimization, pods`

→ **`id`, `reportId`, `filters` are not guaranteed.** Parser must use `.get()`.

### Nested JSON-in-string

Several fields are JSON documents stored *as strings* inside the JSON.
They must be `json.loads`-ed a second time:

- `layout.config`, `layout.filters`
- `section.config`, `section.filters`
- `visualContainer.config`, `visualContainer.filters`, `visualContainer.query`

### Visual inventory (measured)

`Regional Sales Sample.pbix` — 11 pages, **937** containers, only **55 data-bound**:

```
732 actionButton      <- navigation chrome, not content
 68 shape
 26 textbox
 45 (singleVisualGroup - no singleVisual key)
 16 kpi *
 10 barChart *
  6 lineChart *
  5 pivotTable *
  3 tableEx *
  ... 1-3 each: scatter, funnel, ribbon, shapeMap, decompositionTree, qnaVisual
```

`Retail Analysis Sample PBIX.pbix` — **24** containers, **16 data-bound**:
clusteredColumnChart x4, card x2, map x2, scatterChart x2, slicer x2,
pie/area/line/column x1, plus 7 textboxes and 1 image.

**Consequence:** Retail Analysis is the right Milestone-1 fixture (small, classic
chart types). Regional Sales is the stress test (schema variance, groups, 937
containers, custom visual).

**Consequence:** a visual with no `singleVisual` key is a *group* container, and a
visual with `projections == None` is decorative. Both must be handled, not crashed on.

### The `query` field is the real prize

Each data-bound container carries a `SemanticQueryDataShapeCommand`:

```json
{"Query": {
  "From":   [{"Name":"t","Entity":"Time"}, {"Name":"s","Entity":"Sales"}],
  "Select": [
    {"Column":  {"Expression":{"SourceRef":{"Source":"t"}}, "Property":"FiscalMonth"},
     "Name":"Time.FiscalMonth"},
    {"Measure": {"Expression":{"SourceRef":{"Source":"s"}}, "Property":"Total Sales Variance"},
     "Name":"Sales.Total Sales Variance"}
  ],
  "Where":   [...],
  "OrderBy": [{"Direction":1, "Expression":{...}}]
}}
```

This is a clean relational structure. Critically it distinguishes:

- `Column`      -> a raw dimension/attribute
- `Measure`     -> a model measure (name only; DAX body is in DataModel)
- `Aggregation` -> inline agg over a column, with `Function` as an int enum

`config.singleVisual.projections` maps those `Name`s onto visual roles
(`Category`, `Series`, `Y`, ...) which is exactly what a Tableau shelf assignment needs.

## 3. `DataModel` — an ABF backup, NOT reliably parseable

Header bytes decode (UTF-16LE) to:

```
STREAM_STORAGE_SIGNATURE_)!@#$%^&*(<BackupLog><BackupRestore...
```

That is an Analysis Services **ABF** (backup) container. Measured: only 30.3% of
the 13.5 MB is printable ASCII.

DAX *fragments* are visible in the tail region as compression literals, e.g.
`CALCULATE([...], [ScenarioID]=2)`, `COUNTA('Store'[...])`, `AVERAGE(...)`,
`DISTINCTCOUNT(...)`, plus `<Format Format="CurrencyCustom">` blocks and KPI
goal/threshold definitions.

But they are interleaved with binary back-references (LZ77/XPress9-style), so the
readable runs are **fragments, not complete expressions**. A lookup for the known
measure name `Total Sales Variance` returns no hit in either UTF-8 or UTF-16.

**Decision: we do not scrape DAX out of the ABF blob.** Scraping would silently
emit corrupted calculations, which violates project rule 8 (never hide errors by
producing plausible-but-wrong output).

Instead the model layer is a **separate, explicit input contract**. To migrate
measures, the user supplies a TMDL export produced by a supported route:

1. Open the `.pbix` in Power BI Desktop -> *Save as* `.pbip` (Power BI Project).
   This writes the semantic model as plain-text **TMDL**. Official, no 3rd party.
2. Or Tabular Editor / DAX Studio against the running Desktop instance.

The tool must **detect** that only a `.pbix` was supplied and say so plainly,
rather than guessing measure definitions.

## 4. Windows gotcha

Default console encoding here is cp1252; printing parsed report text raises
`UnicodeEncodeError`. All IO must specify `encoding="utf-8"` explicitly, and the
CLI should not rely on the ambient console codepage.

## 5. Corrections caught by the Milestone 0 tests

The first test run failed and exposed two assumptions that were wrong. Recorded
here because they directly constrain the parser design.

### 5.1 Position is stored in two places, and the canonical one is nested

| File | top-level `x/y/z/width/height` | `config.layouts[0].position` |
|---|---|---|
| Regional Sales | present on all 937 containers | present, **full precision** |
| Retail Analysis | **absent entirely** | present |

Regional's top-level `x: 866.09` is a rounded mirror of
`config.layouts[0].position.x: 866.092245311708`.

**Rule:** read position from `config.layouts[0].position`; fall back to the
top-level keys only if `layouts` is missing. Never assume `vc["x"]` exists.

Container key-sets actually observed:

```
retail    15x ['config','dataTransforms','query']
           8x ['config']
           1x ['config','dataTransforms','filters','query']
regional 840x ['config','filters','height','id','width','x','y','z']
          48x [... 'dataTransforms','query','queryHash' ...]
          46x ['config','height','id','width','x','y','z']
           3x [... 'query','queryHash' ... no dataTransforms]
```

### 5.2 `query` and `projections` do NOT imply each other

Retail is perfectly aligned (16 bound / 16 with query), which is what misled the
first draft. Regional is not:

- **7 visuals have `projections` but no `query`** — all of them "advanced" types:
  `lineStackedColumnComboChart`, `pivotTable` (x2), `keyDriversVisual` (x3),
  `qnaVisual`. These build their query at runtime.
- **3 visuals have a `query` but no `projections`** — all `textbox`, i.e. dynamic
  field-driven text.

**Rule:** treat `query` as optional even for data-bound visuals. A visual is
"migratable" only if it has projections *and* a resolvable query; the analyzer
(Milestone 4) must report the rest as manual work rather than dropping them.

### 5.3 `dataTransforms` is a third JSON-in-string field

Present on 15/16 bound Retail visuals and 48 Regional visuals. It carries the
projection-to-query-column mapping and per-field display metadata. Not yet
parsed; relevant from Milestone 3 onward.

## 6. Tableau `.twb` — verified against the shipped Superstore workbook

Reference: `C:\Program Files\Tableau\Tableau 2026.2\help\Workbooks\en_US\Superstore.twbx`
(a ZIP; `Superstore.twb` inside is 1.2 MB of XML, `version="18.1"`).

Top-level order observed: `preferences, datasources, worksheets, dashboards, windows`
(plus feature-flag nodes prefixed `_.fcp.` that we do not emit).

**Datasource** (`Sales Commission`, CSV-backed):

```xml
<datasource caption=".." inline="true" name="federated.<id>" version="18.1">
  <connection class="federated">
    <named-connections>
      <named-connection caption=".." name="textscan.<id>">
        <connection class="textscan" directory="Data/Superstore" filename="Sales Commission.csv" .../>
      </named-connection>
    </named-connections>
    <relation .../>            <!-- Superstore wraps this in _.fcp.ObjectModelEncapsulateLegacy -->
  </connection>
  <column caption=".." datatype="integer|real|string" name="[X]" role="measure|dimension" type="quantitative|nominal">
    <calculation class="tableau" formula="[Sales]"/>     <!-- calculated fields -->
  </column>
  <column-instance column="[X]" derivation="Min" name="[min:X:qk]" pivot="key" type="quantitative"/>
</datasource>
```

**Worksheet**: `table > view` (datasources, `datasource-dependencies` with `column`
and `column-instance`, filters, `<aggregation value="true"/>`), then
`panes > pane > mark class="Bar|Area|Circle|Square|Automatic|Multipolygon"` and
`encodings` with children `color | lod | tooltip | size | text | geometry`, then
`rows` / `cols` holding field expressions such as
`[federated.x].[none:Region:nk]` (dimension) and `[federated.x].[sum:Sales:qk]`
(measure). Multiple fields join with ` / ` (nesting) or ` + ` (measures).

**Text tables with several measures** use `[:Measure Names]` — declared as a
string dimension column in the datasource — filtered by
`<groupfilter function="member" level="[:Measure Names]" member="&quot;[ds].[sum:X:qk]&quot;"/>`.

**Dashboard**: `<size maxheight minheight sizing-mode="vscroll"/>` and nested
`<zone h id w x y type-v2="layout-basic">` with coordinates in 0–100000 units.

**Window** per sheet: `<window class="worksheet" name=".."><cards>…` listing card
types `pages, filters, marks, columns, rows`.

Not present in Superstore, so **not verified**: a Pie mark, and a Line mark
(Line is standard Tableau but unobserved here). Pies are therefore emitted as
bars and downgraded to *approximate*.

### 6.1 Retail file re-saved on 2026-09-12

Between the investigation and the build, `Retail Analysis Sample PBIX.pbix`
was re-saved by Power BI Desktop 2.149 (size 13,620,450 → 9,712,677 bytes;
SHA-256 now `3F03073ED49A9BA3A0CE81B2D8C42C390B01C95821C3DB0D8339A80C2D24EE91`).
The `DataMashup` part disappeared and every container gained top-level
`x/y/width/height` mirrors — while pages, visuals and bindings were unchanged
(4 pages, 24 containers, 16 data-bound). Both vintages are handled; the
Milestone-0 tests were updated to assert only what holds across both.

## 7. What Tableau 2026.2 actually requires to load a `.twb` — found by bisect

The first generated workbook failed with the generic dialog *"Errors occurred
while trying to load the workbook"* and masked details. Screen access was not
available, so the cause was found empirically: ~60 variants were opened in
Tableau Desktop by a PowerShell harness that launches `tableau.exe <file>`,
watches the window title, and greps Tableau's own log
(`My Tableau Repository\Logs\log*.txt`) for `show-detailed-error-dialog` and
`end-workspace.load-workbook … "rk":"ok"`. Bisecting *down* from the shipped
Superstore.twb (known good) and *up* from ours isolated each requirement.

| Requirement | Evidence |
|---|---|
| `<document-format-change-manifest>` at the top **and** a `source-build` attribute on `<workbook>` | removing either from a working file breaks it (u1, u2); any build string is accepted (u5) |
| `<_.fcp.ExplainData_AuthorControls.true...explain-data>` as the last child | removing only it from Superstore breaks the file (f_no_explaindata) |
| `<simple-id uuid="{…}"/>` on every worksheet, dashboard and window | removing it from one Superstore sheet breaks the file (y2); the manifest's `WindowsPersistSimpleIdentifiers` / `SheetIdentifierTracking` explain why |
| `<style/>` inside every worksheet `<table>` (may be empty) | y1 fails without it, k6 opens with an empty one |
| Empty shelves written as `<rows></rows>` / `<cols></cols>`, **not** self-closing | a card sheet fails as `<rows />` and opens as `<rows></rows>` (c3) — Tableau's loader is not a plain XML parser |
| Newlines inside text nodes as `&#10;` / `&#13;` entities | an ElementTree round-trip of Superstore fails until they are re-escaped (s5 vs m8) |
| At least one worksheet | a datasource-only workbook always fails (e0) |
| Each `datasource-dependencies datasource="X"` needs `X` listed in the view's `<datasources>` | h3 fails, h4 opens |
| `<window class="dashboard">` needs a `<viewpoint>` per sheet | an **empty** `<viewpoints/>` makes the file unloadable (d4); one viewpoint per zone loads fine (section 10.2) |

Optional (verified removable): `repository-location`, `mapsources`,
`datasource-relationships`, `thumbnails`, `actions`, root `<style>`,
worksheet `<layout-options>`, `metadata-records`, the `_.fcp…relation`
wrappers (a plain `<relation>` works), and `preferences` content.

Several intermediate "OPENED: Tableau - Book1/Book2" results were **false
positives**: Tableau had failed silently and opened a blank book. Only a window
title equal to the file name plus `rk=ok` in the log counts as a pass.

Final result: both generated workbooks open with `load-workbook rk=ok`
(Retail: 14 sheets / 3 dashboards; Regional: 47 sheets / 9 dashboards).
## 8. Data snapshots — evaluating each visual in Power BI's own engine

The default workbook has empty tables (the rows are locked in the ABF blob).
With the report open in Power BI Desktop, `src/extractors/data_extract.py`
builds one DAX query per visual and `model_reader.run_queries` runs it:

```dax
EVALUATE SUMMARIZECOLUMNS('Time'[FiscalMonth], 'District'[DM],
    TREATAS({"Same Store"}, 'Store'[Store Type]),
    "Sales_Total Sales Variance", [Total Sales Variance])
```

Verified against Regional Sales open in Desktop 2.149 (44 of 47 sheets
snapshotted, 4,968 rows):

- `SUMMARIZECOLUMNS` headers come back as `Table[Column]` for grouped columns
  and `[alias]` for named expressions; PowerShell 5.1 serialises dates as
  `/Date(ms)/` unless converted first.
- **Hierarchy levels** are stored as `{"HierarchyLevel": {"Expression": {"Hierarchy":
  …}, "Level": "State or Province"}}` and query as the level's column.
- **Name-only fields are ambiguous**: `Opportunities.Revenue Won` looks like a
  column but is a measure. The pipeline re-classifies such fields against the
  live model before building queries (`reconcile_fields`).
- Some visuals reference columns that no longer exist in the model
  (`Products[Product LOB]`, `Owners[Owner Goal]`); the engine rejects them and
  the visual falls back to the stub datasource - it errors in Power BI as well.
- **Time-relative filters age**: Regional's Sales Overview page filters on
  `RELATIVE MONTH >= -6`, but the column ranges −92…−45 today (data ends 2022),
  so 29 visuals are empty in Power BI Desktop itself. Snapshots reproduce that
  faithfully; the `snapshot_filters=none|visual|all` option exists for demos.
- `TREATAS({}, col)` (an `In` filter with no values) is an engine error; such
  filters are skipped with a note.
- With two Desktop windows open, `find_port_for` picks the instance whose
  `TMSCHEMA_TABLES` overlap the report's tables (an "Untitled" window has none).

## 9. Visual types a 2026 report actually uses

A real customer report (healthcare, 4 pages, 88 containers) exposed three
`visualType` values that were not in the map, so 22 of its 52 data visuals were
wrongly reported as *unsupported*:

| `visualType` | Neutral type | Tableau |
|---|---|---|
| `cardVisual` | `CARD` | the newer card visual (2023+), distinct from `card` |
| `treemap` | `TREEMAP` | **direct**: Square mark, dimension on Detail, measure on Size |
| `waterfallChart` | `WATERFALL` | **manual**: Tableau needs Gantt bars + `RUNNING_SUM` |

Two projection roles were also unmapped and fell through to `Role.OTHER`:
`Data` (the value slot of `cardVisual`) and `Group` (its breakdown).

After mapping them, the same report went from **14 worksheets / 22 unsupported**
to **35 worksheets / 0 unsupported**, 98.1% migratable. The lesson: the map is
open-ended by design, and an unknown type is reported as `UNKNOWN` → unsupported
rather than guessed - which is what made the gap visible in the first place.

## 10. Packaged workbooks (.twbx) and hidden sheets

Downloading only the `.twb` gives Tableau *"File: data\model_stub.csv not found"* —
the workbook's connections are relative and the data folder is not beside it. The
cure is a `.twbx`: a ZIP holding `<name>.twb` at the root plus its data under the
folder the connections name (`data/`).

Getting it to load took another bisect (variants A-Q, each opened in Tableau and
judged by `load-workbook … "rk":"ok"`):

| Variant | Result |
|---|---|
| `data/` vs `Data/` vs `Data/<name>/` | **all three fail** - the folder name is not the issue |
| `<columns character-set…>` schema block on `<relation>` | still fails - not required |
| stub datasource alone, packaged | **ok** |
| stub + 1 snapshot datasource | fails |
| same file with `hidden="true"` removed from every window | **ok** |
| hidden sheets + hand-written dashboard windows (minimal or Superstore-shaped) | fails, in `.twb` too |
| hidden sheets with **one** worksheet window left visible | **ok** |

So the rule is: **a workbook must keep at least one visible window.** We hide every
worksheet that sits on a dashboard - which is what stops 35 building-block tabs
crowding the 4 page tabs - but always leave the first one visible. Dashboard windows
stay un-emitted (finding §7); Tableau recreates them.

### 10.1 Snapshot values decide column types

A Power BI `cardVisual` often holds a **pre-formatted string**: the snapshot for one
card was the single value `₹470.75 Cr`. Declaring it `datatype="real"` and wrapping it
in `SUM()` produced *"The field Total Revenue does not exist in the database."*

`FieldRegistry.retype_from_rows` now reads each snapshot column's actual values: all
numeric → `real`/measure, otherwise `string`/dimension, and `instance()` honours the
result so text is never aggregated. On the same report this re-typed 6 columns.
Cards place every value field on Text, numeric or not.

### 10.2 Correction: dashboard windows are fine - if they carry viewpoints

Section 7 concluded that hand-written `<window class="dashboard">` elements always
fail. That was **incomplete**. They fail when `<viewpoints>` is empty; they load
cleanly when the window carries one `<viewpoint name="<sheet>"/>` for every sheet
on that dashboard:

```xml
<window class="dashboard" maximized="true" name="Executive">
  <viewpoints>
    <viewpoint name="Executive - Monthly Revenue Trend" />
    <viewpoint name="Executive - Revenue by ConsultationType" />
  </viewpoints>
  <active id="-1" />
  <simple-id uuid="{...}" />
</window>
```

That is what lets every worksheet be hidden: the workbook still has visible windows
(the dashboards), so it loads, and the tab bar shows **only the pages** - 4 tabs for
a 4-page report instead of 39. Verified on both `.twb` and `.twbx`.

### 10.3 Declare the CSV schema or Tableau guesses the delimiter

A snapshot of a card is a one-column CSV:

```
_Measures_Total Revenue Display
Rs 470.75 Cr
```

There is no comma anywhere in the file, so Tableau's textscan connector guesses -
and picks the space, splitting that header into three phantom fields
(`_Measures_Total`, `Revenue`, `Display`) and the value into two. The sheet then
reports *"The field Total Revenue does not exist in the database."*

The cure is to declare the schema on the relation, which is what Tableau itself
writes:

```xml
<relation connection="textscan.v1" name="v1_....csv" table="[v1_....csv]" type="table">
  <columns character-set="UTF-8" header="yes" locale="en_US" separator=",">
    <column datatype="string" name="_Measures_Total Revenue Display" ordinal="0" />
  </columns>
</relation>
```

In one real report **20 of 33 snapshot CSVs had no comma at all**, so this affected
every card on every page.


## 11. Presentation data: what the report really carries

The first generated workbooks were structurally correct but looked nothing like the
originals. Rather than style them to taste, we probed the three sample files for
presentation data that genuinely exists.

### 11.1 What is there, and what is not

| Thing | Where it lives | Private report | Retail | Regional |
|---|---|---|---|---|
| Sort order | `query.Commands[].SemanticQueryDataShapeCommand.Query.OrderBy` | 12 | 9 | 42 |
| Number format | `dataTransforms.selects[].format` | 5 distinct | 4 | - |
| Data labels on/off | `config.singleVisual.objects.labels[].properties.show` | 10 | 7 | 12 |
| Page headings | `objects.general[].properties.paragraphs[].textRuns[]` | 8 | 7 | 25 |
| Theme palette | see 11.3 | 480 colours | **none** | 480 colours |
| Per-visual colour | `objects.dataPoint` | **0** | **0** | **0** |

The last row is the important negative: **no visual in any of the three files
overrides its colour**. Any per-visual palette would therefore be our invention, so
we do not emit one.

### 11.2 OrderBy

```json
{"Direction": 2,
 "Expression": {"Measure": {"Expression": {"SourceRef": {"Source": "_"}},
                            "Property": "Total Revenue"}}}
```

`Direction` is 1 = ascending, 2 = descending. This is what makes "Top 10 Clinics by
Revenue" actually ranked; without it Tableau sorts alphabetically.

Two cautions found in the real files: some `OrderBy` expressions resolve to a table
we cannot name (`'?'[Year]`, from a date hierarchy) and some to a literal
(`Opportunities.Blank`). A `<computed-sort>` pointing at a column the worksheet never
declares breaks that sheet, so a sort is emitted **only** when both its `column` and
its `using` resolve to column-instances the worksheet already declares.

### 11.3 The theme is named in one place and stored in another

`Report/Layout`'s `config` names a theme collection:

```json
{"baseTheme":   {"name": "CY25SU11"},
 "customTheme": {"name": "CustomTheme1234567890123456.json"}}
```

- a **custom** theme is embedded at `Report/StaticResources/RegisteredResources/<name>`
- a **base** theme is embedded at `Report/StaticResources/SharedResources/BaseThemes/<name>.json`
- a **built-in** theme is *not embedded at all* - only its name is stored

The Retail sample names `"Concourse"` and ships no theme file, so that report has no
palette to migrate and keeps Tableau's defaults. The private report's theme is real brand
data: `#1f3a5f`, `#2ca58d`, `#f4a261`, ...

### 11.4 The Tableau side (all verified in Superstore.twb)

| Intent | Construct |
|---|---|
| Number format | `default-format` attribute on `<column>`: `c"$"#,##0`, `p0.00%`, `n#,##0.0` |
| Sort | `<computed-sort column="..." direction="DESC" using="..."/>`, a child of `<view>` |
| Fit to zone | `<zoom type="entire-view"/>`, the first child of `<viewpoint>` |
| Solid mark colour | `<style-rule element="mark"><format attr="mark-color" value="#rrggbb"/>` inside `table/style` |
| Colour per member | `<style-rule element="mark"><encoding attr="color" field="..." type="palette"><map to="#rrggbb"><bucket>"North"</bucket>` |
| Mark labels | `<format attr="mark-labels-show" value="true"/>` (+ `mark-labels-cull`) |
| Heading | `<zone type-v2="text"><formatted-text><run bold="true" fontcolor="#fff" fontsize="16">` |
| Zone padding / backdrop | `<zone-style><format attr="margin"/>`, `background-color`, `padding` |

### 11.5 Format-string translation

Power BI writes .NET custom numeric formats in up to three `positive;negative;zero`
sections. Only the positive section is mapped; Tableau derives the rest.

| Power BI | Tableau |
|---|---|
| `\$#,0;(\$#,0);\$#,0` | `c"$"#,##0` |
| `"Rs" #,0.00;...` | `c"Rs"#,##0.00` |
| `0.00 %;-0.00 %;0.00 %` | `p0.00%` |
| `#,0` | `n#,##0` |
| `G`, `D`, `dd/mm/yyyy` | *(none - leave Tableau's default)* |

`#` is an **optional** digit and `0` a **forced** one, so `#,0.###############` must
not collapse into fifteen forced zeros - an early version did exactly that.

### 11.6 White text on a white dashboard

Power BI page headings are often white (`#ffffff`) because a coloured banner *shape*
sits behind them. We do not carry shapes, so that text would land invisibly on
Tableau's white dashboard. When a heading's text is light we give its zone a
`background-color` taken from the report's own theme; with no theme available we drop
the colour and let Tableau use its readable default.
