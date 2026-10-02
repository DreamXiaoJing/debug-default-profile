#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
patch_browser.py —— 一个脚本，两个二进制补丁（官方 Chrome / Edge）

    python patch_browser.py debug-port  --browser chrome   # 默认用户数据目录下也能开 CDP 端口
    python patch_browser.py no-debugger --browser chrome   # 让浏览器忽略 JS 的 debugger 语句
    python patch_browser.py auto --path "<dll 或浏览器 exe 的路径>"   # 只给路径，其余自己推导
    python patch_browser.py                                # 不带子命令＝自检一遍 + 教程（只读）

不带子命令直接运行，会把两个补丁 × 两个浏览器全部体检一遍（版本、补丁状态、备份、
写权限、浏览器是否还开着），然后打印下一步该敲哪条命令和常用教程。全程只读，不写任何文件。

`auto` 是最省事的入口：只给它一个路径（chrome.dll / msedge.dll、浏览器 exe、或版本目录都行），
品牌、版本、补丁点全部自己推导，然后该打的打、已打的跳过。

版本更新后会变的参数（品牌/搜索目录、debug-port 的补丁点）都在同目录的 `patch_db.json` 里，
改那个文件即可，不用改本脚本；`--locate --save` 和 `auto` 也会把新推导出的补丁点写回去。

两个子命令互相独立，可以只用其中一个，也可以都打：补丁点不同、备份后缀不同
（`debug-port` 用 `.orig.bak`，`no-debugger` 用 `.nodebug.bak`），互不覆盖。

debug-port
----------
Chromium 禁止在默认用户数据目录下绑定 --remote-debugging-port /
--remote-debugging-pipe。源码里是 IsRemoteDebuggingAllowed()（std::optional<bool>，
fail-closed），编译进 chrome.dll / msedge.dll 后塌缩成几对 cmp + jne；
本工具把那几条 jne NOP 掉，让校验恒放行。
完整逆向过程见 docs/cdp_user_data_dir_check.md。

no-debugger
-----------
`debugger;` 不管解释执行还是 JIT，最终都调用同一个运行时函数
Runtime_HandleDebuggerStatement；函数体里只有一道闸门
`if (isolate->debug()->break_points_active())`，编译后是一条
`cmp byte ptr [rcx+disp], 1` + `jne <跳过>`。把这条 jne 换成**等长** jmp，
if 体就永远被跳过：`debugger` 变空操作，文件长度不变，尾部的 HandleInterrupts()
照常执行。手动行号断点走 Runtime::kDebugBreakOnBytecode，不受影响。
完整逆向过程见 docs/no_debugger_statement.md。

