"""
backup_engine.py
----------------
Full backup pipeline:
  1. Chunking       — content-defined chunking (CDC) splits files into variable blocks
  2. Deduplication  — SHA-256 per chunk, skip chunks already in the store
  3. Compression    — zstd for text/code, passthrough for media
  4. Save           — write to local backup_folder/ AND upload to S3 simultaneously

Usage:
    from backup.backup_engine import BackupEngine

    engine = BackupEngine(
        backup_dir="backup_folder",
        s3_bucket="my-bucket",          # set None to skip S3
        s3_prefix="backups/",
    )
    engine.run(watch_root="test_folder", index_path=".backup/state.json")
"""

import hashlib
import io
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Iterator

import zstandard as zstd

from backup.change_detector import ChangeDetector, ChangeType, FileChange
from backup.ai_predictor import AIPredictor

try:
    from server.client import MetadataClient
    _CLIENT_AVAILABLE = True
except ImportError:
    _CLIENT_AVAILABLE = False

# ── Optional S3 import (graceful if boto3 not configured) ─────────────────────
try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
    _S3_AVAILABLE = True
except ImportError:
    _S3_AVAILABLE = False


# ── Constants ─────────────────────────────────────────────────────────────────

# CDC parameters (bytes)
CDC_MIN_CHUNK  =  512 * 1024   #  512 KB
CDC_AVG_CHUNK  = 1024 * 1024   #    1 MB  (target average)
CDC_MAX_CHUNK  = 4096 * 1024   #    4 MB

# File types that are already compressed — skip compression
SKIP_COMPRESS_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".mov",
    ".avi", ".mkv", ".mp3", ".aac", ".flac", ".zip", ".gz",
    ".bz2", ".xz", ".7z", ".rar", ".zst",
}

ZSTD_LEVEL = 3   # fast + good ratio


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class Chunk:
    index:      int
    sha256:     str
    size:       int          # original size
    compressed: bool
    data:       bytes = field(repr=False)


@dataclass
class ChunkRef:
    """Lightweight reference stored in the manifest (no raw bytes)."""
    index:      int
    sha256:     str
    size:       int
    compressed: bool
    local_path: str | None   # relative path inside backup_folder
    s3_key:     str | None


@dataclass
class FileManifest:
    """Per-file record written to backup_folder/.manifest.json"""
    path:        str          # relative to watch root
    sha256:      str          # whole-file hash
    size:        int
    chunks:      list[ChunkRef]
    backed_up_at: float


@dataclass
class RunSummary:
    files_new:       int = 0
    files_modified:  int = 0
    files_deleted:   int = 0
    files_unchanged: int = 0
    chunks_total:    int = 0
    chunks_skipped:  int = 0   # dedup hits
    bytes_original:  int = 0
    bytes_stored:    int = 0   # after compression
    duration_s:      float = 0.0


# ── CDC Chunker ───────────────────────────────────────────────────────────────

class CDCChunker:
    """
    Content-Defined Chunking using a rolling Rabin-like fingerprint.
    Produces variable-size chunks averaging CDC_AVG_CHUNK bytes.
    Boundary condition: fingerprint & mask == 0
    """

    MASK = (CDC_AVG_CHUNK - 1)   # works when avg is a power of 2

    def __init__(self, min_size=CDC_MIN_CHUNK, max_size=CDC_MAX_CHUNK):
        self.min_size = min_size
        self.max_size = max_size

    def cut(self, data: bytes) -> Iterator[bytes]:
        """Yield variable-size byte chunks from data."""
        offset = 0
        length = len(data)

        while offset < length:
            # Never go below min
            start = offset
            end   = min(offset + self.min_size, length)

            # Scan for a boundary up to max_size
            fp = 0
            while end < min(start + self.max_size, length):
                fp = (fp * 31 + data[end]) & 0xFFFFFFFF
                if fp & self.MASK == 0:
                    end += 1
                    break
                end += 1

            yield data[start:end]
            offset = end


# ── Compressor ────────────────────────────────────────────────────────────────

class Compressor:
    def __init__(self, level: int = ZSTD_LEVEL):
        self._cctx = zstd.ZstdCompressor(level=level)

    def should_compress(self, file_path: str) -> bool:
        return Path(file_path).suffix.lower() not in SKIP_COMPRESS_EXTS

    def compress(self, data: bytes) -> bytes:
        compressed = self._cctx.compress(data)
        # Only use compressed version if it's actually smaller
        return compressed if len(compressed) < len(data) else data

    def is_compressed(self, original: bytes, result: bytes) -> bool:
        return len(result) < len(original)


# ── Chunk Store ───────────────────────────────────────────────────────────────

