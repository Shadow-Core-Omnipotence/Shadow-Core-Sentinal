//! Shadow-Core Sentinel — standalone dashboard.
//!
//! WHY THIS EXISTS
//! ---------------
//! The Python dashboard (`dashboard.py`, 25 KB of stdlib `http.server`) runs in
//! a **daemon thread inside the Sentinel process**. TECH_DEBT_AUDIT.md issue #4
//! records what that costs: when the main thread died, the daemon thread kept
//! the process alive, so a dead server looked healthy. A monitoring tool whose
//! own failure mode is "looks fine, reports nothing" is worse than no tool.
//!
//! This is a separate process. It cannot mask a Sentinel crash, because it is
//! not holding Sentinel up — if Sentinel dies, this keeps serving and says so.
//!
//! READ-ONLY BY CONSTRUCTION
//! -------------------------
//! Sentinel's writer process owns these SQLite files and is actively inserting
//! into them. Every connection here is opened with SQLITE_OPEN_READ_ONLY, so
//! this binary physically cannot corrupt the audit trail it exists to display.
//! That is enforced by the open flags, not by being careful.
//!
//! It also reads the audit tree directly rather than asking Sentinel, which
//! means it shows EVERY watched project at once. The Python dashboard could
//! only ever show the one the live config pointed at.

use std::collections::HashMap;
use std::fs;
use std::net::SocketAddr;
use std::path::{Path, PathBuf};

use axum::{
    extract::{Query, State},
    http::{header, StatusCode},
    response::{Html, IntoResponse, Response},
    routing::get,
    Json, Router,
};
use rusqlite::{Connection, OpenFlags};
use serde::Serialize;
use serde_json::json;

const DEFAULT_PORT: u16 = 7655;
const DEFAULT_EVENT_LIMIT: usize = 200;
/// Hard ceiling on rows per request. The audit trail runs to hundreds of MB;
/// an unbounded query would happily try to serialise all of it into one
/// response and take the browser down with it.
const MAX_EVENT_LIMIT: usize = 5_000;

#[derive(Clone)]
struct AppState {
    audit_root: PathBuf,
}

#[derive(Serialize)]
struct Project {
    name: String,
    events: i64,
    snapshots: usize,
    /// Most recent event timestamp, or None for a project that has recorded
    /// nothing yet. Distinguishing "idle" from "broken" is the whole job.
    last_event: Option<String>,
}

#[derive(Serialize)]
struct Event {
    ts: String,
    kind: String,
    src_path: String,
    dest_path: Option<String>,
    sha256: Option<String>,
    watch_path: Option<String>,
}

#[derive(Serialize)]
struct Snapshot {
    name: String,
    label: String,
    taken: String,
    bytes: u64,
}

// ── Audit tree discovery ─────────────────────────────────────────────────────

/// Every `<audit_root>/<project>/sentinel.db`, plus a bare `sentinel.db` at the
/// root (older layout, still present on this machine).
fn discover_dbs(audit_root: &Path) -> Vec<(String, PathBuf)> {
    let mut found = Vec::new();

    let root_db = audit_root.join("sentinel.db");
    if root_db.is_file() {
        found.push(("(root)".to_string(), root_db));
    }

    if let Ok(entries) = fs::read_dir(audit_root) {
        for entry in entries.flatten() {
            let path = entry.path();
            if !path.is_dir() {
                continue;
            }
            let db = path.join("sentinel.db");
            if db.is_file() {
                if let Some(name) = path.file_name().and_then(|n| n.to_str()) {
                    found.push((name.to_string(), db));
                }
            }
        }
    }

    found.sort_by(|a, b| a.0.to_lowercase().cmp(&b.0.to_lowercase()));
    found
}

fn db_for(audit_root: &Path, project: &str) -> Option<PathBuf> {
    discover_dbs(audit_root)
        .into_iter()
        .find(|(name, _)| name == project)
        .map(|(_, path)| path)
}

/// Open read-only. Returns None rather than panicking: Sentinel may be
/// mid-write, or the file may have been removed since discovery, and neither
/// should take the dashboard down.
fn open_ro(path: &Path) -> Option<Connection> {
    Connection::open_with_flags(
        path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_URI,
    )
    .ok()
}

fn count_events(conn: &Connection) -> i64 {
    conn.query_row("SELECT COUNT(*) FROM events", [], |r| r.get(0))
        .unwrap_or(0)
}

