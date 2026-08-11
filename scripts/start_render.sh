#!/usr/bin/env bash
# Render start command: run the trading bot and the Streamlit dashboard in
# one service (they share the SQLite database on the mounted disk).
#
# SIGTERM (deploys, restarts, scale-downs) is forwarded to the bot so its
# graceful-shutdown hooks flush all state to SQLite before the container
# stops. If either process dies, the script exits non-zero and Render
# restarts the whole service.
set -euo pipefail

# Ensure the persistent-disk directories exist (DB_PATH/LOG_FILE point here).
mkdir -p "$(dirname "${DB_PATH:-data/bot_state.db}")"
mkdir -p "$(dirname "${LOG_FILE:-logs/bot.log}")"

python main.py &
BOT_PID=$!

streamlit run dashboard/app.py \
  --server.port "${PORT:-8501}" \
  --server.address 0.0.0.0 \
  --server.headless true &
WEB_PID=$!

shutdown() {
  # Bot first so it can flush state; then the dashboard.
  kill -TERM "$BOT_PID" 2>/dev/null || true
  wait "$BOT_PID" 2>/dev/null || true
  kill -TERM "$WEB_PID" 2>/dev/null || true
  wait "$WEB_PID" 2>/dev/null || true
}
trap shutdown TERM INT

# Exit as soon as either process stops, then clean up the other.
wait -n "$BOT_PID" "$WEB_PID"
STATUS=$?
shutdown
exit "$STATUS"
