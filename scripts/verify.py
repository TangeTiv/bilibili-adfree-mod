#!/usr/bin/env python3
"""Post-install checks for a modified APK: crash-free, and what you actually shipped.

Why this exists
---------------
Four failure modes cost real debugging time and are all mechanically checkable:

* the app dies silently from a native signature check (`exited cleanly`, no FATAL);
* a bad smali edit makes ART reject a class (`VerifyError`) and a whole screen
  goes empty -- which reads like "the filter was too aggressive";
* the device is running a *different* APK than the one just built;
* **the UI dump fails or shows the wrong page, and "no ad found" gets reported
  as "ad removed"**. 这一次改版的重点就是堵住这条假通过路径。

设计上的一条硬规则
------------------
**拿不到证据 ≠ 通过。** dump 失败、启动失败、页面元素数量异常，都算 FAIL，
不许静默跳过。原来的版本在 dump 失败时直接跳过关键词检查，然后退出码 0 —— 
等于教人把"没看见"当成"已修复"。

Usage
-----
    # 基线：先在**没改过**的版本上跑一次，记下 elements 的数值
    python3 scripts/verify.py --apk base.apk --launch --refresh --expect-elements 0

    # 改完之后：要求页面元素数不低于基线的一半，才算"页面正常渲染"
    python3 scripts/verify.py --apk ./signed/mod-aligned-signed.apk \
        --launch --refresh --dump-keywords ad_tint_frame,广告 --expect-elements 120

Exit code is 0 only when every enabled check passes.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys

try:  # 非 UTF-8 控制台（Windows 中文 GBK 等）兜底：个别符号降级为 ?，而不是抛 UnicodeEncodeError
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass
import time
import xml.etree.ElementTree as ET


def find_adb(explicit: str | None) -> str:
    if explicit:
        if not os.path.isfile(explicit):
            raise SystemExit(f"adb 不存在: {explicit}")
        return explicit
    from_env = os.environ.get("REA_ADB_PATH")
    if from_env and os.path.isfile(from_env):
        return from_env
    found = shutil.which("adb")
    if not found:
        raise SystemExit(
            "--adb 未指定，REA_ADB_PATH 也未设置，且 PATH 中找不到 adb。"
            "\n可用: --adb /path/to/adb  或  export REA_ADB_PATH=/path/to/adb"
        )
    return found


class Adb:
    def __init__(self, binary: str, serial: str | None):
        self.binary = binary
        self.serial = serial

    def _prefix(self) -> list[str]:
        cmd = [self.binary]
        if self.serial:
            cmd += ["-s", self.serial]
        return cmd

    def run(self, *args: str, timeout: int = 60, text: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            self._prefix() + list(args),
            capture_output=True, timeout=timeout,
            text=text,
            encoding="utf-8" if text else None,
            errors="replace" if text else None,
        )

    def shell(self, *args: str, timeout: int = 60) -> str:
        return self.run("shell", *args, timeout=timeout).stdout

    def exec_out(self, *args: str, timeout: int = 120) -> bytes:
        return self.run("exec-out", *args, timeout=timeout, text=False).stdout


def line_count(text: str, needle: str) -> int:
    return sum(1 for line in text.splitlines() if needle in line)


def check_device(adb: Adb) -> tuple[bool, str]:
    out = adb.run("devices").stdout
    rows = [r.split("\t") for r in out.splitlines()[1:] if "\t" in r]
    ready = [r[0] for r in rows if r[1].strip() == "device"]
    if adb.serial:
        if adb.serial in ready:
            return True, f"{adb.serial} 在线"
        return False, f"{adb.serial} 不在 device 状态（当前: {ready or '无'}）"
    if len(ready) == 1:
        adb.serial = ready[0]
        return True, f"{ready[0]} 在线（已自动选中）"
    if not ready:
        return False, "没有可用设备（检查 USB 调试与授权）"
    return False, f"多台设备，请用 --serial 指定: {ready}"


def resolve_activity(adb: Adb, package: str, explicit: str | None) -> str:
    """入口 Activity 每个大版本都可能改，别写死。"""
    if explicit:
        return explicit if "/" in explicit or explicit.startswith(".") else f"{package}/{explicit}"
    out = adb.shell("cmd", "package", "resolve-activity", "--brief", package, timeout=30)
    for line in reversed(out.splitlines()):
        line = line.strip()
        if "/" in line and not line.startswith("priority"):
            return line
    return f"{package}/.MainActivityV2"


def launch(adb: Adb, package: str, activity: str) -> tuple[bool, str]:
    adb.run("shell", "am", "force-stop", package)
    adb.run("logcat", "-c")
    out = adb.run("shell", "am", "start", "-W", "-n", activity, timeout=90).stdout
    if "Error:" in out or "does not exist" in out or "Activity not started" in out:
        return False, f"启动失败: {out.strip().splitlines()[0] if out.strip() else '无输出'}"
    return True, f"已启动 {activity}"


def check_process(adb: Adb, package: str) -> tuple[bool, str, str]:
    pid = adb.shell("pidof", package).strip()
    if pid:
        return True, f"进程存活 pid={pid.split()[0]}", pid.split()[0]
    return False, "进程不在（崩溃、被杀，或尚未启动）", ""


def scoped_log(adb: Adb, pid: str) -> str:
    """只取本进程的日志。

    原来的实现统计整个缓冲区，别人手机上任何 app 的历史 FATAL 都会让检查变红；
    反过来，如果缓冲里恰有一条我们自己的旧 FATAL，也会污染结论。
    """
    if pid:
        out = adb.run("logcat", "-d", "--pid", pid, timeout=90).stdout
        if out.strip():
            return out
    # 老平台不支持 --pid，退回按包名过滤
    return adb.run("logcat", "-d", timeout=90).stdout


def check_crash(adb: Adb, package: str, pid: str) -> tuple[bool, str]:
    log = scoped_log(adb, pid)
    fatal = line_count(log, "FATAL EXCEPTION")
    verify = line_count(log, "VerifyError")
    died = sum(1 for ln in log.splitlines()
               if ("has died" in ln or "exited cleanly" in ln) and package in ln)
    detail = f"FATAL={fatal} VerifyError={verify} 本包死亡行={died}"

    # 只有在"进程已经不在了 + 没有任何 FATAL"时，才把死亡行归因为原生自毁。
    # 原来不看进程存活，而 --launch 自己就会 force-stop，正常 kill 也会留一行
    # "has died"，健康包会被误判成自毁，然后被拉去改 so。
    if pid and died:
        return True, detail + "（进程在场，死亡行来自启动前的 force-stop，忽略）"
    if died and not fatal:
        return False, detail + " —— 无 FATAL 却死亡，疑似原生签名校验（见 SKILL 阶段 3）"
    return fatal == 0 and verify == 0, detail


def check_package_identity(adb: Adb, package: str, local_apk: str, strict: bool) -> tuple[bool, str]:
    raw = adb.shell("pm", "path", package)
    paths = [p.replace("package:", "").strip() for p in raw.splitlines() if p.startswith("package:")]
    if not paths:
        return False, f"未安装 {package}"
    if len(paths) > 1:
        return False, (f"端上是分包（{len(paths)} 个 APK）。本流程只支持单 APK，"
                       "别在这里下'装对了'的结论")
    base = paths[0]
    out = adb.shell("stat", "-c", "%s", base).strip()
    local_size = os.path.getsize(local_apk)
    if not out.isdigit():
        return False, f"无法读取端上包大小: {out!r}"
    on_device = int(out)
    if on_device != local_size:
        return False, f"端上 {on_device:,} / 本地 {local_size:,} —— 不一致（装的不是这份包）"

    if strict:
        dev_hash = adb.shell("sha256sum", base, timeout=180).strip().split()
        if not dev_hash or len(dev_hash[0]) != 64:
            return True, f"大小一致 {on_device:,}（端上 sha256 不可用，未能强校验）"
        with open(local_apk, "rb") as fh:
            h = hashlib.sha256()
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
        if dev_hash[0].lower() != h.hexdigest():
            return False, "sha256 不一致 —— 端上不是这份包"
        return True, f"sha256 一致 {h.hexdigest()[:16]}…"

    return True, f"大小一致 {on_device:,}（加 --strict-identity 可强校验 sha256）"


def screen_size(adb: Adb) -> tuple[int, int]:
    out = adb.shell("wm", "size")
    m = re.search(r"(\d+)x(\d+)", out)
    if not m:
        return 1080, 2400
    return int(m.group(1)), int(m.group(2))


def refresh_home(adb: Adb) -> None:
    """下拉刷新。坐标按屏幕尺寸算 —— 写死 600,500→600,1900 在别的分辨率上
    可能点开一个视频页，之后 dump 出来当然没有广告，于是"验证通过"。"""
    w, h = screen_size(adb)
    x = w // 2
    y1 = int(h * 0.30)
    y2 = int(h * 0.75)
    for _ in range(2):
        adb.shell("input", "swipe", str(x), str(y1), str(x), str(y2), "350")
        time.sleep(5)


def dump_ui(adb: Adb, out_dir: str, retries: int = 3) -> tuple[str | None, str]:
    remote = "/sdcard/rea_dump.xml"
    last = ""
    for attempt in range(1, retries + 1):
        adb.run("shell", "uiautomator", "dump", remote, timeout=90)
        raw = adb.exec_out("cat", remote)
        if len(raw) > 200 and b"<hierarchy" in raw:
            os.makedirs(out_dir, exist_ok=True)
            path = os.path.join(out_dir, "ui_dump.xml")
            with open(path, "wb") as fh:
                fh.write(raw)
            return path, f"第 {attempt} 次成功（{len(raw):,} 字节）"
        last = f"{len(raw)} 字节"
        time.sleep(2)
    return None, f"连续 {retries} 次失败（最后一次 {last}）"


def keyword_report(dump_path: str, keywords: list[str]) -> list[tuple[str, int]]:
    with open(dump_path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    results = []
    for kw in keywords:
        hits = sum(1 for _, value in re.findall(r'([\w-]+)="([^"]*)"', text) if kw in value)
        results.append((kw, hits))
    return results


def count_elements(dump_path: str) -> int:
    """页面里带 resource-id 的节点数。用来做"页面确实渲染出来了"的正向对照。"""
    with open(dump_path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    return len(re.findall(r'resource-id="[^"]+"', text))


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify a freshly installed modified APK.")
    ap.add_argument("--serial", help="adb serial；只有一台设备时可省略")
    ap.add_argument("--adb", help="adb 路径（默认 REA_ADB_PATH / PATH）")
    ap.add_argument("--package", default="tv.danmaku.bili")
    ap.add_argument("--apk", help="本地签名包路径，用于与端上包比对")
    ap.add_argument("--strict-identity", action="store_true",
                    help="比对端上包 sha256（默认只比大小）")
    ap.add_argument("--activity", help="入口 Activity；不给就自动解析")
    ap.add_argument("--launch", action="store_true", help="force-stop 后重新启动应用")
    ap.add_argument("--wait", type=int, default=14, help="启动后等待秒数（默认 14）")
    ap.add_argument("--watch", type=int, default=0,
                    help="启动后再持续观察 N 秒，进程中途消失即 FAIL。"
                         "签名自毁可能延迟触发（实测 9.18.0 在 22 秒才退出），"
                         "只抽查一次会漏。阶段 3 建议 --watch 40")
    ap.add_argument("--refresh", action="store_true", help="对首页做两次下拉刷新")
    ap.add_argument("--dump-keywords", default="", help="逗号分隔，如 ad_tint_frame,广告")
    ap.add_argument("--expect-elements", type=int, default=0,
                    help="UI dump 里至少要有的 resource-id 节点数；0=不检查。"
                         "先在未修改版本上跑一次取基线，**取基线的 80%%**"
                         "（实测取一半会放过'误滑到搜索页'这种情况）")
    ap.add_argument("--out-dir", default="./verify_out", help="dump 保存目录")
    args = ap.parse_args()

    adb = Adb(find_adb(args.adb), args.serial)
    results: list[tuple[str, bool, str]] = []

    ok, msg = check_device(adb)
    results.append(("设备连接", ok, msg))
    if not ok:
        for name, passed, detail in results:
            print(f"[{'PASS' if passed else 'FAIL'}] {name}: {detail}")
        return 1

    if args.launch:
        activity = resolve_activity(adb, args.package, args.activity)
        started, msg = launch(adb, args.package, activity)
        results.append(("启动", started, msg))
        time.sleep(args.wait)

    if args.refresh:
        refresh_home(adb)

    alive, msg, pid = check_process(adb, args.package)
    results.append(("进程", alive, msg))

    # 抽查一次不够：签名自毁可能是延迟触发的（9.18.0 实测 22 秒才退），
    # 而 14 秒的 --wait 正好在它之前，会给出一个假的"存活"。
    if args.watch > 0 and pid:
        vanished_at = 0
        for tick in range(2, args.watch + 1, 2):
            time.sleep(2)
            if not adb.shell("pidof", args.package).strip():
                vanished_at = tick
                break
        if vanished_at:
            results.append(("稳定性", False,
                            f"启动后约 {args.wait + vanished_at} 秒进程消失 —— "
                            "延迟触发的自毁/崩溃，见 SKILL 阶段 3"))
        else:
            results.append(("稳定性", True, f"持续观察 {args.watch} 秒未消失"))
        # 重新取一次 pid，后面的日志过滤要用
        alive, msg, pid = check_process(adb, args.package)
        results.append(("进程（观察后）", alive, msg))

    results.append(("崩溃与校验", *check_crash(adb, args.package, pid)))

    if args.apk:
        results.append(("包一致性", *check_package_identity(
            adb, args.package, args.apk, args.strict_identity)))

    dump_path = None
    if args.dump_keywords or args.expect_elements:
        dump_path, msg = dump_ui(adb, args.out_dir)
        # 拿不到 dump 就是拿不到证据 —— 必须 FAIL，不能跳过。
        results.append(("UI dump", dump_path is not None, msg))

    if dump_path and args.expect_elements:
        n = count_elements(dump_path)
        results.append(("页面渲染", n >= args.expect_elements,
                        f"resource-id 节点 {n} 个（要求 >= {args.expect_elements}）。"
                        "低于阈值通常意味着页面没加载出来、停在开屏、或根本没在首页"))

    if dump_path and args.dump_keywords:
        kws = [k.strip() for k in args.dump_keywords.split(",") if k.strip()]
        for kw, hits in keyword_report(dump_path, kws):
            results.append((f"UI 关键词「{kw}」", hits == 0,
                            f"命中 {hits} 次（>0 需人工确认是否真广告；"
                            "先看过「页面渲染」这一行再相信它）"))

    width = max(len(n) for n, _, _ in results)
    for name, passed, detail in results:
        print(f"[{'PASS' if passed else 'FAIL'}] {name.ljust(width)}  {detail}")
    if dump_path:
        print(f"\nUI 层级已保存: {dump_path}")
    print(f"\n截图建议: adb exec-out screencap -p > {os.path.join(args.out_dir, 'screen.png')}")

    return 0 if all(p for _, p, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