class ChunkStore:
    """
    Tracks which chunk hashes are already stored.
    Persisted to backup_folder/.chunk_index.json
    """

    def __init__(self, backup_dir: Path):
        self.backup_dir  = backup_dir
        self.index_path  = backup_dir / ".chunk_index.json"
        self._known: set[str] = set()
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        if self.index_path.exists():
            data = json.loads(self.index_path.read_text())
            self._known = set(data.get("chunks", []))

    def save(self):
        self.index_path.write_text(
            json.dumps({"chunks": list(self._known)}, indent=2)
        )

    def exists(self, sha256: str) -> bool:
        with self._lock:
            return sha256 in self._known

    def add(self, sha256: str):
        with self._lock:
            self._known.add(sha256)


# ── S3 Uploader ───────────────────────────────────────────────────────────────

class S3Uploader:
    def __init__(self, bucket: str, prefix: str = ""):
        if not _S3_AVAILABLE:
            raise RuntimeError("boto3 is not installed. Run: pip install boto3")
        self.bucket = bucket
        self.prefix = prefix.rstrip("/") + "/" if prefix else ""
        self._client = boto3.client("s3")

    def upload(self, key: str, data: bytes) -> str:
        full_key = f"{self.prefix}{key}"
        self._client.put_object(Bucket=self.bucket, Key=full_key, Body=data)
        return full_key

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=f"{self.prefix}{key}")
            return True
        except ClientError:
            return False


# ── Backup Engine ─────────────────────────────────────────────────────────────

