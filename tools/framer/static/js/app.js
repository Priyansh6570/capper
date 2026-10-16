// ---------------------------------------------------------------------------
// state (kept in JS memory only; exported to the backend on demand)
//
//   line  = { text, duration_s, pace:null|PACES, mode:'sequence'|'together', frames:[frame] }
//   frame = { bbox:[tx,ty,tw,th], flip:bool, blur:[[tx,ty,tw,th],...] }
// ---------------------------------------------------------------------------
const S = {
  project: null,           // project slug (scopes chapter_work_dir/chapter_output_dir server-side)
  ch_no: null,             // chapter NUMBER within the project (see config.chapter_scope)
  chapter_id: null,
  source_pdf: null,
  strip_w: 0, strip_h: 0,
  preview_scale: 1,        // preview_px = true_px * preview_scale  (base downscale)
  preview_w: 0, preview_h: 0,
  tiles: [],               // [{url,y,w,h}] in preview px
  zoom: 1,
  fitZoom: 1, minZoom: 0.05, maxZoom: 4.0,   // FIXED 5%–400% range
  vscale: 1,               // TRUE px -> on-screen world px = preview_scale * zoom
  lines: [],
  active: -1,              // active line index
  activeFrame: -1,         // active frame index WITHIN the active line
  mode: 'frame',           // 'frame' | 'blur'
  blurStyle: 'm',          // style for NEW blur regions: 'm' mosaic | 'g' gaussian
  blurShape: '',           // '' rectangle | 'o' oval (stored as a suffix on the style: 'mo', 'go')
  watchedJobId: null,      // render job this chapter session should announce completion for
  watchedScriptJobId: null, // Claude script job whose narration should be loaded when it finishes
};

const $ = id => document.getElementById(id);
const stage = $('stage'), world = $('world');
const tilesDiv = $('tiles'), boxesDiv = $('boxes'), rubber = $('rubber');
const clamp = (v,a,b) => Math.max(a, Math.min(b, v));

// ---------------------------------------------------------------------------
// project scoping - every editor request carries the current project + chapter
// number so the backend redirects chapter_work_dir()/chapter_output_dir() (see
// config.chapter_scope in config.py) to projects/<slug>/chapters/<n>/{work,
// output} instead of the legacy global work/<chapter>/ tree.
// ---------------------------------------------------------------------------
function scopeQS() {
  return S.project && S.ch_no ? `&project=${encodeURIComponent(S.project)}&ch_no=${encodeURIComponent(S.ch_no)}` : '';
}
function scopeBody(obj) {
  return S.project && S.ch_no ? Object.assign({}, obj, { project: S.project, ch_no: S.ch_no }) : obj;
}

function setStatus(msg, isErr) {
  const el = $('status');
  if (!el) return;
  const row = document.createElement('div');
  row.className = 'actrow' + (isErr ? ' err' : '');
  const t = new Date();
  const time = [t.getHours(), t.getMinutes(), t.getSeconds()].map(n => String(n).padStart(2,'0')).join(':');
  row.innerHTML = '<span class="acttime">' + time + '</span>' +
    '<span class="actico">' + ic(isErr ? 'alert' : 'info', 13) + '</span>' +
    '<span class="actmsg"></span>';
  row.querySelector('.actmsg').textContent = String(msg);
  el.appendChild(row);
  while (el.children.length > 80) el.removeChild(el.firstChild);
  el.scrollTop = el.scrollHeight;
  if (isErr) showToast(String(msg).split('\n')[0], null, null, 'err');
}

// ---------------------------------------------------------------------------
// loading overlay (spinner + message over the strip pane)
// ---------------------------------------------------------------------------
function showOverlay(msg) { $('overlayMsg').textContent = msg || 'Working…'; $('overlay').classList.add('show'); }
function setOverlay(msg) { $('overlayMsg').textContent = msg; }
function hideOverlay() { $('overlay').classList.remove('show'); }

// ---------------------------------------------------------------------------
// undo / redo - snapshot the editable state BEFORE each destructive edit, so any
// listed action (remove line/frame, reorder, add box, add/clear blur, flip, play
// mode) is reversible. Keeps the last UNDO_LIMIT steps; redo mirrors it.
// ---------------------------------------------------------------------------
const UNDO_LIMIT = 30;
let undoStack = [], redoStack = [];

function snapshot() {
  return {
    lines: JSON.parse(JSON.stringify(S.lines)),
    active: S.active, activeFrame: S.activeFrame, mode: S.mode,
  };
}
function pushUndo(label) {
  undoStack.push({ snap: snapshot(), label });
  if (undoStack.length > UNDO_LIMIT) undoStack.shift();
  redoStack = [];
  updateUndoUI();
  markDirty();
}
function restoreState(snap) {
  S.lines = JSON.parse(JSON.stringify(snap.lines));
  S.active = Math.min(snap.active, S.lines.length - 1);
  S.activeFrame = snap.activeFrame;
  setMode(snap.mode);
  renderLines(); redrawBoxes();
}
function undo() {
  if (!undoStack.length) { setStatus('Nothing to undo.'); return; }
  const item = undoStack.pop();
  redoStack.push({ snap: snapshot(), label: item.label });
  restoreState(item.snap);
  updateUndoUI();
  setStatus(`Undid: ${item.label}`);
}
function redo() {
  if (!redoStack.length) { setStatus('Nothing to redo.'); return; }
  const item = redoStack.pop();
  undoStack.push({ snap: snapshot(), label: item.label });
  restoreState(item.snap);
  updateUndoUI();
  setStatus(`Redid: ${item.label}`);
}
function updateUndoUI() {
  const u = $('undoBtn'), r = $('redoBtn');
  if (u) { u.disabled = !undoStack.length;
    u.title = undoStack.length
      ? `Undo: ${undoStack[undoStack.length-1].label} (${undoStack.length} step${undoStack.length>1?'s':''})`
      : 'Nothing to undo'; }
  if (r) { r.disabled = !redoStack.length;
    r.title = redoStack.length ? `Redo: ${redoStack[redoStack.length-1].label}` : 'Nothing to redo'; }
}

// Ctrl/Cmd+Z = undo, Ctrl/Cmd+Shift+Z or Ctrl+Y = redo. Ignored while typing in a
// text field so the browser's native text-undo still works there.
window.addEventListener('keydown', e => {
  if (SCREEN !== 'editor') return;   // only the editor screen owns undo/redo
  const t = e.target;
  if (t && (t.tagName === 'TEXTAREA' || t.tagName === 'INPUT')) return;
  if (!(e.ctrlKey || e.metaKey)) return;
  const k = e.key.toLowerCase();
  if (k === 'z' && !e.shiftKey) { e.preventDefault(); undo(); }
  else if ((k === 'z' && e.shiftKey) || k === 'y') { e.preventDefault(); redo(); }
});

// ---------------------------------------------------------------------------
// toast - transient message with an optional action button (used as the
// "are you sure?" safety net for remove-line: shows an inline Undo).
// ---------------------------------------------------------------------------
function showToast(msg, actionLabel, actionFn, kind) {
  const stack = $('toast');
  if (!stack) return;
  const el = document.createElement('div');
  el.className = 'tst ' + (kind || 'info');
  const icon = (kind === 'err' || kind === 'warn') ? 'alert' : kind === 'ok' ? 'check' : 'info';
  el.innerHTML = '<span class="tico">' + ic(icon, 16) + '</span><span class="tmsg"></span>';
  el.querySelector('.tmsg').textContent = msg;
  if (actionLabel && actionFn) {
    const btn = document.createElement('button');
    btn.className = 'small'; btn.textContent = actionLabel;
    btn.onclick = () => { actionFn(); el.remove(); };
    el.appendChild(btn);
  }
  const close = document.createElement('button');
  close.className = 'small tclose'; close.title = 'dismiss';
  close.innerHTML = ic('x', 13);
  close.onclick = () => el.remove();
  el.appendChild(close);
  stack.appendChild(el);
  while (stack.children.length > 4) stack.firstChild.remove();
  setTimeout(() => el.remove(), kind === 'err' ? 12000 : 6000);
}
function hideToast() { const s = $('toast'); if (s && s.lastChild) s.lastChild.remove(); }

// ---------------------------------------------------------------------------
// "Open <site>" buttons next to a series/chapter URL field - opens the site
// itself in a new tab so you can browse/search for a series there instead of
// leaving the app to search for it yourself, then copy its URL back into the
// field. These are convenience links only - the source site used for an
// actual fetch/download is always auto-detected from the pasted URL.
const SITE_HOME_URLS = {
  webtoons: 'https://www.webtoons.com/',
  kingofshojo: 'https://kingofshojo.com/',
  mgeko: 'https://www.mgeko.cc/',
};
function openSiteHome(site) {
  window.open(SITE_HOME_URLS[site], '_blank', 'noopener');
}

// download a chapter -> PDF (auto-detecting the site from the URL). The PDF feeds the
// "Stitch PDF" flow: we drop its path into the PDF-path field and the chapter id into its field.
// ---------------------------------------------------------------------------
async function doDownload() {
  const url = $('dlUrl').value.trim();
  const no = $('dlNo').value.trim();
  if (!url) { setStatus('Paste a webtoons.com, kingofshojo.com or mgeko.cc chapter URL.', true); return; }
  const chap = $('chapId').value.trim() || no;   // reuse the chapter-id field; default to the number
  showOverlay('Downloading chapter');
  setGlobalProg(null, 'Downloading chapter');
  setStatus(`Downloading chapter ${no || ''}…\n` +
            `This can take a while for image-heavy chapters - leave it running.`);
  btnBusy($('dlBtn'), true);
  try {
    const res = await fetch('/download', { method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify(scopeBody({ url, chapter_no: no, chapter_id: chap })) });
    const d = await res.json();
    if (!res.ok) throw new Error(d.error || 'download failed');

    // feed the Stitch PDF flow with the FULL chapter PDF
    $('chapId').value = d.chapter_id;
    $('pdfPath').value = d.pdf;
    S.chapter_id = d.chapter_id;
    S.source_pdf = d.pdf || null;

    setStatus((d.steps || []).join('\n'));
    showToast('Downloaded ' + d.page_count + ' pages.', null, null, 'ok');
    $('stripInfo').textContent = `Downloaded ${d.page_count}p. Click Stitch PDF.`;
    hideOverlay();
  } catch (e) {
    setStatus('Download error: ' + e.message, true);
    hideOverlay();
  } finally {
    btnBusy($('dlBtn'), false);
    clearGlobalProg();
    refreshChapterSourceStatus();   // re-derive from disk - disables Download if it now exists
  }
}

// ---------------------------------------------------------------------------
// Claude Code script engine (see claude_engine.py): a "script" job on the same
// polled queue as renders. When it finishes, the chapter's editor state is
// reloaded - the server pre-fills every narration block boxed to its panels.
// ---------------------------------------------------------------------------
async function loadClaudeStatus() {
  try {
    const d = await (await fetch('/api/claude/status')).json();
    $('claudeStatus').textContent = d.message;
    $('claudeStatus').style.color = d.ready ? '' : 'var(--warn)';
    if (SCREEN === 'editor' && !d.ready) $('genScriptStatus').textContent = d.message;
  } catch (_) { $('claudeStatus').textContent = ''; }
}

async function startClaudeScript() {
  if (!S.chapter_id) { setStatus('Load/Export a chapter first.', true); return; }
  $('genScriptResult').innerHTML = '';
  try {
    const res = await fetch('/api/script/claude', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(scopeBody({ chapter: S.chapter_id })) });
    const d = await res.json();
    if (!res.ok) throw new Error(d.error || 'failed to start');
    S.watchedScriptJobId = d.job_id;
    pollJobs();
  } catch (e) {
    $('genScriptStatus').textContent = e.message;
    setStatus(e.message, true);
  }
}

const fmtTokens = n => n >= 1e6 ? (n / 1e6).toFixed(2) + 'M' : n >= 1e3 ? (n / 1e3).toFixed(1) + 'k' : String(n);
const tokenLine = u => `${fmtTokens(u.input)} in · ${fmtTokens(u.output)} out · ${fmtTokens(u.cache_read)} cache read · ${fmtTokens(u.cache_write)} cache write`;
const costLine = u => `$${u.cost_usd.toFixed(2)} estimated API cost (not charged on subscription)`;
const clockSeconds = s => s >= 60 ? `${Math.floor(s / 60)}m ${Math.round(s % 60)}s` : `${Math.round(s)}s`;

// One line per pass for the activity feed, widget and project view alike.
function claudeStatsHtml(j) {
  const c = j.claude;
  if (!c) return '';
  const elapsed = j.started_at ? clockSeconds((Date.now() - new Date(j.started_at).getTime()) / 1000) : '';
  const rows = [`<b>${escHtml(j.message || '')}</b> · ${elapsed} elapsed · panels read <b>${c.panels_read}/${c.panels_total}</b>` +
                (c.current ? ` · now ${escHtml(c.current)}` : '')];
  [['Pass 1', c.pass1], ['Pass 2', c.pass2]].forEach(([label, u]) => {
    if (u) rows.push(`${label}: ${tokenLine(u)}` + (u.cost_usd ? ` · ${costLine(u)}` : ''));
  });
  if (c.total) rows.push(`<b>Chapter total</b>: ${tokenLine(c.total)} · ${costLine(c.total)}`);
  return rows.join('<br>');
}

// Strip viewer marks: panels read so far, the one being read, and any skipped.
const readState = { panels: [], read: new Set(), dropped: new Set(), current: null, done: false, es: null, jobId: null, positioned: false };

async function loadReadPanels() {
  const d = await (await fetch(`/api/claude/panels?chapter=${encodeURIComponent(S.chapter_id)}${scopeQS()}`)).json();
  readState.panels = d.panels;
  redrawReadMarks();
}

function redrawReadMarks() {
  const layer = $('readmarks');
  layer.innerHTML = '';
  const newest = Math.max(-1, ...readState.panels.map((p, i) => readState.read.has(p.id) ? i : -1));
  readState.panels.forEach((p, i) => {
    const read = readState.read.has(p.id);
    const skipped = !read && (readState.done || i < newest - READ_BATCH_SLACK);
    const cls = p.id === readState.current ? 'reading' : read ? 'read' : readState.dropped.has(p.id) ? 'dropped' : skipped ? 'skipped' : '';
    if (cls) layer.appendChild(boxEl(p.bbox, cls, p.id));
  });
}
const READ_BATCH_SLACK = 10;

// While a Claude job runs the strip belongs to it: it starts at the chapter's
// first panel at fit-width zoom, and the user cannot scroll, zoom or draw.
function setStripLocked(on) {
  document.body.classList.toggle('stripLocked', on);
  if (!on) readState.positioned = false;
  else if (S.preview_w && !readState.positioned) {
    readState.positioned = true;
    computeZoomRange();
    setZoom(S.fitZoom);
    $('stage').scrollTop = 0;
  }
}

function scrollToPanel(id) {
  const p = readState.panels.find(x => x.id === id);
  if (p) $('stage').scrollTop = trueToWorld(p.bbox[1]) - $('stage').clientHeight / 3;
}

async function openClaudeFeed(job) {
  if (readState.jobId === job.id) return;
  closeClaudeFeed();
  readState.jobId = job.id;
  readState.read = new Set(); readState.dropped = new Set(); readState.current = null; readState.done = false;
  $('claudeFeed').hidden = false;
  $('cfText').textContent = '';
  await loadReadPanels();
  if (readState.jobId !== job.id) return;
  readState.es = new EventSource(`/api/jobs/${encodeURIComponent(job.id)}/events`);
  readState.es.onmessage = e => handleClaudeEvent(JSON.parse(e.data));
}

function closeClaudeFeed() {
  if (readState.es) readState.es.close();
  readState.es = null; readState.jobId = null;
}

function handleClaudeEvent(ev) {
  const text = $('cfText');
  if (ev.type === 'plan') {
    readState.dropped = new Set(ev.dropped);
    redrawReadMarks();
  } else if (ev.type === 'read') {
    readState.read.add(ev.panel);
    readState.current = ev.panel;
    redrawReadMarks();
    scrollToPanel(ev.panel);
    text.textContent += `\nReading ${ev.panel}\n`;
  } else if (ev.type === 'text') {
    text.textContent += ev.text;
  } else if (ev.type === 'step') {
    text.textContent += `\n— ${ev.label} —\n`;
    if (ev.pass === 2) { readState.done = true; readState.current = null; redrawReadMarks(); }
  } else if (ev.type === 'log') {
    setStatus(ev.line, ev.line.startsWith('ERROR'));
    text.textContent += `\n${ev.line}\n`;
  } else if (ev.type === 'end') {
    readState.done = true; readState.current = null; redrawReadMarks();
    closeClaudeFeed();
    return;
  }
  text.scrollTop = text.scrollHeight;
}

function scriptJobText(j) {
  if (j.status === 'queued') return 'Queued…';
  return j.resume_at ? j.message : `${j.message} (${Math.round((j.progress || 0) * 100)}%)`;
}

function syncScriptJobUI() {
  if (SCREEN !== 'editor' || !S.chapter_id) return;
  const job = jobsForChapter(S.chapter_id).find(j => j.what === 'script');
  const active = !!job && (job.status === 'queued' || job.status === 'running');
  setStripLocked(!!job && job.status === 'running');
  btnBusy($('genScriptBtn'), active);
  if (active) {
    S.watchedScriptJobId = job.id;
    $('genScriptStatus').innerHTML = escHtml(scriptJobText(job)) +
      ` <button type="button" class="small ghost" onclick="cancelScriptJob('${job.id}')">Stop</button>`;
    $('cfStats').innerHTML = claudeStatsHtml(job);
    if (job.status === 'running') openClaudeFeed(job);
    return;
  }
  if (job && job.claude) $('cfStats').innerHTML = claudeStatsHtml(job);
  if (!job || S.watchedScriptJobId !== job.id) return;
  S.watchedScriptJobId = null;
  if (job.status === 'done') {
    $('genScriptStatus').textContent = job.message + ' Fix any wrong boxes, then export.';
    showToast('Claude narration ready.', null, null, 'ok');
    loadEditorState(false, false, true);
  } else if (job.status === 'failed') {
    $('genScriptStatus').innerHTML = escHtml(job.message) +
      ' <button type="button" class="small ghost" onclick="startClaudeScript()">Retry</button>';
    setStatus(job.message, true);
  } else {
    $('genScriptStatus').textContent = job.message;
  }
}

async function cancelScriptJob(jobId) {
  await fetch(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, { method: 'POST' });
  pollJobs();
}

function fmtSize(n) {
  if (!n && n !== 0) return '';
  const units = ['B','KB','MB','GB']; let v = n, i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return (i === 0 ? Math.round(v) : v.toFixed(1)) + units[i];
}
function escAttr(s) { return String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;'); }

async function copyToClipboard(text) {
  try {
    await navigator.clipboard.writeText(text);
    showToast('Copied to clipboard.', null, null, 'ok');
  } catch (_) {
    setStatus('Copy this path: ' + text, false);
  }
}

async function openFolder(path) {
  try {
    const res = await fetch('/open_folder', { method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({ path }) });
    const d = await res.json();
    if (!res.ok) throw new Error(d.error || 'could not open folder');
  } catch (e) {
    setStatus('Open folder: ' + e.message, true);
  }
}

// ---------------------------------------------------------------------------
// load PDF -> tiled preview
// ---------------------------------------------------------------------------
async function loadPdf() {
  const file = $('pdfFile').files[0];
  const path = $('pdfPath').value.trim();
  const chap = $('chapId').value.trim();
  if (!file && !path) { setStatus('Enter a PDF path or choose a file.', true); return; }
  showOverlay('Stitching PDF');
  btnBusy($('loadPdfBtn'), true);
  setGlobalProg(null, 'Stitching PDF');
  setStatus('Stitching PDF… (rendering every page + tiling the preview, this can take a moment)');
  try {
    let res;
    if (file) {
      const fd = new FormData();
      fd.append('pdf', file);
      if (chap) fd.append('chapter_id', chap);
      if (S.project && S.ch_no) { fd.append('project', S.project); fd.append('ch_no', S.ch_no); }
      res = await fetch('/load_pdf', { method:'POST', body:fd });
    } else {
      res = await fetch('/load_pdf', { method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify(scopeBody({ pdf_path: path, chapter_id: chap || undefined })) });
    }
    const d = await res.json();
    if (!res.ok) throw new Error(d.error || 'load failed');
    setOverlay('Building preview…');
    S.chapter_id = d.chapter_id;
    S.source_pdf = d.source_pdf || null;
    S.preview_scale = d.preview_scale;
    S.strip_w = d.strip_w; S.strip_h = d.strip_h;
    S.preview_w = d.preview_w; S.preview_h = d.preview_h;
    S.tiles = d.tiles;
    $('chapId').value = d.chapter_id;
    buildTiles();
    computeZoomRange();
    setZoom(S.fitZoom);             // start fit-width so the whole width is visible
    stage.scrollTop = 0;
    await firstTilePromise();       // wait for the first tile to paint before hiding
    $('stripInfo').textContent =
      `strip ${d.strip_w}×${d.strip_h}px · preview ${d.preview_w}×${d.preview_h} (${d.tiles.length} tiles) · base ${d.preview_scale.toFixed(4)}`;
    setStatus(`Loaded ${d.chapter_id}. Full-res ${d.strip_w}×${d.strip_h}. Zoom out to navigate, in to see the art.`);
    showToast('Stitched ' + d.tiles.length + ' tiles - strip ' + d.strip_w + 'x' + d.strip_h + 'px.', null, null, 'ok');
    maybeOfferRecovery(d.chapter_id);
  } catch (e) { setStatus('PDF load error: ' + e.message, true); }
  finally {
    btnBusy($('loadPdfBtn'), false); clearGlobalProg(); hideOverlay();
    refreshChapterSourceStatus();   // re-derive from disk - disables Stitch now that a strip exists
  }
}

function buildTiles() {
  tilesDiv.innerHTML = '';
  S.tiles.forEach((t, idx) => {
    const img = new Image();
    img.className = 'tile';
    img.loading = idx === 0 ? 'eager' : 'lazy';   // eager first tile so we can wait on it
    img.dataset.y = t.y; img.dataset.h = t.h;
    img.src = t.url + '?t=' + Date.now() + scopeQS();
    tilesDiv.appendChild(img);
  });
}

// resolves once the first (eager) tile has painted, or after a short timeout, so
// "Building preview…" stays up until there's actually art on screen.
function firstTilePromise() {
  const first = tilesDiv.querySelector('img.tile');
  if (!first || first.complete) return Promise.resolve();
  return new Promise(res => {
    first.addEventListener('load', res, { once:true });
    first.addEventListener('error', res, { once:true });
    setTimeout(res, 2500);
  });
}

// ---------------------------------------------------------------------------
// zoom + layout  (FIXED 5%–400% so you can zoom out to the whole strip)
// ---------------------------------------------------------------------------
function computeZoomRange() {
  S.fitZoom = stage.clientWidth / S.preview_w;   // for the Fit-width button
  S.minZoom = 0.05;                              // 5%  - whole strip for navigation
  S.maxZoom = 4.0;                               // 400%
}

function layout() {
  S.vscale = S.preview_scale * S.zoom;           // TRUE px -> world px
  const ww = Math.round(S.preview_w * S.zoom);
  const wh = Math.round(S.preview_h * S.zoom);
  world.style.width = ww + 'px';
  world.style.height = wh + 'px';
  for (const img of tilesDiv.children) {
    const y = +img.dataset.y, h = +img.dataset.h;
    img.style.top = (y * S.zoom) + 'px';
    img.style.width = ww + 'px';
    img.style.height = (h * S.zoom) + 'px';
  }
  redrawBoxes();
  const eff = (S.vscale * 100).toFixed(1);
  $('zoomLbl').textContent = `Zoom ${Math.round(S.zoom*100)}%  (${eff}% true)`;
  const t = (S.zoom - S.minZoom) / (S.maxZoom - S.minZoom);
  $('zoom').value = String(Math.round(clamp(t,0,1) * 100));
}

// set zoom, keeping the strip point under (cx,cy) [client px] fixed.
function setZoom(z, cx, cy) {
  z = clamp(z, S.minZoom, S.maxZoom);
  const rect = stage.getBoundingClientRect();
  if (cx == null) { cx = rect.left + stage.clientWidth/2; cy = rect.top + stage.clientHeight/2; }
  const vx = cx - rect.left, vy = cy - rect.top;
  const wx = stage.scrollLeft + vx, wy = stage.scrollTop + vy;
  const truePt = [ wx / S.vscale, wy / S.vscale ];     // invariant point (old zoom)
  S.zoom = z; layout();
  stage.scrollLeft = truePt[0] * S.vscale - vx;
  stage.scrollTop  = truePt[1] * S.vscale - vy;
}

const isStripLocked = () => document.body.classList.contains('stripLocked');
// Zoom requested by the user; the strip ignores it while a Claude job owns the viewer.
function userZoom(z, cx, cy) { if (!isStripLocked()) setZoom(z, cx, cy); }

$('zoom').addEventListener('input', e => {
  const t = (+e.target.value) / 100;
  userZoom(S.minZoom + t * (S.maxZoom - S.minZoom));
});
$('zoomIn').onclick  = () => userZoom(S.zoom * 1.25);
$('zoomOut').onclick = () => userZoom(S.zoom / 1.25);
$('fitBtn').onclick  = () => { computeZoomRange(); userZoom(S.fitZoom); };
$('oneBtn').onclick  = () => userZoom(1);

stage.addEventListener('wheel', e => {
  if (!(e.ctrlKey || e.metaKey)) return;     // plain wheel = scroll
  e.preventDefault();
  const factor = e.deltaY < 0 ? 1.12 : 1/1.12;
  userZoom(S.zoom * factor, e.clientX, e.clientY);
}, { passive:false });

window.addEventListener('resize', () => { if (S.preview_w) { computeZoomRange(); layout(); } });

// ---------------------------------------------------------------------------
// coordinate mapping (screen <-> TRUE strip px) - exact at ANY zoom/scroll.
// world.getBoundingClientRect() folds in scroll AND the zoom-scaled CSS size,
// so dividing by vscale (= preview_scale * zoom) yields TRUE px directly.
// ---------------------------------------------------------------------------
function screenToTrue(clientX, clientY) {
  const r = world.getBoundingClientRect();
  return [ (clientX - r.left) / S.vscale, (clientY - r.top) / S.vscale ];
}
function trueToWorld(v) { return v * S.vscale; }

// ---------------------------------------------------------------------------
// drawing boxes on the strip - Pointer Events cover mouse AND touch/pen with
// the SAME clientX/clientY semantics as the old mousedown/mousemove/mouseup,
// so screenToTrue()/the box math below are byte-identical to before.
//
// Touch adds one thing on top: a SECOND simultaneous pointer cancels any
// in-progress box and enters pinch - each move recomputes the live midpoint
// and distance and feeds them straight into the existing setZoom(z, cx, cy),
// which re-anchors the view on that point. Because the midpoint itself moves
// as the fingers drag, this gives pinch-zoom AND two-finger pan for free,
// with no change to setZoom itself.
// ---------------------------------------------------------------------------
let dragging = false, startTrue = null;
const activePointers = new Map();   // pointerId -> {x,y} (client px)
let pinch = null;                   // {startDist, startZoom}

function activeLine() { return S.lines[S.active]; }
function ptDist(a, b) { return Math.hypot(a.x - b.x, a.y - b.y); }
function ptMid(a, b) { return [(a.x + b.x) / 2, (a.y + b.y) / 2]; }

world.addEventListener('pointerdown', ev => {
  if (document.body.classList.contains('stripFullscreen')) return;   // fullscreen is read-only
  if (ev.pointerType === 'mouse' && ev.button !== 0) return;
  activePointers.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });

  if (activePointers.size >= 2) {
    if (dragging) { dragging = false; rubber.style.display = 'none'; }   // a 2nd touch cancels any box in progress
    const pts = [...activePointers.values()];
    pinch = { startDist: ptDist(pts[0], pts[1]), startZoom: S.zoom };
    ev.preventDefault();
    return;
  }

  if (!S.tiles.length) return;
  if (S.active < 0) { setStatus('Click a script line first to make it active.', true); return; }
  if (S.mode === 'blur' && (!activeLine() || S.activeFrame < 0)) {
    setStatus('Add a frame first, then select it to draw blur on it.', true); return;
  }
  dragging = true;
  startTrue = screenToTrue(ev.clientX, ev.clientY);
  rubber.className = (S.mode === 'blur') ? 'blur' : '';
  rubber.style.display = 'block';
  drawRubber(startTrue, startTrue);
  ev.preventDefault();
});
window.addEventListener('pointermove', ev => {
  if (activePointers.has(ev.pointerId)) activePointers.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });

  if (pinch && activePointers.size >= 2) {
    const pts = [...activePointers.values()];
    const dist = ptDist(pts[0], pts[1]);
    const mid = ptMid(pts[0], pts[1]);
    if (pinch.startDist > 0) userZoom(pinch.startZoom * (dist / pinch.startDist), mid[0], mid[1]);
    return;
  }
  if (!dragging) return;
  drawRubber(startTrue, screenToTrue(ev.clientX, ev.clientY));
});
function endPointer(ev) {
  activePointers.delete(ev.pointerId);
  if (activePointers.size < 2) pinch = null;
  if (!dragging) return;
  dragging = false;
  rubber.style.display = 'none';
  const cur = screenToTrue(ev.clientX, ev.clientY);
  const x = Math.min(startTrue[0], cur[0]), y = Math.min(startTrue[1], cur[1]);
  const w = Math.abs(cur[0]-startTrue[0]), h = Math.abs(cur[1]-startTrue[1]);
  if (w * S.vscale < 4 || h * S.vscale < 4) return;     // ignore tiny clicks
  const box = [
    Math.round(clamp(x, 0, S.strip_w-1)),
    Math.round(clamp(y, 0, S.strip_h-1)),
    Math.round(clamp(w, 1, S.strip_w)),
    Math.round(clamp(h, 1, S.strip_h)),
  ];
  const ln = activeLine();
  if (S.mode === 'frame') {
    pushUndo('add frame');
    ln.frames.push({ bbox: box, flip:false, blur:[] });   // ADD a frame
    S.activeFrame = ln.frames.length - 1;
    setStatus(`Added frame ${ln.frames.length} to line ${S.active+1}: [${box.join(', ')}] true px`);
  } else {
    pushUndo('add blur');
    (ln.frames[S.activeFrame].blur ||= []).push([...box, S.blurStyle + S.blurShape]);  // box + style (+ 'o' for an oval)
    setStatus(`Blur region (${S.blurStyle==='m'?'mosaic':'gaussian'}${S.blurShape ? ', oval' : ''}) added to line ${S.active+1}, frame ${S.activeFrame+1}.`);
  }
  redrawBoxes();
  renderRow(S.active);
}
window.addEventListener('pointerup', endPointer);
window.addEventListener('pointercancel', endPointer);

