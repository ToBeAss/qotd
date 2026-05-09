#!/bin/bash
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$DIR/.venv/bin/python3"

LOG_DIR="$DIR/logs"
mkdir -p "$LOG_DIR"
SCHED_LOG="$LOG_DIR/scheduler.log"

log() {
    echo "$(date -Iseconds) [scheduler] $*" >> "$SCHED_LOG"
}

# Load .env so HEALTHCHECK_URL (and anything else) is available to this shell.
set -a
[ -f "$DIR/.env" ] && . "$DIR/.env"
set +a

ping_hc() {
    # Best-effort heartbeat. Never abort the script if curl fails (e.g. no network).
    local suffix="$1"
    local body="${2:-}"
    if [ -z "${HEALTHCHECK_URL:-}" ]; then
        log "hc ping skipped (HEALTHCHECK_URL unset) suffix='$suffix'"
        return 0
    fi
    if [ -n "$body" ]; then
        curl -fsS -m 10 --retry 3 -o /dev/null --data-raw "$body" "${HEALTHCHECK_URL}${suffix}" \
            || log "hc ping '$suffix' failed"
    else
        curl -fsS -m 10 --retry 3 -o /dev/null "${HEALTHCHECK_URL}${suffix}" \
            || log "hc ping '$suffix' failed"
    fi
}

run_main() {
    # Run main.py, capturing exit code without letting `set -e` kill us before
    # we can ping healthchecks and log the result.
    local label="$1"
    shift
    log "running main.py $* ($label)"
    set +e
    "$PYTHON" "$DIR/main.py" "$@" >> "$SCHED_LOG" 2>&1
    local rc=$?
    set -e
    log "main.py $* exited rc=$rc"
    if [ "$rc" -eq 0 ]; then
        ping_hc ""
    else
        ping_hc "/fail" "$(tail -c 5000 "$SCHED_LOG")"
    fi
    return $rc
}

log "scheduler started"
ping_hc "/start"

# Seconds remaining until midnight
END=$(date -d "tomorrow 00:00:00" +%s)
NOW=$(date +%s)
REMAINING=$((END - NOW - 1))

# Pick a random time within the remaining window and send the daily quote
SLEEP1=$(python3 -c "import random; print(random.randint(0, $REMAINING))")
log "sleeping ${SLEEP1}s before primary run (window=${REMAINING}s)"
sleep "$SLEEP1"

run_main "primary" || true

# ~15% chance of a bonus quote later in the day
if python3 -c "import random, sys; sys.exit(0 if random.random() < 0.15 else 1)"; then
    log "bonus roll: hit"
    NOW=$(date +%s)
    REMAINING=$((END - NOW - 1))
    if [ "$REMAINING" -gt 300 ]; then
        SLEEP2=$(python3 -c "import random; print(random.randint(0, $REMAINING))")
        log "sleeping ${SLEEP2}s before bonus run (window=${REMAINING}s)"
        sleep "$SLEEP2"
        run_main "bonus" --bonus || true
    else
        log "bonus skipped: only ${REMAINING}s left in day"
    fi
else
    log "bonus roll: miss"
fi

log "scheduler done"
