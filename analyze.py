#!/usr/bin/env python3
"""
MusicLab · 音频分析
====================

把一个音频文件榨成 JSON，喂给前端。

【核心原则 —— 分清「算的」和「估的」】
  这份分析里有两类数据。把它们混在一起展示，就是在骗人：

  算的（measured）—— 对原始信号做数学变换，没有猜测成分：
      · 波形        · 频谱图      · 响度(LUFS)
      · 真峰值      · 频段能量    · 立体声宽度

  估的（estimated）—— 模型推断出来的，有置信度，可能是错的：
      · BPM（速度） · 调性(Key)

  所以每个数据块都带一个 `method` 字段，值是 "measured" 或 "estimated"。
  前端据此把两类数据在视觉上分开渲染 —— 这是这个播放器最要紧的一条规矩。

术语速查：
  LUFS     —— 响度单位（Loudness Units Full Scale）。流媒体平台的交付标准，
              Spotify 要 -14。比 dB 更贴近人耳的实际感受。
  dBTP     —— 真峰值（True Peak）。数字信号里最尖的那一点，越过 0 就削波。
  LRA      —— 响度范围（Loudness Range）。一首歌最响和最轻差多少，衡量动态。
  频谱图    —— 时间 × 频率 × 能量的二维图。横轴时间，纵轴频率，亮的地方能量大。
  STFT     —— 短时傅里叶变换。把声音拆成「每一刻都有哪些频率」的数学方法。
  RMS      —— 均方根。衡量一段信号的平均能量，比峰值更接近「听起来多响」。
  BPM      —— Beats Per Minute，每分钟多少拍。
  chroma   —— 色度特征。把每个音按「音名」归类（不管高低八度），用于判调性。

跑：
    python3 analyze.py media/vocals.wav
    python3 analyze.py media/*.flac --out analysis/
"""

import argparse
import base64
import json
import os
import shutil
import sys
import time

import numpy as np
import soundfile as sf
import librosa
import pyloudnorm as pyln

# 分析用的采样率。人耳上限 20kHz，按奈奎斯特定理 22050 够用 —— 分析不需要 192kHz。
ANALYSIS_SR = 22050
# 波形降到多少点。
# ★ 2026-09-26：2000 → 40000。
#   2000 那个数的理由是「前端屏幕就 2000 像素宽，多了白给」—— 那个前提是**整曲铺满一屏**。
#   现在波形要跟着动态窗口放大到 2 秒：2000 点在 2 秒里只剩 16 个点，
#   放大出来全是插值的假细节。40000 点 = 6.2ms/点，2 秒窗口 323 点，
#   落在 1896 个物理像素上是每点 5.9 像素 —— 正好。
WAVE_POINTS = 40000
SPEC_BINS = 448         # 频谱图频率轴格数（对数分布，见 _spectrogram）
SPEC_F_LO = 30.0        # 频率轴下限（Hz）

# ★★ 2026-09-26：FFT 窗口 4096 → 1024。这是整张频谱图清晰度的关键，值得写清楚。
#
#   【窗长和 hop 是两件事，之前一直把它们混为一谈】
#     n_fft（窗长）决定**时间分辨率** —— 一次 STFT 观察多长的一段信号。
#     hop（步进）决定**采样密度** —— 相邻两列隔多少毫秒。
#
#   4096 @22050Hz = 185.8ms 的窗。而一记军鼓只有 20–50ms。
#   它被塞进一个 185.8ms 的观察窗里 —— **不管 hop 取多少，
#   这记军鼓在时间轴上就是 185.8ms 宽**。hop 只决定你用几个格子去描述这团糊。
#
#   【只减 hop 会更糊，不是更清 —— 这条反直觉，用受控信号实测过】
#     底鼓（60Hz 衰减正弦）+ 军鼓（200–6kHz 带通噪声）交替，屏幕上该看到 32 条竖线。
#     窗长 4096 不动、只把 hop 从 1024 压到 256：军鼓**反而看不见了**。
#     因为一记 30ms 的军鼓已经被摊成 185.8ms 宽，能量本来就摊薄了；
#     hop 变小只是把同样这点能量分给更多的列，**每一列更暗**，
#     直接掉到 SPEC_DB_FLOOR 以下被当成黑。屏幕上是一团没有骨头的雾。
#     （当时我判断"hop 是关键"，合成信号一跑就打脸了。）
#
#   【所以是缩窗，不是加密】
#     1024 @22050 = 46.4ms —— 跟一记军鼓同量级，一列就能框住它。
#     hop 保持 NFFT/4 = 256（75% 重叠，STFT 的标准做法），11.6ms 一列。
#     实测这个组合线条最锐利，底鼓那一下的**音高下滑**（起音瞬间频率从高往低扫）
#     都能看出来 —— 那种结构在长窗下是不存在的。
#
#   代价：频率分辨率减半（bin 间距 5.38Hz → 21.53Hz）。
#     30–150Hz 那段独立的 STFT bin 从 22 个降到 5 个。实测**没有出现硬横条** ——
#     分箱循环里那条「窄频段线性插值」的兜底扛住了（见 _spectrogram 里那段注释）。
#     但最低那两个八度的细节是物理上少了，不是画出来的假象，得认。
SPEC_NFFT = 1024

