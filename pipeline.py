#!/usr/bin/env python3
"""MusicLab · 喂歌流水线

把一首歌放进 media/，几秒后就能听、能看波形和频谱；
分轨在后台慢慢补，补完页面自己把它长出来。

【为什么分成两段】（2026-10-28 实测，116 秒的曲子）
    demucs 6 轨 + 模型加载    19.4s   ← 占 75%
    analyze.py（波形/频谱/响度） 6.1s   ← 占 24%
    stems 逐帧分析             0.4s   ← 占 1%

    八成的时间花在最后一步，但最后一步**不影响"看一眼"**。
    所以：先交前一步，后一步排队。加歌的体感从「等 40 秒」
    变成「等 6 秒，然后分轨那一层慢慢填上来」。

【它做什么】
    每 3 秒扫一次 media/，只看缺什么：
      analysis/<id>.json         缺 → 立刻分析（快）
      analysis/<id>.stems.json   缺 → 排进分轨队列（慢）
    状态写 analysis/_pipeline.json，server 通过 /api/pipeline 给前端。
    新文件不在 library.json 里的，自动补一条（只补新的，不碰你手工写的）。

【它不做什么】
    不删任何东西，不覆盖已经存在的分析结果，不改已有条目的元数据。
    中断了直接重启 —— 已经有的会跳过。

跑：
    .venv/bin/python pipeline.py          # 常驻，边跑边等新歌
    .venv/bin/python pipeline.py --once   # 补齐现有缺口就退出
"""
import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "tools"))

MEDIA = os.environ.get("MUSICLAB_MEDIA") or os.path.join(ROOT, "media")
ANALYSIS = os.environ.get("MUSICLAB_ANALYSIS") or os.path.join(ROOT, "analysis")
STEMS = os.environ.get("MUSICLAB_STEMS") or os.path.join(ROOT, "stems")
LIBRARY = os.environ.get("MUSICLAB_LIBRARY") or os.path.join(ROOT, "library.json")
STATE = os.path.join(ANALYSIS, "_pipeline.json")

MODEL = "htdemucs_6s"
DEVICE = "mps"
AUDIO_EXT = (".flac", ".wav", ".m4a", ".ogg")
STEM_NAMES = ["vocals", "drums", "bass", "guitar", "piano", "other"]

_lock = threading.Lock()
_state = {"updated": 0, "items": {}}
_seen = set()          # 已经在队列里或正在跑的 id，防止重复入队


# ── 状态 ────────────────────────────────────────────────────────────
def set_stage(tid, stage, note=""):
    with _lock:
        _state["items"][tid] = {
            "stage": stage,
            "since": round(time.time(), 1),
            "note": note,
        }
        _state["updated"] = round(time.time(), 1)
        tmp = STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, STATE)      # 原子替换：前端永远读不到半个文件
    print(f"  · {stage:<11} {tid[:52]} {note}", flush=True)


# ── 阶段 1：波形 / 频谱 / 响度（快） ────────────────────────────────
def ensure_library(tid, path):
    """新歌自动补进 library.json。已有的条目一个字段都不碰。"""
    with open(LIBRARY, encoding="utf-8") as f:
        lib = json.load(f)
    if any(x.get("id") == tid for x in lib.get("shelf", [])):
        return
    # 文件名约定「Artist-Title.ext」。标题里也可能有 -，所以只切第一个。
    base = os.path.splitext(os.path.basename(path))[0]
    artist, _, title = base.partition("-")
    if not title:
        artist, title = "", base
    lib.setdefault("shelf", []).append({
        "id": tid,
        "file": os.path.basename(path),
        "title": title,
        "artist": artist,
        "album": title,
        "quality": "",
        "analyzed": True,
    })
    tmp = LIBRARY + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(lib, f, ensure_ascii=False, indent=2)
    os.replace(tmp, LIBRARY)
    print(f"  + 入库 {tid[:52]}  （artist={artist!r} title={title!r}）", flush=True)


def rebuild_index():
    import build_index
    build_index.main()


