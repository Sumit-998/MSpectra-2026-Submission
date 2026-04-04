"""
scheduler.py
------------
Runs backups automatically in the background.

Two modes:
  1. Interval mode  — backs up every X minutes
  2. Watcher mode   — watches for file changes and backs up immediately

Usage:
    from backup.scheduler import BackupScheduler

    scheduler = BackupScheduler(
        watch_root      = "test_folder",
        index_path      = ".backup/state.json",
        backup_dir      = "backup_folder",
        metadata_server = "https://team-dhurandar-api.onrender.com",
        interval_mins   = 5,
    )
    scheduler.start()   # runs forever in background
    scheduler.stop()    # graceful shutdown
"""

import os
import sys
import time
import threading
import logging
from pathlib import Path
from datetime import datetime

from backup.backup_engine import BackupEngine


# ── Logging setup ─────────────────────────────────────────────────────────────

logging.basicConfig(
    level  = logging.INFO,
    format = "  %(asctime)s  %(levelname)s  %(message)s",
    datefmt= "%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("scheduler")


# ── Scheduler ─────────────────────────────────────────────────────────────────

class BackupScheduler:
    """
    Automatically runs backups on a schedule or when files change.

    Args:
        watch_root:      Directory to monitor and back up.
        index_path:      State index path for change detection.
        backup_dir:      Local backup destination folder.
        metadata_server: Metadata server URL (optional).
        interval_mins:   How often to run a backup (in minutes).
        use_watcher:     If True, also watch for file changes in real time.
    """

    def __init__(
        self,
        watch_root:      str | Path,
        index_path:      str | Path = ".backup/state.json",
        backup_dir:      str | Path = "backup_folder",
        metadata_server: str | None = None,
        interval_mins:   int        = 5,
        use_watcher:     bool       = True,
    ):
        self.watch_root      = Path(watch_root).expanduser().resolve()
        self.index_path      = index_path
        self.backup_dir      = backup_dir
        self.metadata_server = metadata_server
        self.interval_secs   = interval_mins * 60
        self.use_watcher     = use_watcher

        self._engine         = BackupEngine(
            backup_dir      = backup_dir,
            metadata_server = metadata_server,
        )
        self._stop_event     = threading.Event()
        self._backup_lock    = threading.Lock()   # prevent overlapping runs
        self._threads:list   = []

        # Stats
        self.runs_total      = 0
        self.runs_successful = 0
        self.last_run_at     = None
        self.next_run_at     = None

    # ── Public API ─────────────────────────────────────────────────────────────

    def start(self, block: bool = True):
        """
        Start the scheduler.

        Args:
            block: If True, runs forever (use for terminal).
                   If False, runs in background thread (use in scripts).
        """
        log.info(f"Scheduler starting — watching '{self.watch_root}'")
        log.info(f"Interval: every {self.interval_secs // 60} minute(s)")
        if self.metadata_server:
            log.info(f"Metadata server: {self.metadata_server}")

        # Run an immediate backup on startup
        self._run_backup(reason="startup")

        # Sync all known files to metadata server on startup
        if self._engine.meta:
            self._sync_all_to_server()

        # Start interval timer thread
        interval_thread = threading.Thread(
            target=self._interval_loop,
            name="interval-backup",
            daemon=True,
        )
        interval_thread.start()
        self._threads.append(interval_thread)

        # Start file watcher thread if enabled
        if self.use_watcher:
            watcher_thread = threading.Thread(
                target=self._watcher_loop,
                name="file-watcher",
                daemon=True,
            )
            watcher_thread.start()
            self._threads.append(watcher_thread)

        if block:
            log.info("Scheduler running. Press Ctrl+C to stop.\n")
            try:
                while not self._stop_event.is_set():
                    time.sleep(1)
            except KeyboardInterrupt:
                self.stop()

    def stop(self):
        """Gracefully stop the scheduler."""
        log.info("Stopping scheduler...")
        self._stop_event.set()
        for t in self._threads:
            t.join(timeout=5)
        log.info(
            f"Scheduler stopped. "
            f"Ran {self.runs_total} backups, "
            f"{self.runs_successful} successful."
        )

    def status(self) -> dict:
        """Return current scheduler status."""
        return {
            "watching":       str(self.watch_root),
            "interval_mins":  self.interval_secs // 60,
            "runs_total":     self.runs_total,
            "runs_successful":self.runs_successful,
            "last_run_at":    self.last_run_at,
            "next_run_at":    self.next_run_at,
        }

    # ── Internal loops ─────────────────────────────────────────────────────────

    def _interval_loop(self):
        """Fires a backup every interval_secs."""
        while not self._stop_event.is_set():
            self.next_run_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            # Wait interval, checking stop every second
            for _ in range(self.interval_secs):
                if self._stop_event.is_set():
                    return
                time.sleep(1)
            if not self._stop_event.is_set():
                self._run_backup(reason="interval")

    def _watcher_loop(self):
        """
        Watches the directory for changes using mtime polling.
        Triggers a backup immediately when a change is detected.
        Debounced by 10 seconds to avoid rapid-fire backups.
        """
        log.info("File watcher active — polling for changes every 10s")
        snapshot    = self._take_snapshot()
        debounce_s  = 10
        last_change = 0.0

        while not self._stop_event.is_set():
            time.sleep(10)
            if self._stop_event.is_set():
                break

            new_snapshot = self._take_snapshot()
            if new_snapshot != snapshot:
                snapshot = new_snapshot
                now = time.time()

                # Debounce — don't fire if a backup ran very recently
                if now - last_change > debounce_s:
                    last_change = now
                    log.info("File change detected — triggering backup")
                    self._run_backup(reason="file-change")

    def _take_snapshot(self) -> dict[str, float]:
        """
        Take a lightweight snapshot of the watch directory.
        Returns {relative_path: mtime} for quick change detection.
        """
        snapshot = {}
        try:
            for dirpath, dirnames, filenames in os.walk(self.watch_root):
                # Skip hidden and noise dirs
                dirnames[:] = [
                    d for d in dirnames
                    if not d.startswith(".") and d not in {"__pycache__", "node_modules"}
                ]
                for fname in filenames:
                    abs_path = Path(dirpath) / fname
                    try:
                        rel  = str(abs_path.relative_to(self.watch_root))
                        snapshot[rel] = abs_path.stat().st_mtime
                    except (OSError, ValueError):
                        pass
        except Exception:
            pass
        return snapshot

    def _sync_all_to_server(self):
        """
        Push all files from the state index to the metadata server.
        Called on startup so the dashboard always shows everything,
        even files backed up in previous runs.
        """
        try:
            import json
            from pathlib import Path

            index_path = Path(self.index_path)
            if not index_path.exists():
                return

            db = json.loads(index_path.read_text())
            if not db:
                return

            import time
            files = []
            for path, record in db.items():
                files.append({
                    "path":         path,
                    "sha256":       record.get("sha256", ""),
                    "size":         record.get("size", 0),
                    "mtime":        record.get("mtime", 0),
                    "backed_up_at": record.get("mtime", time.time()),
                    "change_type":  "unchanged",
                    "compressed":   False,
                    "chunk_count":  1,
                })

            if files:
                self._engine.meta.send_run(
                    watch_root = str(self.watch_root),
                    manifest   = [],   # empty — we send raw files directly
                    summary    = {},
                    run_id     = f"startup_sync_{int(time.time())}",
                )
                # Send directly via httpx
                import httpx
                payload = {
                    "run_id":     f"startup_sync_{int(time.time())}",
                    "watch_root": str(self.watch_root),
                    "files":      files,
                    "summary":    {},
                }
                r = httpx.post(
                    f"{self._engine.meta.server_url}/backup",
                    json=payload,
                    timeout=10,
                )
                if r.status_code == 200:
                    log.info(f"Startup sync: pushed {len(files)} files to metadata server")
                else:
                    log.warning(f"Startup sync failed: {r.status_code}")
        except Exception as e:
            log.warning(f"Startup sync skipped: {e}")

    def _run_backup(self, reason: str = "scheduled"):
        """Run one backup cycle. Thread-safe via lock."""
        if not self._backup_lock.acquire(blocking=False):
            log.info("Backup already running — skipping this trigger")
            return

        try:
            self.runs_total  += 1
            self.last_run_at  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            log.info(f"Backup #{self.runs_total} starting ({reason})")

            self._engine.run(
                watch_root = self.watch_root,
                index_path = self.index_path,
            )

            self.runs_successful += 1
            log.info(f"Backup #{self.runs_total} complete")

        except Exception as e:
            log.error(f"Backup #{self.runs_total} failed: {e}")
        finally:
            self._backup_lock.release()