function drawRubber(a, b) {
  const x = Math.min(a[0],b[0]), y = Math.min(a[1],b[1]);
  const w = Math.abs(b[0]-a[0]), h = Math.abs(b[1]-a[1]);
  rubber.style.left = trueToWorld(x)+'px';
  rubber.style.top = trueToWorld(y)+'px';
  rubber.style.width = trueToWorld(w)+'px';
  rubber.style.height = trueToWorld(h)+'px';
}

function boxEl(box, cls, lbl) {
  const d = document.createElement('div');
  d.className = 'box ' + cls;
  d.style.left = trueToWorld(box[0])+'px';
  d.style.top = trueToWorld(box[1])+'px';
  d.style.width = trueToWorld(box[2])+'px';
  d.style.height = trueToWorld(box[3])+'px';
  if (lbl) d.dataset.lbl = lbl;
  return d;
}

function redrawBoxes() {
  redrawReadMarks();
  boxesDiv.innerHTML = '';
  const ln = activeLine();
  if (!ln) return;
  ln.frames.forEach((f, j) => {
    boxesDiv.appendChild(boxEl(f.bbox, 'frame' + (j===S.activeFrame ? ' cur':''), 'frame ' + (j+1)));
  });
  const cur = ln.frames[S.activeFrame];
  if (cur) for (const b of (cur.blur || [])) {
    const el = boxEl(b, 'blur');
    if (String(b[4] || '').endsWith('o')) el.style.borderRadius = '50%';
    // LIVE blur preview on the strip: fill the box with the same gaussian-blurred
    // crop the exporter will bake for this region.
    if (S.chapter_id) {
      el.style.backgroundImage = `url(${cropUrl(b, [b])})`;
      el.style.backgroundSize = '100% 100%';
      el.style.backgroundRepeat = 'no-repeat';
    }
    boxesDiv.appendChild(el);
  }
}

// ---------------------------------------------------------------------------
// thumbnails - server crops from the preview tiles (always correct, light)
// ---------------------------------------------------------------------------
function cropUrl(box, blurRegions) {
  let q = `chapter=${encodeURIComponent(S.chapter_id)}&x=${box[0]}&y=${box[1]}&w=${box[2]}&h=${box[3]}`;
  if (blurRegions && blurRegions.length)
    q += `&blur=${blurRegions.map(b => b.join(',')).join(';')}`;
  return `/crop?${q}${scopeQS()}`;
}
// a frame's thumbnail includes its blur regions, so the blur shows LIVE
function thumbUrl(f) { return cropUrl(f.bbox, f.blur); }

// ---------------------------------------------------------------------------
// script lines
// ---------------------------------------------------------------------------
// how a line is delivered by the voice (see pacing.py); null = chosen from the text
const PACES = ['calm', 'tense', 'action', 'reveal', 'sad', 'comic'];
function newLine(text, pace) { return { text: text||'', duration_s:4, pace: pace||null, mode:'sequence', frames:[] }; }

async function loadScript() {
  const text = $('scriptText').value;
  const txtPath = $('txtPath').value.trim();
  if (!text.trim() && !txtPath) { setStatus('Paste a script or give a .txt path.', true); return; }
  try {
    const body = text.trim() ? { text } : { txt_path: txtPath };
    const res = await fetch('/load_script', { method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) });
    const d = await res.json();
    if (!res.ok) throw new Error(d.error || 'load failed');
    if (S.lines.length) pushUndo('load script');   // don't silently nuke existing work
    S.lines = d.lines.map((t, i) => newLine(t, (d.paces || [])[i]));
    S.active = S.lines.length ? 0 : -1;
    S.activeFrame = -1;
    renderLines(); redrawBoxes();
    setStatus(`Loaded ${S.lines.length} script line(s).`);
    markDirty();
  } catch (e) { setStatus('Script load error: ' + e.message, true); }
}

function addLine() {
  pushUndo('add line');
  S.lines.push(newLine(''));
  setActiveLine(S.lines.length - 1);
}

function setActiveLine(i) {
  S.active = i;
  const ln = S.lines[i];
  S.activeFrame = (ln && ln.frames.length) ? ln.frames.length - 1 : -1;
  renderLines(); redrawBoxes();
}
function setActiveFrame(i, j) {
  S.active = i; S.activeFrame = j;
  renderLines(); redrawBoxes();
}

function renderLines() {
  const box = $('lines'); box.innerHTML = '';
  box.appendChild(buildInsertBar(0));                 // insert at the very top
  S.lines.forEach((ln, i) => {
    box.appendChild(buildRow(ln, i));
    box.appendChild(buildInsertBar(i + 1));           // ...and between every pair
  });
  const n = S.lines.length;
  const badge = $('lineCountBadge'); if (badge) badge.textContent = `${n} line${n===1?'':'s'}`;
  const tabBadge = $('tabScriptBadge'); if (tabBadge) tabBadge.textContent = n ? String(n) : '';
}

// a thin "+ insert" affordance that drops a blank line at the given position
function buildInsertBar(pos) {
  const bar = document.createElement('div');
  bar.className = 'insbar';
  bar.innerHTML = `<button class="small">+ insert</button>`;
  bar.querySelector('button').addEventListener('click', e => { e.stopPropagation(); insertLine(pos); });
  return bar;
}
function insertLine(pos) {
  pushUndo('insert line');
  S.lines.splice(pos, 0, newLine(''));
  setActiveLine(pos);
  scrollActiveRowIntoView();
}

// split a line into two at the text cursor (or in half if no cursor). The first
// part keeps the original line's frames; the new part inherits its mode/duration.
function splitLine(i, pos) {
  const ln = S.lines[i];
  const text = ln.text || '';
  if (pos == null || pos < 0 || pos > text.length) pos = Math.floor(text.length / 2);
  pushUndo('split line');
  const after = text.slice(pos);
  ln.text = text.slice(0, pos);
  const nl = newLine(after);
  nl.duration_s = ln.duration_s; nl.mode = ln.mode; nl.pace = ln.pace;
  S.lines.splice(i + 1, 0, nl);          // frames stay with line i
  setActiveLine(i);
  scrollActiveRowIntoView();
}

// merge a line UP into the one above: combine text, keep BOTH lines' frames in order.
function mergeUp(i) {
  if (i <= 0) return;
  pushUndo('merge line');
  const up = S.lines[i - 1], cur = S.lines[i];
  const a = (up.text || ''), b = (cur.text || '');
  up.text = (a.trim() && b.trim()) ? (a + ' ' + b) : (a + b);
  up.frames = up.frames.concat(cur.frames);
  S.lines.splice(i, 1);
  setActiveLine(i - 1);
  scrollActiveRowIntoView();
}
function renderRow(i) {
  const old = document.querySelector(`.row[data-i="${i}"]`);
  if (old) old.replaceWith(buildRow(S.lines[i], i));
  else renderLines();
}

function buildRow(ln, i) {
  const row = document.createElement('div');
  row.className = 'row' + (i===S.active ? ' active':'');
  row.dataset.i = i;
  row.innerHTML = `
    <span class="grip" title="drag to reorder lines" draggable="true">${ic('grip',15)}</span>
    <div class="rbody">
      <textarea class="ltext" rows="2"></textarea>
      <div class="rmeta">
        <span class="idx">#${i+1}</span>
        <label class="durwrap"><span class="durlbl">dur s</span><input class="dur" type="number" min="0.5" step="0.5" value="${ln.duration_s}"></label>
        <label title="How the voice delivers this line: pauses and speed. Auto picks from the text."><span>pace</span><select class="pace"><option value="">auto</option>${PACES.map(p => `<option value="${p}"${ln.pace===p?' selected':''}>${p}</option>`).join('')}</select></label>
        <span class="pill" style="padding:1px 4px">play:</span>
        <button class="seg seqbtn ${ln.mode==='sequence'?'on':''}">sequence</button>
        <button class="seg togbtn ${ln.mode==='together'?'on':''}">together</button>
        <button class="small splitbtn" title="split this line at the cursor">${ic('scissors',13)}<span>split</span></button>
        <button class="small mergebtn" title="merge into the line above">${ic('arrowup',13)}<span>merge</span></button>
        <button class="small danger del" title="remove this line">${ic('trash',13)}<span>line</span></button>
      </div>
      <div class="frames"></div>
    </div>`;

  const ta = row.querySelector('.ltext'); ta.value = ln.text;
  ta.addEventListener('input', e => { ln.text = e.target.value; updateDurUI(row, ln); markDirty(); });
  row.querySelector('.dur').addEventListener('input', e => { ln.duration_s = parseFloat(e.target.value)||0; markDirty(); });
  row.querySelector('.pace').addEventListener('change', e => { pushUndo('pace'); ln.pace = e.target.value || null; markDirty(); });
  row.querySelector('.splitbtn').addEventListener('click', e => { e.stopPropagation();
    splitLine(i, ta.selectionStart); });
  const mb = row.querySelector('.mergebtn');
  if (i === 0) mb.disabled = true;
  mb.addEventListener('click', e => { e.stopPropagation(); mergeUp(i); });
  row.querySelector('.seqbtn').addEventListener('click', e => { e.stopPropagation();
    if (ln.mode!=='sequence') { pushUndo('play: sequence'); ln.mode='sequence'; } renderRow(i); });
  row.querySelector('.togbtn').addEventListener('click', e => { e.stopPropagation();
    if (ln.mode!=='together') { pushUndo('play: together'); ln.mode='together'; } renderRow(i); });
  row.querySelector('.del').addEventListener('click', e => { e.stopPropagation();
    pushUndo('remove line');
    const lbl = (ln.text||'').trim()
      ? `"${ln.text.trim().slice(0,28)}${ln.text.trim().length>28?'…':''}"` : `line ${i+1}`;
    S.lines.splice(i,1);
    if (S.active>=S.lines.length) S.active=S.lines.length-1;
    S.activeFrame = -1; renderLines(); redrawBoxes();
    showToast(`Removed ${lbl}.`, 'Undo', undo, 'warn'); });
  row.addEventListener('mousedown', () => { if (i!==S.active) setActiveLine(i); });
  updateDurUI(row, ln);

  // ---- per-line frame cards -------------------------------------------------
  const fbox = row.querySelector('.frames');
  ln.frames.forEach((f, j) => fbox.appendChild(buildFrameCard(ln, i, f, j)));
  const hint = document.createElement('div');
  hint.className = 'addhint';
  hint.textContent = ln.frames.length ? '+ drag on strip to add another frame'
                                      : 'drag a box on the strip to add a frame';
  fbox.appendChild(hint);

  // drag-reorder LINES via the grip
  const grip = row.querySelector('.grip');
  grip.addEventListener('dragstart', e => { e.dataTransfer.setData('text/plain', 'line:'+i); row.classList.add('dragover'); });
  grip.addEventListener('dragend', () => row.classList.remove('dragover'));
  row.addEventListener('dragover', e => { e.preventDefault(); row.classList.add('dragover'); });
  row.addEventListener('dragleave', () => row.classList.remove('dragover'));
  row.addEventListener('drop', e => { e.preventDefault(); row.classList.remove('dragover');
    const tok = e.dataTransfer.getData('text/plain');
    if (!tok.startsWith('line:')) return;
    const from = parseInt(tok.slice(5), 10), to = i;
    if (Number.isNaN(from) || from===to) return;
    pushUndo('reorder lines');
    const [moved] = S.lines.splice(from,1); S.lines.splice(to,0,moved);
    setActiveLine(to); });

  return row;
}

// Make the duration field's behaviour clear: lines WITH narration text are timed
// by their generated audio (s6 overwrites duration_s), so the field is greyed and
// relabelled "auto (audio length)". Lines with no text fall back to this value.
function updateDurUI(row, ln) {
  const wrap = row.querySelector('.durwrap');
  const dur = row.querySelector('.dur');
  if (!wrap || !dur) return;
  const narrated = !!(ln.text || '').trim();
  dur.disabled = narrated;
  wrap.classList.toggle('auto', narrated);
  wrap.querySelector('.durlbl').textContent = narrated ? 'auto (audio length)' : 'dur s';
  wrap.title = narrated
    ? 'This line has narration text, so the frame is shown for the generated AUDIO length - this value is ignored unless the line ends up with no audio.'
    : 'No narration text → the frame is shown for this many seconds.';
}

function buildFrameCard(ln, i, f, j) {
  const card = document.createElement('div');
  card.className = 'fcard' + (i===S.active && j===S.activeFrame ? ' cur':'');
  card.innerHTML = `
    <div class="fthumb${f.flip?' flip':''}"></div>
    <div class="fctl">
      <span class="fn">${j+1}</span>
      ${f.blur && f.blur.length ? `<span class="blurchip">${f.blur.length} blur</span><button class="blurclr danger" title="clear blur on this frame">${ic('xcircle',13)}</button>`:''}
      <button class="mvL" title="move left">${ic('chevleft',13)}</button>
      <button class="mvR" title="move right">${ic('chevright',13)}</button>
      <button class="fdel danger" title="remove frame">${ic('x',13)}</button>
    </div>
    <div class="fctl">
      <label><input type="checkbox" class="fflip" ${f.flip?'checked':''}> flip</label>
    </div>`;

  const thumb = card.querySelector('.fthumb');
  thumb.style.backgroundImage = `url(${thumbUrl(f)})`;
  thumb.title = 'click to select this frame (blur draws on it)';
  thumb.addEventListener('click', e => { e.stopPropagation(); setActiveFrame(i, j); });

  card.querySelector('.fflip').addEventListener('change', e => {
    pushUndo('flip frame');
    f.flip = e.target.checked; thumb.classList.toggle('flip', f.flip); });
  card.querySelector('.fdel').addEventListener('click', e => { e.stopPropagation();
    pushUndo('remove frame');
    ln.frames.splice(j,1);
    if (S.active===i) S.activeFrame = Math.min(S.activeFrame, ln.frames.length-1);
    renderRow(i); redrawBoxes(); });
  card.querySelector('.mvL').addEventListener('click', e => { e.stopPropagation(); moveFrame(ln,i,j,-1); });
  card.querySelector('.mvR').addEventListener('click', e => { e.stopPropagation(); moveFrame(ln,i,j,+1); });
  const clr = card.querySelector('.blurclr');
  if (clr) clr.addEventListener('click', e => { e.stopPropagation();
    pushUndo('clear blur'); f.blur = [];
    renderRow(i); redrawBoxes(); });
  return card;
}

function moveFrame(ln, i, j, dir) {
  const k = j + dir;
  if (k < 0 || k >= ln.frames.length) return;
  pushUndo('reorder frame');
  const [m] = ln.frames.splice(j,1); ln.frames.splice(k,0,m);
  if (S.active===i && S.activeFrame===j) S.activeFrame = k;
  renderRow(i); redrawBoxes();
}

const blurEnabled = () => !!(CURRENT_PROJECT && CURRENT_PROJECT.settings.framing && CURRENT_PROJECT.settings.framing.blur_text);

function paintBlurTool() {
  const on = blurEnabled();
  ['blurModeBtn', 'blurStyle', 'blurShape'].forEach(id => {
    const el = $(id);
    (el.closest('.sidefield') || el).style.display = on ? '' : 'none';
  });
  if (!on && S.mode === 'blur') setMode('frame');
}

function setMode(m) {
  S.mode = m;
  $('blurModeBtn').classList.toggle('on', m==='blur');
  $('frameModeBtn').classList.toggle('on', m==='frame');
}

// ---------------------------------------------------------------------------
// export
// ---------------------------------------------------------------------------
async function doExport() {
  if (!S.chapter_id) { setStatus('Load a PDF first.', true); return; }
  const entries = S.lines.map(ln => ({
    line_text: ln.text, duration_s: ln.duration_s || 4, pace: ln.pace || null, mode: ln.mode,
    frames: ln.frames.map(f => ({ bbox: f.bbox, flip: !!f.flip, blur: f.blur || [] })),
  }));
  const lines = entries.filter(e => e.frames.length).length;
  const frames = entries.reduce((n,e) => n + e.frames.length, 0);
  if (!lines) { setStatus('No lines have a frame. Draw a box on the strip first.', true); return; }
  setStatus(`Exporting ${frames} frame(s) across ${lines} line(s)...`);
  btnBusy($('exportBtn'), true);
  setGlobalProg(null, 'Exporting frames');
  try {
    const res = await fetch('/export', { method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify(scopeBody({ chapter_id: S.chapter_id, entries })) });
    const d = await res.json();
    if (!res.ok) throw new Error(d.error || 'export failed');
    setStatus(
      `Exported ${d.frames_written} frame(s) across ${d.lines_written} line(s).\n` +
      `frames:   ${d.frames_dir}\n` +
      `mapping:  ${d.mapping_path}\n` +
      `manifest: ${d.manifest_path}\n\n` +
      `Next, run the pipeline's audio + video:\n  ${d.next_command}`);
    $('exportHint').textContent = d.frames_dir;
    saveEditorState(false, { silent: true });
    showToast('Exported ' + d.frames_written + ' frames across ' + d.lines_written + ' lines.', null, null, 'ok');
  } catch (e) { setStatus('Export error: ' + e.message, true); }
  finally { btnBusy($('exportBtn'), false); clearGlobalProg(); }
}

// ---------------------------------------------------------------------------
// generate audio + video - a server-authoritative job QUEUE (see /api/generate,
// /api/jobs in app.py), NOT an SSE stream. The frontend polls GET /api/jobs on
// an interval (startJobsPolling(), called once at boot) and treats that as the
// single source of truth for what's queued/running/just-finished - so
// navigating away and back to a chapter always shows its REAL state, never a
// stale assumption from a closed connection. One poll loop drives both the
// global corner widget (jobWidget - visible on every screen) and, when the
// chapter editor is open, its detailed genbar panel.
// ---------------------------------------------------------------------------
let JOBS = [];                 // most recent GET /api/jobs result: active + a recent-finished tail

// Calls `fn` every `ms` while the tab is visible. A hidden tab stops polling, and showing it again
// polls at once, so nothing is stale when the person comes back.
function pollWhileVisible(fn, ms) {
  let timer = null;
  const start = () => { if (!timer) { fn(); timer = setInterval(fn, ms); } };
  const stop = () => { clearInterval(timer); timer = null; };
  document.addEventListener('visibilitychange', () => document.hidden ? stop() : start());
  if (!document.hidden) start();
}

const GEN_LABELS = { both: 'Generating audio + video', voice: 'Re-rendering voice', video: 'Re-rendering video' };
const gen = { total: 0, what: 'both' };   // just enough for setGenBusy()'s button targeting

function startJobsPolling() {
  pollWhileVisible(pollJobs, 1500);
}
async function pollJobs() {
  try {
    const res = await fetch('/api/jobs', { cache: 'no-store' });
    if (!res.ok) return;
    JOBS = (await res.json()).jobs || [];
    renderJobWidget();
    syncEditorJobUI();
    syncScriptJobUI();
    syncMergedJobs();
  } catch (_) { /* transient - the next tick retries; never throws into the caller */ }
}
function jobsForChapter(chapterId) { return JOBS.filter(j => j.chapter_id === chapterId); }

function setGenBusy(b) {
  btnBusy($('genAVBtn'), b && gen.what === 'both');
  btnBusy($('genVoiceBtn'), b && gen.what === 'voice');
  btnBusy($('genVidBtn'), b && gen.what === 'video');
  $('genAVBtn').disabled = b; $('genVoiceBtn').disabled = b; $('genVidBtn').disabled = b;
  $('genCancelBtn').disabled = !b;
  const gb = $('genbar'); if (gb) gb.classList.toggle('running', !!b);
  if (!b) clearGlobalProg();
}
function setGenBar(frac, indet, label) {
  const wrap = $('genProgress'), bar = $('genBar');
  wrap.style.display = 'block';
  setGlobalProg(indet ? null : Math.max(0, Math.min(1, frac)), label || GEN_LABELS[gen.what] || GEN_LABELS.both);
  wrap.classList.remove('err');
  wrap.classList.toggle('indet', !!indet);
  if (!indet) bar.style.width = Math.round(clamp(frac, 0, 1) * 100) + '%';
}
function setGenLog(lines) {
  const el = $('genStatus');
  el.textContent = (lines || []).join('\n');
  el.scrollTop = el.scrollHeight;
}

// Start a job. Returns immediately (job_id) - it does NOT stream; the poll
// loop above picks up its progress on the next tick (kicked off right away
// here too, so the UI doesn't wait up to 1.5s to look busy).
async function startGenerate(what) {
  if (!S.chapter_id) { setStatus('Load/Export a chapter first.', true); return; }
  if (jobsForChapter(S.chapter_id).some(j => j.status === 'queued' || j.status === 'running')) {
    setStatus('A generate job is already queued or running for this chapter.', true);
    return;
  }
  gen.what = what;
  clearGenResult(); $('genProgress').classList.remove('err');
  setGenLog([]); setGenBusy(true); setGenBar(0, true);
  try {
    const res = await fetch('/api/generate', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(scopeBody({ chapter: S.chapter_id, what })) });
    const d = await res.json();
    if (!res.ok) throw new Error(d.error || 'failed to start');
    S.watchedJobId = d.job_id;
    showToast(d.job && d.job.status === 'running' ? 'Started.' : 'Queued.', null, null, 'ok');
    pollJobs();
  } catch (e) {
    setGenBusy(false);
    setStatus('Generate error: ' + e.message, true);
  }
}