# 详情切片的时长（秒）。
# ★ 为什么要切片：hop 从 1024 降到 256，一首 4 分钟的歌从 5331 列变成 21324 列。
#   前端一直用「整曲源图」的画法（建一张 cols × rows 的离屏 canvas，每帧从它裁窗口），
#   21324 × 448 **超过画布单维上限**（Safari/Chrome 一般 16384）——
#   整曲源图这条路物理上死了，不是慢，是建不出来。
#   前端一次最多看 16 秒的窗口，按 32 秒切片正好让它最多跨两块。
SPEC_CHUNK_SEC = 32.0

# 概览条的目标列数。
# 概览条是**缩小**显示（5000 多列铺进约 1900 物理像素），用不着详情那份密度；
# 池化到 5000 列上下，体积稳定在 3MB 左右 —— 跟改参数之前一个量级。
SPEC_OV_COLS = 5331

# ★ 显示下限。注意这不是"数据的物理下限"，是「画出来多暗就当它是黑的」。
#   归一化是 v = clip((dB + 55) / 55, 0, 1)，而 dB 是相对**这首歌自己的峰值**
#   （amplitude_to_db(ref=np.max)），所以每一首自动自适应。
#   为什么是 55 不是 80：拿 80 试过，实测 5% 分位在 −64.6dB，一半以上的动态范围
#   白白花在噪声上，画出来一片糊、什么都分不出。55 之后中位数落在 0.24 左右，
#   数据铺满了整个色阶。
SPEC_DB_FLOOR = 55.0

# 【频率轴为什么是 448 行】
#   前端 `plotH_css = rows` —— 画布高度**等于行数**，于是 dpr=2 时每行落 2 个物理像素、
#   dpr=1 时每行落 1 个，两种屏幕都是整数。以前写死 320px 高，
#   448 行铺进 604 物理像素 = 每行 1.35px，屏幕上就是一片 1px/2px 参差的毛边。
#   **行数不是越多越好，是要跟画布对得上。**
#
# 【数据分两路走 —— 这是 2026-09-26 改的，原因见 SPEC_CHUNK_SEC 那段】
#   概览条  → 池化到 SPEC_OV_COLS 列，base64 塞进 JSON 的 `data`（约 3MB/首）
#   详情条  → 不池化，整曲按 SPEC_CHUNK_SEC 切片，写成二进制文件
#                analysis/{曲名}.spec/{000,001,…}.bin
#
#   这么分是因为两者的**观看方式完全不同**：
#   概览条是缩小的（5000 列 → 1900px，每列不到半个像素），密度堆上去也看不见；
#   详情条是放大的（8 秒窗口里每列占 10 个物理像素），**每一列都独立可见**，
#   在这里池化掉一列就是实打实丢了一个时间点 —— 军鼓可能整列被跳过。
#
#   切片的另一个理由更硬：整曲 21324 列 × 448 行，前端那张「整曲源图」
#   超过画布单维上限，**建不出来**。不是性能问题，是画不出来。
#
# 【格子数远小于画布 = 放大 = 糊；远大于 = 缩小 = 摩尔纹】—— 这个判断交给前端。