只用于你自己的浏览器，本地自动化 / 调试 / 测试。
"""

import argparse
import json
import mmap
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# 参数文件：版本更新后会变的参数都在 patch_db.json 里。
#   brands   : 品牌 -> {dll 文件名, 安装搜索根目录}
#   patch_db : debug-port 的补丁点；每个版本 = { "文件偏移(hex)": "原始字节(hex)" }
# no-debugger 的补丁点每次运行自己推导，不存在这里。
# 只追加、不删旧版本条目 —— 旧版本是回滚与取证依据。
#
# 放哪由 resolve_db_path() 决定（git clone 出来用就放在脚本旁边；
# pip 装出来的包装在用户目录，见那里的说明）。
# ---------------------------------------------------------------------------
DB_NAME = "patch_db.json"
DB_DEFAULT_NAME = "patch_db.default.json"     # 随包安装的出厂参数
SCRIPT_DIR = Path(__file__).resolve().parent
DB_PATH = None                                # 由 load_db() 确定

BRANDS = {}      # {品牌: {"dll": ..., "roots": [...]}}
PATCH_DB = {}    # {品牌: {版本: [(偏移, "原始字节hex"), ...]}}

VERSION_RE = re.compile(r"^\d+(?:\.\d+)+$")


# ---------------------------------------------------------------------------
# 公共工具
# ---------------------------------------------------------------------------
def fail(msg):
    print("[FAIL] " + msg, file=sys.stderr)
    sys.exit(1)


def info(msg):
    print("[info] " + msg)


def ok(msg):
    print("[ ok ] " + msg)


def warn(msg):
    print("[warn] " + msg)


def vkey(v):
    """把 '154.0.8037.58' 变成可比较的整数元组。"""
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return (0,)


def hexb(s):
    return bytes.fromhex(s)


# ---------------------------------------------------------------------------
# 参数文件读写（patch_db.json）
# ---------------------------------------------------------------------------
def _sites_from_json(sites):
    """把 JSON 里一段补丁点转成 [(偏移, "原始字节hex"), ...]，按偏移排序。

    两种写法都认：
        {"0x24A6383": "0F 85 FB 00 00 00", ...}      ← 推荐，手改最直观
        [["0x24A6383", "0F 85 FB 00 00 00"], ...]
    """
    if isinstance(sites, dict):
        items = [(int(k, 16), v) for k, v in sites.items()]
    elif isinstance(sites, list):
        items = [(int(o, 16) if isinstance(o, str) else int(o), b) for o, b in sites]
    else:
        raise ValueError("补丁点既不是对象也不是数组：%r" % (sites,))
    return sorted(((int(off), str(orig).upper()) for off, orig in items), key=lambda s: s[0])


def resolve_db_path():
    """决定用哪份 patch_db.json —— 三种用法都要能跑：

    1. `PATCH_BROWSER_DB` 环境变量指定的路径（想放哪就放哪）；
    2. 和脚本放在一起的 `patch_db.json`：git clone 出来直接跑、
       或者「只拷 patch_browser.py + patch_db.json 两个文件」的用法；
    3. 用户数据目录 `%LOCALAPPDATA%\\patch-browser\\patch_db.json`：
       pip 装出来的包走这条，首次运行时从随包的 `patch_db.default.json` 拷一份过去。
       不能直接写 site-packages —— 升级会覆盖、uninstall 会删掉，还可能没权限。
    """
    env = os.environ.get("PATCH_BROWSER_DB")
    if env:
        return Path(env).expanduser()
    beside = SCRIPT_DIR / DB_NAME
    if beside.is_file():
        return beside
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
    user = Path(base) / "patch-browser" / DB_NAME
    if not user.is_file():
        seed = SCRIPT_DIR / DB_DEFAULT_NAME
        if seed.is_file():
            try:
                user.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(seed, user)
                info("首次运行：已把出厂参数复制到 %s" % user)
            except OSError as e:
                fail("无法创建用户参数文件 %s: %r\n"
                     "       可以用环境变量 PATCH_BROWSER_DB 指定一个可写路径。" % (user, e))
    return user


def load_db(path=None):
    """读取 patch_db.json；缺文件或写坏了都给一条能看懂的错误。"""
    global BRANDS, PATCH_DB, DB_PATH
    path = Path(path) if path else resolve_db_path()
    DB_PATH = path
    if not path.is_file():
        fail("找不到参数文件 %s。\n"
             "       git clone 的用法：让 patch_db.json 和 patch_browser.py 放在同一个目录；\n"
             "       pip 安装的用法：可以用环境变量 PATCH_BROWSER_DB 指定路径。" % path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        fail("读取 %s 失败: %r" % (path, e))
    except ValueError as e:
        fail("%s 不是合法 JSON：%s\n       手改过的话先修好语法，或从备份恢复。" % (path.name, e))

    brands = raw.get("brands") or {}
    if not brands:
        fail("%s 里没有 brands（品牌 -> dll 名 + 搜索根目录）" % path.name)
    BRANDS = {b: {"dll": cfg["dll"], "roots": list(cfg.get("roots") or [])}
              for b, cfg in brands.items()}

    db = {}
    try:
        for brand, vers in (raw.get("patch_db") or {}).items():
            db[brand] = {ver: _sites_from_json(sites) for ver, sites in vers.items()}
    except (TypeError, ValueError, KeyError) as e:
        fail("%s 里的 patch_db 格式不对：%s" % (path.name, e))
    PATCH_DB = db
    return path


def save_db(path=None):
    """把内存里的补丁点写回 patch_db.json（保留 _readme 之类的其它键）。"""
    path = Path(path) if path else DB_PATH
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        if not isinstance(raw, dict):
            raw = {}
    except (OSError, ValueError):
        raw = {}
    raw["brands"] = {b: {"dll": cfg["dll"], "roots": cfg["roots"]}
                     for b, cfg in BRANDS.items()}
    raw["patch_db"] = {b: {ver: {"0x%X" % off: orig for off, orig in sites}
                           for ver, sites in vers.items()}
                       for b, vers in PATCH_DB.items()}
    text = json.dumps(raw, ensure_ascii=False, indent=2) + "\n"
    try:
        path.write_text(text, encoding="utf-8")
    except OSError as e:
        fail("写回 %s 失败: %r" % (path, e))
    ok("已更新参数文件: %s" % path)


# ---------------------------------------------------------------------------
# README 里的版本表：从 patch_db.json 生成，收录新版本时顺手刷新
# 文件不存在、或没有 version-table 标记 → 安静跳过，绝不因此报错
# ---------------------------------------------------------------------------
MD_TABLE_FILES = ("README.md", "README.en.md")
MD_TABLE_BEGIN = "<!-- version-table:start -->"
MD_TABLE_END = "<!-- version-table:end -->"


def version_table(lang="zh"):
    """生成 Markdown 版本表（lang='zh' 用中文表头，其它用英文）。"""
    cur = "（当前）" if lang == "zh" else " (current)"
    head = ("| 浏览器 | 版本 | 目标文件 |" if lang == "zh"
            else "| Browser | Version | Target file |")
    rows = [head, "| --- | --- | --- |"]
    for brand in sorted(PATCH_DB):
        dll = BRANDS.get(brand, {}).get("dll", "?")
        for i, ver in enumerate(sorted(PATCH_DB[brand], key=vkey, reverse=True)):
            rows.append("| %s | %s%s | %s |"
                        % (brand.capitalize(), ver, cur if i == 0 else "", dll))
    return "\n".join(rows)


def update_md_tables():
    """把版本表写回带标记的 README；文件不在 / 没标记 / 写不进去都只提示，不报错。"""
    updated, skipped = [], []
    for name in MD_TABLE_FILES:
        path = DB_PATH.with_name(name)
        if not path.is_file():
            skipped.append(name)
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            skipped.append(name)
            continue
        if MD_TABLE_BEGIN not in text or MD_TABLE_END not in text:
            skipped.append(name)
            continue
        lang = "en" if ".en." in name else "zh"
        before, rest = text.split(MD_TABLE_BEGIN, 1)
        after = rest.split(MD_TABLE_END, 1)[1]
        new = (before + MD_TABLE_BEGIN + "\n" + version_table(lang) + "\n"
               + MD_TABLE_END + after)
        if new == text:
            continue
        try:
            path.write_text(new, encoding="utf-8")
            updated.append(name)
        except OSError as e:
            warn("刷新 %s 失败（不影响打补丁）: %r" % (name, e))
    if updated:
        ok("已刷新版本表: " + "、".join(updated))
    if skipped:
        info("跳过版本表（文件不在或没有 version-table 标记）: " + "、".join(skipped))
    if not updated and not skipped:
        info("版本表已是最新")
    return updated


def scan_brand(brand, versions=None):
    """扫描 BRANDS[brand] 的搜索根目录，返回 {版本: dll路径}。

    versions=None 收录所有版本目录，否则只收录集合内的版本。
    同版本命中多个根目录时保留先扫到的那个。
    """
    cfg = BRANDS[brand]
    found = {}
    for root in cfg["roots"]:
        p = Path(root)
        if not p.is_dir():
            continue
        try:
            subs = [d for d in p.iterdir() if d.is_dir()]
        except OSError:
            continue
        for vdir in subs:
            if not VERSION_RE.match(vdir.name):
                continue
            if versions is not None and vdir.name not in versions:
                continue
            dll = vdir / cfg["dll"]
            if dll.is_file():
                found.setdefault(vdir.name, dll)
    return found


def infer_brand(dll):
    """只给了 --path 时，从文件名反推品牌，用于提示文案与查表。"""
    n = dll.name.lower()
    if n.startswith("msedge"):
        return "edge"
    if n.startswith("chrome"):
        return "chrome"
    return None


def resolve_dll(brand, version, explicit_path, installed=None):
    """确定要操作的 dll 路径和版本号。"""
    if explicit_path:
        dll = Path(explicit_path)
        if not dll.is_file():
            fail("找不到文件: " + str(dll))
        ver = version or (dll.parent.name if VERSION_RE.match(dll.parent.name) else None)
        if not ver:
            fail("无法从路径判断版本，请用 --version 指定")
        return dll, ver

    if installed is None:
        installed = scan_brand(brand)
    if version:
        if version not in installed:
            found = ", ".join(sorted(installed, key=vkey)) or "无"
            fail("版本 %s 未在本机检测到，当前检测到：%s" % (version, found))
        return installed[version], version

    ver = max(installed, key=vkey)
    return installed[ver], ver


def is_writable(path):
    """能否以读写方式打开（只开句柄，不写任何字节）。"""
    try:
        with open(path, "r+b"):
            return True
    except OSError:
        return False


def backup_path_for(dll, backup_dir, suffix):
    name = dll.name + suffix
    return Path(backup_dir) / name if backup_dir else dll.with_name(name)


def write_report(dll):
    """dry-run 的最后一行：写入权限提示。"""
    info("写入权限：%s" % ("可写" if is_writable(dll) else
                          "不可写，正式打补丁需要以管理员身份运行，且先关闭浏览器"))


# ===========================================================================
# 子命令 1：debug-port —— 默认用户数据目录下也能开远程调试端口
# ===========================================================================
# 「闸门形状」匹配：小版本自动更新后，用它在 dll 里重新定位补丁点（--locate）。
#
# 编译出来的校验代码长这样：
#     mov  eax, 2                  ; NotStartedReason::kDisabledByDefaultUserDataDir
#     cmp  byte ptr [rsp+d1], 1    ; std::optional::has_value()
#     jne  <拒绝>                   ; 算不出来 -> 按「是默认目录」处理，fail-closed
#     <第二条检查>                  ; 目录是否等于默认目录（见下面两种形态）
#     jne  <拒绝>
#     <放行>
# 栈偏移 d1/d2、跳转位移、以及前后文都会随构建变化，所以只固定指令骨架，
# 变化的字节留通配符。跳转有 6 字节（0F 85 rel32）和 2 字节（75 rel8）两种形态：
# 同一个闸门在不同调用点被内联时形态不同，两套都要扫。
#
# 第二条检查见过两种编译结果，语义等价，都要认：
#     80 bc 24 .. .. .. .. 00    cmp  byte ptr [rsp+d2], 0     ← 154 这一代
#     f6 84 24 .. .. .. .. 01    test byte ptr [rsp+d2], 1     ← 更老的一代（如 Edge 153）
#
# 已打过补丁的 dll 里，那两条 jcc 已经变成等长 NOP（`--restore` 之前一直是 NOP），
# 所以还要认「NOP 形态」，否则会把「早就打过补丁」误报成「没找到闸门指令」。
# ---------------------------------------------------------------------------
_GATE_HEAD = rb"\xb8\x02\x00\x00\x00\x80\xbc\x24..\x00\x00\x01"
_GATE_MIDS = (
    rb"\x80\xbc\x24..\x00\x00\x00",           # cmp  byte ptr [rsp+d2], 0
    rb"\xf6\x84\x24..\x00\x00\x01",           # test byte ptr [rsp+d2], 1
)
_GATE_JCCS = (
    # (每条 jcc 的字节数, 未打补丁的两条 jcc, 已打补丁的两条 jcc, 是否已打补丁)
    (6, rb"\x0f\x85....", rb"\x0f\x85....", False),
    (2, rb"\x75.", rb"\x75.", False),
    (6, rb"\x90" * 6, rb"\x90" * 6, True),    # 两条 jcc 已被等长 NOP 覆盖
    (2, rb"\x90" * 2, rb"\x90" * 2, True),
)


def _gate_re(mid, jcc1, jcc2):
    return re.compile(_GATE_HEAD + jcc1 + mid + jcc2, re.S)


GATE_PATTERNS = tuple(
    (_gate_re(mid, j1, j2), jlen, patched)
    for mid in _GATE_MIDS
    for jlen, j1, j2, patched in _GATE_JCCS
)


def locate_gate_sites(data):
    """按「闸门形状」定位补丁点，返回 (sites, patched)。

    sites   = [(偏移, 该处当前字节), ...]，按偏移排序（偏移＝jcc 操作码的位置）
    patched = True 表示这些位置已经是等长 NOP —— 也就是这个 dll **已经打过补丁**，
              此时拿不到原始字节，只能从旧版本 dll 或备份上重新定位。
    """
    for want_patched in (False, True):
        sites = []
        for pat, jlen, is_patched in GATE_PATTERNS:
            if is_patched != want_patched:
                continue
            for m in pat.finditer(data):
                base = m.start()
                j1 = base + 13                       # 第 1 个 jcc 的操作码
                j2 = base + (13 + jlen + 8)          # 中间隔一条 8 字节的 cmp
                sites.append((j1, bytes(data[j1:j1 + jlen])))
                sites.append((j2, bytes(data[j2:j2 + jlen])))
        if sites:
            return sorted(sites, key=lambda s: s[0]), want_patched
    return [], False


def load_patch_sites(brand, version):
    return [(off, hexb(orig), b"\x90" * len(hexb(orig)))
            for off, orig in PATCH_DB[brand][version]]


def verify(data, sites, expect_patched=False):
    """返回 (是否全部匹配, 出错偏移, 期望, 实际)。"""
    for off, orig, new in sites:
        want = new if expect_patched else orig
        cur = bytes(data[off:off + len(want)])
        if cur != want:
            return False, off, want, cur
    return True, None, None, None


def cmd_debug_port(args):
    # --- --locate：只读扫描，打印可直接贴进 PATCH_DB 的补丁点 ---
    if args.locate:
        brand = args.browser
        if args.path:
            dll = Path(args.path)
            if not dll.is_file():
                fail("找不到文件: " + str(dll))
            ver = args.version or (dll.parent.name if VERSION_RE.match(dll.parent.name) else "unknown")
            brand = brand or infer_brand(dll)
        else:
            if not brand:
                fail("--locate 需要 --browser 或 --path")
            inst = scan_brand(brand)
            if not inst:
                fail("未在搜索目录里找到 %s 的 dll，请用 --path 指定" % brand)
            if args.version:
                if args.version not in inst:
                    fail("未找到版本 %s，检测到：%s"
                         % (args.version, ", ".join(sorted(inst, key=vkey))))
                ver = args.version
            else:
                ver = max(inst, key=vkey)
            dll = inst[ver]

        info("扫描: %s" % dll)
        with open(dll, "rb") as f:
            with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as data:
                sites, patched = locate_gate_sites(data)
        if patched:
            ok("该 dll 已打过补丁：%d 处闸门都是 NOP，没有原始字节可以收录。" % len(sites))
            info("要拿原始补丁点，请扫一份**没打过补丁**的同版本 dll（或从备份恢复后再扫）。")
            return
        if not sites:
            fail("没找到闸门指令：该版本代码结构可能变了，按 docs/cdp_user_data_dir_check.md 手工定位。")
        print('        "%s": {' % ver)
        for off, orig in sites:
            print('            "0x%X": "%s",' % (off, orig.hex(" ").upper()))
        print('        },')
        info("共 %d 个补丁点。" % len(sites))
        if args.save:
            if not brand or brand not in BRANDS:
                fail("--save 需要能确定品牌：请用 --browser，或让 dll 名是 chrome.dll / msedge.dll。")
            PATCH_DB.setdefault(brand, {})[ver] = [(off, orig.hex(" ").upper())
                                                   for off, orig in sites]
            save_db()
            update_md_tables()
        else:
            where = 'patch_db.json 的 patch_db["%s"]' % brand if brand else "patch_db.json"
            info("贴进 %s 里，再跑 --dry-run 校验；加 --save 可以直接写进去。" % where)
        if brand not in BRANDS:
            info("新品牌的话，还要在 patch_db.json 的 brands 里补上 dll 名与搜索根目录。")
        return

    # --- --list：已收录的版本 + 本机检测到的版本 ---
    if args.list:
        if args.markdown:
            print(version_table("zh"))
        if args.update_md:
            update_md_tables()
        if args.markdown or args.update_md:
            return
        for brand in sorted(PATCH_DB):
            print("%s:" % brand)
            for ver in sorted(PATCH_DB[brand], key=vkey):
                print("  %s  (%d 个补丁点)" % (ver, len(PATCH_DB[brand][ver])))
            inst = scan_brand(brand)          # 所有已安装版本，不只已收录的
            if inst:
                known = [v for v in sorted(inst, key=vkey) if v in PATCH_DB[brand]]
                new = [v for v in sorted(inst, key=vkey) if v not in PATCH_DB[brand]]
                if known:
                    print("  本机检测到: " + ", ".join(known))
                if new:
                    print("  未收录但已安装: " + ", ".join(new)
                          + "  （auto / 一键修复.bat 会现场定位并收录）")
        return

    # --- 正式流程 ---
    if not args.browser and not args.path:
        fail("需要 --browser chrome|edge，或用 --path 指定 dll 路径（也可 --list）")

    brand = args.browser
    if args.path:
        dll = Path(args.path)
        if brand is None:
            brand = infer_brand(dll)
        if brand is None:
            fail("无法从文件名判断品牌（%s），请用 --browser 指定。" % dll.name)
        installed = {}
    else:
        # 所有已安装版本都算候选，不只已收录的 —— 否则浏览器一更新就会报「未找到已支持版本」
        installed = scan_brand(brand)
        if not installed:
            fail("未找到 %s 安装目录。搜索：%s"
                 % (brand, ", ".join(BRANDS[brand]["roots"])))

    dll, version = resolve_dll(brand, args.version, args.path, installed)
    if version not in PATCH_DB.get(brand, {}):
        supported = ", ".join(sorted(PATCH_DB.get(brand, {}), key=vkey)) or "无"
        fail("版本 %s 不在 patch_db.json 的已收录列表（%s）。\n"
             "       省事做法：python patch_browser.py auto --all（或双击 一键修复.bat）——\n"
             "       没收录的版本会现场按闸门形状定位、打完自动收录；\n"
             "       只想看定位结果：python patch_browser.py debug-port --browser %s --locate --save"
             % (version, supported, brand))

    sites = load_patch_sites(brand, version)
    info("目标: %s (%s)，%d 个补丁点" % (dll, version, len(sites)))

    bak = backup_path_for(dll, args.backup_dir, ".orig.bak")

    if args.restore:
        if not bak.is_file():
            fail("备份不存在: " + str(bak))
        try:
            shutil.copy2(bak, dll)
        except PermissionError:
            fail("恢复被拒绝：%s 不可写。请以管理员身份运行，并先关闭浏览器。" % dll)
        except OSError as e:
            fail("恢复失败: %r" % e)
        ok("已从备份恢复: " + str(dll))
        return

    try:
        data = bytearray(open(dll, "rb").read())
    except OSError as e:
        fail("读取失败（浏览器没关？）: %r" % e)

    good, off, want, cur = verify(data, sites)
    if not good:
        # 校验不符有两种可能：① 已经被本工具打过补丁（补丁点已是 NOP）；
        # ② 真的是版本/构建变了。先区分开，否则「已打过」会误报成「版本不符」。
        done, _, _, _ = verify(data, sites, expect_patched=True)
        if done:
            ok("该 dll 已打过补丁：%d 个补丁点均为 NOP，无需重复写入。" % len(sites))
            info("还原原始 dll：python patch_browser.py debug-port --browser %s --restore" % brand)
            return
        fail("校验失败，偏移 0x%x：期望 %s，实际 %s。版本/构建不符或文件已被改过，拒绝写入。\n"
             "       若是自动更新后的新构建，先跑 --locate 重新定位补丁点。" %
             (off, want.hex(" "), cur.hex(" ")))
    info("写入前校验通过")

    if args.dry_run:
        ok("dry-run：%d 个补丁点全部匹配，未写入。" % len(sites))
        write_report(dll)
        return

    if not is_writable(dll):
        fail("没有写入权限: %s\n       需要以管理员身份运行（右键 PowerShell → 以管理员身份运行），且先关闭浏览器。\n"
             "       只想确认补丁点是否匹配，可以用 --dry-run。" % dll)

    if not args.no_backup:
        if bak.is_file():
            info("备份已存在（跳过）: " + str(bak))
        else:
            try:
                Path(bak.parent).mkdir(parents=True, exist_ok=True)
                shutil.copy2(dll, bak)
            except PermissionError:
                fail("备份写入被拒绝: %s\n       备份目录不可写：用 --backup-dir 指定一个可写目录，或以管理员身份运行。" % bak)
            except OSError as e:
                fail("备份失败: %r" % e)
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
    info("回滚：python patch_browser.py debug-port --browser %s --restore" % brand)


# ===========================================================================
# 子命令 2：no-debugger —— 忽略 JS 的 debugger 语句
# ===========================================================================
# 运行时函数表项 32 字节，name 在 +8，entry 在 +16
ENTRY_STRIDE = 32
OFF_NAME = 8
OFF_ENTRY = 16
OFF_NARGS = 24
OFF_RSIZE = 25

NAME_RE = re.compile(rb"_?[A-Za-z][A-Za-z0-9_]*")
FUNC_NAME = b"HandleDebuggerStatement"

# 闸门骨架：mov rcx,[r8+disp32] / cmp byte ptr [rcx+disp8], 1 / jcc
# 变化的只有 isolate->debug_ 的偏移（disp32）和 Debug::break_points_active_ 的
# 偏移（disp8），都留通配符；jcc 的形态与位移也随构建变。
GATE_PREFIX = rb"\x49\x8b\x88.{4}\x80\x79.\x01"
GATE_JNE_OFF = 11          # jcc 操作码在骨架里的下标
GATE_SCAN_WINDOW = 0x200   # 在函数入口后多少字节内找闸门


class LocateError(Exception):
    pass


def find_all(hay, needle):
    out = []
    i = hay.find(needle)
    while i >= 0:
        out.append(i)
        i = hay.find(needle, i + 1)
    return out


# ---------------------------------------------------------------------------
# 最小 PE 解析：段表 + RVA/文件偏移互转
# ---------------------------------------------------------------------------
class Pe:
    def __init__(self, data):
        self.data = data
        d = data
        if d[:2] != b"MZ":
            raise LocateError("不是 PE 文件")
        pe_off = struct.unpack_from("<I", d, 0x3C)[0]
        if d[pe_off:pe_off + 4] != b"PE\0\0":
            raise LocateError("没有 PE 签名")
        coff = pe_off + 4
        nsec, = struct.unpack_from("<H", d, coff + 2)
        opt_size, = struct.unpack_from("<H", d, coff + 16)
        opt = coff + 20
        magic, = struct.unpack_from("<H", d, opt)
        if magic != 0x20B:
            raise LocateError("不是 PE32+（x64），magic=%#x" % magic)
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


# ---------------------------------------------------------------------------
# 定位：函数名字符串 -> 运行时函数表项 -> 函数地址 -> 闸门
# ---------------------------------------------------------------------------
def find_runtime_function(pe, name=FUNC_NAME):
    """返回 (表项文件偏移, function_id, entry_off, nargs, rsize)。"""
    data = pe.data
    hits = find_all(data, name + b"\x00")
    if len(hits) != 1:
        raise LocateError("函数名串 '%s' 在 dll 里命中 %d 处（要求恰好 1 处）"
                          % (name.decode(), len(hits)))
    str_off = hits[0]
    str_rva = pe.off_to_rva(str_off)
    if str_rva is None:
        raise LocateError("函数名串不在任何段内")
    str_va = pe.image_base + str_rva

    ptr = struct.pack("<Q", str_va)
    refs = []
    for s in pe.sections:
        if not s["rawsize"]:
            continue
        blob = data[s["raw"]:s["raw"] + s["rawsize"]]
        for i in find_all(blob, ptr):
            refs.append(s["raw"] + i)
    if len(refs) != 1:
        raise LocateError("引用该字符串的指针有 %d 个（要求恰好 1 个）" % len(refs))

    entry_off_struct = refs[0] - OFF_NAME
    fid, itype = struct.unpack_from("<ii", data, entry_off_struct)
    entry, = struct.unpack_from("<Q", data, entry_off_struct + OFF_ENTRY)
    nargs, rsize = struct.unpack_from("<bb", data, entry_off_struct + OFF_NARGS)

    # --- 结构校验：证明这确实是 kIntrinsicFunctions[] 而不是撞车的普通数据 ---
    if itype != 0:
        raise LocateError("表项 intrinsic_type=%d（期望 0=RUNTIME）" % itype)
    for k in (-2, -1, 0, 1, 2):
        base = entry_off_struct + k * ENTRY_STRIDE
        if base < 0 or base + ENTRY_STRIDE > len(data):
            raise LocateError("表项越界（k=%d）" % k)
        nfid, ntype = struct.unpack_from("<ii", data, base)
        if nfid != fid + k:
            raise LocateError("相邻表项 id 不连续（k=%d: %d，期望 %d）"
                              % (k, nfid, fid + k))
        if ntype not in (0, 1):
            raise LocateError("相邻表项 intrinsic_type=%d 非法（k=%d）" % (ntype, k))
        nptr, = struct.unpack_from("<Q", data, base + OFF_NAME)
        nm = pe.read_cstr(nptr)
        if nm is None or not NAME_RE.fullmatch(nm):
            raise LocateError("相邻表项 name 不是可读标识符（k=%d）" % k)
        nent, = struct.unpack_from("<Q", data, base + OFF_ENTRY)
        if not pe.is_exec(nent - pe.image_base):
            raise LocateError("相邻表项 entry 不在可执行段（k=%d）" % k)

    entry_rva = entry - pe.image_base
    if not pe.is_exec(entry_rva):
        raise LocateError("函数 entry 不在可执行段")
    entry_off = pe.rva_to_off(entry_rva)
    if entry_off is None:
        raise LocateError("函数 entry 无法映射到文件偏移")

    return dict(struct_off=entry_off_struct, func_id=fid, name_ptr=str_va,
                name_str_off=str_off, entry_rva=entry_rva, entry_off=entry_off,
                nargs=nargs, rsize=rsize)


def locate_gate(pe, entry_off, patched=False):
    """在函数体内定位 break_points_active() 闸门，返回 (站点偏移, 原始字节, 目标字节)。

    patched=True 时按「已打过补丁」的形态找（jne 已变成 jmp）。
    """
    data = pe.data
    o8 = b"\xeb" if patched else b"\x75"            # jmp / jne rel8
    o32 = b"\xe9" if patched else b"\x0f\x85"       # jmp / jne rel32
    pats = (
        (re.compile(GATE_PREFIX + re.escape(o8) + rb".", re.S), 2),
        (re.compile(GATE_PREFIX + re.escape(o32) + rb".{4}", re.S), 6),
    )
    body = data[entry_off:entry_off + GATE_SCAN_WINDOW]
    hits = {}
    for pat, jlen in pats:
        for m in pat.finditer(body):
            off = entry_off + m.start() + GATE_JNE_OFF
            hits[off] = (off, bytes(data[off:off + jlen]), jlen)
    sites = [hits[k] for k in sorted(hits)]
    if len(sites) != 1:
        raise LocateError("函数入口后 %#x 字节内找到 %d 处闸门（要求恰好 1 处）"
                          % (GATE_SCAN_WINDOW, len(sites)))
    off, orig, jlen = sites[0]
    new = (b"\xeb" if jlen == 2 else b"\xe9") + orig[1:]
    return off, orig, new


def derive(data, patched=False):
    """完整推导一次，返回报告字典。"""
    pe = Pe(data)
    fn = find_runtime_function(pe)
    site_off, site_orig, site_new = locate_gate(pe, fn["entry_off"], patched=patched)
    fn.update(site_off=site_off, site_orig=site_orig, site_new=site_new)
    fn["size"] = len(data)
    return fn


def report(fn, dll, version, site_label="原始"):
    info("目标: %s (%s)" % (dll, version))
    info("函数名串: 文件偏移 %#x（唯一命中）" % fn["name_str_off"])
    info("运行时表项: 文件偏移 %#x   function_id=%d  nargs=%d result_size=%d"
         % (fn["struct_off"], fn["func_id"], fn["nargs"], fn["rsize"]))
    info("目标函数: Runtime_HandleDebuggerStatement  RVA=%#x  文件偏移=%#x"
         % (fn["entry_rva"], fn["entry_off"]))
    info("闸门站点: 文件偏移 %#x  [%s] %s -> %s"
         % (fn["site_off"], site_label,
            fn["site_orig"].hex(" ").upper(), fn["site_new"].hex(" ").upper()))


def cmd_list_no_debugger():
    for brand in sorted(BRANDS):
        print("%s:" % brand)
        installed = scan_brand(brand)
        if not installed:
            print("  （未检测到安装）")
            continue
        for ver in sorted(installed, key=vkey):
            dll = installed[ver]
            try:
                data = dll.read_bytes()
                # 先按「未打补丁」形态推导，失败再按「已打补丁」形态 —— 否则已打过补丁的
                # dll 会因为找不到 jne 而被报成「未定位」，看不出真实状态。
                try:
                    fn = derive(data, patched=False)
                    st = "未打补丁"
                except LocateError:
                    fn = derive(data, patched=True)
                    st = "已打补丁"
                print("  %-16s %-8s 函数 RVA=%#x 闸门 %#x  %s"
                      % (ver, st, fn["entry_rva"], fn["site_off"], fn["site_orig"].hex(" ").upper()))
            except LocateError as e:
                print("  %-16s 未定位   (%s)" % (ver, e))
            except OSError as e:
                print("  %-16s 读取失败 (%r)" % (ver, e))


def cmd_no_debugger(args):
    if args.list:
        cmd_list_no_debugger()
        return

    if args.locate and not args.browser and not args.path:
        fail("--locate 需要 --browser 或 --path")
    if not args.browser and not args.path:
        fail("需要 --browser chrome|edge，或用 --path 指定 dll 路径（也可 --list）")

    brand = args.browser
    if args.path:
        installed = {}
    else:
        installed = scan_brand(brand)
        if not installed:
            fail("未找到 %s 安装目录。搜索：%s" % (brand, ", ".join(BRANDS[brand]["roots"])))

    dll, version = resolve_dll(brand, args.version, args.path, installed)
    if brand is None:  # 只给了 --path，从文件名反推品牌，仅用于提示文案
        brand = infer_brand(dll) or "?"

    try:
        data = dll.read_bytes()
    except OSError as e:
        fail("读取失败: %r" % e)

    # --- 推导（先按「未打补丁」形态，失败再按「已打补丁」形态） ---
    try:
        fn = derive(data, patched=False)
        already = False
    except LocateError:
        try:
            fn = derive(data, patched=True)
            already = True
        except LocateError as e:
            fail("定位失败：%s\n       该构建的代码结构可能变了，"
                 "参考 docs/no_debugger_statement.md 手工复核。" % e)

    if args.locate:
        report(fn, dll, version, "已打补丁" if already else "原始")
        info("共 1 个补丁点（等长改写，文件大小不变）")
        return

    report(fn, dll, version, "已打补丁" if already else "原始")

    bak = backup_path_for(dll, args.backup_dir, ".nodebug.bak")

    if args.restore:
        if not bak.is_file():
            fail("备份不存在: " + str(bak))
        try:
            shutil.copy2(bak, dll)
        except PermissionError:
            fail("恢复被拒绝：%s 不可写。请以管理员身份运行，并先关闭浏览器。" % dll)
        except OSError as e:
            fail("恢复失败: %r" % e)
        ok("已从备份恢复: %s" % dll)
        info("注意：备份是该工具上次打补丁前的状态，不包含其他工具的改动。")
        return

    if already:
        ok("该 dll 已打过补丁：闸门 %#x 已是 %s，无需重复写入。"
           % (fn["site_off"], fn["site_orig"].hex(" ").upper()))
        info("还原原始 dll：python patch_browser.py no-debugger --browser %s --restore" % brand)
        return

    info("写入前校验通过")

    if args.dry_run:
        ok("dry-run：定位与校验全部通过，未写入。")
        write_report(dll)
        return

    if not is_writable(dll):
        fail("没有写入权限: %s\n       需要以管理员身份运行，且先关闭浏览器。\n"
             "       只想确认能否定位，可以用 --dry-run。" % dll)

    if not args.no_backup:
        if bak.is_file():
            info("备份已存在（跳过）: %s" % bak)
        else:
            try:
                Path(bak.parent).mkdir(parents=True, exist_ok=True)
                shutil.copy2(dll, bak)
            except PermissionError:
                fail("备份写入被拒绝: %s\n       用 --backup-dir 指定可写目录，或以管理员身份运行。" % bak)
            except OSError as e:
                fail("备份失败: %r" % e)
            ok("已备份: %s" % bak)

    buf = bytearray(data)
    off = fn["site_off"]
    buf[off:off + len(fn["site_new"])] = fn["site_new"]

    try:
        with open(dll, "r+b") as f:
            f.seek(0)
            f.write(bytes(buf))
    except PermissionError:
        fail("写入被拒绝。需要管理员权限，并关闭浏览器。")
    except OSError as e:
        fail("写入失败（浏览器没关？）: %r" % e)

    # --- 写后复验：重读整个文件，重新推导一次，确认闸门已是 jmp ---
    try:
        fn2 = derive(dll.read_bytes(), patched=True)
    except LocateError as e:
        fail("写入后复验失败：%s" % e)
    if fn2["site_off"] != off:
        fail("写入后复验失败：闸门位置变了（%#x -> %#x）" % (off, fn2["site_off"]))

    ok("补丁完成：闸门 %#x 已由 %s 改为 %s（等长，文件大小不变）。"
       % (off, fn["site_orig"].hex(" ").upper(), fn2["site_orig"].hex(" ").upper()))
    info("验证：打开 DevTools（F12）后执行 (function(){debugger;return 42})()")
    info("      期望直接返回 42、Sources 面板不断住；手工步骤见 README「验证」小节")
    info("回滚：python patch_browser.py no-debugger --browser %s --restore" % brand)


# ===========================================================================
# 子命令 3：auto —— 只给一个路径，其余参数自己推导，然后打补丁
# ===========================================================================
def _dll_in_dir(d, want_version=None):
    """在一个目录里找目标 dll；目录本身放 dll、或是一堆 <版本>\\ 子目录都能认。

    返回 (dll 路径, 品牌) 或 None。给了 want_version 就只认那个版本目录。
    """
    for brand in sorted(BRANDS):
        cand = d / BRANDS[brand]["dll"]
        if cand.is_file() and (not want_version or d.name == want_version):
            return cand, brand
    try:
        subs = [s for s in d.iterdir() if s.is_dir() and VERSION_RE.match(s.name)]
    except OSError:
        subs = []
    found = []
    for sub in subs:
        for brand in sorted(BRANDS):
            cand = sub / BRANDS[brand]["dll"]
            if cand.is_file():
                found.append((vkey(sub.name), sub.name, cand, brand))
    if not found:
        return None
    if want_version:
        for _, name, cand, brand in found:
            if name == want_version:
                return cand, brand
        return None
    found.sort(key=lambda x: x[0])
    return found[-1][2], found[-1][3]


def loose_target(path, want_version=None):
    """dll / 浏览器 exe / 版本目录 / Application 目录 都接受，返回 (dll 路径, 品牌或 None)。"""
    p = Path(path)
    if not p.exists():
        fail("路径不存在: " + str(p))
    dlls = " 或 ".join(BRANDS[b]["dll"] for b in sorted(BRANDS))

    if p.is_dir():
        hit = _dll_in_dir(p, want_version)
        if hit:
            return hit
        if want_version:
            fail("这个目录里没找到版本 %s 的 %s：%s" % (want_version, dlls, p))
        fail("这个目录（含版本子目录）里没找到 %s：%s" % (dlls, p))

    if p.name.lower().endswith(".exe"):
        # Chrome / Edge 的 exe 在 Application\ 下，dll 在 Application\<版本>\ 里
        hit = _dll_in_dir(p.parent, want_version)
        if hit:
            return hit
        if want_version:
            fail("exe 所在目录里没找到版本 %s 的 %s：%s" % (want_version, dlls, p.parent))
        fail("exe 所在目录（含版本子目录）里没找到 %s：%s" % (dlls, p.parent))

    return p, infer_brand(p)


def version_near(dll):
    """从 dll 所在目录往上找形如 154.0.8037.93 的版本目录名。"""
    d = dll.parent
    for _ in range(3):
        if VERSION_RE.match(d.name):
            return d.name
        if d.parent == d:
            break
        d = d.parent
    return None


def ensure_backup(dll, backup_dir, suffix, no_backup):
    """备份原文件（已存在就跳过），返回备份路径。"""
    bak = backup_path_for(dll, backup_dir, suffix)
    if no_backup:
        return bak
    if bak.is_file():
        info("备份已存在（跳过）: %s" % bak)
        return bak
    try:
        Path(bak.parent).mkdir(parents=True, exist_ok=True)
        shutil.copy2(dll, bak)
    except PermissionError:
        fail("备份写入被拒绝: %s\n       用 --backup-dir 指定可写目录，或以管理员身份运行。" % bak)
    except OSError as e:
        fail("备份失败: %r" % e)
    ok("已备份: %s" % bak)
    return bak


def write_patched(dll, edits):
    """把 [(偏移, 新字节)] 写进 dll（整份重写），返回写后的字节供复验。"""
    data = bytearray(dll.read_bytes())
    for off, new in edits:
        data[off:off + len(new)] = new
    try:
        with open(dll, "r+b") as f:
            f.seek(0)
            f.write(bytes(data))
    except PermissionError:
        fail("写入被拒绝。需要管理员权限（右键 PowerShell → 以管理员身份运行），并关闭浏览器。")
    except OSError as e:
        fail("写入失败（浏览器没关？）: %r" % e)
    return dll.read_bytes()


def auto_one(dll, brand, version, args, targets):
    """处理一份 dll（品牌/版本/补丁点都已定好），返回 (done, skipped, warnings, errors)。"""
    done = []
    skipped = []
    warnings = []
    errors = []

    if not args.dry_run and not is_writable(dll):
        errors.append("%s：没有写入权限（正式打补丁要以管理员身份运行，且先关闭浏览器）；"
                      "本次没动它。" % (brand or dll.name))
        return done, skipped, warnings, errors

    try:
        data = dll.read_bytes()
    except OSError as e:
        errors.append("%s：读取失败 %r（浏览器没关？）" % (brand or dll.name, e))
        return done, skipped, warnings, errors

    # ---------- debug-port ----------
    if "debug-port" in targets:
        st, note = state_debug_port(data, brand, version)
        if st == "已打补丁":
            skipped.append("debug-port：%s" % note)
        elif st == "未收录":
            info("debug-port：patch_db.json 里没有 %s %s，现场按闸门形状定位…"
                 % (brand or "该品牌", version))
            located, _ = locate_gate_sites(data)
            if not located:
                errors.append("debug-port：没找到闸门指令 —— 这个构建的这段代码形状变了"
                              "（比如更老的大版本），照 docs/cdp_user_data_dir_check.md 手工定位。"
                              "本次没动它。")
            elif len(located) < 2 or len(located) % 2:
                errors.append("debug-port：现场定位到 %d 个补丁点（期望偶数且 ≥2），"
                              "形状可疑，没写入。" % len(located))
            elif args.dry_run:
                skipped.append("debug-port：dry-run，未写入（现场定位到 %d 个补丁点）" % len(located))
            else:
                sites = [(off, bytes(data[off:off + len(orig)]), b"\x90" * len(orig))
                         for off, orig in located]
                info("debug-port：定位到 %d 个补丁点" % len(sites))
                ensure_backup(dll, args.backup_dir, ".orig.bak", args.no_backup)
                d2 = write_patched(dll, [(off, new) for off, _, new in sites])
                good, off, want, cur = verify(d2, sites, expect_patched=True)
                if not good:
                    fail("debug-port 写入后校验失败，偏移 0x%x：期望 %s 实际 %s"
                         % (off, want.hex(" "), cur.hex(" ")))
                done.append("debug-port：%d 个补丁点已 NOP 化" % len(sites))
                if brand in BRANDS and version != "unknown":
                    PATCH_DB.setdefault(brand, {})[version] = [(o, b.hex(" ").upper())
                                                              for o, b in located]
                    save_db()
                    update_md_tables()
                else:
                    warnings.append("debug-port：品牌/版本没认出来，补丁点没有写进 patch_db.json")
        elif st == "未打补丁":
            sites = load_patch_sites(brand, version)
            if args.dry_run:
                skipped.append("debug-port：dry-run，未写入（%s）" % note)
            else:
                ensure_backup(dll, args.backup_dir, ".orig.bak", args.no_backup)
                d2 = write_patched(dll, [(off, new) for off, _, new in sites])
                good, off, want, cur = verify(d2, sites, expect_patched=True)
                if not good:
                    fail("debug-port 写入后校验失败，偏移 0x%x：期望 %s 实际 %s"
                         % (off, want.hex(" "), cur.hex(" ")))
                done.append("debug-port：%d 个补丁点已 NOP 化" % len(sites))
        else:
            errors.append("debug-port：%s（%s）—— 该构建和 patch_db.json 里的记录对不上，"
                          "先跑 debug-port --locate --save 重新定位。本次没动它。" % (st, note))

    # ---------- no-debugger ----------
    if "no-debugger" in targets:
        st, note = state_no_debugger(data)
        if st == "已打补丁":
            skipped.append("no-debugger：%s" % note)
        elif st == "未打补丁":
            fn = derive(data, patched=False)
            if args.dry_run:
                skipped.append("no-debugger：dry-run，未写入（%s）" % note)
            else:
                ensure_backup(dll, args.backup_dir, ".nodebug.bak", args.no_backup)
                d2 = write_patched(dll, [(fn["site_off"], fn["site_new"])])
                try:
                    fn2 = derive(d2, patched=True)
                except LocateError as e:
                    fail("no-debugger 写入后复验失败：%s" % e)
                if fn2["site_off"] != fn["site_off"]:
                    fail("no-debugger 写入后复验失败：闸门位置变了（0x%X -> 0x%X）"
                         % (fn["site_off"], fn2["site_off"]))
                done.append("no-debugger：闸门 0x%X 已由 %s 改为 %s"
                            % (fn["site_off"], fn["site_orig"].hex(" ").upper(),
                               fn2["site_orig"].hex(" ").upper()))
        else:
            errors.append("no-debugger：%s（%s）—— 见 docs/no_debugger_statement.md。本次没动它。"
                          % (st, note))

    return done, skipped, warnings, errors


def cmd_auto(args):
    """--path 处理指定的一份 dll；--all 扫 patch_db.json 里所有品牌，各处理最新已安装版本。"""
    targets = [t for t in ("debug-port", "no-debugger") if not args.only or args.only == t]

    if bool(args.path) == bool(args.all):
        fail("--path 和 --all 必须二选一：--path 指定一份 dll，--all 扫所有品牌的最新版。")

    jobs = []
    if args.all:
        for brand in sorted(BRANDS):
            inst = scan_brand(brand)
            if not inst:
                info("未检测到 %s 安装，跳过" % brand)
                continue
            ver = max(inst, key=vkey)
            older = [v for v in sorted(inst, key=vkey) if v != ver]
            if older:
                info("%s：本机还有旧版本目录 %s，只处理最新版 %s"
                     % (brand, "、".join(older), ver))
            jobs.append((inst[ver], brand, ver))
        if not jobs:
            fail("没找到本机已安装的 Chrome / Edge；用 --path 直接指定 dll。")
    else:
        dll, brand = loose_target(args.path, args.version)
        jobs.append((dll, brand, args.version or version_near(dll) or "unknown"))

    done = []
    skipped = []
    warnings = []
    errors = []
    for dll, brand, version in jobs:
        info("目标: %s" % dll)
        info("品牌: %s    版本: %s" % (brand or "未知（按 dll 名判断不出来）", version))
        if len(jobs) > 1 or not args.all:
            info("要处理的补丁: %s" % "、".join(targets))
        d, s, w, e = auto_one(dll, brand, version, args, targets)
        done += d
        skipped += s
        warnings += w
        errors += e

    # ---------- 汇总 ----------
    print()
    for line in done:
        ok(line)
    for line in skipped:
        info("跳过：" + line)
    for line in warnings:
        warn(line)
    for line in errors:
        print("[FAIL] " + line, file=sys.stderr)
    if args.dry_run:
        info("dry-run：没有写入任何文件。去掉 --dry-run 再来一次就是正式打补丁。")
    elif done:
        info("验证 CDP：浏览器加 --remote-debugging-port=9222，访问 http://127.0.0.1:9222/json/version")
        info("验证 debugger：开 DevTools（F12）执行 (function(){debugger;return 42})()，期望直接返回 42")
        info('回滚：python patch_browser.py <子命令> --path "<dll>" --restore')
    elif not errors:
        info("没有需要改动的地方。")
    if errors:
        sys.exit(1)


# ===========================================================================
# 不带子命令直接运行：全量自检 + 教程（只读，不写任何文件）
# ===========================================================================
def running_browsers():
    """正在运行的浏览器进程名（小写、去重）；取不到返回 None（跳过这项检查）。"""
    try:
        p = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                           capture_output=True, text=True, timeout=15,
                           encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    procs = set()
    for line in p.stdout.splitlines():
        name = line.split(",")[0].strip().strip('"').lower()
        if name in ("chrome.exe", "msedge.exe"):
            procs.add(name)
    return sorted(procs)


def state_debug_port(data, brand, version):
    """体检一个 dll 的 CDP 补丁状态，返回 (状态, 说明)。"""
    if version not in PATCH_DB.get(brand, {}):
        # 没收录：现场按闸门形状看一眼，能区分「已打过补丁」和「真的没收录」
        sites, patched = locate_gate_sites(data)
        if patched:
            return "已打补丁", "版本未收录，但 %d 处闸门都已是 NOP" % len(sites)
        if sites:
            return "未收录", "不在 patch_db.json 里，现场定位到 %d 个补丁点" % len(sites)
        return "未收录", "该构建不在 patch_db.json 里，先跑 --locate 定位补丁点"
    sites = load_patch_sites(brand, version)
    n = len(sites)
    if verify(data, sites)[0]:
        return "未打补丁", "%d 个补丁点全部匹配原始字节，待写入" % n
    if verify(data, sites, expect_patched=True)[0]:
        return "已打补丁", "%d/%d 个补丁点均为 NOP" % (n, n)
    _, off, want, cur = verify(data, sites)
    return "校验失败", "偏移 0x%X 期望 %s 实际 %s" % (off, want.hex(" "), cur.hex(" "))


def state_no_debugger(data):
    """体检一个 dll 的 debugger 补丁状态，返回 (状态, 说明)。"""
    try:
        fn = derive(data, patched=False)
        return "未打补丁", "闸门 0x%X = %s，待写入" % (fn["site_off"], fn["site_orig"].hex(" ").upper())
    except LocateError:
        pass
    try:
        fn = derive(data, patched=True)
        return "已打补丁", "闸门 0x%X = %s（等长 jmp）" % (fn["site_off"], fn["site_orig"].hex(" ").upper())
    except LocateError as e:
        return "未定位", str(e)


def full_check():
    """把两个子命令 × 两个浏览器体检一遍，再打印下一步与教程。只读。"""
    print("=" * 72)
    print("patch_browser.py 自检 —— 只读，不会写入任何文件")
    print("=" * 72)

    # ---------- 环境 ----------
    print("\n[环境]")
    pyver = "%d.%d.%d" % sys.version_info[:3]
    if sys.version_info >= (3, 11):
        ok("Python %s（需要 >=3.11）" % pyver)
    else:
        warn("Python %s 低于要求的 3.11" % pyver)
    info("平台 %s %s ；只依赖标准库，无需安装依赖" % (platform.system(), platform.machine()))
    nver = sum(len(v) for v in PATCH_DB.values())
    info("参数文件 %s：%d 个品牌 / %d 个版本条目（版本更新后改这个文件）"
         % (DB_PATH, len(BRANDS), nver))

    procs = running_browsers()
    if procs:
        warn("检测到正在运行：%s —— 打补丁前先完全退出浏览器" % "、".join(procs))
    elif procs is not None:
        ok("没有检测到正在运行的 chrome.exe / msedge.exe")

    # ---------- 逐品牌、逐版本体检（每个 dll 只读一次，两个子命令共用） ----------
    rows = {"debug-port": [], "no-debugger": []}
    todo = []
    writable_any = None

    for brand in sorted(BRANDS):
        installed = scan_brand(brand)
        if not installed:
            for tool in rows:
                rows[tool].append((brand, "-", "未安装", "没在这个品牌的搜索目录里找到 dll", None, ""))
            todo.append("没检测到 %s 安装，跳过；装好后重跑本自检" % brand)
            continue
        newest = max(installed, key=vkey)
        for ver in sorted(installed, key=vkey):
            dll = installed[ver]
            # 旧版本目录（同品牌装了不止一个版本）打不打都行，提示里标出来
            is_old = ver != newest
            old_tag = "[旧版本]" if is_old else ""
            old_note = "（旧版本目录，一般可以不管）" if is_old else ""
            try:
                data = dll.read_bytes()
            except OSError as e:
                for tool in rows:
                    rows[tool].append((brand, ver, "读取失败", repr(e), None, old_tag))
                todo.append("%s %s 读不出来：%r" % (brand, ver, e))
                continue

            st_cdp, note_cdp = state_debug_port(data, brand, ver)
            st_nd, note_nd = state_no_debugger(data)
            rows["debug-port"].append((brand, ver, st_cdp, note_cdp,
                                       backup_path_for(dll, None, ".orig.bak").is_file(), old_tag))
            rows["no-debugger"].append((brand, ver, st_nd, note_nd,
                                        backup_path_for(dll, None, ".nodebug.bak").is_file(), old_tag))
            if writable_any is None:
                writable_any = is_writable(dll)
            del data

            for tool, st, note in (("debug-port", st_cdp, note_cdp),
                                   ("no-debugger", st_nd, note_nd)):
                if st == "已打补丁":
                    continue
                # 指定 --version 才能精确落到这一份 dll 上；不带 --version 时打的是最新版本
                cmd = ("python patch_browser.py %s --browser %s" % (tool, brand)
                       if not is_old else
                       "python patch_browser.py %s --browser %s --version %s" % (tool, brand, ver))
                if st == "未打补丁":
                    todo.append("%s %s 的 %s 补丁未打%s —— 以管理员运行：%s"
                                % (brand, ver, tool, old_note, cmd))
                elif st == "未收录":
                    todo.append("%s %s 的 %s 未收录%s —— 双击 一键修复.bat（或 %s --locate --save）"
                                % (brand, ver, tool, old_note, cmd))
                else:
                    todo.append("%s %s 的 %s 状态异常（%s：%s）%s"
                                % (brand, ver, tool, st, note, old_note))

    for tool, title in (("debug-port", "[debug-port] 默认用户数据目录下开 CDP 端口"),
                        ("no-debugger", "[no-debugger] 忽略 JS 的 debugger 语句")):
        print("\n" + title)
        if tool == "debug-port":
            for brand in sorted(PATCH_DB):
                info("已收录版本 %s：%s（没收录的会现场定位，不用等更新）"
                     % (brand, "、".join(sorted(PATCH_DB[brand], key=vkey))))
        for brand, ver, st, note, bak, old in rows[tool]:
            mark = {"已打补丁": ok, "未安装": info}.get(st, warn)
            line = "%s（%s）" % (st, note) if note else st
            if old:
                line += " " + old
            tail = "  备份 %s" % ("有" if bak else "无") if bak is not None else ""
            mark("%-7s %-16s %s%s" % (brand, ver, line, tail))

    # ---------- 写权限 ----------
    print("\n[权限]")
    if writable_any is None:
        info("没有体检到 dll，跳过写权限检查")
    elif writable_any:
        ok("Program Files 下的 dll 当前可写（已是管理员），可以直接打补丁")
    else:
        info("当前不可写 Program Files —— 正式打补丁要「以管理员身份运行」并先完全退出浏览器；"
             "--dry-run / --list / 本自检 都不需要管理员")

    # ---------- 下一步 ----------
    print("\n[下一步]")
    if todo:
        for t in todo:
            warn(t)
    else:
        ok("没有需要处理的事情：检测到的浏览器，两个补丁都已就位。")

    # ---------- 教程 ----------
    print("\n[教程] <子命令> = debug-port（别名 cdp）｜ no-debugger（别名 nodebug）｜ auto")
    print("       （下面按 git clone 的用法写；pip 装的把 `python patch_browser.py` 换成 `patch-browser`）")
    print("  0) 最省事：一键修复.bat 双击（浏览器更新后补丁失效就跑它）；命令行等价写法：")
    print("       python patch_browser.py auto --all --dry-run    # 先预览，不动手")
    print("       python patch_browser.py auto --all              # 需要管理员")
    print('       python patch_browser.py auto --path "C:\\...\\chrome.dll"   # 只处理一份')
    print("  1) 只读体检（不需要管理员）：")
    print("       python patch_browser.py <子命令> --list")
    print("       python patch_browser.py <子命令> --browser chrome --dry-run")
    print("  2) 正式打补丁（提权 + 先完全退出浏览器）：")
    print("       python patch_browser.py <子命令> --browser chrome")
    print("  3) 回滚（用 dll 旁边的备份，后缀 .orig.bak / .nodebug.bak）：")
    print("       python patch_browser.py <子命令> --browser chrome --restore")
    print("  4) 浏览器自动更新后补丁会失效（参数都在 patch_db.json 里，改它就行）：")
    print("       debug-port —— --locate 重新定位闸门，加 --save 直接写回 patch_db.json；")
    print("       no-debugger —— 每次运行都自己重新推导，一般不用改任何东西。")
    print("  5) 验证：")
    print("       CDP：浏览器加 --remote-debugging-port=9222，访问 http://127.0.0.1:9222/json/version")
    print('       debugger：开 DevTools（F12），Console 执行 (function(){debugger;return 42})()，')
    print("                 期望直接返回 42、Sources 面板不断住（未打补丁会断在那里）")
    print("  6) 只看某个子命令的完整参数：python patch_browser.py <子命令> --help")
    print("  详细说明见 README.md / README.en.md、docs/cdp_user_data_dir_check.md、docs/no_debugger_statement.md")


# ===========================================================================
# 命令行
# ===========================================================================
def add_common_args(p, help_list, help_locate, help_backup):
    p.add_argument("--browser", choices=sorted(BRANDS),
                   help="目标浏览器（不传 --path 时必填）")
    p.add_argument("--path", help="直接指定 dll 路径（覆盖自动检测）")
    p.add_argument("--version", help="指定版本号（默认自动检测已安装的最高版本）")
    p.add_argument("--backup-dir", help=help_backup)
    p.add_argument("--no-backup", action="store_true", help="不生成备份")
    p.add_argument("--dry-run", action="store_true", help="只校验，不写入")
    p.add_argument("--restore", action="store_true", help="从备份恢复原始 dll")
    p.add_argument("--list", action="store_true", help=help_list)
    p.add_argument("--locate", action="store_true", help=help_locate)


def main(argv=None):
    load_db()                 # 参数都在 patch_db.json 里（位置见 resolve_db_path）
    prog = os.path.basename(sys.argv[0] or "") or "patch-browser"
    if prog.lower().endswith(".exe"):
        prog = prog[:-4]
    if prog in ("-c", "-m", "python", "python.exe"):
        prog = "patch-browser"
    ap = argparse.ArgumentParser(
        prog=prog,
        description="给官方 Chrome / Edge 打二进制补丁：debug-port（默认目录下也能开 CDP 端口）"
                    " / no-debugger（忽略 JS 的 debugger 语句）"
                    " / auto（只给一个路径，品牌、版本、补丁点自己推导）",
        epilog=("不带子命令直接运行＝把两个补丁全部体检一遍并打印教程；例：%s auto --path "
                "\"C:\\Program Files\\Google\\Chrome\\Application\\154.0.8037.93\\chrome.dll\""
                % prog),
    )
    sub = ap.add_subparsers(dest="command", metavar="{debug-port,no-debugger,auto}")

    p1 = sub.add_parser("debug-port", aliases=["cdp"],
                        help="允许在默认用户数据目录下开启远程调试端口",
                        description="允许 Chrome / Edge 在默认用户数据目录下开启远程调试端口")
    add_common_args(p1,
                    help_list="列出支持与检测到的版本",
                    help_locate="扫描 dll 里的闸门指令，打印可贴进 patch_db.json 的补丁点（只读）",
                    help_backup="备份存放目录（默认放在 dll 旁边，后缀 .orig.bak）")
    p1.add_argument("--save", action="store_true",
                    help="配合 --locate：把定位到的补丁点直接写回 patch_db.json")
    p1.add_argument("--markdown", action="store_true",
                    help="配合 --list：直接打印可贴进 README 的 Markdown 版本表")
    p1.add_argument("--update-md", action="store_true",
                    help="配合 --list：把版本表写回 README（文件不在或没标记就跳过）")
    p1.set_defaults(func=cmd_debug_port)

    p2 = sub.add_parser("no-debugger", aliases=["nodebug"],
                        help="让浏览器忽略 JS 的 debugger 语句",
                        description="让官方 Chrome / Edge 忽略 JS 的 debugger 语句（二进制补丁）")
    add_common_args(p2,
                    help_list="列出已安装版本与补丁状态",
                    help_locate="只做定位推导并打印报告（只读）",
                    help_backup="备份存放目录（默认放在 dll 旁边，后缀 .nodebug.bak）")
    p2.set_defaults(func=cmd_no_debugger)

    p3 = sub.add_parser("auto",
                        help="只给一个路径，品牌 / 版本 / 补丁点全部自己推导后再打",
                        description="只给一个路径（dll、浏览器 exe 或版本目录都行）："
                                    "品牌、版本、补丁点自己推导，该打的打、已打的跳过；"
                                    "--all 则扫所有品牌的最新已安装版本")
    p3.add_argument("--path", help="chrome.dll / msedge.dll、浏览器 exe、Application 目录或版本目录")
    p3.add_argument("--all", action="store_true",
                    help="不用给路径：扫 patch_db.json 里的所有品牌，各处理本机最新已安装版本")
    p3.add_argument("--version", help="手动指定版本号（默认从目录名推断）")
    p3.add_argument("--only", choices=["debug-port", "no-debugger"],
                    help="只处理其中一个补丁（默认两个都处理）")
    p3.add_argument("--backup-dir", help="备份存放目录（默认放在 dll 旁边）")
    p3.add_argument("--no-backup", action="store_true", help="不生成备份")
    p3.add_argument("--dry-run", action="store_true", help="只推导与校验，不写入")
    p3.set_defaults(func=cmd_auto)

    args = ap.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        full_check()          # 裸跑：全量自检 + 教程（只读）
        return
    func(args)


if __name__ == "__main__":
    main()
