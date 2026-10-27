#!/usr/bin/env bash
cd "$(dirname "$0")"
PORT=2345

# 清理已占用 2345 端口的旧进程
OLD_PID=$(lsof -ti tcp:${PORT} 2>/dev/null)
if [ -n "${OLD_PID}" ]; then
    kill -9 ${OLD_PID} 2>/dev/null
    sleep 0.5
fi

(sleep 1 && open "http://127.0.0.1:${PORT}") &
python3 server_2345.py
