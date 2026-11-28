#!/bin/sh
# Stop the PatroLink board node on the board.   Start it again with ./start.sh
cd "$(dirname "$0")" || exit 1
if [ -f node.pid ] && kill "$(cat node.pid)" 2>/dev/null; then
    echo "stopped (pid $(cat node.pid))"
else
    echo "not running"
fi
rm -f node.pid
