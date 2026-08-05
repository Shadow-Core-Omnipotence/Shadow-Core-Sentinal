"""The dashboard's HTTP layer — previously untested in full.

Two things are checked here. First that request-scoped project selection really
travels in the query string, because that is what lets two browser tabs view two
projects without either moving the server's primary. Second that a bad request
gets an ANSWER: the POST body was parsed before any route dispatch with no
guard, so malformed JSON raised inside the handler and BaseHTTPRequestHandler
dropped the connection — the caller saw a reset socket rather than a reason.
"""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dashboard import start_dashboard  # noqa: E402


class Calls:
    """Records what the HTTP layer passed through to the wiring."""

    def __init__(self):
        self.stats_projects = []
        self.events_projects = []
        self.snapshots_projects = []
        self.pivoted = []
        self.rollbacks = 0
        self.audits = []
        self.ignored = []


@pytest.fixture
def server():
    calls = Calls()

    def get_stats(project=None):
        calls.stats_projects.append(project)
        return {"watch_dir": project or "PRIMARY", "alerts_scope": "process"}

    def get_events(project=None):
        calls.events_projects.append(project)
        return [{"ts": "2026-08-03T00:00:00+00:00", "kind": "MODIFIED",
                 "src_path": "/p/a.py", "dest_path": None, "sha256": None}]

    def get_snapshots(project=None):
        calls.snapshots_projects.append(project)
        return ["snapshot-x.md"]

    def get_projects():
        return [{"key": "/p", "name": "P", "path": "/p", "db_events": 1,
                 "live_events": 0, "is_primary": True, "suspended": False,
                 "idle_seconds": 0}]

    def do_pivot(path):
        calls.pivoted.append(path)
        return {"status": "ok", "new_watch_path": path}

    def do_rollback():
        calls.rollbacks += 1
        return {"status": "ok", "restored_path": "/prev"}

    def do_audit(label, mode, project=None):
        calls.audits.append((label, mode, project))
        return {"status": "ok", "snapshot_uri": "snapshot-y.md"}

    def do_ignore(pattern):
        calls.ignored.append(pattern)
        return {"status": "ok", "pattern": pattern}

    srv = start_dashboard(0, get_stats, get_events, get_snapshots, get_projects,
                          do_pivot, do_rollback, do_audit, do_ignore)
    srv.calls = calls
    srv.base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield srv
    srv.shutdown()
    srv.server_close()


def _get(srv, path):
    with urllib.request.urlopen(srv.base + path, timeout=5) as r:
        return r.status, r.read()


def _post(srv, path, raw: bytes, content_type="application/json"):
    req = urllib.request.Request(
        srv.base + path, data=raw, method="POST",
        headers={"Content-Type": content_type})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


# ── a bad request gets an answer ─────────────────────────────────────────────

@pytest.mark.parametrize("raw", [
    b"{not json",
    b"[]",
    b'"a string"',
    b"\xff\xfe\x00",
])
def test_a_malformed_body_is_refused_not_dropped(server, raw):
    """The regression: this used to reset the connection with no response."""
    status, body = _post(server, "/api/pivot", raw)

    assert status == 400
    assert body["status"] == "error"
    assert body["message"]


def test_an_empty_body_is_accepted_as_an_empty_object(server):
    status, body = _post(server, "/api/rollback", b"")
    assert status == 200
    assert server.calls.rollbacks == 1


def test_an_oversized_body_is_refused(server):
    status, body = _post(server, "/api/ignore", b"x" * (65 * 1024))
    assert status == 413
    assert body["status"] == "error"


def test_a_lying_content_length_does_not_take_the_server_down(server):
    req = urllib.request.Request(
        server.base + "/api/ignore", data=b"{}", method="POST",
        headers={"Content-Type": "application/json", "Content-Length": "5"})
    try:
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass

    # Still serving.
    assert _get(server, "/api/projects")[0] == 200


def test_an_unknown_post_route_is_404(server):
    req = urllib.request.Request(
        server.base + "/api/nope", data=b"{}", method="POST")
    try:
        urllib.request.urlopen(req, timeout=5)
        assert False, "expected 404"
    except urllib.error.HTTPError as e:
        assert e.code == 404


def test_an_unknown_get_route_is_404(server):
    try:
        _get(server, "/api/nope")
        assert False, "expected 404"
    except urllib.error.HTTPError as e:
        assert e.code == 404


# ── scoping travels in the request ───────────────────────────────────────────

def test_a_project_query_reaches_the_wiring(server):
    _get(server, "/api/stats?project=" + urllib.parse.quote(r"C:\work\alpha"))
    assert server.calls.stats_projects == [r"C:\work\alpha"]


def test_no_project_query_means_the_primary(server):
    _get(server, "/api/stats")
    assert server.calls.stats_projects == [None]


def test_an_empty_project_query_means_the_primary(server):
    _get(server, "/api/stats?project=")
    assert server.calls.stats_projects == [None]


def test_events_and_snapshots_are_scoped_too(server):
    _get(server, "/api/events?project=/p/a")
    _get(server, "/api/snapshots?project=/p/a")
    assert server.calls.events_projects == ["/p/a"]
    assert server.calls.snapshots_projects == ["/p/a"]


def test_an_audit_carries_the_project_from_the_body(server):
    _post(server, "/api/audit",
          json.dumps({"label": "L", "mode": "disk", "project": "/p/b"}).encode())
    assert server.calls.audits == [("L", "disk", "/p/b")]


def test_an_audit_falls_back_to_the_query_project(server):
    _post(server, "/api/audit?project=/p/c", json.dumps({"label": "L"}).encode())
    label, mode, project = server.calls.audits[0]
    assert (label, mode, project) == ("L", "disk", "/p/c")


# ── the ordinary routes ──────────────────────────────────────────────────────

def test_the_index_serves_html(server):
    status, body = _get(server, "/")
    assert status == 200
    assert b"SHADOW-CORE SENTINEL" in body


def test_projects_returns_the_tab_strip(server):
    status, body = _get(server, "/api/projects")
    assert status == 200
    assert json.loads(body)[0]["name"] == "P"


def test_a_pivot_passes_the_path_through(server):
    status, body = _post(server, "/api/pivot",
                         json.dumps({"path": r"C:\work\new"}).encode())
    assert status == 200
    assert server.calls.pivoted == [r"C:\work\new"]


def test_a_pivot_with_no_path_still_reaches_the_wiring(server):
    """An empty path is the wiring's error to report, not the parser's."""
    status, _ = _post(server, "/api/pivot", b"{}")
    assert status == 200
    assert server.calls.pivoted == [""]


def test_an_ignore_pattern_passes_through(server):
    _post(server, "/api/ignore", json.dumps({"pattern": "*.log"}).encode())
    assert server.calls.ignored == ["*.log"]
