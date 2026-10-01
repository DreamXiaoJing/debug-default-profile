#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probe_runtime_fn.py —— 诊断工具：在无符号的 chrome.dll / msedge.dll 里定位任意 V8 运行时函数。

它做的是 `patch_browser.py no-debugger` 的第一步（不含改写）：
    运行时函数名字符串 -> kIntrinsicFunctions[] 表项 -> 函数入口文件偏移

原理见 docs/no_debugger_statement.md 第三节。当你要把补丁扩展到别的 V8 行为
（例如 Runtime_DebugBreakOnBytecode / Runtime_DebugPrint）时，先用它确认函数在哪。

用法:
    python probe_runtime_fn.py <dll 路径> [函数名]
    python probe_runtime_fn.py "C:\\...\\chrome.dll"                  # 默认 HandleDebuggerStatement
    python probe_runtime_fn.py "C:\\...\\chrome.dll" DebugBreakOnBytecode

只读，不改任何文件。
"""

import re
import struct
import sys

NAME_RE = re.compile(rb"_?[A-Za-z][A-Za-z0-9_]*")
ENTRY_STRIDE = 32
OFF_NAME = 8
OFF_ENTRY = 16
WINDOW = 32          # 打印函数入口前多少字节


class Pe:
    """最小 PE 解析：段表 + RVA/文件偏移互转。"""

    def __init__(self, data):
        self.data = data
        d = data
        if d[:2] != b"MZ":
            raise ValueError("不是 PE 文件")
        pe_off = struct.unpack_from("<I", d, 0x3C)[0]
        if d[pe_off:pe_off + 4] != b"PE\0\0":
            raise ValueError("没有 PE 签名")
        coff = pe_off + 4
        nsec, = struct.unpack_from("<H", d, coff + 2)
        # 注意：SizeOfOptionalHeader 在 COFF 头 +16（+2 是 NumberOfSections），
        # 写错会解析出一堆乱码段名却不报错。
        opt_size, = struct.unpack_from("<H", d, coff + 16)
        opt = coff + 20
        magic, = struct.unpack_from("<H", d, opt)
        if magic != 0x20B:
            raise ValueError("不是 PE32+（x64），magic=%#x" % magic)
        self.image_base, = struct.unpack_from("<Q", d, opt + 24)
        self.sections = []
        sec = opt + opt_size
        for i in range(nsec):
            o = sec + i * 40
            name = d[o:o + 8].rstrip(b"\0").decode("latin1")
            vsize, vaddr, rawsize, rawptr = struct.unpack_from("<IIII", d, o + 8)
            chars, = struct.unpack_from("<I", d, o + 36)
            self.sections.append(dict(name=name, va=vaddr, vsize=vsize,
                                      raw=rawptr, rawsize=rawsize, chars=chars))

    def rva_to_off(self, rva):
        for s in self.sections:
            if s["va"] <= rva < s["va"] + max(s["vsize"], s["rawsize"]):
                o = s["raw"] + (rva - s["va"])
                return o if o < s["raw"] + s["rawsize"] else None
        return None

    def off_to_rva(self, off):
        for s in self.sections:
            if s["raw"] <= off < s["raw"] + s["rawsize"]:
                return s["va"] + (off - s["raw"])
        return None

    def section_of(self, rva):
        for s in self.sections:
            if s["va"] <= rva < s["va"] + max(s["vsize"], s["rawsize"]):
                return s
        return None

    def is_exec(self, rva):
        s = self.section_of(rva)
        return bool(s and (s["chars"] & 0x20000000))

    def read_cstr(self, va, limit=64):
        off = self.rva_to_off(va - self.image_base)
        if off is None:
            return None
        end = self.data.find(b"\x00", off, off + limit)
        return None if end < 0 else self.data[off:end]


def find_all(hay, needle):
    out, i = [], hay.find(needle)
    while i >= 0:
        out.append(i)
        i = hay.find(needle, i + 1)
    return out


def dump_struct(pe, base, tag):
    d = pe.data
    fid, itype = struct.unpack_from("<ii", d, base)
    nptr, = struct.unpack_from("<Q", d, base + OFF_NAME)
    ent, = struct.unpack_from("<Q", d, base + OFF_ENTRY)
    nargs, rsize = struct.unpack_from("<bb", d, base + 24)
    nm = pe.read_cstr(nptr)
    nm = nm.decode("latin1") if nm is not None else "<unreadable>"
    rva = ent - pe.image_base
    sec = pe.section_of(rva)
    print("    %-8s base=%#x  id=%-4d type=%d  nargs=%-2d rsize=%d  entry_rva=%#x [%s]  name=%s"
          % (tag, base, fid, itype, nargs, rsize, rva,
             (sec["name"] if sec else "?"), repr(nm)))


def main(dll_path, func_name):
    with open(dll_path, "rb") as f:
        data = f.read()
    pe = Pe(data)
    name = func_name.encode()

    print("文件      : %s" % dll_path)
    print("大小      : %.1f MB   ImageBase=%#x   段数=%d"
          % (len(data) / 1048576, pe.image_base, len(pe.sections)))

    hits = find_all(data, name + b"\x00")
    print("\n名字串 '%s' 命中 %d 处: %s"
          % (func_name, len(hits), [hex(h) for h in hits]))
    if len(hits) != 1:
        print("  -> 需要恰好 1 处才能自动定位。0 处：名字可能被裁剪或函数名拼错；"
              "多处：换更长的名字片段。")
        return 1

    str_off = hits[0]
    str_va = pe.image_base + pe.off_to_rva(str_off)
    print("名字串    : 文件偏移 %#x  rva=%#x  va=%#x  段=%s"
          % (str_off, pe.off_to_rva(str_off), str_va,
             pe.section_of(pe.off_to_rva(str_off))["name"]))

    ptr = struct.pack("<Q", str_va)
    refs = []
    for s in pe.sections:
        if not s["rawsize"]:
            continue
        blob = data[s["raw"]:s["raw"] + s["rawsize"]]
        for i in find_all(blob, ptr):
            refs.append(s["raw"] + i)
    print("引用该 VA 的指针 %d 处: %s" % (len(refs), [hex(r) for r in refs]))
    if not refs:
        print("  -> 没有指针引用它，说明这张表里没有这个函数（或表结构变了）。")
        return 1

    for ref in refs:
        base = ref - OFF_NAME
        print("\n候选表项 @ %#x（name 指针位于 +%#x）:" % (base, OFF_NAME))
        for k in (-2, -1, 0, 1, 2):
            dump_struct(pe, base + k * ENTRY_STRIDE, ("<-目标" if k == 0 else "邻居"))

        fid, itype = struct.unpack_from("<ii", data, base)
        ent, = struct.unpack_from("<Q", data, base + OFF_ENTRY)
        entry_rva = ent - pe.image_base
        ok = itype == 0 and pe.is_exec(entry_rva)
        print("\n  结构判定 : intrinsic_type=%d（期望 0=RUNTIME）、entry 可执行=%s -> %s"
              % (itype, pe.is_exec(entry_rva), "通过" if ok else "不通过"))
        if not ok:
            continue
        entry_off = pe.rva_to_off(entry_rva)
        print("  函数入口 : RVA=%#x  文件偏移=%#x" % (entry_rva, entry_off))
        lo = max(0, entry_off - WINDOW)
        print("  入口前 %d 字节: %s" % (entry_off - lo, data[lo:entry_off].hex(" ")))
        print("  入口后 64 字节: %s" % data[entry_off:entry_off + 64].hex(" "))
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    sys.exit(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "HandleDebuggerStatement"))
