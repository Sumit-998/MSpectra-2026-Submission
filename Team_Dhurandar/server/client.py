"""
server/client.py
----------------
Client that sends backup metadata to the metadata server.
Used by backup_engine.py after every successful backup run.

Works whether the server is on the same machine or another
machine on the network — just change the server_url.

Usage:
    from server.client import MetadataClient

    client = MetadataClient(server_url="https://team-dhurandar-api.onrender.com")

    # Send metadata after a backup run
    client.send_run(
        run_id     = "run_20260403_120000",
        watch_root = "test_folder",
        manifest   = manifest,       # list of FileChange
        summary    = summary_dict,
    )

    # Check if server is reachable
    client.ping()
"""

import time
import uuid
from pathlib import Path

try:
    import httpx
    _HTTPX_AVAILABLE = True
except ImportError:
    _HTTPX_AVAILABLE = False


class MetadataClient:
    """
    Sends backup metadata to the metadata server via HTTP.

    Args:
        server_url: Full URL of the metadata server.
                    Same machine : "https://team-dhurandar-api.onrender.com"
                    Network      : "http://192.168.1.x:8000"
        timeout:    Request timeout in seconds.
    """

    def __init__(
        self,
        server_url: str = "https://team-dhurandar-api.onrender.com",
        timeout:    int = 10,
    ):
        if not _HTTPX_AVAILABLE:
            raise RuntimeError("httpx not installed. Run: pip3 install httpx --break-system-packages")

        self.server_url = server_url.rstrip("/")
        self.timeout    = timeout

    # ── Public API ─────────────────────────────────────────────────────────────

    def ping(self) -> bool:
        """
        Check if the metadata server is reachable.

        Returns:
            True if server responded, False otherwise.
        """
        try:
            r = httpx.get(f"{self.server_url}/health", timeout=self.timeout)
            if r.status_code == 200:
                data = r.json()
                print(f"  Server reachable — {data['files']} files tracked, "
                      f"{data['runs']} runs recorded.")
                return True
        except Exception:
            pass
        print(f"  Server not reachable at {self.server_url}")
        return False

    def send_run(
        self,
        watch_root: str,
        manifest:   list,
        summary:    dict,
        run_id:     str | None = None,
    ) -> bool:
        """
        Send metadata for a completed backup run to the server.

        Args:
            watch_root: Directory that was backed up.
            manifest:   List of FileChange objects from change_detector.
            summary:    Run summary dict from backup_engine.
            run_id:     Optional unique run ID. Auto-generated if not provided.

        Returns:
            True if server accepted the data, False if failed (non-fatal).
        """
        if run_id is None:
            run_id = f"run_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"

        # Convert FileChange objects to the server's expected format
        files = []
        for fc in manifest:
            # Only send files that exist (skip deleted)
            if fc.sha256 is None:
                continue
            files.append({
                "path":         fc.path,
                "sha256":       fc.sha256,
                "size":         fc.size or 0,
                "mtime":        fc.mtime or time.time(),
                "backed_up_at": time.time(),
                "change_type":  fc.change_type.value,
                "compressed":   False,
                "chunk_count":  1,
            })

        payload = {
            "run_id":     run_id,
            "watch_root": str(watch_root),
            "files":      files,
            "summary":    summary,
        }

        try:
            r = httpx.post(
                f"{self.server_url}/backup",
                json=payload,
                timeout=self.timeout,
            )
            if r.status_code == 200:
                data = r.json()
                print(f"  Metadata server ✓ — "
                      f"{data['registered']} new, "
                      f"{data['updated']} updated "
                      f"(run: {run_id})")
                return True
            else:
                print(f"  Metadata server error: {r.status_code} — {r.text}")
                return False

        except Exception as e:
            # Never crash the backup if the metadata server is unreachable
            print(f"  Metadata server unreachable — skipping ({e})")
            return False

    def get_stats(self) -> dict | None:
        """Fetch overall stats from the server."""
        try:
            r = httpx.get(f"{self.server_url}/stats", timeout=self.timeout)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        return None

    def list_files(self, limit: int = 50) -> list:
        """Fetch list of backed-up files from the server."""
        try:
            r = httpx.get(
                f"{self.server_url}/files",
                params={"limit": limit},
                timeout=self.timeout,
            )
            if r.status_code == 200:
                return r.json().get("files", [])
        except Exception:
            pass
        return []
