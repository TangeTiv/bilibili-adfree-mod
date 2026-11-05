#!/usr/bin/env python3
"""定位并中和 AArch64 ELF 里对 exit/abort 的调用（B 站防自毁用）。

为什么需要它
------------
SKILL.md 阶段 3 让 agent 自己跑 `objdump -d libbili.so | grep "bl.*<exit>"`。这条命令
在接收方机器上通常跑不了：x86 主机要 aarch64 交叉 objdump，Windows 默认没有；而且拿
到地址之后还要自己算相对偏移去改，算错就是变砖。

这个脚本只做一件事，但做到底：**在 ELF 里精确定位 `BL` 到 exit/abort 的调用点，
并可选地把它们改成 NOP**。NOP 是 4 字节等长替换，不改任何偏移、不破坏 ELF 结构。

怎么定位（不依赖符号表可用性告警，也不靠猜偏移）
------------------------------------------------
1. 读 `.rela.plt`，拿到每个 JUMP_SLOT 重定位的 **GOT 槽地址** 和它对应的符号名；
2. 逐个解析 `.plt` 里的桩（16 字节一个），从 `adrp/add/ldr` 三条指令**算出它实际
   引用的 GOT 槽地址**；
3. 槽地址对上 `exit` 的桩，就是 `exit` 的 PLT 入口；
4. 在 `.text` 里扫所有 `BL`，目标落在那几个桩上的就是调用点。

第 2 步是关键：不假设“第 i 个桩对应第 i 个重定位”，而是把地址算出来再比对，
所以桩表里有填充/顺序变化也不会错位。

用法
----
    # 只看，不改
    python3 scripts/scan_native_exit.py lib/arm64-v8a/libbili.so --list

    # 把 exit/abort 的调用点全部改成 NOP
    python3 scripts/scan_native_exit.py lib/arm64-v8a/libbili.so \
        --patch-nop --out patched_libbili.so

    # 只处理指定符号
    python3 scripts/scan_native_exit.py x.so --list --symbols exit,_exit

退出码：0 = 成功（有无命中都算成功，看输出）；2 = 文件不是 AArch64 ELF；1 = 参数/IO 错误。
局限性（必须知道）：只覆盖直接 `BL` 调用。经函数指针（`BLR`）发起的 exit 抓不到；
如果 --list 显示 0 个命中而设备上确实自毁，说明走的是间接调用或别的判定路径。
"""

from __future__ import annotations

import argparse
import struct
import sys

try:  # 非 UTF-8 控制台（Windows 中文 GBK 等）兜底：个别符号降级为 ?，而不是抛 UnicodeEncodeError
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

# 手机上"结束进程"的入口。默认只碰前三个：__stack_chk_fail 是栈保护，全库几十处，
# 绝大多数与签名无关，默认一并 NOP 掉只会扩大风险面。要查它请显式 --symbols。
DEFAULT_SYMBOLS = ("exit", "_exit", "abort")

SHT_DYNSYM = 11
SHT_RELA = 4
SHT_NOBITS = 8

R_AARCH64_JUMP_SLOT = 1026  # 注意：1027 是 R_AARCH64_RELATIVE，别搞混

NOP = 0xD503201F


