#!/usr/bin/env python3
"""把 37 首分析结果里的关键字段汇总成一个索引。

【为什么要有这个文件】
  进入页要列全部 37 首，每首要带 BPM / 调性 / 时长 / 响度。
  直接读 37 个分析 JSON 得拖 **120MB 以上**进浏览器 ——
  每个 JSON 里 3.2MB 是频谱概览的 base64，而那点数据在列表里一个像素都用不上。
  汇总之后整个索引 6KB 左右，一次请求拿全。

【这是派生数据，不是分析结果】
  它随分析结果变。**每次重跑 analyze.py 之后要重新生成一次**：
      python3 build_index.py

  它自己不做任何计算 —— 只是把已有的字段搬过来，不编数。
"""

import json
import os

ROOT = os.path.dirname(os.path.abspath(__file__))
# 这三个都能用环境变量顶掉 —— pipeline.py 的测试和以后的多曲库都靠这个，
# 不然一跑测试就会动到真索引。
ANALYSIS = os.environ.get("MUSICLAB_ANALYSIS") or os.path.join(ROOT, "analysis")
LIBRARY = os.environ.get("MUSICLAB_LIBRARY") or os.path.join(ROOT, "library.json")
OUT = os.path.join(ANALYSIS, "_index.json")


def main():
    with open(LIBRARY, encoding="utf-8") as f:
        lib = json.load(f)

    tracks = []
    for item in lib["shelf"]:
        p = os.path.join(ANALYSIS, item["id"] + ".json")
        if not os.path.isfile(p):
            continue                       # 没分析过的就跳过，不占位、不编数
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        k = d.get("key") or {}
        tracks.append({
            "id":      d["id"],
            "title":   item.get("title") or d["id"],
            "artist":  item.get("artist") or "",
            "album":   item.get("album") or "",
            "quality": item.get("quality") or "",
            "dur":     round(d["meta"]["duration"], 2),
            "sr":      d["meta"]["sample_rate"],
            "subtype": d["meta"]["subtype"],
            "bpm":     d["tempo"]["bpm"],
            "key":     k.get("label"),
            "key_conf": k.get("confidence"),
            "lufs":    d["loudness"]["integrated_lufs"],
            "peak_db": d["loudness"]["sample_peak_db"],
            "lra":     d["loudness"]["lra_lu"],
            "corr":    d["stereo"]["correlation"],
        })

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"count": len(tracks), "tracks": tracks}, f,
                  ensure_ascii=False, separators=(",", ":"))

    print(f"  → analysis/_index.json   {len(tracks)} 首 · "
          f"{os.path.getsize(OUT) / 1024:.1f} KB")


if __name__ == "__main__":
    main()
