#!/bin/bash
# ============================================================
#  AI Backup System — Desktop Launcher
#  Double-click this file to start everything automatically
# ============================================================

# Go to project folder (same folder as this script)
cd "$(dirname "$0")"

# Kill any previous instances
pkill -f "metadata_server" 2>/dev/null
pkill -f "main.py schedule" 2>/dev/null
sleep 1

echo "============================================"
echo "   AI Backup System — Starting Up"
echo "============================================"
echo ""

# Start metadata server in background
echo "  [1/3] Starting metadata server..."
python3 main.py server:start &
SERVER_PID=$!
sleep 3

# Check server is up
if curl -s http://localhost:8000/health > /dev/null; then
    echo "  [1/3] Metadata server running ✓"
else
    echo "  [1/3] Server failed to start — check your setup"
    read -p "Press ENTER to close..."
    exit 1
fi

# Start scheduler in background
echo "  [2/3] Starting scheduler..."
python3 main.py schedule \
    --root tests \
    --index .backup/state.json \
    --dest backup_folder \
    --metadata-server http://localhost:8000 &
SCHEDULER_PID=$!
sleep 2
echo "  [2/3] Scheduler running ✓"

# Open browser
echo "  [3/3] Opening dashboard in browser..."
sleep 1
xdg-open http://localhost:8000 2>/dev/null || \
    python3 -m webbrowser http://localhost:8000

echo ""
echo "============================================"
echo "   Everything is running!"
echo "   Dashboard → http://localhost:8000"
echo "   Press Ctrl+C to stop everything"
echo "============================================"
echo ""

# Wait — keep running until Ctrl+C
trap "echo ''; echo 'Stopping...'; kill $SERVER_PID $SCHEDULER_PID 2>/dev/null; exit 0" INT TERM
wait
