#!/bin/bash
# Convenient wrapper for enter_spammer.py
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python3 "$DIR/scripts/enter_spammer.py" "$@"
