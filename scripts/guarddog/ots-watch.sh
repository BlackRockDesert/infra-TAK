#!/bin/bash
# OpenTAKServer Guard Dog health check
# Checks OTS container health and restarts if unhealthy

SERVER_IDENTIFIER=$(cat /opt/tak-guarddog/server_identifier 2>/dev/null || echo "$(hostname)")
STATE_DIR="/var/lib/takguard"
FAIL_FILE="$STATE_DIR/ots.failcount"
COOLDOWN_FILE="$STATE_DIR/ots_last_restart"
REASON_FILE="$STATE_DIR/restart_reason"
LAST_RESTART_FILE="$STATE_DIR/ots_last_restart_time"
RESTART_LOCK="$STATE_DIR/restart.lock"

CONTAINER="opentakserver"
MAX_FAILS=3
COOLDOWN_SECS=900
MIN_UPTIME_SECS=900

mkdir -p "$STATE_DIR"

# Don't run during first 15 minutes after boot
UPTIME_SECS=$(awk '{print int($1)}' /proc/uptime)
if [ "$UPTIME_SECS" -lt "$MIN_UPTIME_SECS" ]; then
  exit 0
fi

# Check if we're in grace period (15 minutes after any restart)
if [ -f "$LAST_RESTART_FILE" ]; then
  LAST_RESTART=$(cat "$LAST_RESTART_FILE")
  CURRENT_TIME=$(date +%s)
  TIME_SINCE_RESTART=$((CURRENT_TIME - LAST_RESTART))
  if [ $TIME_SINCE_RESTART -lt 900 ]; then
    exit 0
  fi
fi

# Only run if OTS container exists
docker inspect "$CONTAINER" >/dev/null 2>&1 || exit 0

# Check if another monitor is already restarting
if [ -f "$RESTART_LOCK" ]; then
  exit 0
fi

# ── Health check ──
# Check 1: Container is running
CONTAINER_OK=false
if docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q true; then
  CONTAINER_OK=true
fi

# Check 2: API is responding
API_OK=false
if curl -sf --max-time 5 http://127.0.0.1:8081/api/ots/health >/dev/null 2>&1; then
  API_OK=true
fi

# Healthy: container running and API responding
if $CONTAINER_OK && $API_OK; then
  echo 0 > "$FAIL_FILE"
  exit 0
fi

# Increment fail counter
FAILS=0
[ -f "$FAIL_FILE" ] && FAILS=$(cat "$FAIL_FILE")
FAILS=$((FAILS+1))
echo "$FAILS" > "$FAIL_FILE"

# Need consecutive failures
if [ "$FAILS" -lt "$MAX_FAILS" ]; then
  exit 0
fi

# Check cooldown period (15 minutes between restarts)
NOW=$(date +%s)
LAST=0
[ -f "$COOLDOWN_FILE" ] && LAST=$(cat "$COOLDOWN_FILE")
if [ $((NOW - LAST)) -lt "$COOLDOWN_SECS" ]; then
  exit 0
fi

# Daily restart cap
DAILY_COUNT_FILE="$STATE_DIR/ots_restart_count_24h"
DAILY_WINDOW_FILE="$STATE_DIR/ots_restart_window"
MAX_DAILY_RESTARTS=3
_window_start=$(cat "$DAILY_WINDOW_FILE" 2>/dev/null || echo 0)
if [ $((NOW - _window_start)) -ge 86400 ]; then
  echo "$NOW" > "$DAILY_WINDOW_FILE"
  echo 0 > "$DAILY_COUNT_FILE"
fi
_daily=$(cat "$DAILY_COUNT_FILE" 2>/dev/null || echo 0)
if [ "$_daily" -ge "$MAX_DAILY_RESTARTS" ]; then
  TS="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  mkdir -p /var/log/takguard
  echo "$TS | SKIP | OTS unhealthy but daily restart cap ($MAX_DAILY_RESTARTS) reached — manual intervention required" >> /var/log/takguard/ots-restarts.log
  exit 0
fi

# Log and alert
logger -t takguard "OTS unhealthy for $FAILS checks; restarting opentakserver"

echo "$NOW" > "$COOLDOWN_FILE"
echo 0 > "$FAIL_FILE"
echo "guard dog_ots" > "$REASON_FILE"

# Detailed logging
LOGDIR="/var/log/takguard"
LOGFILE="$LOGDIR/ots-restarts.log"
mkdir -p "$LOGDIR"

TS="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
LOAD="$(cut -d' ' -f1-3 /proc/loadavg)"
MEMFREE="$(free -h | awk '/Mem:/ {print $4}')"

DETAIL="container=$CONTAINER_OK api=$API_OK"
echo "$TS | restart | OTS unhealthy | $DETAIL | load=$LOAD | mem_free=$MEMFREE" >> "$LOGFILE"

# Send alerts
SUBJ="OTS Guard Dog Restart on $SERVER_IDENTIFIER"
BODY="OpenTAKServer was automatically restarted by the guard dog.

Server: $SERVER_IDENTIFIER
Reason: OTS container unhealthy for $FAILS consecutive checks.
Time (UTC): $TS

System State:
- Load: $LOAD
- Free Memory: $MEMFREE
- Container running: $CONTAINER_OK
- API responding: $API_OK

This usually indicates:
- OTS container crashed or stopped
- API process stuck or unresponsive
- RabbitMQ connection issue

Check /var/log/takguard/ots-restarts.log for history.
"

echo -e "$BODY" | /opt/tak-guarddog/send-alert-email.sh "$SUBJ" "ALERT_EMAIL_PLACEHOLDER" 2>/dev/null || true

# Create restart lock
touch "$RESTART_LOCK"

# Record restart time for grace period
date +%s > "$LAST_RESTART_FILE"

# Increment daily restart counter
echo $((_daily + 1)) > "$DAILY_COUNT_FILE"

# Restart the container
docker compose -f /root/opentakserver/docker-compose.yml restart opentakserver

# Wait 30 seconds then remove lock
sleep 30
rm -f "$RESTART_LOCK"