async function cancelGenerate() {
  const active = jobsForChapter(S.chapter_id).find(j => j.what !== 'script' && (j.status === 'queued' || j.status === 'running'));
  const jobId = S.watchedJobId || (active && active.id);
  if (!jobId) return;
  try {
    await fetch(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, { method: 'POST' });
  } catch (e) { setStatus('Stop error: ' + e.message, true); return; }
  showToast('Job stopped.', null, null, 'warn');
  pollJobs();
}

// videoUrl carries the file's mtime (see _video_url in app.py), so it only
// changes on a re-render. Called on every job poll, so an unchanged URL must
// leave the existing <video> alone - rebuilding it restarts playback (flicker).
function showGenResult(videoUrl) {
  const box = $('genResult');
  if (box.dataset.videoUrl === videoUrl) return;
  box.dataset.videoUrl = videoUrl;
  box.innerHTML =
    `<div class="jobcard">
       <div class="jobhead">${ic('check',15)}<b>Video ready</b></div>
       <video class="genvideo" src="${videoUrl}" controls preload="metadata"></video>
       <div class="jobacts">
         <a class="btnlink" href="${videoUrl}" target="_blank">${ic('play',14)}<span>Play full size</span></a>
         <a class="btnlink" href="${videoUrl}" download="recap.mp4">${ic('download',14)}<span>Download recap.mp4</span></a>
       </div>
     </div>`;
}
function clearGenResult() {
  const box = $('genResult');
  box.innerHTML = '';
  delete box.dataset.videoUrl;
}

// Reconciles the chapter editor's genbar with the ACTUAL job state for
// S.chapter_id on every poll tick - this (not any assumption made when the
// editor opened) is what fixes "shows rendering when it isn't". Three cases:
// (1) a job is queued/running for this chapter right now - reflect it live;
// (2) a job I was watching (started here, or found active when I opened this
//     chapter) just finished - announce it (toast/result), once; (3) neither -
//     make sure the UI is idle, and quietly show the last result if there is one.
function syncEditorJobUI() {
  if (SCREEN !== 'editor' || !S.chapter_id) return;
  const mine = jobsForChapter(S.chapter_id).filter(j => j.what !== 'script');
  const active = mine.find(j => j.status === 'queued' || j.status === 'running');

  if (active) {
    S.watchedJobId = active.id;
    gen.what = active.what; gen.total = active.total_beats;
    setGenBusy(true);
    setGenLog(active.log);
    const etaTxt = active.status === 'running' ? ' - ' + fmtEtaOrElapsed(active)
      : (active.eta_s != null ? ' - ' + fmtEta(active.eta_s) : '');
    if (active.status === 'queued') setGenBar(0, true, 'Queued' + etaTxt);
    // A not-yet-cached voice model is downloading (see stages/s6_tts.py) -
    // audio/video counters can't move yet, so a determinate bar would just
    // sit at a misleading 0%. Show the live message + a pulsing bar instead,
    // same as "Queued", so this never reads as frozen.
    else if (active.downloading_model) setGenBar(0, true, active.message || 'Downloading voice model…');
    else setGenBar(active.progress || 0, false, (GEN_LABELS[active.what] || GEN_LABELS.both) + etaTxt);
    return;
  }

  if (S.watchedJobId) {
    const finished = mine.find(j => j.id === S.watchedJobId);
    S.watchedJobId = null;
    if (finished) { setGenBusy(false); announceJobResult(finished); return; }
  }

  setGenBusy(false);
  const recent = mine.filter(j => j.status !== 'queued' && j.status !== 'running')
                      .sort((a, b) => (b.finished_at || '').localeCompare(a.finished_at || ''))[0];
  if (recent) {
    setGenLog(recent.log);
    if (recent.status === 'done' && recent.ok && recent.video_url) showGenResult(recent.video_url);
    else if (recent.status !== 'done') $('genProgress').classList.add('err');
  }
}

function announceJobResult(job) {
  setGenLog(job.log);
  if (job.status === 'done' && job.ok) {
    setGenBar(1, false);
    showToast(job.video_url ? 'Video ready.' : (job.message || 'Done.'), null, null, 'ok');
    if (job.video_url) showGenResult(job.video_url);
    setStatus(job.video_url ? 'Render complete - playing below.' : (job.message || 'Done.'));
  } else {
    $('genProgress').classList.add('err');
    const label = job.status === 'cancelled' ? 'Generation stopped.'
                : (job.message || 'Generation failed - see the log.');
    setStatus(label, job.status !== 'cancelled');
    if (job.status !== 'cancelled') showToast('Render failed.', null, null, 'err');
  }
}

function fmtEta(s) {
  if (s == null) return '';
  s = Math.max(0, Math.round(s));
  if (s < 5) return 'almost done';
  if (s < 60) return `~${s}s left`;
  const m = Math.floor(s / 60);
  if (m < 60) return `~${m}m ${s % 60}s left`;
  return `~${Math.floor(m / 60)}h ${m % 60}m left`;
}
function fmtElapsed(startedAtIso) {
  const s = Math.max(0, Math.round((Date.now() - new Date(startedAtIso).getTime()) / 1000));
  if (s < 60) return `${s}s elapsed`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s elapsed`;
  return `${Math.floor(m / 60)}h ${m % 60}m elapsed`;
}
// eta_s is a fixed estimate * (1 - progress) - before the first beat finishes,
// progress is exactly 0, so it reads as a frozen number no matter how long
// model loading takes. Elapsed time always ticks, so it never looks stuck;
// once real progress exists, the estimate below it is genuinely decreasing.
function fmtEtaOrElapsed(j) {
  if (j.progress > 0 && j.eta_s != null) return fmtEta(j.eta_s);
  return j.started_at ? fmtElapsed(j.started_at) : '';
}
function escHtml(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }

// ---------------------------------------------------------------------------
// global job widget - a corner card, OUTSIDE the screen containers (see the
// #jobWidget markup near </body>), so it's visible on every screen: projects
// list, chapter list, or the editor for a DIFFERENT chapter than the one
// actually rendering. Shows the running job's progress/ETA and the rest of
// the queue in order.
// ---------------------------------------------------------------------------
function jobLabel(j) {
  if (j.what === 'brand') return `Final render - ${j.target}`;
  if (j.what === 'script') return `Claude script - ${j.chapter_id}`;
  const w = { both: 'Audio + video', voice: 'Voice', video: 'Video' }[j.what] || 'Render';
  return `${w} - ${j.chapter_id}`;
}
function renderJobWidget() {
  const widget = $('jobWidget');
  const active = JOBS.filter(j => j.status === 'queued' || j.status === 'running');
  if (!active.length) { widget.classList.remove('on'); return; }
  widget.classList.add('on');
  const running = active.filter(j => j.status === 'running');
  const queued = active.filter(j => j.status === 'queued');
  let html = running.map(j => j.downloading_model || j.resume_at ? `
    <div class="jwjob running">
      <div class="jwrow"><span class="jwname">${escHtml(jobLabel(j))}</span></div>
      <div class="jwrow"><span class="jwname" style="font-weight:400">${escHtml(j.message || 'Downloading voice model…')}</span></div>
      ${j.claude ? `<div class="jwrow" style="font-size:11px;font-weight:400;display:block">${claudeStatsHtml(j)}</div>` : ''}
    </div>` : `
    <div class="jwjob running">
      <div class="jwrow"><span class="jwname">${escHtml(jobLabel(j))}</span><span class="jweta">${fmtEtaOrElapsed(j)}</span></div>
      <div class="jwtrack"><div class="jwfill" style="width:${Math.round((j.progress || 0) * 100)}%"></div></div>
      ${j.claude ? `<div class="jwrow" style="font-size:11px;font-weight:400;display:block">${claudeStatsHtml(j)}</div>` : ''}
    </div>`).join('');
  if (queued.length) {
    html += `<div class="jwqlabel">Queued (${queued.length})</div>` + queued.map(j => `
      <div class="jwjob queued">
        <div class="jwrow"><span class="jwname">${escHtml(jobLabel(j))}</span><span class="jweta">${fmtEta(j.eta_s)}</span></div>
      </div>`).join('');
  }
  $('jwBody').innerHTML = html;
}
$('jwCollapseBtn').onclick = () => {
  $('jwBody').classList.toggle('collapsed');
  const chev = $('jwCollapseBtn');
  chev.dataset.icon = $('jwBody').classList.contains('collapsed') ? 'chevup' : 'chevdown';
  chev.removeAttribute('data-ic'); chev.innerHTML = ''; paintIcons(chev.parentElement);
};

// Drag the widget by its header to move it off whatever it covers; the spot is
// remembered per browser.
(() => {
  const widget = $('jobWidget'), KEY = 'framer.jobWidgetPos';
  const place = (x, y) => {
    const w = widget.offsetWidth, h = widget.offsetHeight;
    Object.assign(widget.style, {
      width: w + 'px', right: 'auto', bottom: 'auto',
      left: Math.min(Math.max(0, x), Math.max(0, innerWidth - w)) + 'px',
      top: Math.min(Math.max(0, y), Math.max(0, innerHeight - h)) + 'px',
    });
  };
  try {
    const saved = JSON.parse(localStorage.getItem(KEY));
    if (saved) place(saved.x, saved.y);
  } catch (e) { /* storage unavailable: keep the default corner */ }

  $('jwHead').addEventListener('pointerdown', e => {
    if (e.target.closest('button')) return;
    const box = widget.getBoundingClientRect();
    const dx = e.clientX - box.left, dy = e.clientY - box.top;
    const head = e.currentTarget;
    head.setPointerCapture(e.pointerId);
    widget.classList.add('dragging');
    const move = ev => place(ev.clientX - dx, ev.clientY - dy);
    const end = () => {
      head.removeEventListener('pointermove', move);
      head.removeEventListener('pointerup', end);
      head.removeEventListener('pointercancel', end);
      widget.classList.remove('dragging');
      const { left, top } = widget.getBoundingClientRect();
      try { localStorage.setItem(KEY, JSON.stringify({ x: left, y: top })); } catch (err) { /* not persisted */ }
    };
    head.addEventListener('pointermove', move);
    head.addEventListener('pointerup', end);
    head.addEventListener('pointercancel', end);
  });

  addEventListener('resize', () => {
    if (widget.style.top) {
      const { left, top } = widget.getBoundingClientRect();
      place(left, top);
    }
  });
})();

// ---------------------------------------------------------------------------
// save / load project + periodic autosave (recovery)
// ---------------------------------------------------------------------------
function normLine(l) {
  return {
    text: l.text || '',
    duration_s: (l.duration_s ?? 4),
    pace: PACES.includes(l.pace) ? l.pace : null,
    mode: l.mode === 'together' ? 'together' : 'sequence',
    frames: (l.frames || []).map(f => ({ bbox: f.bbox, flip: !!f.flip, blur: blurEnabled() ? f.blur || [] : [] })),
  };
}
function projectObject() {
  return {
    chapter_id: S.chapter_id,
    source_pdf: S.source_pdf || null,
    strip_w: S.strip_w, strip_h: S.strip_h,
    preview_scale: S.preview_scale, preview_w: S.preview_w, preview_h: S.preview_h,
    active: S.active,
    lines: S.lines,
  };
}
function setSaveState(msg) { const el = $('saveState'); if (el) el.textContent = msg; }

async function saveEditorState(recovery, opts) {
  const silent = !!(opts && opts.silent);
  if (!S.chapter_id) { if (!recovery && !silent) setStatus('Load a PDF (or a project) first.', true); return; }
  // Autosave only persists real edits. A freshly opened editor is empty until
  // its saved state is restored, and writing that empty state would overwrite
  // the very work being restored.
  if (recovery) {
    if (!dirty) return;
    dirty = false;
  }
  try {
    const res = await fetch('/editor_state/save', { method:'POST', keepalive: true,
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify(scopeBody({ chapter_id: S.chapter_id, recovery: !!recovery, state: projectObject() })) });
    const d = await res.json();
    if (!res.ok) throw new Error(d.error || 'save failed');
    const t = new Date().toLocaleTimeString();
    if (recovery) setSaveState(`auto-saved ${t}`);
    else {
      setSaveState(`saved ${t}`);
      if (!silent) { setStatus('Editor state saved to ' + d.path); showToast('Saved.', null, null, 'ok'); }
    }
  } catch (e) {
    if (recovery) { dirty = true; setSaveState('autosave failed'); }
    else if (!silent) setStatus('Save error: ' + e.message, true);
  }
}

function applyStripPreview(preview) {
  S.chapter_id = preview.chapter_id;
  S.preview_scale = preview.preview_scale;
  S.strip_w = preview.strip_w; S.strip_h = preview.strip_h;
  S.preview_w = preview.preview_w; S.preview_h = preview.preview_h;
  S.tiles = preview.tiles;
  $('chapId').value = preview.chapter_id;
  buildTiles(); computeZoomRange(); setZoom(S.fitZoom); stage.scrollTop = 0;
  $('stripInfo').textContent =
    `strip ${preview.strip_w}×${preview.strip_h}px · preview ${preview.preview_w}×${preview.preview_h} (${preview.tiles.length} tiles)`;
}

function applyProject(project, preview) {
  if (preview) {
    applyStripPreview(preview);
  } else {
    S.chapter_id = project.chapter_id;
    setStatus('Project loaded, but no stitched strip found - re-stitch the PDF to box frames.', true);
  }
  S.source_pdf = project.source_pdf || null;
  S.lines = (project.lines || []).map(normLine);
  S.active = S.lines.length ? clamp(project.active ?? 0, 0, S.lines.length - 1) : -1;
  S.activeFrame = (S.active >= 0 && S.lines[S.active].frames.length) ? 0 : -1;
  undoStack = []; redoStack = []; updateUndoUI();
  renderLines(); redrawBoxes();
  discardPendingAutosave();
}

// Chapter Source / Stitch Strip panels' TRUE state, re-derived from disk (see
// /chapter_source_status) every time a chapter is opened AND right after a
// download/stitch finishes - never trusted to editor_state.json, which only
// ever held boxing data (lines/frames) and silently looked stale for this.
async function refreshChapterSourceStatus() {
  if (!S.chapter_id) return;
  try {
    const res = await fetch(`/chapter_source_status?chapter=${encodeURIComponent(S.chapter_id)}${scopeQS()}`);
    if (!res.ok) return;
    applyChapterSourceStatus(await res.json());
  } catch (_) { /* best-effort - buttons just keep their current state */ }
}

function applyChapterSourceStatus(d) {
  const dlBtn = $('dlBtn'), dlStatus = $('dlStatus');
  if (d.pdf) {
    $('pdfPath').value = d.pdf.path;
    S.source_pdf = d.pdf.path;
    dlBtn.disabled = true;
    dlStatus.hidden = false;
    dlStatus.innerHTML =
      `Downloaded: ${escHtml(d.pdf.name)} (${d.pdf.page_count}p)<br>` +
      `<span class="jobpath">${escHtml(d.pdf.path)}</span> ` +
      `<button type="button" class="btnlink" id="dlRedoBtn">${ic('refresh', 12)}<span>Re-download</span></button>`;
    $('dlRedoBtn').onclick = () => { dlBtn.disabled = false; dlStatus.hidden = true; };
  } else {
    dlBtn.disabled = false;
    dlStatus.hidden = true;
  }

  if (d.video_url) showGenResult(d.video_url);

  const stitchBtn = $('loadPdfBtn'), stitchStatus = $('stitchStatus');
  if (d.strip) {
    applyStripPreview(d.strip);
    stitchBtn.disabled = true;
    stitchStatus.hidden = false;
    stitchStatus.innerHTML =
      `Already stitched - ${d.strip.strip_w}×${d.strip.strip_h}px ` +
      `<button type="button" class="btnlink" id="stitchRedoBtn">${ic('refresh', 12)}<span>Re-stitch</span></button>`;
    $('stitchRedoBtn').onclick = () => { stitchBtn.disabled = false; stitchStatus.hidden = true; };
  } else {
    stitchBtn.disabled = false;
    stitchStatus.hidden = true;
  }
}

// `optional`: a chapter with nothing saved yet is expected (just opened), not an error.
async function loadEditorState(recovery, optional, narration) {
  const chap = ($('chapId').value.trim() || S.chapter_id || '');
  if (!chap) { setStatus('Enter a chapter id to load its saved state.', true); return; }
  showOverlay(recovery ? 'Restoring recovery' : 'Loading editor state');
  setGlobalProg(null, recovery ? 'Restoring recovery' : 'Loading editor state');
  try {
    const res = await fetch(`/editor_state/load?chapter=${encodeURIComponent(chap)}${recovery ? '&recovery=1' : ''}${narration ? '&narration=1' : ''}${scopeQS()}`);
    if (res.status === 404 && optional) return;
    const d = await res.json();
    if (!res.ok) throw new Error(d.error || 'load failed');
    applyProject(d.state, d.preview);
    await firstTilePromise();
    setStatus(`Loaded ${recovery ? 'recovery' : 'saved'} state for ${d.state.chapter_id} - ${S.lines.length} line(s).`);
    setSaveState(`loaded ${new Date().toLocaleTimeString()}`);
    showToast('Editor state loaded - ' + S.lines.length + ' lines.', null, null, 'ok');
  } catch (e) { setStatus('Load error: ' + e.message, true); }
  finally { clearGlobalProg(); hideOverlay(); }
}

// after a chapter is opened, offer to restore newer unsaved (recovery) work
async function maybeOfferRecovery(chap) {
  try {
    const res = await fetch(`/editor_state/status?chapter=${encodeURIComponent(chap)}${scopeQS()}`);
    const d = await res.json();
    if (d.recovery && d.recovery.exists &&
        (!d.project || !d.project.exists || d.recovery.mtime > d.project.mtime)) {
      showToast('Unsaved recovery found for this chapter.', 'Restore', () => loadEditorState(true));
    }
  } catch (_) { /* non-fatal */ }
}

// debounced autosave on every edit + a 30s backstop, both to the recovery file -
// only while the editor screen is actually open (see showScreen()).
let autosaveTimer = null;
let dirty = false;   // edits not yet written to recovery.json
function markDirty() {
  if (!S.chapter_id) return;
  dirty = true;
  setSaveState('unsaved…');
  clearTimeout(autosaveTimer);
  autosaveTimer = setTimeout(() => saveEditorState(true), 1500);
}
function discardPendingAutosave() {
  clearTimeout(autosaveTimer);
  dirty = false;
}
setInterval(() => { if (SCREEN === 'editor' && S.chapter_id) saveEditorState(true); }, 30000);
// closing the app within the debounce window must not lose the last edits
window.addEventListener('pagehide', () => { if (SCREEN === 'editor') saveEditorState(true); });

// ---------------------------------------------------------------------------
// theme (light / dark) + split position - both persisted SERVER-SIDE per session
// ---------------------------------------------------------------------------
let curTheme = 'light';
function applyTheme(name) {
  curTheme = name === 'light' ? 'light' : 'dark';
  document.body.classList.toggle('light', curTheme === 'light');
  const label = ic(curTheme === 'light' ? 'moon' : 'sun', 15) + '<span>' + (curTheme === 'light' ? 'Dark' : 'Light') + '</span>';
  ['themeBtn', 'themeBtnP'].forEach(id => { const b = $(id); if (b) b.innerHTML = label; });
}
function toggleTheme() { applyTheme(curTheme === 'light' ? 'dark' : 'light'); saveSetting({ theme: curTheme }); }

async function saveSetting(obj) {
  try { await fetch('/settings', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(obj) }); }
  catch (_) { /* non-fatal */ }
}
async function loadSettings() {
  try {
    const d = await (await fetch('/settings')).json();
    if (d.theme) applyTheme(d.theme);
    if (d.split) applySplit(d.split);
  } catch (_) { /* non-fatal */ }
}

// Wider drag range than the strip needs a hard minimum for: down to 240px
// (a narrow script-only view) and up to within 220px of the full window (the
// strip keeps just its rail + a sliver), so the split can go much wider than
// before while never fully swallowing either pane.
const SPLIT_MIN = 240;
function splitMax() { return Math.max(360, window.innerWidth - 220); }
function applySplit(px) {
  px = clamp(px, SPLIT_MIN, splitMax());
  $('right').style.width = px + 'px';
  if (S.preview_w) { computeZoomRange(); layout(); }
}

// drag the divider to resize the strip / script split (rAF-throttled), then persist.
// Pointer Events + setPointerCapture (rather than mousedown/mousemove/mouseup on
// window) so the drag survives the cursor leaving the divider - or the whole
// window - mid-gesture (fast drags, a trackpad, or the panel briefly covering
// the cursor): captured events keep targeting the divider regardless of where
// the pointer physically is, and a capture always gets a matching pointerup/
// pointercancel, so `resizing` can never get stuck.
let resizing = false, pendW = null, rafId = null;
function scheduleSplit(w) { pendW = w; if (rafId) return; rafId = requestAnimationFrame(() => { rafId = null; applySplit(pendW); }); }
function beginResize(e) {
  resizing = true; $('divider').classList.add('active');
  document.body.style.cursor = 'col-resize'; document.body.style.userSelect = 'none';
  try { $('divider').setPointerCapture(e.pointerId); } catch (_) { /* older browser - falls back to plain listeners */ }
  e.preventDefault();
}
function moveResize(e) { if (resizing) scheduleSplit(window.innerWidth - e.clientX); }
function endResize() {
  if (!resizing) return;
  resizing = false; $('divider').classList.remove('active');
  document.body.style.cursor = ''; document.body.style.userSelect = '';
  saveSetting({ split: parseInt($('right').style.width) || 460 });
}
$('divider').addEventListener('pointerdown', beginResize);
$('divider').addEventListener('pointermove', moveResize);
$('divider').addEventListener('pointerup', endResize);
$('divider').addEventListener('pointercancel', endResize);
// Fallback for browsers without Pointer Events (or if capture silently no-ops):
// window-level mouse listeners cover the same gesture as before.
window.addEventListener('mousemove', moveResize);
window.addEventListener('mouseup', endResize);

// ---------------------------------------------------------------------------
// keyboard navigation: ↑/↓ or j/k move the active line; Enter focuses its text;
// the strip scrolls to the active line's first frame.
// ---------------------------------------------------------------------------
function scrollActiveRowIntoView() {
  const r = document.querySelector(`.row[data-i="${S.active}"]`);
  if (r) r.scrollIntoView({ block:'nearest' });
}
function scrollToActiveFrame() {
  if (isStripLocked()) return;
  const ln = activeLine();
  if (!ln || !ln.frames.length || !S.tiles.length) return;
  const b = ln.frames[0].bbox;
  const cx = trueToWorld(b[0] + b[2] / 2), cy = trueToWorld(b[1] + b[3] / 2);
  stage.scrollTo({
    left: clamp(cx - stage.clientWidth / 2, 0, Math.max(0, world.offsetWidth - stage.clientWidth)),
    top:  clamp(cy - stage.clientHeight / 2, 0, Math.max(0, world.offsetHeight - stage.clientHeight)),
    behavior: 'smooth',
  });
}
function moveActive(d) {
  if (!S.lines.length) return;
  const ni = clamp((S.active < 0 ? 0 : S.active) + d, 0, S.lines.length - 1);
  if (ni !== S.active) { setActiveLine(ni); scrollActiveRowIntoView(); }
  scrollToActiveFrame();
}
function focusActive() {
  const r = document.querySelector(`.row[data-i="${S.active}"]`);
  const ta = r && r.querySelector('.ltext');
  if (ta) { ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); }
}
window.addEventListener('keydown', e => {
  if (SCREEN !== 'editor') return;   // j/k/Enter navigation is editor-only
  const t = e.target;
  if (t && (t.tagName === 'TEXTAREA' || t.tagName === 'INPUT')) {
    if (e.key === 'Escape') t.blur();
    return;
  }
  if (e.ctrlKey || e.metaKey || e.altKey) return;   // undo/redo handled elsewhere
  if (e.key === 'ArrowDown' || e.key === 'j') { e.preventDefault(); moveActive(1); }
  else if (e.key === 'ArrowUp' || e.key === 'k') { e.preventDefault(); moveActive(-1); }
  else if (e.key === 'Enter') { e.preventDefault(); focusActive(); }
});

// ---------------------------------------------------------------------------
// PROJECT SYSTEM - Projects landing screen, per-project chapter list +
// settings, and screen switching. The editor workspace above is unchanged; it
// is simply one of three top-level screens (see the <section id="scr-*">
// wrappers in the body). Navigation is deep-linked via ?project=&chapter=.
// ---------------------------------------------------------------------------
let SCREEN = 'projects';
let CURRENT_PROJECT = null;   // the project object backing the open Project screen

function showScreen(name) {
  SCREEN = name;
  ['projects', 'project', 'hub', 'settings', 'editor'].forEach(n => {
    const el = $('scr-' + n);
    if (el) el.classList.toggle('on', n === name);
  });
  if (name === 'editor') {
    // the workspace was hidden (display:none => 0 client size); its zoom/fit
    // math needs real geometry, so recompute once it is actually visible.
    requestAnimationFrame(() => { if (S.preview_w) { computeZoomRange(); layout(); redrawBoxes(); } });
  }
}

// ---------------------------------------------------------------------------
// fullscreen strip view - "read like a webtoon": everything but the stage is
// hidden via CSS (body.stripFullscreen), the zoom/scroll mechanics themselves
// are untouched (same #stage/#world, same S.zoom), just recomputed once the
// container actually resizes (display:none -> real size takes a layout tick).
// ---------------------------------------------------------------------------
function setFullscreen(on) {
  document.body.classList.toggle('stripFullscreen', on);
  const btn = $('fullscreenBtn');
  btn.innerHTML = ic(on ? 'minimize' : 'maximize', 14) + '<span>' + (on ? 'Exit fullscreen' : 'Fullscreen') + '</span>';
  requestAnimationFrame(() => { if (S.preview_w) { computeZoomRange(); layout(); redrawBoxes(); } });
}

// ---------------------------------------------------------------------------
// script-only view - hide the strip, let the script column fill/center
// itself for a cleaner read. The divider may have set an inline width on
// #right; stash it while script-only is on and restore it on the way out so
// the remembered split isn't lost.
// ---------------------------------------------------------------------------
let savedRightWidth = null;
function setScriptOnly(on) {
  document.body.classList.toggle('scriptOnlyView', on);
  $('scriptOnlyBtn').classList.toggle('on', on);
  if (on) {
    savedRightWidth = $('right').style.width;
    $('right').style.width = '';
  } else if (savedRightWidth !== null) {
    $('right').style.width = savedRightWidth;
    savedRightWidth = null;
  }
  requestAnimationFrame(() => { if (S.preview_w && !on) { computeZoomRange(); layout(); redrawBoxes(); } });
}

function navTo(params, push) {
  const qs = new URLSearchParams();
  if (params.project) qs.set('project', params.project);
  if (params.chapter) qs.set('chapter', params.chapter);
  if (params.view) qs.set('view', params.view);
  const url = location.pathname + (qs.toString() ? '?' + qs.toString() : '');
  if (push !== false) history.pushState(params, '', url);
  dispatchRoute(params);
}
function dispatchRoute(params) {
  if (params.project && params.chapter) openChapterEditor(params.project, params.chapter);
  else if (params.project && params.view === 'settings') openSettingsScreen(params.project);
  else if (params.project && (params.view === 'merged' || params.view === 'library')) openHubScreen(params.project, params.view);
  else if (params.project) openProjectScreen(params.project);
  else { showScreen('projects'); renderProjectsList(); }
}
function route() {
  const qs = new URLSearchParams(location.search);
  dispatchRoute({ project: qs.get('project'), chapter: qs.get('chapter'), view: qs.get('view') });
}
window.addEventListener('popstate', () => route());

// --- Projects landing screen -------------------------------------------------
// Coarse "Nh ago" / "N days ago" label from an ISO updated_at timestamp - purely
// cosmetic (sidebar "recent" list + project card), never used for any logic.
function timeAgo(iso) {
  if (!iso) return '';
  const ms = Date.now() - new Date(iso).getTime();
  if (!isFinite(ms) || ms < 0) return '';
  const m = Math.round(ms / 60000);
  if (m < 1) return 'just now';
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h ago`;
  const dd = Math.round(h / 24);
  return `${dd}d ago`;
}

