#!/usr/bin/env python3
"""分轨的逐帧能量曲线 + 入场/退场检测。

这是「实时推进的数据」的第一半 —— 把「谁在第几秒进来、第几秒出去」提前算出来。
（第二半在前端：播放时按 currentTime 查表渲染。**「实时」是呈现方式，不是计算方式。**）

── 输入 ──────────────────────────────────────────────
Demucs 的输出目录：
    <root>/<模型名>/<曲名>/{vocals,drums,bass,guitar,piano,other}.wav

── 输出 ──────────────────────────────────────────────
一条轨一个 JSON，喂给前端。

★ 全局参考电平（这是这份数据诚不诚实的关键）
  所有轨**共用同一个参考值** = 全部 stem 里最大的那个 RMS。
  不这么做的话，给每条轨各自归一化，钢琴和鼓画出来一样高 ——
  那是假的，钢琴就是比鼓轻十几 dB。共用参考，轻的轨在图上就是矮的。

★ 迟滞双阈值（防抖）
  单阈值会在阈值附近反复横跳，一首歌能给你抖出几百个 enter/exit。
  所以用两个：越过 -35dB 才算进场，跌破 -40dB 才算离场。
  中间那 5dB 是缓冲区 —— 状态一旦定了，不轻易改。

跑：
    .venv/bin/python tools/analyze_stems.py stems/htdemucs_6s/<曲名> --id <曲库id>
"""
import argparse
import json
import os
import sys
import time

import numpy as np
import soundfile as sf

# 帧长（毫秒）。50ms 是「看得见又能画得动」的折中 ——
# 20ms 太碎（一首歌 9000 帧，前端画不动），100ms 会漏掉快到 0.1 秒的进出。
FRAME_MS = 50

# 迟滞阈值（dB，相对全局参考）
ENTER_DB = -35.0        # 越过这条线 → 判定「进场」
EXIT_DB = -40.0         # 跌破这条线 → 判定「离场」

# 低于这个电平一律当静音（-60dB 以下人耳基本听不见，且是模型残留噪声）
FLOOR_DB = -60.0

# 一次「进场」至少要持续这么久才算数（秒）。
# 否则前奏里一声镲片、模型的一点抖动，都会变成事件。
MIN_EVENT_SEC = 0.4

# ★ 检测前先平滑的窗口（秒）—— 这个参数解决了一个概念性错误。
#
# 初版直接在原始帧上跑状态机，鼓跑出 164 个事件：一首歌里鼓「进出」82 次。
# 错不在阈值，在**概念** —— 鼓、贝斯、拨弦都是**脉冲型**信号，
# 每个音之间有衰减间隙，RMS 在阈值线附近反复穿越。
# 「进场/退场」对这类乐器本来就不成立。
#
# 我们要回答的是 presence（这段里有没有它），不是「这一拍有没有敲」。
# 所以先用一个够长的窗口把脉冲填平，再判断进出。
#
# 代价：入场时刻会被模糊掉约半个窗口。0.8s 是折中 ——
# 鼓的典型间隙在 0.2–0.5s，0.8s 能填平；再长就会把真正的快速交替也糊掉。
SMOOTH_SEC = 0.8

# 相邻两段之间只要间隔这么短，就算同一段（合并）。
# 平滑之后还剩的碎片，靠这个收尾。
MERGE_GAP_SEC = 1.2

STEM_ORDER = ["vocals", "drums", "bass", "guitar", "piano", "other"]

# 中文名 —— 前端直接显示用
STEM_LABEL = {
    "vocals": "人声", "drums": "鼓", "bass": "贝斯",
    "guitar": "吉他", "piano": "钢琴", "other": "其他",
}


def frame_rms(path: str, frame_ms: int):
    """把一条 stem 榨成逐帧 RMS。

    返回 (rms 数组, 采样率, 帧长样本数)。
    """
    data, sr = sf.read(path, always_2d=True, dtype="float32")
    mono = data.mean(axis=1)
    hop = max(1, int(sr * frame_ms / 1000))

    n = len(mono) // hop * hop
    if n == 0:
        return np.zeros(0, dtype=np.float64), sr, hop
    blocks = mono[:n].reshape(-1, hop)
    # RMS = 均方根。比峰值更接近「听起来多响」，适合画能量条。
    rms = np.sqrt((blocks.astype(np.float64) ** 2).mean(axis=1))
    return rms, sr, hop


def db(x: np.ndarray) -> np.ndarray:
    """线性幅度 → dB（相对 1.0）。0 以下全是负数，越大越接近 0。"""
    return 20.0 * np.log10(np.maximum(x, 1e-12))


def smooth_env(x: np.ndarray, win: int) -> np.ndarray:
    """滑动平均，把脉冲型信号填平成「在不在」的包络。

    ★ 两端用 edge padding 补，不能直接 convolve(mode="same") ——
      后者会把开头结尾当成 0 参与平均，导致前几秒电平被压低，
      凭空造出一个假的「进场」。
    """
    if win <= 1:
        return x
    k = np.ones(win) / win
    pad = win // 2
    xp = np.pad(x, pad, mode="edge")
    return np.convolve(xp, k, mode="valid")[: len(x)]


