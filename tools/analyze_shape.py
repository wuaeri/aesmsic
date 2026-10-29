#!/usr/bin/env python3
"""声学画像 —— 不依赖任何模型的声部刻画。

【为什么要有这个东西】
  Demucs 的 `htdemucs_6s` 官方自称 experimental，钢琴轨「a lot of bleeding
  and artifacts」。所以「这一轨是钢琴」这句话本身不可信 —— 它是 estimated。

  但「这一轨此刻听起来是什么性质」是**可以直接测量的**：对信号做数学变换，
  没有猜测成分。这份分析给出的就是后者。

  ★ 整份输出 method = measured。
    它描述的是「声音长什么样」，不是「这是什么乐器」——
    前者能算，后者不能。

  ★ 判据的锚点：拿可信轨校准，再应用到不可信轨上。
    Demucs 的训练目标是 4 轨（vocals/drums/bass/other），这三条有官方 SDR 基准，
    可信。如果「鼓掉得最快、人声最不掉」这个规律在可信轨上成立，
    那同一把尺子量钢琴轨得出的结论就有依据。

术语速查：
  谱质心    —— 频谱的「重心」频率。低=闷（贝斯、底鼓），高=亮（镲片、齿音）。
  谱滚降    —— 85% 的能量落在这个频率以下。比质心稳，不受单个极值影响。
  谱平坦度  —— 几何均值 / 算术均值。接近 1 = 像白噪声，接近 0 = 像纯乐音。
  过零率    —— 信号每秒穿过零点的次数。噪声和高频内容会推高它。
  音符级衰减 —— 一个音起音之后 300ms 掉了多少 dB。
                鼓/拨弦掉得快（一击即散），弓弦/pad 几乎不掉（按住就一直有）。
                ★ 这是唯一能把「衰减型」和「持续型」分开的量。
                  在**帧级**算包络斜率是不行的 —— 六条轨的分布会完全一样，
                  因为「响不响」是音乐共性，不是乐器个性。

跑：
    .venv/bin/python tools/analyze_shape.py stems/htdemucs_6s/<曲名> --id <曲库id>
"""
import argparse
import json
import os
import sys
import time

import numpy as np
import soundfile as sf
import librosa

# ── 参数 ─────────────────────────────────────────────────────────────
SR = 22050          # 分析采样率。只关心包络和音色，不需要 44.1k
NFFT = 4096         # 频率分辨率 5.38Hz —— 分得开贝斯的 40Hz 和 80Hz
HOP = 1102          # 50ms @ 22050，跟 analyze_stems.py 的 FRAME_MS 对齐
FRAME_MS = 50.0

WIN_SEC = 2.0       # 每个窗口多长
HOP_SEC = 0.5       # 窗口之间走多远（滑窗，前端画出来才是连续的）

# 相对本轨峰值的门限 —— 低于这个的帧算「没声音」，不参与统计
GATE_DB = 45.0

# 音符级衰减的观察窗口。
# ★ 300ms 试过 —— 只有鼓能分出来（11.8dB vs 其它全部 1–2.2dB），
#   因为钢琴一个音的衰减尾有 1–2 秒，300ms 内它和 pad 都还没开始掉。
#   撑到 1000ms 才能看见「按下去之后到底掉不掉」。
DECAY_MS = 1000.0

STEM_ORDER = ["vocals", "drums", "bass", "guitar", "piano", "other"]
STEM_LABEL = {
    "vocals": "人声", "drums": "鼓", "bass": "贝斯",
    "guitar": "吉他", "piano": "钢琴", "other": "其他",
}


def load_mono(path):
    """读成 22.05kHz 单声道。"""
    data, sr = sf.read(path, always_2d=True, dtype="float32")
    mono = data.mean(axis=1)
    if sr != SR:
        mono = librosa.resample(mono, orig_sr=sr, target_sr=SR)
    return mono