// --- project export / import (see api_export_project / api_import_project in app.py) ---
async function loadBackgroundOptions() {
  const { ok, data } = await apiJson(`/api/background_options?project=${enc(CURRENT_PROJECT.slug)}`, 'GET');
  const options = ok ? data.options : [];
  $('bgOptionsBlock').hidden = !options.length;
  $('bgOptions').innerHTML = options.map(o => `<div class="bgopt ${o.id === data.current ? 'on' : ''}">` +
    `<video muted loop playsinline preload="metadata" src="/api/background_options/${enc(o.id)}#t=0.1"></video>` +
    `<div class="bgoptfoot"><span>${escHtml(o.label)}</span>` +
    `<button type="button" class="small ${o.id === data.current ? '' : 'ghost'}" data-bgopt="${escHtml(o.id)}" ${o.id === data.current ? 'disabled' : ''}>${o.id === data.current ? 'In use' : 'Use'}</button></div></div>`).join('');
}

$('bgOptions').addEventListener('mouseover', e => { const v = e.target.closest('video'); if (v) v.play(); });
$('bgOptions').addEventListener('mouseout', e => { const v = e.target.closest('video'); if (v) v.pause(); });
$('bgOptions').addEventListener('click', async e => {
  const video = e.target.closest('video'), use = e.target.closest('[data-bgopt]');
  if (video) { video.paused ? video.play() : video.pause(); return; }
  if (!use) return;
  const { ok, data } = await apiJson(projectApi('/background'), 'POST', { option: use.dataset.bgopt });
  if (!ok) { showToast(data.error || 'Could not use that video.', null, null, 'warn'); return; }
  await loadCurrentProject(CURRENT_PROJECT.slug);
  fillSettingsForm();
  loadBackgroundOptions();
  showToast('Background video changed.', null, null, 'ok');
});

async function saveDefaults() {
  if ($('pjSettingsSaved').classList.contains('saving')) { showToast('Wait until the settings finish saving.', null, null, 'warn'); return; }
  const body = "New projects will start with this project's settings and the custom files they use. The series brief is not included. " +
    "The defaults are saved in the app's defaults folder; commit and push it to share them through Check for updates.";
  if (!await askConfirm({ title: 'Make these your default settings?', body, confirmLabel: 'Save as defaults' })) return;
  const { ok, data } = await apiJson(projectApi('/defaults'), 'POST');
  showToast(ok ? 'Default settings saved.' : data.error || 'Could not save the defaults.', null, null, ok ? 'ok' : 'warn');
}

async function applyDefaults(allProjects) {
  const { ok, data } = await apiJson(allProjects ? '/api/defaults/changes' : `/api/defaults/changes?project=${enc(CURRENT_PROJECT.slug)}`, 'GET');
  if (!ok) { showToast(data.error || 'Could not compare with the defaults.', null, null, 'warn'); return; }
  if (!data.projects.length) { showToast(allProjects ? 'Every project already uses the default settings.' : 'This project already uses the default settings.', null, null, 'ok'); return; }
  const body = '<div class="defchanges">' + data.projects.map(p => `<div><b>${escHtml(p.name)}</b>` +
    p.changes.map(c => `<div>${escHtml(c.group)}, ${escHtml(c.name)}: ${escHtml(String(c.current))} &rarr; ${escHtml(String(c.default))}</div>`).join('') + '</div>').join('') + '</div>';
  const count = data.projects.length;
  if (!await askConfirm({ title: `Apply the default settings to ${count} project${count === 1 ? '' : 's'}?`, body, confirmLabel: 'Apply' })) return;
  const applied = await apiJson('/api/defaults/apply', 'POST', { projects: data.projects.map(p => p.slug) });
  if (!applied.ok) { showToast(applied.data.error || 'Could not apply the defaults.', null, null, 'warn'); return; }
  await loadCurrentProject(CURRENT_PROJECT.slug);
  fillSettingsForm();
  showToast('Default settings applied.', null, null, 'ok');
}

function exportProject() {
  const videos = $('exportVideos').checked ? 1 : 0;
  showToast('Preparing the export - the download starts when it is ready. Large projects can take a minute.', null, null, 'ok');
  window.location = `/api/projects/${encodeURIComponent(CURRENT_PROJECT.slug)}/export?videos=${videos}`;
}

async function importProject() {
  const input = $('importProjectFile');
  const file = input.files[0];
  input.value = '';
  if (!file) return;
  const btn = $('importProjectBtn');
  btnBusy(btn, true);
  setGlobalProg(null, 'Importing project');
  try {
    const form = new FormData();
    form.append('file', file);
    const res = await fetch('/api/projects/import', { method: 'POST', body: form });
    const d = await res.json();
    if (!res.ok) throw new Error(d.error || 'Import failed');
    showToast(`Imported "${d.project.name}".`, null, null, 'ok');
    navTo({ project: d.slug });
  } catch (e) {
    showToast('Import failed: ' + e.message, null, null, 'err');
  } finally {
    clearGlobalProg();
    btnBusy(btn, false);
  }
}

async function renderProjectsList() {
  loadProjectsFolder();
  const list = $('pList');
  const recent = $('recentList');
  list.innerHTML = '<div class="empty">Loading…</div>';
  try {
    const d = await (await fetch('/api/projects')).json();
    const items = d.projects || [];
    if (!items.length) {
      list.innerHTML = '<div class="empty">No projects yet - click "New project" above.</div>';
      recent.innerHTML = '';
      return;
    }
    list.innerHTML = '';
    items.forEach(p => {
      const done = (p.counts && ((p.counts.rendered || 0) + (p.counts.complete || 0))) || 0;
      const pct = p.chapters ? Math.round(100 * done / p.chapters) : 0;
      const card = document.createElement('button');
      card.className = 'pcard';
      card.innerHTML =
        `<div class="ptop"><span class="pinitial"></span>` +
        `<div class="ptexts"><span class="pname"></span><span class="purl"></span></div></div>` +
        `<div class="pbottom"><span class="ptrack"><span class="pfill" style="width:${pct}%"></span></span>` +
        `<div class="pmeta"><span class="pcount">${done}/${p.chapters} done</span>` +
        `<span class="popened">${escHtml(timeAgo(p.updated_at))}</span></div></div>`;
      card.querySelector('.pinitial').textContent = (p.name || '?').trim().charAt(0).toUpperCase();
      card.querySelector('.pname').textContent = p.name;
      card.querySelector('.purl').textContent = p.url || '(no URL set)';
      card.onclick = () => navTo({ project: p.slug });
      list.appendChild(card);
    });
    recent.innerHTML = '';
    items.slice(0, 4).forEach(p => {
      const done = (p.counts && ((p.counts.rendered || 0) + (p.counts.complete || 0))) || 0;
      const pct = p.chapters ? Math.round(100 * done / p.chapters) : 0;
      const item = document.createElement('button');
      item.className = 'recentitem';
      item.innerHTML = `<span class="rn"></span><span class="recenttrack"><span class="recentfill" style="width:${pct}%"></span></span>`;
      item.querySelector('.rn').textContent = p.name;
      item.onclick = () => navTo({ project: p.slug });
      recent.appendChild(item);
    });
  } catch (e) {
    list.innerHTML = '<div class="empty">Could not load projects.</div>';
    recent.innerHTML = '';
  }
}

async function loadProjectsFolder() {
  const d = await (await fetch('/api/app_settings')).json();
  $('npFolder').value = d.projects_folder;
}

async function saveProjectsFolder() {
  btnBusy($('npFolderBtn'), true);
  try {
    const res = await fetch('/api/app_settings', { method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ projects_folder: $('npFolder').value }) });
    const d = await res.json();
    if (!res.ok) throw new Error(d.error || 'failed');
    showToast(d.projects_folder ? `New projects go to ${d.projects_folder}` : "New projects go to the app's own folder.", null, null, 'ok');
    renderProjectsList();
  } catch (e) { setStatus('Projects folder error: ' + e.message, true); }
  finally { btnBusy($('npFolderBtn'), false); }
}

async function createProject() {
  const name = $('npName').value.trim();
  const url = $('npUrl').value.trim();
  if (!name) { setStatus('Enter a project name.', true); return; }
  btnBusy($('npCreateBtn'), true);
  $('npProg').classList.add('on');
  $('npFetchResult').classList.remove('on', 'ok', 'warn');
  $('npManualRow').style.display = 'none';
  try {
    const res = await fetch('/api/projects', { method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ name, url }) });
    const d = await res.json();
    if (!res.ok) throw new Error(d.error || 'create failed');
    if (d.fetch && d.fetch.ok) {
      $('npFetchResult').textContent = `Found ${d.fetch.total} chapter(s) - "${d.fetch.title}".`;
      $('npFetchResult').classList.add('on', 'ok');
      $('npName').value = ''; $('npUrl').value = '';
      setTimeout(() => navTo({ project: d.slug }), 600);
    } else {
      $('npFetchResult').textContent = (d.fetch && d.fetch.reason) || "Couldn't read the series page.";
      $('npFetchResult').classList.add('on', 'warn');
      $('npManualRow').style.display = 'flex';
      $('npManualRow').dataset.slug = d.slug;
    }
  } catch (e) { setStatus('Create project error: ' + e.message, true); }
  finally { btnBusy($('npCreateBtn'), false); $('npProg').classList.remove('on'); }
}

async function useManualRange() {
  const slug = $('npManualRow').dataset.slug;
  if (!slug) return;
  const from = parseInt($('npFrom').value, 10) || 1;
  const to = parseInt($('npTo').value, 10) || from;
  btnBusy($('npManualBtn'), true);
  try {
    const res = await fetch(`/api/projects/${encodeURIComponent(slug)}/chapters`, { method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({ from, to }) });
    const d = await res.json();
    if (!res.ok) throw new Error(d.error || 'failed');
    navTo({ project: slug });
  } catch (e) { setStatus('Chapter range error: ' + e.message, true); }
  finally { btnBusy($('npManualBtn'), false); }
}

// --- Project screen: chapters -------------------------------------------------
// Shared by the project (chapters) screen and the settings screen - both show
// the same sidebar project title + "Chapters" nav count, and both need
// CURRENT_PROJECT loaded (fetching it again is cheap and keeps each screen
// independently deep-linkable / refreshable via the URL).
let projectLoadedAt = 0;
const PROJECT_FRESH_MS = 60_000;   // coming back from settings within this long reuses the loaded project
const CHAPTER_PAGE = 50;           // chapters shown (and detailed by the server) at a time
let chaptersShown = CHAPTER_PAGE;

async function loadCurrentProject(slug) {
  const d = await (await fetch(`/api/projects/${encodeURIComponent(slug)}?limit=${chaptersShown}`)).json();
  if (!d.project) throw new Error(d.error || 'not found');
  CURRENT_PROJECT = d.project;
  projectLoadedAt = Date.now();
  return CURRENT_PROJECT;
}

function paintProjectChrome() {
  const p = CURRENT_PROJECT;
  paintBlurTool();
  const chapters = p.chapters || [];
  const done = chapters.filter(c => c.status === 'complete').length;
  ['pjSideTitle', 'stSideTitle', 'hubSideTitle'].forEach(id => { const el = $(id); if (el) el.textContent = p.name; });
  ['pjChapNavCount', 'stChapNavCount', 'hubChapNavCount'].forEach(id => { const el = $(id); if (el) el.textContent = String(chapters.length); });
  if ($('pjSideProgress')) $('pjSideProgress').textContent = `${done}/${chapters.length}`;
  const usage = p.claude_usage;
  if ($('pjClaudeUsage')) $('pjClaudeUsage').innerHTML = usage && usage.output
    ? `Claude tokens: ${tokenLine(usage)}<br>${costLine(usage)}` : '';
  if ($('pjSideProgressFill')) $('pjSideProgressFill').style.width = (chapters.length ? Math.round(100 * done / chapters.length) : 0) + '%';
  $('pjMoreBtn').hidden = !p.site;
  const src = $('pjSourceLink');
  if (src) {
    if (p.url) { src.hidden = false; $('pjSourceLabel').textContent = shortHost(p.url); src.onclick = () => window.open(p.url, '_blank'); }
    else src.hidden = true;
  }
}

function shortHost(url) {
  try { return new URL(url).hostname.replace(/^www\./, ''); } catch (_) { return url; }
}

async function openProjectScreen(slug) {
  const cached = CURRENT_PROJECT && CURRENT_PROJECT.slug === slug;
  if (!cached) chaptersShown = CHAPTER_PAGE;
  showScreen('project');
  if (!cached) {
    $('pjTitle').textContent = 'Loading…';
    $('chList').innerHTML = '<div class="empty">Loading…</div>';
  }
  SELECTION.clear(); selectionAnchor = null; BATCH = null; batchStepCount = null; BATCH_LOG_VIEW.clear();
  CHAPTER_VIEW = loadChapterView(slug);
  $('batchCard').hidden = true;
  $('mergeStatus').textContent = ''; $('mergeResult').innerHTML = '';
  try {
    if (!cached) await loadCurrentProject(slug);
    $('pjTitle').textContent = CURRENT_PROJECT.name;
    paintProjectChrome();
    paintFolds();
    renderChapterList();
    pollBatch();
    if (cached && Date.now() - projectLoadedAt > PROJECT_FRESH_MS) {
      await loadCurrentProject(slug);
      paintProjectChrome();
      renderChapterList();
    }
  } catch (e) {
    setStatus('Open project error: ' + e.message, true);
    $('chList').innerHTML = '<div class="empty">Could not load this project.</div>';
  }
}

// --- Pipeline queue (batch automation, see batch.py) ------------------------------
const BATCH_STAGES = ['download', 'stitch', 'script', 'export', 'review', 'audio', 'video'];
const WINDOW_LABELS = { five_hour: '5-hour', seven_day: 'weekly' };
let BATCH = null;   // the last /batch payload: {batch, usage, thresholds}

const foldKey = () => `recapper.folded.${CURRENT_PROJECT.slug}`;

function readFolded() {
  try { return JSON.parse(localStorage.getItem(foldKey())) || []; } catch (_) { return []; }
}

function paintFolds() {
  const folded = readFolded();
  document.querySelectorAll('[data-fold]').forEach(btn => {
    const card = $(btn.dataset.fold), isFolded = folded.includes(btn.dataset.fold);
    card.classList.toggle('folded', isFolded);
    btn.setAttribute('aria-expanded', String(!isFolded));
    btn.title = `${isFolded ? 'Expand' : 'Collapse'} the ${btn.dataset.fold === 'batchCard' ? 'pipeline' : 'chapters'}`;
  });
}

function toggleFold(id) {
  const folded = readFolded();
  const next = folded.includes(id) ? folded.filter(x => x !== id) : [...folded, id];
  try { localStorage.setItem(foldKey(), JSON.stringify(next)); } catch (_) { /* the fold just isn't remembered */ }
  paintFolds();
}

// A folded section still says the pipeline is working.
function paintWorking() {
  const data = BATCH && BATCH.batch;
  const text = batchActive() ? `Pipeline ${data.status === 'paused' ? 'paused' : 'working'}: ${data.chapters.filter(c => c.state === 'done').length} of ${data.chapters.length} chapters done` : '';
  document.querySelectorAll('[data-working]').forEach(el => { el.hidden = !text; el.textContent = text; });
}

document.querySelectorAll('[data-fold]').forEach(btn => { btn.onclick = () => toggleFold(btn.dataset.fold); });

const batchActive = () => !!BATCH && !!BATCH.batch && ['running', 'paused'].includes(BATCH.batch.status);

async function pollBatch() {
  if (SCREEN !== 'project' || !CURRENT_PROJECT) return;
  try {
    const res = await fetch(projectApi('/batch'), { cache: 'no-store' });
    if (!res.ok) return;
    BATCH = await res.json();
    renderBatch();
  } catch (_) { /* the next tick retries */ }
}
pollWhileVisible(pollBatch, 2000);

async function batchCall(path, body) {
  const { ok, data } = await apiJson(projectApi('/batch' + path), 'POST', body);
  if (!ok) showToast(data.error || 'Request failed.', null, null, 'warn');
  pollBatch();
  return ok;
}

// Clocks tick every second without redrawing the card: data-since/-until/-base count up,
// data-left counts down to an epoch time.
function tickBatchClocks() {
  document.querySelectorAll('#batchCard [data-since]').forEach(el => {
    const until = el.dataset.until ? Date.parse(el.dataset.until) : Date.now();
    el.textContent = fmtClock(Math.max(0, (until - Date.parse(el.dataset.since)) / 1000 + Number(el.dataset.base || 0)));
  });
  document.querySelectorAll('#batchCard [data-left]').forEach(el => {
    el.textContent = fmtClock(Math.max(0, Number(el.dataset.left) - Date.now() / 1000));
  });
}
pollWhileVisible(tickBatchClocks, 1000);