def detect_events(level_db: np.ndarray, frame_sec: float):
    """挑出「谁在第几秒进来、第几秒出去」。

    三步，每一步解决一个具体问题：

      ① 平滑 —— 把脉冲填平。不然鼓点之间的衰减间隙会被当成「离场」，
                 一首歌能给你抖出上百个事件。（初版就是这么错的。）
      ② 迟滞状态机 —— 进场用 -35dB、离场用 -40dB 两个不同阈值。
                 中间 5dB 是缓冲区，状态定了就不因一格抖动翻转。
      ③ 合并 + 最短时长 —— 平滑之后还剩的碎片，间隔 <1.2s 的并成一段；
                 总长 <0.4s 的直接丢（那是模型噪声，不是乐器）。

    返回的事件带 avg_db：这一段里它平均有多响。
    """
    # ① 平滑
    win = max(1, int(round(SMOOTH_SEC / frame_sec)))
    env = smooth_env(level_db, win)

    # ② 迟滞状态机
    segs = []
    active = False
    start = 0
    for i, v in enumerate(env):
        if not active and v > ENTER_DB:
            active, start = True, i
        elif active and v < EXIT_DB:
            active = False
            segs.append([start, i])
    if active:
        segs.append([start, len(env)])

    # ③ 合并邻近段 + 丢掉太短的
    merged = []
    for s in segs:
        if merged and (s[0] - merged[-1][1]) * frame_sec < MERGE_GAP_SEC:
            merged[-1][1] = s[1]
        else:
            merged.append(s)
    merged = [s for s in merged
              if (s[1] - s[0]) * frame_sec >= MIN_EVENT_SEC]

    events = []
    for s0, s1 in merged:
        events.append({
            "t": round(s0 * frame_sec, 3), "type": "enter",
            "avg_db": round(float(env[s0:s1].mean()), 1),
        })
        events.append({"t": round(s1 * frame_sec, 3), "type": "exit"})
    return events, merged


def analyze_dir(stem_dir: str, track_id: str) -> dict:
    t0 = time.time()
    frame_sec = FRAME_MS / 1000.0

    # ── 第一遍：读全部轨，找出全局参考电平 ──
    # 参考值必须**先**知道，才能一次性把所有轨放到同一把尺子上。
    raw = {}
    ref = 1e-12
    for name in STEM_ORDER:
        p = os.path.join(stem_dir, name + ".wav")
        if not os.path.isfile(p):
            continue
        rms, sr, hop = frame_rms(p, FRAME_MS)
        if rms.size:
            raw[name] = rms
            ref = max(ref, float(rms.max()))
        print(f"    读 {name:<7} {rms.size:>5} 帧  ({len(rms) * frame_sec:.1f}s)")

    if not raw:
        raise ValueError(f"{stem_dir} 里一个 stem wav 都没找到")

    # ── 第二遍：归一化 + 检测事件 ──
    tracks = []
    for name, rms in raw.items():
        level = db(rms)                       # 相对 1.0 的 dB
        rel = level - db(np.array([ref]))[0]  # 相对全局参考的 dB

        # 曲线：0..1。全局参考处 = 1.0，-60dB 以下 = 0
        curve = np.clip((rel - FLOOR_DB) / (0.0 - FLOOR_DB), 0.0, 1.0)

        events, segs = detect_events(rel, frame_sec)
        active = rel > EXIT_DB
        n_active = int(active.sum())

        tracks.append({
            "name": name,
            "label": STEM_LABEL.get(name, name),
            "method": "estimated",     # ★ 模型分的轨，是猜的
            "curve": [round(float(v), 3) for v in curve],
            "peak_db": round(float(rel.max()), 1),
            "mean_db_active": (round(float(rel[active].mean()), 1)
                               if n_active else None),
            "active_ratio": round(n_active / len(rel), 3) if len(rel) else 0.0,
            "segments": len(segs),     # 进出几段 —— 比 events 长度直观
            "events": events,
        })

    # 事件多的排前面 —— 前端列表按「活跃度」排比按固定顺序更有信息量
    tracks.sort(key=lambda x: -x["segments"])

    return {
        "id": track_id,
        "analyzed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_sec": round(time.time() - t0, 2),
        "stems": {
            "method": "estimated",        # ★ 整个分轨都是模型推断的
            "model": os.path.basename(os.path.dirname(stem_dir.rstrip("/"))),
            "frame_ms": FRAME_MS,
            "duration": round(len(level) * frame_sec, 2),
            "ref_note": "所有轨共用同一参考电平 = 全部 stem 的最大 RMS。"
                        "轻的轨在图上就是矮的，不做逐轨归一化。",
            "thresholds": {
                "enter_db": ENTER_DB, "exit_db": EXIT_DB, "floor_db": FLOOR_DB,
                "min_event_sec": MIN_EVENT_SEC,
                "smooth_sec": SMOOTH_SEC, "merge_gap_sec": MERGE_GAP_SEC,
                "note": "先进场阈值(-35dB)与离场阈值(-40dB)不同 = 迟滞防抖；"
                        "事件前先做 0.8s 平滑，否则脉冲型乐器会抖出上百个事件。",
            },
            "truth_note": "「第几秒有东西进出」是真（能量跳变）；"
                          "「那个东西是钢琴」是估（模型给的标签）。",
            "tracks": tracks,
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stem_dir", help="demucs 输出的单曲目录")
    ap.add_argument("--id", required=True, help="曲库 id（跟 analysis/<id>.json 对齐）")
    ap.add_argument("--out", default="analysis")
    args = ap.parse_args()

    print(f"  分轨分析 {args.id} …")
    res = analyze_dir(args.stem_dir, args.id)

    os.makedirs(args.out, exist_ok=True)
    dst = os.path.join(args.out, args.id + ".stems.json")
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False)

    print(f"\n  轨        峰值dB  活跃占比  段数")
    for t in res["stems"]["tracks"]:
        print(f"  {t['label']:<6} {t['peak_db']:>7.1f}  {t['active_ratio']:>7.1%}"
              f"  {t['segments']:>5}")
    print(f"\n  → {dst}  ({res['elapsed_sec']}s)")


if __name__ == "__main__":
    main()
