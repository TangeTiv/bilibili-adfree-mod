#!/usr/bin/env python3
"""Rebuild an APK by replacing selected entries, then prove each one landed.

Why this exists
---------------
Updating a 200 MB+ APK in place with `zip -X` is routinely blocked by file
locks and can leave a half-updated archive behind -- which is worse than a
clean failure, because the next step signs it and you ship an unmodified build.

This script rewrites the archive from scratch (streaming every untouched entry
from the source) and then re-reads the output to compare sha256 for every entry
it claims to have replaced. It exits non-zero if any comparison fails.

Usage
-----
    python3 rebuild.py --base base.apk --out mod.apk \
        --replace classes4.dex=/work/out/classes4.dex \
        --replace lib/arm64-v8a/libbili.so=/work/out/patched_libbili.so

    # optional zipalign + sign via uber-apk-signer
    python3 rebuild.py --base base.apk --out mod.apk \
        --replace classes15.dex=/work/out/classes15.dex \
        --sign --signer /tools/uber-apk-signer.jar \
        --ks /work/mod.jks --ks-alias mod --ks-pass secret --ks-key-pass secret

    # inspect entry names first
    python3 rebuild.py --base base.apk --list
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys

try:  # 非 UTF-8 控制台（Windows 中文 GBK 等）兜底：个别符号降级为 ?，而不是抛 UnicodeEncodeError
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass
import zipfile


def sha256_file(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def sha256_stream(fh, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    for block in iter(lambda: fh.read(chunk), b""):
        h.update(block)
    return h.hexdigest()


def parse_replacements(items: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for raw in items:
        if "=" not in raw:
            raise SystemExit(f"--replace 需要 ENTRY=PATH 形式，收到: {raw}")
        entry, _, path = raw.partition("=")
        entry = entry.strip().lstrip("/")
        path = path.strip()
        if not entry or not path:
            raise SystemExit(f"--replace 参数为空: {raw}")
        if not os.path.isfile(path):
            raise SystemExit(f"替换文件不存在: {path}")
        if entry in mapping:
            raise SystemExit(f"同一 ENTRY 被重复替换: {entry}")
        mapping[entry] = os.path.abspath(path)
    return mapping


def list_entries(base: str) -> None:
    with zipfile.ZipFile(base) as src:
        for info in src.infolist():
            print(f"{info.file_size:>12}  {info.filename}")
        print(f"\n共 {len(src.infolist())} 个条目", file=sys.stderr)


def rebuild(base: str, out: str, replacements: dict[str, str]) -> tuple[bool, list[str]]:
    """Rewrite `base` into `out`, substituting `replacements`. Returns (ok, log)."""
    log: list[str] = []

    with zipfile.ZipFile(base) as src:
        names = set(src.namelist())

        missing = [e for e in replacements if e not in names]
        if missing:
            # A replacement that is not an existing entry would silently *add* a
            # file to the archive -- almost always a typo in the entry name.
            raise SystemExit(
                "以下 ENTRY 在源 APK 中不存在（拒绝新增条目）:\n  "
                + "\n  ".join(missing)
                + "\n可用 --list 查看真实条目名。"
            )

        tmp = out + ".partial"
        if os.path.exists(tmp):
            os.remove(tmp)

        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as dst:
            for info in src.infolist():
                if info.filename in replacements:
                    with open(replacements[info.filename], "rb") as fh:
                        data = fh.read()
                else:
                    data = src.read(info.filename)

                clone = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                clone.compress_type = info.compress_type
                clone.external_attr = info.external_attr
                clone.internal_attr = info.internal_attr
                clone.create_system = info.create_system
                clone.comment = info.comment
                dst.writestr(clone, data)

        os.replace(tmp, out)

    # Verify by reading the produced archive back, not by trusting the write.
    ok = True
    with zipfile.ZipFile(out) as dst:
        for entry, source in replacements.items():
            want = sha256_file(source)
            with dst.open(entry) as fh:
                got = sha256_stream(fh)
            good = want == got
            ok = ok and good
            log.append(f"{'OK ' if good else 'BAD'}  {entry}  {got[:16]}…")
    return ok, log


def has_v2_signing_block(path: str, tail: int = 8 << 20) -> bool:
    """APK Signature Scheme v2/v3 会在中央目录前插一个签名块。

    目标 SDK 30+ 的包没有 v2 签名的装不上，而 uber-apk-signer 失败时退出码不总是非零
    ——所以签完必须自己看一眼，不能只信它说"成功"。
    """
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - tail))
        return b"APK Sig Block 42" in fh.read()


def verify_signed(path: str) -> None:
    import zipfile as _zip

    if not _zip.is_zipfile(path):
        raise SystemExit(f"签名产物不是合法 zip: {path}")
    if not has_v2_signing_block(path):
        raise SystemExit(
            f"{path} 里没有 v2/v3 签名块。\n"
            "Android 11+ 上 targetSdk 30+ 的包没有它装不上（或装了立刻崩）。\n"
            "别急着装机：先确认 uber-apk-signer 的完整输出，再决定是否重签。"
        )


def sign(out: str, args: argparse.Namespace) -> str:
    if not args.signer or not os.path.isfile(args.signer):
        raise SystemExit("--sign 需要 --signer 指向 uber-apk-signer.jar")
    if not args.ks or not os.path.isfile(args.ks):
        raise SystemExit("--sign 需要 --ks 指向已存在的 keystore（不要临时新建，否则无法覆盖安装）")

    outdir = os.path.join(os.path.dirname(os.path.abspath(out)), "signed")
    key_pass = args.ks_key_pass or args.ks_pass
    cmd = [
        args.java or "java",
        "-jar", args.signer,
        "-a", out,
        "-o", outdir,
        "--ks", args.ks,
        "--ksAlias", args.ks_alias,
        "--ksPass", args.ks_pass,
        "--ksKeyPass", key_pass,
        "--allowResign",
    ]
    # 口令绝不回显：这段输出会被贴进 issue、录屏、CI 日志。
    shown = ["<已隐去>" if c and c in (args.ks_pass, key_pass) else c for c in cmd]
    print("$ " + " ".join(shown), file=sys.stderr)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    # 某些环境下（输出极多时）stdout/stderr 可能为 None，直接 write 会 TypeError。
    # 签名本身已经执行过了，不该因为"回显失败"把它判成失败。
    sys.stderr.write(proc.stdout or "")
    sys.stderr.write(proc.stderr or "")
    if proc.returncode != 0:
        raise SystemExit(f"签名失败，退出码 {proc.returncode}")

    stem = os.path.splitext(os.path.basename(out))[0]
    signed = os.path.join(outdir, f"{stem}-aligned-signed.apk")
    if not os.path.isfile(signed):
        raise SystemExit(f"签名器未产出预期文件: {signed}")
    verify_signed(signed)
    return signed


def main() -> int:
    ap = argparse.ArgumentParser(description="Replace APK entries and verify them byte-for-byte.")
    ap.add_argument("--base", required=True, help="干净的标准 APK")
    ap.add_argument("--out", help="输出 APK 路径")
    ap.add_argument("--replace", action="append", default=[], metavar="ENTRY=PATH",
                    help="要替换的条目，可重复")
    ap.add_argument("--list", action="store_true", help="列出源 APK 的条目后退出")
    ap.add_argument("--sign", action="store_true", help="构建后调用 uber-apk-signer")
    ap.add_argument("--signer", help="uber-apk-signer.jar 路径")
    ap.add_argument("--java", default="java", help="java 可执行文件（默认 java）")
    ap.add_argument("--ks", help="keystore 路径")
    ap.add_argument("--ks-alias", default="mod")
    ap.add_argument("--ks-pass", default="", help="store 口令（会出现在 ps 里，能不用就不用）")
    ap.add_argument("--ks-key-pass", default="")
    ap.add_argument("--ks-pass-file", help="从文件读 store 口令（首行），比 --ks-pass 安全；"
                                           "也可用环境变量 KS_PASS")
    args = ap.parse_args()

    if args.ks_pass_file:
        with open(args.ks_pass_file, encoding="utf-8") as fh:
            args.ks_pass = fh.readline().strip()
    if not args.ks_pass:
        args.ks_pass = os.environ.get("KS_PASS", "")

    if not os.path.isfile(args.base):
        raise SystemExit(f"源 APK 不存在: {args.base}")

    if args.list:
        list_entries(args.base)
        return 0

    if not args.out:
        raise SystemExit("缺少 --out")
    if os.path.abspath(args.out) == os.path.abspath(args.base):
        raise SystemExit("--out 不能等于 --base：干净副本必须留着，下一版还要从它重建")
    if not args.replace:
        raise SystemExit("至少需要一个 --replace；仅想重打包请用 --list 确认后说明意图")

    replacements = parse_replacements(args.replace)
    base_size = os.path.getsize(args.base)
    print(f"源包: {args.base} ({base_size:,} 字节)")
    for entry, path in replacements.items():
        print(f"  替换 {entry} <- {path} ({os.path.getsize(path):,} 字节)")

    ok, log = rebuild(args.base, args.out, replacements)
    for line in log:
        print("  " + line)

    print(f"\n产物: {args.out} ({os.path.getsize(args.out):,} 字节)")
    if not ok:
        print("失败：至少一个条目未按预期写入，不要拿去签名。", file=sys.stderr)
        return 1

    if args.sign:
        signed = sign(args.out, args)
        print(f"已签名: {signed} ({os.path.getsize(signed):,} 字节)")
        print(f"sha256: {sha256_file(signed)}")

    print("全部替换条目校验通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
