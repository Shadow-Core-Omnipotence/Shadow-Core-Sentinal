"""
dashboard.py — Shadow-Core Sentinel Mission Control Dashboard v1.2.0
Serves a developer-facing HTML dashboard and JSON API.
"""

import json
import logging
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable, List
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Shadow-Core Sentinel</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;700&family=Syne:wght@400;700;800&display=swap');

  :root {
    --bg:        #080c10;
    --surface:   #0d1117;
    --surface2:  #161b22;
    --border:    #21262d;
    --border2:   #30363d;
    --text:      #e6edf3;
    --muted:     #7d8590;
    --green:     #3fb950;
    --green-dim: #1a3a20;
    --yellow:    #d29922;
    --red:       #f85149;
    --red-dim:   #3a1a1a;
    --blue:      #58a6ff;
    --blue-dim:  #1a2a3a;
    --purple:    #bc8cff;
    --accent:    #00ff88;
    --accent-dim:#003320;
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    font-family: 'JetBrains Mono', monospace;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    overflow-x: hidden;
  }

  /* Scanline overlay */
  body::before {
    content: '';
    position: fixed;
    inset: 0;
    background: repeating-linear-gradient(
      0deg,
      transparent,
      transparent 2px,
      rgba(0,255,136,0.015) 2px,
      rgba(0,255,136,0.015) 4px
    );
    pointer-events: none;
    z-index: 1000;
  }

  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 1.25rem 2rem;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
    position: sticky;
    top: 0;
    z-index: 100;
  }

  .logo {
    display: flex;
    align-items: center;
    gap: 0.75rem;
  }

  .logo-icon {
    width: 32px;
    height: 32px;
    border: 2px solid var(--accent);
    border-radius: 6px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 16px;
    box-shadow: 0 0 12px rgba(0,255,136,0.3);
    animation: pulse-border 3s ease-in-out infinite;
  }

  @keyframes pulse-border {
    0%, 100% { box-shadow: 0 0 12px rgba(0,255,136,0.3); }
    50% { box-shadow: 0 0 24px rgba(0,255,136,0.6); }
  }

  .logo-text {
    font-family: 'Syne', sans-serif;
    font-weight: 800;
    font-size: 1.1rem;
    letter-spacing: 0.05em;
    color: var(--accent);
  }

  .logo-sub {
    font-size: 0.65rem;
    color: var(--muted);
    letter-spacing: 0.15em;
    text-transform: uppercase;
  }

  .header-right {
    display: flex;
    align-items: center;
    gap: 1.5rem;
  }

  .status-pill {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.3rem 0.75rem;
    background: var(--accent-dim);
    border: 1px solid var(--accent);
    border-radius: 100px;
    font-size: 0.7rem;
    color: var(--accent);
    letter-spacing: 0.1em;
  }

  .status-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--accent);
    animation: blink 1.5s ease-in-out infinite;
  }

  @keyframes blink {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.2; }
  }

  .refresh-counter {
    font-size: 0.7rem;
    color: var(--muted);
  }

  main {
    padding: 1.5rem 2rem;
    max-width: 1400px;
    margin: 0 auto;
  }

  /* Watch path bar */
  .watch-bar {
    background: var(--surface2);
    border: 1px solid var(--border2);
    border-radius: 8px;
    padding: 0.75rem 1.25rem;
    margin-bottom: 1.5rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
  }

  .watch-label {
    font-size: 0.65rem;
    color: var(--muted);
    letter-spacing: 0.15em;
    text-transform: uppercase;
    white-space: nowrap;
  }

  .watch-path {
    font-size: 0.85rem;
    color: var(--blue);
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .pivot-form {
    display: flex;
    gap: 0.5rem;
    align-items: center;
  }

  .pivot-input {
    background: var(--bg);
    border: 1px solid var(--border2);
    border-radius: 6px;
    padding: 0.4rem 0.75rem;
    color: var(--text);
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.75rem;
    width: 280px;
    outline: none;
    transition: border-color 0.2s;
  }

  .pivot-input:focus { border-color: var(--accent); }
  .pivot-input::placeholder { color: var(--muted); }

  .btn {
    padding: 0.4rem 1rem;
    border-radius: 6px;
    border: 1px solid;
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.75rem;
    cursor: pointer;
    transition: all 0.15s;
    letter-spacing: 0.05em;
  }

  .btn-accent {
    background: var(--accent-dim);
    border-color: var(--accent);
    color: var(--accent);
  }
  .btn-accent:hover { background: var(--accent); color: var(--bg); }

  .btn-muted {
    background: transparent;
    border-color: var(--border2);
    color: var(--muted);
  }
  .btn-muted:hover { border-color: var(--text); color: var(--text); }

  /* Stats grid */
  .stats-grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 1rem;
    margin-bottom: 1.5rem;
  }

  .stat-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 1.25rem;
    position: relative;
    overflow: hidden;
    transition: border-color 0.2s;
  }

  .stat-card:hover { border-color: var(--border2); }

  .stat-card::before {
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 2px;
  }

  .stat-card.green::before { background: var(--green); }
  .stat-card.blue::before  { background: var(--blue); }
  .stat-card.yellow::before { background: var(--yellow); }
  .stat-card.red::before   { background: var(--red); }

  .stat-label {
    font-size: 0.65rem;
    color: var(--muted);
    letter-spacing: 0.15em;
    text-transform: uppercase;
    margin-bottom: 0.5rem;
  }

  .stat-value {
    font-family: 'Syne', sans-serif;
    font-size: 2rem;
    font-weight: 800;
    line-height: 1;
    margin-bottom: 0.25rem;
  }

  .stat-card.green .stat-value { color: var(--green); }
  .stat-card.blue  .stat-value { color: var(--blue); }
  .stat-card.yellow .stat-value { color: var(--yellow); }
  .stat-card.red   .stat-value { color: var(--red); }

  .stat-sub {
    font-size: 0.7rem;
    color: var(--muted);
  }

  /* Main content grid */
  .content-grid {
    display: grid;
    grid-template-columns: 1fr 380px;
    gap: 1.5rem;
  }

  .panel {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    overflow: hidden;
  }

  .panel-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0.875rem 1.25rem;
    border-bottom: 1px solid var(--border);
    background: var(--surface2);
  }

  .panel-title {
    font-size: 0.7rem;
    letter-spacing: 0.15em;
    text-transform: uppercase;
    color: var(--muted);
  }

  .panel-badge {
    font-size: 0.65rem;
    padding: 0.15rem 0.5rem;
    border-radius: 100px;
    background: var(--border);
    color: var(--muted);
  }

  /* Event feed */
  .event-feed {
    height: 420px;
    overflow-y: auto;
    scrollbar-width: thin;
    scrollbar-color: var(--border2) transparent;
  }

  .event-row {
    display: grid;
    grid-template-columns: 110px 90px 1fr 100px;
    gap: 0.75rem;
    padding: 0.6rem 1.25rem;
    border-bottom: 1px solid var(--border);
    font-size: 0.72rem;
    align-items: center;
    animation: slide-in 0.2s ease;
    transition: background 0.15s;
  }

  .event-row:hover { background: var(--surface2); }

  @keyframes slide-in {
    from { opacity: 0; transform: translateX(-8px); }
    to   { opacity: 1; transform: translateX(0); }
  }

  .event-ts { color: var(--muted); }

  .event-kind {
    font-size: 0.65rem;
    padding: 0.15rem 0.5rem;
    border-radius: 4px;
    letter-spacing: 0.08em;
    font-weight: 500;
    text-align: center;
  }

  .kind-MODIFIED  { background: var(--blue-dim);   color: var(--blue);   border: 1px solid rgba(88,166,255,0.3); }
  .kind-CREATED   { background: var(--green-dim);  color: var(--green);  border: 1px solid rgba(63,185,80,0.3); }
  .kind-DELETED   { background: var(--red-dim);    color: var(--red);    border: 1px solid rgba(248,81,73,0.3); }
  .kind-MOVED     { background: #2a1f3a;           color: var(--purple); border: 1px solid rgba(188,140,255,0.3); }

  .event-path {
    color: var(--text);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .event-hash {
    color: var(--muted);
    font-size: 0.65rem;
    text-align: right;
    font-variant-numeric: tabular-nums;
  }

  .feed-empty {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    height: 100%;
    color: var(--muted);
    font-size: 0.8rem;
    gap: 0.5rem;
  }

  /* Right panel — sidebar */
  .sidebar { display: flex; flex-direction: column; gap: 1rem; }

  /* Alerts */
  .alerts-list { padding: 0.75rem; display: flex; flex-direction: column; gap: 0.5rem; }

  .alert-item {
    background: var(--red-dim);
    border: 1px solid rgba(248,81,73,0.3);
    border-radius: 6px;
    padding: 0.6rem 0.875rem;
    font-size: 0.72rem;
  }

  .alert-count { color: var(--red); font-weight: 500; }
  .alert-time  { color: var(--muted); font-size: 0.65rem; margin-top: 0.2rem; }

  .no-alerts {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.875rem 1.25rem;
    font-size: 0.75rem;
    color: var(--green);
  }

  /* Snapshots */
  .snapshot-list { max-height: 260px; overflow-y: auto; }

  .snapshot-item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0.6rem 1.25rem;
    border-bottom: 1px solid var(--border);
    font-size: 0.72rem;
    transition: background 0.15s;
    cursor: pointer;
  }

  .snapshot-item:hover { background: var(--surface2); }
  .snapshot-name { color: var(--blue); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 200px; }
  .snapshot-ts   { color: var(--muted); font-size: 0.65rem; }

  /* Ignore patterns */
  .patterns-wrap {
    padding: 0.875rem 1.25rem;
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
    max-height: 150px;
    overflow-y: auto;
  }

  .pattern-tag {
    font-size: 0.65rem;
    padding: 0.15rem 0.5rem;
    background: var(--surface2);
    border: 1px solid var(--border2);
    border-radius: 4px;
    color: var(--muted);
  }

  .add-pattern {
    display: flex;
    gap: 0.5rem;
    padding: 0.75rem 1.25rem;
    border-top: 1px solid var(--border);
  }

  .add-pattern input {
    flex: 1;
    background: var(--bg);
    border: 1px solid var(--border2);
    border-radius: 6px;
    padding: 0.35rem 0.6rem;
    color: var(--text);
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.72rem;
    outline: none;
  }

  .add-pattern input:focus { border-color: var(--accent); }

  /* Audit trigger */
  .audit-controls {
    padding: 0.875rem 1.25rem;
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
  }

  .audit-row { display: flex; gap: 0.5rem; }

  .audit-result {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 0.6rem 0.875rem;
    font-size: 0.7rem;
    color: var(--green);
    min-height: 36px;
    word-break: break-all;
    display: none;
  }

  .audit-result.visible { display: block; }

  /* Footer */
  footer {
    text-align: center;
    padding: 1.5rem;
    font-size: 0.65rem;
    color: var(--muted);
    letter-spacing: 0.1em;
    border-top: 1px solid var(--border);
    margin-top: 2rem;
  }

  /* Scrollbar */
  ::-webkit-scrollbar { width: 4px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: var(--border2); border-radius: 2px; }
  /* Project tabs — one per watched project */
  .tab-strip {
    display: flex;
    align-items: stretch;
    gap: 2px;
    padding: 0 2rem;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    overflow-x: auto;
  }
  .tab {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.6rem 1rem;
    font-size: 0.75rem;
    color: var(--muted);
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    font-family: inherit;
    cursor: pointer;
    white-space: nowrap;
    transition: color .15s, border-color .15s, background .15s;
  }
  .tab:hover { color: var(--text); background: var(--surface2); }
  .tab.active {
    color: var(--accent);
    border-bottom-color: var(--accent);
    background: var(--accent-dim);
  }
  .tab-count {
    font-size: 0.65rem;
    padding: 1px 6px;
    border-radius: 10px;
    background: var(--surface2);
    color: var(--muted);
  }
  .tab.active .tab-count { background: var(--accent); color: var(--bg); }
  .tab-live {
    width: 6px; height: 6px; border-radius: 50%;
    background: var(--green);
    box-shadow: 0 0 6px var(--green);
  }
  /* Suspended: dimmed but still selectable. Its history is intact and the
     next prompt in that project re-arms it. */
  .tab.suspended { opacity: 0.45; }
  .tab.suspended.active { opacity: 0.75; }
  .tab-idle {
    width: 6px; height: 6px; border-radius: 50%;
    background: var(--muted);
    border: 1px solid var(--border2);
  }
  .tab-empty {
    padding: 0.6rem 1rem;
    font-size: 0.75rem;
    color: var(--muted);
  }
  .scope-note {
    font-size: 0.6rem;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.1em;
  }
</style>
</head>
<body>

<header>
  <div class="logo">
    <div class="logo-icon">🛡</div>
    <div>
      <div class="logo-text">SHADOW-CORE SENTINEL</div>
      <div class="logo-sub">Mission Control</div>
    </div>
  </div>
  <div class="header-right">
    <div class="status-pill">
      <div class="status-dot"></div>
      <span>ACTIVE</span>
    </div>
    <div class="refresh-counter" id="refresh-counter">Refreshing in 5s</div>
  </div>
</header>

<!-- Project tabs. Selecting one changes only what THIS browser requests;
     it does not move the server's primary or touch another session. -->
<div class="tab-strip" id="tab-strip">
  <div class="tab-empty">No projects watched — call watch_project, or pivot below.</div>
</div>

<main>

  <!-- Watch path bar -->
  <div class="watch-bar">
    <span class="watch-label">Viewing</span>
    <div>
      <span class="watch-path" id="watch-path">—</span>
      <div style="font-size:0.65rem;color:var(--accent);margin-top:2px"
           id="project-name">—</div>
    </div>
    <div class="pivot-form">
      <input class="pivot-input" id="pivot-input" placeholder="Pivot to new path..." type="text">
      <button class="btn btn-accent" onclick="pivotPath()">PIVOT</button>
      <button class="btn btn-muted" onclick="rollback()">ROLLBACK</button>
    </div>
  </div>

  <!-- Stats row -->
  <div class="stats-grid">
    <div class="stat-card green">
      <div class="stat-label">DB Events Total</div>
      <div class="stat-value" id="stat-db">—</div>
      <div class="stat-sub">persisted to SQLite</div>
    </div>
    <div class="stat-card blue">
      <div class="stat-label">Memory Buffer</div>
      <div class="stat-value" id="stat-mem">—</div>
      <div class="stat-sub">recent events in RAM</div>
    </div>
    <div class="stat-card yellow">
      <div class="stat-label">Active Alerts</div>
      <div class="stat-value" id="stat-alerts">—</div>
      <div class="stat-sub">rate threshold breaches</div>
    </div>
    <div class="stat-card red">
      <div class="stat-label">Ignore Patterns</div>
      <div class="stat-value" id="stat-patterns">—</div>
      <div class="stat-sub scope-note">global — all projects</div>
    </div>
  </div>

  <!-- Main content -->
  <div class="content-grid">

    <!-- Live event feed -->
    <div>
      <div class="panel">
        <div class="panel-header">
          <span class="panel-title">Live Event Feed</span>
          <span class="panel-badge" id="feed-count">0 events</span>
        </div>
        <div class="event-feed" id="event-feed">
          <div class="feed-empty">
            <span>⏳</span>
            <span>Waiting for filesystem events...</span>
          </div>
        </div>
      </div>
    </div>

    <!-- Sidebar -->
    <div class="sidebar">

      <!-- Trigger audit -->
      <div class="panel">
        <div class="panel-header">
          <span class="panel-title">Trigger Audit</span>
        </div>
        <div class="audit-controls">
          <div class="audit-row">
            <input class="pivot-input" style="flex:1" id="audit-label" placeholder="Snapshot label..." type="text">
          </div>
          <div class="audit-row">
            <button class="btn btn-accent" style="flex:1" onclick="triggerAudit('disk')">DISK SNAPSHOT</button>
            <button class="btn btn-muted" style="flex:1" onclick="triggerAudit('events')">EVENTS SNAPSHOT</button>
          </div>
          <div class="audit-result" id="audit-result"></div>
        </div>
      </div>

      <!-- Alerts -->
      <div class="panel">
        <div class="panel-header">
          <span class="panel-title">Alerts</span>
          <span class="panel-badge" id="alert-badge">0</span>
        </div>
        <div id="alerts-container">
          <div class="no-alerts">✓ No alerts — system nominal</div>
        </div>
      </div>

      <!-- Recent snapshots -->
      <div class="panel">
        <div class="panel-header">
          <span class="panel-title">Snapshots</span>
        </div>
        <div class="snapshot-list" id="snapshot-list">
          <div style="padding:1rem;font-size:0.75rem;color:var(--muted)">No snapshots yet</div>
        </div>
      </div>

      <!-- Ignore patterns -->
      <div class="panel">
        <div class="panel-header">
          <span class="panel-title">Ignore Patterns</span>
          <span class="panel-badge" id="pattern-count">0</span>
        </div>
        <div class="patterns-wrap" id="patterns-wrap"></div>
        <div class="add-pattern">
          <input id="pattern-input" placeholder="Add pattern e.g. *.log" type="text"
            onkeydown="if(event.key==='Enter') addPattern()">
          <button class="btn btn-accent" onclick="addPattern()">ADD</button>
        </div>
      </div>

    </div>
  </div>
</main>

<footer>
  SHADOW-CORE SENTINEL v<span id="footer-version">—</span> &nbsp;·&nbsp;
  <span id="footer-time">—</span>
</footer>

<script>
  let countdown = 5;
  let stats = {};
  let events = [];
  let projects = [];

  // Which project this browser is looking at. Kept CLIENT-side so two windows
  // can watch two projects at once; the server's primary is never moved by
  // clicking a tab. Survives refresh, and falls back to the primary when the
  // remembered project is no longer watched.
  let currentProject = localStorage.getItem('sentinel.project') || null;

  function scoped(url) {
    return currentProject
      ? url + '?project=' + encodeURIComponent(currentProject)
      : url;
  }

  function selectProject(key) {
    currentProject = key;
    if (key) localStorage.setItem('sentinel.project', key);
    else localStorage.removeItem('sentinel.project');
    renderTabs();
    refresh();
  }

  async function fetchProjects() {
    try {
      const r = await fetch('/api/projects');
      projects = await r.json();
      // A remembered project that is no longer watched would otherwise pin the
      // view to a dead tab and silently show the primary's numbers under its
      // name. Drop the selection and fall back.
      if (currentProject && !projects.some(p => p.key === currentProject)) {
        currentProject = null;
        localStorage.removeItem('sentinel.project');
      }
      if (!currentProject) {
        const primary = projects.find(p => p.is_primary) || projects[0];
        if (primary) currentProject = primary.key;
      }
      renderTabs();
    } catch(e) { console.error('Projects fetch error', e); }
  }

  function renderTabs() {
    const strip = document.getElementById('tab-strip');
    if (!projects.length) {
      strip.innerHTML = '<div class="tab-empty">No projects watched — call watch_project, or pivot below.</div>';
      return;
    }
    strip.innerHTML = projects.map(p => {
      const active = p.key === currentProject ? ' active' : '';
      const idle = p.suspended ? ' suspended' : '';
      const dot = p.suspended
        ? '<span class="tab-idle" title="Suspended for inactivity — resumes on your next prompt"></span>'
        : (p.live_events > 0 ? '<span class="tab-live"></span>' : '');
      const tip = p.suspended
        ? `${p.path}\nSUSPENDED — idle ${Math.round(p.idle_seconds / 60)} min. Resumes on your next prompt in this project.`
        : p.path;
      return `<button class="tab${active}${idle}" title="${tip}"
                onclick="selectProject('${p.key.replace(/\\/g, '\\\\').replace(/'/g, "\\'")}')">
                ${dot}<span>${p.name}</span>
                <span class="tab-count">${p.db_events.toLocaleString()}</span>
              </button>`;
    }).join('');
  }

  async function fetchStats() {
    try {
      const r = await fetch(scoped('/api/stats'));
      stats = await r.json();
      renderStats();
    } catch(e) { console.error('Stats fetch error', e); }
  }

  async function fetchEvents() {
    try {
      const r = await fetch(scoped('/api/events'));
      events = await r.json();
      renderFeed();
    } catch(e) { console.error('Events fetch error', e); }
  }

  async function fetchSnapshots() {
    try {
      const r = await fetch(scoped('/api/snapshots'));
      const snaps = await r.json();
      renderSnapshots(snaps);
    } catch(e) {}
  }

  function renderStats() {
    document.getElementById('watch-path').textContent = stats.watch_dir || 'Nothing watched';
    document.getElementById('project-name').textContent =
      stats.project_name ? '📁 ' + stats.project_name : '';
    document.getElementById('stat-db').textContent = (stats.total_db_events || 0).toLocaleString();
    document.getElementById('stat-mem').textContent = (stats.memory_events || 0).toLocaleString();
    document.getElementById('stat-alerts').textContent = (stats.recent_alerts || 0);
    document.getElementById('stat-patterns').textContent = (stats.ignore_patterns || 0);
    document.getElementById('footer-version').textContent = stats.version || '—';
    document.getElementById('footer-time').textContent = stats.as_of ? new Date(stats.as_of).toLocaleTimeString() : '—';

    // Alerts
    const alertBadge = document.getElementById('alert-badge');
    alertBadge.textContent = stats.recent_alerts || 0;
    const container = document.getElementById('alerts-container');
    if (!stats.alerts_detail || stats.alerts_detail.length === 0) {
      container.innerHTML = '<div class="no-alerts">✓ No alerts — system nominal</div>';
    } else {
      container.innerHTML = '<div class="alerts-list">' +
        stats.alerts_detail.map(a =>
          `<div class="alert-item">
            <div class="alert-count">⚡ ${a.count} events in ${a.window}s</div>
            <div class="alert-time">${new Date(a.ts * 1000).toLocaleTimeString()}</div>
          </div>`
        ).join('') + '</div>';
    }

    // Patterns
    const patterns = stats.patterns_list || [];
    document.getElementById('pattern-count').textContent = patterns.length;
    document.getElementById('patterns-wrap').innerHTML =
      patterns.map(p => `<span class="pattern-tag">${p}</span>`).join('');
  }

  function renderFeed() {
    const feed = document.getElementById('event-feed');
    document.getElementById('feed-count').textContent = events.length + ' events';
    if (!events.length) {
      feed.innerHTML = '<div class="feed-empty"><span>⏳</span><span>Waiting for filesystem events...</span></div>';
      return;
    }
    // Paths are shown RELATIVE TO THE PROJECT ROOT, not as the last two
    // segments. Truncating to two segments renders `backend/main.py` and
    // `tools/backend/main.py` identically, and same-named files are the norm
    // across and within projects — the row has to say which one changed.
    const root = (stats.watch_dir || '').replace(/\\/g, '/').replace(/\/+$/, '');
    const rows = [...events].reverse().map(e => {
      const ts = new Date(e.ts).toLocaleTimeString('en', {hour12: false, hour:'2-digit', minute:'2-digit', second:'2-digit'});
      const hash = e.sha256 ? e.sha256.substring(0, 8) + '...' : '—';
      const full = e.src_path.replace(/\\/g, '/');
      const path = (root && full.toLowerCase().startsWith(root.toLowerCase() + '/'))
        ? full.slice(root.length + 1)
        : full;
      return `<div class="event-row">
        <span class="event-ts">${ts}</span>
        <span class="event-kind kind-${e.kind}">${e.kind}</span>
        <span class="event-path" title="${e.src_path}">${path}</span>
        <span class="event-hash">${hash}</span>
      </div>`;
    }).join('');
    feed.innerHTML = rows;
  }

  function renderSnapshots(snaps) {
    const el = document.getElementById('snapshot-list');
    if (!snaps.length) {
      el.innerHTML = '<div style="padding:1rem;font-size:0.75rem;color:var(--muted)">No snapshots yet</div>';
      return;
    }
    el.innerHTML = snaps.slice(0, 20).map(s =>
      `<div class="snapshot-item">
        <span class="snapshot-name" title="${s}">${s}</span>
      </div>`
    ).join('');
  }

  async function pivotPath() {
    const path = document.getElementById('pivot-input').value.trim();
    if (!path) return;
    try {
      const r = await fetch('/api/pivot', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({path})
      });
      const d = await r.json();
      if (d.status === 'ok') {
        document.getElementById('pivot-input').value = '';
        // Pivot ADDS a watch. Jump this browser to the new project's tab.
        await fetchProjects();
        selectProject(d.new_watch_path);
      } else {
        alert('Pivot failed: ' + (d.message || JSON.stringify(d)));
      }
    } catch(e) { alert('Pivot error: ' + e); }
  }

  async function rollback() {
    try {
      const r = await fetch('/api/rollback', { method: 'POST' });
      const d = await r.json();
      await fetchProjects();
      if (d.status === 'ok') selectProject(d.restored_path);
      else refresh();
    } catch(e) {}
  }

  async function triggerAudit(mode) {
    const label = document.getElementById('audit-label').value.trim() || 'dashboard';
    const resultEl = document.getElementById('audit-result');
    resultEl.textContent = 'Running audit...';
    resultEl.classList.add('visible');
    try {
      const r = await fetch('/api/audit', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({label, mode, project: currentProject})
      });
      const d = await r.json();
      resultEl.textContent = d.snapshot_uri || JSON.stringify(d);
      fetchSnapshots();
    } catch(e) {
      resultEl.textContent = 'Error: ' + e;
    }
  }

  async function addPattern() {
    const pattern = document.getElementById('pattern-input').value.trim();
    if (!pattern) return;
    try {
      await fetch('/api/ignore', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({pattern})
      });
      document.getElementById('pattern-input').value = '';
      fetchStats();
    } catch(e) {}
  }

  function tick() {
    countdown--;
    document.getElementById('refresh-counter').textContent =
      countdown <= 0 ? 'Refreshing...' : `Refreshing in ${countdown}s`;
    if (countdown <= 0) {
      countdown = 5;
      refreshAll();
    }
  }

  function refresh() {
    fetchStats();
    fetchEvents();
    fetchSnapshots();
  }

  async function refreshAll() {
    // Tabs first: the project list decides what everything else is scoped to.
    await fetchProjects();
    refresh();
  }

  refreshAll();
  setInterval(tick, 1000);
