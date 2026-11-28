#!/bin/sh
# Start the PatroLink board node in the background on the board (STM32MP257, OpenSTLinux).
# Running it again does nothing if the node is already running.   Stop it with ./stop.sh
# usage: ./start.sh        log: ./log/node.log
cd "$(dirname "$0")" || exit 1
mkdir -p log
if [ -f node.pid ] && kill -0 "$(cat node.pid)" 2>/dev/null; then
    echo "already running (pid $(cat node.pid))"
else
    setsid nohup python3 board_node.py --out "$PWD/log" >> log/node.log 2>&1 &
    echo $! > node.pid
    sleep 1
    if kill -0 "$(cat node.pid)" 2>/dev/null; then
        echo "started (pid $(cat node.pid))"
    else
        echo "failed to start; last lines of the log:"
        tail -5 log/node.log
        rm -f node.pid
        exit 1
    fi
fi
ip -4 -o addr show | awk '/ wlan0 / {print "wlan0 address: " $4}'
echo "log: $PWD/log/node.log"
echo "on the laptop: python pc_collect.py doctor --board <the wlan0 address above>"