const timeOfDay = epoch => new Date(epoch * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
function dayAndTime(epoch) {
  const when = new Date(epoch * 1000);
  return when.toDateString() === new Date().toDateString() ? timeOfDay(epoch)
    : when.toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' });
}

function statTile(label, value, sub) {
  return `<div class="bstat"><span class="k">${label}</span><span class="v">${value}</span><span class="s">${sub || '&nbsp;'}</span></div>`;
}

function claudeTotals(chapters) {
  return chapters.reduce((sum, c) => {
    if (c.claude) for (const k of ['input', 'output', 'seconds', 'cost_usd']) sum[k] += c.claude[k] || 0;
    return sum;
  }, { input: 0, output: 0, seconds: 0, cost_usd: 0 });
}

function batchStatsHtml(data) {
  const live = ['running', 'paused'].includes(data.status);
  const done = data.chapters.filter(c => c.state === 'done').length;
  const failed = data.chapters.filter(c => c.state === 'failed').length;
  const claude = claudeTotals(data.chapters);
  const clock = `<span data-since="${data.created_at}" data-until="${data.finished_at || ''}"></span>`;
  const left = live && data.finish_at
    ? statTile('Time left', `<span data-left="${data.finish_at}"></span>`,
        `done about ${dayAndTime(data.finish_at)}${data.hold ? ' · includes the usage wait' : ''}`)
    : statTile('Time left', '-', live ? 'working it out' : '');
  return statTile('Pipeline clock', clock, live ? 'since you started it' : 'total time') + left +
    statTile('Chapters', `${done}/${data.chapters.length}`, failed ? `${failed} failed` : 'complete') +
    statTile('Claude', fmtTokens(claude.input + claude.output), claude.seconds
      ? `${fmtTokens(claude.output)} out · ${clockSeconds(claude.seconds)} of Claude time` : 'tokens so far');
}

function meterHtml(window, usage, limit) {
  const u = usage[window];
  if (!u) return `<div class="bmeter"><div class="top"><span><b>${WINDOW_LABELS[window]}</b> usage</span><span>no reading yet</span></div><div class="track"></div></div>`;
  const hot = u.percent >= limit;
  return `<div class="bmeter"><div class="top"><span><b>${WINDOW_LABELS[window]}</b> usage ${Math.round(u.percent)}%</span>` +
    `<span>limit ${limit}% · resets ${dayAndTime(u.resets_at)} (in <span data-left="${u.resets_at}"></span>)</span></div>` +
    `<div class="track"><div class="fill ${hot ? 'hot' : ''}" style="width:${Math.min(100, u.percent)}%"></div>` +
    `<div class="limit" style="left:${limit}%"></div></div></div>`;
}

function lanesHtml(data) {
  return BATCH_STAGES.map(stage => {
    const here = data.chapters.filter(c => c.stage === stage);
    const running = here.find(c => c.state === 'running' || c.state === 'waiting');
    const queued = here.filter(c => c.state === 'queued').length;
    return `<span class="blane ${running ? 'busy' : ''}"><b>${stage}</b> ${running ? '#' + running.n : '-'}${queued ? ` · ${queued} waiting` : ''}</span>`;
  }).join('');
}

function holdText(hold) {
  const reached = hold.windows.map(w => `${WINDOW_LABELS[w.window]} usage is ${Math.round(w.percent)}% (limit ${w.threshold}%)`).join('; ');
  const resumes = `<span data-left="${hold.resume_at}"></span> (at ${dayAndTime(hold.resume_at)})`;
  return hold.choice === 'wait'
    ? `Claude work is on hold: ${reached}. ${hold.automatic ? 'Waiting automatically' : 'Waiting'} for the reset; the pipeline resumes by itself in ${resumes}. Chapters already past Claude keep going.`
    : `Claude work is on hold: ${reached}. Wait for the reset (in ${resumes}) or continue anyway?`;
}

function stageSeconds(c, stage) {
  return c.stage_seconds[stage] || 0;
}

function stageChips(c) {
  const stages = BATCH_STAGES.filter(s => s !== 'review' || c.stage === 'review' || c.completed.includes('review'));
  return stages.map(s => {
    const running = c.state === 'running' && s === c.stage && c.stage_started_at;
    const cls = c.completed.includes(s) ? 'done' : s === c.stage ? ({ running: 'running', waiting: 'waiting', failed: 'failed' }[c.state] || '') : '';
    const time = running ? ` · <span data-since="${c.stage_started_at}" data-base="${stageSeconds(c, s)}"></span>`
      : stageSeconds(c, s) ? ` · ${clockSeconds(stageSeconds(c, s))}` : '';
    if (s === 'review' || c.state === 'running') return `<span class="bstage ${cls}">${s}${time}</span>`;
    return `<button class="bstage rerun ${cls}" data-stage="${s}" title="Choose what to run, starting with ${s}">${s}${time} ↻</button>`;
  }).join('');
}

const BATCH_LOG_VIEW = new Map();   // chapter -> {open, scrollTop, stick}: survives the card being redrawn

function batchLogHtml(c) {
  const view = BATCH_LOG_VIEW.get(c.n);
  const open = view ? view.open : ['running', 'failed'].includes(c.state);
  return `<details class="blog" ${open ? 'open' : ''}><summary>Log (${c.log.length} lines)</summary>` +
         `<pre>${escHtml(c.log.join('\n'))}</pre></details>`;
}

function rememberBatchLogs() {
  $('batchList').querySelectorAll('.bchap').forEach(row => {
    const details = row.querySelector('.blog'), pre = row.querySelector('.blog pre');
    BATCH_LOG_VIEW.set(+row.dataset.n, { open: details.open, scrollTop: pre.scrollTop,
      stick: pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 8 });
  });
}

function restoreBatchLogs() {
  $('batchList').querySelectorAll('.bchap').forEach(row => {
    const view = BATCH_LOG_VIEW.get(+row.dataset.n), pre = row.querySelector('.blog pre');
    pre.scrollTop = !view || view.stick ? pre.scrollHeight : view.scrollTop;
  });
}

function chapterTimeHtml(c) {
  const spent = Object.values(c.stage_seconds).reduce((a, b) => a + b, 0);
  const active = c.state === 'running' && c.stage_started_at
    ? `<span data-since="${c.stage_started_at}" data-base="${spent}"></span>` : clockSeconds(spent);
  const eta = c.finish_at && c.state !== 'done' ? ` · done about ${dayAndTime(c.finish_at)}` : '';
  return `<span class="btime">working time <b>${active}</b>${eta}</span>`;
}

function claudeLine(u) {
  return u ? `<div class="bclaude">Claude: ${fmtTokens(u.input)} in · ${fmtTokens(u.output)} out · ${clockSeconds(u.seconds)}` +
    ` · about $${u.cost_usd.toFixed(2)} at API prices (not charged on a subscription)</div>` : '';
}

function batchChapterHtml(c) {
  const ch = CURRENT_PROJECT.chapters.find(x => x.n === c.n);
  const job = c.job;
  const detail = c.state === 'waiting' ? 'Waiting for your frame review. Fix the boxes in the editor, export again if you changed anything, then continue.'
    : c.state === 'running' ? (job && job.claude ? claudeStatsHtml(job) : escHtml((job && job.message) || c.message || `${c.stage}…`))
    : c.state === 'done' ? 'Done.' : c.state === 'cancelled' ? 'Cancelled.' : c.state === 'queued' ? `Waiting for ${c.stage}.` : '';
  const progress = c.state === 'running' && job && job.status === 'running' ? Math.round((job.progress || 0) * 100) : null;
  const acts = [];
  if (c.state === 'failed') acts.push(`<button class="small primary" data-act="retry" data-stage="${c.stage}">Retry ${escHtml(c.stage)}</button>`);
  if (c.state === 'waiting') acts.push(`<button class="small primary" data-act="continue">Continue to audio</button>`);
  if (['waiting', 'failed'].includes(c.state) || (c.state === 'running' && job && job.claude)) {
    acts.push(`<button class="small ghost" data-act="open">${c.state === 'running' ? 'Watch live' : 'Open editor'}</button>`);
  }
  return `<div class="bchap" data-n="${c.n}">
    <div class="bchaphead"><span class="chno">#${c.n}</span><span class="btitle">${escHtml(ch ? ch.title : '')}</span>
      <span class="stpill st-${c.state === 'done' ? 'complete' : 'downloaded'}">${c.state === 'waiting' ? 'awaiting review' : c.state}</span>${chapterTimeHtml(c)}</div>
    <div class="bstages">${stageChips(c)}</div>
    ${progress === null ? '' : `<div class="btrack"><div style="width:${progress}%"></div></div>`}
    <div class="bdetail">${detail}</div>
    ${c.warnings.map(w => `<div class="bwarn">⚠ ${escHtml(w)}</div>`).join('')}
    ${c.error ? `<div class="berror">${escHtml(c.error)}</div>` : ''}
    ${claudeLine(c.claude)}
    ${acts.length ? `<div class="bacts">${acts.join('')}</div>` : ''}
    ${batchLogHtml(c)}
  </div>`;
}

function renderBatch() {
  const data = BATCH && BATCH.batch;
  $('batchCard').hidden = !data;
  paintWorking();
  paintActionStates();
  if (!data) return;
  const pill = $('batchPill');
  pill.textContent = data.hold ? 'waiting for usage reset' : data.status;
  pill.className = `bpill ${data.hold ? 'paused' : data.status}`;
  $('batchStats').innerHTML = batchStatsHtml(data);
  $('batchMeters').innerHTML = ['five_hour', 'seven_day'].map(w => meterHtml(w, BATCH.usage, BATCH.thresholds[w === 'five_hour' ? 'five_hour_pct' : 'seven_day_pct'])).join('');
  $('batchLanes').innerHTML = lanesHtml(data);
  $('batchPauseBtn').querySelector('span').textContent = data.status === 'paused' ? 'Resume' : 'Pause';
  $('batchPauseBtn').disabled = $('batchCancelBtn').disabled = !batchActive();
  $('batchHold').hidden = !data.hold;
  if (data.hold) {
    $('batchHoldText').innerHTML = holdText(data.hold);
    $('batchWaitBtn').hidden = data.hold.choice === 'wait';
  }
  rememberBatchLogs();
  $('batchList').innerHTML = data.chapters.map(batchChapterHtml).join('');
  restoreBatchLogs();
  tickBatchClocks();
  $('batchList').querySelectorAll('[data-act]').forEach(btn => {
    const n = +btn.closest('.bchap').dataset.n;
    btn.onclick = () => {
      if (btn.dataset.act === 'open') navTo({ project: CURRENT_PROJECT.slug, chapter: n });
      else if (btn.dataset.act === 'retry') openPipelineModal([n], PIPELINE_STEPS.indexOf(btn.dataset.stage));
      else batchCall(`/chapters/${n}/continue`);
    };
  });
  $('batchList').querySelectorAll('.bstage.rerun').forEach(chip => {
    const n = +chip.closest('.bchap').dataset.n, stage = chip.dataset.stage;
    chip.onclick = () => openPipelineModal([n], PIPELINE_STEPS.indexOf(stage));
  });
  refreshProjectWhenStepsFinish(data);
}

let batchStepCount = null;   // finished steps seen by the last poll; null until the first poll sets the baseline
async function refreshProjectWhenStepsFinish(data) {
  const finished = data.chapters.reduce((sum, c) => sum + c.completed.length, 0);
  if (batchStepCount !== null && finished !== batchStepCount) {
    await loadCurrentProject(CURRENT_PROJECT.slug);
    renderChapterList();
  }
  batchStepCount = finished;
}

const PREFLIGHT_MARKS = { ok: '✓', warn: '!', fail: '✕' };

async function startPipeline() {
  const plans = Object.fromEntries(PIPE.rows.map(pipePlan).filter(plan => plan.runs.length).map(plan => [plan.ch.n, plan.runs]));
  const steps = [...new Set(Object.values(plans).flat())];
  savePipeChoice();
  closePipeModal();
  const { ok, data } = await apiJson(projectApi(`/batch/preflight?chapters=${Object.keys(plans).join(',')}&steps=${steps.join(',')}`), 'GET');
  if (!ok) { showToast(data.error || 'Preflight check failed.', null, null, 'warn'); return; }
  if (data.checks.some(c => c.status !== 'ok')) {
    const blocked = data.checks.some(c => c.status === 'fail');
    const rows = data.checks.map(c => `<div class="pfrow ${c.status}"><span class="pfmark">${PREFLIGHT_MARKS[c.status]}</span>` +
      `<span><b>${escHtml(c.name)}</b><span>${escHtml(c.detail)}</span></span></div>`).join('');
    const go = await askConfirm({ title: blocked ? 'Fix these before starting' : 'Check before you start', body: rows,
                                  confirmLabel: blocked ? 'Close' : 'Run anyway' });
    if (blocked || !go) return;
  }
  if (await batchCall('', { plans })) {
    SELECTION.clear(); selectionAnchor = null;
    renderChapterList();
  }
}

$('batchList').addEventListener('toggle', rememberBatchLogs, true);
$('batchRunBtn').onclick = () => openPipelineModal([...SELECTION]);
$('batchPauseBtn').onclick = () => batchCall(BATCH.batch.status === 'paused' ? '/resume' : '/pause');
$('batchCancelBtn').onclick = async () => {
  if (await askConfirm({ title: 'Cancel the pipeline?', body: 'Running steps are stopped. Finished steps are kept, and you can run any step again from the chapter cards.', confirmLabel: 'Cancel pipeline' })) batchCall('/cancel');
};
$('batchWaitBtn').onclick = () => batchCall('/usage', { choice: 'wait' });
$('batchContinueBtn').onclick = () => batchCall('/usage', { choice: 'continue' });

// --- Merged videos + media library (scr-hub) -----------------------------------
// Two views of one project, sharing a sidebar: the on-disk list of merged videos
// (with intro/outro branding) and the media library (space used, deleting
// regenerable files). Everything shown is read from the server, i.e. from disk.
let HUB_VIEW = 'merged';
const enc = encodeURIComponent;
const projectApi = path => `/api/projects/${enc(CURRENT_PROJECT.slug)}${path}`;

async function apiJson(url, method, body) {
  const res = await fetch(url, { method, headers: { 'Content-Type': 'application/json' },
                                 body: body === undefined ? undefined : JSON.stringify(body) });
  return { ok: res.ok, data: await res.json().catch(() => ({})) };
}

function askConfirm({ title, body, confirmLabel }) {
  return new Promise(resolve => {
    $('cfmTitle').textContent = title;
    $('cfmBody').innerHTML = body;
    $('cfmOk').firstElementChild.textContent = confirmLabel;
    const modal = $('confirmModal');
    modal.classList.add('on');
    const finish = answer => {
      modal.classList.remove('on');
      $('cfmOk').onclick = $('cfmCancel').onclick = null;
      resolve(answer);
    };
    $('cfmOk').onclick = () => finish(true);
    $('cfmCancel').onclick = () => finish(false);
  });
}

function fmtClock(seconds) {
  const s = Math.round(seconds);
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), sec = String(s % 60).padStart(2, '0');
  return h ? `${h}h ${String(m).padStart(2, '0')}m ${sec}s` : `${m}m ${sec}s`;
}

const isBrandJob = j => j.what === 'brand' && CURRENT_PROJECT && j.project === CURRENT_PROJECT.slug;
const isRunningJob = j => j.status === 'queued' || j.status === 'running';

async function openHubScreen(slug, view) {
  JOBS.filter(j => j.what === 'brand' && !isRunningJob(j)).forEach(j => brandedHandled.add(j.id));
  showScreen('hub');
  HUB_VIEW = view;
  $('hubMerged').hidden = view !== 'merged';
  $('hubLibrary').hidden = view !== 'library';
  $('hubViewCrumb').textContent = view === 'merged' ? 'Merged videos' : 'Media library';
  document.querySelectorAll('#scr-hub [data-hubview]')
    .forEach(a => a.classList.toggle('on', a.dataset.hubview === view));
  try {
    await loadCurrentProject(slug);
    $('hubCrumbProject').textContent = CURRENT_PROJECT.name;
    paintProjectChrome();
    if (view === 'merged') await loadMerged(); else await loadLibrary();
  } catch (e) {
    setStatus(`Open ${view} error: ${e.message}`, true);
  }
}

// --- merged videos -------------------------------------------------------------
let MERGED = { merges: [], branding: {}, default_clips: {}, folder: '' };
const BRAND_KINDS = ['intro', 'outro'];
const brandedHandled = new Set();   // finished final-render jobs already announced

async function loadMerged() {
  MERGED = await (await fetch(projectApi('/merges'))).json();
  renderMerged();
}

function renderMerged() {
  const list = $('mergedList');
  if (!MERGED.merges.length) {
    list.innerHTML = '<div class="empty">No merged videos yet. Tick two or more complete chapters on the Chapters screen and press "Merge selected videos".</div>';
    return;
  }
  list.innerHTML = '';
  MERGED.merges.forEach(m => {
    const card = document.createElement('div');
    card.className = 'mcard';
    card.dataset.name = m.name;
    card.innerHTML = mergedCardHtml(m);
    wireMergedCard(card, m);
    list.appendChild(card);
  });
  paintIcons(list);
  syncMergedJobs();
}

function brandRowHtml(kind) {
  const cfg = MERGED.branding[kind];
  const label = cfg.file ? `Custom: ${cfg.file.split('/').pop()}` : `Default: ${MERGED.default_clips[kind]}`;
  return `<div class="brandrow" data-kind="${kind}">
    <label class="togglesw"><input type="checkbox" class="brandToggle" ${cfg.enabled ? 'checked' : ''} /><span class="tk"></span><span>Add ${kind}</span></label>
    <span class="brandclip" title="${escAttr(label)}">${escHtml(label)}</span>
    <div class="brandacts">
      <button class="small ghost brandPreview" data-icon="play" data-ic-size="13"><span>Preview</span></button>
      <button class="small ghost brandBrowse" data-icon="upload" data-ic-size="13"><span>Upload custom</span></button>
      ${cfg.file ? '<button class="small ghost brandReset" data-icon="refresh" data-ic-size="13" title="Go back to the default clip"><span>Use default</span></button>' : ''}
      <input type="file" class="filehidden brandFile" accept="video/*" />
    </div>
  </div>`;
}

function mergedCardHtml(m) {
  const meta = `${fmtClock(m.duration)} · ${fmtSize(m.size)} · ${new Date(m.created_at).toLocaleString()}`;
  const final = m.final ? `
    <div class="mfinal">
      <div class="mtop"><b>Branded version</b>
        <span class="mmeta">${fmtClock(m.final.duration)} · ${fmtSize(m.final.size)}</span>
        <div class="macts">
          <a class="btnlink" href="${m.final.url}" download>${ic('download', 14)}<span>Download</span></a>
          <button class="small ghost danger finalDelete" data-icon="trash" data-ic-size="13"><span>Delete</span></button>
        </div>
      </div>
      <video controls preload="metadata" src="${m.final.url}?v=${Date.parse(m.final.created_at)}"></video>
    </div>` : '';
  return `
    <div><video controls preload="metadata" src="${m.url}"></video></div>
    <div>
      <div class="mtop">
        <b>Chapters ${m.chapters.join(', ') || '?'}</b><span class="mmeta">${meta}</span>
        <div class="macts">
          <button class="small ghost mergeFolder" data-icon="folder" data-ic-size="13"><span>Open folder</span></button>
          <button class="small ghost danger mergeDelete" data-icon="trash" data-ic-size="13"><span>Delete</span></button>
        </div>
      </div>
      <div class="mbrand">
        ${BRAND_KINDS.map(brandRowHtml).join('')}
        <div class="mfinalrow">
          <button class="primary finalRender" data-icon="film" data-ic-size="14"><span>${m.final ? 'Re-render final' : 'Render final'}</span></button>
          <button class="small danger finalStop" data-icon="stop" data-ic-size="13" hidden><span>Stop</span></button>
          <div class="progress"><div class="bar"></div></div>
          <span class="hint finalState"></span>
        </div>
        <span class="hint finalMsg"></span>
      </div>
      ${final}
    </div>`;
}

function wireMergedCard(card, m) {
  card.querySelectorAll('.brandrow').forEach(row => {
    const kind = row.dataset.kind;
    const input = row.querySelector('.brandFile');
    row.querySelector('.brandToggle').onchange = e => setBrandEnabled(kind, e.target.checked);
    row.querySelector('.brandPreview').onclick = () => toggleBrandPreview(row, kind);
    row.querySelector('.brandBrowse').onclick = () => input.click();
    input.onchange = () => uploadBrandClip(kind, input.files[0], row);
    const reset = row.querySelector('.brandReset');
    if (reset) reset.onclick = () => resetBrandClip(kind);
  });
  card.querySelector('.finalRender').onclick = () => startFinalRender(m.name);
  card.querySelector('.finalStop').onclick = () => cancelFinalRender(m.name);
  card.querySelector('.mergeFolder').onclick = () => openFolder(MERGED.folder);
  card.querySelector('.mergeDelete').onclick = () => deleteMerged(m, false);
  const deleteFinal = card.querySelector('.finalDelete');
  if (deleteFinal) deleteFinal.onclick = () => deleteMerged(m, true);
}

function toggleBrandPreview(row, kind) {
  const open = row.nextElementSibling;
  if (open && open.classList.contains('brandpreview')) { open.remove(); return; }
  const video = document.createElement('video');
  video.className = 'genvideo brandpreview';
  video.controls = true;
  video.autoplay = true;
  video.src = `${projectApi('/branding/' + kind)}?t=${Date.now()}`;
  row.after(video);
}

function syncBrandingState(branding) {
  MERGED.branding = branding;
  CURRENT_PROJECT.settings.branding = branding;
}

async function setBrandEnabled(kind, enabled) {
  const { ok, data } = await apiJson(projectApi('/settings'), 'POST', { branding: { [kind]: { enabled } } });
  if (!ok) { setStatus(data.error || 'Could not save the choice.', true); return; }
  syncBrandingState(data.settings.branding);
  document.querySelectorAll(`#mergedList .brandrow[data-kind="${kind}"] .brandToggle`)
    .forEach(box => { box.checked = enabled; });
  syncMergedJobs();
}

async function uploadBrandClip(kind, file, row) {
  if (!file) return;
  try {
    const d = await uploadAssetFile(kind, file, row.querySelector('.brandclip'),
      row.querySelector('.brandBrowse'), () => {});
    MERGED.branding[kind].file = d.path;
    CURRENT_PROJECT.settings.branding[kind].file = d.path;
    showToast(`Custom ${kind} uploaded.`, null, null, 'ok');
    renderMerged();
  } catch (e) {
    setStatus('Upload error: ' + e.message, true);
  }
}

async function resetBrandClip(kind) {
  const { ok, data } = await apiJson(projectApi('/asset/' + kind), 'DELETE');
  if (!ok) { setStatus(data.error || 'Could not reset the clip.', true); return; }
  MERGED.branding[kind].file = null;
  CURRENT_PROJECT.settings.branding[kind].file = null;
  renderMerged();
}

async function startFinalRender(name) {
  const { ok, data } = await apiJson(projectApi(`/merges/${enc(name)}/final`), 'POST');
  if (!ok) { setStatus(data.error || 'Could not start the final render.', true); return; }
  showToast('Final render queued.', null, null, 'ok');
  pollJobs();
}

async function cancelFinalRender(name) {
  const job = JOBS.find(j => isBrandJob(j) && j.target === name && isRunningJob(j));
  if (!job) return;
  await apiJson(`/api/jobs/${enc(job.id)}/cancel`, 'POST');
  pollJobs();
}

async function deleteMerged(m, finalOnly) {
  const what = finalOnly ? m.final : m;
  const size = finalOnly ? m.final.size : m.size + (m.final ? m.final.size : 0);
  const ok = await askConfirm({
    title: finalOnly ? 'Delete the branded version?' : 'Delete this merged video?',
    body: `<p><b>${escHtml(what.name)}</b>${!finalOnly && m.final ? ' and its branded version' : ''} will be deleted, freeing <b>${fmtSize(size)}</b>.</p>
           <p class="hint">${finalOnly ? 'The raw merge is kept.' : 'You can merge the chapters again from the Chapters screen.'}</p>`,
    confirmLabel: 'Delete',
  });
  if (!ok) return;
  document.querySelectorAll('#mergedList video').forEach(v => { v.pause(); v.removeAttribute('src'); v.load(); });
  const res = await apiJson(projectApi(`/merges/${enc(m.name)}${finalOnly ? '?final=1' : ''}`), 'DELETE');
  if (!res.ok) setStatus(res.data.error || 'Delete failed.', true);
  else showToast('Deleted.', null, null, 'ok');
  loadMerged();
}

// Called on every job poll: shows each card's final-render progress and reloads
// the list once when a render finishes.
function syncMergedJobs() {
  if (SCREEN !== 'hub' || HUB_VIEW !== 'merged' || !CURRENT_PROJECT) return;
  const jobs = JOBS.filter(isBrandJob);
  let finished = false;
  jobs.filter(j => !isRunningJob(j) && !brandedHandled.has(j.id)).forEach(j => {
    brandedHandled.add(j.id);
    if (j.status === 'done') { showToast('Final video ready.', null, null, 'ok'); finished = true; }
    else if (j.status === 'failed') setStatus('Final render failed: ' + j.message, true);
    else showToast('Final render stopped.', null, null, 'warn');
  });
  if (finished) { loadMerged(); return; }

  const anyEnabled = BRAND_KINDS.some(k => MERGED.branding[k] && MERGED.branding[k].enabled);
  document.querySelectorAll('#mergedList .mcard').forEach(card => {
    const mine = jobs.filter(j => j.target === card.dataset.name);
    const active = mine.find(isRunningJob);
    const last = mine.find(j => !isRunningJob(j));
    const bar = card.querySelector('.progress');
    card.querySelector('.finalRender').disabled = !!active || !anyEnabled;
    card.querySelector('.finalStop').hidden = !active;
    bar.style.display = active ? 'block' : 'none';
    bar.classList.toggle('indet', !!active && active.status === 'queued');
    bar.firstElementChild.style.width = active ? Math.round((active.progress || 0) * 100) + '%' : '0';
    card.querySelector('.finalState').textContent = !active ? ''
      : active.status === 'queued' ? 'Queued - ' + fmtEta(active.eta_s)
      : `Rendering ${Math.round((active.progress || 0) * 100)}% - ${fmtEtaOrElapsed(active)}`;
    card.querySelector('.finalMsg').textContent = !active && last && last.status === 'failed'
      ? 'Last final render failed: ' + last.message
      : (anyEnabled ? '' : 'Turn on the intro or the outro to render a final video.');
  });
}

// --- media library ---------------------------------------------------------------
let LIB = null;                       // GET /library: sizes per type, chapter and project
const LIB_OPEN = new Set();           // chapters whose folder is expanded
const LIB_FILE_LIMIT = 30;
const BULK_LABELS = { video: 'Videos', audio: 'Audio', frames: 'Frames', panels: 'Panels', strips: 'Strips', pdfs: 'PDFs' };
const bulk = { kind: 'video', selected: new Set() };

async function loadLibrary() {
  const d = await (await fetch(projectApi('/library'))).json();
  if (!d.kinds) throw new Error(d.error || 'library unavailable');
  LIB = d;
  renderLibrary();
}

function renderLibrary() {
  renderLibrarySummary();
  renderBulk();
  renderLibraryChapters();
  renderLibraryMerged();
}

function renderLibrarySummary() {
  const parts = [
    ...LIB.kinds.map(k => ({ key: k.key, label: k.label, bytes: LIB.by_kind[k.key] })),
    { key: 'merged', label: 'Merged videos', bytes: LIB.merged_bytes },
    { key: 'kept', label: 'Work, settings & uploads (kept)', bytes: LIB.kept_bytes },
  ];
  $('libTotalHint').textContent = `${fmtSize(LIB.total_bytes)} in this project.`;
  $('libBar').innerHTML = parts.filter(p => p.bytes > 0).map(p =>
    `<span class="k-${p.key}" style="width:${(100 * p.bytes / LIB.total_bytes).toFixed(2)}%" title="${escAttr(p.label)}: ${fmtSize(p.bytes)}"></span>`).join('');
  $('libLegend').innerHTML = parts.map(p =>
    `<span><i class="k-${p.key}"></i>${escHtml(p.label)}<b>${fmtSize(p.bytes) || '0B'}</b></span>`).join('');
}

// bulk delete: one file type across several chapters
function renderBulk() {
  const kind = LIB.kinds.find(k => k.key === bulk.kind);
  const rows = LIB.chapters.filter(c => c.kinds[bulk.kind].count > 0);
  bulk.selected = new Set([...bulk.selected].filter(n => rows.some(c => c.n === n)));
  $('bulkKinds').innerHTML = LIB.kinds.map(k =>
    `<label class="segopt"><input type="radio" name="bulkKind" value="${k.key}" ${k.key === bulk.kind ? 'checked' : ''} />${BULK_LABELS[k.key]}</label>`).join('');
  $('bulkRegen').textContent = kind.regenerate;
  $('bulkList').innerHTML = rows.length ? rows.map(c =>
    `<label class="bulkrow"><input type="checkbox" data-n="${c.n}" ${bulk.selected.has(c.n) ? 'checked' : ''} />
       <b>#${c.n}</b><span class="hint">${escHtml(c.title)}</span>
       <span class="bsize">${c.kinds[bulk.kind].count} file(s) · ${fmtSize(c.kinds[bulk.kind].bytes)}</span></label>`).join('')
    : `<div class="empty">No chapter has any ${BULK_LABELS[bulk.kind].toLowerCase()} on disk.</div>`;
  updateBulkSummary(rows);
}

function updateBulkSummary(rows) {
  rows = rows || LIB.chapters.filter(c => c.kinds[bulk.kind].count > 0);
  const bytes = rows.filter(c => bulk.selected.has(c.n)).reduce((sum, c) => sum + c.kinds[bulk.kind].bytes, 0);
  const all = $('bulkAll');
  all.checked = rows.length > 0 && bulk.selected.size === rows.length;
  all.indeterminate = bulk.selected.size > 0 && bulk.selected.size < rows.length;
  all.disabled = !rows.length;
  $('bulkAllSize').textContent = rows.length ? `${fmtSize(rows.reduce((sum, c) => sum + c.kinds[bulk.kind].bytes, 0))} in total` : '';
  $('bulkSummary').textContent = bulk.selected.size
    ? `${bulk.selected.size} chapter(s) selected - about ${fmtSize(bytes)} to free.` : 'Select the chapters to clean up.';
  $('bulkDeleteBtn').disabled = !bulk.selected.size;
}

$('bulkKinds').onchange = e => { bulk.kind = e.target.value; bulk.selected.clear(); renderBulk(); };
$('bulkList').onchange = e => {
  const n = +e.target.dataset.n;
  if (e.target.checked) bulk.selected.add(n); else bulk.selected.delete(n);
  updateBulkSummary();
};
$('bulkAll').onchange = e => {
  bulk.selected = e.target.checked
    ? new Set(LIB.chapters.filter(c => c.kinds[bulk.kind].count > 0).map(c => c.n)) : new Set();
  renderBulk();
};
$('bulkDeleteBtn').onclick = () => deleteLibraryKind([...bulk.selected], bulk.kind);

