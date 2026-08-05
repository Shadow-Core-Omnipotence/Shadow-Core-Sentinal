"""The SHA-256 every claim in the audit trail rests on.

`sha256_of` returns None rather than raising, so its failure modes are quiet by
design — which is exactly why they need testing. A None means "unknown", and
the rest of the system is careful to treat that differently from "changed":
`diff_states` refuses to report a file as modified when either side is None.
That contract is only worth anything if None really is what unreadable files
produce, and if a transient lock really is retried rather than reported.
"""
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hasher  # noqa: E402
from hasher import sha256_of  # noqa: E402


def test_a_known_content_hashes_to_the_known_digest(tmp_path):
    f = tmp_path / "a.txt"
    f.write_bytes(b"hello world")
    assert sha256_of(f) == hashlib.sha256(b"hello world").hexdigest()


def test_an_empty_file_hashes_to_the_empty_digest(tmp_path):
    f = tmp_path / "empty.txt"
    f.write_bytes(b"")
    assert sha256_of(f) == hashlib.sha256(b"").hexdigest()


def test_a_file_larger_than_one_chunk_is_hashed_whole(tmp_path):
    """Chunked reads must not truncate — _CHUNK is 1 MiB."""
    payload = bytes(range(256)) * 8192  # 2 MiB
    f = tmp_path / "big.bin"
    f.write_bytes(payload)
    assert sha256_of(f) == hashlib.sha256(payload).hexdigest()


def test_two_files_with_the_same_bytes_hash_alike(tmp_path):
    (tmp_path / "a").write_bytes(b"same")
    (tmp_path / "b").write_bytes(b"same")
    assert sha256_of(tmp_path / "a") == sha256_of(tmp_path / "b")


def test_one_changed_byte_changes_the_hash(tmp_path):
    f = tmp_path / "a"
    f.write_bytes(b"aaaa")
    before = sha256_of(f)
    f.write_bytes(b"aaab")
    assert sha256_of(f) != before


# ── unreadable is UNKNOWN, not an exception ──────────────────────────────────

def test_a_missing_file_is_none(tmp_path):
    assert sha256_of(tmp_path / "nope.txt") is None


def test_a_directory_is_none(tmp_path):
    assert sha256_of(tmp_path) is None


def test_a_permission_error_is_retried_then_gives_up(tmp_path, monkeypatch):
    """The backoff branch: transient IDE/build locks are worth waiting out."""
    f = tmp_path / "locked.txt"
    f.write_bytes(b"x")

    attempts = []
    slept = []
    monkeypatch.setattr(hasher.time, "sleep", lambda s: slept.append(s))

    def always_locked(*a, **kw):
        attempts.append(1)
        raise PermissionError("locked")

    monkeypatch.setattr(Path, "open", always_locked)

    assert sha256_of(f, max_retries=3) is None
    assert len(attempts) == 4, "initial attempt plus three retries"
    assert slept == [0.1, 0.2, 0.4], "backoff doubles"


def test_a_lock_that_clears_is_hashed_successfully(tmp_path, monkeypatch):
    """A retry that succeeds must return the real digest, not None."""
    f = tmp_path / "flaky.txt"
    f.write_bytes(b"payload")
    real_open = Path.open
    calls = []

    monkeypatch.setattr(hasher.time, "sleep", lambda s: None)

    def flaky(self, *a, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise PermissionError("still locked")
        return real_open(self, *a, **kw)

    monkeypatch.setattr(Path, "open", flaky)

    assert sha256_of(f) == hashlib.sha256(b"payload").hexdigest()
    assert len(calls) == 2


def test_an_oserror_is_not_retried(tmp_path, monkeypatch):
    """A missing file will not appear on the next attempt; waiting is waste."""
    f = tmp_path / "gone.txt"
    f.write_bytes(b"x")
    calls = []

    def always_oserror(*a, **kw):
        calls.append(1)
        raise OSError("gone")

    monkeypatch.setattr(Path, "open", always_oserror)

    assert sha256_of(f) is None
    assert len(calls) == 1


def test_no_retries_means_one_attempt(tmp_path, monkeypatch):
    f = tmp_path / "a.txt"
    f.write_bytes(b"x")
    calls = []

    def locked(*a, **kw):
        calls.append(1)
        raise PermissionError("locked")

    monkeypatch.setattr(hasher.time, "sleep", lambda s: None)
    monkeypatch.setattr(Path, "open", locked)

    assert sha256_of(f, max_retries=0) is None
    assert len(calls) == 1


# ── the contract the rest of the system relies on ────────────────────────────

def test_an_unknown_hash_is_not_reported_as_a_modification():
    """diff_states must not invent a change from a None on either side."""
    from lease import diff_states

    _, _, modified = diff_states({"a": None}, {"a": "abc"})
    assert modified == []

    _, _, modified = diff_states({"a": "abc"}, {"a": None})
    assert modified == []


def test_a_real_hash_difference_is_reported():
    from lease import diff_states

    _, _, modified = diff_states({"a": "abc"}, {"a": "def"})
    assert modified == ["a"]
