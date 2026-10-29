#!/usr/bin/env python3
"""批量分轨：media/*.flac → demucs 6 轨 → 逐帧能量 + 入场检测。

两阶段：
    阶段 1  一次把所有文件喂给 demucs（★ 模型只加载一次，省掉每首十几秒）
    阶段 2  逐首跑 analyze_stems，产出 analysis/<id>.stems.json

★ 已经跑过的会跳过（看 stems/<模型>/<id>/ 在不在），所以中断了直接重跑。

跑：
    .venv/bin/python tools/batch_stems.py
    .venv/bin/python tools/batch_stems.py --limit 3
"""
import argparse
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

MEDIA = os.path.join(ROOT, "media")
STEMS = os.path.join(ROOT, "stems")
OUT = os.path.join(ROOT, "analysis")
MODEL = "htdemucs_6s"
DEVICE = "mps"          # M5 的 GPU；测过能跑通
AUDIO_EXT = (".flac", ".wav")

STEM_NAMES = ["vocals", "drums", "bass", "guitar", "piano", "other"]


def have_stems(tid: str) -> bool:
    """这套 stem 是不是齐的。缺一条就算没跑过 —— 宁重跑不冒险。"""
    d = os.path.join(STEMS, MODEL, tid)
    return all(os.path.isfile(os.path.join(d, n + ".wav")) for n in STEM_NAMES)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", default="", help="只跑文件名含这个串的")
    args = ap.parse_args()

    names = sorted(n for n in os.listdir(MEDIA)
                   if n.lower().endswith(AUDIO_EXT) and not n.startswith("."))
    if args.only:
        names = [n for n in names if args.only.lower() in n.lower()]
    if args.limit:
        names = names[: args.limit]

    todo = [n for n in names if not have_stems(os.path.splitext(n)[0])]
    print(f"  media/ 共 {len(names)} 首，待分离 {len(todo)} 首"
          f"（已完成 {len(names) - len(todo)}）\n", flush=True)
    if not todo:
        print("  没什么要做的。")
        return

    # ── 阶段 1：demucs 批量 ──
    t0 = time.time()
    print(f"  ── 阶段 1/2：分离 ──", flush=True)
    cmd = [os.path.join(ROOT, ".venv/bin/python"), "-m", "demucs",
           "-n", MODEL, "-d", DEVICE, "-o", STEMS] + \
          [os.path.join(MEDIA, n) for n in todo]
    print(f"  {' '.join(cmd[:8])} … (+{len(todo)} 个文件)\n", flush=True)
    r = subprocess.run(cmd, cwd=ROOT)
    if r.returncode != 0:
        print(f"\n  ✗ demucs 退出码 {r.returncode} —— 已跑出来的继续分析。", flush=True)
    t_split = time.time() - t0
    print(f"\n  ── 分离耗时 {t_split / 60:.1f} 分钟 ──\n", flush=True)

    # ── 阶段 2：逐首分析 ──
    print(f"  ── 阶段 2/2：逐帧能量 + 入场检测 ──", flush=True)
    import analyze_stems
    import json

    ok, bad = 0, []
    for i, n in enumerate(todo, 1):
        tid = os.path.splitext(n)[0]
        dst = os.path.join(OUT, tid + ".stems.json")
        d = os.path.join(STEMS, MODEL, tid)
        if not have_stems(tid):
            print(f"  [{i}/{len(todo)}] ⏭ {tid[:48]}（stem 不全，跳过）", flush=True)
            bad.append((tid, "stem 不全"))
            continue
        if os.path.isfile(dst):
            print(f"  [{i}/{len(todo)}] ⏭ {tid[:48]}（已分析）", flush=True)
            continue
        try:
            res = analyze_stems.analyze_dir(d, tid)
            with open(dst, "w", encoding="utf-8") as f:
                json.dump(res, f, ensure_ascii=False)
            top = res["stems"]["tracks"][0]
            print(f"  [{i}/{len(todo)}] ✅ {tid[:40]:<42} "
                  f"最活跃：{top['label']}({top['segments']}段)", flush=True)
            ok += 1
        except Exception as e:
            print(f"  [{i}/{len(todo)}] ❌ {tid[:48]} {type(e).__name__}: {e}", flush=True)
            bad.append((tid, str(e)))

    print(f"\n{'=' * 64}")
    print(f"  完成  ✅ {ok} 成功  ❌ {len(bad)} 失败"
          f"   总耗时 {(time.time() - t0) / 60:.1f} 分钟")
    for t, e in bad:
        print(f"    - {t[:56]}  {e[:60]}")
    du = subprocess.run(["du", "-sh", STEMS], capture_output=True, text=True)
    print(f"  stem 占用：{du.stdout.strip()}")


if __name__ == "__main__":
    main()
