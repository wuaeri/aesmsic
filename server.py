#!/usr/bin/env python3
"""
MusicLab · 本地音乐分析播放器
================================

跑在 localhost 上的音乐播放器。正面是播放，翻过来是编曲解剖。

【这一版做到哪】
  ✅ 本地 HTTP 服务（含 Range 请求 —— 能拖进度条）
  ✅ 曲库索引（library.json）+ 汇总索引（analysis/_index.json，见 build_index.py）
  ✅ 分析数据服务（analysis/*.json）
  ✅ 频谱详情切片服务（analysis/*.spec/*.bin）
  ✅ 总入口（static/home.html，挂在 /）—— 音乐 / 影视两扇门
  ✅ 播放器（static/index.html，挂在 /player）
  ✅ 放映厅（static/screening.html，挂在 /screening）
     ⚠️ 它的**数据**不在这个项目里，见下面「两个区，一份服务」。
  ⬜ 分轨播放

【两个区，一份服务】（2026-09-27）
  音乐和影视以前是两个项目、两个 python 进程、两个端口（8900 / 8899），
  两套审美各长各的。现在并成一个站点：一个 server、一个端口、一个品牌。

  ★ 但**数据没搬家**。放映厅的片库索引、13 张海报、还有那个 421MB 的
    Detour.mp4，全都还在 ~/Projects/ScreeningRoom/ —— 这里只是把 URL 指过去。
    理由：媒体文件是数据不是代码；跨目录读比复制一份 420MB 干净，
    而且哪天想拆回去，改一行常量就行。**可逆。**
    路径想改的话：MUSICLAB_SCREENING=/别的/地方 python3 server.py

【为什么手写 HTTP 服务，不用现成框架】
  整个文件唯一有技术含量的东西是 Range 请求。
  浏览器必须拿到 `Accept-Ranges: bytes` + `206 Partial Content` 才肯让你拖进度条，
  否则只能从头顺播 —— 一个 264MB 的 24bit/192kHz 文件，那样根本没法用。
  标准库里没有现成的，所以自己实现这几十行。

术语速查：
  Range 请求 —— HTTP 协议里「我只要这个文件的第 X 到 Y 字节」的说法。
                音频播放器靠它实现拖动跳转（否则得把整个文件下完才能跳）。
  206        —— HTTP 状态码，「部分内容」，Range 请求成功时返回这个。
  416        —— 「你要的范围不合法」，比如起点已经超出文件大小了。

跑：
    python3 server.py            # → http://127.0.0.1:8900
"""

import json
import mimetypes
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

# ── 配置 ───────────────────────────────────────────────────────────
PORT = 8900
CHUNK = 256 * 1024   # 每次读 256KB。太大占内存，太小拖进度条会一顿一顿的

ROOT = os.path.dirname(os.path.abspath(__file__))
MEDIA_DIR = os.path.join(ROOT, "media")
STATIC_DIR = os.path.join(ROOT, "static")
ANALYSIS_DIR = os.path.join(ROOT, "analysis")
LIBRARY = os.path.join(ROOT, "library.json")

# ── 影视区（放映厅）的数据在**另一个项目目录**里 ────────────────────
# 不搬家的理由写在文件头的「两个区，一份服务」。这里只记一句：
# 下面这四个路径是这个服务**唯一**跨出项目根的地方，全部集中在这里，
# 好让"它到底读了外面什么"一眼能看完。
SCREENING = os.environ.get("MUSICLAB_SCREENING") or os.path.expanduser("~/Projects/ScreeningRoom")
S_LIBRARY = os.path.join(SCREENING, "library.json")
S_COVERS = os.path.join(SCREENING, "covers")
S_MEDIA = os.path.join(SCREENING, "media")

# 音频扩展名 → MIME 类型。浏览器靠 MIME 判断这东西它能不能播。
mimetypes.add_type("audio/flac", ".flac")
mimetypes.add_type("audio/wav", ".wav")
mimetypes.add_type("audio/mp4", ".m4a")
mimetypes.add_type("audio/ogg", ".ogg")


def _safe(base: str, rel: str):
    """把 URL 里的路径钉死在 base 目录内，防止 ../../ 跑到别处去（目录穿越）。"""
    path = os.path.realpath(os.path.join(base, rel.lstrip("/")))
    if path != base and not path.startswith(base + os.sep):
        return None
    return path


