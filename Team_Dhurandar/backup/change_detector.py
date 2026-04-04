"""
change_detector.py
------------------
Scans a directory, compares file hashes against a persistent state index,
and produces a change manifest (new / modified / deleted / unchanged).

Output manifest is JSON-serialisable and ready to feed into the chunking phase.
"""

import hashlib
import json
import os
import time
from dataclasses import dataclass, asdict
from enum import Enum
from pathlib import Path
from typing import Iterator


# ── Types ──────────────────────────────────────────────────────────────────────

class ChangeType(str, Enum):
    NEW       = "new"
    MODIFIED  = "modified"
    DELETED   = "deleted"
    UNCHANGED = "unchanged"


@dataclass
class FileRecord:
    """A single file's state as stored in the index."""
    path: str          # Relative to watch root
    sha256: str
    size: int
    mtime: float       # Last-modified epoch (fast pre-filter)


@dataclass
class FileChange:
    """One entry in the output manifest."""
    path: str
    change_type: ChangeType
    sha256: str | None   # None for DELETED
    size: int | None
    mtime: float | None
    prev_sha256: str | None  # Previous hash for MODIFIED files


# ── State index ────────────────────────────────────────────────────────────────

class StateIndex:
    """
    Persists the last-known state of every file to a JSON file.
    Loaded once at startup, flushed after a successful scan.
    """

    def __init__(self, index_path: Path):
        self.index_path = index_path
        self._records: dict[str, FileRecord] = {}
        self._load()

    def _load(self):
        if self.index_path.exists():
            raw = json.loads(self.index_path.read_text())
            self._records = {
                k: FileRecord(**v) for k, v in raw.items()
            }

    def save(self):
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self.index_path.write_text(
            json.dumps({k: asdict(v) for k, v in self._records.items()}, indent=2)
        )

    def get(self, rel_path: str) -> FileRecord | None:
        return self._records.get(rel_path)

    def all_paths(self) -> set[str]:
        return set(self._records.keys())

    def update(self, record: FileRecord):
        self._records[record.path] = record

    def remove(self, rel_path: str):
        self._records.pop(rel_path, None)


# ── Hashing ────────────────────────────────────────────────────────────────────

def hash_file(path: Path, chunk_bytes: int = 65_536) -> str:
    """SHA-256 a file in streaming chunks — safe for large files."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            block = f.read(chunk_bytes)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


# ── Scanner ────────────────────────────────────────────────────────────────────

IGNORED_DIRS  = {".git", "__pycache__", ".DS_Store", "node_modules", ".venv"}
IGNORED_EXTS  = {".pyc", ".pyo", ".swp", ".tmp"}


def iter_files(root: Path) -> Iterator[Path]:
    """Walk a directory, yielding absolute paths while skipping noise."""
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune ignored dirs in-place so os.walk doesn't descend into them
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
        for fname in filenames:
            p = Path(dirpath) / fname
            if p.suffix not in IGNORED_EXTS:
                yield p


# ── Detector ───────────────────────────────────────────────────────────────────

class ChangeDetector:
    """
    Main entry point.

    Usage:
        detector = ChangeDetector(watch_root="~/projects", index_path=".backup/state.json")
        manifest = detector.scan()
        detector.commit(manifest)   # persist new state after a successful backup
    """

    def __init__(self, watch_root: str | Path, index_path: str | Path):
        self.root  = Path(watch_root).expanduser().resolve()
        self.index = StateIndex(Path(index_path).expanduser().resolve())

    # ── Public API ─────────────────────────────────────────────────────────────

    def scan(self, full: bool = False) -> list[FileChange]:
        """
        Scan the watch root and return a change manifest.

        Args:
            full: If True, hash every file regardless of mtime (slower but exact).
                  Default: use mtime as a fast pre-filter.

        Returns:
            List of FileChange objects sorted by change type, then path.
        """
        changes:  list[FileChange] = []
        seen:     set[str]         = set()
        # Never include the index file itself in backup manifests
        index_abs = self.index.index_path.resolve()

        for abs_path in iter_files(self.root):
            if abs_path.resolve() == index_abs:
                continue
            rel = str(abs_path.relative_to(self.root))
            seen.add(rel)

            stat    = abs_path.stat()
            mtime   = stat.st_mtime
            size    = stat.st_size
            prev    = self.index.get(rel)

            # Fast path: mtime + size unchanged → almost certainly the same file
            if not full and prev and prev.mtime == mtime and prev.size == size:
                changes.append(FileChange(
                    path=rel, change_type=ChangeType.UNCHANGED,
                    sha256=prev.sha256, size=size, mtime=mtime,
                    prev_sha256=None,
                ))
                continue

            # Slow path: hash the file
            sha256 = hash_file(abs_path)

            if prev is None:
                changes.append(FileChange(
                    path=rel, change_type=ChangeType.NEW,
                    sha256=sha256, size=size, mtime=mtime,
                    prev_sha256=None,
                ))
            elif sha256 != prev.sha256:
                changes.append(FileChange(
                    path=rel, change_type=ChangeType.MODIFIED,
                    sha256=sha256, size=size, mtime=mtime,
                    prev_sha256=prev.sha256,
                ))
            else:
                # mtime changed but content is identical (e.g. touch)
                changes.append(FileChange(
                    path=rel, change_type=ChangeType.UNCHANGED,
                    sha256=sha256, size=size, mtime=mtime,
                    prev_sha256=None,
                ))

        # Detect deletions
        for rel in self.index.all_paths() - seen:
            prev = self.index.get(rel)
            changes.append(FileChange(
                path=rel, change_type=ChangeType.DELETED,
                sha256=None, size=None, mtime=None,
                prev_sha256=prev.sha256 if prev else None,
            ))

        # Sort: new → modified → deleted → unchanged
        order = [ChangeType.NEW, ChangeType.MODIFIED, ChangeType.DELETED, ChangeType.UNCHANGED]
        changes.sort(key=lambda c: (order.index(c.change_type), c.path))
        return changes

    def commit(self, manifest: list[FileChange]):
        """
        Persist the new state after a successful backup run.
        Call this ONLY after the backup upload has succeeded.
        """
        for fc in manifest:
            if fc.change_type == ChangeType.DELETED:
                self.index.remove(fc.path)
            elif fc.change_type != ChangeType.UNCHANGED or self.index.get(fc.path) is None:
                self.index.update(FileRecord(
                    path=fc.path,
                    sha256=fc.sha256,
                    size=fc.size,
                    mtime=fc.mtime,
                ))
        self.index.save()

    # ── Helpers ────────────────────────────────────────────────────────────────

    def manifest_to_json(self, manifest: list[FileChange]) -> str:
        return json.dumps([asdict(c) for c in manifest], indent=2)

    def summary(self, manifest: list[FileChange]) -> dict:
        counts = {ct: 0 for ct in ChangeType}
        total_bytes = 0
        for fc in manifest:
            counts[fc.change_type] += 1
            if fc.size and fc.change_type != ChangeType.UNCHANGED:
                total_bytes += fc.size
        return {
            "new":        counts[ChangeType.NEW],
            "modified":   counts[ChangeType.MODIFIED],
            "deleted":    counts[ChangeType.DELETED],
            "unchanged":  counts[ChangeType.UNCHANGED],
            "bytes_to_backup": total_bytes,
        }
