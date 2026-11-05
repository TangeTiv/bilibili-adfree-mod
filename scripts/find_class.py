#!/usr/bin/env python3
"""在 APK 里定位类所在的 dex，并比对汇编前后的类数。

为什么需要它
------------
35 个 dex，要改一个类，第一件事是知道它在哪个 dex。没有这个工具，agent 只有两条路：
把 35 个 dex 全反汇编（十几 GB、几十分钟），或者猜——猜错的代价是一整轮白跑。

另外 SKILL.md §5.4 要求"用类数比对判断是否有类丢了"，但原来没有任何工具能做这件事。

用法
----
    # 类在哪几个 dex（按类描述符精确匹配，不会误中同前缀的类）
    python3 scripts/find_class.py base.apk --class com.bilibili.pegasus.api.k

    # 每个 dex 的类数（基线，改之前先存一份）
    python3 scripts/find_class.py base.apk --counts

    # 汇编后比对：重新打包前确认类数没变
    python3 scripts/find_class.py base.apk --entry classes14.dex \
        --compare /work/out/classes14.dex

退出码：0 = 正常；1 = 找不到类 / 类数不一致。
"""

from __future__ import annotations

import argparse
import struct
import sys

try:  # 非 UTF-8 控制台（Windows 中文 GBK 等）兜底：个别符号降级为 ?，而不是抛 UnicodeEncodeError
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass
import zipfile

DEX_MAGIC = b"dex\n"
CLASS_DEFS_SIZE_OFF = 0x60  # u4，dex 头里 class_defs_size 的位置


def dex_class_count(data: bytes) -> int | None:
    """只读 dex 头，拿 class_defs_size。不解压、不反汇编，毫秒级。"""
    if len(data) < 0x70 or not data.startswith(DEX_MAGIC):
        return None
    return struct.unpack_from("<I", data, CLASS_DEFS_SIZE_OFF)[0]


def iter_dex(apk: str):
    """按 classes.dex, classes2.dex … classes35.dex 的自然顺序产出 (entry, bytes)。"""
    with zipfile.ZipFile(apk) as z:
        names = [n for n in z.namelist() if n.startswith("classes") and n.endswith(".dex")]

        def key(n: str) -> int:
            mid = n[len("classes"):-len(".dex")]
            return int(mid) if mid.isdigit() else 1

        for name in sorted(names, key=key):
            yield name, z.read(name)


def find_class(apk: str, query: str) -> int:
    # 归一化：接受 a.b.C / La/b/C; / a/b/C
    q = query.strip()
    if q.startswith("L") and q.endswith(";"):
        q = q[1:-1]
    desc = ("L" + q.replace(".", "/") + ";").encode()

    hits = []
    for entry, data in iter_dex(apk):
        n = data.count(desc)
        if n:
            hits.append((entry, n))

    if not hits:
        print(f"未找到 {q}（按描述符 {desc.decode()} 精确匹配）", file=sys.stderr)
        print("提示：混淆类名每个版本都会变。先 --counts 看规模，再按 SKILL §4.1 的锚点"
              "（UI resource-id / 字符串 / 字段）反查，不要凭上一版的类名硬找。", file=sys.stderr)
        return 1

    for entry, n in hits:
        print(f"{entry}  （命中 {n} 次）")
    if len(hits) > 1:
        print("\n注意：命中多个 dex。改写前确认哪个是真正的实现类——"
              "多 dex 里出现同名描述符通常是重复打包，改错那个不会生效。", file=sys.stderr)
    return 0


def counts(apk: str) -> int:
    total = 0
    for entry, data in iter_dex(apk):
        c = dex_class_count(data)
        if c is None:
            print(f"{entry}: 不是合法 dex", file=sys.stderr)
            continue
        total += c
        print(f"{entry:<16} {c:>7} 类  {len(data):>10,} 字节")
    print(f"\n合计 {total} 类")
    return 0


def compare(apk: str, entry: str, path: str) -> int:
    with zipfile.ZipFile(apk) as z:
        if entry not in z.namelist():
            print(f"APK 里没有 {entry}", file=sys.stderr)
            return 1
        before = dex_class_count(z.read(entry))
    with open(path, "rb") as fh:
        after = dex_class_count(fh.read())

    if before is None or after is None:
        print("有一方不是合法 dex，无法比对", file=sys.stderr)
        return 1

    same = before == after
    print(f"{entry}: 原始 {before} 类 -> 汇编后 {after} 类  {'一致 OK' if same else '不一致 MISMATCH'}")
    if not same:
        print(f"差 {after - before:+d} 类。类数变化通常意味着 smali 改坏了（而不是「正常」），"
              "不要拿体积判断，也不要把这个 dex 打进包里。", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="定位类所在 dex / 比对汇编前后类数")
    ap.add_argument("apk", help="APK 路径")
    ap.add_argument("--class", dest="cls", help="要查的类，如 com.bilibili.pegasus.api.k")
    ap.add_argument("--counts", action="store_true", help="列出每个 dex 的类数")
    ap.add_argument("--entry", help="--compare 时对应的 dex 条目名，如 classes14.dex")
    ap.add_argument("--compare", help="汇编后的 dex 文件，与 APK 里同名条目比类数")
    args = ap.parse_args()

    try:
        if args.cls:
            return find_class(args.apk, args.cls)
        if args.counts:
            return counts(args.apk)
        if args.compare:
            if not args.entry:
                ap.error("--compare 需要同时给 --entry")
            return compare(args.apk, args.entry, args.compare)
    except (OSError, zipfile.BadZipFile) as exc:
        print(f"读取失败: {exc}", file=sys.stderr)
        return 1

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
