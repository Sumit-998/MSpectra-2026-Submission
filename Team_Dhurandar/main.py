"""
main.py  —  AI Backup System entry point

Commands:
    python3 main.py scan      --root tests --index .backup/state.json
    python3 main.py backup    --root tests --index .backup/state.json --dest backup_folder
    python3 main.py predict
    python3 main.py server:start
    python3 main.py server:status
    python3 main.py server:files
    python3 main.py restore:list
    python3 main.py restore:file --file sample.txt
    python3 main.py restore:all
    python3 main.py schedule  --root tests --index .backup/state.json
"""

import argparse
import os
import time
from pathlib import Path

from backup.change_detector import ChangeDetector, ChangeType
from backup.backup_engine import BackupEngine
from backup.ai_predictor import AIPredictor
from backup.restore import RestoreEngine
from backup.scheduler import BackupScheduler
from server.client import MetadataClient


# ── Formatting helpers ────────────────────────────────────────────────────────

COLORS = {
    ChangeType.NEW:       "\033[32m",
    ChangeType.MODIFIED:  "\033[33m",
    ChangeType.DELETED:   "\033[31m",
    ChangeType.UNCHANGED: "\033[90m",
}
ICONS = {
    ChangeType.NEW:       "+ new      ",
    ChangeType.MODIFIED:  "~ modified ",
    ChangeType.DELETED:   "- deleted  ",
    ChangeType.UNCHANGED: "  unchanged",
}
RESET = "\033[0m"


def fmt_bytes(n):
    if n is None: return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024: return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ── scan command ──────────────────────────────────────────────────────────────

def cmd_scan(args):
    detector = ChangeDetector(watch_root=args.root, index_path=args.index)
    t0 = time.perf_counter()
    manifest = detector.scan(full=args.full)
    elapsed = time.perf_counter() - t0

    if args.json:
        import json
        from dataclasses import asdict
        print(json.dumps([asdict(c) for c in manifest], indent=2))
        return

    for fc in manifest:
        if fc.change_type.value == "unchanged":
            continue
        color = COLORS[fc.change_type]
        icon  = ICONS[fc.change_type]
        size  = fmt_bytes(fc.size)
        extra = f"  (was {fc.prev_sha256[:8]}…)" if fc.prev_sha256 else ""
        print(f"{color}{icon}  {fc.path:<60} {size:>10}{extra}{RESET}")

    print()
    s = detector.summary(manifest)
    print(f"  Scanned in {elapsed:.2f}s")
    print(f"  New: {s['new']}  Modified: {s['modified']}  Deleted: {s['deleted']}  Unchanged: {s['unchanged']}")
    print(f"  Data to transfer: {fmt_bytes(s['bytes_to_backup'])}")

    if args.commit:
        detector.commit(manifest)
        print(f"\n  State index updated → {args.index}")


# ── backup command ────────────────────────────────────────────────────────────

def cmd_backup(args):
    engine = BackupEngine(
        backup_dir      = args.dest,
        s3_bucket       = args.s3_bucket,
        s3_prefix       = args.s3_prefix,
        metadata_server = args.metadata_server,
        workers         = args.workers,
    )
    engine.run(watch_root=args.root, index_path=args.index)


# ── server commands ───────────────────────────────────────────────────────────

def cmd_server_start(args):
    import uvicorn
    from server.metadata_server import app
    print(f"\n  Starting metadata server on {args.host}:{args.port}")
    print(f"  API docs  → http://{args.host}:{args.port}/docs")
    print(f"  Dashboard → http://localhost:{args.port}\n")
    uvicorn.run(app, host=args.host, port=args.port)


def cmd_server_status(args):
    client = MetadataClient(server_url=args.url)
    if not client.ping():
        return
    stats = client.get_stats()
    if stats:
        print(f"\n  Files tracked : {stats['total_files']}")
        print(f"  Backup runs   : {stats['total_runs']}")
        print(f"  Total size    : {stats['total_size_mb']} MB")
        if stats["last_backup"]:
            last = time.strftime("%Y-%m-%d %H:%M", time.localtime(stats["last_backup"]))
            print(f"  Last backup   : {last}")
        print()


def cmd_server_files(args):
    client = MetadataClient(server_url=args.url)
    files  = client.list_files(limit=args.limit)
    if not files:
        print("  No files found on server.")
        return
    print(f"\n  {'File':<45} {'Size':>10}  {'Type':<10}  Backed up")
    print("  " + "-" * 80)
    for f in files:
        backed_up = time.strftime("%Y-%m-%d %H:%M", time.localtime(f.get("backed_up_at", 0)))
        print(f"  {f['path']:<45} {fmt_bytes(f['size']):>10}  "
              f"{f['change_type']:<10}  {backed_up}")
    print()


# ── predict command ───────────────────────────────────────────────────────────

def cmd_predict(args):
    predictor = AIPredictor(history_path=args.history)
    top = predictor.top_files(n=args.top)

    if not top:
        print("  No history yet — run a few backups first.")
        return

    print(f"\n  Top {len(top)} files most likely to change next:\n")
    print(f"  {'#':<4} {'Bar':<22} {'Score':>5}  {'Label':<8}  File")
    print("  " + "-" * 65)
    for i, (path, label, score) in enumerate(top, 1):
        bar   = "█" * int(score * 20) + "░" * (20 - int(score * 20))
        color = "\033[32m" if label == "high" else "\033[33m" if label == "medium" else "\033[90m"
        reset = "\033[0m"
        print(f"  {i:>2}.  {bar}  {score:.2f}  {color}{label.upper():<8}{reset}  {path}")
    print()


