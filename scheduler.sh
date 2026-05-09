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

sleep_until() {
    # Sleep until an absolute epoch time. No-op if already past.
    local target=$1
    local now
    now=$(date +%s)
    local diff=$((target - now))
    if [ "$diff" -gt 0 ]; then
        sleep "$diff"
    fi
}

log "scheduler started"
ping_hc "/start"

# Three time bands, each delivers an independent quote at a random moment in the band.
# Slot A: 06:00-12:00 (guaranteed)
# Slot B: 12:00-18:00 (guaranteed)
# Slot C: 18:00-24:00 (30% chance)
# A slot whose window has already closed when cron fires is skipped so we don't
# fire two messages back-to-back when cron is late.
T_NOON=$(date -d "today 12:00:00" +%s)
T_18=$(date -d "today 18:00:00" +%s)
T_END=$(date -d "tomorrow 00:00:00" +%s)

run_slot() {
    # run_slot <label> <window_start_epoch> <window_end_epoch> [extra args to main.py]
    local label=$1
    local lo=$2
    local hi=$3
    shift 3
    local now
    now=$(date +%s)
    if [ "$now" -ge "$hi" ]; then
        log "$label: skipped (window already closed)"
        return 0
    fi
    if [ "$now" -gt "$lo" ]; then
        lo=$now
    fi
    local target
    target=$(python3 -c "import random; print(random.randint($lo, $hi - 60))")
    log "$label target: $(date -d @"$target" -Iseconds)"
    sleep_until "$target"
    run_main "$label" "$@" || true
}

run_slot "slot-a" "$(date -d "today 06:00:00" +%s)" "$T_NOON"
run_slot "slot-b" "$T_NOON" "$T_18"

# Slot C — 30% chance.
if python3 -c "import random, sys; sys.exit(0 if random.random() < 0.30 else 1)"; then
    run_slot "slot-c" "$T_18" "$T_END"
else
    log "slot-c: skipped (roll missed)"
fi

log "scheduler done"