// the confirmation states exactly what the server will remove (dry run)
async function deleteLibraryKind(chapters, kindKey) {
  const kind = LIB.kinds.find(k => k.key === kindKey);
  const url = projectApi('/library/delete');
  const plan = await apiJson(url, 'POST', { chapters, kind: kindKey, dry_run: true });
  if (!plan.ok) { setStatus(plan.data.error || 'Could not check what would be deleted.', true); return; }
  if (!plan.data.bytes) { showToast('Nothing to delete.', null, null, 'warn'); return; }
  const confirmed = await askConfirm({
    title: `Delete ${kind.label.toLowerCase()}?`,
    body: `<p>This permanently deletes <b>${plan.data.count} file(s)</b> from <b>${chapters.length} chapter(s)</b> and frees exactly <b>${fmtSize(plan.data.bytes)}</b> (${plan.data.bytes.toLocaleString()} bytes).</p>
           <p class="hint">To get it back: ${escHtml(kind.regenerate)}</p>`,
    confirmLabel: `Delete and free ${fmtSize(plan.data.bytes)}`,
  });
  if (!confirmed) return;
  document.querySelectorAll('#hubLibrary audio, #hubLibrary video')
    .forEach(media => { media.pause(); media.removeAttribute('src'); media.load(); });
  const res = await apiJson(url, 'POST', { chapters, kind: kindKey });
  if (res.ok) showToast(`Freed ${fmtSize(res.data.freed)}. ${kind.regenerate}`, null, null, 'ok');
  else setStatus(res.data.error || 'Delete failed.', true);
  bulk.selected.clear();
  await loadCurrentProject(CURRENT_PROJECT.slug);
  paintProjectChrome();
  await loadLibrary();
}

function renderLibraryChapters() {
  const box = $('libChapters');
  if (!LIB.chapters.length) { box.innerHTML = '<div class="empty">No chapter has any files yet.</div>'; return; }
  box.innerHTML = '';
  LIB.chapters.forEach(c => box.appendChild(libChapterEl(c)));
  paintIcons(box);
}

function libChapterEl(c) {
  const el = document.createElement('div');
  el.className = 'libchap' + (LIB_OPEN.has(c.n) ? ' open' : '');
  const chips = LIB.kinds.filter(k => c.kinds[k.key].bytes)
    .map(k => `<span>${BULK_LABELS[k.key].toLowerCase()} <b>${fmtSize(c.kinds[k.key].bytes)}</b></span>`).join('');
  el.innerHTML = `
    <div class="libchaphead">
      <span class="chev" style="display:flex">${ic('chevright', 14)}</span>
      <span class="chno">#${c.n}</span><span class="chtitle">${escHtml(c.title)}</span>
      <span class="stpill st-${c.status}">${STATUS_LABELS[c.status] || c.status.replace('_', ' ')}</span>
      <span class="chips">${chips}<span>total <b>${fmtSize(c.bytes)}</b></span></span>
    </div>
    <div class="libchapbody"></div>`;
  const body = el.querySelector('.libchapbody');
  el.querySelector('.libchaphead').onclick = () => {
    const open = el.classList.toggle('open');
    if (open) { LIB_OPEN.add(c.n); loadChapterDetail(c.n, body); } else LIB_OPEN.delete(c.n);
  };
  if (LIB_OPEN.has(c.n)) loadChapterDetail(c.n, body);
  return el;
}

async function loadChapterDetail(n, body) {
  body.innerHTML = '<div class="hint">Loading…</div>';
  const detail = await (await fetch(projectApi(`/library/${n}`))).json();
  renderChapterDetail(n, body, detail, new Set());
}

function renderChapterDetail(n, body, detail, expanded) {
  const fileUrl = f => projectApi(`/library/${n}/file?path=${enc(f.path)}`);
  const fileHtml = (key, f) => {
    const url = fileUrl(f);
    const player = key === 'audio' ? `<div class="libplayer"><audio controls preload="none" src="${url}"></audio></div>`
      : f.name.endsWith('.mp4') ? `<div class="libplayer"><video controls preload="none" src="${url}"></video></div>` : '';
    return `<div class="libfile"><span>${escHtml(f.name)}</span>${player ? '' : `<a href="${url}" target="_blank">open</a>`}<span class="fsize">${fmtSize(f.size)}</span>${player}</div>`;
  };
  const groupHtml = g => {
    const shown = expanded.has(g.key) ? g.files : g.files.slice(0, LIB_FILE_LIMIT);
    const more = g.files.length - shown.length;
    return `<div class="libgroup">
      <div class="libghead"><i class="gdot k-${g.key}"></i><b>${escHtml(g.label)}</b>
        <span class="hint">${g.files.length ? `${g.files.length} file(s) · ${fmtSize(g.bytes)}` : 'none on disk'}</span>
        ${g.files.length ? `<button class="small danger libdel" data-kind="${g.key}" data-icon="trash" data-ic-size="13"><span>Delete</span></button>` : ''}
      </div>
      ${g.files.length ? `<span class="hint">To get it back: ${escHtml(g.regenerate)}</span>` : ''}
      ${shown.map(f => fileHtml(g.key, f)).join('')}
      ${more > 0 ? `<button class="small ghost libmore" data-kind="${g.key}"><span>Show ${more} more</span></button>` : ''}
    </div>`;
  };
  const keptBytes = detail.kept.reduce((sum, f) => sum + f.size, 0);
  body.innerHTML = detail.groups.map(groupHtml).join('') + `
    <details class="libkept"><summary>Kept work - ${detail.kept.length} file(s), ${fmtSize(keptBytes)}. Script, mapping, editor state and settings can't be deleted here.</summary>
      ${detail.kept.map(f => `<div class="libfile"><span>${escHtml(f.name)}</span><span class="fsize">${fmtSize(f.size)}</span></div>`).join('')}
    </details>`;
  paintIcons(body);
  body.querySelectorAll('.libdel').forEach(btn => btn.onclick = () => deleteLibraryKind([n], btn.dataset.kind));
  body.querySelectorAll('.libmore').forEach(btn => btn.onclick = () => {
    expanded.add(btn.dataset.kind);
    renderChapterDetail(n, body, detail, expanded);
  });
}

function renderLibraryMerged() {
  const row = (name, hint, size, url) =>
    `<div class="libfile" style="padding:10px 18px"><span>${escHtml(name)}</span><span class="hint">${hint}</span><span class="fsize">${fmtSize(size)}</span><div class="libplayer"><video controls preload="none" src="${url}"></video></div></div>`;
  $('libMerged').innerHTML = LIB.merges.length
    ? LIB.merges.map(m => row(m.name, `chapters ${m.chapters.join(', ')}`, m.size, m.url) +
        (m.final ? row(m.final.name, 'with intro / outro', m.final.size, m.final.url) : '')).join('')
    : '<div class="empty">No merged videos yet.</div>';
}

// --- Project settings screen ---------------------------------------------------
async function openSettingsScreen(slug) {
  showScreen('settings');
  try {
    if (!CURRENT_PROJECT || CURRENT_PROJECT.slug !== slug) await loadCurrentProject(slug);
    $('stCrumbProject').textContent = CURRENT_PROJECT.name;
    paintProjectChrome();
    fillSettingsForm();
    loadBackgroundOptions();
    loadGpuStatus();
  } catch (e) {
    setStatus('Open settings error: ' + e.message, true);
  }
}

// --- GPU acceleration status (see gpu.status in gpu.py) ---
async function loadGpuStatus() {
  const rows = $('gpuRows');
  rows.innerHTML = '<div class="gprow"><span class="gpname">Checking…</span></div>';
  const row = (label, ok, note) =>
    `<div class="gprow"><span class="gpname">${escHtml(label)}</span>` +
    `<span class="gpmeta" style="color:var(${ok ? '--ok' : '--warn'})">${escHtml(note)}</span></div>`;
  const lastRun = (u, what) => u
    ? `Last ${what} run: ${u.backend} on ${u.gpu ? 'GPU' : 'CPU'} (${u.at})`
    : `No ${what} run yet`;
  try {
    const d = await (await fetch('/api/gpu/status')).json();
    rows.innerHTML =
      row('GPU', !!d.gpu, d.gpu ? `${d.gpu.name}, ${Math.round(d.gpu.vram_mb / 1024)} GB, driver ${d.gpu.driver}` : 'No NVIDIA GPU detected') +
      row('Audio (narration)', d.audio.gpu_ready, d.audio.gpu_ready ? 'GPU ready' : 'CPU only') +
      `<div class="gprow"><span class="gpmeta" style="margin-left:0;white-space:normal">${escHtml(d.audio.note)} ${escHtml(lastRun(d.last_run.audio, 'audio'))}</span></div>` +
      row('Video (encoding)', d.video.gpu_ready, d.video.gpu_ready ? 'GPU ready (NVENC)' : 'CPU only (libx264)') +
      `<div class="gprow"><span class="gpmeta" style="margin-left:0;white-space:normal">${escHtml(d.video.note)} ${escHtml(lastRun(d.last_run.video, 'video'))}</span></div>`;
  } catch (e) {
    rows.innerHTML = row('GPU status', false, 'Could not check: ' + e.message);
  }
}
$('gpuRecheckBtn').onclick = loadGpuStatus;

// --- Chapter list: a list view and a card view over one shared selection ---------------
const PIPELINE_STEPS = ['download', 'stitch', 'script', 'export', 'audio', 'video'];
const STEP_STATE_LABEL = { done: 'done', current: 'running', stale: 'out of date', todo: 'not done' };
const STATUS_LABELS = { boxed: 'needs render' };
const SELECTION = new Set();     // chapter numbers ticked in either view
let selectionAnchor = null;      // last ticked chapter, the start of a shift-click range
let CHAPTER_VIEW = 'list';

const chapterViewKey = slug => `recapper.chapterView.${slug}`;
const chapterByNumber = n => CURRENT_PROJECT.chapters.find(c => c.n === n);

// Only the chapters on the loaded page carry detail (steps, video length, ...). A bulk action can
// cover chapters beyond it, so fetch the detail for the ones it is about to use.
const hasDetail = n => !!chapterByNumber(n).steps;

async function ensureChapterDetails(numbers) {
  const missing = numbers.filter(n => !hasDetail(n));
  if (!missing.length) return;
  const slug = encodeURIComponent(CURRENT_PROJECT.slug);
  const d = await (await fetch(`/api/projects/${slug}/chapter_details?numbers=${missing.join(',')}`)).json();
  d.chapters.forEach(detail => Object.assign(chapterByNumber(detail.n), detail));
}

function loadSelectionDetails() {
  const missing = [...SELECTION].filter(n => !hasDetail(n));
  if (!missing.length) return;
  ensureChapterDetails(missing).then(() => { updateMergeBar(); paintActionStates(); })
    .catch(e => setStatus('Could not load chapter details: ' + e.message, true));
}
const hasVideo = ch => ch.duration != null;

function loadChapterView(slug) {
  try { return localStorage.getItem(chapterViewKey(slug)) === 'cards' ? 'cards' : 'list'; } catch (_) { return 'list'; }
}

function setChapterView(view) {
  CHAPTER_VIEW = view;
  try { localStorage.setItem(chapterViewKey(CURRENT_PROJECT.slug), view); } catch (_) { /* the choice just isn't remembered */ }
  renderChapterList();
}

function fmtVideoLength(seconds) {
  const total = Math.round(seconds), minutes = Math.floor(total / 60), rest = total % 60;
  if (!minutes) return `${rest} s`;
  return rest ? `${minutes} min ${rest} s` : `${minutes} min`;
}

// "1, 2, 3, 5" -> "1-3, 5"
function listNumbers(numbers) {
  const runs = [];
  [...numbers].sort((a, b) => a - b).forEach(n => {
    const last = runs[runs.length - 1];
    if (last && n === last[1] + 1) last[1] = n; else runs.push([n, n]);
  });
  return runs.map(([from, to]) => from === to ? `${from}` : to === from + 1 ? `${from}, ${to}` : `${from}-${to}`).join(', ');
}

function selectChapter(n, on, range) {
  const numbers = CURRENT_PROJECT.chapters.map(c => c.n);
  const [from, to] = range && selectionAnchor !== null ? [n, selectionAnchor].sort((a, b) => a - b) : [n, n];
  numbers.filter(x => x >= from && x <= to).forEach(x => on ? SELECTION.add(x) : SELECTION.delete(x));
  selectionAnchor = n;
  paintSelection();
}

function toggleSelection(numbers) {
  const all = numbers.length > 0 && numbers.every(n => SELECTION.has(n));
  numbers.forEach(n => all ? SELECTION.delete(n) : SELECTION.add(n));
  paintSelection();
}

function runningStage(ch) {
  const live = BATCH && BATCH.batch && BATCH.batch.chapters.find(c => c.n === ch.n && c.state === 'running');
  return live ? live.stage : null;
}

function trackerState(ch, step, running) {
  if (step === running) return 'current';
  if (ch.steps.stale.includes(step)) return 'stale';
  return ch.steps.done.includes(step) ? 'done' : 'todo';
}

function progressHtml(ch) {
  const running = runningStage(ch);
  const states = PIPELINE_STEPS.map(step => [step, trackerState(ch, step, running)]);
  const label = states.map(([step, state]) => `${step} ${STEP_STATE_LABEL[state]}`).join(', ');
  const next = running ? `Running: ${running}` : ch.steps.next ? `Next: ${ch.steps.next}` : 'All steps done';
  return `<div class="tracker" role="img" aria-label="${label}">` +
    states.map(([step, state]) => `<span class="trk ${state}" title="${step}: ${STEP_STATE_LABEL[state]}"></span>`).join('') +
    `</div><span class="nextstep">${next}</span>`;
}

function chapterActionsHtml(ch) {
  const notStarted = ch.status === 'not_started';
  const buttons = [
    `<button class="small ${notStarted ? 'primary' : ''}" data-act="open" data-icon="${notStarted ? 'download' : 'crop'}" data-ic-size="13"><span>${notStarted ? 'Download' : ch.status === 'complete' ? 'Re-open editor' : 'Open editor'}</span></button>`,
    ch.status === 'complete'
      ? '<button class="small ghost" data-act="toggle" data-icon="refresh" data-ic-size="13" title="Reset to rendered"><span>Reset</span></button>'
      : '<button class="small ghost" data-act="toggle" data-icon="check" data-ic-size="13" title="Mark complete"><span>Mark complete</span></button>',
    hasVideo(ch) ? '<button class="small ghost" data-act="preview" data-icon="play" data-ic-size="13" title="Play the rendered recap"><span>Preview</span></button>' : '',
    '<button class="small ghost" data-act="pipeline" data-icon="layers" data-ic-size="13" title="Choose which steps to run for this chapter"><span>Pipeline</span></button>',
  ];
  return buttons.join('');
}

const TONE_LABELS = { epic: 'Epic', serious: 'Serious', dramatic: 'Dramatic', comedic: 'Comedic' };

function toneSelectHtml(ch) {
  const inherited = TONE_LABELS[CURRENT_PROJECT.settings.script.tone];
  const options = [`<option value="" ${ch.tone ? '' : 'selected'}>Project default (${inherited})</option>`,
    ...Object.entries(TONE_LABELS).map(([id, label]) => `<option value="${id}" ${ch.tone === id ? 'selected' : ''}>${label}</option>`)];
  return `<label class="chtone" title="Tone of this chapter's script and narration voice">Tone<select data-tone>${options.join('')}</select></label>`;
}

function narrationHtml(ch) {
  if (!ch.narration) return '';
  return `<span class="chlen" title="Estimated from the script's length at this project's speaking pace">Narration ~${fmtVideoLength(ch.narration.seconds)} (${ch.narration.words.toLocaleString()} words)</span>`;
}

function chapterMeta(ch) {
  return ch.updated_at ? `Updated ${timeAgo(ch.updated_at)}` : 'Not downloaded';
}

function selectBox(ch) {
  return `<input type="checkbox" class="selCk" aria-label="Select chapter ${ch.n}" ${SELECTION.has(ch.n) ? 'checked' : ''} />`;
}

function chapterRowHtml(ch) {
  const status = STATUS_LABELS[ch.status] || ch.status.replace('_', ' ');
  return `<div class="chrow ${SELECTION.has(ch.n) ? 'sel' : ''}" data-n="${ch.n}">${selectBox(ch)}<span class="chno">#${ch.n}</span>` +
    `<span class="chtexts"><span class="chtitle">${escHtml(ch.title)}</span><span class="chmeta">${chapterMeta(ch)}</span></span>` +
    `<div class="chstatus"><span class="stpill st-${ch.status}">${status}</span>` +
    `${narrationHtml(ch)}${hasVideo(ch) ? `<span class="chlen">Video ${fmtVideoLength(ch.duration)}</span>` : ''}${toneSelectHtml(ch)}</div>` +
    `<div class="chprogress" data-progress>${progressHtml(ch)}</div>` +
    `<div class="chacts">${chapterActionsHtml(ch)}</div></div>`;
}

function chapterCardHtml(ch) {
  const status = STATUS_LABELS[ch.status] || ch.status.replace('_', ' ');
  const thumb = ch.has_thumb
    ? `<img loading="lazy" alt="" src="${projectApi(`/chapters/${ch.n}/thumb`)}?v=${encodeURIComponent(ch.updated_at || '')}" />`
    : 'No frame yet';
  return `<article class="chcard ${SELECTION.has(ch.n) ? 'sel' : ''}" data-n="${ch.n}">` +
    `<div class="chcardtop">${selectBox(ch)}<span class="chno">#${ch.n}</span><span class="chtitle" title="${escHtml(ch.title)}">${escHtml(ch.title)}</span></div>` +
    `<div class="chthumb">${thumb}</div>` +
    `<div class="chprogress" data-progress>${progressHtml(ch)}</div>` +
    `<div class="chcardmeta"><span class="stpill st-${ch.status}">${status}</span>` +
    `${narrationHtml(ch)}${hasVideo(ch) ? `<span class="chlen">Video ${fmtVideoLength(ch.duration)}</span>` : ''}${toneSelectHtml(ch)}<span class="chmeta">${chapterMeta(ch)}</span></div>` +
    `<div class="chacts">${chapterActionsHtml(ch)}</div></article>`;
}

async function showMoreChapters() {
  const btn = $('chMoreBtn');
  btnBusy(btn, true);
  chaptersShown += CHAPTER_PAGE;
  try {
    await loadCurrentProject(CURRENT_PROJECT.slug);
    renderChapterList();
  } catch (e) {
    chaptersShown -= CHAPTER_PAGE;
    btnBusy(btn, false);
    setStatus('Could not load more chapters: ' + e.message, true);
  }
}

function renderChapterList() {
  const list = $('chList');
  const chapters = CURRENT_PROJECT.chapters || [];
  SELECTION.forEach(n => { if (!chapterByNumber(n)) SELECTION.delete(n); });
  const cards = CHAPTER_VIEW === 'cards';
  list.classList.toggle('cards', cards);
  $('viewListBtn').setAttribute('aria-pressed', String(!cards));
  $('viewCardsBtn').setAttribute('aria-pressed', String(cards));
  $('viewListBtn').classList.toggle('on', !cards);
  $('viewCardsBtn').classList.toggle('on', cards);
  const page = chapters.slice(0, chaptersShown);
  list.innerHTML = chapters.length ? page.map(cards ? chapterCardHtml : chapterRowHtml).join('') : '<div class="empty">No chapters yet.</div>';
  if (chapters.length > page.length) {
    const more = Math.min(CHAPTER_PAGE, chapters.length - page.length);
    list.insertAdjacentHTML('beforeend', `<div class="chmore"><button id="chMoreBtn" class="small ghost">Show ${more} more <span class="hint">(${page.length} of ${chapters.length} shown)</span></button></div>`);
    $('chMoreBtn').onclick = showMoreChapters;
  }
  paintIcons(list);
  const counts = { not_started: 0, in_progress: 0, complete: 0 };
  chapters.forEach(c => {
    if (c.status === 'not_started') counts.not_started++;
    else if (c.status === 'complete') counts.complete++;
    else counts.in_progress++;
  });
  if ($('stNotStarted')) $('stNotStarted').textContent = String(counts.not_started);
  if ($('stInProgress')) $('stInProgress').textContent = String(counts.in_progress);
  if ($('stComplete')) $('stComplete').textContent = String(counts.complete);
  paintProjectChrome();
  paintSelection();
}

// Selection changes touch only the checkboxes and the action buttons, so a click never redraws the list.
function paintSelection() {
  $('chList').querySelectorAll('[data-n]').forEach(el => {
    const on = SELECTION.has(+el.dataset.n);
    el.classList.toggle('sel', on);
    el.querySelector('.selCk').checked = on;
  });
  $('selCount').textContent = `${SELECTION.size} selected`;
  updateMergeBar();
  paintActionStates();
  loadSelectionDetails();
}

function runBlocker() {
  return SELECTION.size ? '' : 'Select at least one chapter to run the pipeline.';
}

// Actions that can't run say why, in their tooltip and (for the bulk bar) next to the button.
function paintActionStates() {
  const blocker = runBlocker();
  $('batchRunBtn').disabled = !!blocker;
  $('batchRunBtn').title = blocker;
  $('runWhy').textContent = blocker;
  $('batchRunBtn').querySelector('span').textContent = SELECTION.size ? `Run pipeline for ${SELECTION.size}` : 'Run pipeline';
  paintTrackers();
}

function paintTrackers() {
  $('chList').querySelectorAll('[data-n]').forEach(el => {
    const html = progressHtml(chapterByNumber(+el.dataset.n));
    const cell = el.querySelector('[data-progress]');
    if (cell.dataset.html !== html) { cell.innerHTML = html; cell.dataset.html = html; }
  });
}

$('chList').addEventListener('change', async e => {
  const select = e.target.closest('[data-tone]');
  if (!select) return;
  const ch = chapterByNumber(+select.closest('[data-n]').dataset.n);
  const res = await fetch(projectApi(`/chapters/${ch.n}/tone`), { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tone: select.value || null }) });
  if (res.ok) ch.tone = select.value || null;
  else select.value = ch.tone || '';
});

$('chList').addEventListener('click', e => {
  const holder = e.target.closest('[data-n]');
  if (!holder) return;
  const ch = chapterByNumber(+holder.dataset.n);
  if (e.target.closest('.selCk')) { selectChapter(ch.n, e.target.checked, e.shiftKey); return; }
  const act = e.target.closest('[data-act]');
  if (!act) return;
  const slug = CURRENT_PROJECT.slug;
  if (act.dataset.act === 'open') {
    if (ch.status === 'not_started') openChapterEditor(slug, ch.n, { autoDownload: true, url: CURRENT_PROJECT.url });
    else navTo({ project: slug, chapter: ch.n });
  } else if (act.dataset.act === 'toggle') toggleComplete(ch);
  else if (act.dataset.act === 'preview') window.open(`/output/${encodeURIComponent(`${slug}_ch${ch.n}`)}/recap.mp4?project=${encodeURIComponent(slug)}&ch_no=${ch.n}`, '_blank');
  else if (act.dataset.act === 'pipeline') openPipelineModal([ch.n]);
});

$('viewListBtn').onclick = () => setChapterView('list');
$('viewCardsBtn').onclick = () => setChapterView('cards');
$('selAllBtn').onclick = () => toggleSelection(CURRENT_PROJECT.chapters.map(c => c.n));
$('selCompleteBtn').onclick = () => toggleSelection(CURRENT_PROJECT.chapters.filter(c => c.status === 'complete').map(c => c.n));
$('selClearBtn').onclick = () => { SELECTION.clear(); selectionAnchor = null; paintSelection(); };

function mergeBlocker() {
  if (mergeES) return 'A merge is already running.';
  if (SELECTION.size < 2) return 'Select at least 2 chapters to merge.';
  if ([...SELECTION].some(n => !hasDetail(n))) return 'Loading the selected chapters…';
  const without = [...SELECTION].filter(n => !hasVideo(chapterByNumber(n)));
  return without.length ? `Merge needs a video for every selected chapter. No video for ${listNumbers(without)}.` : '';
}

function updateMergeBar() {
  const total = [...SELECTION].reduce((sum, n) => sum + (chapterByNumber(n).duration || 0), 0);
  const blocker = mergeBlocker();
  $('mergeSelHint').textContent = SELECTION.size === 0 ? blocker
    : `${SELECTION.size} chapter(s) selected: ${listNumbers(SELECTION)}. ${blocker || `The merged video will be ${fmtClock(total)} (before any intro/outro).`}`;
  $('mergeBtn').disabled = !!blocker;
  $('mergeBtn').title = blocker;
}

// --- Pipeline step modal -------------------------------------------------------------
// Each chapter runs one contiguous stretch of steps, `first`..`last`, in order. `target` is the step
// the user ticked (everything before it is a prerequisite); `first` is the earliest step that has
// to run: a missing or out-of-date prerequisite, or the first step when completed ones are redone.
// Steps before `first` are reused. `last` is `target` plus the built steps after `first` that its
// re-running makes out of date; the user can untick those to keep them.
const PIPE = { rows: [], target: PIPELINE_STEPS.length - 1, redo: false, remember: true, ticks: new Map(), open: new Set(), groups: [] };
const pipeChoiceKey = () => `recapper.pipeline.${CURRENT_PROJECT.slug}`;

function stepStates(ch) {
  return PIPELINE_STEPS.map(step => !ch.steps.done.includes(step) ? 'missing' : ch.steps.stale.includes(step) ? 'stale' : 'done');
}

function firstStep(states, target, redo) {
  if (target < 0) return -1;
  if (redo) return 0;
  const gap = states.findIndex((state, i) => i <= target && state !== 'done');
  return gap === -1 ? target : gap;
}

function tickTarget(states, target, redo) {
  const first = firstStep(states, target, redo);
  const lastBuilt = states.findLastIndex(state => state !== 'missing');
  return { target, last: first >= 0 && lastBuilt > first ? Math.max(target, lastBuilt) : target };
}