class Handler(BaseHTTPRequestHandler):
    server_version = "MusicLab/0.1"

    # ── 工具 ──────────────────────────────────────────────────────
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, head_only=False):
        """送文件。核心是处理 Range 请求。"""
        if not path or not os.path.isfile(path):
            self.send_error(404, "not found")
            return

        size = os.path.getsize(path)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        partial = False

        if rng:
            # 只认 `bytes=起点-终点` 这一种。终点可以省（`bytes=100-`）。
            m = re.match(r"bytes=(\d*)-(\d*)\s*$", rng.strip())
            if not m or (m.group(1) == "" and m.group(2) == ""):
                self.send_error(416, "bad range")
                return
            a, b = m.group(1), m.group(2)
            if a == "":
                # `bytes=-500` 意思是「最后 500 字节」，不是「从头到 500」。容易搞反。
                start = max(0, size - int(b))
            else:
                start = int(a)
                end = int(b) if b else size - 1
            if start >= size or start > end:
                # 416 要带上 Content-Range，告诉对方这文件到底多大。
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            end = min(end, size - 1)
            partial = True

        length = end - start + 1

        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")   # ← 没这行浏览器不给拖
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        # ★ 只让音频走缓存，其它一律 no-store。
        #   音频是几百 MB 的大家伙，不缓存的话拖一次进度条就重下一遍，没法用；
        #   但页面和 JSON 必须每次拿最新的 —— 否则改完前端刷新，
        #   浏览器把旧文件喂给你，你就对着一个根本不存在的版本 debug。
        #   （2026-09-16 栽过一次：改完 waves 组件，看到的还是旧文件。）
        if not ctype.startswith("audio/"):
            self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()

        if head_only:
            return

        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    buf = f.read(min(CHUNK, left))
                    if not buf:
                        break
                    self.wfile.write(buf)
                    left -= len(buf)
        except (BrokenPipeError, ConnectionResetError):
            # 你拖进度条、或者把页面关了 —— 连接断掉是正常现象，不是错误。
            pass

    # ── 路由 ──────────────────────────────────────────────────────
    def do_GET(self):
        # ★ 顺序不能反：先按原始 URL 切掉查询串，再 unquote。
        #   反过来的话，文件名里编码的 %3F 会被当成查询串分隔符，路径直接被切断。
        path = unquote(self.path.split("?")[0])

        # ★ 2026-09-26：`/` 改成**进入页**，播放器退到 /player。
        #   播放器仍然保留 /index.html 这条老路径 —— 加了道门不该把已有的
        #   书签和深链接弄断（?id= / ?t= 那些地址都是直接指向播放器的）。
        if path in ("/", "/home", "/home.html"):
            return self._file(os.path.join(STATIC_DIR, "home.html"))

        if path in ("/player", "/player.html", "/index.html"):
            return self._file(os.path.join(STATIC_DIR, "index.html"))

        # ── 影视区 ────────────────────────────────────────────────
        if path in ("/screening", "/screening.html"):
            return self._file(os.path.join(STATIC_DIR, "screening.html"))

        if path == "/api/screening":
            if os.path.isfile(S_LIBRARY):
                try:
                    with open(S_LIBRARY, encoding="utf-8") as f:
                        return self._json(json.load(f))
                except Exception as e:
                    return self._json({"error": str(e)}, 500)
            return self._json({"shelf": []})

        # 海报和片子都指向 ScreeningRoom 的数据目录。
        # ★ 两个前缀各走各的 `_safe()` base —— _safe 把路径钉死在传入的目录内，
        #   所以 /screening/covers/../../library.json 这种是出不去的。
        for sp, sbase in (("/screening/covers/", S_COVERS),
                          ("/screening/media/", S_MEDIA)):
            if path.startswith(sp):
                return self._file(_safe(sbase, path[len(sp):]))

        # 喂歌流水线的状态（pipeline.py 写，前端每几秒拉一次）。
        # 没有这个文件 = 流水线没跑过，返回空表，前端按"全部就绪"处理。
        if path == "/api/pipeline":
            p = os.path.join(ANALYSIS_DIR, "_pipeline.json")
            if os.path.isfile(p):
                try:
                    with open(p, encoding="utf-8") as f:
                        return self._json(json.load(f))
                except Exception as e:
                    return self._json({"items": {}, "updated": 0, "error": str(e)})
            return self._json({"items": {}, "updated": 0})

        if path == "/api/library":
            if os.path.isfile(LIBRARY):
                try:
                    with open(LIBRARY, encoding="utf-8") as f:
                        return self._json(json.load(f))
                except Exception as e:
                    return self._json({"error": str(e)}, 500)
            return self._json({"shelf": []})

        if path.startswith("/api/analysis/"):
            name = path[len("/api/analysis/"):]
            if not name.endswith(".json"):
                name += ".json"
            return self._file(_safe(ANALYSIS_DIR, name))

        # 频谱详情切片：/api/spec/{曲名}/{序号}.bin → analysis/{曲名}.spec/{序号}.bin
        # ★ 从**右边**找最后一个 / 来切。曲名里什么字符都可能有（空格、逗号、括号），
        #   将来也难说会不会有斜杠；从左边数第一个 / 是不安全的切法，
        #   右边这一段才是保证只有「{序号}.bin」的部分。
        if path.startswith("/api/spec/"):
            stem, sep, name = path[len("/api/spec/"):].rpartition("/")
            if not sep:
                return self.send_error(404, "not found")
            return self._file(_safe(ANALYSIS_DIR, f"{stem}.spec/{name}"))

        for prefix, base in (("/media/", MEDIA_DIR), ("/static/", STATIC_DIR)):
            if path.startswith(prefix):
                return self._file(_safe(base, path[len(prefix):]))

        self.send_error(404, "not found")

    def do_HEAD(self):
        # ★ 顺序不能反：先按原始 URL 切掉查询串，再 unquote。
        #   反过来的话，文件名里编码的 %3F 会被当成查询串分隔符，路径直接被切断。
        path = unquote(self.path.split("?")[0])
        # ★ 每个前缀都必须和 do_GET 保持一致。少了 /api/analysis/ 那条，
        #   HEAD 会一路掉到最后的 index.html 兜底 —— 响应头返回 text/html，
        #   看起来像"JSON 服务坏了"，其实只是 HEAD 走错了分支。
        if path.startswith("/api/analysis/"):
            name = path[len("/api/analysis/"):]
            if not name.endswith(".json"):
                name += ".json"
            return self._file(_safe(ANALYSIS_DIR, name), head_only=True)
        if path.startswith("/api/spec/"):
            stem, sep, name = path[len("/api/spec/"):].rpartition("/")
            if not sep:
                return self.send_error(404, "not found")
            return self._file(_safe(ANALYSIS_DIR, f"{stem}.spec/{name}"), head_only=True)
        if path == "/api/screening":
            return self._file(S_LIBRARY, head_only=True)
        for sp, sbase in (("/screening/covers/", S_COVERS),
                          ("/screening/media/", S_MEDIA)):
            if path.startswith(sp):
                return self._file(_safe(sbase, path[len(sp):]), head_only=True)
        for prefix, base in (("/media/", MEDIA_DIR), ("/static/", STATIC_DIR)):
            if path.startswith(prefix):
                return self._file(_safe(base, path[len(prefix):]), head_only=True)
        # ★ 和 do_GET 的首页分支保持一致：/ 是进入页，/player 才是播放器。
        #   这里两处写岔过一次（HEAD 一路掉到兜底，响应头返回 text/html，
        #   看着像"服务坏了"），所以两边都显式列出来。
        if path in ("/", "/home", "/home.html"):
            return self._file(os.path.join(STATIC_DIR, "home.html"), head_only=True)
        if path in ("/player", "/player.html", "/index.html"):
            return self._file(os.path.join(STATIC_DIR, "index.html"), head_only=True)
        if path in ("/screening", "/screening.html"):
            return self._file(os.path.join(STATIC_DIR, "screening.html"), head_only=True)
        self._file(os.path.join(STATIC_DIR, "home.html"), head_only=True)

    def log_message(self, fmt, *args):
        # 2xx 不记 —— 拖一次进度条能产生几十个 Range 请求，全打出来没法看。
        line = fmt % args
        if " 200 " not in line and " 206 " not in line:
            sys.stderr.write("  " + line + "\n")


def main():
    os.makedirs(MEDIA_DIR, exist_ok=True)
    os.makedirs(ANALYSIS_DIR, exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"\n  MusicLab  →  http://127.0.0.1:{PORT}\n")
    print(f"  media/     {MEDIA_DIR}")
    print(f"  analysis/  {ANALYSIS_DIR}\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  停。\n")


if __name__ == "__main__":
    main()
