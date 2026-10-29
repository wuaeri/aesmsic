#!/usr/bin/env python3
"""批量跑主分析（analyze.py）。扫 media/，跳过已经分析过的。

★ 为什么要有这个脚本，而不是直接 `analyze.py media/*.flac`：
  这个库里的文件名带逗号、括号、弯引号、韩文组合字符、日文，
  而且真实名字会被终端显示**截断**。今天已经栽过一次 ——
  照着一行被截断的日志去手打路径，结果文件根本不存在。
  **文件名一律由 os.listdir 交给程序，不经过人的手，也不从日志里抄。**

跑：
    .venv/bin/python tools/batch_analyze.py
    .venv/bin/python tools/batch_analyze.py --redo   # 连已有的也重跑
"""
import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import analyze  # noqa: E402

MEDIA = os.path.join(ROOT, "media")
OUT = os.path.join(ROOT, "analysis")
EXT = (".flac", ".wav", ".mp3", ".m4a", ".aiff")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--redo", action="store_true", help="已有的也重跑")
    args = ap.parse_args()

    files = sorted(n for n in os.listdir(MEDIA)
                   if n.lower().endswith(EXT) and not n.startswith("."))
    print(f"  media/ 共 {len(files)} 个音频文件\n", flush=True)

    ok = skip = 0
    bad = []
    t0 = time.time()
    for i, n in enumerate(files, 1):
        tid = os.path.splitext(n)[0]
        dst = os.path.join(OUT, tid + ".json")
        if not args.redo and os.path.isfile(dst):
            print(f"  [{i:>2}/{len(files)}] ⏭ {tid[:52]}", flush=True)
            skip += 1
            continue
        try:
            r = analyze.analyze(os.path.join(MEDIA, n), OUT)
            print(f"  [{i:>2}/{len(files)}] ✅ {tid[:44]:<46} {r['elapsed_sec']:>6.1f}s",
                  flush=True)
            ok += 1
        except Exception as e:
            print(f"  [{i:>2}/{len(files)}] ❌ {tid[:44]}  {type(e).__name__}: {e}",
                  flush=True)
            bad.append((tid, str(e)))

    print(f"\n{'=' * 64}")
    print(f"  ✅ {ok} 新增   ⏭ {skip} 跳过   ❌ {len(bad)} 失败"
          f"   共 {(time.time() - t0) / 60:.1f} 分钟")
    for t, e in bad:
        print(f"    - {t[:56]}  {e[:60]}")


if __name__ == "__main__":
    main()