def stage_wave(tid, path):
    set_stage(tid, "wave")
    import analyze
    analyze.analyze(path, ANALYSIS)
    ensure_library(tid, path)
    rebuild_index()
    set_stage(tid, "wave_done")


# ── 阶段 2：分轨（慢） ──────────────────────────────────────────────
def have_stems(tid):
    d = os.path.join(STEMS, MODEL, tid)
    return all(os.path.isfile(os.path.join(d, n + ".wav")) for n in STEM_NAMES)


def stage_stems(tid, path):
    if not have_stems(tid):
        set_stage(tid, "separating")
        cmd = [sys.executable, "-m", "demucs", "-n", MODEL, "-d", DEVICE,
               "-o", STEMS, path]
        r = subprocess.run(cmd, cwd=ROOT)
        if r.returncode != 0:
            raise RuntimeError(f"demucs 退出码 {r.returncode}")
    set_stage(tid, "tracks")
    import analyze_stems
    res = analyze_stems.analyze_dir(os.path.join(STEMS, MODEL, tid), tid)
    dst = os.path.join(ANALYSIS, tid + ".stems.json")
    tmp = dst + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False)
    os.replace(tmp, dst)
    set_stage(tid, "done")


# ── 两个工人（快慢分开，新歌不会被慢活挡住） ───────────────────────
wave_q, stems_q = queue.Queue(), queue.Queue()


def worker(q, fn):
    while True:
        item = q.get()
        if item is None:
            q.task_done()
            return
        tid, path = item
        try:
            fn(tid, path)
        except Exception as e:
            traceback.print_exc()
            set_stage(tid, "error", f"{type(e).__name__}: {e}")
        finally:
            with _lock:
                _seen.discard(tid)
            q.task_done()


def scan():
    """只看缺口，不看时间戳。已有的永远不重跑。"""
    if not os.path.isdir(MEDIA):
        return []
    todo = []
    for f in sorted(os.listdir(MEDIA)):
        if f.startswith(".") or not f.lower().endswith(AUDIO_EXT):
            continue
        tid = os.path.splitext(f)[0]
        if tid in _seen:
            continue
        path = os.path.join(MEDIA, f)
        if not os.path.isfile(os.path.join(ANALYSIS, tid + ".json")):
            todo.append(("wave", tid, path))
        elif not os.path.isfile(os.path.join(ANALYSIS, tid + ".stems.json")):
            todo.append(("stems", tid, path))
    return todo


def dispatch(todo):
    for kind, tid, path in todo:
        with _lock:
            if tid in _seen:
                continue
            _seen.add(tid)
        (wave_q if kind == "wave" else stems_q).put((tid, path))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="补齐现有缺口就退出")
    ap.add_argument("--interval", type=float, default=3.0)
    args = ap.parse_args()

    os.makedirs(ANALYSIS, exist_ok=True)
    with _lock:                      # 一启动就落一个状态文件，前端随时能读
        _state["updated"] = round(time.time(), 1)
        with open(STATE + ".tmp", "w", encoding="utf-8") as f:
            json.dump(_state, f, ensure_ascii=False, indent=1)
        os.replace(STATE + ".tmp", STATE)
    if os.path.isfile(STATE):
        try:
            with open(STATE, encoding="utf-8") as f:
                _state.update(json.load(f))
        except Exception:
            pass

    threads = [
        threading.Thread(target=worker, args=(wave_q, stage_wave), daemon=True),
        threading.Thread(target=worker, args=(stems_q, stage_stems), daemon=True),
    ]
    for t in threads:
        t.start()

    print("\n  MusicLab pipeline  —  media/ 里丢歌就行\n", flush=True)
    try:
        while True:
            dispatch(scan())
            if args.once:
                # 波形跑完才会暴露"缺 stems.json"，所以要跑到没有新任务为止。
                while True:
                    wave_q.join()
                    stems_q.join()
                    more = scan()
                    if not more:
                        break
                    dispatch(more)
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n  停。\n", flush=True)
    if args.once:
        print("  补齐完成。\n", flush=True)


if __name__ == "__main__":
    main()
