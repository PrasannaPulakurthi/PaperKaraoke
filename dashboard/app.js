/* PaperKaraoke frontend.
   Generation is per page: the server streams each page's audio + word timing as
   it finishes. We render all PDF pages up front, then play the page segments in
   order as one continuous track — starting page 1 while later pages are still
   being synthesized — with a highlight that follows the spoken word. */

const pdfjsLib = window['pdfjsLib'];
pdfjsLib.GlobalWorkerOptions.workerSrc =
  'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';

const $ = (id) => document.getElementById(id);
const selectEl  = $('pdfSelect');
const uploadBtn = $('uploadBtn');
const fileInput = $('fileInput');
const statusEl  = $('status');
const progressEl= $('progress');
const barFill   = $('barFill');
const progMsg   = $('progMsg');
const pagesEl   = $('pages');
const emptyEl   = $('empty');
const viewerEl  = $('viewer');
const audio     = $('audio');
const playBtn   = $('playBtn');
const seek      = $('seek');
const curEl     = $('cur');
const durEl     = $('dur');
const rateEl    = $('rate');
const zoomInB   = $('zoomIn');
const zoomOutB  = $('zoomOut');
const zoomLabel = $('zoomLabel');

// zoom state
let pdfDoc = null;        // loaded PDF.js document (for re-render on zoom)
let renderSizes = [];     // page sizes in PDF points
let baseScale = 1;        // fit-to-width scale (== 100%)
let zoom = 1;             // user zoom multiplier
let renderSeq = 0;        // guards against overlapping re-renders

// per-run state
let totalPages = 0;
let pageSizes = [];
let pageDivs = [];        // .page container per page
let hl = null;
let segs = [];            // segs[i] = {url, events, dur(ms), empty} once fetched
let gotPages = new Set(); // page indices fetched
let curSeg = -1;          // page index currently loaded in <audio>
let waiting = null;       // {i, autoplay} when playback is stalled awaiting a page
let pendingOffset = null; // seconds to seek to after the next load
let wantPlay = false;
let lastPage = -1;
let rendered = false;
let pollTimer = null;
let allDone = false;
let curPdf = null;

function fmt(t) {
  if (!isFinite(t)) return '0:00';
  t = Math.max(0, Math.floor(t));
  return Math.floor(t / 60) + ':' + String(t % 60).padStart(2, '0');
}
function setStatus(msg, isErr) {
  statusEl.textContent = msg || '';
  statusEl.classList.toggle('err', !!isErr);
}
function showProgress(pct, msg) {
  progressEl.hidden = false;
  barFill.style.width = Math.round((pct || 0) * 100) + '%';
  progMsg.textContent = msg || '';
}
function hideProgress() { progressEl.hidden = true; }
function busy(on) { selectEl.disabled = on; uploadBtn.disabled = on; }

// ---------- pdf list ----------
async function loadPdfList(selected) {
  const { pdfs } = await (await fetch('/api/pdfs')).json();
  selectEl.innerHTML = '<option value="">— select a paper —</option>';
  for (const p of pdfs) {
    const o = document.createElement('option');
    o.value = p; o.textContent = p;
    selectEl.appendChild(o);
  }
  if (selected) selectEl.value = selected;
}

// ---------- start / poll generation ----------
function startGeneration(pdfName) {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  resetRun();
  curPdf = pdfName;
  busy(true);
  setStatus('');
  showProgress(0, 'Starting…');
  fetch('/api/generate?pdf=' + encodeURIComponent(pdfName), { method: 'POST' })
    .then(() => { pollTimer = setInterval(poll, 700); poll(); })
    .catch((e) => failGen(e.message));
}

async function poll() {
  let s;
  try { s = await (await fetch('/api/progress?pdf=' + encodeURIComponent(curPdf))).json(); }
  catch (e) { return; }
  if (s.state === 'error') { failGen(s.error || 'generation failed'); return; }

  if (!rendered && s.page_sizes && s.pdf_url) {
    rendered = true;
    totalPages = s.n_pages;
    pageSizes = s.page_sizes;
    segs = new Array(totalPages);
    await renderPdf(s.pdf_url, pageSizes);
  }

  if (s.state !== 'done') showProgress(s.pct || 0, s.msg || 'Working…');

  // fetch each newly-ready page's events once
  for (const idx of (s.ready || [])) {
    if (!gotPages.has(idx)) { gotPages.add(idx); fetchPage(idx); }
  }

  if (s.state === 'done') {
    allDone = true;
    if (gotPages.size >= totalPages) {
      clearInterval(pollTimer); pollTimer = null;
      hideProgress();
    }
    updateTimeline();
  }
  busy(false);
}

function failGen(msg) {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  hideProgress(); busy(false);
  setStatus('Failed: ' + msg, true);
}

async function fetchPage(idx) {
  let e;
  try { e = await (await fetch('/api/page?pdf=' + encodeURIComponent(curPdf) + '&page=' + idx)).json(); }
  catch (err) { gotPages.delete(idx); return; }         // retry next poll
  if (e.error) { gotPages.delete(idx); return; }
  segs[idx] = { url: e.audio_url || null, events: e.events || [],
                dur: e.duration_ms || 0, empty: !!e.empty };
  onSegFetched(idx);
}