class BackupEngine:
    """
    Orchestrates the full backup pipeline.

    Args:
        backup_dir:  Local folder where chunks and manifests are stored.
        s3_bucket:   S3 bucket name. Set to None to disable cloud upload.
        s3_prefix:   Key prefix inside the bucket (e.g. "backups/laptop/").
        s3_region:   AWS region (reads from env/config if not set).
        workers:     Thread count for parallel chunk processing.
    """

    def __init__(
        self,
        backup_dir:      str | Path = "backup_folder",
        s3_bucket:       str | None = None,
        s3_prefix:       str        = "backups/",
        metadata_server: str | None = None,
        workers:         int        = 4,
    ):
        self.backup_dir = Path(backup_dir)
        self.backup_dir.mkdir(parents=True, exist_ok=True)

        self.chunker    = CDCChunker()
        self.compressor = Compressor()
        self.store      = ChunkStore(self.backup_dir)
        self.s3         = S3Uploader(s3_bucket, s3_prefix) if s3_bucket else None
        self.workers    = workers
        self.predictor  = AIPredictor(history_path=".backup/predictor.json")
        self.meta       = MetadataClient(metadata_server) if (metadata_server and _CLIENT_AVAILABLE) else None

        self._manifest_path = self.backup_dir / ".manifest.json"
        self._file_manifests: dict[str, FileManifest] = self._load_manifests()

    # ── Public API ─────────────────────────────────────────────────────────────

    def run(self, watch_root: str | Path, index_path: str | Path) -> RunSummary:
        """
        Full backup run:
          1. Detect changes
          2. Process changed files through the pipeline
          3. Commit state on success
        """
        detector = ChangeDetector(watch_root=watch_root, index_path=index_path)
        summary  = RunSummary()
        t0       = time.perf_counter()

        print(f"\n  Scanning {watch_root} for changes…")
        manifest = detector.scan()

        # Count unchanged
        summary.files_unchanged = sum(
            1 for fc in manifest if fc.change_type == ChangeType.UNCHANGED
        )

        # Rank files by AI priority before processing
        ranked     = self.predictor.rank(manifest, watch_root)
        to_process = [
            fc for sf in ranked
            for fc in manifest
            if fc.path == sf.path and fc.change_type in (ChangeType.NEW, ChangeType.MODIFIED)
        ]
        to_delete = [
            fc for fc in manifest if fc.change_type == ChangeType.DELETED
        ]

        summary.files_new      = sum(1 for fc in to_process if fc.change_type == ChangeType.NEW)
        summary.files_modified = sum(1 for fc in to_process if fc.change_type == ChangeType.MODIFIED)
        summary.files_deleted  = len(to_delete)

        if not to_process and not to_delete:
            print("  Nothing to back up — all files unchanged.")
            summary.duration_s = time.perf_counter() - t0
            return summary

        print(f"  {summary.files_new} new  |  {summary.files_modified} modified  |  {summary.files_deleted} deleted\n")

        # Process files in parallel
        root = Path(watch_root).expanduser().resolve()
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {
                pool.submit(self._process_file, root / fc.path, fc): fc
                for fc in to_process
            }
            for future in as_completed(futures):
                fc = futures[future]
                try:
                    file_manifest, stats = future.result()
                    self._file_manifests[fc.path] = file_manifest
                    summary.chunks_total   += stats["chunks_total"]
                    summary.chunks_skipped += stats["chunks_skipped"]
                    summary.bytes_original += stats["bytes_original"]
                    summary.bytes_stored   += stats["bytes_stored"]
                    print(f"  ✓  {fc.path}")
                except Exception as e:
                    print(f"  ✗  {fc.path}  →  {e}")

        # Handle deletions
        for fc in to_delete:
            self._file_manifests.pop(fc.path, None)
            print(f"  –  {fc.path}  (removed from manifest)")

        # Persist everything
        self._save_manifests()
        self.store.save()
        self.predictor.record(manifest)
        detector.commit(manifest)

        # Send metadata to server if configured
        if self.meta:
            self.meta.send_run(
                watch_root = watch_root,
                manifest   = manifest,
                summary    = asdict(summary) if hasattr(summary, '__dataclass_fields__') else {},
            )

        summary.duration_s = time.perf_counter() - t0
        self._print_summary(summary)
        return summary

    # ── Pipeline ───────────────────────────────────────────────────────────────

    def _process_file(
        self, abs_path: Path, fc: FileChange
    ) -> tuple[FileManifest, dict]:
        """Run a single file through chunk → dedup → compress → save."""
        data   = abs_path.read_bytes()
        chunks = list(self.chunker.cut(data))
        stats  = {
            "chunks_total":   len(chunks),
            "chunks_skipped": 0,
            "bytes_original": len(data),
            "bytes_stored":   0,
        }
        should_compress = self.compressor.should_compress(str(abs_path))
        chunk_refs: list[ChunkRef] = []

        for i, raw_chunk in enumerate(chunks):
            sha256 = hashlib.sha256(raw_chunk).hexdigest()

            # ── Deduplication ──
            if self.store.exists(sha256):
                stats["chunks_skipped"] += 1
                chunk_refs.append(ChunkRef(
                    index=i, sha256=sha256, size=len(raw_chunk),
                    compressed=False,
                    local_path=self._chunk_local_path(sha256),
                    s3_key=self._chunk_s3_key(sha256),
                ))
                continue

            # ── Compression ──
            if should_compress:
                stored = self.compressor.compress(raw_chunk)
                compressed = len(stored) < len(raw_chunk)
            else:
                stored     = raw_chunk
                compressed = False

            stats["bytes_stored"] += len(stored)

            # ── Save ──
            local_path = self._save_local(sha256, stored)
            s3_key     = self._save_s3(sha256, stored) if self.s3 else None
            self.store.add(sha256)

            chunk_refs.append(ChunkRef(
                index=i, sha256=sha256, size=len(raw_chunk),
                compressed=compressed,
                local_path=local_path,
                s3_key=s3_key,
            ))

        file_manifest = FileManifest(
            path=fc.path,
            sha256=fc.sha256,
            size=fc.size,
            chunks=chunk_refs,
            backed_up_at=time.time(),
        )
        return file_manifest, stats

    # ── Storage helpers ────────────────────────────────────────────────────────

    def _chunk_local_path(self, sha256: str) -> str:
        return f"chunks/{sha256[:2]}/{sha256}"

    def _chunk_s3_key(self, sha256: str) -> str:
        return f"chunks/{sha256[:2]}/{sha256}"

    def _save_local(self, sha256: str, data: bytes) -> str:
        rel  = self._chunk_local_path(sha256)
        dest = self.backup_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return rel

    def _save_s3(self, sha256: str, data: bytes) -> str | None:
        try:
            key = self._chunk_s3_key(sha256)
            return self.s3.upload(key, data)
        except Exception as e:
            print(f"    S3 upload failed for {sha256[:8]}: {e}")
            return None

    # ── Manifest persistence ───────────────────────────────────────────────────

    def _load_manifests(self) -> dict[str, FileManifest]:
        if not self._manifest_path.exists():
            return {}
        raw = json.loads(self._manifest_path.read_text())
        result = {}
        for path, entry in raw.items():
            entry["chunks"] = [ChunkRef(**c) for c in entry["chunks"]]
            result[path] = FileManifest(**entry)
        return result

    def _save_manifests(self):
        data = {}
        for path, fm in self._file_manifests.items():
            d = asdict(fm)
            data[path] = d
        self._manifest_path.write_text(json.dumps(data, indent=2))

    # ── Summary ────────────────────────────────────────────────────────────────

    def _print_summary(self, s: RunSummary):
        def fmt(n):
            for unit in ("B", "KB", "MB", "GB"):
                if n < 1024: return f"{n:.1f} {unit}"
                n /= 1024
            return f"{n:.1f} TB"

        dedup_pct = (s.chunks_skipped / s.chunks_total * 100) if s.chunks_total else 0
        saved     = s.bytes_original - s.bytes_stored
        saved_pct = (saved / s.bytes_original * 100) if s.bytes_original else 0

        print(f"""
  ── Run complete ──────────────────────────────
  Files      {s.files_new} new · {s.files_modified} modified · {s.files_deleted} deleted · {s.files_unchanged} unchanged
  Chunks     {s.chunks_total} total · {s.chunks_skipped} deduped ({dedup_pct:.0f}%)
  Data       {fmt(s.bytes_original)} original → {fmt(s.bytes_stored)} stored  (saved {saved_pct:.0f}%)
  Duration   {s.duration_s:.2f}s
  ──────────────────────────────────────────────
        """)
