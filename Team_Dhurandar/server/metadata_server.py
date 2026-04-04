"""
server/metadata_server.py
--------------------------
FastAPI metadata server with CORS enabled for React frontend.

ENDPOINTS:
    GET    /health              → Health check
    POST   /backup              → Register a backup run
    GET    /files               → List all backed-up files
    GET    /files/{path}        → Get metadata for a specific file
    DELETE /files/{path}        → Remove a file record
    GET    /runs                → List all backup runs
    GET    /stats               → Overall backup statistics
    POST   /scheduler/start     → Start the scheduler
    POST   /scheduler/stop      → Stop the scheduler
    GET    /scheduler/status    → Get scheduler status

HOW TO RUN:
    python3 server/metadata_server.py
    python3 server/metadata_server.py --host 0.0.0.0 --port 8000
"""

import argparse
import json
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


# ── App setup ─────────────────────────────────────────────────────────────────

app = FastAPI(
    title="AI Backup Metadata Server",
    description="Stores and serves metadata about backed-up files.",
    version="1.0.0",
)

# CORS — allows React frontend to talk to this server
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory store
DB_PATH = Path(".backup/metadata_db.json")
_db: dict = {"files": {}, "runs": []}

# Scheduler state
_scheduler        = None
_scheduler_thread = None


# ── Pydantic models ───────────────────────────────────────────────────────────

class FileMetadata(BaseModel):
    path:         str
    sha256:       str
    size:         int
    mtime:        float
    backed_up_at: float
    change_type:  str
    compressed:   bool = False
    chunk_count:  int  = 1


class BackupRunPayload(BaseModel):
    run_id:     str
    watch_root: str
    files:      list[FileMetadata]
    summary:    dict = {}


class SchedulerStartPayload(BaseModel):
    watch_root:    str
    index_path:    str  = ".backup/state.json"
    backup_dir:    str  = "backup_folder"
    interval_mins: int  = 5


# ── Persistence ───────────────────────────────────────────────────────────────

def _load_db():
    global _db
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        _db = json.loads(DB_PATH.read_text())

def _save_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    DB_PATH.write_text(json.dumps(_db, indent=2))


# ── Startup ───────────────────────────────────────────────────────────────────

@app.on_event("startup")
def startup():
    _load_db()
    print(f"\n  AI Backup Metadata Server running.")
    print(f"  Database     : {DB_PATH.resolve()}")
    print(f"  Files tracked: {len(_db['files'])}")
    print(f"  Backup runs  : {len(_db['runs'])}\n")

    # Auto-start scheduler if WATCH_ROOT env var is set (Railway deployment)
    import os
    watch_root = os.environ.get("WATCH_ROOT")
    if watch_root:
        try:
            from backup.scheduler import BackupScheduler
            global _scheduler, _scheduler_thread
            _scheduler = BackupScheduler(
                watch_root      = watch_root,
                index_path      = os.environ.get("INDEX_PATH", ".backup/state.json"),
                backup_dir      = os.environ.get("BACKUP_DIR", "backup_folder"),
                metadata_server = f"http://localhost:{os.environ.get('PORT', 8000)}",
                interval_mins   = int(os.environ.get("INTERVAL_MINS", 5)),
                use_watcher     = True,
            )
            _scheduler_thread = threading.Thread(
                target=_scheduler.start,
                kwargs={"block": True},
                daemon=True,
            )
            _scheduler_thread.start()
            print(f"  Auto-started scheduler watching: {watch_root}")
        except Exception as e:
            print(f"  Scheduler auto-start skipped: {e}")


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {
        "status":      "ok",
        "files":       len(_db["files"]),
        "runs":        len(_db["runs"]),
        "server_time": time.time(),
    }


# ── Backup endpoints ──────────────────────────────────────────────────────────

@app.post("/backup")
def register_backup(payload: BackupRunPayload):
    registered = 0
    updated    = 0
    for fm in payload.files:
        entry = {
            "path":         fm.path,
            "sha256":       fm.sha256,
            "size":         fm.size,
            "mtime":        fm.mtime,
            "backed_up_at": fm.backed_up_at,
            "change_type":  fm.change_type,
            "compressed":   fm.compressed,
            "chunk_count":  fm.chunk_count,
            "run_id":       payload.run_id,
        }
        if fm.path in _db["files"]:
            updated += 1
        else:
            registered += 1
        _db["files"][fm.path] = entry

    _db["runs"].append({
        "run_id":     payload.run_id,
        "watch_root": payload.watch_root,
        "timestamp":  time.time(),
        "file_count": len(payload.files),
        "summary":    payload.summary,
    })
    _save_db()
    return {"status": "ok", "run_id": payload.run_id, "registered": registered, "updated": updated}


