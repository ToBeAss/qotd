#!/bin/bash
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$DIR/.venv/bin/python3"

# Seconds remaining until midnight
END=$(date -d "tomorrow 00:00:00" +%s)
NOW=$(date +%s)
REMAINING=$((END - NOW - 1))

# Pick a random time within the remaining window and send the daily quote
SLEEP1=$(python3 -c "import random; print(random.randint(0, $REMAINING))")
sleep "$SLEEP1"

"$PYTHON" "$DIR/main.py"

# ~15% chance of a bonus quote later in the day
if python3 -c "import random, sys; sys.exit(0 if random.random() < 0.15 else 1)"; then
    NOW=$(date +%s)
    REMAINING=$((END - NOW - 1))
    if [ "$REMAINING" -gt 300 ]; then
        SLEEP2=$(python3 -c "import random; print(random.randint(0, $REMAINING))")
        sleep "$SLEEP2"
        "$PYTHON" "$DIR/main.py" --bonus
    fi
fi