// ---------- upload ----------
async function uploadAndGenerate(file) {
  resetRun();
  busy(true);
  showProgress(0, 'Uploading…');
  let j;
  try {
    const r = await fetch('/api/upload?name=' + encodeURIComponent(file.name),
                          { method: 'POST', body: file });
    j = await r.json();
    if (j.error) throw new Error(j.error);
  } catch (e) { failGen('upload — ' + e.message); return; }
  await loadPdfList(j.pdf);
  startGeneration(j.pdf);
}

// ---------- pdf render ----------
function resetRun() {
  audio.pause();
  audio.removeAttribute('src');
  playBtn.disabled = true; seek.disabled = true; seek.value = 0;
  curEl.textContent = '0:00'; durEl.textContent = '0:00';
  totalPages = 0; pageSizes = []; pageDivs = []; hl = null;
  segs = []; gotPages = new Set(); curSeg = -1; waiting = null;
  pendingOffset = null; wantPlay = false; lastPage = -1;
  rendered = false; allDone = false;
  pdfDoc = null; renderSizes = []; zoom = 1; updateZoomLabel();
  pagesEl.innerHTML = '';
  emptyEl.style.display = 'none';
}

async function renderPdf(url, sizes) {
  renderSizes = sizes;
  pdfDoc = await pdfjsLib.getDocument(url).promise;
  const avail = Math.min(viewerEl.clientWidth - 40, 1000);
  const maxW = Math.max(...sizes.map(p => p.w));
  baseScale = Math.max(0.6, Math.min(1.8, avail / maxW));   // fit-to-width == 100%
  zoom = 1;
  updateZoomLabel();
  await renderAllPages();
}

// (Re)render every page canvas at the current zoom. Crisp (not CSS-scaled).
async function renderAllPages() {
  if (!pdfDoc) return;
  const seq = ++renderSeq;
  const eff = baseScale * zoom;
  pagesEl.innerHTML = '';
  pageDivs = new Array(renderSizes.length).fill(null);
  lastPage = -1;
  for (let i = 0; i < pdfDoc.numPages; i++) {
    const page = await pdfDoc.getPage(i + 1);
    if (seq !== renderSeq) return;                 // a newer render superseded us
    const viewport = page.getViewport({ scale: eff });
    const div = document.createElement('div');
    div.className = 'page';
    div.style.width = viewport.width + 'px';
    div.style.height = viewport.height + 'px';
    div.dataset.scale = viewport.width / renderSizes[i].w;   // canvas px per PDF point
    const canvas = document.createElement('canvas');
    canvas.width = viewport.width; canvas.height = viewport.height;
    div.appendChild(canvas);
    pagesEl.appendChild(div);
    pageDivs[i] = div;
    page.render({ canvasContext: canvas.getContext('2d'), viewport });
  }
  if (seq !== renderSeq) return;
  hl = document.createElement('div');
  hl.className = 'hl';
  pageDivs[0].appendChild(hl);
  if (curSeg >= 0) moveHighlight(curSeg, audio.currentTime * 1000);  // keep box placed
}

function updateZoomLabel() { zoomLabel.textContent = Math.round(zoom * 100) + '%'; }

function setZoom(z) {
  if (!pdfDoc) return;
  zoom = Math.max(0.4, Math.min(4, z));
  updateZoomLabel();
  renderAllPages();
}

// ---------- playback engine (segments played in order) ----------
function prefixCount() {                 // pages 0..k-1 all fetched
  let k = 0;
  while (k < totalPages && segs[k]) k++;
  return k;
}
function prefixDur() {                    // ms of the contiguous ready prefix
  let d = 0;
  for (let i = 0; i < prefixCount(); i++) d += segs[i].dur;
  return d;
}
function baseOf(i) {                      // ms before segment i (prior segs fetched)
  let d = 0;
  for (let j = 0; j < i; j++) d += (segs[j] ? segs[j].dur : 0);
  return d;
}

function playSeg(i, offsetSec, autoplay) {
  curSeg = i;
  pendingOffset = offsetSec || 0;
  wantPlay = autoplay;
  audio.src = segs[i].url;
  audio.load();
}

function advance(i, autoplay) {          // find next playable segment from i
  while (i < totalPages && segs[i] && segs[i].empty) i++;   // skip known empties
  if (i >= totalPages && allDone) { onFinished(); return; }
  if (i >= totalPages) { waiting = { i, autoplay }; return; }
  if (!segs[i]) {                        // not fetched yet → stall
    waiting = { i, autoplay };
    if (autoplay) setStatus(`Buffering page ${i + 1}…`);
    return;
  }
  waiting = null;
  playSeg(i, 0, autoplay);
}

function onSegFetched(idx) {
  updateTimeline();
  if (prefixCount() >= 1 && playBtn.disabled) playBtn.disabled = false;
  if (waiting) {                         // were we stalled on this (or a skipped) page?
    let i = waiting.i;
    while (i < totalPages && segs[i] && segs[i].empty) i++;
    if (i < totalPages && segs[i]) { const w = waiting; waiting = null; advance(w.i, w.autoplay); }
  }
}