/// Does this database have the `watch_path` column?
///
/// Older audit databases predate it. Python's EventStore upgrades them with an
/// ALTER TABLE on open — which this process must never do, because it holds the
/// file read-only by design. So it adapts to the schema it finds instead.
/// Without this, every pre-migration database returned 500 while its event
/// COUNT worked fine, which reads as "server broken" rather than "older file".
fn has_watch_path(conn: &Connection) -> bool {
    conn.prepare("SELECT watch_path FROM events LIMIT 0").is_ok()
}

fn last_event_ts(conn: &Connection) -> Option<String> {
    conn.query_row("SELECT ts FROM events ORDER BY id DESC LIMIT 1", [], |r| {
        r.get::<_, String>(0)
    })
    .ok()
}

fn list_snapshots(dir: &Path) -> Vec<Snapshot> {
    let mut out = Vec::new();
    let Ok(entries) = fs::read_dir(dir) else {
        return out;
    };

    for entry in entries.flatten() {
        let path = entry.path();
        let Some(name) = path.file_name().and_then(|n| n.to_str()) else {
            continue;
        };
        if !name.starts_with("snapshot-") || !name.ends_with(".md") {
            continue;
        }

        // snapshot-<ISO8601>-<label>.md — the label may itself contain hyphens,
        // so split only on the first two.
        let stem = &name["snapshot-".len()..name.len() - 3];
        let (taken, label) = match stem.split_once('-') {
            Some((t, l)) => (t.to_string(), l.to_string()),
            None => (stem.to_string(), String::new()),
        };

        out.push(Snapshot {
            name: name.to_string(),
            label,
            taken,
            bytes: entry.metadata().map(|m| m.len()).unwrap_or(0),
        });
    }

    out.sort_by(|a, b| b.taken.cmp(&a.taken)); // newest first
    out
}

// ── Handlers ─────────────────────────────────────────────────────────────────

async fn index() -> Html<&'static str> {
    Html(INDEX_HTML)
}

/// Liveness for THIS process plus a read of the audit tree. Deliberately
/// unauthenticated and cheap — it is what you curl when something looks wrong.
async fn health(State(st): State<AppState>) -> Response {
    let dbs = discover_dbs(&st.audit_root);
    let reachable = dbs.iter().filter(|(_, p)| open_ro(p).is_some()).count();

    Json(json!({
        "ok": true,
        "audit_root": st.audit_root.to_string_lossy(),
        "audit_root_exists": st.audit_root.is_dir(),
        "databases_found": dbs.len(),
        "databases_readable": reachable,
    }))
    .into_response()
}

async fn projects(State(st): State<AppState>) -> Response {
    let mut out = Vec::new();

    for (name, db) in discover_dbs(&st.audit_root) {
        let (events, last) = match open_ro(&db) {
            Some(conn) => (count_events(&conn), last_event_ts(&conn)),
            None => (-1, None), // -1 == present but unreadable, not "empty"
        };
        let snaps = db.parent().map(list_snapshots).unwrap_or_default().len();

        out.push(Project {
            name,
            events,
            snapshots: snaps,
            last_event: last,
        });
    }

    Json(out).into_response()
}

async fn stats(State(st): State<AppState>) -> Response {
    let dbs = discover_dbs(&st.audit_root);
    let mut total = 0i64;
    let mut by_project = HashMap::new();

    for (name, db) in &dbs {
        if let Some(conn) = open_ro(db) {
            let n = count_events(&conn);
            total += n;
            by_project.insert(name.clone(), n);
        }
    }

    Json(json!({
        "total_events": total,
        "projects": dbs.len(),
        "by_project": by_project,
    }))
    .into_response()
}

#[derive(serde::Deserialize)]
struct EventQuery {
    project: Option<String>,
    limit: Option<usize>,
}

