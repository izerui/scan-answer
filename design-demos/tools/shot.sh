#!/bin/bash
# 用法: shot.sh <html/svg 路径> <输出png> [宽,高] [#hash]
f=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")
size=${3:-1440,900}
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --disable-gpu --hide-scrollbars \
  --force-device-scale-factor=1 --window-size=$size --virtual-time-budget=4000 \
  --screenshot="$2" "file://$f$4" >/dev/null 2>&1
echo "$2"
