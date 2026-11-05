#!/usr/bin/env python3
"""Lint the skill package: structure, frontmatter, and that the scripts run.

This checks the *package*, not the skill's judgement -- behavioural checks live
in evals/cases.md. Run it after editing SKILL.md or the scripts.

    python3 evals/run_checks.py
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

try:  # 非 UTF-8 控制台（Windows 中文 GBK 等）兜底：个别符号降级为 ?，而不是抛 UnicodeEncodeError
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL = os.path.join(ROOT, "SKILL.md")

REQUIRED_SECTIONS = [
    ("交付定义", "完成标准必须可判定"),
    ("硬性约束", "禁令必须在正文显式出现"),
    ("验证", "验证方法不能只留在经验里"),
]

REQUIRED_SCRIPTS = [
    "scripts/preflight.py",
    "scripts/find_class.py",
    "scripts/scan_native_exit.py",
    "scripts/rebuild.py",
    "scripts/verify.py",
]
REQUIRED_FILES = ["references/case-studies.md", "evals/cases.md"]
DOCUMENTED_SCRIPTS = ["preflight.py", "find_class.py", "scan_native_exit.py",
                      "rebuild.py", "verify.py"]

TRIGGER_WORDS = ["去广告", "无广", "bilibili", "安装包"]

results: list[tuple[bool, str]] = []


def check(ok: bool, message: str) -> None:
    results.append((ok, message))


def parse_frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    block = text[3:end]
    meta: dict[str, str] = {}
    key = None
    for line in block.splitlines():
        if not line.strip():
            continue
        if re.match(r"^\s+\S", line) and key:          # folded continuation
            meta[key] += " " + line.strip()
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            meta[key] = value.strip()
    return meta


def main() -> int:
    if not os.path.isfile(SKILL):
        print(f"缺少 SKILL.md: {SKILL}")
        return 1

    text = open(SKILL, encoding="utf-8").read()

    meta = parse_frontmatter(text)
    check(bool(meta), "SKILL.md 有 YAML frontmatter")
    check("name" in meta, "frontmatter 含 name")
    check("description" in meta, "frontmatter 含 description")

    if "name" in meta:
        expected = os.path.basename(ROOT)
        check(meta["name"] == expected,
              f"name 与目录名一致（{meta['name']} vs {expected}）")

    desc = meta.get("description", "")
    check(len(desc) >= 40, f"description 足够描述触发场景（{len(desc)} 字符）")
    hits = [w for w in TRIGGER_WORDS if w in desc]
    check(bool(hits), f"description 含触发词（命中: {', '.join(hits) or '无'}）")

    for section, why in REQUIRED_SECTIONS:
        check(section in text, f"正文含「{section}」章节（{why}）")

    # Anti-patterns that previously caused real breakage.
    check("zip -X" not in text or "禁止" in text or "不要" in text,
          "若提到 zip -X，必须同时标明禁用")
    check("PATCHED" not in text or "汇总" in text or "丢失" in text,
          "若提到注释标记，必须同时说明汇编后丢失")

    # 每个脚本都要在正文里被指到，否则接收方的 agent 根本不知道它存在。
    for script in DOCUMENTED_SCRIPTS:
        check(script in text, f"SKILL.md 提到 {script}")

    # 口令不许回显（原来 rebuild.py 会把整条 uber-apk-signer 命令打出来）
    rebuild_src = open(os.path.join(ROOT, "scripts/rebuild.py"), encoding="utf-8").read()
    check("<已隐去>" in rebuild_src, "rebuild.py 回显命令时隐去口令")
    check("--ks-pass-file" in rebuild_src, "rebuild.py 支持从文件读口令")

    # 假通过防线：dump 失败必须判 FAIL，且必须有正向对照参数
    verify_src = open(os.path.join(ROOT, "scripts/verify.py"), encoding="utf-8").read()
    check("expect_elements" in verify_src, "verify.py 有正向对照（--expect-elements）")
    check("dump_path is not None" in verify_src,
          "verify.py 在拿不到 UI dump 时判 FAIL（而不是跳过）")

    for rel in REQUIRED_FILES:
        check(os.path.isfile(os.path.join(ROOT, rel)), f"存在 {rel}")

    for rel in REQUIRED_SCRIPTS:
        path = os.path.join(ROOT, rel)
        if not os.path.isfile(path):
            check(False, f"存在 {rel}")
            continue
        check(True, f"存在 {rel}")
        proc = subprocess.run([sys.executable, path, "--help"],
                              capture_output=True, text=True)
        check(proc.returncode == 0,
              f"{rel} --help 可运行" + ("" if proc.returncode == 0
                                        else f"（stderr: {proc.stderr.strip()[:160]}）"))

    width = max(len(m) for _, m in results)
    failures = 0
    for ok, msg in results:
        print(f"[{'PASS' if ok else 'FAIL'}] {msg}")
        if not ok:
            failures += 1

    print(f"\n{len(results) - failures}/{len(results)} 通过")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
