/* BI Migration Assistant - front-end. Vanilla JS, no build step.
   Talks only to /api/*; every number shown comes from the pipeline result. */
(function () {
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const html = document.documentElement;
  const VERDICTS = ['direct', 'approximate', 'manual', 'unsupported', 'decorative'];
  const MEANING = {
    direct: 'same mark and shelves', approximate: 'buildable, looks different',
    manual: 'rebuild by hand', unsupported: 'no Tableau equivalent', decorative: 'layout chrome',
  };
  const state = { queue: [], modelMode: 'none', desktop: false, snapFilters: 'all', current: null, busy: false, seq: 0 };

  /* ---------- theme ---------- */
  const store = { get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } }, set(k, v) { try { localStorage.setItem(k, v); } catch {} } };
  function applyTheme() {
    $$('[data-theme-pick]').forEach(b => b.classList.toggle('on', b.dataset.themePick === html.dataset.theme));
    Backgrounds.set(html.dataset.theme, html.dataset.mode);
  }
  function initTheme() {
    const sysDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
    html.dataset.theme = store.get('theme', 'ocean');
    html.dataset.mode = store.get('mode', sysDark ? 'dark' : 'light');
    const reduced = store.get('motion', window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'reduce' : 'full') === 'reduce';
    $('#motionBtn').setAttribute('aria-pressed', String(reduced));
    applyTheme(); Backgrounds.reduce(reduced);
    $$('[data-theme-pick]').forEach(b => b.addEventListener('click', () => { html.dataset.theme = b.dataset.themePick; store.set('theme', html.dataset.theme); applyTheme(); }));
    $('#modeBtn').addEventListener('click', () => { html.dataset.mode = html.dataset.mode === 'dark' ? 'light' : 'dark'; store.set('mode', html.dataset.mode); applyTheme(); });
    $('#motionBtn').addEventListener('click', e => {
      const on = e.currentTarget.getAttribute('aria-pressed') !== 'true';
      e.currentTarget.setAttribute('aria-pressed', String(on)); store.set('motion', on ? 'reduce' : 'full'); Backgrounds.reduce(on);
    });
  }

  /* ---------- helpers ---------- */
  const fmtBytes = n => n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`;
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  let toastT;
  function toast(msg, err = false) {
    const t = $('#toast'); t.textContent = msg; t.classList.toggle('err', err); t.hidden = false;
    clearTimeout(toastT); toastT = setTimeout(() => t.hidden = true, err ? 8000 : 3800);
  }
  async function api(path, opts) {
    let r;
    try { r = await fetch(path, opts); }
    catch {
      throw new Error(`Cannot reach the app server at ${location.origin}. Is the run_app.bat window still open? Start it again and reload this page.`);
    }
    if (!r.ok) { let m = r.statusText; try { m = (await r.json()).detail || m; } catch {} throw new Error(m); }
    return r.status === 204 ? null : r.json();
  }

  /* ---------- health & inputs ---------- */
  async function loadHealth() {
    const req = $('#desktopReq'), txt = $('#desktopText');
    try {
      const h = await api('/api/health');
      const ports = h.desktop_ports || [], on = ports.length > 0;
      state.desktop = on;
      req.className = 'req ' + (on ? 'ok' : 'off') + (shouldShowReq() ? ' show' : '');
      txt.textContent = on
        ? `Power BI Desktop is open — snapshots and live measures are available`
        : `Power BI Desktop is not open — open this report there first, or leave these off`;
      $('#liveBtn').title = on ? 'Read measures from the open report' : 'Open the .pbix in Power BI Desktop first';
      $('#footEnv').innerHTML = `<b>v${esc(h.version)}</b> · Python ${esc(h.python)}`
        + (h.tableau_installed ? ' · Tableau Desktop detected' : '');
    } catch {
      state.desktop = false;
      req.className = 'req off show'; txt.textContent = 'Cannot reach the local server — is the run_app.bat window still open?';
    }
  }
  function shouldShowReq() { return $('#dataLive').checked || state.modelMode === 'live'; }
  function syncReq() {
    const req = $('#desktopReq');
    req.classList.toggle('show', shouldShowReq());
  }
  async function loadInputs() {
    const sel = $('#inputSelect');
    try {
      const items = await api('/api/inputs');
      sel.innerHTML = '<option value="">— add a file to the queue —</option>'
        + items.map(i => `<option value="${esc(i.rel)}" data-size="${i.size}">${esc(i.name)}  (${fmtBytes(i.size)}${i.uploaded ? ', uploaded' : ''})</option>`).join('');
    } catch { sel.innerHTML = '<option value="">could not list input/</option>'; }
  }

  /* ---------- the upload queue ---------- */
  function addFiles(files) {
    let added = 0, rejected = 0;
    for (const f of files) {
      if (!/\.pbix$/i.test(f.name)) { rejected++; continue; }
      if (state.queue.some(q => q.file && q.file.name === f.name && q.file.size === f.size)) continue;
      state.queue.push({ id: ++state.seq, file: f, name: f.name, size: f.size, status: 'queued' });
      added++;
    }
    renderQueue();
    if (rejected) toast(`${rejected} file(s) ignored - only .pbix is accepted.`, true);
    return added;
  }
  function addSample(rel, name, size) {
    if (state.queue.some(q => q.rel === rel)) { toast(`${name} is already in the queue.`); return; }
    state.queue.push({ id: ++state.seq, rel, name, size, status: 'queued' });
    renderQueue();
  }
  function removeFromQueue(id) {
    const row = $(`.qrow[data-id="${id}"]`);
    const drop = () => { state.queue = state.queue.filter(q => q.id !== id); renderQueue(); };
    if (row) { row.classList.add('leaving'); setTimeout(drop, 300); } else drop();
  }
  function clearQueue() {
    const rows = $$('.qrow');
    if (!rows.length) return;
    rows.forEach((r, i) => setTimeout(() => r.classList.add('leaving'), i * 45));
    setTimeout(() => { state.queue = []; renderQueue(); }, rows.length * 45 + 320);
  }
  const STATUS_TEXT = { queued: 'waiting', running: 'analysing…', done: 'done', error: 'failed' };
  function renderQueue() {
    const q = state.queue, host = $('#queue');
    $('#queueHead').hidden = q.length === 0;
    $('#queueCount').textContent = q.length === 1 ? '1 file' : `${q.length} files`;
    const done = q.filter(x => x.status === 'done').length;
    if (done && q.length) $('#queueCount').textContent += ` · ${done} of ${q.length} done`;
    host.innerHTML = q.map(x => {
      const badge = x.status === 'done'
        ? '<svg viewBox="0 0 24 24" width="17" height="17"><path class="tick" d="M5 12.5l4.5 4.5L19 7.5" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/></svg>'
        : x.status === 'error' ? '!' : 'PBIX';
      const meta = x.status === 'done' && x.result
        ? `${x.result.data_visuals} data visuals · ${x.result.migratable_pct}% generated`
        : x.status === 'error' ? esc(x.error || 'failed')
        : `${fmtBytes(x.size || 0)} · ${STATUS_TEXT[x.status]}`;
      return `<li class="qrow" data-id="${x.id}" data-status="${x.status}" style="--p:${x.status === 'done' ? 100 : x.status === 'running' ? 60 : 0}%">
        <span class="qicon">${badge}</span>
        <span class="qmain"><span class="qname">${esc(x.name)}</span><span class="qmeta">${meta}</span></span>
        ${x.status === 'running' ? '' : `<button class="qx" data-remove="${x.id}" aria-label="Remove ${esc(x.name)}">×</button>`}
      </li>`;
    }).join('');
    $$('[data-remove]', host).forEach(b => b.addEventListener('click', e => { e.stopPropagation(); removeFromQueue(+b.dataset.remove); }));
    $$('.qrow[data-status="done"]', host).forEach(r => r.addEventListener('click', () => {
      const item = state.queue.find(q => q.id === +r.dataset.id);
      if (item && item.result) { show(item.result); $('#results').scrollIntoView({ behavior: 'smooth' }); }
    }));
  }

  /* ---------- form ---------- */
  function initForm() {
    const drop = $('#drop'), fi = $('#fileInput');
    drop.addEventListener('click', () => fi.click());
    drop.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fi.click(); } });
    fi.addEventListener('change', () => { addFiles(fi.files); fi.value = ''; });
    ['dragenter', 'dragover'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add('over'); }));
    ['dragleave', 'drop'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove('over'); }));
    drop.addEventListener('drop', e => {
      const n = addFiles(e.dataTransfer.files);
      if (n > 1) toast(`${n} reports queued - they will run one after another.`);
    });
    $('#clearBtn').addEventListener('click', clearQueue);
    $('#inputSelect').addEventListener('change', e => {
      const o = e.target.selectedOptions[0];
      if (e.target.value) addSample(e.target.value, o.textContent.split('  (')[0], +o.dataset.size || 0);
      e.target.value = '';
    });

    const hints = {
      none: 'Report structure only. Measures become labelled placeholders in Tableau.',
      live: 'Reads measures, calculated columns and relationships from the Analysis Services instance that Power BI Desktop runs for the OPEN report - with full DAX.',
      dir: 'Reads a saved folder of TMSCHEMA_*.json DMV exports (the repo ships one under tests/fixtures/dmv).',
    };
    $$('[data-model]').forEach(b => b.addEventListener('click', () => {
      $$('[data-model]').forEach(x => x.classList.toggle('on', x === b));
      state.modelMode = b.dataset.model; $('#modelHint').textContent = hints[state.modelMode];
      $('#modelDir').hidden = state.modelMode !== 'dir';
      syncReq();
    }));
    $('#dataLive').addEventListener('change', e => { $('#snapFilters').hidden = !e.target.checked; syncReq(); });
    $$('[data-snap]').forEach(b => b.addEventListener('click', () => {
      $$('[data-snap]').forEach(x => x.classList.toggle('on', x === b)); state.snapFilters = b.dataset.snap;
    }));
    $('#runBtn').addEventListener('click', runAll);
    $('#refreshRuns').addEventListener('click', loadRuns);
    $$('.tab').forEach(t => t.addEventListener('click', () => {
      $$('.tab').forEach(x => x.classList.toggle('on', x === t));
      $$('.panel').forEach(p => p.hidden = p.dataset.panel !== t.dataset.tab);
    }));
    $('#visualSearch').addEventListener('input', () => renderVisuals());
    $('#hideDeco').addEventListener('change', () => renderVisuals());
  }

  /* ---------- running the queue ---------- */
  function setStep(i, done = false) {
    $$('#steps li').forEach((li, n) => { li.classList.toggle('done', n < i || done); li.classList.toggle('active', n === i && !done); });
  }
  function analyseOne(item) {
    const fd = new FormData();
    if (item.file) fd.append('file', item.file); else fd.append('input_rel', item.rel);
    fd.append('model_mode', state.modelMode);
    if (state.modelMode === 'dir') fd.append('model_dir', $('#modelDir').value.trim());
    fd.append('generate', $('#genTwb').checked ? 'true' : 'false');
    fd.append('data_mode', $('#dataLive').checked ? 'live' : 'none');
    fd.append('snapshot_filters', state.snapFilters);
    return api('/api/analyze', { method: 'POST', body: fd });
  }
  async function runAll() {
    if (state.busy) return;
    const pending = state.queue.filter(q => q.status === 'queued' || q.status === 'error');
    if (!pending.length) { toast('Add at least one .pbix to the queue first.', true); return; }

    state.busy = true;
    const btn = $('#runBtn'); btn.disabled = true; btn.classList.add('busy');
    const label = btn.querySelector('.btn-label'), fill = btn.querySelector('.fill');
    let finished = 0, failed = 0, last = null;

    for (const item of pending) {
      item.status = 'running'; renderQueue();
      label.textContent = pending.length > 1 ? `Analysing ${finished + 1} of ${pending.length}…` : 'Working…';
      let step = 0; setStep(0);
      const ticker = setInterval(() => { if (step < 3) setStep(++step); }, 550);
      try {
        item.result = await analyseOne(item);
        item.status = 'done'; last = item.result;
      } catch (e) {
        item.status = 'error'; item.error = e.message; failed++;
      }
      clearInterval(ticker); setStep(4, true);
      finished++;
      fill.style.setProperty('--p', `${Math.round(100 * finished / pending.length)}%`);
      renderQueue();
      if (pending.length > 1) await sleep(120);
    }

    if (last) { show(last); $('#results').scrollIntoView({ behavior: 'smooth', block: 'start' }); }
    loadRuns(); loadInputs();
    const ok = finished - failed;
    toast(failed ? `${ok} of ${finished} reports analysed - ${failed} failed (see the queue).`
                 : `${ok} report${ok === 1 ? '' : 's'} analysed.`, failed > 0);

    state.busy = false; btn.disabled = false; btn.classList.remove('busy');
    label.textContent = 'Run migration analysis';
    setTimeout(() => fill.style.setProperty('--p', '0%'), 700);
  }

  /* ---------- previous runs ---------- */
  async function loadRuns() {
    const ul = $('#runList');
    try {
      const runs = await api('/api/runs');
      if (!runs.length) { ul.innerHTML = '<li class="muted">No runs yet.</li>'; return; }
      ul.innerHTML = runs.map(r => `
        <li data-run="${esc(r.run_id)}" class="${state.current && state.current.run_id === r.run_id ? 'current' : ''}">
          <div class="rn"><span>${esc(r.report)}</span><span class="pct">${r.migratable_pct ?? 0}%</span></div>
          <div class="rm"><span>${r.pages} pages</span><span>${r.data_visuals} data visuals</span><span>${r.model_available ? 'model ✓' : 'no model'}</span><span>${esc((r.modified || '').replace('T', ' '))}</span></div>
          <button class="rdel" data-del="${esc(r.run_id)}" title="Delete this run" aria-label="Delete ${esc(r.report)}">
            <svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true">
              <path class="lid" d="M5 7h14M9.5 7V4.5h5V7" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
              <path d="M6.5 7l1 12.5h9L17.5 7M10 10.5v6M14 10.5v6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
          </button>
        </li>`).join('');
      $$('li[data-run]', ul).forEach(li => li.addEventListener('click', async e => {
        if (e.target.closest('[data-del]')) return;
        try { show(await api(`/api/runs/${encodeURIComponent(li.dataset.run)}`)); loadRuns(); $('#results').scrollIntoView({ behavior: 'smooth' }); }
        catch (err) { toast(err.message, true); }
      }));
      $$('[data-del]', ul).forEach(btn => btn.addEventListener('click', e => { e.stopPropagation(); armDelete(btn); }));
    } catch { ul.innerHTML = '<li class="muted">Could not load runs.</li>'; }
  }
  let armedTimer;
  function armDelete(btn) {
    if (!btn.classList.contains('armed')) {           // first click arms, second confirms
      $$('.rdel.armed').forEach(b => disarm(b));
      btn.classList.add('armed'); btn.innerHTML = 'Delete?';
      clearTimeout(armedTimer); armedTimer = setTimeout(() => disarm(btn), 3500);
      return;
    }
    clearTimeout(armedTimer);
    const li = btn.closest('li'), id = btn.dataset.del;
    li.classList.add('deleting');
    api(`/api/runs/${encodeURIComponent(id)}`, { method: 'DELETE' })
      .then(() => {
        if (state.current && state.current.run_id === id) { $('#results').hidden = true; state.current = null; }
        setTimeout(loadRuns, 420); toast(`Deleted "${id}".`);
      })
      .catch(err => { li.classList.remove('deleting'); toast(err.message, true); });
  }
  function disarm(btn) {
    btn.classList.remove('armed');
    btn.innerHTML = '<svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true">'
      + '<path class="lid" d="M5 7h14M9.5 7V4.5h5V7" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>'
      + '<path d="M6.5 7l1 12.5h9L17.5 7M10 10.5v6M14 10.5v6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  }

  /* ---------- results ---------- */
  function show(r) {
    state.current = r;
    $('#results').hidden = false;
    $('#resTitle').textContent = r.report;
    $('#resMeta').textContent = `${r.source_path} · ${(r.elapsed_ms / 1000).toFixed(1)}s · output/${r.run_id}`;
    $('#tPages').textContent = r.pages; $('#tContainers').textContent = r.containers; $('#tData').textContent = r.data_visuals;
    $('#tPct').textContent = `${r.migratable_pct}%`;
    const wb = r.workbook;
    $('#tSheets').textContent = wb ? `${wb.worksheets.length} / ${wb.dashboards.length}` : '—';
    $('#tSheets').closest('.tile').title = wb
      ? `Tableau puts one chart per worksheet. ${(wb.hidden_sheets || []).length} of them are hidden behind the ${wb.dashboards.length} dashboard tabs, which are your Power BI pages.`
      : '';
    $('#tMeasures').textContent = r.model.available ? `${r.model.measures_translated} / ${r.model.measures}` : '—';

    const data = VERDICTS.filter(v => v !== 'decorative').map(v => [v, r.verdicts[v] || 0]);
    const total = data.reduce((a, [, n]) => a + n, 0) || 1; let acc = 0;
    const stops = data.map(([v, n]) => { const from = acc / total * 100; acc += n; return `var(--v-${v}) ${from}% ${acc / total * 100}%`; });
    $('#donut').style.background = `conic-gradient(${stops.join(',')})`;
    $('#donutN').textContent = r.data_visuals;
    $('#legend').innerHTML = VERDICTS.map(v => `<li style="--c:var(--v-${v})"><i></i>${v} <small>· ${MEANING[v]}</small><b>${r.verdicts[v] || 0}</b></li>`).join('');

    const mn = $('#modelNote');
    if (r.model.available) {
      mn.className = 'model-note on';
      mn.innerHTML = `<b>Model layer available</b> - ${r.model.tables} tables, ${r.model.columns} columns, ${r.model.measures} measures, ${r.model.relationships} relationships from <i>${esc(r.model.source)}</i>. ${r.model.measures_translated} measure(s) translated to Tableau calculations; the rest are placeholders with the refusal reason shown under Measures.`;
    } else {
      mn.className = 'model-note';
      mn.innerHTML = `<b>Model layer not requested.</b> Measures used by visuals (${r.measures_referenced.length}) are emitted as labelled placeholder calculations. Open the .pbix in Power BI Desktop and re-run with <b>Power BI Desktop (live)</b> to translate them.`;
    }
    const d = r.data || { mode: 'none', sheets: 0, rows: 0, problems: [] };
    if (d.mode === 'live') {
      const bad = d.problems.filter(p => p.kind === 'query'), empty = d.problems.filter(p => p.kind === 'empty');
      mn.innerHTML += `<div style="margin-top:10px"><b>Data snapshots</b> (filters: ${esc(d.filters || 'all')}): ${d.sheets} sheet(s) carry ${d.rows} row(s) evaluated by Power BI itself, so they draw with real numbers in Tableau.${empty.length ? ` <b>${empty.length}</b> sheet(s) came back with no rows - Power BI shows them empty too under the current filters (details in Skipped).` : ''}${bad.length ? ` <b>${bad.length}</b> visual(s) fell back to the empty stub because the engine rejected their query - see the Skipped tab.` : ''}</div>`;
    } else {
      mn.innerHTML += `<div style="margin-top:10px" class="muted">No data snapshots: the workbook has the right shelves but empty tables (the rows are locked inside the .pbix). Tick <b>Include data snapshots</b> with the report open in Power BI Desktop to ship real rows.</div>`;
    }

    const dl = $('#downloads'), base = `/api/runs/${encodeURIComponent(r.run_id)}`;
    const links = [];
    if (r.workbook && r.workbook.twbx) {
      // .twbx carries the workbook AND its data in one file - the only download that just works
      links.push([`${base}/files/${encodeURIComponent(r.workbook.twbx)}`, `⬇ ${r.workbook.twbx}`, 'main']);
    }
    if (r.workbook) links.push([`${base}/files/${encodeURIComponent(r.workbook.twb)}`, `${r.workbook.twb} (needs the data folder)`, '']);
    links.push([`${base}/bundle`, 'Everything (.zip)', '']);
    links.push([`${base}/files/assessment.md`, 'assessment.md', '']);
    dl.innerHTML = links.map(([href, text, cls]) =>
      `<a class="dl ${cls}" href="${href}" download><span class="dlfill"></span><span>${esc(text)}</span></a>`).join('');
    $$('.dl', dl).forEach(a => a.addEventListener('click', () => {
      a.classList.remove('done'); a.classList.add('busy');
      setTimeout(() => { a.classList.remove('busy'); a.classList.add('done'); }, 1150);
    }));

    renderVisuals(); renderMeasures(); renderFilters(); renderSkipped(); renderFiles();
  }

  const fieldChip = (role, f) => `<span class="fld ${f.kind === 'column' ? '' : 'm'}"><span class="r">${esc(role)}</span>${esc(f.label)}</span>`;
  function renderVisuals() {
    const r = state.current; if (!r) return;
    const q = $('#visualSearch').value.trim().toLowerCase(), hide = $('#hideDeco').checked;
    const out = [];
    for (const p of r.pages_detail) {
      const vis = p.visuals.filter(v => !(hide && v.verdict === 'decorative'))
        .filter(v => !q || [v.title, v.source_type, v.chart_type, v.verdict, ...Object.values(v.roles).flat().map(f => f.label)].join(' ').toLowerCase().includes(q));
      out.push(`<div class="pg"><div class="pg-head"><h3>${esc(p.name)}</h3><span class="muted">${p.data_visuals} data · ${p.decorative} decorative · ${p.width}×${p.height}</span></div>`);
      if (!vis.length) { out.push('<div class="empty">Nothing to show on this page with the current filter.</div></div>'); continue; }
      out.push(`<table><thead><tr><th>Visual</th><th>Type</th><th>Verdict</th><th>Mark</th><th>Fields</th></tr></thead><tbody>`);
      for (const v of vis) {
        const fields = Object.entries(v.roles).flatMap(([role, fs]) => fs.map(f => fieldChip(role, f))).join('') || '<span class="muted">—</span>';
        out.push(`<tr class="row" data-v="${esc(v.id)}"><td><b>${esc(v.title || v.id)}</b>${v.sheet ? `<div class="mark">→ ${esc(v.sheet)}${v.snapshot_rows != null ? ` · ${v.snapshot_rows} rows` : ''}</div>` : ''}</td><td>${esc(v.source_type)}</td><td><span class="badge" style="--c:var(--v-${v.verdict})">${v.verdict}</span></td><td class="mark">${esc(v.mark || '—')}</td><td><div class="fields">${fields}</div></td></tr>`);
        const filters = v.filters.length ? v.filters.map(f => `<li><code>${esc(f.field || '?')}</code> ${esc(f.condition || 'no value applied')}</li>`).join('') : '<li class="muted">none</li>';
        out.push(`<tr class="detail" hidden data-for="${esc(v.id)}"><td colspan="5">
          <ul class="reasons">${v.reasons.map(x => `<li>${esc(x)}</li>`).join('')}${v.notes.map(x => `<li class="muted">${esc(x)}</li>`).join('')}</ul>
          <dl class="kv"><dt>Neutral type</dt><dd>${esc(v.chart_type)}</dd><dt>Position</dt><dd>x ${Math.round(v.placement.x)} · y ${Math.round(v.placement.y)} · ${Math.round(v.placement.w)}×${Math.round(v.placement.h)}</dd><dt>Source roles</dt><dd>${Object.keys(v.source_roles).map(esc).join(', ') || '—'}</dd><dt>Query</dt><dd>${v.has_query ? 'stored in the file' : 'built at runtime by Power BI'}</dd><dt>Visual filters</dt><dd><ul class="reasons">${filters}</ul></dd></dl></td></tr>`);
      }
      out.push('</tbody></table></div>');
    }
    const host = $('#visualPages'); host.innerHTML = out.join('');
    $$('tr.row', host).forEach(tr => tr.addEventListener('click', () => {
      const d = $(`tr.detail[data-for="${CSS.escape(tr.dataset.v)}"]`, host); d.hidden = !d.hidden;
    }));
  }
  function renderMeasures() {
    const r = state.current, host = $('#measures');
    if (!r.model.available) {
      host.innerHTML = `<div class="empty">No model attached to this run. Measures referenced by visuals:</div><div class="fields" style="padding:0 4px 8px">${r.measures_referenced.map(m => `<span class="fld m">${esc(m)}</span>`).join('') || '<span class="muted">none</span>'}</div>`;
      return;
    }
    host.innerHTML = r.model.items.map(m => `<div class="mrow"><div><div class="mn">${esc(m.name)}</div><div class="mt">${esc(m.table)}${m.format ? ` · <code>${esc(m.format)}</code>` : ''}</div><div style="margin-top:8px"><span class="badge soft" style="--c:var(--v-${m.tableau ? 'direct' : 'unsupported'})">${m.tableau ? 'translated' : 'refused'}</span></div></div>
      <div><div class="lbl">DAX</div><pre>${esc(m.dax)}</pre>${m.tableau ? `<div class="lbl">Tableau</div><pre class="ok">${esc(m.tableau)}</pre>` : `<div class="lbl">Why not</div><pre class="no">${esc(m.error)}</pre>`}</div></div>`).join('');
  }
  function renderFilters() {
    const r = state.current, host = $('#filters');
    const rows = [...r.report_filters.map(f => ({ ...f, where: 'report' })), ...r.pages_detail.flatMap(p => p.filters.map(f => ({ ...f, where: p.name })))];
    host.innerHTML = rows.length
      ? `<table><thead><tr><th>Scope</th><th>Field</th><th>Type</th><th>Condition</th></tr></thead><tbody>${rows.map(f => `<tr><td>${esc(f.where)}</td><td><code>${esc(f.field || '?')}</code></td><td class="mark">${esc(f.type || '')}</td><td>${esc(f.condition || 'no value applied')}</td></tr>`).join('')}</tbody></table>`
      : '<div class="empty">No report- or page-level filters.</div>';
  }
  function renderSkipped() {
    const r = state.current, host = $('#skipped');
    const s = r.workbook ? r.workbook.skipped : [];
    const probs = (r.data && r.data.problems) || [];
    let out = s.length
      ? `<table><thead><tr><th>Page</th><th>Visual</th><th>Why it has no worksheet</th></tr></thead><tbody>${s.map(x => `<tr><td>${esc(x.page)}</td><td>${esc(x.visual)}</td><td>${esc(x.reason)}</td></tr>`).join('')}</tbody></table>`
      : `<div class="empty">${r.workbook ? 'Every data visual became a worksheet.' : 'Workbook generation was switched off for this run.'}</div>`;
    if (probs.length) out += `<h3 style="margin:18px 0 8px;font-size:15px">Snapshot problems</h3><table><thead><tr><th>Page</th><th>Visual</th><th>What happened</th></tr></thead><tbody>${probs.map(p => `<tr><td>${esc(p.page)}</td><td>${esc(p.visual)}</td><td>${esc(p.error)}${p.dax ? `<pre style="margin:6px 0 0;font-size:12px;white-space:pre-wrap">${esc(p.dax)}</pre>` : ''}</td></tr>`).join('')}</tbody></table>`;
    host.innerHTML = out;
  }
  function renderFiles() {
    const r = state.current, base = `/api/runs/${encodeURIComponent(r.run_id)}`;
    $('#files').innerHTML = r.files.map(f => `<div class="file"><code>${esc(f.name)}</code><span><span class="muted" style="margin-right:12px">${fmtBytes(f.size)}</span><a class="dl" href="${base}/files/${f.name.split('/').map(encodeURIComponent).join('/')}" download><span class="dlfill"></span><span>download</span></a></span></div>`).join('');
    $$('#files .dl').forEach(a => a.addEventListener('click', () => {
      a.classList.add('busy'); setTimeout(() => { a.classList.remove('busy'); a.classList.add('done'); }, 1150);
    }));
  }

  /* ---------- one ripple for every button on the page ---------- */
  function initRipples() {
    const reduced = () => $('#motionBtn').getAttribute('aria-pressed') === 'true';
    document.addEventListener('pointerdown', e => {
      if (reduced()) return;
      const b = e.target.closest('button, .dl');
      if (!b || b.disabled) return;
      const r = b.getBoundingClientRect();
      const size = Math.max(r.width, r.height) * 2.2;
      const s = document.createElement('span');
      s.className = 'ripple';
      s.style.width = s.style.height = `${size}px`;
      s.style.left = `${e.clientX - r.left - size / 2}px`;
      s.style.top = `${e.clientY - r.top - size / 2}px`;
      b.appendChild(s);
      setTimeout(() => s.remove(), 640);
    }, { passive: true });
  }

  /* ---------- boot ---------- */
  initTheme(); initForm(); initRipples(); loadHealth(); loadInputs(); loadRuns();
})();
