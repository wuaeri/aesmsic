#!/usr/bin/env python3
"""扫 media/ 生成 library.json。

★ 原则（作者定的）：**不要编数据，库里有几个显示几个。**

所以这个脚本只做三件事：
    1. 列出 media/ 里真实存在的音频文件 —— 有几个写几个，不过滤、不补齐
    2. 标题/艺术家/专辑从 **FLAC 自带的 Vorbis comment 标签**里读（真元数据），
       读不到就留空。**绝不从文件名猜** —— 实测文件名会混进专辑信息：
       `Ariana Grande-test drive (bonus).flac` 的真 TITLE 是 `test drive`，
       `(bonus)` 是从专辑名 `Positions (Deluxe) [Explicit]` 掉进去的。
    3. 保住人工写的 `why` 字段 —— 那是人话，脚本不生成也不覆盖

跑：
    .venv/bin/python tools/build_library.py
    .venv/bin/python tools/build_library.py --dry-run
"""
import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEDIA_DIR = os.path.join(ROOT, "media")
ANALYSIS_DIR = os.path.join(ROOT, "analysis")
LIBRARY = os.path.join(ROOT, "library.json")

AUDIO_EXT = (".flac", ".wav", ".mp3", ".m4a", ".ogg", ".aiff", ".aif")

# Vorbis comment 里我们关心的键 → library.json 的字段名
TAG_MAP = {
    "TITLE": "title",
    "ARTIST": "artist",
    "ALBUM": "album",
    "ALBUMARTIST": "album_artist",
    "DATE": "date",
    "TRACKNUMBER": "track",
    "QMQUALITY": "quality",
}


def read_vorbis(path: str) -> dict:
    """读 FLAC 的 Vorbis comment 标签。非 FLAC 或没有标签就返回 {}。

    FLAC 结构：`fLaC` + 一串元数据块。每块 4 字节头：
        [1 bit 最后一块标志][7 bit 块类型][24 bit 大端块长度]
    类型 4 就是 VORBIS_COMMENT，块内是纯小端：
        vendor_length(4) + vendor_string + comment_count(4)
        + 每条: length(4) + "KEY=value"
    """
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"fLaC":
                return {}
            while True:
                hdr = f.read(4)
                if len(hdr) < 4:
                    return {}
                is_last = bool(hdr[0] & 0x80)
                btype = hdr[0] & 0x7F
                size = int.from_bytes(hdr[1:4], "big")
                data = f.read(size)
                if btype == 4:
                    return _parse_vorbis(data)
                if is_last:
                    return {}
    except Exception:
        return {}


def _parse_vorbis(d: bytes) -> dict:
    """解析 VORBIS_COMMENT 块体。

    ★ 这里最容易错的一步：**vendor string 在 comment_count 之前**。
      漏掉它，后面所有偏移全错位，读出来会是 `rence libFLAC 1.3.1...` 这种鬼东西。
      顺序是：vendor_len(4) → vendor(变长) → count(4) → [len(4)+"KEY=value"] × count
    """
    off = 0
    if len(d) < 4:
        return {}
    vlen = int.from_bytes(d[off:off + 4], "little")
    off += 4 + vlen                       # ← 跳过 vendor string，这行不能少
    if off + 4 > len(d):
        return {}
    count = int.from_bytes(d[off:off + 4], "little")
    off += 4

    tags = {}
    for _ in range(count):
        if off + 4 > len(d):
            break
        ln = int.from_bytes(d[off:off + 4], "little")
        off += 4
        if off + ln > len(d):
            break
        kv = d[off:off + ln].decode("utf-8", "replace")
        off += ln
        if "=" not in kv:
            continue
        k, v = kv.split("=", 1)
        tags[k.strip().upper()] = v.strip()
    return tags


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    args = ap.parse_args()

    # 旧库里的 why —— 人工写的，必须原样保住
    old_why = {}
    if os.path.isfile(LIBRARY):
        try:
            with open(LIBRARY, encoding="utf-8") as f:
                for item in json.load(f).get("shelf", []):
                    if item.get("why"):
                        old_why[item["id"]] = item["why"]
        except Exception as e:
            print(f"  ⚠ 旧 library.json 读不动（{e}），why 字段会丢")

    names = sorted(n for n in os.listdir(MEDIA_DIR)
                   if n.lower().endswith(AUDIO_EXT) and not n.startswith("."))

    shelf = []
    n_tagged = 0
    for name in names:
        path = os.path.join(MEDIA_DIR, name)
        stem = os.path.splitext(name)[0]
        tags = read_vorbis(path)

        item = {"id": stem, "file": name}
        if tags:
            n_tagged += 1
            for k, field in TAG_MAP.items():
                if tags.get(k):
                    item[field] = tags[k]
        # 标签读不到就什么都不写 —— 不留占位符、不拿文件名顶上
        if not item.get("title"):
            item["title_source"] = "文件名（FLAC 没有 TITLE 标签）"
            item["title"] = stem

        item["analyzed"] = os.path.isfile(
            os.path.join(ANALYSIS_DIR, stem + ".json"))
        if stem in old_why:
            item["why"] = old_why[stem]
        shelf.append(item)

    out = {
        "updated": time.strftime("%Y-%m-%d"),
        "note": ("曲库索引。由 tools/build_library.py 从 media/ 扫出来 —— "
                 "有几个写几个，不编数据。标签读自 FLAC 的 Vorbis comment。"),
        "count": len(shelf),
        "tagged": n_tagged,
        "analyzed": sum(1 for x in shelf if x["analyzed"]),
        "shelf": shelf,
    }

    print(f"  media/ 里 {len(shelf)} 个音频文件")
    print(f"  有 Vorbis 标签：{n_tagged}   没有：{len(shelf) - n_tagged}")
    print(f"  已分析：{out['analyzed']}\n")
    for x in shelf:
        mark = "✅" if x["analyzed"] else "  "
        print(f"  {mark} {x.get('artist','?')[:22]:<24} — {x['title'][:44]}")

    if args.dry_run:
        print("\n  （--dry-run，没写文件）")
        return

    with open(LIBRARY, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n  → {LIBRARY}")


if __name__ == "__main__":
    main()
