#!/bin/bash
# MusicLab 一键起停。
#   ./start.sh          起 server + pipeline
#   ./start.sh stop     全停
# 两个进程是分开的：server 只管发文件，pipeline 只管算。
# 关掉 pipeline 不影响听歌，只是新歌不会被分析。
cd "$(dirname "$0")" || exit 1

# 有 venv 用 venv，没有就用系统的 python3 —— 别人 clone 下来不必先建环境。
PY=".venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
[ -n "$PY" ] || { echo "  找不到 python3"; exit 1; }

# 曲库索引不进版本库，第一次跑的时候从模板生一份。
[ -f library.json ] || cp library.example.json library.json

stop() {
  pkill -f "MusicLab/server.py" 2>/dev/null
  pkill -f "MusicLab/pipeline.py" 2>/dev/null
  # 上面那种写法匹配不到相对路径启动的进程，再补一刀。
  pgrep -f "python.* pipeline.py" | xargs kill 2>/dev/null
  pgrep -f "python.* server.py" | xargs kill 2>/dev/null
  echo "  stopped"
}

case "$1" in
  stop) stop ;;
  restart) stop; sleep 1; "$0" ;;
  *)
    nohup "$PY" server.py   > /tmp/musiclab.log 2>&1 &
    nohup "$PY" pipeline.py > /tmp/musiclab_pipeline.log 2>&1 &
    sleep 2
    echo "  server    http://musiclab.localhost:8900"
    echo "  pipeline  media/ 里丢歌就会自动分析"
    ;;
esac