function pipePlan(ch) {
  const states = stepStates(ch), { target, last } = PIPE.ticks.get(ch.n), first = firstStep(states, target, PIPE.redo);
  const steps = states.map((state, i) => {
    if (i > last) return { ticked: false, kind: 'idle', tag: state === 'missing' ? '' : first >= 0 && i > first ? 'out of date, kept' : 'done' };
    if (i < first) return { ticked: true, kind: 'reuse', tag: 'done, will reuse' };
    if (state === 'missing') return { ticked: true, kind: 'run', tag: 'missing, will run' };
    if (state === 'stale') return { ticked: true, kind: 'stale', tag: 'out of date, will run' };
    if (PIPE.redo) return { ticked: true, kind: 'run', tag: 'redo' };
    return i > first ? { ticked: true, kind: 'stale', tag: 'out of date, will re-run' } : { ticked: true, kind: 'run', tag: 'will run' };
  });
  steps.forEach((step, i) => { step.locked = states[i] === 'missing' && i < last; });
  const runs = first < 0 ? [] : PIPELINE_STEPS.slice(first, last + 1);
  const kept = PIPELINE_STEPS.filter((_, i) => steps[i].tag === 'out of date, kept');
  const reused = first > 0 ? `${PIPELINE_STEPS[first - 1]} done, ` : '';
  const what = !runs.length ? 'nothing selected'
    : `${reused}will run ${runs.length === PIPELINE_STEPS.length ? 'all steps' : runs.join(' + ')}${kept.length ? `; ${kept.join(', ')} kept out of date` : ''}`;
  return { ch, steps, runs, what };
}

function pipeTick(stepIndex, on, ch) {
  (ch ? [ch] : PIPE.rows).forEach(row => {
    const { target, last } = PIPE.ticks.get(row.n);
    PIPE.ticks.set(row.n, on ? tickTarget(stepStates(row), stepIndex, PIPE.redo)
      : { target: Math.min(target, stepIndex - 1), last: Math.min(last, stepIndex - 1) });
  });
  if (!ch) PIPE.target = on ? stepIndex : stepIndex - 1;
}

function pipeGlobalStepsHtml(plans) {
  return PIPELINE_STEPS.map((step, i) => {
    const ticked = plans.filter(p => p.steps[i].ticked).length;
    const running = plans.filter(p => p.runs.includes(step)).length;
    const stale = plans.filter(p => p.steps[i].kind === 'stale').length;
    const tag = `${running ? `runs in ${running} of ${plans.length}` : ticked ? 'reused' : 'not selected'}${stale ? `, out of date in ${stale}` : ''}`;
    return `<div class="pstep ${ticked ? 'on' : ''}"><label><input type="checkbox" data-step="${i}" data-focus="s${i}" ${ticked === plans.length ? 'checked' : ''} ${ticked && ticked < plans.length ? 'data-partial' : ''} />${step}</label><span class="ptag ${stale ? 'stale' : ''}">${tag}</span></div>`;
  }).join('');
}

function pipeChapterHtml(plan) {
  const cells = PIPELINE_STEPS.map((step, i) => {
    const cell = plan.steps[i];
    return `<div class="pcstep ${cell.locked ? 'locked' : ''}"><label><input type="checkbox" data-cn="${plan.ch.n}" data-cstep="${i}" data-focus="c${plan.ch.n}-${i}" ${cell.ticked ? 'checked' : ''} ${cell.locked ? 'disabled' : ''} />${step}</label>` +
      `<span class="ptag ${cell.kind}">${cell.tag}</span></div>`;
  }).join('');
  return `<div class="pcrow"><div class="pctitle"><span class="chno">#${plan.ch.n}</span><b>${escHtml(plan.ch.title)}</b></div><div class="pcsteps">${cells}</div></div>`;
}

function pipeGroupsHtml(plans) {
  const byWhat = new Map();
  plans.forEach(plan => byWhat.set(plan.what, [...(byWhat.get(plan.what) || []), plan]));
  PIPE.groups = [...byWhat.values()];
  return PIPE.groups.map((group, gi) => {
    const open = group.filter(p => PIPE.open.has(p.ch.n));
    const allOpen = open.length === group.length;
    return `<section class="pgroup"><div class="pghead"><div><b>${group.length} chapter${group.length === 1 ? '' : 's'}: ${group[0].what}</b>` +
      `<div class="hint">Chapter ${listNumbers(group.map(p => p.ch.n))}</div></div>` +
      `<button class="small ghost" data-group="${gi}" data-focus="g${gi}">${allOpen ? 'Hide chapters' : 'Adjust chapters'}</button></div>` +
      `${open.length ? `<div class="pgrows">${open.map(pipeChapterHtml).join('')}</div>` : ''}</section>`;
  }).join('');
}

function pipeSummary(plans) {
  const running = plans.filter(p => p.runs.length);
  if (!running.length) return 'Nothing to run: tick at least one step.';
  const perStep = PIPELINE_STEPS.map(step => [step, running.filter(p => p.runs.includes(step)).length]).filter(([, count]) => count);
  const skipped = plans.length - running.length;
  return `Will run ${running.length} chapter${running.length === 1 ? '' : 's'} &times; steps (${running.reduce((sum, p) => sum + p.runs.length, 0)} step runs): ` +
    `${perStep.map(([step, count]) => `${step} &times; ${count}`).join(', ')}.${skipped ? ` ${skipped} chapter${skipped === 1 ? ' has' : 's have'} nothing selected.` : ''}`;
}

function renderPipeModal() {
  const focused = document.activeElement && document.activeElement.dataset.focus;
  const plans = PIPE.rows.map(pipePlan);
  $('pipeSteps').innerHTML = pipeGlobalStepsHtml(plans);
  $('pipeSteps').querySelectorAll('[data-partial]').forEach(box => { box.indeterminate = true; });
  $('pipeRedo').checked = PIPE.redo;
  $('pipeGroups').innerHTML = pipeGroupsHtml(plans);
  $('pipeSummary').innerHTML = pipeSummary(plans);
  $('pipeRun').disabled = !plans.some(p => p.runs.length);
  if (focused) {
    const again = $('pipeModal').querySelector(`[data-focus="${focused}"]`);
    if (again && !again.disabled) again.focus();
  }
}

function readPipeChoice() {
  try { return JSON.parse(localStorage.getItem(pipeChoiceKey())) || {}; } catch (_) { return {}; }
}

function savePipeChoice() {
  if (!PIPE.remember) return;
  try { localStorage.setItem(pipeChoiceKey(), JSON.stringify({ target: PIPE.target, redo: PIPE.redo })); } catch (_) { /* not remembered */ }
}

const closePipeModal = () => $('pipeModal').classList.remove('on', 'loading');

// `step` (an index) pre-ticks that step; without it the last used choice comes back.
async function openPipelineModal(numbers, step) {
  $('pipeTitle').textContent = 'Run pipeline';
  $('pipeModal').classList.add('on', 'loading');
  try {
    await loadCurrentProject(CURRENT_PROJECT.slug);   // the steps shown must be what the disk has now
    await ensureChapterDetails(numbers);
  } catch (e) {
    $('pipeModal').classList.remove('on', 'loading');
    setStatus('Could not load the chapters: ' + e.message, true);
    return;
  }
  renderChapterList();
  const choice = step === undefined ? readPipeChoice() : { target: step };
  PIPE.remember = step === undefined;
  PIPE.rows = CURRENT_PROJECT.chapters.filter(c => numbers.includes(c.n));
  PIPE.target = choice.target >= 0 ? choice.target : PIPELINE_STEPS.length - 1;
  PIPE.redo = !!choice.redo;
  PIPE.open = new Set(PIPE.rows.length === 1 ? [PIPE.rows[0].n] : []);
  PIPE.ticks = new Map(PIPE.rows.map(ch => [ch.n, tickTarget(stepStates(ch), PIPE.target, PIPE.redo)]));
  $('pipeTitle').textContent = PIPE.rows.length === 1 ? `Run pipeline: chapter ${PIPE.rows[0].n}` : `Run pipeline: ${PIPE.rows.length} chapters`;
  renderPipeModal();
  $('pipeModal').classList.remove('loading');
  $('pipeModal').querySelector('[data-step]').focus();
}

$('pipeModal').addEventListener('change', e => {
  const box = e.target;
  if (box.id === 'pipeRedo') {
    PIPE.redo = box.checked;
    PIPE.rows.forEach(ch => PIPE.ticks.set(ch.n, tickTarget(stepStates(ch), PIPE.target, PIPE.redo)));
  } else if (box.dataset.step !== undefined) pipeTick(+box.dataset.step, box.checked);
  else if (box.dataset.cstep !== undefined) pipeTick(+box.dataset.cstep, box.checked, chapterByNumber(+box.dataset.cn));
  renderPipeModal();
});

$('pipeModal').addEventListener('click', e => {
  const toggle = e.target.closest('[data-group]');
  if (!toggle) return;
  const group = PIPE.groups[+toggle.dataset.group].map(p => p.ch.n);
  const allOpen = group.every(n => PIPE.open.has(n));
  group.forEach(n => allOpen ? PIPE.open.delete(n) : PIPE.open.add(n));
  renderPipeModal();
});

$('pipeModal').addEventListener('keydown', e => { if (e.key === 'Escape') closePipeModal(); });
$('pipeCancel').onclick = closePipeModal;
$('pipeRun').onclick = startPipeline;

// ---------------------------------------------------------------------------
// merge selected "complete" chapters' recap.mp4 into one video, in chapter
// order - same SSE pattern as generate (see startGenerate above).
// ---------------------------------------------------------------------------
let mergeES = null, mergeDone = false;
const mergeLines = [];

function setMergeBusy(b) {
  btnBusy($('mergeBtn'), b);
  $('mergeCancelBtn').disabled = !b;
  if (!b) updateMergeBar();
}
function setMergeBar(frac, indet) {
  const wrap = $('mergeProgress'), bar = $('mergeBar');
  wrap.style.display = 'block';
  wrap.classList.remove('err');
  wrap.classList.toggle('indet', !!indet);
  if (!indet) bar.style.width = Math.round(clamp(frac, 0, 1) * 100) + '%';
}
function appendMergeLog(line) {
  mergeLines.push(line);
  if (mergeLines.length > 500) mergeLines.shift();
  const el = $('mergeStatus');
  el.textContent = mergeLines.join('\n');
  el.scrollTop = el.scrollHeight;
}

function startMerge() {
  if (mergeES) { setStatus('A merge is already running.', true); return; }
  const blocker = mergeBlocker();
  if (blocker) { setStatus(blocker, true); return; }
  const chapters = [...SELECTION].sort((a, b) => a - b);
  mergeLines.length = 0; mergeDone = false;
  $('mergeStatus').textContent = ''; $('mergeResult').innerHTML = '';
  setMergeBusy(true); setMergeBar(0, true);
  appendMergeLog(`Requesting merge of chapters ${chapters.join(', ')}…`);

  const url = `/api/projects/${encodeURIComponent(CURRENT_PROJECT.slug)}/merge_stream?chapters=${chapters.join(',')}`;
  mergeES = new EventSource(url);
  mergeES.onmessage = e => { try { handleMergeEvent(JSON.parse(e.data)); } catch (_) {} };
  mergeES.onerror = () => {
    if (mergeDone) return;
    appendMergeLog('[connection lost]');
    setStatus('Merge stream disconnected.', true);
    finishMerge();
  };
}

function handleMergeEvent(ev) {
  if (ev.type === 'start') {
    appendMergeLog(ev.message || 'Starting…');
    setMergeBar(0.05, true);
  } else if (ev.type === 'log') {
    appendMergeLog(ev.line || '');
    setMergeBar(0.5, true);   // ffmpeg concat is fast/bursty - a pulse reads better than a fake %
  } else if (ev.type === 'done') {
    mergeDone = true;
    if (ev.ok) {
      setMergeBar(1, false);
      appendMergeLog('OK  ' + (ev.message || 'Done.'));
      showToast('Merged video ready.', null, null, 'ok');
      if (ev.video) showMergeResult(ev.video);
      setStatus(ev.message || 'Merge complete.');
    } else {
      $('mergeProgress').classList.add('err');
      appendMergeLog('FAILED  ' + (ev.message || 'Failed.'));
      setStatus(ev.message || 'Merge failed - see the log.', true);
    }
    finishMerge();
  } else if (ev.type === 'error') {
    mergeDone = true;
    $('mergeProgress').classList.add('err');
    appendMergeLog('FAILED  ' + (ev.message || 'Error'));
    setStatus(ev.message || 'Merge error.', true);
    finishMerge();
  }
}

function showMergeResult(videoUrl) {
  const sep = videoUrl.includes('?') ? '&' : '?';
  const src = videoUrl + sep + 't=' + Date.now();
  $('mergeResult').innerHTML =
    `<div class="jobcard">
       <div class="jobhead">${ic('check',15)}<b>Merged video ready</b><span class="jobpath">${videoUrl}</span></div>
       <video class="genvideo" src="${src}" controls preload="metadata"></video>
       <div class="jobacts">
         <a class="btnlink" href="${src}" target="_blank">${ic('play',14)}<span>Play full size</span></a>
         <a class="btnlink" href="${src}" download="merged.mp4">${ic('download',14)}<span>Download</span></a>
         <button class="btnlink" onclick="navTo({ project: CURRENT_PROJECT.slug, view: 'merged' })">${ic('film',14)}<span>Add intro / outro</span></button>
       </div>
     </div>`;
}

function finishMerge() {
  if (mergeES) { mergeES.close(); mergeES = null; }
  setMergeBusy(false);
}
function cancelMerge() {
  if (!mergeES) return;
  mergeDone = true;
  finishMerge();
  $('mergeProgress').style.display = 'none';
  appendMergeLog('[stopped by user]');
  setStatus('Merge stopped.');
  showToast('Merge stopped.', null, null, 'warn');
}

async function toggleComplete(ch) {
  const status = ch.status === 'complete' ? 'rendered' : 'complete';
  try {
    const res = await fetch(`/api/projects/${encodeURIComponent(CURRENT_PROJECT.slug)}/chapters/${ch.n}/status`,
      { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ status, force:true }) });
    const d = await res.json();
    if (!res.ok) throw new Error(d.error || 'failed');
    ch.status = d.chapter.status;
    renderChapterList();
  } catch (e) { setStatus('Status update error: ' + e.message, true); }
}

// Refreshes the chapter list, then downloads as many of the next missing chapters as asked for.
function streamEvents(url, onEvent) {
  return new Promise((resolve, reject) => {
    const es = new EventSource(url);
    es.onmessage = ev => {
      const frame = JSON.parse(ev.data);
      if (frame.type === 'error') { es.close(); reject(new Error(frame.message)); }
      else if (frame.type === 'done') { es.close(); resolve(frame); }
      else onEvent(frame);
    };
    es.onerror = () => { es.close(); reject(new Error('Lost the connection to the app.')); };
  });
}

async function downloadMoreChapters() {
  const slug = encodeURIComponent(CURRENT_PROJECT.slug), btn = $('pjMoreBtn');
  btnBusy(btn, true);
  try {
    setStatus('Refreshing the chapter list…');
    await refetchChapters();
    const missing = await (await fetch(`/api/projects/${slug}/missing_chapters`)).json();
    if (!missing.count) { setStatus('All chapters are already downloaded.'); return; }
    const answer = prompt(`${missing.count} of ${missing.total} chapters are not downloaded yet. How many should be downloaded now?`, String(Math.min(5, missing.count)));
    const count = parseInt(answer, 10);
    if (!count || count < 1) { setStatus('Nothing downloaded.'); return; }
    const result = await streamEvents(`/api/projects/${slug}/download_stream?count=${count}`, f => {
      if (f.type === 'progress') setStatus(`Downloading chapter ${f.chapter} (${f.position} of ${f.total})…`);
      else if (f.line) setStatus(f.line);
    });
    await loadCurrentProject(CURRENT_PROJECT.slug);
    renderChapterList();
    const failed = result.failed.map(x => `${x.chapter} (${x.error})`).join(', ');
    setStatus(failed ? `Downloaded ${result.requested - result.failed.length} of ${result.requested} chapters. Failed: ${failed}` : `Downloaded ${result.requested} chapter(s).`, !!failed);
  } catch (e) { setStatus('Download error: ' + e.message, true); }
  finally { btnBusy(btn, false); }
}

async function deleteProject() {
  const { slug, name } = CURRENT_PROJECT;
  const typed = prompt(`This permanently deletes "${name}" and every file of it - chapters, scripts, audio, videos and the copy in the projects folder.

Type the project name to confirm:`);
  if (typed === null) return;
  if (typed !== name) { setStatus('The name did not match - nothing was deleted.', true); return; }
  btnBusy($('pjDeleteBtn'), true);
  try {
    const res = await fetch(`/api/projects/${encodeURIComponent(slug)}`, { method:'DELETE',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({ confirm: typed }) });
    const d = await res.json();
    if (!res.ok) throw new Error(d.error || 'failed');
    navTo({});
    showToast(`Deleted "${name}".`, null, null, 'ok');
  } catch (e) { setStatus('Delete error: ' + e.message, true); }
  finally { btnBusy($('pjDeleteBtn'), false); }
}

async function refetchChapters() {
  if (!CURRENT_PROJECT) return;
  btnBusy($('pjRefetchBtn'), true);
  try {
    const res = await fetch(`/api/projects/${encodeURIComponent(CURRENT_PROJECT.slug)}/chapters`,
      { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ refetch:true }) });
    const d = await res.json();
    if (!res.ok || d.ok === false) throw new Error(d.error || 'Refetch failed.');
    await loadCurrentProject(CURRENT_PROJECT.slug);
    renderChapterList();
    showToast('Chapter list refreshed.', null, null, 'ok');
  } catch (e) { setStatus('Refetch error: ' + e.message, true); }
  finally { btnBusy($('pjRefetchBtn'), false); }
}

// --- Project settings (music / voice / watermark / background) + live previews
function fillSettingsForm() {
  const s = CURRENT_PROJECT.settings;
  document.querySelector(`input[name=musicMode][value="${s.music.mode}"]`).checked = true;
  $('musicName').textContent = s.music.file ? s.music.file.split('/').pop() : '';

  document.querySelector(`input[name=voiceMode][value="${s.voice.mode}"]`).checked = true;
  document.querySelector(`input[name=voiceEngine][value="${s.voice.engine || 'chatterbox'}"]`).checked = true;
  $('kokoroVoice').value = s.voice.kokoro_voice || $('kokoroVoice').value;
  paintVoiceEngine();
  $('voiceName').textContent = s.voice.file ? s.voice.file.split('/').pop() : '';
  refreshVoiceActive();

  $('wmEnabled').checked = !!s.watermark.enabled;
  document.querySelector(`input[name=wmType][value="${s.watermark.type}"]`).checked = true;
  $('wmText').value = s.watermark.text || '';
  $('wmFileName').textContent = s.watermark.file ? s.watermark.file.split('/').pop() : '';
  $('wmOpacity').value = Math.round(s.watermark.opacity * 100);
  $('wmSize').value = Math.round(s.watermark.size * 100);
  $('wmSpacing').value = Math.round(s.watermark.spacing * 100);
  $('wmAngle').value = Math.round(s.watermark.angle);

  document.querySelector(`input[name=bgMode][value="${s.background.mode}"]`).checked = true;
  $('bgFileName').textContent = s.background.file ? s.background.file.split('/').pop() : '';
  $('bgDim').value = Math.round(s.background.dim * 100);
  $('bgVideoHint').hidden = s.background.mode !== 'video';

  document.querySelector(`input[name=animStyle][value="${s.animation.style}"]`).checked = true;
  $('animDirection').value = s.animation.direction || 'left';
  $('animDuration').value = Math.round(s.animation.duration * 100);
  $('animZoom').checked = !!s.animation.zoom;
  $('animZoomAmount').value = Math.round(s.animation.zoom_amount * 100);
  $('animZoomAmountLbl').textContent = `${(+$('animZoomAmount').value / 100).toFixed(2)}x`;
  $('framingBlur').checked = !!(s.framing || {}).blur_text;

  const sc = s.script || {};
  $('scriptTone').value = sc.tone || 'dramatic';
  $('scriptNarrationType').value = sc.narration_type || 'first_person';
  $('scriptStyleRef').value = sc.style_reference || '';
  const cl = s.claude || {};
  $('claudeBrief').value = cl.series_brief || '';
  $('claudeComprehensionModel').value = cl.comprehension_model || 'opus';
  $('claudeNarrationModel').value = cl.narration_model || 'sonnet';
  $('claudeMinFrame').value = cl.min_frame_s || 3;
  const bt = s.batch || {};
  $('videoQuality').value = (s.video || {}).quality || 'tiny';
  $('batchReview').checked = !!bt.review_before_audio;
  $('batchAutoWait').checked = bt.auto_wait_five_hour !== false;
  $('batchNotify').checked = bt.notifications !== false;
  $('batchFiveHour').value = bt.five_hour_pct || 70;
  $('batchSevenDay').value = bt.seven_day_pct || 60;
  loadClaudeStatus();

  updateSliderLabels();
  refreshWatermarkPreview();
  refreshBackgroundPreview();
  setSettingsSaveState('ok', 'Autosaved');
}

// settings save indicator - these fields autosave (debounced) on every change,
// there is no explicit Save button; this badge is the only feedback that a
// change actually persisted, so it always reflects the current state
// (idle/saving/saved/failed), not just a transient message.
function setSettingsSaveState(kind, text) {
  const el = $('pjSettingsSaved');
  if (!el) return;
  el.textContent = text;
  el.classList.remove('saving', 'ok', 'err');
  el.classList.add(kind);
}

function updateSliderLabels() {
  $('wmOpacityLbl').textContent = $('wmOpacity').value + '%';
  $('wmSizeLbl').textContent = $('wmSize').value + '%';
  $('wmSpacingLbl').textContent = $('wmSpacing').value + '%';
  $('wmAngleLbl').textContent = $('wmAngle').value + '°';
  $('bgDimLbl').textContent = $('bgDim').value + '%';
  $('animDurationLbl').textContent = (+$('animDuration').value / 100).toFixed(2) + 's';
}

function paintVoiceEngine() {
  const kokoro = document.querySelector('input[name=voiceEngine]:checked').value === 'kokoro';
  $('kokoroBlock').hidden = !kokoro;
  $('chatterboxBlock').hidden = kokoro;
}

async function loadKokoroVoices() {
  const { voices } = await (await fetch('/api/tts/kokoro/voices')).json();
  $('kokoroVoice').innerHTML = Object.entries(voices).map(([id, label]) => `<option value="${id}">${escHtml(label)}</option>`).join('');
}

async function previewKokoro() {
  const btn = $('kokoroPreviewBtn');
  btnBusy(btn, true);
  $('kokoroHint').textContent = 'Rendering a sample (the first one loads the model, which can take a minute)…';
  try {
    const res = await fetch(`/api/tts/kokoro/preview?voice=${encodeURIComponent($('kokoroVoice').value)}`);
    if (!res.ok) throw new Error((await res.json()).error || 'Preview failed.');
    const audio = $('kokoroSample');
    audio.src = URL.createObjectURL(await res.blob());
    audio.hidden = false;
    audio.play();
    $('kokoroHint').textContent = '';
  } catch (e) { $('kokoroHint').textContent = e.message; }
  finally { btnBusy(btn, false); }
}

function refreshVoiceActive() {
  if (!CURRENT_PROJECT) return;
  // mode reads the LIVE radio (updates instantly on click, before the
  // debounced save round-trips); the filename comes from the last-known
  // upload, kept in sync by fillSettingsForm() and uploadAsset().
  const mode = document.querySelector('input[name=voiceMode]:checked').value;
  const file = CURRENT_PROJECT.settings.voice.file;
  const custom = mode === 'file' && file;
  $('voiceActiveHint').textContent = custom
    ? `Active: ${file.split('/').pop()} (project)` : 'Active: default (assets/voice_ref.wav)';
  const audio = $('voicePreview');
  if (custom) {
    audio.src = `/api/projects/${encodeURIComponent(CURRENT_PROJECT.slug)}/asset/voice?t=${Date.now()}`;
    audio.hidden = false;
  } else {
    audio.removeAttribute('src');
    audio.hidden = true;
  }
}

function currentSettingsFromForm() {
  return {
    music: { mode: document.querySelector('input[name=musicMode]:checked').value },
    voice: { mode: document.querySelector('input[name=voiceMode]:checked').value,
             engine: document.querySelector('input[name=voiceEngine]:checked').value,
             kokoro_voice: $('kokoroVoice').value },
    watermark: {
      enabled: $('wmEnabled').checked,
      type: document.querySelector('input[name=wmType]:checked').value,
      text: $('wmText').value,
      opacity: (+$('wmOpacity').value) / 100,
      size: (+$('wmSize').value) / 100,
      spacing: (+$('wmSpacing').value) / 100,
      angle: +$('wmAngle').value,
    },
    background: {
      mode: document.querySelector('input[name=bgMode]:checked').value,
      dim: (+$('bgDim').value) / 100,
    },
    animation: {
      style: document.querySelector('input[name=animStyle]:checked').value,
      direction: $('animDirection').value,
      duration: (+$('animDuration').value) / 100,
      zoom: $('animZoom').checked,
      zoom_amount: (+$('animZoomAmount').value) / 100,
    },
    framing: { blur_text: $('framingBlur').checked },
    script: {
      tone: $('scriptTone').value,
      narration_type: $('scriptNarrationType').value,
      style_reference: $('scriptStyleRef').value,
    },
    claude: {
      series_brief: $('claudeBrief').value,
      comprehension_model: $('claudeComprehensionModel').value,
      narration_model: $('claudeNarrationModel').value,
      min_frame_s: parseFloat($('claudeMinFrame').value) || 3,
    },
    video: { quality: $('videoQuality').value },
    batch: {
      review_before_audio: $('batchReview').checked,
      auto_wait_five_hour: $('batchAutoWait').checked,
      notifications: $('batchNotify').checked,
      five_hour_pct: parseInt($('batchFiveHour').value, 10) || 70,
      seven_day_pct: parseInt($('batchSevenDay').value, 10) || 60,
    },
  };
}

let settingsSaveTimer = null;
function saveSettingsDebounced() {
  updateSliderLabels();
  refreshVoiceActive();
  refreshWatermarkPreview();
  refreshBackgroundPreview();
  setSettingsSaveState('saving', 'Saving…');
  clearTimeout(settingsSaveTimer);
  settingsSaveTimer = setTimeout(async () => {
    try {
      const res = await fetch(`/api/projects/${encodeURIComponent(CURRENT_PROJECT.slug)}/settings`,
        { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(currentSettingsFromForm()) });
      const d = await res.json();
      if (!res.ok) throw new Error(d.error || 'save failed');
      CURRENT_PROJECT.settings = d.settings;
      paintBlurTool();
      setSettingsSaveState('ok', 'Autosaved ' + new Date().toLocaleTimeString());
    } catch (e) { setSettingsSaveState('err', 'Save failed'); }
  }, 400);
}

