#!/usr/bin/env python3
"""开工前预检：把"能静默失败的环境前提"变成会报错的判据。

为什么需要它
------------
这套流程里有三个前提，任何一个不成立都不会报错，只会让人在几小时后才发现白做：

1. **adb 不在 PATH**（Windows 上很常见）——REA 的每条命令都会失败，但报错像是
   "能力不可用"，不像"你没配路径"。
2. **装的是分包（split APK）**——`pm path` 返回 base + split_config.* 多行。
   只改 base.apk 对 split 里的 dex/so 完全无效，而且不会报错。
3. **端上不是你以为的那份包**——上一轮留下的改包、或商店刚推送的版本。
   照字面 `pull-adb-package` 会把改包当"干净副本"，后面全错。

另外这个脚本还会按包规模给出 JADX 堆大小建议：B 站 9.18.0 有 34 万个类，
默认 4 GB 堆必 OOM，加载本身还要 2 分钟以上（默认操作上限 120 秒）。

用法
----
    python3 scripts/preflight.py --package tv.danmaku.bili
    python3 scripts/preflight.py --package tv.danmaku.bili --apk base.apk

退出码：0 = 全部通过；1 = 有 FAIL（不要往下走）。
"""

from __future__ import annotations

import argparse
import os
import shutil
import struct
import subprocess
import sys
import zipfile

COMMON_ADB = [
    r"D:\Android\Sdk\platform-tools\adb.exe",
    r"C:\Android\Sdk\platform-tools\adb.exe",
    os.path.expanduser("~/Library/Android/sdk/platform-tools/adb"),
    os.path.expanduser("~/Android/Sdk/platform-tools/adb"),
    "/usr/lib/android-sdk/platform-tools/adb",
]

results: list[tuple[bool, str, str]] = []   # (ok, 名称, 详情)


def add(ok: bool, name: str, detail: str) -> None:
    results.append((ok, name, detail))


def find_adb(explicit: str | None) -> str | None:
    for cand in (explicit, os.environ.get("REA_ADB_PATH")):
        if cand and os.path.isfile(cand):
            return cand
    found = shutil.which("adb")
    if found:
        return found
    for cand in COMMON_ADB:
        if os.path.isfile(cand):
            return cand
    return None