def note_drops(db, onsets, gate_db, w):
    """音符级衰减：每个起音之后 w 帧内掉了多少 dB。

    ★ 这个量是整份分析的骨干。为什么不在帧上算斜率：
      帧级斜率测的是「这一轨此刻响不响」，六条轨的分布会完全一样
      （实测：钢琴和鼓的衰减帧占比 17.3% vs 26.0%，分不开）。
      而盯住**一个具体的音**从起音看到它自己掉下去，才是在测「这个乐器
      按下去之后会怎样」—— 那是乐器的性质。

    ★ 段长取「到这个音自己的下一个起音为止」，最多 w 帧 ——
      不用固定窗口。固定窗会把下一个音的能量算进来，衰减率被稀释。

    返回一个和 db 等长的数组，起音处填衰减值（正数 = 掉了多少 dB），其余 NaN。
    """
    out = np.full(len(db), np.nan)
    n = len(db)
    for i, o in enumerate(onsets):
        nxt = int(onsets[i + 1]) if i + 1 < len(onsets) else n - 1
        end = min(nxt, o + w, n - 1)
        seg = db[o:end + 1]
        if len(seg) < 4:
            continue
        # 段内峰值必须够响，否则算出来的是噪声的衰减
        if seg.max() < gate_db:
            continue
        peak = float(seg.max())
        # 末尾三帧平均，抗单帧抖动。
        # ★ 尾部要夹到门限上 —— 低于门限的帧按定义就是「没声音」，
        #   拿它算出来的落差是假的。实测不夹的话会出现 236dB 的值：
        #   那是 peak 减去 rms 的假地板（1e-12 → −240dB），物理上没有意义。
        #   夹住之后衰减上限就是 GATE_DB，含义干净：
        #   「这个音掉到听不见了」。
        tail = max(float(seg[-3:].mean()), gate_db)
        out[o] = peak - tail
    return out


def track_shape(path):
    """一条轨 → 逐窗的声学画像。"""
    mono = load_mono(path)
    S = np.abs(librosa.stft(mono, n_fft=NFFT, hop_length=HOP)) ** 2   # 功率谱
    n = S.shape[1]
    if n == 0:
        return None

    rms = np.sqrt(S.sum(axis=0))
    db = 20.0 * np.log10(np.maximum(rms, 1e-12))
    gate = float(db.max()) - GATE_DB
    active = db > gate

    cen = librosa.feature.spectral_centroid(S=S, sr=SR)[0]
    roll = librosa.feature.spectral_rolloff(S=S, sr=SR, roll_percent=0.85)[0]
    flat = librosa.feature.spectral_flatness(S=S)[0]
    zcr = librosa.feature.zero_crossing_rate(
        mono, frame_length=NFFT, hop_length=HOP, center=True)[0]

    # 起音检测。wait=2 帧 = 100ms 最小间隔（十六分音符 @150bpm 就是 100ms，
    # 再密就不是「一个音」了）。backtrack 把起音点拉回到能量上升之前。
    env = librosa.onset.onset_strength(S=librosa.power_to_db(S), sr=SR, hop_length=HOP)
    onsets = librosa.onset.onset_detect(
        onset_envelope=env, sr=SR, hop_length=HOP,
        backtrack=True, delta=0.15, wait=2)
    drops = note_drops(db, onsets, gate, int(DECAY_MS / FRAME_MS))

    # 长度对齐（不同特征偶尔差一帧）
    k = min(len(cen), len(roll), len(flat), len(zcr), n)
    cen, roll, flat, zcr, db, active = (
        cen[:k], roll[:k], flat[:k], zcr[:k], db[:k], active[:k])
    drops = drops[:k]

    win = max(1, int(round(WIN_SEC * 1000 / FRAME_MS)))
    step = max(1, int(round(HOP_SEC * 1000 / FRAME_MS)))

    windows = []
    for s in range(0, max(1, k - win + 1), step):
        e = min(s + win, k)
        act = active[s:e]
        ar = float(act.mean())
        rec = {"t": round(s * FRAME_MS / 1000.0, 2),
               "active": round(ar, 3)}
        if ar > 0.02:
            m = act                                   # 只统计有声的帧
            d = drops[s:e]
            d = d[np.isfinite(d)]
            rec.update({
                "centroid": round(float(np.median(cen[s:e][m])), 0),
                "rolloff": round(float(np.median(roll[s:e][m])), 0),
                # 平坦度数值极小，存 log10 —— 前端要做线性映射得先取对数
                "flat_db": round(float(np.log10(max(np.median(flat[s:e][m]), 1e-9))), 2),
                "zcr": round(float(np.median(zcr[s:e][m])), 4),
                "decay": (round(float(np.median(d)), 1) if d.size else None),
                "onset_rate": round(len(d) / max(1e-9, (e - s) * FRAME_MS / 1000.0), 2),
                "rms_db": round(float(np.median(db[s:e][m])), 1),
            })
        windows.append(rec)

    # 整轨汇总（前端列表页用，不用扫全部窗口）
    d_all = drops[np.isfinite(drops)]
    summary = {
        "active_ratio": round(float(active.mean()), 3),
        "centroid": round(float(np.median(cen[active])), 0) if active.any() else None,
        "rolloff": round(float(np.median(roll[active])), 0) if active.any() else None,
        "flat_db": round(float(np.log10(max(np.median(flat[active]), 1e-9))), 2)
                    if active.any() else None,
        "zcr": round(float(np.median(zcr[active])), 4) if active.any() else None,
        "decay": round(float(np.median(d_all)), 1) if d_all.size else None,
        "decay_p25": round(float(np.percentile(d_all, 25)), 1) if d_all.size else None,
        "decay_p75": round(float(np.percentile(d_all, 75)), 1) if d_all.size else None,
        "n_notes": int(d_all.size),
        "onsets": int(len(onsets)),
    }
    return {"windows": windows, "summary": summary}