@app.get("/files")
def list_files(limit: int = 100, offset: int = 0):
    files = sorted(_db["files"].values(), key=lambda f: f.get("backed_up_at", 0), reverse=True)
    return {"total": len(files), "offset": offset, "limit": limit, "files": files[offset:offset+limit]}


@app.get("/files/{file_path:path}")
def get_file(file_path: str):
    if file_path not in _db["files"]:
        raise HTTPException(status_code=404, detail=f"'{file_path}' not found.")
    return _db["files"][file_path]


@app.delete("/files/{file_path:path}")
def delete_file(file_path: str):
    if file_path not in _db["files"]:
        raise HTTPException(status_code=404, detail=f"'{file_path}' not found.")
    del _db["files"][file_path]
    _save_db()
    return {"status": "deleted", "path": file_path}


@app.get("/runs")
def list_runs(limit: int = 20):
    runs = sorted(_db["runs"], key=lambda r: r.get("timestamp", 0), reverse=True)
    return {"total": len(runs), "runs": runs[:limit]}


@app.get("/stats")
def stats():
    files      = list(_db["files"].values())
    total_size = sum(f.get("size", 0) for f in files)
    runs       = _db["runs"]
    return {
        "total_files":      len(files),
        "total_runs":       len(runs),
        "total_size_bytes": total_size,
        "total_size_mb":    round(total_size / (1024 * 1024), 2),
        "last_backup":      runs[-1]["timestamp"] if runs else None,
        "change_breakdown": {
            "new":      sum(1 for f in files if f.get("change_type") == "new"),
            "modified": sum(1 for f in files if f.get("change_type") == "modified"),
        },
    }


# ── Scheduler endpoints ───────────────────────────────────────────────────────

@app.post("/scheduler/start")
def scheduler_start(payload: SchedulerStartPayload):
    global _scheduler, _scheduler_thread

    if _scheduler is not None and not _scheduler._stop_event.is_set():
        return {"status": "already_running", "message": "Scheduler is already running."}

    try:
        from backup.scheduler import BackupScheduler
        _scheduler = BackupScheduler(
            watch_root    = payload.watch_root,
            index_path    = payload.index_path,
            backup_dir    = payload.backup_dir,
            metadata_server = "http://localhost:8000",
            interval_mins = payload.interval_mins,
            use_watcher   = True,
        )
        _scheduler_thread = threading.Thread(
            target = _scheduler.start,
            kwargs = {"block": True},
            daemon = True,
        )
        _scheduler_thread.start()
        return {"status": "started", "watch_root": payload.watch_root, "interval_mins": payload.interval_mins}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/scheduler/stop")
def scheduler_stop():
    global _scheduler
    if _scheduler is None or _scheduler._stop_event.is_set():
        return {"status": "not_running", "message": "Scheduler is not running."}
    _scheduler.stop()
    return {"status": "stopped"}


@app.get("/scheduler/status")
def scheduler_status():
    global _scheduler
    if _scheduler is None or _scheduler._stop_event.is_set():
        return {"running": False}
    s = _scheduler.status()
    s["running"] = True
    return s


# ── AI Prioritization endpoint ────────────────────────────────────────────────

class PrioritizePayload(BaseModel):
    api_key: str


@app.post("/ai/prioritize")
def ai_prioritize(payload: PrioritizePayload):
    """
    Uses Groq LLM to rank all backed-up files by priority.
    Returns files sorted from highest to lowest priority.
    """
    files = list(_db["files"].values())
    if not files:
        return {"files": [], "message": "No files to prioritize"}

    try:
        from server.groq_prioritizer import prioritize_files_with_groq
        ranked = prioritize_files_with_groq(files, payload.api_key)
        return {"files": ranked, "total": len(ranked)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/")
def frontend():
    """Serve the React frontend."""
    frontend_path = Path("frontend/index.html")
    if frontend_path.exists():
        return FileResponse(frontend_path)
    return {"message": "Frontend not found. Place frontend/index.html in your project root."}


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AI Backup Metadata Server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)