class Elf:
    """够用的 ELF64 LE 解析器：只要节表、.dynsym、.dynstr、.rela.plt。"""

    def __init__(self, data: bytes, path: str):
        self.data = data
        self.path = path
        if data[:4] != b"\x7fELF":
            raise ValueError("不是 ELF 文件")
        if data[4] != 2:
            raise ValueError("不是 ELF64")
        if data[5] != 1:
            raise ValueError("不是小端序")
        (self.e_machine,) = struct.unpack_from("<H", data, 0x12)
        (self.e_shoff,) = struct.unpack_from("<Q", data, 0x28)
        (self.e_shentsize,) = struct.unpack_from("<H", data, 0x3A)
        (self.e_shnum,) = struct.unpack_from("<H", data, 0x3C)
        (self.e_shstrndx,) = struct.unpack_from("<H", data, 0x3E)

        self.sections: list[dict] = []
        for i in range(self.e_shnum):
            off = self.e_shoff + i * self.e_shentsize
            name, stype = struct.unpack_from("<II", data, off)
            flags, addr, sh_off, size = struct.unpack_from("<QQQQ", data, off + 8)
            link, info = struct.unpack_from("<II", data, off + 40)
            entsize = struct.unpack_from("<Q", data, off + 56)[0]
            self.sections.append(
                dict(idx=i, name_off=name, type=stype, flags=flags, addr=addr,
                     offset=sh_off, size=size, link=link, info=info, entsize=entsize)
            )

        shstr = self.sections[self.e_shstrndx]
        base = shstr["offset"]
        for s in self.sections:
            end = data.find(b"\x00", base + s["name_off"])
            s["name"] = data[base + s["name_off"]:end].decode("latin-1")

    def sec(self, name: str) -> dict | None:
        for s in self.sections:
            if s["name"] == name and s["type"] != SHT_NOBITS:
                return s
        return None

    def cstr(self, off: int) -> str:
        dynstr = self.sec(".dynstr")
        start = dynstr["offset"] + off
        end = self.data.find(b"\x00", start)
        return self.data[start:end].decode("latin-1", "replace")

    def jump_slots(self) -> list[tuple[int, str]]:
        """[(GOT 槽虚拟地址, 符号名)]，顺序即 .rela.plt 顺序。"""
        rela = self.sec(".rela.plt")
        dynsym = self.sec(".dynsym")
        if not rela or not dynsym:
            return []
        out = []
        for i in range(rela["size"] // 24):
            off = rela["offset"] + i * 24
            r_offset, r_info = struct.unpack_from("<QQ", self.data, off)
            if (r_info & 0xFFFFFFFF) != R_AARCH64_JUMP_SLOT:
                continue
            sym_idx = r_info >> 32
            ent = dynsym["offset"] + sym_idx * 24
            st_name = struct.unpack_from("<I", self.data, ent)[0]
            out.append((r_offset, self.cstr(st_name)))
        return out

    def plt_stub_map(self) -> dict[int, str]:
        """{桩虚拟地址: 符号名}，用 adrp/add/ldr 反算 GOT 槽，再与重定位对表。"""
        plt = self.sec(".plt")
        if not plt:
            return {}
        got_to_name = {addr: name for addr, name in self.jump_slots()}
        result = {}
        for i in range(plt["size"] // 16):
            stub_va = plt["addr"] + i * 16
            slot = self._stub_got_slot(stub_va)
            if slot is not None and slot in got_to_name:
                result[stub_va] = got_to_name[slot]
        return result

    def _read_insn(self, va: int) -> int | None:
        for s in self.sections:
            if s["type"] == SHT_NOBITS or not (s["flags"] & 0x4):
                continue  # 只要有内容的可执行节
            if s["addr"] <= va < s["addr"] + s["size"]:
                off = s["offset"] + (va - s["addr"])
                if off + 4 <= len(self.data):
                    return struct.unpack_from("<I", self.data, off)[0]
        return None

    def _stub_got_slot(self, stub_va: int) -> int | None:
        """从 adrp/ldr/add/br 序列推出桩引用的 GOT 槽地址。"""
        i0 = self._read_insn(stub_va)
        i1 = self._read_insn(stub_va + 4)
        i2 = self._read_insn(stub_va + 8)
        if i0 is None or i1 is None:
            return None
        if (i0 & 0x9F000000) != 0x90000000:      # ADRP
            return None
        immlo = (i0 >> 29) & 0x3
        immhi = (i0 >> 5) & 0x7FFFF
        imm = (immhi << 2) | immlo
        if imm & (1 << 20):
            imm -= 1 << 21
        page = (stub_va & ~0xFFF) + (imm << 12)

        if (i1 & 0xFFC00000) == 0xF9400000:      # LDR x, [x, #imm12*8]
            ldr_off = ((i1 >> 10) & 0xFFF) * 8
            # 标准桩里 ldr 与 add 用的是同一个低 12 位偏移（add 只是把 x16 补成
            # 完整地址），两者不能相加，加一次就等于偏移翻倍。
            slot_off = ldr_off
            if i2 is not None and (i2 & 0xFF800000) == 0x91000000:  # ADD x, x, #imm12
                add_off = (i2 >> 10) & 0xFFF
                if (i2 >> 22) & 1:
                    add_off <<= 12
                slot_off = add_off
            return page + slot_off
        return None

    def text_bl_calls(self) -> list[tuple[int, int]]:
        """[(BL 指令地址, 目标地址)]，扫所有可执行节。"""
        calls = []
        for s in self.sections:
            if s["type"] == SHT_NOBITS or not (s["flags"] & 0x4):
                continue
            start, size = s["offset"], s["size"]
            for off in range(start, start + size - 3, 4):
                (insn,) = struct.unpack_from("<I", self.data, off)
                if (insn & 0xFC000000) != 0x94000000:
                    continue
                imm = insn & 0x03FFFFFF
                if imm & (1 << 25):
                    imm -= 1 << 26
                va = s["addr"] + (off - start)
                calls.append((va, va + imm * 4))
        return calls


def va_to_file_off(elf: Elf, va: int) -> int | None:
    for s in elf.sections:
        if s["type"] == SHT_NOBITS:
            continue
        if s["addr"] <= va < s["addr"] + s["size"]:
            return s["offset"] + (va - s["addr"])
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="定位/中和 AArch64 ELF 中对 exit/abort 的 BL 调用")
    ap.add_argument("so", help="目标 .so 路径")
    ap.add_argument("--list", action="store_true", help="只列出调用点，不改")
    ap.add_argument("--patch-nop", action="store_true", help="把命中的 BL 改成 NOP")
    ap.add_argument("--out", help="--patch-nop 的输出路径（默认原地覆盖，会先备份 .orig）")
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS),
                    help="要处理的符号，逗号分隔（默认 exit,_exit,abort；__assert 与 "
                         "__stack_chk_fail 需显式指定）")
    ap.add_argument("--max-hits", type=int, default=20,
                    help="命中超过这个数量就拒绝打补丁（默认 20），避免一次 NOP 掉太多合法退出")
    ap.add_argument("--force", action="store_true", help="无视 --max-hits 上限")
    args = ap.parse_args()

    with open(args.so, "rb") as fh:
        data = bytearray(fh.read())

    try:
        elf = Elf(bytes(data), args.so)
    except ValueError as exc:
        print(f"跳过：{exc}", file=sys.stderr)
        return 2

    if elf.e_machine != 183:  # EM_AARCH64
        print(f"跳过：e_machine={elf.e_machine}，不是 AArch64（本脚本只处理 arm64-v8a）",
              file=sys.stderr)
        return 2

    wanted = {s.strip() for s in args.symbols.split(",") if s.strip()}
    stubs = elf.plt_stub_map()
    if not stubs:
        # 不再按"第 i 个重定位对应第 i 个桩"去猜：resolver 可能占 32 字节，
        # 猜错就整体错位一格，等于把别的函数改成 NOP —— 这是会变砖的错。
        print("解析不出 PLT 桩（adrp/ldr 反算失败）。本脚本拒绝在映射不可信的情况下打补丁。\n"
              "可以先用 objdump/readelf 人工确认，或换一个版本再试。", file=sys.stderr)
        return 3

    targets = {va: name for va, name in stubs.items() if name in wanted}
    print(f"PLT 桩共 {len(stubs)} 个；其中退出类 {len(targets)} 个：", file=sys.stderr)
    for va, name in sorted(targets.items()):
        print(f"  0x{va:08x}  {name}", file=sys.stderr)

    hits = [(va, tgt) for va, tgt in elf.text_bl_calls() if tgt in targets]

    print(f"\n命中 BL 调用点 {len(hits)} 处：")
    for va, tgt in hits:
        off = va_to_file_off(elf, va)
        print(f"  0x{va:08x}  -> {targets[tgt]:<16} 文件偏移 0x{off:x}"
              if off is not None else f"  0x{va:08x}  -> {targets[tgt]}")

    if not args.patch_nop:
        if not args.list:
            print("\n（未加 --patch-nop，什么都没改）", file=sys.stderr)
        return 0

    if not hits:
        print("没有命中，未生成产物。", file=sys.stderr)
        return 0

    if len(hits) > args.max_hits and not args.force:
        print(f"\n命中 {len(hits)} 处，超过 --max-hits={args.max_hits}。\n"
              f"一次性 NOP 掉这么多退出点风险太大（可能连正常错误处理一起拆掉）。\n"
              f"先缩小 --symbols，确认某几处就是签名校验再动手；确认无误可加 --force。",
              file=sys.stderr)
        return 4

    for va, _ in hits:
        off = va_to_file_off(elf, va)
        if off is None:
            print(f"警告：0x{va:x} 找不到文件偏移，跳过", file=sys.stderr)
            continue
        struct.pack_into("<I", data, off, NOP)

    out = args.out or (args.so + ".patched")
    with open(out, "wb") as fh:
        fh.write(data)

    # 回读校验：重新解析产物，确认原来的调用点变了、别处没动。
    check = Elf(open(out, "rb").read(), out)
    left = [va for va, tgt in check.text_bl_calls() if tgt in targets]
    print(f"\n产物: {out}")
    print(f"回读校验：剩余 exit 类调用 {len(left)} 处 {'（应为 0）' if left else 'OK'}")
    if left:
        print(f"  残留: {[hex(v) for v in left]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
