#!/usr/bin/env python3
"""Local web UI for previewing and clipping video files.

Launches a Flask server bound to 127.0.0.1 only (never exposed to network).
Open http://127.0.0.1:8765 to load a video, mark in/out points, and export
audio segments, video segments, IG-Story-ready 9:16 vertical clips, or
single frames as JPG. Clips are saved next to the source file.

Usage:
    python clipweb.py                    # opens browser automatically
    python clipweb.py --port 9000        # custom port
    python clipweb.py --no-browser       # don't auto-open browser
"""

import argparse
import re
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
from urllib.parse import unquote

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from ytclip import fmt_time  # noqa: E402

try:
    from flask import Flask, request, Response, send_file, jsonify
except ImportError:
    sys.stderr.write("Missing Flask. Install with: pip install flask\n")
    sys.exit(1)


app = Flask(__name__)


HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>ytclip web</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: system-ui, -apple-system, sans-serif; max-width: 1100px;
         margin: 20px auto; padding: 0 20px; background: #171719; color: #e0e0e0; }
  h1 { font-size: 22px; margin: 6px 0 16px; }
  h3 { font-size: 15px; margin: 18px 0 8px; color: #b0a0d0; }
  input[type=text], input[type=number], select {
    background: #26262b; border: 1px solid #444; color: #e0e0e0;
    padding: 6px 10px; border-radius: 4px; font-family: inherit; font-size: 14px;
  }
  button {
    background: #384a3a; border: 1px solid #567; color: #e0e0e0;
    padding: 6px 12px; border-radius: 4px; cursor: pointer;
    font-family: inherit; font-size: 13px;
  }
  button:hover { background: #4a6a4c; }
  button.big {
    background: #5a3a6a; border-color: #8a5a9a; padding: 12px 24px;
    font-size: 16px; font-weight: 600;
  }
  button.big:hover { background: #7a4a8a; }
  .row { display: flex; align-items: center; gap: 10px; margin: 8px 0;
         flex-wrap: wrap; }
  .row label { min-width: 80px; color: #aaa; font-size: 13px; }
  .path-row input { flex: 1; min-width: 300px; }
  .time-input { width: 100px !important; text-align: center; }
  .fmt-opts { display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; }
  .fmt-opts label {
    display: flex; align-items: center; gap: 8px; padding: 10px 14px;
    background: #26262b; border: 1px solid #333; border-radius: 4px;
    cursor: pointer; font-size: 14px; margin: 0; min-width: unset;
  }
  .fmt-opts label:hover { border-color: #567; }
  .fmt-opts label:has(input:checked) { background: #3a2a4a; border-color: #7a5a9a; }
  .info { color: #888; font-size: 13px; font-family: 'Consolas', monospace; }
  #log { background: #0f0f11; border: 1px solid #333; padding: 10px;
         margin-top: 10px; max-height: 220px; min-height: 60px; overflow-y: auto;
         font-family: 'Consolas', monospace; font-size: 12px;
         white-space: pre-wrap; color: #a0a0a0; border-radius: 4px; }
  .status-ok  { color: #90e090; }
  .status-err { color: #ff9090; }

  /* video area with optional crop overlay */
  .video-wrap { position: relative; width: 100%; margin-top: 8px;
                background: #000; border-radius: 6px; overflow: hidden;
                line-height: 0; }
  video { width: 100%; max-height: 60vh; display: block; }
  .crop-overlay { position: absolute; inset: 0; display: none; z-index: 3;
                  pointer-events: none; }
  .crop-overlay.active { display: block; }
  .crop-mask {
    position: absolute; inset: 0; pointer-events: auto;
    background:
      linear-gradient(transparent, transparent);
  }
  .crop-box {
    position: absolute; border: 2px dashed #d0a0ff;
    box-shadow: 0 0 0 9999px rgba(0, 0, 0, 0.55);
    cursor: move; pointer-events: auto;
    z-index: 4;
  }
  .crop-handle {
    position: absolute; width: 14px; height: 14px;
    background: #d0a0ff; border: 1.5px solid #fff; border-radius: 50%;
    z-index: 5; pointer-events: auto;
  }
  .ch-nw { top: -8px;   left: -8px;   cursor: nwse-resize; }
  .ch-ne { top: -8px;   right: -8px;  cursor: nesw-resize; }
  .ch-sw { bottom: -8px;left: -8px;   cursor: nesw-resize; }
  .ch-se { bottom: -8px;right: -8px;  cursor: nwse-resize; }
  .ch-n  { top: -8px;   left: 50%;    transform: translateX(-50%); cursor: ns-resize; }
  .ch-s  { bottom: -8px;left: 50%;    transform: translateX(-50%); cursor: ns-resize; }
  .ch-w  { top: 50%;    left: -8px;   transform: translateY(-50%); cursor: ew-resize; }
  .ch-e  { top: 50%;    right: -8px;  transform: translateY(-50%); cursor: ew-resize; }

  /* custom timeline */
  .timeline {
    position: relative; height: 44px; background: #1e1e22;
    border: 1px solid #333; border-radius: 6px; margin: 12px 0 4px;
    cursor: pointer; user-select: none;
  }
  .tl-track { position: absolute; inset: 18px 0; background: #2a2a30; border-radius: 3px; }
  .tl-selection {
    position: absolute; top: 6px; bottom: 6px;
    background: rgba(160, 100, 220, 0.28);
    border-left: 2px solid #b080e0; border-right: 2px solid #b080e0;
    pointer-events: none;
  }
  .tl-playhead {
    position: absolute; top: 0; bottom: 0; width: 2px;
    background: #f0f0f0; box-shadow: 0 0 4px rgba(255,255,255,0.5);
    pointer-events: none;
  }
  .tl-handle {
    position: absolute; top: 2px; bottom: 2px; width: 12px;
    background: #b080e0; border-radius: 3px; z-index: 2;
    cursor: ew-resize;
    box-shadow: 0 0 6px rgba(176, 128, 224, 0.5);
  }
  .tl-handle::after {
    content: ''; position: absolute; left: -4px; right: -4px;
    top: 0; bottom: 0;
  }
  .tl-handle-in  { transform: translateX(-100%); }
  .tl-handle-out { }

  /* vocal-onset markers */
  .tl-markers { position: absolute; top: 0; left: 0; right: 0; height: 14px;
                pointer-events: none; }
  .tl-marker  {
    position: absolute; top: 0; width: 0; height: 0;
    border-left: 5px solid transparent; border-right: 5px solid transparent;
    border-top: 8px solid #e0c060;
    transform: translateX(-50%);
    cursor: pointer; pointer-events: auto;
    filter: drop-shadow(0 0 2px rgba(224, 192, 96, 0.7));
  }
  .tl-marker:hover { border-top-color: #ffe080; }
  .tl-marker.hot   { border-top-color: #ff8080;
                     filter: drop-shadow(0 0 3px rgba(255, 128, 128, 0.9)); }
  .tl-chorus  {
    position: absolute; top: 14px; bottom: 4px; display: none;
    background: rgba(224, 192, 96, 0.16);
    border-left: 1px solid rgba(224, 192, 96, 0.6);
    border-right: 1px solid rgba(224, 192, 96, 0.6);
    pointer-events: none;
  }
  .tl-chorus.active { display: block; }
  .preset-row button { background: #3a3550; border-color: #6a5a8a; }
  .preset-row button:hover { background: #4e4670; }
  .preset-row button:disabled { background: #2a2a2e; border-color: #3a3a3e;
                                color: #666; cursor: not-allowed; }

  .times-row { display: flex; gap: 20px; font-size: 13px;
               font-family: 'Consolas', monospace; color: #aaa; margin: 8px 0; }
  .times-row .k { color: #888; }
  .times-row .v { color: #ddd; }
  .hotkey { color: #666; font-size: 12px; margin-left: auto; }
</style>
</head>
<body>
<h1>ytclip web · clip &amp; export</h1>

<div class="row path-row">
  <label>Source:</label>
  <input type="text" id="path" placeholder="D:\path\to\video.mp4" />
  <button onclick="loadVideo()">Load</button>
</div>

<div class="video-wrap" id="videoWrap">
  <video id="v" controls preload="metadata"></video>
  <div class="crop-overlay" id="cropOverlay">
    <div class="crop-box" id="cropBox">
      <div class="crop-handle ch-nw" data-h="nw"></div>
      <div class="crop-handle ch-ne" data-h="ne"></div>
      <div class="crop-handle ch-sw" data-h="sw"></div>
      <div class="crop-handle ch-se" data-h="se"></div>
      <div class="crop-handle ch-n"  data-h="n"></div>
      <div class="crop-handle ch-s"  data-h="s"></div>
      <div class="crop-handle ch-w"  data-h="w"></div>
      <div class="crop-handle ch-e"  data-h="e"></div>
    </div>
  </div>
</div>

<div class="timeline" id="timeline">
  <div class="tl-track"></div>
  <div class="tl-chorus" id="tlChorus"></div>
  <div class="tl-markers" id="tlMarkers"></div>
  <div class="tl-selection" id="tlSel"></div>
  <div class="tl-playhead" id="tlPlay"></div>
  <div class="tl-handle tl-handle-in"  id="tlIn"></div>
  <div class="tl-handle tl-handle-out" id="tlOut"></div>
</div>

<div class="row" style="margin: 4px 0 6px;">
  <button onclick="scanOnsets()" id="scanBtn" title="detect vocal entry points">🎤 scan vocal onsets</button>
  <button onclick="jumpPrevOnset()" title="p">⇤ prev onset</button>
  <button onclick="jumpNextOnset()" title="n">next onset ⇥</button>
  <button onclick="clearOnsets()" title="hide markers">✕ clear</button>
  <button onclick="cycleChorus()" id="chorusBtn" disabled
          title="the first chorus is a guess — cycle if it picked the wrong section">⟳ chorus 0/0</button>
  <span class="info" id="onsetInfo">no scan yet</span>
</div>

<div class="row preset-row" style="margin: 4px 0 6px;">
  <label>Presets:</label>
  <button onclick="preset('line1-end')"    id="pLineEnd"   disabled>1st line → end</button>
  <button onclick="preset('line1-chorus')" id="pLineChor"  disabled>1st line → chorus 1 end</button>
  <button onclick="preset('chorus')"       id="pChorus"    disabled>chorus 1 only</button>
  <button onclick="preset('line1-60')"     id="pLine60"    disabled>1st line +60s</button>
  <button onclick="preset('line1-90')"     id="pLine90"    disabled>1st line +90s</button>
  <button onclick="preset('trim')"         id="pTrim"      disabled>trim head/tail silence</button>
</div>

<div class="times-row">
  <span><span class="k">in</span>  <span class="v" id="inLbl">--:--</span></span>
  <span><span class="k">out</span> <span class="v" id="outLbl">--:--</span></span>
  <span><span class="k">length</span> <span class="v" id="lenLbl">--:--</span></span>
  <span><span class="k">cur</span> <span class="v" id="curLbl">--:--</span></span>
  <span><span class="k">total</span> <span class="v" id="durLbl">--:--</span></span>
  <span class="hotkey">[ ] set · ← → seek 5s · , . ±1 frame · k play · l loop · n/p next/prev onset</span>
</div>

<div class="row">
  <button onclick="setIn()"  title="[">[ Set In</button>
  <button onclick="setOut()" title="]">Set Out ]</button>
  <button onclick="jumpIn()">⇤ jump in</button>
  <button onclick="jumpOut()">jump out ⇥</button>
  <button onclick="playSel()">▶ selection</button>
  <button onclick="toggleLoop()" id="loopBtn">🔁 loop off</button>
  <label style="min-width:unset;">
    <input type="text" id="inTime"  class="time-input" value="00:00">
  </label>
  <label style="min-width:unset;">
    <input type="text" id="outTime" class="time-input" value="00:10">
  </label>
  <button onclick="applyTimeInputs()">apply</button>
</div>

<h3>Spatial crop (optional)</h3>
<div class="row">
  <label><input type="checkbox" id="cropEnabled" onchange="toggleCrop()"> enable crop</label>
  <label style="min-width:unset;">aspect:</label>
  <select id="cropAspect" onchange="applyAspect()">
    <option value="0">free</option>
    <option value="0.5625">9:16 (IG story)</option>
    <option value="1">1:1 (square)</option>
    <option value="0.8">4:5 (IG portrait)</option>
    <option value="1.7778">16:9 (widescreen)</option>
  </select>
  <button onclick="cropReset()">reset to full</button>
  <button onclick="cropCenter50()">center 50%</button>
  <span class="info" id="cropInfo">crop off</span>
</div>

<h3>Export mode</h3>
<div class="fmt-opts">
  <label><input type="radio" name="mode" value="audio" checked>
    Audio (.m4a, stream-copy · zero re-encode)</label>
  <label><input type="radio" name="mode" value="video">
    Video (.mp4, stream-copy · fast, keyframe-aligned)</label>
  <label><input type="radio" name="mode" value="video-re">
    Video (.mp4, re-encode · frame-accurate, honours crop)</label>
  <label><input type="radio" name="mode" value="frame">
    Frame at current time (.jpg, honours crop)</label>
  <label><input type="radio" name="mode" value="story-crop">
    <b>IG Story 9:16</b> center-crop · auto-split &gt;60s</label>
  <label><input type="radio" name="mode" value="story-blur">
    <b>IG Story 9:16</b> blur-pad · keeps full frame</label>
</div>
<div class="info" style="margin-top:4px;">
  crop box, when enabled, applies first — story-crop / story-blur then
  process the cropped region. stream-copy modes ignore crop (they can't re-encode).
</div>

<div class="row" style="margin-top: 14px;">
  <label>Filename:</label>
  <input type="text" id="fname" placeholder="my_clip" style="flex:1; max-width:400px;" />
</div>

<div class="row">
  <button class="big" onclick="doExport()">Export &amp; Download</button>
  <span id="status" class="info"></span>
</div>

<div id="log"></div>

<script>
// ---------- refs ----------
const v          = document.getElementById('v');
const timeline   = document.getElementById('timeline');
const tlSel      = document.getElementById('tlSel');
const tlPlay     = document.getElementById('tlPlay');
const tlIn       = document.getElementById('tlIn');
const tlOut      = document.getElementById('tlOut');
const cropOverlay= document.getElementById('cropOverlay');
const cropBoxEl  = document.getElementById('cropBox');
const cropInfo   = document.getElementById('cropInfo');
const log        = document.getElementById('log');
const status_    = document.getElementById('status');
const videoWrap  = document.getElementById('videoWrap');
const loopBtn    = document.getElementById('loopBtn');

// ---------- helpers ----------
function fmtTime(t) {
  if (!isFinite(t) || t < 0) return '00:00';
  const h = Math.floor(t / 3600);
  const m = Math.floor((t % 3600) / 60);
  const s = (t % 60).toFixed(1).padStart(4, '0');
  return h > 0 ? `${h}:${String(m).padStart(2,'0')}:${s}`
               : `${String(m).padStart(2,'0')}:${s}`;
}
function parseTime(s) {
  s = String(s).trim();
  if (s.includes(':')) {
    const p = s.split(':');
    if (p.length === 2) return parseInt(p[0]) * 60 + parseFloat(p[1]);
    if (p.length === 3) return parseInt(p[0]) * 3600 + parseInt(p[1]) * 60 + parseFloat(p[2]);
  }
  return parseFloat(s);
}
function basename(p) { return p.split(/[\\/]/).pop(); }
function stem(p) { return basename(p).replace(/\.[^.]+$/, ''); }
function clamp(x, lo, hi) { return Math.max(lo, Math.min(hi, x)); }

// ---------- state ----------
let inSec = 0, outSec = 10;
let looping = false;
let cropBox = null;  // {x, y, w, h} in SOURCE video-pixel coords
let cropAspect = 0;  // 0 = free, >0 = width/height ratio
let onsets = [];     // [{t, score}, ...] vocal-entry candidates
let analysis = null; // {vocal_start, audio_start, audio_end, chorus_candidates}
let chorusIdx = -1;  // which chorus candidate is selected
const tlMarkers = document.getElementById('tlMarkers');
const tlChorus  = document.getElementById('tlChorus');
const scanBtn   = document.getElementById('scanBtn');
const chorusBtn = document.getElementById('chorusBtn');
const onsetInfo = document.getElementById('onsetInfo');

function chorusSel() {
  if (!analysis || chorusIdx < 0) return null;
  return analysis.chorus_candidates[chorusIdx] || null;
}

// ---------- load ----------
function loadVideo() {
  const path = document.getElementById('path').value.trim();
  if (!path) return;
  v.src = '/video?path=' + encodeURIComponent(path);
  clearOnsets();  // stale markers from previous file
  v.addEventListener('loadedmetadata', () => {
    inSec = 0;
    outSec = Math.min(10, v.duration);
    if (!document.getElementById('fname').value.trim()) {
      document.getElementById('fname').value = stem(path) + '_clip';
    }
    document.getElementById('inTime').value  = fmtTime(inSec);
    document.getElementById('outTime').value = fmtTime(outSec);
    updateTimeline();
    updateCropDisplay();
  }, { once: true });
  v.addEventListener('error', () => {
    status_.innerHTML = '<span class="status-err">failed to load — check path exists</span>';
  }, { once: true });
}

// ---------- vocal-onset scan ----------
async function scanOnsets() {
  const path = document.getElementById('path').value.trim();
  if (!path) { alert('load a video first'); return; }
  scanBtn.disabled = true;
  scanBtn.textContent = '🎤 scanning…';
  onsetInfo.textContent = 'extracting audio & computing band-RMS…';
  try {
    const resp = await fetch('/scan?path=' + encodeURIComponent(path));
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || 'scan failed');
    onsets = data.onsets || [];
    analysis = data;
    chorusIdx = (data.chorus_index === null || data.chorus_index === undefined)
      ? -1 : data.chorus_index;
    renderMarkers();
    renderChorus();
    refreshPresets();
    describeScan();
  } catch (e) {
    onsetInfo.innerHTML = '<span class="status-err">' + e.message + '</span>';
  } finally {
    scanBtn.disabled = false;
    scanBtn.textContent = '🎤 scan vocal onsets';
  }
}

function clearOnsets() {
  onsets = [];
  analysis = null;
  chorusIdx = -1;
  tlMarkers.innerHTML = '';
  tlChorus.classList.remove('active');
  refreshPresets();
  onsetInfo.textContent = 'no scan yet';
}

function describeScan() {
  const bits = [`${onsets.length} onsets`];
  if (analysis && analysis.vocal_start !== null) {
    bits.push(`1st line ${fmtTime(analysis.vocal_start)}`);
  }
  const c = chorusSel();
  bits.push(c ? `chorus ${fmtTime(c.start)}–${fmtTime(c.end)}`
              : 'chorus not found — set it by hand');
  onsetInfo.textContent = bits.join(' · ');
  const n = analysis ? (analysis.chorus_candidates || []).length : 0;
  chorusBtn.disabled = n < 2;
  chorusBtn.textContent = `⟳ chorus ${n ? chorusIdx + 1 : 0}/${n}`;
}

function cycleChorus() {
  const n = analysis ? (analysis.chorus_candidates || []).length : 0;
  if (n < 2) return;
  chorusIdx = (chorusIdx + 1) % n;
  renderChorus();
  refreshPresets();
  describeScan();
  const c = chorusSel();
  if (c) v.currentTime = c.start;
}

function renderChorus() {
  const dur = v.duration || 0;
  const c = chorusSel();
  if (!c || !dur) { tlChorus.classList.remove('active'); return; }
  tlChorus.style.left  = ((c.start / dur) * 100) + '%';
  tlChorus.style.width = (((c.end - c.start) / dur) * 100) + '%';
  tlChorus.classList.add('active');
}

// ---------- presets ----------
function refreshPresets() {
  const haveLine = !!(analysis && analysis.vocal_start !== null);
  const haveChor = !!chorusSel();
  const haveSpan = !!(analysis && analysis.audio_end !== null);
  document.getElementById('pLineEnd').disabled  = !haveLine;
  document.getElementById('pLineChor').disabled = !(haveLine && haveChor);
  document.getElementById('pChorus').disabled   = !haveChor;
  document.getElementById('pLine60').disabled   = !haveLine;
  document.getElementById('pLine90').disabled   = !haveLine;
  document.getElementById('pTrim').disabled     = !haveSpan;
}

// Start a hair before the detected entry so the first syllable isn't clipped.
const LEAD_IN = 0.4;

function preset(kind) {
  if (!analysis || !v.duration) return;
  const line = analysis.vocal_start;
  const c = chorusSel();
  let i, o;
  if (kind === 'line1-end')         { i = line - LEAD_IN; o = analysis.audio_end; }
  else if (kind === 'line1-chorus') { i = line - LEAD_IN; o = c.end; }
  else if (kind === 'chorus')       { i = c.start - LEAD_IN; o = c.end; }
  else if (kind === 'line1-60')     { i = line - LEAD_IN; o = line - LEAD_IN + 60; }
  else if (kind === 'line1-90')     { i = line - LEAD_IN; o = line - LEAD_IN + 90; }
  else if (kind === 'trim')         { i = analysis.audio_start; o = analysis.audio_end; }
  else return;
  inSec  = clamp(i, 0, v.duration - 0.05);
  outSec = clamp(o, inSec + 0.05, v.duration);
  document.getElementById('inTime').value  = fmtTime(inSec);
  document.getElementById('outTime').value = fmtTime(outSec);
  v.currentTime = inSec;
  updateTimeline();
  status_.innerHTML = '<span class="status-ok">preset: ' + kind + ' → ' +
    fmtTime(inSec) + ' – ' + fmtTime(outSec) +
    ' (' + fmtTime(outSec - inSec) + ')</span>';
}

function renderMarkers() {
  tlMarkers.innerHTML = '';
  const dur = v.duration || 0;
  if (!dur) return;
  for (const o of onsets) {
    const m = document.createElement('div');
    m.className = 'tl-marker';
    m.style.left = ((o.t / dur) * 100) + '%';
    m.title = `${fmtTime(o.t)}  (score ${o.score})`;
    m.addEventListener('click', (e) => {
      e.stopPropagation();
      v.currentTime = o.t;
      highlightMarker(o.t);
    });
    tlMarkers.appendChild(m);
  }
}

function highlightMarker(t) {
  const dur = v.duration || 1;
  const x = (t / dur) * 100;
  for (const el of tlMarkers.children) {
    const left = parseFloat(el.style.left);
    el.classList.toggle('hot', Math.abs(left - x) < 0.1);
  }
}

function jumpNextOnset() {
  if (!onsets.length) return;
  const t = v.currentTime + 0.05;
  const next = onsets.find(o => o.t > t);
  if (next) { v.currentTime = next.t; highlightMarker(next.t); }
}
function jumpPrevOnset() {
  if (!onsets.length) return;
  const t = v.currentTime - 0.05;
  let prev = null;
  for (const o of onsets) { if (o.t < t) prev = o; else break; }
  if (prev) { v.currentTime = prev.t; highlightMarker(prev.t); }
}

// ---------- timeline (custom) ----------
function updateTimeline() {
  const dur = v.duration || 0;
  document.getElementById('inLbl').textContent  = fmtTime(inSec);
  document.getElementById('outLbl').textContent = fmtTime(outSec);
  document.getElementById('lenLbl').textContent = fmtTime(outSec - inSec);
  document.getElementById('curLbl').textContent = fmtTime(v.currentTime);
  document.getElementById('durLbl').textContent = fmtTime(dur);
  if (!dur) return;
  const iP = (inSec / dur) * 100;
  const oP = (outSec / dur) * 100;
  const cP = (v.currentTime / dur) * 100;
  tlSel.style.left  = iP + '%';
  tlSel.style.width = (oP - iP) + '%';
  tlIn.style.left   = iP + '%';
  tlOut.style.left  = oP + '%';
  tlPlay.style.left = cP + '%';
}

let tlDrag = null; // 'in' | 'out' | 'seek'
function tlEventToSec(e) {
  const rect = timeline.getBoundingClientRect();
  const x = clamp((e.clientX - rect.left) / rect.width, 0, 1);
  return x * (v.duration || 0);
}
timeline.addEventListener('mousedown', (e) => {
  if (!v.duration) return;
  if (e.target === tlIn)  { tlDrag = 'in';  return; }
  if (e.target === tlOut) { tlDrag = 'out'; return; }
  const t = tlEventToSec(e);
  v.currentTime = t;
  tlDrag = 'seek';
});
document.addEventListener('mousemove', (e) => {
  if (!tlDrag) return;
  const t = tlEventToSec(e);
  if (tlDrag === 'in') {
    inSec = clamp(t, 0, outSec - 0.05);
    v.currentTime = inSec;
    document.getElementById('inTime').value = fmtTime(inSec);
  } else if (tlDrag === 'out') {
    outSec = clamp(t, inSec + 0.05, v.duration);
    v.currentTime = outSec;
    document.getElementById('outTime').value = fmtTime(outSec);
  } else if (tlDrag === 'seek') {
    v.currentTime = t;
  }
  updateTimeline();
});
document.addEventListener('mouseup', () => { tlDrag = null; });

// ---------- in/out actions ----------
function setIn()  {
  inSec = Math.min(v.currentTime, outSec - 0.05);
  document.getElementById('inTime').value = fmtTime(inSec);
  updateTimeline();
}
function setOut() {
  outSec = Math.max(v.currentTime, inSec + 0.05);
  document.getElementById('outTime').value = fmtTime(outSec);
  updateTimeline();
}
function jumpIn()  { v.currentTime = inSec; }
function jumpOut() { v.currentTime = outSec; }
function applyTimeInputs() {
  const i = parseTime(document.getElementById('inTime').value);
  const o = parseTime(document.getElementById('outTime').value);
  if (isFinite(i) && isFinite(o) && o > i) {
    inSec = clamp(i, 0, v.duration);
    outSec = clamp(o, inSec + 0.05, v.duration);
    updateTimeline();
  }
}

function playSel() {
  v.currentTime = inSec;
  v.play();
}
function toggleLoop() {
  looping = !looping;
  loopBtn.textContent = looping ? '🔁 loop on' : '🔁 loop off';
}
v.addEventListener('timeupdate', () => {
  updateTimeline();
  if (looping && v.currentTime >= outSec - 0.02) v.currentTime = inSec;
});

// ---------- crop overlay ----------
function toggleCrop() {
  const on = document.getElementById('cropEnabled').checked;
  cropOverlay.classList.toggle('active', on);
  if (on && (!cropBox || cropBox.w === 0)) {
    cropCenter50();
  } else {
    updateCropDisplay();
  }
}
function cropReset() {
  if (!v.videoWidth) return;
  cropBox = { x: 0, y: 0, w: v.videoWidth, h: v.videoHeight };
  updateCropDisplay();
}
function cropCenter50() {
  if (!v.videoWidth) return;
  const w = Math.round(v.videoWidth / 2);
  const h = cropAspect > 0 ? Math.round(w / cropAspect) : Math.round(v.videoHeight / 2);
  cropBox = {
    x: Math.round((v.videoWidth - w) / 2),
    y: Math.round((v.videoHeight - h) / 2),
    w: w, h: Math.min(h, v.videoHeight)
  };
  updateCropDisplay();
}
function applyAspect() {
  cropAspect = parseFloat(document.getElementById('cropAspect').value);
  if (!cropBox || cropAspect === 0) { updateCropDisplay(); return; }
  // Fit current center, adjust height to new aspect
  const cx = cropBox.x + cropBox.w / 2;
  const cy = cropBox.y + cropBox.h / 2;
  let w = cropBox.w;
  let h = w / cropAspect;
  if (h > v.videoHeight) { h = v.videoHeight; w = h * cropAspect; }
  cropBox.w = Math.round(w);
  cropBox.h = Math.round(h);
  cropBox.x = Math.round(clamp(cx - w/2, 0, v.videoWidth - w));
  cropBox.y = Math.round(clamp(cy - h/2, 0, v.videoHeight - h));
  updateCropDisplay();
}

function videoDisplayRect() {
  // Compute actual rendered rect of the video content within the wrap
  const wrapRect = videoWrap.getBoundingClientRect();
  if (!v.videoWidth) return { w: wrapRect.width, h: wrapRect.height, ox: 0, oy: 0 };
  // Video preserves aspect, letterboxed within wrap
  const vAsp = v.videoWidth / v.videoHeight;
  const wAsp = wrapRect.width / wrapRect.height;
  let dispW, dispH, ox, oy;
  if (wAsp > vAsp) {
    dispH = wrapRect.height;
    dispW = dispH * vAsp;
    ox = (wrapRect.width - dispW) / 2;
    oy = 0;
  } else {
    dispW = wrapRect.width;
    dispH = dispW / vAsp;
    ox = 0;
    oy = (wrapRect.height - dispH) / 2;
  }
  return { w: dispW, h: dispH, ox: ox, oy: oy };
}

function updateCropDisplay() {
  if (!cropBox || !v.videoWidth) {
    cropInfo.textContent = document.getElementById('cropEnabled').checked
      ? '(load a video first)' : 'crop off';
    return;
  }
  const disp = videoDisplayRect();
  const scaleX = disp.w / v.videoWidth;
  const scaleY = disp.h / v.videoHeight;
  cropBoxEl.style.left   = (disp.ox + cropBox.x * scaleX) + 'px';
  cropBoxEl.style.top    = (disp.oy + cropBox.y * scaleY) + 'px';
  cropBoxEl.style.width  = (cropBox.w * scaleX) + 'px';
  cropBoxEl.style.height = (cropBox.h * scaleY) + 'px';
  cropInfo.textContent =
    `${cropBox.w}×${cropBox.h} at (${cropBox.x},${cropBox.y})  ` +
    `[source ${v.videoWidth}×${v.videoHeight}]`;
}

window.addEventListener('resize', updateCropDisplay);
v.addEventListener('loadedmetadata', () => setTimeout(updateCropDisplay, 50));

// ---------- crop drag/resize ----------
let cropDrag = null; // { mode: 'move'|'nw'|... , startX, startY, box0 }
cropBoxEl.addEventListener('mousedown', (e) => {
  if (!cropBox) return;
  const handleAttr = e.target.dataset.h;
  cropDrag = {
    mode: handleAttr || 'move',
    startX: e.clientX, startY: e.clientY,
    box0: { ...cropBox }
  };
  e.preventDefault(); e.stopPropagation();
});
document.addEventListener('mousemove', (e) => {
  if (!cropDrag || !v.videoWidth) return;
  const disp = videoDisplayRect();
  const scaleX = disp.w / v.videoWidth;
  const scaleY = disp.h / v.videoHeight;
  const dx = (e.clientX - cropDrag.startX) / scaleX;
  const dy = (e.clientY - cropDrag.startY) / scaleY;
  const b0 = cropDrag.box0;
  let nb = { ...b0 };
  const m = cropDrag.mode;
  if (m === 'move') {
    nb.x = clamp(b0.x + dx, 0, v.videoWidth - b0.w);
    nb.y = clamp(b0.y + dy, 0, v.videoHeight - b0.h);
  } else {
    // resize; respect aspect if set
    if (m.includes('e')) nb.w = b0.w + dx;
    if (m.includes('w')) { nb.w = b0.w - dx; nb.x = b0.x + dx; }
    if (m.includes('s')) nb.h = b0.h + dy;
    if (m.includes('n')) { nb.h = b0.h - dy; nb.y = b0.y + dy; }
    if (cropAspect > 0) {
      // enforce aspect: derive one from the other, keep opposite anchor
      if (m === 'e' || m === 'w') {
        const h = nb.w / cropAspect;
        const dy2 = (h - nb.h) / 2;
        nb.y -= dy2; nb.h = h;
      } else if (m === 'n' || m === 's') {
        const w = nb.h * cropAspect;
        const dx2 = (w - nb.w) / 2;
        nb.x -= dx2; nb.w = w;
      } else {
        // corners: use the larger delta so aspect is preserved
        const wFromH = nb.h * cropAspect;
        const hFromW = nb.w / cropAspect;
        if (wFromH > nb.w) nb.w = wFromH; else nb.h = hFromW;
        if (m.includes('n')) nb.y = b0.y + b0.h - nb.h;
        if (m.includes('w')) nb.x = b0.x + b0.w - nb.w;
      }
    }
    // clamp into video bounds
    if (nb.x < 0) { nb.w += nb.x; nb.x = 0; }
    if (nb.y < 0) { nb.h += nb.y; nb.y = 0; }
    if (nb.x + nb.w > v.videoWidth)  nb.w = v.videoWidth  - nb.x;
    if (nb.y + nb.h > v.videoHeight) nb.h = v.videoHeight - nb.y;
    if (nb.w < 16) nb.w = 16;
    if (nb.h < 16) nb.h = 16;
  }
  cropBox = {
    x: Math.round(nb.x), y: Math.round(nb.y),
    w: Math.round(nb.w), h: Math.round(nb.h)
  };
  updateCropDisplay();
});
document.addEventListener('mouseup', () => { cropDrag = null; });

// ---------- keyboard ----------
document.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
  if (e.key === '[') { setIn(); e.preventDefault(); }
  else if (e.key === ']') { setOut(); e.preventDefault(); }
  else if (e.key === ',') { v.currentTime -= 1/30; e.preventDefault(); }
  else if (e.key === '.') { v.currentTime += 1/30; e.preventDefault(); }
  else if (e.key === 'ArrowLeft')  { v.currentTime -= 5; e.preventDefault(); }
  else if (e.key === 'ArrowRight') { v.currentTime += 5; e.preventDefault(); }
  else if (e.key === 'k' || e.key === ' ') {
    e.preventDefault();
    v.paused ? v.play() : v.pause();
  }
  else if (e.key === 'l') { toggleLoop(); e.preventDefault(); }
  else if (e.key === 'n') { jumpNextOnset(); e.preventDefault(); }
  else if (e.key === 'p') { jumpPrevOnset(); e.preventDefault(); }
});

// ---------- export ----------
async function doExport() {
  const path  = document.getElementById('path').value.trim();
  const mode  = document.querySelector('input[name=mode]:checked').value;
  const fname = document.getElementById('fname').value.trim() || 'clip';
  const atS   = v.currentTime;
  if (!path) { alert('need a source path'); return; }
  if (mode !== 'frame' && !(outSec > inSec)) { alert('out must be > in'); return; }

  const cropEnabled = document.getElementById('cropEnabled').checked;
  const cropPayload = (cropEnabled && cropBox) ? cropBox : null;

  status_.textContent = 'processing…';
  status_.className = 'info';
  log.textContent = '';

  try {
    const resp = await fetch('/clip', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        path, mode, fname,
        in: inSec, out: outSec, at: atS,
        crop: cropPayload
      })
    });
    const data = await resp.json();
    log.textContent = data.log || '';
    if (!resp.ok) {
      status_.innerHTML = '<span class="status-err">failed: ' + (data.error || 'unknown') + '</span>';
      return;
    }
    const names = data.files.map(basename).join(', ');
    status_.innerHTML = '<span class="status-ok">✓ saved: ' + names + '</span>';
    for (const f of data.files) {
      const a = document.createElement('a');
      a.href = '/download?path=' + encodeURIComponent(f);
      a.download = basename(f);
      document.body.appendChild(a);
      a.click(); a.remove();
      await new Promise(r => setTimeout(r, 300));
    }
  } catch (e) {
    status_.innerHTML = '<span class="status-err">error: ' + e + '</span>';
    log.textContent = String(e);
  }
}
</script>
</body>
</html>
"""


def _run_ff(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.returncode, r.stderr or ""


FRAME_HOP_S = 0.05  # analysis frame step, seconds


SR = 16000
CHROMA_HOP_S = 0.5


def _decode(src: Path):
    """Decode src to a 16 kHz mono float32 array, or None if ffmpeg gave nothing."""
    import numpy as np

    cmd = [
        "ffmpeg", "-nostdin", "-loglevel", "quiet",
        "-i", str(src), "-vn",
        "-ac", "1", "-ar", str(SR),
        "-f", "s16le", "pipe:1",
    ]
    proc = subprocess.run(cmd, capture_output=True, check=False)
    if not proc.stdout:
        return None
    x = np.frombuffer(proc.stdout, dtype="<i2").astype(np.float32) / 32768.0
    return x if len(x) >= SR * 2 else None


def _frames_of(x, win, hop):
    import numpy as np
    n = (len(x) - win) // hop + 1
    return np.lib.stride_tricks.as_strided(
        x, shape=(n, win), strides=(x.strides[0] * hop, x.strides[0]))


def _envelopes(x):
    """Return (vocal_band, full_band) energy envelopes at FRAME_HOP_S.

    Vocal band is 200-3400 Hz (voice formant range), which tracks singing while
    ignoring bass and cymbals; full band tracks overall loudness.
    """
    import numpy as np

    win = 512
    frames = _frames_of(x, win, int(SR * FRAME_HOP_S))
    n_frames = frames.shape[0]
    window = np.hanning(win).astype(np.float32)
    freqs = np.fft.rfftfreq(win, 1.0 / SR)
    band = (freqs >= 200) & (freqs <= 3400)
    # Chunked so a 20-minute setlist video doesn't allocate a huge spectrogram
    voc = np.empty(n_frames, dtype=np.float32)
    full = np.empty(n_frames, dtype=np.float32)
    for a in range(0, n_frames, 2048):
        b = min(a + 2048, n_frames)
        mag = np.abs(np.fft.rfft(frames[a:b] * window, axis=1))
        voc[a:b] = mag[:, band].sum(axis=1)
        full[a:b] = mag.sum(axis=1)
    return voc, full


def _chroma(x):
    """Return L2-normalised 12-bin chroma frames at CHROMA_HOP_S.

    Folding the spectrum onto pitch classes makes two passes of the same
    chord progression look alike even when the arrangement differs, which is
    what lets the self-similarity step find repeated sections.
    """
    import numpy as np

    win = 4096
    frames = _frames_of(x, win, int(SR * CHROMA_HOP_S))
    n = frames.shape[0]
    window = np.hanning(win).astype(np.float32)
    freqs = np.fft.rfftfreq(win, 1.0 / SR)
    keep = (freqs > 80) & (freqs < 5000)
    midi = 69 + 12 * np.log2(np.maximum(freqs, 1e-6) / 440.0)
    pc = np.round(midi).astype(int) % 12
    masks = [keep & (pc == k) for k in range(12)]
    C = np.zeros((n, 12), dtype=np.float32)
    for a in range(0, n, 512):
        b = min(a + 512, n)
        mag = np.abs(np.fft.rfft(frames[a:b] * window, axis=1))
        for k in range(12):
            C[a:b, k] = mag[:, masks[k]].sum(axis=1)
    C = np.log1p(C)
    C -= C.mean(axis=1, keepdims=True)
    return C / np.maximum(np.linalg.norm(C, axis=1, keepdims=True), 1e-6)


def _repeated_sections(x, full_env, first_vocal_i, end_i):
    """Find sustained repeated sections — the chorus candidate pool.

    Builds a chroma self-similarity matrix, scores each lag by its strongest
    sustained diagonal stripe (a stripe means "this span recurs `lag` later"),
    and keeps spans that start once singing has begun. Overlapping spans
    collapse to the louder one and near-adjacent spans merge, so a chorus split
    across two stripes comes back as one section.

    Returns [{"start_i", "end_i", "lag", "energy"}] in time order, at most 5.
    Caller decides which one is "the" chorus; repetition alone cannot tell a
    chorus from a verse, since both recur.
    """
    import numpy as np

    C = _chroma(x)
    n = C.shape[0]
    if n < int(40 / CHROMA_HOP_S):
        return []
    S = C @ C.T
    ratio = CHROMA_HOP_S / FRAME_HOP_S  # chroma frame -> envelope frame

    lo = int(15.0 / CHROMA_HOP_S)
    hi = min(int(150.0 / CHROMA_HOP_S), n - int(10 / CHROMA_HOP_S))
    stripe_k = int(6 / CHROMA_HOP_S)
    lag_scores = []
    for L in range(lo, hi):
        d = np.diagonal(S, offset=L)
        if d.size < stripe_k:
            continue
        sm = np.convolve(d, np.ones(stripe_k) / stripe_k, mode="valid")
        lag_scores.append((float(sm.max()), L))
    if not lag_scores:
        return []
    lag_scores.sort(reverse=True)

    min_len = int(10.0 / CHROMA_HOP_S)
    max_len = int(60.0 / CHROMA_HOP_S)
    first_c = max(0, int(first_vocal_i / ratio) - int(2 / CHROMA_HOP_S))
    env_max = float(full_env.max()) or 1.0
    spans = []
    for _score, L in lag_scores[:6]:
        d = np.diagonal(S, offset=L)
        k = int(4 / CHROMA_HOP_S)
        sm = np.convolve(d, np.ones(k) / k, mode="same")
        for a, b in _runs_above(sm >= float(np.percentile(sm, 80)), min_len):
            if a < first_c:
                continue
            b = min(b, a + max_len)
            ea, eb = int(a * ratio), min(int(b * ratio), end_i)
            if eb - ea < int(10 / FRAME_HOP_S):
                continue
            energy = float(full_env[ea:eb].mean()) / env_max
            spans.append({"start_i": ea, "end_i": eb, "lag": round(L * CHROMA_HOP_S, 1),
                          "energy": energy})
    if not spans:
        return []
    # Collapse overlaps, keeping the louder span of each cluster
    spans.sort(key=lambda s: -s["energy"])
    picked = []
    for s in spans:
        if any(s["start_i"] < p["end_i"] and p["start_i"] < s["end_i"] for p in picked):
            continue
        picked.append(s)
    picked.sort(key=lambda s: s["start_i"])
    # Merge near-adjacent spans — one chorus often shows up as two stripes
    gap = int(3.0 / FRAME_HOP_S)
    merged = [picked[0]]
    for s in picked[1:]:
        prev = merged[-1]
        if s["start_i"] - prev["end_i"] <= gap:
            span_a = prev["end_i"] - prev["start_i"]
            span_b = s["end_i"] - s["start_i"]
            prev["energy"] = ((prev["energy"] * span_a + s["energy"] * span_b)
                              / max(1, span_a + span_b))
            prev["end_i"] = s["end_i"]
        else:
            merged.append(s)
    return merged[:5]


def _smooth(a, seconds):
    import numpy as np
    k = max(1, int(seconds / FRAME_HOP_S))
    return np.convolve(a, np.ones(k, dtype=np.float32) / k, mode="same")


def _runs_above(mask, min_frames):
    """Yield (start, end) index pairs for contiguous True runs of mask."""
    import numpy as np
    edges = np.diff(mask.astype(np.int8))
    starts = list(np.flatnonzero(edges == 1) + 1)
    ends = list(np.flatnonzero(edges == -1) + 1)
    if mask.size and mask[0]:
        starts.insert(0, 0)
    if mask.size and mask[-1]:
        ends.append(mask.size)
    return [(s, e) for s, e in zip(starts, ends) if e - s >= min_frames]


def analyze_audio(src: Path, max_onsets: int = 30):
    """Locate vocal phrase entries, the first chorus, and the audible span.

    Returns a dict with:
      onsets       [{"t", "score"}]  vocal phrase entry candidates
      vocal_start  seconds, first sung line (None if nothing detected)
      audio_start  seconds, first audible audio (leading silence trimmed)
      audio_end    seconds, last audible audio (trailing silence trimmed)
      chorus       {"start", "end"} of the first sustained loud section, or None
    """
    import numpy as np

    x = _decode(src)
    if x is None:
        return {"onsets": [], "vocal_start": None, "audio_start": None,
                "audio_end": None, "chorus": None, "chorus_candidates": [],
                "chorus_index": None}
    return analyze_samples(x, max_onsets=max_onsets)


def analyze_samples(x, max_onsets: int = 30):
    """Same as analyze_audio but on already-decoded 16 kHz mono samples."""
    import numpy as np

    voc_raw, full_raw = _envelopes(x)
    voc = _smooth(voc_raw, 0.5)
    full = _smooth(full_raw, 0.5)
    n = len(voc)
    voc_n = voc / (float(voc.max()) or 1.0)
    full_n = full / (float(full.max()) or 1.0)

    def t_of(i):
        return round(i * FRAME_HOP_S, 2)

    # --- audible span: trim leading/trailing silence ---
    audible = np.flatnonzero(full_n > 0.02)
    audio_start = t_of(int(audible[0])) if audible.size else 0.0
    audio_end = t_of(int(audible[-1])) if audible.size else t_of(n - 1)

    # --- vocal phrase entries: rising edges out of a quiet baseline ---
    lookback = int(1.0 / FRAME_HOP_S)
    min_gap = int(3.0 / FRAME_HOP_S)
    cand = []
    last_idx = -min_gap
    for i in range(lookback + 4, n - 6):
        if i - last_idx < min_gap:
            continue
        past = voc_n[i - lookback:i - 4]
        fut = voc_n[i:i + 6]
        if past.size == 0 or fut.size == 0:
            continue
        if past.max() < 0.18 and fut.mean() > 0.30:
            cand.append((i, float(fut.mean() - past.max())))
            last_idx = i
    cand.sort(key=lambda c: -c[1])
    cand = cand[:max_onsets]
    cand.sort(key=lambda c: c[0])
    onsets = [{"t": t_of(i), "score": round(min(1.0, s), 3)} for i, s in cand]
    vocal_start = onsets[0]["t"] if onsets else None

    # --- chorus candidates: loudest repeated sections once singing has begun ---
    first_i = cand[0][0] if cand else int(audio_start / FRAME_HOP_S)
    end_i = int(audio_end / FRAME_HOP_S)
    cands = _repeated_sections(x, full, first_i, end_i)
    snap_win = int(6 / FRAME_HOP_S)
    chorus_candidates = []
    for s in cands:
        s_i = s["start_i"]
        # snap the start back to a real phrase entry when one is close
        near = [i for i, _ in cand if s_i - snap_win <= i <= s_i]
        if near:
            s_i = near[-1]
        chorus_candidates.append({
            "start": t_of(s_i), "end": t_of(s["end_i"]),
            "lag": s["lag"], "energy": round(s["energy"], 3),
        })

    # Default pick = the FIRST chorus, not the loudest one (that is usually the
    # final chorus). Drop quiet sections, and skip the span that opens right on
    # the first sung line, since what a song opens with is essentially always a
    # verse. Everything stays in the list so the UI can offer alternates.
    chorus_index = None
    if chorus_candidates:
        bar = 0.55 * max(c["energy"] for c in chorus_candidates)
        loud = [i for i, c in enumerate(chorus_candidates) if c["energy"] >= bar]
        after_opening = [i for i in loud
                         if vocal_start is None
                         or chorus_candidates[i]["start"] - vocal_start >= 8.0]
        chorus_index = (after_opening or loud or [0])[0]

    return {"onsets": onsets, "vocal_start": vocal_start,
            "audio_start": audio_start, "audio_end": audio_end,
            "chorus": chorus_candidates[chorus_index] if chorus_index is not None else None,
            "chorus_candidates": chorus_candidates,
            "chorus_index": chorus_index}


@app.route("/")
def index():
    return HTML


@app.route("/video")
def video():
    path = unquote(request.args.get("path", ""))
    p = Path(path)
    if not p.exists() or not p.is_file():
        return "not found", 404
    file_size = p.stat().st_size
    range_header = request.headers.get("Range")
    if not range_header:
        return send_file(str(p), mimetype="video/mp4", conditional=True)
    m = re.match(r"bytes=(\d+)-(\d*)", range_header)
    if not m:
        return "invalid range", 416
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else file_size - 1
    end = min(end, file_size - 1)
    length = end - start + 1

    def generate():
        with open(p, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    resp = Response(generate(), 206, mimetype="video/mp4")
    resp.headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    resp.headers["Accept-Ranges"] = "bytes"
    resp.headers["Content-Length"] = str(length)
    return resp


@app.route("/scan")
def scan():
    path = unquote(request.args.get("path", ""))
    p = Path(path)
    if not p.exists() or not p.is_file():
        return jsonify({"error": "not found"}), 404
    try:
        return jsonify(analyze_audio(p))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/download")
def download():
    path = unquote(request.args.get("path", ""))
    p = Path(path)
    if not p.exists():
        return "not found", 404
    return send_file(str(p), as_attachment=True, download_name=p.name)


@app.route("/clip", methods=["POST"])
def clip():
    data = request.get_json(silent=True) or {}
    src = Path(data.get("path", ""))
    if not src.exists():
        return jsonify({"error": f"Source not found: {src}"}), 404

    mode = data.get("mode", "audio")
    fname = data.get("fname", "clip")
    in_s = float(data.get("in") or 0.0)
    out_s = float(data.get("out") or 0.0)
    at_s = float(data.get("at") or in_s)
    crop = data.get("crop")  # {x, y, w, h} in source pixels, or None

    def crop_filter():
        if not crop:
            return None
        # ffmpeg crop=W:H:X:Y — even values are safer for libx264
        w = max(2, int(crop["w"]) & ~1)
        h = max(2, int(crop["h"]) & ~1)
        x = max(0, int(crop["x"]))
        y = max(0, int(crop["y"]))
        return f"crop={w}:{h}:{x}:{y}"

    outdir = src.parent
    files = []
    log_lines = []

    def _run(cmd, tail=1500):
        log_lines.append("$ " + " ".join(cmd))
        rc, err = _run_ff(cmd)
        log_lines.append(err[-tail:] if err else "")
        if rc != 0:
            raise RuntimeError(f"ffmpeg exit {rc}")

    try:
        if mode == "audio":
            # crop is irrelevant for audio
            out = outdir / f"{fname}.m4a"
            _run(["ffmpeg", "-y", "-ss", fmt_time(in_s), "-to", fmt_time(out_s),
                  "-i", str(src), "-vn", "-c:a", "copy", str(out)])
            files.append(str(out))

        elif mode == "video":
            out = outdir / f"{fname}.mp4"
            if crop:
                # stream-copy can't apply filters; force re-encode when cropping
                cf = crop_filter()
                _run(["ffmpeg", "-y", "-i", str(src),
                      "-ss", fmt_time(in_s), "-to", fmt_time(out_s),
                      "-vf", cf,
                      "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                      "-c:a", "aac", "-b:a", "192k",
                      "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)])
            else:
                _run(["ffmpeg", "-y", "-ss", fmt_time(in_s), "-to", fmt_time(out_s),
                      "-i", str(src), "-c", "copy",
                      "-movflags", "+faststart", str(out)])
            files.append(str(out))

        elif mode == "video-re":
            out = outdir / f"{fname}.mp4"
            cmd = ["ffmpeg", "-y", "-i", str(src),
                   "-ss", fmt_time(in_s), "-to", fmt_time(out_s)]
            if crop:
                cmd += ["-vf", crop_filter()]
            cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "20",
                    "-c:a", "aac", "-b:a", "192k",
                    "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]
            _run(cmd)
            files.append(str(out))

        elif mode == "frame":
            out = outdir / f"{fname}.jpg"
            cmd = ["ffmpeg", "-y", "-ss", fmt_time(at_s), "-i", str(src),
                   "-frames:v", "1", "-q:v", "2"]
            if crop:
                cmd += ["-vf", crop_filter()]
            cmd += [str(out)]
            _run(cmd)
            files.append(str(out))

        elif mode in ("story-crop", "story-blur"):
            dur = out_s - in_s
            SEG = 60.0
            n_parts = max(1, int((dur + SEG - 0.001) // SEG))
            for i in range(n_parts):
                seg_start = in_s + i * SEG
                seg_end = min(in_s + (i + 1) * SEG, out_s)
                seg_out = outdir / (f"{fname}.mp4" if n_parts == 1
                                    else f"{fname}_part{i+1}.mp4")
                # If user gave a crop box, apply that first;
                # otherwise fall back to auto 9:16 center crop.
                if mode == "story-crop":
                    if crop:
                        filt = f"{crop_filter()},scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black"
                    else:
                        filt = "crop=ih*9/16:ih,scale=1080:1920"
                    filt_args = ["-vf", filt]
                else:  # story-blur
                    if crop:
                        pre = crop_filter() + ","
                    else:
                        pre = ""
                    filt = (
                        f"[0:v]{pre}scale=1080:1920:force_original_aspect_ratio=increase,"
                        "crop=1080:1920,boxblur=luma_radius=50:luma_power=1[bg];"
                        f"[0:v]{pre}scale=1080:-2[fg];"
                        "[bg][fg]overlay=(W-w)/2:(H-h)/2"
                    )
                    filt_args = ["-filter_complex", filt]
                _run([
                    "ffmpeg", "-y", "-i", str(src),
                    "-ss", fmt_time(seg_start), "-to", fmt_time(seg_end),
                    *filt_args,
                    "-c:v", "libx264", "-preset", "medium", "-crf", "22",
                    "-c:a", "aac", "-b:a", "192k",
                    "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                    str(seg_out),
                ])
                files.append(str(seg_out))

        else:
            return jsonify({"error": f"unknown mode: {mode}"}), 400

        return jsonify({"files": files, "log": "\n".join(log_lines)})

    except Exception as e:
        return jsonify({"error": str(e), "log": "\n".join(log_lines)}), 500


def main():
    ap = argparse.ArgumentParser(description="Local clip-and-export web UI")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    url = f"http://127.0.0.1:{args.port}"
    print(f"ytclip web → {url}")
    print("Bound to 127.0.0.1 only. Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=args.port, debug=False)


if __name__ == "__main__":
    main()
