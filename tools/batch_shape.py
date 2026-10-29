#!/usr/bin/env python3
"""批量声学画像：stems/<模型>/<id>/ → analysis/<id>.shape.json

★ 已跑过的会跳过（看 .shape.json 在不在），中断了直接重跑。

跑：
    .venv/bin/python tools/batch_shape.py
    .venv/bin/python tools/batch_shape.py --only poly     # 只跑名字含这个串的
    .venv/bin/python tools/batch_shape.py --redo          # 全部重跑
"""
import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

from analyze_shape import analyze_dir, STEM_ORDER   # noqa: E402

STEMS = os.path.join(ROOT, "stems")
OUT = os.path.join(ROOT, "analysis")
MODEL = "htdemucs_6s"


def have_stems(d):
    return all(os.path.isfile(os.path.join(d, n + ".wav")) for n in STEM_ORDER)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--redo", action="store_true")
    args = ap.parse_args()

    base = os.path.join(STEMS, MODEL)
    ids = sorted(n for n in os.listdir(base)
                 if have_stems(os.path.join(base, n)))
    if args.only:
        ids = [i for i in ids if args.only.lower() in i.lower()]
    if args.limit:
        ids = ids[: args.limit]

    todo = [i for i in ids
            if args.redo or not os.path.isfile(os.path.join(OUT, i + ".shape.json"))]
    print(f"  {len(ids)} 首，待跑 {len(todo)} 首"
          f"（已完成 {len(ids) - len(todo)}）\n", flush=True)
    if not todo:
        print("  没什么要做的。")
        return

    t0 = time.time()
    ok, bad = 0, []
    for k, tid in enumerate(todo, 1):
        try:
            res = analyze_dir(os.path.join(base, tid), tid, quiet=True)
            with open(os.path.join(OUT, tid + ".shape.json"), "w",
                      encoding="utf-8") as f:
                json.dump(res, f, ensure_ascii=False)
            tr = sorted(res["shape"]["tracks"],
                        key=lambda t: -(t["summary"]["decay"] or 0))
            top = tr[0]
            print(f"  [{k:>2}/{len(todo)}] ✅ {tid[:36]:<38} 掉最快："
                  f"{top['label']}({top['summary']['decay']}dB)", flush=True)
            ok += 1
        except Exception as e:
            print(f"  [{k:>2}/{len(todo)}] ❌ {tid[:38]} "
                  f"{type(e).__name__}: {e}", flush=True)
            bad.append((tid, str(e)))

    print(f"\n{'=' * 60}")
    print(f"  完成  ✅ {ok}   ❌ {len(bad)}   {(time.time() - t0) / 60:.1f} 分钟")
    for t, e in bad:
        print(f"    - {t[:56]}  {e[:60]}")


if __name__ == "__main__":
    main()
