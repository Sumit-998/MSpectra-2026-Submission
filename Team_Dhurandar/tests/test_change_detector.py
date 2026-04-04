"""
tests/test_change_detector.py
"""

import json
import time
from pathlib import Path

import pytest

from backup.change_detector import ChangeDetector, ChangeType


@pytest.fixture
def workspace(tmp_path):
    """A fresh temp dir with a few files."""
    (tmp_path / "a.txt").write_text("hello world")
    (tmp_path / "b.txt").write_text("foo bar baz")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c.py").write_text("print('hi')")
    return tmp_path


@pytest.fixture
def index_path(tmp_path):
    return tmp_path / ".backup" / "state.json"


def make_detector(workspace, index_path):
    return ChangeDetector(watch_root=workspace, index_path=index_path)


# ── First scan (empty index) ────────────────────────────────────────────────

def test_first_scan_all_new(workspace, index_path):
    det = make_detector(workspace, index_path)
    manifest = det.scan()
    types = {fc.change_type for fc in manifest}
    assert types == {ChangeType.NEW}
    assert len(manifest) == 3


# ── After commit, second scan shows unchanged ───────────────────────────────

def test_second_scan_unchanged(workspace, index_path):
    det = make_detector(workspace, index_path)
    det.commit(det.scan())

    manifest = det.scan()
    assert all(fc.change_type == ChangeType.UNCHANGED for fc in manifest)


# ── Modify a file → MODIFIED ────────────────────────────────────────────────

def test_modified_file(workspace, index_path):
    det = make_detector(workspace, index_path)
    det.commit(det.scan())

    # Touch with new content
    time.sleep(0.01)
    (workspace / "a.txt").write_text("hello world CHANGED")

    manifest = det.scan()
    modified = [fc for fc in manifest if fc.change_type == ChangeType.MODIFIED]
    assert len(modified) == 1
    assert modified[0].path == "a.txt"
    assert modified[0].prev_sha256 is not None


# ── Same content but mtime touched → UNCHANGED (hash check) ─────────────────

def test_touch_without_content_change(workspace, index_path):
    det = make_detector(workspace, index_path)
    det.commit(det.scan())

    p = workspace / "b.txt"
    content = p.read_bytes()
    time.sleep(0.01)
    p.write_bytes(content)   # same bytes, new mtime

    manifest = det.scan()
    b_record = next(fc for fc in manifest if fc.path == "b.txt")
    assert b_record.change_type == ChangeType.UNCHANGED


# ── Add a new file → NEW ─────────────────────────────────────────────────────

def test_new_file(workspace, index_path):
    det = make_detector(workspace, index_path)
    det.commit(det.scan())

    (workspace / "d.txt").write_text("brand new")

    manifest = det.scan()
    new_files = [fc for fc in manifest if fc.change_type == ChangeType.NEW]
    assert len(new_files) == 1
    assert new_files[0].path == "d.txt"


# ── Delete a file → DELETED ──────────────────────────────────────────────────

def test_deleted_file(workspace, index_path):
    det = make_detector(workspace, index_path)
    det.commit(det.scan())

    (workspace / "b.txt").unlink()

    manifest = det.scan()
    deleted = [fc for fc in manifest if fc.change_type == ChangeType.DELETED]
    assert len(deleted) == 1
    assert deleted[0].path == "b.txt"
    assert deleted[0].sha256 is None


# ── JSON manifest round-trip ─────────────────────────────────────────────────

def test_json_manifest(workspace, index_path):
    det = make_detector(workspace, index_path)
    manifest = det.scan()
    raw = det.manifest_to_json(manifest)
    parsed = json.loads(raw)
    assert len(parsed) == 3
    assert all("sha256" in entry for entry in parsed)


# ── Summary counts ───────────────────────────────────────────────────────────

def test_summary(workspace, index_path):
    det = make_detector(workspace, index_path)
    det.commit(det.scan())

    (workspace / "new.txt").write_text("x")
    (workspace / "a.txt").write_text("changed")
    (workspace / "b.txt").unlink()

    manifest = det.scan()
    s = det.summary(manifest)
    assert s["new"]      == 1
    assert s["modified"] == 1
    assert s["deleted"]  == 1
    assert s["unchanged"] == 1   # sub/c.py
    assert s["bytes_to_backup"] > 0


# ── Full-scan flag forces hash even on unchanged mtime ───────────────────────

def test_full_scan(workspace, index_path):
    det = make_detector(workspace, index_path)
    det.commit(det.scan())

    manifest = det.scan(full=True)
    assert all(fc.change_type == ChangeType.UNCHANGED for fc in manifest)
