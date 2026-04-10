#!/bin/bash
# Host Resource AI Agent - Run Script
set -e
cd "$(dirname "$0")"
export PYTHONPATH="$(pwd):$PYTHONPATH"

case "${1:-app}" in
  seed)
    echo "🌱 Seeding demo data..."
    python3 scripts/seed_demo.py
    ;;
  app)
    echo "🚀 Starting API + UI on port ${APP_PORT:-8082}..."
    echo "   UI:  http://localhost:${APP_PORT:-8082}"
    echo "   API: http://localhost:${APP_PORT:-8082}/docs"
    uvicorn app.main:app --host 0.0.0.0 --port "${APP_PORT:-8082}" --reload
    ;;
  worker)
    echo "⚙️  Starting worker..."
    python3 app/workers/run_worker.py
    ;;
  all)
    echo "🚀 Starting app + worker..."
    echo ""

    # Start app
    uvicorn app.main:app --host 0.0.0.0 --port "${APP_PORT:-8082}" --reload 2>&1 &
    APP_PID=$!
    echo "   [APP]    PID=$APP_PID  http://localhost:${APP_PORT:-8082}"

    sleep 3

    # Start worker
    python3 app/workers/run_worker.py 2>&1 &
    WORKER_PID=$!
    echo "   [WORKER] PID=$WORKER_PID"
    echo ""
    echo "   Both processes running. Press Ctrl+C to stop."
    echo ""

    trap "echo 'Stopping...'; kill $APP_PID $WORKER_PID 2>/dev/null; wait; echo 'Done.'; exit 0" INT TERM
    wait
    ;;
  test)
    echo "🧪 Running tests..."
    python3 -m pytest tests/ -v
    ;;
  *)
    echo "Usage: $0 {app|worker|all|seed|test}"
    echo ""
    echo "  app    — Start API + UI server only"
    echo "  worker — Start background worker only"
    echo "  all    — Start app + worker together"
    echo "  seed   — Seed demo data into DB"
    echo "  test   — Run pytest"
    exit 1
    ;;
esac