# 五段频带（Hz）。这个划分接近混音师脑子里的分法。
BANDS = [
    ("sub", 20, 60),        # 超低：底鼓的下潜
    ("low", 60, 250),       # 低频：贝斯、底鼓主体
    ("mid", 250, 2000),     # 中频：人声、吉他主体
    ("high_mid", 2000, 6000),   # 中高：人声齿音、军鼓亮度
    ("high", 6000, 16000),  # 高频：镲片、空气感
]

# Krumhansl-Schmuckler 调性模板（音乐心理学实验得出的「各调式音级出现概率」）
KS_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09,
                     2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
KS_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53,
                     2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


# ── 工具 ─────────────────────────────────────────────────────────────

def _downsample_peaks(mono: np.ndarray, points: int):
    """把整首歌压成 points 个峰值点 —— 画波形用的。

    每个格子里取绝对值最大的那一个（不是取平均）。平均值会把瞬态抹平，
    波形看起来就是一条糊掉的香肠；取峰值才能看见鼓点。
    """
    n = len(mono)
    if n == 0:
        return []
    step = max(1, n // points)
    usable = (n // step) * step
    blocks = np.abs(mono[:usable]).reshape(-1, step)
    peaks = blocks.max(axis=1)
    # 归一到 0..1，前端可以直接乘高度
    m = peaks.max() if peaks.size else 1.0
    if m > 0:
        peaks = peaks / m
    # 3 位小数：40000 个点，多一位就是多 40KB。屏幕上分辨不到第 4 位。
    return [round(float(x), 3) for x in peaks]


def _loudness_curve(mono: np.ndarray, sr: int, hop_sec: float = 0.1):
    """逐段算响度，画出「这首歌哪里响哪里轻」。

    用 400ms 窗口、100ms 步进 —— 窗长是 EBU R128 里「momentary loudness」的定义。
    每段单独过一遍 pyloudnorm 的 K-weighting 滤波器（模拟人耳对不同频率的敏感度）。

    ★ 2026-09-26：步进 250ms → 100ms，**窗长 400ms 一动不动**。
      这不是随手加密。窗长决定时间分辨率，步进只决定采样密度 ——
      400ms 的窗意味着响度在时间轴上**物理上就是 400ms 宽**，
      2 秒的窗口里只有 5 个互相独立的窗。加密步进只是让曲线更平滑，
      **不会多出一点真实细节**。
      前端会把窗宽画出来（一条 400ms 宽的带），不假装它更细。
      （同一条教训在频谱上刚栽过一次，见 SPEC_NFFT 那段注释。）

    返回**纯数组**（LUFS 值），时间由 index × hop_sec 推 ——
    省掉每点一个 {"t":..,"lufs":..} 的对象壳，2475 个点从 ~69KB 降到 ~15KB。
    """
    win = int(0.4 * sr)
    hop = int(hop_sec * sr)
    if len(mono) < win:
        return []
    meter = pyln.Meter(sr)
    out = []
    for start in range(0, len(mono) - win, hop):
        seg = mono[start:start + win]
        try:
            l = meter.integrated_loudness(seg)
        except Exception:
            continue
        if np.isfinite(l):
            out.append(round(float(l), 1))
    return out


def _band_energy(y: np.ndarray, sr: int):
    """五段频带的能量分布。

    ★ 这里必须用「功率求和」，不能用「频段内平均」。
      频率轴上每个 bin 的宽度是相同的，但各个频段包含的 bin 数量天差地别 ——
      20–60Hz 只有 ~7 个 bin，6–16kHz 有 ~1800 个。
      如果按频段取平均，低频段会被系统性放大，sub 能虚高到 50% 以上。
      求和才是「这段频率范围内一共装了多少能量」。
    """
    spec = np.abs(librosa.stft(y, n_fft=4096)) ** 2      # 功率谱
    freqs = librosa.fft_frequencies(sr=sr, n_fft=4096)
    total = float(spec.sum())
    out = []
    for name, lo, hi in BANDS:
        mask = (freqs >= lo) & (freqs < hi)
        e = float(spec[mask].sum()) if mask.any() else 0.0
        bw = max(hi - lo, 1e-9)
        out.append({
            "name": name, "lo": lo, "hi": hi,
            "energy": e,
            "share": round(e / total, 4) if total > 0 else 0.0,
            # 每 Hz 的平均功率（dB）—— 消掉带宽差异，跨频段可横向比较
            "density_db": round(float(10 * np.log10(e / bw + 1e-12)), 1),
        })
    return out


def _spectrogram(y: np.ndarray, sr: int):
    """降采样后的频谱图数据，给前端画瀑布图。

    输出 dB 值（不是线性幅度）—— 因为人耳对响度的感知是对数的，
    线性幅度画出来，除了最响的那几个频率，其他全是黑的。

    ─────────────────────────────────────────────────────────────
    ★ 2026-09-25 重写。旧版画在屏幕上「密集到没法看」，三条病根：

    【病根一】频率轴是线性的。
      旧版 0→11025Hz 均分 170 格，每格 64.6Hz。结果音乐真正有内容的
      那一段（20–3000Hz）只占画面底下 27%，上面 73% 画的是空气 ——
      大部分歌 11kHz 以上没东西。底鼓和贝斯全挤在最下面两行。
      人耳本来就是对数感知频率的，坐标轴就该是对数。

    【病根二】格内取平均而不是取峰值。
      对数频段在低频窄（30–31Hz）、高频宽（10000–10700Hz 跨 130 个 bin）。
      取平均的话，一条孤零零的泛音线会被同频段里上千个噪声 bin 稀释掉，
      画出来就没了 —— 而「有一条明确的音在这里」恰恰是最该看见的东西。
      ★ 在 dB 域上取最大值（S 在分格之前已经转成 dB 了）。

    【病根三】格子比像素还小。
      170 行塞进 188px 高的画布 = 每行 1.1px，前端还写了重叠填充，
      相邻行互相盖掉。这一条前端已经治了（画布高度 = 行数，每行整数个物理像素）。

    ─────────────────────────────────────────────────────────────
    ★ 2026-09-26 再改：时间分辨率的病根不在"格子太少"，在**窗太长**。
      算得再密，一记 30ms 的军鼓也会被 185.8ms 的观察窗摊平。
      详见文件头 SPEC_NFFT 那段 —— 结论是把窗从 4096 缩到 1024，
      hop 保持 NFFT/4 = 256。**不是靠加密格子治糊的。**

    【最近 bin 兜底 —— 窗缩短之后，这条路走得更多了】
      bin 间距 = sr / n_fft，窗从 4096 缩到 1024 之后变成 21.53Hz（原来是 5.38Hz）。
      而 30–150Hz 这一段在对数轴上被切成 70 多个频段，绝大多数窄于一个 bin
      （30–31Hz 里一个 bin 都没有）。天真实现会得到空切片 → 整条黑。
      ★ 实测这条兜底扛住了，低频**没有出现硬横条** —— 但那是插值插出来的平滑，
        不是分辨率。想真正分开 30Hz 和 31Hz 需要 1 秒的窗，
        那点信息本来就不在信号里。低两个八度变糊是物理代价，认。
    """
    D = np.abs(librosa.stft(y, n_fft=SPEC_NFFT, hop_length=SPEC_NFFT // 4))
    S = librosa.amplitude_to_db(D, ref=np.max)      # → dB，最大值为 0

    freqs = librosa.fft_frequencies(sr=sr, n_fft=SPEC_NFFT)
    fb = S.shape[0]
    nyq = float(freqs[-1])

    # 对数频段边界。geomspace = 在对数尺度上等距。
    edges = np.geomspace(SPEC_F_LO, nyq, SPEC_BINS + 1)
    cut = np.searchsorted(freqs, edges)          # 每个边界落在哪个 bin 上
    bin_hz = float(sr) / SPEC_NFFT
    centers = np.sqrt(edges[:-1] * edges[1:])    # 每个频段的几何中心频率

    rows = np.empty((SPEC_BINS, S.shape[1]), dtype=np.float32)
    for i in range(SPEC_BINS):
        a, b = int(cut[i]), int(cut[i + 1])
        if b > a:                                # 频段盖住至少一个 bin → 取最大
            rows[i] = S[a:b].max(axis=0)
        else:
            # ★ 频段比一个 bin 还窄（低频那一段有 70 多个这样的频段）。
            #   天真做法是落到最近的那个 bin —— 但那样相邻三四个频段会拿到
            #   一模一样的值，画出来是底部几条硬邦邦的横条，看着像 bug。
            #   改成在相邻两个 bin 之间线性插值：信息量一样（物理上就那么多），
            #   但视觉变成平滑的过渡，条带消失。
            p = centers[i] / bin_hz
            k0 = int(min(max(np.floor(p), 0), fb - 1))
            k1 = min(k0 + 1, fb - 1)
            w = float(min(max(p - k0, 0.0), 1.0))
            rows[i] = S[k0] * (1.0 - w) + S[k1] * w

    # 映射到 0..1（SPEC_DB_FLOOR 以下一律当 0）
    norm = np.clip((rows + SPEC_DB_FLOOR) / SPEC_DB_FLOOR, 0, 1)

    # ★ 存储：uint8，一格 1 字节。
    #   老格式是 [0.123, 0.457, ...] 这样的浮点文本，一格 6 字节；
    #   满分辨率下 239 万格 = 14MB 一首，不可接受。
    #   8 位量化一格 1 字节，量化步长 = 55dB / 255 = **0.216dB** —— 远细于人耳能分的。
    #   而且前端的色阶本来就是 256 项查表，字节直接当索引，连换算都省了。
    u8 = np.ascontiguousarray((norm * 255.0 + 0.5).astype(np.uint8))
    total = int(u8.shape[1])

    # ── 概览条：沿时间轴池化 ────────────────────────────────────
    # ★ 这里用 max 不用 mean，和详情条的做法**正好相反**。
    #   概览条是**缩小**显示（每列分不到 1 个像素），一格代表 pool 列，
    #   取峰值才留得住鼓点；取平均会把鼓点摊平，缩下去就没了。
    #   详情条相反 —— 它是放大的，一列都不能合并，合并就是丢时间点。
    pool = max(1, -(-total // SPEC_OV_COLS))        # 向上取整的整数除法
    n_ov = total // pool                            # 截到整数倍，尾巴丢掉
    ov = np.ascontiguousarray(
        u8[:, :n_ov * pool].reshape(SPEC_BINS, n_ov, pool).max(axis=2))

    chunk_cols = max(1, int(round(SPEC_CHUNK_SEC * sr / (SPEC_NFFT // 4))))

    meta = {
        "rows": SPEC_BINS,
        "cols": int(n_ov),
        # ★ 前端要用这个数把归一化值反算回 dB（频谱重心、当前频段构成都要）。
        #   它和上面的归一化必须一致，改一个就得改另一个。
        "db_floor": -SPEC_DB_FLOOR,
        # 下面三个字段是给前端画频率刻度用的。前端按
        #   f(i) = f_lo * (f_hi / f_lo) ** (i / rows)
        # 反算每一行的中心频率（i 从下往上数）。
        "log": True,
        "f_lo": round(SPEC_F_LO, 1),
        "f_hi": round(nyq, 1),
        # ★ encoding 告诉前端怎么解 data。老文件没有这个字段，
        #   读到就是浮点数组 —— 前端两条路都要认（不可能一次重跑完所有歌）。
        "encoding": "u8b64",
        # 行优先展平：第 0 行是最低频，第 0 列是曲子的开头。
        # ★ 这份是**概览条专用**（池化过的），主窗口要的详情在上面那个 u8 里，
        #   由 analyze() 按切片写文件。
        "data": base64.b64encode(ov.tobytes()).decode("ascii"),
        # ★ 详情条的全部坐标。前端靠它算「当前窗口落在哪个切片」。
        #   ⚠️ 这里的 cols 和上面那个 cols 不是一回事：
        #      s.cols        = 概览条的格子数（池化后）
        #      s.detail.cols = 整曲真实列数（未池化），时间轴换算一律用它
        #   两个都叫 cols 是要命的，前端里必须分清，别混用。
        "detail": {
            "n_fft": SPEC_NFFT,
            "hop": SPEC_NFFT // 4,
            "chunk_sec": SPEC_CHUNK_SEC,
            "chunk_cols": chunk_cols,
            "chunks": -(-total // chunk_cols),
            "cols": total,
        },
    }
    return meta, u8


def _write_spec_chunks(u8, out_dir: str, stem: str, chunk_cols: int) -> int:
    """把整曲频谱按 chunk_cols 列切成二进制文件，给前端按需取用。

    ★ 为什么落成文件，而不是 JSON 里多塞一段 base64：
      整曲 21324 列 × 448 行 = 954 万格。base64 进 JSON 是 12.7MB 一坨，
      浏览器光 JSON.parse 就要好几秒，而且**每次换歌都得整坨解析完**——
      可用户一次只看 8 秒。切成 32 秒一块之后，一帧只要 1.2MB 里的那一块。

    ★ 每次重跑先清空目录。参数一变列数就变，留着旧切片就会出现
      「新 JSON 配旧切片」—— 两边列数对不上，图上时间是错位的。
      这种错不会报错，只会安静地画错，最难查。宁可每次都重建。

    ★ 原始字节，不压缩不编码。前端 fetch 回来就是 arrayBuffer，
      直接 new Uint8Array 就能当查表下标用 —— 一格 1 字节，零转换。
    """
    d = os.path.join(out_dir, stem + ".spec")
    if os.path.isdir(d):
        shutil.rmtree(d)
    os.makedirs(d, exist_ok=True)

    n = int(u8.shape[1])
    for i, a in enumerate(range(0, n, chunk_cols)):
        blk = np.ascontiguousarray(u8[:, a:a + chunk_cols])
        with open(os.path.join(d, f"{i:03d}.bin"), "wb") as f:
            f.write(blk.tobytes())
    return -(-n // chunk_cols)


def _detect_key(chroma: np.ndarray):
    """用 Krumhansl-Schmuckler 算法猜调性。

    ★ 这是 estimated —— 猜的。所以要把候选和置信度一起交出去，不能只报一个答案。
    做法：把整首歌的音级能量做成一个 12 维向量，跟 24 个调式模板（12 大调 + 12 小调）
    逐一算相关系数，最高的那个就是猜测结果。
    """
    profile = chroma.mean(axis=1)
    if profile.sum() <= 0:
        return None
    profile = profile / profile.sum()

    scores = []
    for i in range(12):
        for mode, tpl in (("major", KS_MAJOR), ("minor", KS_MINOR)):
            rolled = np.roll(tpl, i)
            r = float(np.corrcoef(profile, rolled)[0, 1])
            scores.append((r, i, mode))
    scores.sort(reverse=True)

    best_r, best_i, best_mode = scores[0]
    # 置信度：第一名跟第二名差多少。差得越多越有把握。
    gap = best_r - scores[1][0] if len(scores) > 1 else 0.0
    conf = float(np.clip(gap * 4, 0, 1))

    return {
        "tonic": NOTE_NAMES[best_i],
        "mode": best_mode,
        "label": f"{NOTE_NAMES[best_i]} {best_mode}",
        "correlation": round(best_r, 3),
        "confidence": round(conf, 3),
        "candidates": [
            {"label": f"{NOTE_NAMES[i]} {m}", "corr": round(r, 3)}
            for r, i, m in scores[:5]
        ],
    }


# ── 主流程 ───────────────────────────────────────────────────────────

def analyze(path: str, out_dir: str | None = None) -> dict:
    t0 = time.time()
    name = os.path.basename(path)
    stem = os.path.splitext(name)[0]
    print(f"  读 {name} …")

    info = sf.info(path)
    data, sr = sf.read(path, always_2d=True)      # 原始采样率、原始声道
    channels = data.shape[1]
    duration = len(data) / sr

    # 响度和真峰值必须在原始信号上算 —— 重采样会改变峰值。
    mono_native = data.mean(axis=1)
    meter = pyln.Meter(sr)
    try:
        lufs = float(meter.integrated_loudness(mono_native))
    except Exception:
        lufs = float("nan")

    # 样本峰值（精确）。注意：这是「样本峰值」，严格意义上的「真峰值」还要考虑
    # 重采样后的过冲（inter-sample peak），这里先给精确的样本值，不冒充真峰值。
    peak = float(np.max(np.abs(data))) if data.size else 0.0
    peak_db = float(20 * np.log10(peak)) if peak > 0 else -np.inf

    # 立体声宽度：左右声道的相关系数。1.0 = 完全单声道，0 = 完全独立。
    if channels >= 2:
        l, r = data[:, 0], data[:, 1]
        corr = float(np.corrcoef(l, r)[0, 1]) if l.std() > 0 and r.std() > 0 else 1.0
    else:
        corr = 1.0

    # 分析用的单声道 + 降采样版本
    mono = mono_native
    asr = sr
    if asr > ANALYSIS_SR:
        mono = librosa.resample(mono, orig_sr=sr, target_sr=ANALYSIS_SR)
        asr = ANALYSIS_SR

    print(f"  波形 / 响度 …")
    peaks = _downsample_peaks(mono, WAVE_POINTS)
    loud_curve = _loudness_curve(mono, asr)

    # LRA：用响度曲线算。真正的 EBU R128 LRA 带门限（丢掉太轻的段落），
    # 这里用简化的 10%–95% 分位差，所以 method 标 measured_simplified 不冒充标准值。
    if loud_curve:
        vals = np.array(loud_curve)
        lra = float(np.percentile(vals, 95) - np.percentile(vals, 10))
    else:
        lra = 0.0

    print(f"  频谱 …")
    bands = _band_energy(mono, asr)
    spec, spec_u8 = _spectrogram(mono, asr)

    print(f"  速度 / 调性（这两项是估的）…")
    # BPM
    try:
        tempo, beats = librosa.beat.beat_track(y=mono, sr=asr)
        bpm = float(np.atleast_1d(tempo)[0])
        beat_times = [round(float(t), 3) for t in librosa.frames_to_time(beats, sr=asr)]
    except Exception:
        bpm, beat_times = 0.0, []

    # 调性
    try:
        chroma = librosa.feature.chroma_cqt(y=mono, sr=asr)
        key = _detect_key(chroma)
    except Exception:
        key = None

    result = {
        "id": stem,
        "file": name,
        "analyzed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_sec": round(time.time() - t0, 2),

        "meta": {
            "method": "measured",
            "duration": round(duration, 2),
            "sample_rate": sr,
            "channels": channels,
            "format": info.format,
            "subtype": info.subtype,
            "size_bytes": os.path.getsize(path),
            "analysis_sample_rate": asr,
        },

        "waveform": {
            "method": "measured",
            "points": len(peaks),
            "peaks": peaks,
        },

        "loudness": {
            "method": "measured",
            "integrated_lufs": round(lufs, 1) if np.isfinite(lufs) else None,
            "sample_peak_db": round(peak_db, 2) if np.isfinite(peak_db) else None,
            "lra_lu": round(lra, 1),
            "lra_note": "简化算法（10%–95% 分位差），不等于标准 EBU R128 LRA",
            # ★ 曲线是**纯数组**，第 i 个点的时刻 = i × hop_sec（不是对象数组）。
            #   win_sec 一并给出：那是这条曲线**物理上的时间分辨率**，
            #   前端用它画带宽，免得在窄窗口里把它当成比实际更细的东西。
            "curve": loud_curve,
            "hop_sec": 0.1,
            "win_sec": 0.4,
        },

        "stereo": {
            "method": "measured",
            "correlation": round(corr, 3),
            "note": "1.0 = 完全单声道，0 = 左右完全独立，负数 = 相位有问题",
        },

        "spectrum": {
            "method": "measured",
            "bands": bands,
            **spec,
        },

        "tempo": {
            "method": "estimated",          # ★ 猜的
            "bpm": round(bpm, 1),
            "beats": beat_times[:400],
            "note": "librosa beat tracker 推断，复杂编曲可能错",
        },

        "key": ({**key, "method": "estimated"} if key else None),   # ★ 猜的
    }

    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        dst = os.path.join(out_dir, stem + ".json")
        with open(dst, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)

        n_chunk = _write_spec_chunks(spec_u8, out_dir, stem,
                                     spec["detail"]["chunk_cols"])
        print(f"  → {dst}  （{result['elapsed_sec']}s，频谱 {spec['detail']['cols']} 列"
              f" → {n_chunk} 片）")

    return result


def main():
    ap = argparse.ArgumentParser(description="MusicLab 音频分析")
    ap.add_argument("files", nargs="+", help="音频文件")
    ap.add_argument("--out", default="analysis", help="JSON 输出目录")
    args = ap.parse_args()

    for p in args.files:
        if not os.path.isfile(p):
            print(f"  跳过（找不到）：{p}")
            continue
        try:
            analyze(p, args.out)
        except Exception as e:
            print(f"  ✗ {os.path.basename(p)}: {type(e).__name__}: {e}")
            if os.environ.get("MUSICLAB_TRACE"):
                raise


if __name__ == "__main__":
    main()