# ── restore commands ──────────────────────────────────────────────────────────

def cmd_restore_list(args):
    engine = RestoreEngine(backup_dir=args.dest)
    files  = engine.list_files()

    if not files:
        print("  No files in backup yet.")
        return

    print(f"\n  {'#':<4} {'File':<45} {'Size':>10}  Backed up")
    print("  " + "-" * 75)
    for i, f in enumerate(files, 1):
        backed_up = time.strftime("%Y-%m-%d %H:%M", time.localtime(f["backed_up_at"]))
        print(f"  {i:<4} {f['path']:<45} {fmt_bytes(f['size']):>10}  {backed_up}")
    print()


def cmd_restore_file(args):
    engine = RestoreEngine(backup_dir=args.dest)
    result = engine.restore_file(
        file_path  = args.file,
        output_dir = args.output,
    )
    if result.success:
        print(f"\n  ✓  Restored '{args.file}' → {result.output}")
        print(f"     Size: {fmt_bytes(result.size_bytes)}  Time: {result.duration_s}s\n")
    else:
        print(f"\n  ✗  Failed: {result.error}\n")


def cmd_restore_all(args):
    engine = RestoreEngine(backup_dir=args.dest)
    engine.restore_all(output_dir=args.output)


# ── schedule command ──────────────────────────────────────────────────────────

def cmd_schedule(args):
    scheduler = BackupScheduler(
        watch_root      = args.root,
        index_path      = args.index,
        backup_dir      = args.dest,
        metadata_server = args.metadata_server,
        interval_mins   = args.interval,
        use_watcher     = not args.no_watcher,
    )
    scheduler.start(block=True)


# ── CLI setup ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="AI Backup System")
    sub = parser.add_subparsers(dest="command", required=True)

    # ── scan ──
    p_scan = sub.add_parser("scan", help="Detect and show changes (no backup)")
    p_scan.add_argument("--root",   required=True)
    p_scan.add_argument("--index",  required=True)
    p_scan.add_argument("--full",   action="store_true")
    p_scan.add_argument("--json",   action="store_true")
    p_scan.add_argument("--commit", action="store_true")
    p_scan.set_defaults(func=cmd_scan)

    # ── backup ──
    p_back = sub.add_parser("backup", help="Run full backup pipeline")
    p_back.add_argument("--root",            required=True)
    p_back.add_argument("--index",           required=True)
    p_back.add_argument("--dest",            default="backup_folder")
    p_back.add_argument("--s3-bucket",       default=None)
    p_back.add_argument("--s3-prefix",       default="backups/")
    p_back.add_argument("--metadata-server", default=None)
    p_back.add_argument("--workers",         type=int, default=4)
    p_back.set_defaults(func=cmd_backup)

    # ── server start ──
    p_srv = sub.add_parser("server:start", help="Start the metadata server")
    p_srv.add_argument("--host", default="0.0.0.0")
    p_srv.add_argument("--port", type=int, default=8000)
    p_srv.set_defaults(func=cmd_server_start)

    # ── server status ──
    p_ss = sub.add_parser("server:status", help="Check metadata server status")
    p_ss.add_argument("--url", default="http://localhost:8000")
    p_ss.set_defaults(func=cmd_server_status)

    # ── server files ──
    p_sf = sub.add_parser("server:files", help="List files tracked by metadata server")
    p_sf.add_argument("--url",   default="http://localhost:8000")
    p_sf.add_argument("--limit", type=int, default=50)
    p_sf.set_defaults(func=cmd_server_files)

    # ── predict ──
    p_pred = sub.add_parser("predict", help="Show files most likely to change next")
    p_pred.add_argument("--history", default=".backup/predictor.json")
    p_pred.add_argument("--top",     type=int, default=10)
    p_pred.set_defaults(func=cmd_predict)

    # ── restore list ──
    p_rlist = sub.add_parser("restore:list", help="List all files available to restore")
    p_rlist.add_argument("--dest", default="backup_folder")
    p_rlist.set_defaults(func=cmd_restore_list)

    # ── restore file ──
    p_rfile = sub.add_parser("restore:file", help="Restore a single file")
    p_rfile.add_argument("--file",   required=True)
    p_rfile.add_argument("--dest",   default="backup_folder")
    p_rfile.add_argument("--output", default="restored")
    p_rfile.set_defaults(func=cmd_restore_file)

    # ── restore all ──
    p_rall = sub.add_parser("restore:all", help="Restore all files from backup")
    p_rall.add_argument("--dest",   default="backup_folder")
    p_rall.add_argument("--output", default="restored")
    p_rall.set_defaults(func=cmd_restore_all)

    # ── schedule ──
    p_sch = sub.add_parser("schedule", help="Run backups automatically in the background")
    p_sch.add_argument("--root",            required=True)
    p_sch.add_argument("--index",           required=True)
    p_sch.add_argument("--dest",            default="backup_folder")
    p_sch.add_argument("--metadata-server", default=None)
    p_sch.add_argument("--interval",        type=int, default=5)
    p_sch.add_argument("--no-watcher",      action="store_true")
    p_sch.set_defaults(func=cmd_schedule)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