def analyze_dir(stem_dir, track_id, quiet=False):
    t0 = time.time()
    tracks = []
    for name in STEM_ORDER:
        p = os.path.join(stem_dir, name + ".wav")
        if not os.path.isfile(p):
            continue
        r = track_shape(p)
        if r is None:
            continue
        tracks.append({
            "name": name,
            "label": STEM_LABEL.get(name, name),
            # ★ 逐窗的声学量：这是 measured，不是模型给的
            "method": "measured",
            **r,
        })
        s = r["summary"]
        if not quiet:
            print(f"    {STEM_LABEL.get(name,name):<6} 活跃{s['active_ratio']:>6.1%}  "
                  f"质心{s['centroid'] if s['centroid'] is not None else 0:>6.0f}Hz  "
                  f"衰减中位{s['decay'] if s['decay'] is not None else 0:>6.1f}dB  "
                  f"音符{s['n_notes']:>5}")

    return {
        "id": track_id,
        "analyzed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_sec": round(time.time() - t0, 2),
        "shape": {
            "method": "measured",
            "frame_ms": FRAME_MS,
            "win_sec": WIN_SEC,
            "hop_sec": HOP_SEC,
            "note": "逐窗声学画像。整份是 measured —— 对信号直接测量，"
                    "不含模型推断。它描述「声音长什么样」，不描述「这是什么乐器」。",
            "descriptors": {
                "centroid": "谱质心 Hz。低=闷（贝斯/底鼓），高=亮（镲/齿音）",
                "rolloff": "85% 滚降点 Hz。85% 的能量在这个频率以下",
                "flat_db": "谱平坦度的 log10。接近 0 = 像噪声，越负 = 越像乐音",
                "zcr": "过零率。噪声和高频内容推高它",
                "decay": "音符级衰减 dB/300ms。大 = 按下去就掉（鼓/拨弦），"
                         "小 = 按住不变（弓弦/pad/人声）",
                "onset_rate": "每秒起音数",
            },
            "tracks": tracks,
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stem_dir")
    ap.add_argument("--id", required=True)
    ap.add_argument("--out", default="analysis")
    args = ap.parse_args()

    print(f"  声学画像 {args.id} …")
    res = analyze_dir(args.stem_dir, args.id)
    os.makedirs(args.out, exist_ok=True)
    dst = os.path.join(args.out, args.id + ".shape.json")
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False)
    print(f"\n  → {dst}  ({res['elapsed_sec']}s)")


if __name__ == "__main__":
    main()
