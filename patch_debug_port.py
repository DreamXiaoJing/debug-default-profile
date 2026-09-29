#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
patch_debug_port.py

给 Chrome / Edge 打补丁，让它们在「默认用户数据目录」下也能开启远程调试端口。

背景
----
Chromium 系浏览器有一道保险：默认用户数据目录下，不允许绑定
--remote-debugging-port / --remote-debugging-pipe。这个校验在源码里是
RemoteDebuggingServer::GetInstance() -> IsRemoteDebuggingAllowed()，
编译进 chrome.dll / msedge.dll 之后，塌缩成几对 cmp + jne。

本工具把这几个条件跳转 NOP 掉，让校验恒放行。

只用于你自己的浏览器，用于本地自动化 / 调试 / 测试。
"""

import argparse
import re
import shutil
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# 补丁数据库：{浏览器: {版本: {dll 文件名, 搜索根目录, 补丁点}}}
# 每个补丁点 = (文件偏移, 原始字节hex)。替换值统一是等长的 NOP（0x90）。
# 原始字节用于「写入前校验」：版本 / 构建对不上就拒绝写入，不会打坏别的版本。
# ---------------------------------------------------------------------------
PATCH_DB = {
    "chrome": {
        "154.0.8037.58": {
            "dll": "chrome.dll",
            "roots": [
                r"C:\Program Files\Google\Chrome\Application",
                r"C:\Program Files (x86)\Google\Chrome\Application",
            ],
            "sites": [
                (0x256A7D3, "0F 85 FB 00 00 00"),
                (0x256A7E1, "0F 85 ED 00 00 00"),
                (0x256A9C4, "75 19"),
                (0x256A9CE, "75 0F"),
            ],
        },
    },
    "edge": {
        "154.0.4258.37": {
            "dll": "msedge.dll",
            "roots": [
                r"C:\Program Files (x86)\Microsoft\Edge\Application",
                r"C:\Program Files\Microsoft\Edge\Application",
            ],
            "sites": [
                (0x31607BB, "0F 85 E7 07 79 01"),
                (0x31607C9, "0F 85 D9 07 79 01"),
                (0x48F11B7, "75 E6"),
                (0x48F11C1, "75 DC"),
                (0x48F1603, "75 75"),
                (0x48F160D, "75 6B"),
            ],
        },
    },
}

VERSION_RE = re.compile(r"^\d+(?:\.\d+)+$")


def vkey(v):
    """把 '154.0.8037.58' 变成可比较的整数元组。"""
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return (0,)


def fail(msg):
    print("[FAIL] " + msg, file=sys.stderr)
    sys.exit(1)


def info(msg):
    print("[info] " + msg)


def ok(msg):
    print("[ ok ] " + msg)


def hexb(s):
    return bytes.fromhex(s)


def detect_installed(brand):
    """扫描已知版本的搜索根目录，返回 {版本: dll路径}（仅包含已支持版本）。"""
    entries = PATCH_DB.get(brand, {})
    roots = sorted({r for e in entries.values() for r in e["roots"]})
    found = {}
    for root in roots:
        p = Path(root)
        if not p.is_dir():
            continue
        try:
            subdirs = [d for d in p.iterdir() if d.is_dir()]
        except OSError:
            continue
        for vdir in subdirs:
            if vdir.name not in entries or not VERSION_RE.match(vdir.name):
                continue
            dll = vdir / entries[vdir.name]["dll"]
            if dll.is_file():
                found[vdir.name] = dll
    return found


def resolve_path(brand, version, explicit_path):
    """确定要操作的 dll 路径和版本号。"""
    if explicit_path:
        dll = Path(explicit_path)
        if not dll.is_file():
            fail("找不到文件: " + str(dll))
        ver = version or (dll.parent.name if VERSION_RE.match(dll.parent.name) else None)
        if not ver:
            fail("无法从路径判断版本，请用 --version 指定")
        return dll, ver

    installed = detect_installed(brand)
    if not installed:
        roots = sorted({r for e in PATCH_DB[brand].values() for r in e["roots"]})
        fail("未找到已支持版本的 %s。搜索目录：%s" % (brand, ", ".join(roots)))

    if version:
        if version not in installed:
            fail("版本 %s 未安装或未支持，当前检测到：%s" % (version, ", ".join(sorted(installed))))
        return installed[version], version

    ver = max(installed, key=vkey)
    return installed[ver], ver


def backup_path_for(dll, backup_dir):
    if backup_dir:
        return Path(backup_dir) / (dll.name + ".orig.bak")
    return dll.with_name(dll.name + ".orig.bak")


def load_patch_sites(brand, version):
    entry = PATCH_DB[brand][version]
    return [(off, hexb(orig), b"\x90" * len(hexb(orig))) for off, orig in entry["sites"]]


def verify(data, sites, expect_patched=False):
    """返回 (是否全部匹配, 出错偏移, 期望, 实际)。"""
    for off, orig, new in sites:
        want = new if expect_patched else orig
        cur = bytes(data[off:off + len(want)])
        if cur != want:
            return False, off, want, cur
    return True, None, None, None


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="patch_debug_port.py",
        description="允许 Chrome / Edge 在默认用户数据目录下开启远程调试端口",
    )
    ap.add_argument("--browser", choices=sorted(PATCH_DB.keys()),
                    help="目标浏览器（不传 --path 时必填）")
    ap.add_argument("--path", help="直接指定 dll 路径（覆盖自动检测）")
    ap.add_argument("--version", help="指定版本号（默认自动检测已安装的最高已支持版本）")
    ap.add_argument("--backup-dir", help=".orig.bak 备份存放目录（默认放在 dll 旁边）")
    ap.add_argument("--no-backup", action="store_true", help="不生成备份")
    ap.add_argument("--dry-run", action="store_true", help="只校验补丁点，不写入")
    ap.add_argument("--restore", action="store_true", help="从备份恢复原始 dll")
    ap.add_argument("--list", action="store_true", help="列出支持与检测到的版本")
    args = ap.parse_args(argv)

    if args.list:
        for brand in sorted(PATCH_DB):
            print("%s:" % brand)
            for ver in sorted(PATCH_DB[brand], key=vkey):
                n = len(PATCH_DB[brand][ver]["sites"])
                print("  %s  (%d 个补丁点)" % (ver, n))
            inst = detect_installed(brand)
            if inst:
                print("  本机检测到: " + ", ".join(sorted(inst, key=vkey)))
        return

    if not args.browser:
        fail("需要 --browser 或 --list")

    brand = args.browser
    dll, version = resolve_path(brand, args.version, args.path)
    if version not in PATCH_DB[brand]:
        supported = ", ".join(sorted(PATCH_DB[brand], key=vkey))
        fail("版本 %s 不在支持列表（%s）。详见 docs/ 新增版本方法。" % (version, supported))

    sites = load_patch_sites(brand, version)
    info("目标: %s (%s)，%d 个补丁点" % (dll, version, len(sites)))

    bak = backup_path_for(dll, args.backup_dir)

    if args.restore:
        if not bak.is_file():
            fail("备份不存在: " + str(bak))
        shutil.copy2(bak, dll)
        ok("已从备份恢复: " + str(dll))
        return

    try:
        data = bytearray(open(dll, "rb").read())
    except OSError as e:
        fail("读取失败（浏览器没关？）: %r" % e)

    good, off, want, cur = verify(data, sites)
    if not good:
        fail("校验失败，偏移 0x%x：期望 %s，实际 %s。版本/构建不符，拒绝写入。" %
             (off, want.hex(" "), cur.hex(" ")))
    info("写入前校验通过")

    if args.dry_run:
        ok("dry-run：%d 个补丁点全部匹配，未写入。" % len(sites))
        return

    if not args.no_backup:
        if bak.is_file():
            info("备份已存在（跳过）: " + str(bak))
        else:
            Path(bak.parent).mkdir(parents=True, exist_ok=True)
            shutil.copy2(dll, bak)
            ok("已备份: " + str(bak))

    for off, orig, new in sites:
        data[off:off + len(new)] = new

    try:
        with open(dll, "r+b") as f:
            f.seek(0)
            f.write(bytes(data))
    except PermissionError:
        fail("写入被拒绝。需要管理员权限（右键以管理员运行），并关闭浏览器。")
    except OSError as e:
        fail("写入失败（浏览器没关？）: %r" % e)

    d2 = bytearray(open(dll, "rb").read())
    good, off, want, cur = verify(d2, sites, expect_patched=True)
    if not good:
        fail("写入后校验失败，偏移 0x%x：期望 %s，实际 %s" % (off, want.hex(" "), cur.hex(" ")))

    ok("补丁完成，%d 个补丁点已 NOP 化。" % len(sites))
    info("验证：启动浏览器加 --remote-debugging-port=9222，访问 http://127.0.0.1:9222/json/version")
    info("回滚：python patch_debug_port.py --browser %s --restore" % brand)


if __name__ == "__main__":
    main()