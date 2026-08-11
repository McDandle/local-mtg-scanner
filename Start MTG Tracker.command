#!/bin/zsh
# Double-click this file in Finder to start the MTG tracker.
cd "$(dirname "$0")"
echo "Starting MTG tracker — leave this window open (Ctrl-C to stop)."
exec python3 server.py
