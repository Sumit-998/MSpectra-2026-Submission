"""
restore.py
----------
Restores files from the backup by reading the manifest and
reassembling chunks back into the original files.

Features:
  - Restore a single file
  - Restore an entire folder (full restore)
  - Restore to a custom output location
  - Handles compressed chunks automatically

Usage:
    from backup.restore import RestoreEngine

    engine = RestoreEngine(backup_dir="backup_folder")

    # Restore one file
    engine.restore_file("notes.txt", output_dir="restored/")

    # Restore everything
    engine.restore_all(output_dir="restored/")
"""

import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
import hashlib

import zstandard as zstd


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class RestoreResult:
    path:       str
    success:    bool
    output:     str | None   # where the file was written
    error:      str | None   # error message if failed
    size_bytes: int = 0
    duration_s: float = 0.0


# ── Restore Engine ────────────────────────────────────────────────────────────

class RestoreEngine:
    """
    Reads backup_folder/.manifest.json and reassembles files from chunks.

    Args:
        backup_dir: Your local backup_folder/ path.
    """

    def __init__(self, backup_dir: str | Path = "backup_folder"):
        self.backup_dir = Path(backup_dir)
        self._dctx      = zstd.ZstdDecompressor()
        self._manifest  = self._load_manifest()

    # ── Public API ─────────────────────────────────────────────────────────────

    def list_files(self) -> list[dict]:
        """
        List all files available to restore.

        Returns:
            List of dicts with path, size, backed_up_at, chunk_count.
        """
        results = []
        for path, fm in self._manifest.items():
            results.append({
                "path":         path,
                "size":         fm.get("size", 0),
                "backed_up_at": fm.get("backed_up_at", 0),
                "chunks":       len(fm.get("chunks", [])),
            })
        results.sort(key=lambda x: x["backed_up_at"], reverse=True)
        return results

    def restore_file(
        self,
        file_path: str,
        output_dir: str | Path = "restored",
    ) -> RestoreResult:
        """
        Restore a single file by its relative path.

        Args:
            file_path:  Relative path as it appears in the manifest
                        e.g. "notes.txt" or "sub/config.yaml"
            output_dir: Where to write the restored file.

        Returns:
            RestoreResult with success status and output path.
        """
        t0 = time.perf_counter()

        if file_path not in self._manifest:
            return RestoreResult(
                path=file_path, success=False, output=None,
                error=f"'{file_path}' not found in backup manifest.",
            )

        fm          = self._manifest[file_path]
        chunks_meta = fm.get("chunks", [])

        # Sort chunks by index to reassemble in order
        chunks_meta.sort(key=lambda c: c["index"])

        try:
            reassembled = self._reassemble_chunks(chunks_meta)
        except Exception as e:
            return RestoreResult(
                path=file_path, success=False, output=None,
                error=f"Failed to reassemble chunks: {e}",
            )

        # Write to output location
        out_path = Path(output_dir) / file_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(reassembled)

        return RestoreResult(
            path=file_path,
            success=True,
            output=str(out_path),
            error=None,
            size_bytes=len(reassembled),
            duration_s=round(time.perf_counter() - t0, 3),
        )

    def restore_all(
        self,
        output_dir: str | Path = "restored",
    ) -> list[RestoreResult]:
        """
        Restore every file in the manifest.

        Args:
            output_dir: Root folder to restore everything into.

        Returns:
            List of RestoreResult, one per file.
        """
        results = []
        total   = len(self._manifest)

        if total == 0:
            print("  No files in manifest to restore.")
            return results

        print(f"\n  Restoring {total} file(s) to '{output_dir}/'...\n")

        for i, file_path in enumerate(self._manifest, 1):
            result = self.restore_file(file_path, output_dir)
            results.append(result)

            if result.success:
                print(f"  ✓  [{i}/{total}]  {file_path}  →  {result.output}")
            else:
                print(f"  ✗  [{i}/{total}]  {file_path}  →  {result.error}")

        # Summary
        success = sum(1 for r in results if r.success)
        failed  = len(results) - success
        total_bytes = sum(r.size_bytes for r in results if r.success)

        print(f"""
  ── Restore complete ──────────────────────────
  Restored : {success} files
  Failed   : {failed} files
  Total    : {_fmt_bytes(total_bytes)}
  Location : {output_dir}/
  ──────────────────────────────────────────────
        """)

        return results

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _reassemble_chunks(self, chunks_meta: list[dict]) -> bytes:
        """Read each chunk file, decompress if needed, concatenate."""
        parts = []

        for chunk in chunks_meta:
            local_path = chunk.get("local_path")
            compressed = chunk.get("compressed", False)

            if not local_path:
                raise FileNotFoundError(f"No local path for chunk {chunk.get('sha256', '?')[:8]}")

            chunk_file = self.backup_dir / local_path
            if not chunk_file.exists():
                raise FileNotFoundError(f"Chunk file missing: {chunk_file}")

            raw = chunk_file.read_bytes()

            if compressed:
                raw = self._dctx.decompress(raw)

            parts.append(raw)

        return b"".join(parts)
        


    def _load_manifest(self) -> dict:
        manifest_path = self.backup_dir / ".manifest.json"
        if not manifest_path.exists():
            return {}
        return json.loads(manifest_path.read_text())


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"