let wmPreviewTimer = null;
function refreshWatermarkPreview() {
  clearTimeout(wmPreviewTimer);
  wmPreviewTimer = setTimeout(() => {
    if (!CURRENT_PROJECT) return;
    const type = document.querySelector('input[name=wmType]:checked').value;
    // `enabled` must travel with the preview request: the render (via
    // media_settings_snapshot) omits the watermark entirely when this
    // checkbox is off, so the preview has to match or it lies about what
    // will actually be in the video.
    const q = new URLSearchParams({
      enabled: $('wmEnabled').checked ? '1' : '0',
      type, text: $('wmText').value,
      opacity: (+$('wmOpacity').value) / 100, size: (+$('wmSize').value) / 100,
      spacing: (+$('wmSpacing').value) / 100, angle: $('wmAngle').value, t: Date.now(),
    });
    setPreviewImage('wmPreviewBox', `/api/projects/${encodeURIComponent(CURRENT_PROJECT.slug)}/preview/watermark.png?${q}`);
  }, 250);
}
let bgPreviewTimer = null;
function refreshBackgroundPreview() {
  clearTimeout(bgPreviewTimer);
  const mode = document.querySelector('input[name=bgMode]:checked').value;
  $('bgVideoHint').hidden = mode !== 'video';
  bgPreviewTimer = setTimeout(() => {
    if (!CURRENT_PROJECT) return;
    const dim = (+$('bgDim').value) / 100;
    if (mode === 'video') {
      // A real <video> (looped, muted, dimmed via CSS) - not a rendered still,
      // since the whole point is showing that it actually plays/loops. Reuses
      // the plain asset-download route (send_from_directory supports HTTP
      // range requests, which <video> needs for seeking/looping).
      const box = $('bgPreviewBox');
      box.innerHTML = '';
      const video = document.createElement('video');
      video.autoplay = true; video.loop = true; video.muted = true; video.playsInline = true;
      video.style.cssText = 'width:100%;height:100%;object-fit:cover;display:block';
      video.style.filter = `brightness(${dim})`;
      video.onerror = () => { box.innerHTML = '<span class="phmsg">No video uploaded yet</span>'; };
      video.src = `/api/projects/${encodeURIComponent(CURRENT_PROJECT.slug)}/asset/background?t=${Date.now()}`;
      box.appendChild(video);
      return;
    }
    const q = new URLSearchParams({ dim, t: Date.now() });
    setPreviewImage('bgPreviewBox', `/api/projects/${encodeURIComponent(CURRENT_PROJECT.slug)}/preview/background.png?${q}`);
  }, 250);
}
function setPreviewImage(boxId, url) {
  const box = $(boxId);
  const img = new Image();
  img.onload = () => { box.innerHTML = ''; box.appendChild(img); };
  img.onerror = () => { box.innerHTML = '<span class="phmsg">Preview unavailable</span>'; };
  img.src = url;
}

const ASSET_UI = {
  music: { name: 'musicName', browse: 'musicBrowseBtn' },
  voice: { name: 'voiceName', browse: 'voiceBrowseBtn' },
  watermark: { name: 'wmFileName', browse: 'wmBrowseBtn' },
  background: { name: 'bgFileName', browse: 'bgBrowseBtn' },
};
let uploadsInFlight = 0;
window.addEventListener('beforeunload', e => { if (uploadsInFlight) e.preventDefault(); });

// fetch() can't report upload progress, XHR can.
function postWithProgress(url, body, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', url);
    xhr.responseType = 'json';
    xhr.upload.onprogress = e => { if (e.lengthComputable) onProgress(e.loaded / e.total); };
    xhr.onload = () => (xhr.status >= 200 && xhr.status < 300)
      ? resolve(xhr.response)
      : reject(new Error((xhr.response && xhr.response.error) || 'upload failed'));
    xhr.onerror = () => reject(new Error('connection lost during upload'));
    xhr.send(body);
  });
}

// The file only counts as set once the server has stored it: until then `nameEl`
// shows live progress and the old value is kept for a failed upload. Resolves
// with the server's response; the caller records the new file.
async function uploadAssetFile(kind, file, nameEl, browseBtn, onLabel) {
  const previousName = nameEl.textContent;
  const bar = document.createElement('span');
  bar.className = 'uploadbar';
  bar.innerHTML = '<span></span>';
  nameEl.after(bar);
  const setProgress = frac => {
    bar.firstChild.style.width = Math.round(frac * 100) + '%';
    const label = frac < 1 ? `Uploading… ${Math.round(frac * 100)}%` : 'Saving…';
    nameEl.textContent = label;
    onLabel(label);
  };

  const fd = new FormData();
  fd.append('kind', kind);
  fd.append('file', file);
  uploadsInFlight++;
  btnBusy(browseBtn, true);
  setProgress(0);
  try {
    return await postWithProgress(`/api/projects/${encodeURIComponent(CURRENT_PROJECT.slug)}/asset`, fd, setProgress);
  } catch (e) {
    nameEl.textContent = previousName;
    throw e;
  } finally {
    uploadsInFlight--;
    bar.remove();
    btnBusy(browseBtn, false);
  }
}

async function uploadAsset(kind, file) {
  if (!file || !CURRENT_PROJECT) return;
  const nameEl = $(ASSET_UI[kind].name);
  try {
    const d = await uploadAssetFile(kind, file, nameEl, $(ASSET_UI[kind].browse),
      label => setSettingsSaveState('saving', label));
    nameEl.textContent = d.name;
    setSettingsSaveState('ok', 'Uploaded ' + new Date().toLocaleTimeString());
    showToast(kind + ' uploaded.', null, null, 'ok');
    if (kind === 'voice') { CURRENT_PROJECT.settings.voice.file = d.path; refreshVoiceActive(); }
    if (kind === 'watermark') refreshWatermarkPreview();
    if (kind === 'background') refreshBackgroundPreview();
  } catch (e) {
    setSettingsSaveState('err', 'Upload failed');
    setStatus('Upload error: ' + e.message, true);
  }
}

// --- opening a chapter in the editor ------------------------------------------
function resetEditorState() {
  // Render-job UI is reset SYNCHRONOUSLY here (not left stale from whatever
  // chapter was open before) and then re-synced from the server a moment
  // later in openChapterEditor() below (pollJobs()) - reset-then-verify, so
  // "navigate away and back" never shows a leftover busy state that isn't
  // real (see syncEditorJobUI()).
  S.watchedJobId = null;
  setGenBusy(false);
  $('genProgress').classList.remove('err');
  S.chapter_id = null; S.source_pdf = null; S.strip_w = 0; S.strip_h = 0;
  S.preview_w = 0; S.preview_h = 0; S.tiles = [];
  S.lines = []; S.active = -1; S.activeFrame = -1;
  closeClaudeFeed();
  setStripLocked(false);
  readState.panels = []; readState.read = new Set(); readState.dropped = new Set(); readState.current = null;
  $('claudeFeed').hidden = true;
  discardPendingAutosave();
  undoStack = []; redoStack = []; updateUndoUI();
  tilesDiv.innerHTML = ''; boxesDiv.innerHTML = '';
  $('scriptText').value = ''; $('txtPath').value = '';
  $('pdfPath').value = ''; $('dlUrl').value = ''; $('dlNo').value = '';
  $('genStatus').textContent = ''; clearGenResult();
  $('stripInfo').textContent = 'No strip loaded.';
  $('dlBtn').disabled = false; $('dlStatus').hidden = true; $('dlStatus').innerHTML = '';
  $('loadPdfBtn').disabled = false; $('stitchStatus').hidden = true; $('stitchStatus').innerHTML = '';
  setSaveState('');
  renderLines();
  if (document.body.classList.contains('stripFullscreen')) setFullscreen(false);
  if (document.body.classList.contains('scriptOnlyView')) setScriptOnly(false);
}

async function openChapterEditor(slug, chNo, opts) {
  opts = opts || {};
  resetEditorState();
  S.project = slug; S.ch_no = String(chNo);
  S.chapter_id = `${slug}_ch${chNo}`;
  $('chapId').value = S.chapter_id;
  showScreen('editor');
  pollJobs();   // don't wait for the next 1.5s tick - this chapter may already have a job
  if (!CURRENT_PROJECT || CURRENT_PROJECT.slug !== slug) await loadCurrentProject(slug);
  loadClaudeStatus();

  if (opts.autoDownload) {
    $('dlUrl').value = opts.url || '';
    $('dlNo').value = String(chNo);
    await doDownload();
    return;
  }

  // Everything shown here is re-derived from disk: the source/strip/video
  // panels, and the script + frames (the newest populated save, else the
  // exported mapping - see _resolve_editor_state in app.py).
  await refreshChapterSourceStatus();
  await loadEditorState(false, true);
  if (!S.lines.length) setStatus(`Opened ${S.chapter_id}. Download or stitch a PDF to begin.`);
}

function backToProject() { navTo(S.project ? { project: S.project } : {}); }

// ---------------------------------------------------------------------------
// in-app update - pulls the app's own code from git (see /api/update in
// app.py). The pull itself never touches .venv/packages/the model cache; any
// package the new code needs is offered for install right after (see below)
// BEFORE the restart, so the restarted app has everything it imports.
// ---------------------------------------------------------------------------
async function checkForUpdate() {
  const box = $('updateResult');
  box.classList.remove('on', 'ok', 'warn');
  $('depsResult').innerHTML = '';
  btnBusy($('checkUpdateBtn'), true);
  try {
    const res = await fetch('/api/update', { method: 'POST' });
    const d = await res.json();
    const missing = d.missing_packages || [];
    const tools = d.system_tools || [];
    const kind = (d.ok && !missing.length) ? 'ok' : 'warn';
    // An update was actually pulled (not "already up to date", not a
    // failure) - offer to apply it right now instead of leaving the user to
    // close and reopen start.bat themselves.
    setFetchResult(box, d.message || (d.ok ? 'Updated.' : 'Update check failed.'), kind,
                   d.ok && !d.already_up_to_date && !missing.length ? restartApp : null);
    if (!d.ok) showToast('Update check failed.', null, null, 'err');
    else if (missing.length || tools.length) showMissingPackagesPrompt(missing, tools);
    else if (d.already_up_to_date) showToast('Already up to date.', null, null, 'ok');
    else showToast(d.message, null, null, 'ok');
  } catch (e) {
    setFetchResult(box, 'Update check failed: ' + e.message, 'warn');
    showToast('Update check failed.', null, null, 'err');
  } finally {
    btnBusy($('checkUpdateBtn'), false);
  }
}

// --- dependency self-repair: missing packages after a pull (see api_deps_check
// / api_deps_install_stream in app.py) --------------------------------------
function showMissingPackagesPrompt(missing, tools) {
  const rows = missing.map(m =>
    `<div class="gprow"><span class="gpname">${escHtml(m.package)}</span>` +
    `<span class="gpmeta">needed for: ${escHtml(m.feature)}</span></div>`).join('');
  const toolRows = tools.map(tool =>
    `<div class="gprow"><span class="gpname">${escHtml(tool.name)}</span>` +
    `<span class="gpmeta">${escHtml(tool.detail)}</span></div>`).join('');
  const install = missing.length
    ? `<div class="jobacts">
         <button class="small primary" type="button" id="depsInstallBtn">${ic('download',13)}<span>Install missing packages</span></button>
       </div>`
    : '';
  $('depsResult').innerHTML =
    `<div class="jobcard">
       <div class="jobhead">${ic('alert',15)}<b>${missing.length ? `${missing.length} package(s) not installed` : 'Programs missing from this PC'}</b></div>
       <div class="gplist">${rows}${toolRows}</div>
       ${install}
       <div class="progress" id="depsProgress"><div class="bar" id="depsBar"></div></div>
       <div class="genlog" id="depsLog"></div>
     </div>`;
  if (missing.length) $('depsInstallBtn').onclick = installMissingPackages;
}

let depsES = null;
function installMissingPackages() {
  if (depsES) { depsES.close(); depsES = null; }
  const btn = $('depsInstallBtn');
  btnBusy(btn, true);
  const log = $('depsLog');
  log.textContent = '';
  setGlobalProg(null, 'Installing packages');
  $('depsProgress').classList.remove('err');
  $('depsProgress').classList.add('indet');

  depsES = new EventSource('/api/deps/install_stream');
  depsES.onmessage = e => { try { handleDepsInstallEvent(JSON.parse(e.data)); } catch (_) {} };
  depsES.onerror = () => {
    if (!depsES) return;
    log.textContent += '\n[connection lost]';
    finishDepsInstall(false, 'Install stream disconnected.');
  };
}
function handleDepsInstallEvent(ev) {
  if (ev.type === 'log') {
    const log = $('depsLog');
    log.textContent += (log.textContent ? '\n' : '') + ev.line;
    log.scrollTop = log.scrollHeight;
  } else if (ev.type === 'done') {
    const cudaNote = ev.cuda_ok === false ? ' GPU acceleration did not come back - see SETUP_GUIDE.md.' : '';
    finishDepsInstall(ev.ok, ev.ok
      ? 'All packages installed.' + cudaNote
      : `${ev.missing.length} package(s) still missing - see the log above.`);
    if (ev.ok) {
      $('depsInstallBtn').disabled = true;   // nothing left to install
      setFetchResult($('updateResult'), 'Packages installed. Restart to apply the update.', 'ok', restartApp);
    }
  } else if (ev.type === 'error') {
    finishDepsInstall(false, ev.message || 'Install failed.');
  }
}
function finishDepsInstall(ok, message) {
  if (depsES) { depsES.close(); depsES = null; }
  clearGlobalProg();
  $('depsProgress').classList.remove('indet');
  $('depsProgress').classList.toggle('err', !ok);
  btnBusy($('depsInstallBtn'), false);
  showToast(message, null, null, ok ? 'ok' : 'err');
  setStatus(message, !ok);
}

// Fills a .fetchresult box with a message and, optionally, an action button -
// built with DOM nodes (not innerHTML) since `message` can come straight from
// git output. Shared by checkForUpdate() and restartApp() below.
function setFetchResult(box, message, kind, onAction, actionLabel) {
  box.textContent = '';
  box.classList.remove('ok', 'warn');
  const msg = document.createElement('span');
  msg.textContent = message;
  box.appendChild(msg);
  if (onAction) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'small primary';
    btn.innerHTML = ic('refresh', 13) + `<span>${actionLabel || 'Restart now'}</span>`;
    btn.onclick = onAction;
    box.appendChild(btn);
  }
  box.classList.add('on', kind);
}

// Applies a just-pulled update by restarting the Flask process (see
// POST /api/restart in app.py, which spawns a fresh process then exits this
// one) and reloading this page once the server answers again - so the user
// never has to manually close/reopen start.bat to pick up new code.
async function restartApp() {
  const box = $('updateResult');
  setFetchResult(box, 'Restarting…', 'warn');
  try {
    await fetch('/api/restart', { method: 'POST' });
  } catch (_) {
    // The process replaces itself mid-response - a dropped connection here
    // is the EXPECTED outcome, not a failure; the poll below is what matters.
  }
  const deadlineMs = Date.now() + 30000;
  const poll = async () => {
    if (Date.now() > deadlineMs) {
      setFetchResult(box, 'Restart is taking longer than expected - check the '
        + 'console window, or reload this page manually in a moment.', 'warn');
      return;
    }
    try {
      const r = await fetch('/__app_ping__', { cache: 'no-store' });
      if (r.ok) { location.reload(); return; }
    } catch (_) { /* still restarting - keep polling */ }
    setTimeout(poll, 700);
  };
  setTimeout(poll, 1000);   // give the old process a moment to actually exit first
}

// ---------------------------------------------------------------------------
// wire up
// ---------------------------------------------------------------------------
$('npCreateBtn').onclick = createProject;
$('npManualBtn').onclick = useManualRange;
$('checkUpdateBtn').onclick = checkForUpdate;
$('themeBtnP').onclick = toggleTheme;
$('importProjectBtn').onclick = () => $('importProjectFile').click();
$('importProjectFile').onchange = importProject;
$('exportProjectBtn').onclick = exportProject;
$('saveDefaultsBtn').onclick = saveDefaults;
$('applyDefaultsBtn').onclick = () => applyDefaults(false);
$('applyDefaultsAllBtn').onclick = () => applyDefaults(true);
$('npToggleBtn').onclick = () => $('npComposer').classList.toggle('on');
$('npCloseBtn').onclick = () => $('npComposer').classList.remove('on');
$('backToProjectsBtn').onclick = () => navTo({});
$('backToProjectBtn').onclick = backToProject;
$('pjRefetchBtn').onclick = refetchChapters;
$('pjMoreBtn').onclick = downloadMoreChapters;
$('pjDeleteBtn').onclick = deleteProject;
$('npFolderBtn').onclick = saveProjectsFolder;
$('mergeBtn').onclick = startMerge;
$('mergeCancelBtn').onclick = cancelMerge;
$('pjCrumbProjects').onclick = () => navTo({});
$('pjSettingsBtn').onclick = () => navTo({ project: CURRENT_PROJECT.slug, view: 'settings' });
$('pjChaptersNav').onclick = () => navTo({ project: CURRENT_PROJECT.slug });
[['pj', 'Merged', 'merged'], ['pj', 'Library', 'library'], ['st', 'Merged', 'merged'], ['st', 'Library', 'library']]
  .forEach(([screen, name, view]) => $(`${screen}${name}Nav`).onclick = () => navTo({ project: CURRENT_PROJECT.slug, view }));
$('hubAllProjectsBtn').onclick = $('hubCrumbProjects').onclick = () => navTo({});
$('hubChaptersNav').onclick = $('hubCrumbProject').onclick = () => navTo({ project: CURRENT_PROJECT.slug });
$('hubSettingsNav').onclick = () => navTo({ project: CURRENT_PROJECT.slug, view: 'settings' });
$('hubMergedNav').onclick = () => navTo({ project: CURRENT_PROJECT.slug, view: 'merged' });
$('hubLibraryNav').onclick = () => navTo({ project: CURRENT_PROJECT.slug, view: 'library' });
$('hubRefreshBtn').onclick = () => openHubScreen(CURRENT_PROJECT.slug, HUB_VIEW);
$('stAllProjectsBtn').onclick = () => navTo({});
$('stCrumbProjects').onclick = () => navTo({});
$('stCrumbProject').onclick = () => navTo({ project: CURRENT_PROJECT.slug });
$('stChaptersNav').onclick = () => navTo({ project: CURRENT_PROJECT.slug });
document.querySelectorAll('input[name=voiceEngine]').forEach(el => el.onchange = () => { paintVoiceEngine(); saveSettingsDebounced(); });
$('kokoroVoice').onchange = saveSettingsDebounced;
$('kokoroPreviewBtn').onclick = previewKokoro;
loadKokoroVoices();
['musicMode', 'voiceMode', 'wmType', 'bgMode', 'animStyle'].forEach(name => {
  document.querySelectorAll(`input[name=${name}]`).forEach(el => el.onchange = saveSettingsDebounced);
});
$('wmEnabled').onchange = saveSettingsDebounced;
$('wmText').oninput = saveSettingsDebounced;
$('animDirection').onchange = saveSettingsDebounced;
$('animZoom').onchange = saveSettingsDebounced;
$('animZoomAmount').oninput = () => {
  $('animZoomAmountLbl').textContent = `${(+$('animZoomAmount').value / 100).toFixed(2)}x`;
  saveSettingsDebounced();
};
$('framingBlur').onchange = saveSettingsDebounced;
['wmOpacity', 'wmSize', 'wmSpacing', 'wmAngle', 'bgDim', 'animDuration'].forEach(id => { $(id).oninput = saveSettingsDebounced; });
$('scriptTone').onchange = saveSettingsDebounced;
$('scriptNarrationType').onchange = saveSettingsDebounced;
$('scriptStyleRef').oninput = saveSettingsDebounced;
$('claudeBrief').oninput = saveSettingsDebounced;
$('claudeComprehensionModel').onchange = saveSettingsDebounced;
$('claudeNarrationModel').onchange = saveSettingsDebounced;
$('claudeMinFrame').oninput = saveSettingsDebounced;
$('videoQuality').onchange = saveSettingsDebounced;
$('batchReview').onchange = saveSettingsDebounced;
$('batchAutoWait').onchange = saveSettingsDebounced;
$('batchNotify').onchange = saveSettingsDebounced;
$('batchFiveHour').oninput = saveSettingsDebounced;
$('batchSevenDay').oninput = saveSettingsDebounced;
$('musicFile').onchange = e => uploadAsset('music', e.target.files[0]);
$('voiceFile').onchange = e => uploadAsset('voice', e.target.files[0]);
$('wmFile').onchange = e => uploadAsset('watermark', e.target.files[0]);
$('bgFile').onchange = e => uploadAsset('background', e.target.files[0]);
$('musicBrowseBtn').onclick = () => $('musicFile').click();
$('voiceBrowseBtn').onclick = () => $('voiceFile').click();
$('wmBrowseBtn').onclick = () => $('wmFile').click();
$('bgBrowseBtn').onclick = () => $('bgFile').click();

$('dlBtn').onclick = doDownload;
$('loadPdfBtn').onclick = loadPdf;
$('loadScriptBtn').onclick = loadScript;
$('genScriptBtn').onclick = startClaudeScript;
$('addLineBtn').onclick = addLine;
$('exportBtn').onclick = doExport;
$('genAVBtn').onclick = () => startGenerate('both');
$('genVoiceBtn').onclick = () => startGenerate('voice');
$('genVidBtn').onclick = () => startGenerate('video');
$('genCancelBtn').onclick = cancelGenerate;
$('frameModeBtn').onclick = () => setMode('frame');
$('blurModeBtn').onclick = () => setMode('blur');
$('blurStyle').onchange = e => { S.blurStyle = e.target.value; };
$('blurShape').onchange = e => { S.blurShape = e.target.value; };
$('undoBtn').onclick = undo;
$('redoBtn').onclick = redo;
$('saveProjBtn').onclick = async () => { btnBusy($('saveProjBtn'), true); await saveEditorState(false); btnBusy($('saveProjBtn'), false); };
$('loadProjBtn').onclick = async () => {
  btnBusy($('loadProjBtn'), true);
  await loadEditorState(false);
  await refreshChapterSourceStatus();
  btnBusy($('loadProjBtn'), false);
};
$('themeBtn').onclick = toggleTheme;
$('srcToggle').onclick = toggleSrc;
$('fullscreenBtn').onclick = () => setFullscreen(!document.body.classList.contains('stripFullscreen'));
$('scriptOnlyBtn').onclick = () => setScriptOnly(!document.body.classList.contains('scriptOnlyView'));
window.addEventListener('keydown', e => {
  if (e.key === 'Escape' && document.body.classList.contains('stripFullscreen')) setFullscreen(false);
});

// pdf file picker proxy: the folder icon just opens the (visually hidden) native
// file input; loadPdf() already reads $('pdfFile').files[0] unchanged.
$('pdfBrowseBtn').onclick = () => $('pdfFile').click();
$('pdfFile').addEventListener('change', () => {
  // loadPdf() already prefers $('pdfFile').files[0] over the path field when both
  // are set - just mirror the chosen name into the path field for visual feedback
  // (the hidden native input gives none of its own).
  const f = $('pdfFile').files[0];
  if (f) $('pdfPath').value = f.name;
});

// activity log: collapse (client-only) + clear
$('actCollapseBtn').onclick = () => $('actpanel').classList.toggle('collapsed');
$('actClearBtn').onclick = () => { $('status').innerHTML = ''; };

// ---------------------------------------------------------------------------
// mobile layout: sidebar slide-over drawer + bottom tab bar (Strip / Script).
// Both are purely client-side view state; every control they show/hide is the
// exact same element (same id) used on desktop, so no backend call changes.
// ---------------------------------------------------------------------------
function isMobileLayout() {
  return window.matchMedia('(max-width:900px), (hover:none) and (max-width:1100px)').matches;
}
function openSidebar(on) { document.body.classList.toggle('sidebarOpen', on); }
$('sidebarToggleBtn').onclick = () => openSidebar(!document.body.classList.contains('sidebarOpen'));
$('sidebarBackdrop').onclick = () => openSidebar(false);

const MOBILE_TAB_KEY = 'framerMobileTab';
function setMobileTab(tab, persist) {
  document.body.dataset.mobiletab = tab;
  document.querySelectorAll('.tabbar .tabbtn').forEach(b => b.classList.toggle('on', b.dataset.tab === tab));
  if (persist !== false) { try { sessionStorage.setItem(MOBILE_TAB_KEY, tab); } catch (_) {} }
  if (tab === 'strip' && S.preview_w) { computeZoomRange(); layout(); redrawBoxes(); }
}
document.querySelectorAll('.tabbar .tabbtn').forEach(b => b.onclick = () => { setMobileTab(b.dataset.tab); openSidebar(false); });
(function initMobileLayout() {
  let savedTab = 'strip';
  try { savedTab = sessionStorage.getItem(MOBILE_TAB_KEY) || 'strip'; } catch (_) {}
  setMobileTab(savedTab, false);
  if (isMobileLayout()) toggleSrc();   // Source drawer starts closed on mobile to give the strip height
})();

setMode('frame');
updateUndoUI();
loadSettings();
paintIcons();
setStatus('Ready. Stitch a PDF, load a script, then click a line and drag a box on the strip. Ctrl+Z undoes edits.');
route();   // deep-link into ?project=&chapter=, else show the Projects landing screen
startJobsPolling();   // global render-job poll - drives the corner widget on every screen