</script>
</body>
</html>"""


def _make_handler(get_stats: Callable[..., dict], get_events: Callable[..., list],
                  get_snapshots: Callable[..., list], get_projects: Callable[[], list],
                  do_pivot: Callable[[str], dict],
                  do_rollback: Callable[[], dict], do_audit: Callable[..., dict],
                  do_ignore: Callable[[str], dict]):

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def _project(self):
            """`?project=<abs path>` — which project this request is about.

            Scoping travels in the REQUEST rather than in server state, so two
            browser tabs can view two projects at once and neither disturbs the
            other or the primary. Absent means "the primary", which is what a
            freshly opened dashboard asks for.
            """
            q = parse_qs(urlparse(self.path).query)
            vals = q.get('project') or []
            return vals[0] if vals and vals[0] else None

        def do_GET(self):
            p = urlparse(self.path).path
            if p in ('/', '/index.html'):
                self._html(DASHBOARD_HTML)
            elif p == '/api/stats':
                self._json(get_stats(self._project()))
            elif p == '/api/events':
                self._json(get_events(self._project()))
            elif p == '/api/snapshots':
                self._json(get_snapshots(self._project()))
            elif p == '/api/projects':
                self._json(get_projects())
            else:
                self.send_response(404); self.end_headers()

        def do_POST(self):
            p = urlparse(self.path).path
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length) or '{}')
            if p == '/api/pivot':
                self._json(do_pivot(body.get('path', '')))
            elif p == '/api/rollback':
                self._json(do_rollback())
            elif p == '/api/audit':
                self._json(do_audit(body.get('label', 'dashboard'),
                                    body.get('mode', 'disk'),
                                    body.get('project') or self._project()))
            elif p == '/api/ignore':
                self._json(do_ignore(body.get('pattern', '')))
            else:
                self.send_response(404); self.end_headers()

        def _json(self, data):
            body = json.dumps(data, default=str).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(body)

        def _html(self, content: str):
            body = content.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def start_dashboard(port: int, get_stats, get_events, get_snapshots, get_projects,
                    do_pivot, do_rollback, do_audit, do_ignore) -> threading.Thread:
    handler = _make_handler(get_stats, get_events, get_snapshots, get_projects,
                            do_pivot, do_rollback, do_audit, do_ignore)
    server = HTTPServer(('127.0.0.1', port), handler)

    def run():
        logger.info(f"Dashboard → http://127.0.0.1:{port}")
        server.serve_forever()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t