def run(adb: str, *args: str, timeout: int = 60) -> str:
    try:
        p = subprocess.run([adb, *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return p.stdout
    except subprocess.TimeoutExpired:
        return ""


def dex_class_count(data: bytes) -> int | None:
    if len(data) < 0x70 or not data.startswith(b"dex\n"):
        return None
    return struct.unpack_from("<I", data, 0x60)[0]


def inspect_apk(path: str) -> tuple[list[tuple[str, int]], str | None]:
    """返回 ([(dex 条目, 类数)], 打包进 APK 的 abi 目录名或 None)。"""
    dexes, abi = [], None
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if name.startswith("classes") and name.endswith(".dex"):
                dexes.append((name, dex_class_count(z.read(name)) or 0))
            elif name.startswith("lib/") and name.count("/") >= 2:
                candidate = name.split("/")[1]
                abi = abi or candidate
    return dexes, abi


def main() -> int:
    ap = argparse.ArgumentParser(description="改造前的环境与设备预检")
    ap.add_argument("--package", default="tv.danmaku.bili")
    ap.add_argument("--apk", help="准备作为干净副本的本地 APK（可选，给了就一起校验）")
    ap.add_argument("--adb", help="adb 路径")
    ap.add_argument("--serial", help="目标设备 serial")
    args = ap.parse_args()

    device_size: int | None = None   # 端上 base.apk 的字节数，用于和本地副本比对

    # 1) 工具链
    adb = find_adb(args.adb)
    if adb:
        add(True, "adb", adb)
    else:
        add(False, "adb", "找不到。二选一：--adb <路径> 或 export REA_ADB_PATH=<路径>；"
                          "REA 的 inspect-adb-client 也读这个变量")
    add(bool(shutil.which("java")), "java", shutil.which("java") or "不在 PATH，baksmali/smali 会跑不起来")
    add(sys.version_info >= (3, 9), "python", f"{sys.version.split()[0]}（需 3.9+）")

    # REA 是必需工具链：侦察阶段靠它读反编译后的实现、追调用方。
    # 缺了不算"能力不可用"，而是环境没准备好——直接 FAIL，别让 agent 用 grep 硬扛。
    rea_bin = shutil.which("rea")
    if rea_bin:
        add(True, "rea", rea_bin)
    else:
        node = shutil.which("node")
        add(False, "rea",
            "未安装。先装 REA 再开始：npx -y rea-agents@latest setup"
            + ("" if node else "（另需 Node.js，当前 PATH 里没有 node）")
            + "；装完用 rea --version 确认")

    signer = os.environ.get("UBER_APK_SIGNER_JAR") or ""
    # 只有 --sign 才需要它，缺了不算阻塞；但没设变量时 agent 很容易以为"没有这个工具"
    add(True, "uber-apk-signer",
        signer if signer else "未设 UBER_APK_SIGNER_JAR（仅在要签名时需要；"
                              "重建 dex 这一步用不到）")

    # 2) 设备
    if adb:
        devices = run(adb, "devices")
        ready = [ln.split("\t")[0] for ln in devices.splitlines()[1:]
                 if "\t" in ln and ln.split("\t")[1].strip() == "device"]
        if args.serial:
            add(args.serial in ready, "设备", f"{args.serial} {'在线' if args.serial in ready else '不在 device 状态'}")
            serial = args.serial
        elif len(ready) == 1:
            add(True, "设备", f"{ready[0]}（已自动选中）")
            serial = ready[0]
        else:
            add(False, "设备", f"需要 --serial 明确指定，当前在线: {ready or '无'}")
            serial = ready[0] if ready else None

        if serial:
            # 3) 分包判据 —— 这条不通过就别往下做
            paths = [ln.replace("package:", "").strip()
                     for ln in run(adb, "shell", "pm", "path", args.package).splitlines()
                     if ln.startswith("package:")]
            if not paths:
                add(False, "已安装", f"设备上没有 {args.package}")
            elif len(paths) > 1:
                add(False, "分包", f"检出 {len(paths)} 个 APK 分片：{paths}\n"
                                   "      本流程只处理单 APK。分包安装（Play 商店常见）时，"
                                   "dex/so 可能不在 base.apk 里，\n"
                                   "      改 base 不会生效也不报错。请先确认目标版本是单包，否则停手。")
            else:
                add(True, "分包", "单 APK（未分包）")

            dump = run(adb, "shell", "dumpsys", "package", args.package, timeout=90)
            ver = next((ln.strip() for ln in dump.splitlines() if "versionName=" in ln), "")
            code = next((ln.strip() for ln in dump.splitlines() if "versionCode=" in ln), "")
            add(bool(ver), "端上版本", f"{ver.split('=')[-1] if ver else '未知'}  {code.split()[0] if code else ''}")

            if paths:
                size_raw = run(adb, "shell", "stat", "-c", "%s", paths[0]).strip()
                if size_raw.isdigit():
                    device_size = int(size_raw)
                    add(True, "端上包大小", f"{device_size:,} 字节")

    # 4) 本地干净副本
    if args.apk:
        if not os.path.isfile(args.apk):
            add(False, "本地 APK", f"不存在: {args.apk}")
        else:
            dexes, abi = inspect_apk(args.apk)
            total = sum(c for _, c in dexes)
            add(bool(dexes), "本地 APK", f"{len(dexes)} 个 dex，合计 {total:,} 类，abi={abi or '无 native'}")

            # 端上装的和本地这份是不是同一个包。不比对的话，agent 会以为
            # "手机上跑的就是我刚拿到的那版"，而实际上可能差一个大版本。
            if device_size is not None:
                local_size = os.path.getsize(args.apk)
                if device_size == local_size:
                    add(True, "包身份", f"端上与本地同为 {local_size:,} 字节")
                else:
                    add(False, "包身份",
                        f"端上 {device_size:,} 字节 ≠ 本地 {local_size:,} 字节。\n"
                        "      手机上装的不是这份包——可能上一轮留下的改包，或商店已经推了新版本。\n"
                        "      别把端上那份当干净副本；也别假设'我刚装的改动端上就有'。")
            heap = 4096 if total < 50_000 else 6144 if total < 150_000 else 9216
            if total >= 150_000:
                add(True, "JADX 建议",
                    f"这个包很大，默认 4 GB 堆会 OOM。开工前先设：\n"
                    f"      export REA_JADX_HEAP_MIB={heap}\n"
                    f"      export REA_JADX_OPERATION_TIMEOUT_MS=600000\n"
                    f"      （加载本身可能超过默认 120 秒上限）")
            else:
                add(True, "JADX 建议", f"export REA_JADX_HEAP_MIB={heap}")
    else:
        add(True, "本地 APK", "未提供（跳过）；建议先用 --apk 校验干净副本")

    width = max(len(n) for _, n, _ in results)
    fails = 0
    for ok, name, detail in results:
        if not ok:
            fails += 1
        print(f"[{'PASS' if ok else 'FAIL'}] {name.ljust(width)}  {detail}")

    print(f"\n{len(results) - fails}/{len(results)} 通过")
    if fails:
        print("有 FAIL：先修掉再动包。这个脚本存在的意义就是让这些问题现在暴露，"
              "而不是两小时后。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