function onFinished() {
  setStatus('Done.');
  playBtn.textContent = '▶';
}

function updateTimeline() {
  const d = prefixDur();
  seek.max = Math.floor(d) || 1;
  seek.disabled = prefixCount() === 0;
  durEl.textContent = fmt(d / 1000) + (allDone ? '' : '…');
}

function moveHighlight(segIndex, tMs) {
  if (!hl) return;
  const seg = segs[segIndex];
  if (!seg || !seg.events.length) { hl.style.display = 'none'; return; }
  const ev = seg.events;
  let lo = 0, hi = ev.length - 1, idx = -1;
  while (lo <= hi) { const m = (lo + hi) >> 1; if (ev[m].t0 <= tMs) { idx = m; lo = m + 1; } else hi = m - 1; }
  if (idx < 0) { hl.style.display = 'none'; return; }
  const e = ev[idx];
  const div = pageDivs[segIndex];
  if (!div) return;
  if (segIndex !== lastPage) { div.appendChild(hl); lastPage = segIndex; }
  const s = parseFloat(div.dataset.scale);
  hl.style.display = 'block';
  hl.style.left   = (e.x0 * s) + 'px';
  hl.style.top    = (e.y0 * s) + 'px';
  hl.style.width  = ((e.x1 - e.x0) * s) + 'px';
  hl.style.height = ((e.y1 - e.y0) * s) + 'px';
  const cr = viewerEl.getBoundingClientRect(), hr = hl.getBoundingClientRect();
  if (hr.top < cr.top + 60 || hr.bottom > cr.bottom - 60)
    hl.scrollIntoView({ block: 'center', behavior: 'smooth' });
}

// ---------- wiring ----------
selectEl.addEventListener('change', () => { if (selectEl.value) startGeneration(selectEl.value); });
uploadBtn.addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', () => {
  if (fileInput.files[0]) uploadAndGenerate(fileInput.files[0]);
  fileInput.value = '';
});

playBtn.addEventListener('click', () => {
  if (curSeg < 0) { advance(0, true); return; }        // first play
  if (audio.paused) audio.play(); else audio.pause();
});
audio.addEventListener('play',  () => playBtn.textContent = '⏸');
audio.addEventListener('pause', () => playBtn.textContent = '▶');
audio.addEventListener('loadedmetadata', () => {
  audio.playbackRate = Number(rateEl.value);   // re-apply speed (reset by new src)
  if (pendingOffset != null) { try { audio.currentTime = pendingOffset; } catch (e) {} pendingOffset = null; }
  updateTimeline();
  if (wantPlay) { audio.play(); wantPlay = false; }
});
audio.addEventListener('ended', () => advance(curSeg + 1, true));
audio.addEventListener('timeupdate', () => {
  if (curSeg < 0) return;
  const tMs = audio.currentTime * 1000;
  const globalMs = baseOf(curSeg) + tMs;
  seek.value = Math.floor(globalMs);
  curEl.textContent = fmt(globalMs / 1000);
  moveHighlight(curSeg, tMs);
});
seek.addEventListener('input', () => {
  const globalMs = Number(seek.value);
  // locate segment within the ready prefix
  let i = 0, acc = 0;
  const k = prefixCount();
  while (i < k && acc + segs[i].dur <= globalMs) { acc += segs[i].dur; i++; }
  if (i >= k) i = Math.max(0, k - 1), acc = baseOf(i);
  const offsetSec = Math.max(0, (globalMs - acc) / 1000);
  const autoplay = !audio.paused;
  if (i === curSeg) { try { audio.currentTime = offsetSec; } catch (e) {} }
  else if (segs[i] && !segs[i].empty) playSeg(i, offsetSec, autoplay);
  curEl.textContent = fmt(globalMs / 1000);
});
rateEl.addEventListener('change', () => { audio.playbackRate = Number(rateEl.value); });

// zoom controls
zoomInB.addEventListener('click', () => setZoom(zoom * 1.25));
zoomOutB.addEventListener('click', () => setZoom(zoom / 1.25));
zoomLabel.addEventListener('click', () => setZoom(1));          // reset to fit
viewerEl.addEventListener('wheel', (e) => {                     // Ctrl/⌘ + wheel
  if (!(e.ctrlKey || e.metaKey) || !pdfDoc) return;
  e.preventDefault();
  setZoom(zoom * (e.deltaY < 0 ? 1.1 : 1 / 1.1));
}, { passive: false });
window.addEventListener('keydown', (e) => {                     // Ctrl/⌘ + = / - / 0
  if (!(e.ctrlKey || e.metaKey) || !pdfDoc) return;
  if (e.key === '=' || e.key === '+') { e.preventDefault(); setZoom(zoom * 1.25); }
  else if (e.key === '-') { e.preventDefault(); setZoom(zoom / 1.25); }
  else if (e.key === '0') { e.preventDefault(); setZoom(1); }
});

loadPdfList();