async fn events(State(st): State<AppState>, Query(q): Query<EventQuery>) -> Response {
    let dbs = discover_dbs(&st.audit_root);
    let Some(project) = q.project.or_else(|| dbs.first().map(|(n, _)| n.clone())) else {
        return Json(json!({ "events": [], "project": null })).into_response();
    };

    let Some(db) = db_for(&st.audit_root, &project) else {
        return (
            StatusCode::NOT_FOUND,
            Json(json!({ "error": format!("no audit database for project {project:?}") })),
        )
            .into_response();
    };

    let Some(conn) = open_ro(&db) else {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({ "error": "audit database could not be opened for reading" })),
        )
            .into_response();
    };

    let limit = q.limit.unwrap_or(DEFAULT_EVENT_LIMIT).min(MAX_EVENT_LIMIT);

    // NULL literal for the older schema, so the column indices below stay the
    // same either way.
    let watch_col = if has_watch_path(&conn) {
        "watch_path"
    } else {
        "NULL"
    };
    let sql = format!(
        "SELECT ts, kind, src_path, dest_path, sha256, {watch_col} \
         FROM events ORDER BY id DESC LIMIT ?1"
    );

    let mut stmt = match conn.prepare(&sql) {
        Ok(s) => s,
        Err(e) => {
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({ "error": e.to_string() })),
            )
                .into_response()
        }
    };

    let rows = stmt
        .query_map([limit], |r| {
            Ok(Event {
                ts: r.get(0)?,
                kind: r.get(1)?,
                src_path: r.get(2)?,
                dest_path: r.get(3)?,
                sha256: r.get(4)?,
                watch_path: r.get(5)?,
            })
        })
        .and_then(|m| m.collect::<Result<Vec<_>, _>>())
        .unwrap_or_default();

    Json(json!({ "project": project, "count": rows.len(), "events": rows })).into_response()
}

async fn snapshots(State(st): State<AppState>, Query(q): Query<EventQuery>) -> Response {
    let dbs = discover_dbs(&st.audit_root);
    let Some(project) = q.project.or_else(|| dbs.first().map(|(n, _)| n.clone())) else {
        return Json(json!({ "snapshots": [] })).into_response();
    };

    let list = db_for(&st.audit_root, &project)
        .and_then(|db| db.parent().map(list_snapshots))
        .unwrap_or_default();

    Json(json!({ "project": project, "count": list.len(), "snapshots": list })).into_response()
}

async fn not_found() -> impl IntoResponse {
    (
        StatusCode::NOT_FOUND,
        [(header::CONTENT_TYPE, "application/json")],
        r#"{"error":"not found"}"#,
    )
}

// ── Entry point ──────────────────────────────────────────────────────────────

#[tokio::main]
async fn main() {
    // Config by environment, matching the Python service so both read the same
    // AUDIT_DIR without a second place to configure.
    let audit_root = std::env::var("AUDIT_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("./audit_logs"));
    let audit_root = audit_root
        .canonicalize()
        .unwrap_or_else(|_| audit_root.clone());

    let port: u16 = std::env::var("DASHBOARD_RS_PORT")
        .ok()
        .and_then(|p| p.parse().ok())
        .unwrap_or(DEFAULT_PORT);

    let state = AppState {
        audit_root: audit_root.clone(),
    };

    let app = Router::new()
        .route("/", get(index))
        .route("/health", get(health))
        .route("/api/projects", get(projects))
        .route("/api/stats", get(stats))
        .route("/api/events", get(events))
        .route("/api/snapshots", get(snapshots))
        .fallback(not_found)
        .with_state(state);

    // Loopback only. This exposes a filesystem audit trail — every path the
    // user has touched — which has no business being reachable off-machine.
    let addr = SocketAddr::from(([127, 0, 0, 1], port));

    let listener = match tokio::net::TcpListener::bind(addr).await {
        Ok(l) => l,
        Err(e) => {
            // Loud and specific. The Python dashboard's equivalent failure was
            // silent, and that is audit issue #1.
            eprintln!("sentinel-dashboard: cannot bind {addr}: {e}");
            eprintln!("  (another process may already hold port {port})");
            std::process::exit(1);
        }
    };

    let dbs = discover_dbs(&audit_root);
    println!("sentinel-dashboard  →  http://{addr}");
    println!("  audit root : {}", audit_root.display());
    println!("  databases  : {}", dbs.len());
    for (name, _) in &dbs {
        println!("      - {name}");
    }
    if dbs.is_empty() {
        println!("  WARNING: no sentinel.db found — is AUDIT_DIR correct?");
    }

    if let Err(e) = axum::serve(listener, app).await {
        eprintln!("sentinel-dashboard: server error: {e}");
        std::process::exit(1);
    }
}

const INDEX_HTML: &str = include_str!("index.html");
