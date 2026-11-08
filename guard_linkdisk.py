# -*- coding: utf-8 -*-
"""
guard_linkdisk.py — 联动盘（v6.15 新增）
========================================
一句话解释这个功能：

    软件的「数据」实际放到别的盘，C 盘原地址只留一个「门牌号」（NTFS junction）。
    软件照旧按老路径读写，完全感觉不到变化；C 盘空间却已经腾出来了。

为什么需要「主动提示」而不是等用户自己发现：
    绝大多数人根本不知道自己的 C 盘是被某个软件的缓存目录吃掉的。
    等 C 盘变红再来清，往往已经几十 GB、软件还在运行，清理风险高。
    所以在两个「最有说服力」的时机提醒：
      1. 某个软件第一次被使用时（用户刚建立使用习惯，改起来成本最低）
      2. 某个软件在 C 盘的数据超过阈值（默认 2GB）时（此刻清走的收益最直观）

安全原则（与 guard_wizard / guard_migrate 一脉相承）：
    · 先复制到目标盘 → 逐目录核对文件数与字节 → 才改名源目录 → 建 junction
    · 任一步失败立即把源目录改名还原，绝不留「软件打不开」的残局
    · 只搬「数据目录」（AppData / ProgramData 下属于该软件的那一层），
      安装目录交给「软件迁移」页，因为需要处理快捷方式/注册表/服务引用
    · 正在被独占占用的文件复制会失败 —— 那就整体放弃并如实告知，不硬删
    · 全程不联网、不改注册表、不改快捷方式，随时可一键还原

纯逻辑模块，不 import PySide6；系统调用集中在 running_exes / 复制函数里，
测试可用替身覆盖。
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

from guard_appdir import APP_DIR
import guard_wizard as gwizard

# ---------------------------------------------------------------
# 常量
# ---------------------------------------------------------------

LINKS_FILE = APP_DIR / "link_disk.json"
DEFAULT_TARGET_DIRNAME = "C盘守卫联动盘"

THRESHOLD_GB = 2.0          # 超过多少 GB 主动提醒（用户可在页面改）
MIN_PROMPT_MB = 80.0        # 首次使用提示的最低占用（太小没必要打扰）
COOLDOWN_DAYS = 7.0         # 用户选「先不用」后，多少天内不再问
PROMPT_GAP_SEC = 420        # 两次提醒之间至少间隔（避免开机弹一串）
SIZE_TTL_SEC = 1800         # 占用测算缓存有效期（30 分钟）
UNKNOWN_REFRESH_SEC = 45    # 进程枚举节流

# 这些 exe 所在路径不属于「用户装的软件」，直接跳过
_SKIP_EXE = (
    "\\windows\\", "\\windowsapps\\", "\\$recycle.bin",
    "\\windows defender\\", "\\windows\\systemapps\\",
)

# 常见软件的数据目录别名（进程名/安装目录名 → 数据目录可能叫什么）
# 只做「提高命中率」用，命不中也不影响：目录列表会在提示里摊开给用户看。
_ALIASES = {
    "wechat": ("wechat", "weixin", "微信"),
    "weixin": ("weixin", "wechat", "微信"),
    "wechatappex": ("wechat", "wechatappex"),
    "qq": ("qq", "tencent files", "tencent"),
    "tim": ("tim", "tencent files"),
    "wxwork": ("wxwork", "wework", "企业微信"),
    "dingtalk": ("dingtalk", "钉钉"),
    "feishu": ("feishu", "lark", "飞书"),
    "wps": ("kingsoft", "wps office", "wps"),
    "wpp": ("kingsoft", "wps office"),
    "et": ("kingsoft", "wps office"),
    "baidunetdisk": ("baidunetdisk", "百度网盘"),
    "thunder": ("thunder network", "迅雷"),
    "netease": ("netease", "cloudmusic", "网易云音乐"),
    "cloudmusic": ("netease", "cloudmusic"),
    "qqmusic": ("tencent", "qqmusic"),
    "tencentmeeting": ("tencent", "tencentmeeting", "wemeet"),
    "wemeet": ("tencent", "wemeet", "tencentmeeting"),
    "douyin": ("douyin", "抖音"),
    "jianyingpro": ("jianyingpro", "capcut", "剪映"),
    "capcut": ("capcut", "jianyingpro"),
    "chrome": ("google", "chrome"),
    "msedge": ("microsoft", "edge"),
    "firefox": ("mozilla", "firefox"),
    "code": ("code", "vscode"),
    "cursor": ("cursor",),
    "adobe": ("adobe",),
    "photoshop": ("adobe",),
    "steam": ("steam",),
    "onenote": ("microsoft", "onenote"),
    "telegram": ("telegram",),
    "discord": ("discord",),
    "spotify": ("spotify",),
    "zoom": ("zoom",),
    "obs": ("obs-studio", "obs"),
}

_NORM_RE = re.compile(r"[\s\.\-_（）()]+")
_VER_RE = re.compile(r"(?:v)?\d+(?:\.\d+)*$")


# ---------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------

def human_size(n) -> str:
    return gwizard.human_size(n)


def _norm(s) -> str:
    """目录名归一：小写、去空格点横线下划线括号，便于宽松匹配。"""
    return _NORM_RE.sub("", str(s or "").lower())


def _strip_ver(name: str) -> str:
    return _VER_RE.sub("", str(name or "")).strip() or str(name or "")


def _is_junction(path) -> bool:
    """是不是重解析点（junction / symlink）。

    注意：指向目录的 junction 同时带「目录」和「重解析点」两个属性位，
    所以只能看 0x400，不能要求「不是目录」—— 否则永远判不出来。
    """
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return bool(getattr(st, "st_file_attributes", 0) & 0x400)


def _real_dir(path) -> bool:
    """存在、是目录、不是链接（junction / symlink 都不算真实目录）。"""
    try:
        p = Path(path)
        if not p.is_dir():
            return False
        if p.is_symlink():
            return False
        st = os.lstat(p)
        fa = getattr(st, "st_file_attributes", 0)
        return not (fa & 0x400)
    except OSError:
        return False


# ---------------------------------------------------------------
# 一、正在运行的软件（原生枚举，毫秒级，不依赖 PowerShell）
# ---------------------------------------------------------------

if sys.platform == "win32":
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _psapi = ctypes.WinDLL("psapi", use_last_error=True)
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _k32.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD))
    _k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _psapi.EnumProcesses.argtypes = (
        ctypes.POINTER(wintypes.DWORD), wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD))
    _psapi.EnumProcesses.restype = wintypes.BOOL
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
else:                                        # 非 Windows：仅用于测试/导入
    _k32 = _psapi = None


def running_exes(max_procs: int = 2000) -> list:
    """返回当前正在运行的进程可执行文件完整路径（小写）。

    用 EnumProcesses + QueryFullProcessImageNameW：
      · 一次调用拿到全部 PID，再逐个开句柄取路径，全程 ~10-30ms
      · 不需要管理员权限（PROCESS_QUERY_LIMITED_INFORMATION 就够）
      · 系统进程 / 拒绝访问的进程直接跳过，不报错
    """
    if _psapi is None:
        return []
    out = []
    buf = (wintypes.DWORD * max_procs)()
    need = wintypes.DWORD()
    try:
        if not _psapi.EnumProcesses(buf, ctypes.sizeof(buf), ctypes.byref(need)):
            return []
    except Exception:
        return []
    n = int(need.value) // ctypes.sizeof(wintypes.DWORD)
    name_buf = ctypes.create_unicode_buffer(1024)
    for i in range(min(n, max_procs)):
        pid = int(buf[i])
        if pid <= 0:
            continue
        h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            continue
        try:
            size = wintypes.DWORD(len(name_buf))
            if _k32.QueryFullProcessImageNameW(h, 0, name_buf, ctypes.byref(size)):
                exe = name_buf.value
                if exe:
                    out.append(exe.lower())
        except Exception:
            pass
        finally:
            try:
                _k32.CloseHandle(h)
            except Exception:
                pass
    return out


def _install_roots() -> list:
    roots = []
    for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        v = os.environ.get(env)
        if v:
            roots.append(v)
    la = os.environ.get("LOCALAPPDATA")
    if la:
        roots.append(os.path.join(la, "Programs"))
    pd = os.environ.get("ProgramData")
    if pd:
        roots.append(pd)
    seen, out = set(), []
    for r in roots:
        k = _norm(r)
        if k and k not in seen and os.path.isdir(r):
            seen.add(k)
            out.append(r)
    return out


def app_from_exe(exe: str) -> dict:
    """从一个 exe 路径推出「这是哪个软件」。推不出就返回 {}。

    取名字时不能直接拿安装根下的一级目录 —— 那往往是「发行商」
    （Program Files\\Google\\Chrome\\Application\\chrome.exe 的一级是 Google），
    按发行商去匹配数据目录会把同一家的所有软件都算进来。
    所以从 exe 所在目录往上走，跳过 Application / bin / 版本号这类通用层，
    落到真正的软件名（Chrome）。
    """
    low = str(exe or "").lower()
    raw = str(exe or "")
    if not low or not low.endswith(".exe"):
        return {}
    if any(s in low for s in _SKIP_EXE):
        return {}
    base = os.path.basename(low)
    stem = os.path.splitext(base)[0]
    # 这些进程是运行时/更新器/子进程，代表不了「用户在用某个软件」
    if stem in ("updater", "update", "uninstall", "unins000", "setup",
                "installer", "crashpad_handler", "helper", "crashreport",
                "console", "node", "python", "pythonw", "msedgewebview2",
                "regsvr32", "rundll32"):
        return {}
    for root in _install_roots():
        rl = root.lower().rstrip("\\") + "\\"
        if not low.startswith(rl):
            continue
        rel = raw[len(rl):]                # 用原始大小写切，保留真实目录名
        parts = [p for p in rel.split("\\") if p]
        if len(parts) < 2:
            continue
        dirs = parts[:-1]                 # 去掉文件名
        idx = len(dirs) - 1
        while idx > 0 and (_is_generic_dir(dirs[idx]) or _VERSION_RE.match(dirs[idx])):
            idx -= 1
        name_dir = dirs[idx]
        app_dir = os.path.join(root, *parts[:idx + 1])
        name = _clean_app_name(name_dir)
        if not name or _norm(name) in _VENDOR_ONLY:
            return {}                     # 落到发行商层（只有厂商没有具体软件），不猜
        return {"name": name, "dir_name": name_dir,
                "app_dir": app_dir, "exe": exe,
                "key": _norm(name_dir) or _norm(name)}
    # 便携软件：安装在别的盘的自定义目录，这里不主动提议（C 盘没占空间）
    return {}


_VENDOR_ONLY = {
    "microsoft", "windows", "common files", "google", "tencent", "adobe",
    "apple", "intel", "nvidia", "nvidia corporation", "amd", "realtek",
    "mozilla", "package cache", "windowsapps", "microsoft shared",
    "microsoft office", "microsoft update health tools", "modifiablewindowsapps",
}

_GENERIC_SUBDIR = {
    "application", "app", "apps", "bin", "binaries", "build", "client",
    "code", "core", "current", "data", "files", "lib", "module", "modules",
    "program", "release", "resources", "root", "runtime", "src", "system",
    "system32", "update", "updater", "x64", "x86", "amd64", "win64", "win32",
    "desktop", "platform", "framework", "framework64",
}

_VERSION_RE = re.compile(r"^v?\d+(?:[\.\-]\d+)*$", re.I)
_OFFICE_RE = re.compile(r"^office\d+$", re.I)


def _is_generic_dir(name: str) -> bool:
    n = str(name or "").strip().lower()
    return n in _GENERIC_SUBDIR or n in _VENDOR_ONLY or bool(_OFFICE_RE.match(n))


def _clean_app_name(name: str) -> str:
    n = _strip_ver(str(name or "").strip())
    n = re.sub(r"\s*\((?:x86|x64|32|64)\s*(?:bit)?\)\s*$", "", n, flags=re.I)
    n = re.sub(r"\s*(?:x64|x86|64位|32位)$", "", n, flags=re.I)
    return n.strip() or str(name or "").strip()


def _tokens(app: dict) -> list:
    """匹配数据目录用的候选词。越长越具体，优先级越高。"""
    raw = []
    name = app.get("name") or ""
    dirn = app.get("dir_name") or ""
    stem = os.path.splitext(os.path.basename(str(app.get("exe") or "")))[0]
    for s in (name, dirn, stem):
        if not s:
            continue
        raw.append(_norm(s))
        raw.append(_norm(_strip_ver(s)))
        for k in (str(s).lower(), stem.lower()):
            if k in _ALIASES:
                raw.extend(_norm(x) for x in _ALIASES[k])
    seen, out = set(), []
    for t in raw:
        if len(t) < 3 or t in seen:
            continue
        # 发行商 / 通用词（microsoft、tencent、programs…）不能当匹配词，
        # 否则 Edge 会把整个 Microsoft 目录树都算成自己的数据。
        if t in _VENDOR_ONLY or t in _GENERIC_SUBDIR:
            continue
        seen.add(t)
        out.append(t)
    out.sort(key=len, reverse=True)          # 具体词优先
    return out


def _hit(dirname: str, tokens: list) -> bool:
    d = _norm(dirname)
    if not d:
        return False
    for t in tokens:
        if d == t:
            return True
        if len(t) >= 4 and (d.startswith(t) or t.startswith(d)):
            return True
    return False


def _appdata_bases() -> list:
    out = []
    for env in ("APPDATA", "LOCALAPPDATA"):
        v = os.environ.get(env)
        if v and os.path.isdir(v):
            out.append(v)
    up = os.environ.get("USERPROFILE")
    if up:
        p = os.path.join(up, "AppData", "LocalLow")
        if os.path.isdir(p):
            out.append(p)
    pd = os.environ.get("ProgramData")
    if pd and os.path.isdir(pd):
        out.append(pd)
    # 去重
    seen, uniq = set(), []
    for p in out:
        k = _norm(p)
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    return uniq


def _under_program_files(p) -> bool:
    """是不是「程序安装位置」——安装目录不归联动盘管（交给软件迁移页）。"""
    lp = str(p or "").lower().rstrip("\\")
    for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        r = os.environ.get(env)
        if not r:
            continue
        rl = r.lower().rstrip("\\")
        if lp == rl or lp.startswith(rl + "\\"):
            return True
    la = os.environ.get("LOCALAPPDATA")
    if la:
        rl = (la.lower().rstrip("\\")) + "\\programs"
        if lp == rl or lp.startswith(rl + "\\"):
            return True
    return False


def _bad_candidate(path) -> bool:
    """明显不该作为数据目录的候选：程序本体、更新器、带文件后缀的怪目录。"""
    p = Path(path)
    if _under_program_files(p):
        return True
    if re.search(r"\.(exe|dll|sys|lnk|msi|zip|tmp|log)$", p.name, re.I):
        return True
    # 更新器 / 安装器自己的目录：搬了省不了多少空间，还可能让自动更新坏掉
    n = _norm(p.name)
    if any(w in n for w in ("update", "updater", "installer", "uninstall")):
        return True
    return False


_INDEX = {"ts": 0.0, "items": []}
_INDEX_TTL = 600.0            # 目录索引 10 分钟有效（安装/卸载后靠 TTL 自然刷新）


def _dir_index(force: bool = False) -> list:
    """把 AppData / ProgramData 的「一层 + 二层」目录名扫成一张索引表。

    为什么要索引：一次体检要判上百个软件，若每个软件都去 scandir 一遍
    AppData，那是上万次目录读取（几十秒的磁盘噪音）。索引只建一次，
    之后每个软件在内存里做字符串匹配，几十毫秒就能判完。

    返回 [(路径, 目录名, 层级, 父目录名), ...]，层级 1 = 直接挂在 AppData 下，
    层级 2 = 发行商目录里面那一层（如 Tencent\\WeChat）。
    """
    now = time.time()
    if not force and _INDEX["items"] and now - _INDEX["ts"] < _INDEX_TTL:
        return _INDEX["items"]
    items = []
    for base in _appdata_bases():
        children = []
        try:
            with os.scandir(base) as it:
                for i, e in enumerate(it):
                    if i >= 400:
                        break
                    try:
                        if not e.is_dir(follow_symlinks=False):
                            continue
                        st = e.stat(follow_symlinks=False)
                        if getattr(st, "st_file_attributes", 0) & 0x400:
                            continue
                        children.append((e.name, e.path))
                    except OSError:
                        continue
        except OSError:
            continue
        scans = 0
        for cname, cpath in children:
            items.append((cpath, cname, 1, ""))
            if scans >= 300:          # 每个基础目录最多再往下看 300 个，防止爆量
                continue
            scans += 1
            try:
                with os.scandir(cpath) as it2:
                    for j, e2 in enumerate(it2):
                        if j >= 80:
                            break
                        try:
                            if e2.is_dir(follow_symlinks=False):
                                items.append((e2.path, e2.name, 2, cname))
                        except OSError:
                            continue
            except OSError:
                continue
    _INDEX["items"] = items
    _INDEX["ts"] = now
    return items


def find_data_dirs(app: dict) -> list:
    """找出这个软件在 C 盘的数据目录（AppData / ProgramData 下那一层）。

    只认「看起来属于它」的目录：
      · 一级：%APPDATA%\\<名字>、%LOCALAPPDATA%\\<名字>、LocalLow\\<名字>、ProgramData\\<名字>
      · 二级：%APPDATA%\\<发行商>\\<名字>（如 Tencent\\WeChat、Google\\Chrome）
    不动安装目录 —— 那属于「软件迁移」的事（要处理快捷方式/注册表/服务）。
    """
    tokens = _tokens(app)
    if not tokens:
        return []
    app_dir = str(app.get("app_dir") or "").lower().rstrip("\\")

    def same_as_app(p) -> bool:
        """候选目录是不是「程序本体」自己（装在 ProgramData 等非标准位置时）。"""
        lp = str(p).lower().rstrip("\\")
        return bool(app_dir) and (lp == app_dir or lp.startswith(app_dir + "\\"))

    found = []
    for path, name, depth, _parent in _dir_index():
        if not _hit(name, tokens):
            continue
        if depth == 1:
            n = _norm(name)
            # 发行商 / 通用层（Microsoft、Google、Programs…）本身不能当数据目录，
            # 否则一搬就是「同厂商所有软件」的数据，风险太大。
            if n in _VENDOR_ONLY or n in _GENERIC_SUBDIR:
                continue
        if _bad_candidate(path) or same_as_app(path):
            continue
        found.append(path)
    # 去重（Local 与 LocalLow 可能同名）+ 去掉被别的目录包含的父目录
    seen, uniq = set(), []
    for p in sorted(set(found), key=len):
        k = _norm(p)
        if k in seen:
            continue
        if any(_norm(p).startswith(_norm(q) + "\\") for q in uniq):
            continue
        seen.add(k)
        uniq.append(p)
    return uniq


# ---------------------------------------------------------------
# 二、占用测算（带提前退出，绝不为一个数字扫爆磁盘）
# ---------------------------------------------------------------

def dir_size(path, cap_bytes: int = None, max_files: int = 400000,
             max_seconds: float = 4.0) -> tuple:
    """返回 (字节, 文件数, 是否因 cap/时限提前结束)。

    cap_bytes 命中即停 —— 判定「是否超过 2GB」只需要知道它「已经超过」，
    不需要知道到底 2.1GB 还是 21GB，省下的 I/O 是几十倍。
    """
    total = files = 0
    t0 = time.perf_counter()
    stack = [str(path)]
    hit_cap = False
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            st = e.stat(follow_symlinks=False)
                            if getattr(st, "st_file_attributes", 0) & 0x400:
                                continue
                            stack.append(e.path)
                            continue
                        total += e.stat(follow_symlinks=False).st_size
                        files += 1
                    except OSError:
                        continue
                    if cap_bytes and total >= cap_bytes:
                        return total, files, True
                    if files >= max_files:
                        return total, files, True
                    if (files & 1023) == 0 and time.perf_counter() - t0 > max_seconds:
                        return total, files, True
        except OSError:
            continue
    return total, files, hit_cap


def measure(dirs, cap_bytes: int = None, max_seconds: float = 4.0) -> dict:
    """测算一组目录的总占用。"""
    total = files = 0
    deadline = time.perf_counter() + max(max_seconds, 0.5)
    for d in dirs:
        left = max(0.2, deadline - time.perf_counter())
        cap_left = None
        if cap_bytes:
            cap_left = max(0, cap_bytes - total)
            if cap_left <= 0:
                return {"size": total, "files": files, "capped": True}
        sz, cnt, _ = dir_size(d, cap_left, max_seconds=left)
        total += sz
        files += cnt
        if cap_bytes and total >= cap_bytes:
            return {"size": total, "files": files, "capped": True}
    return {"size": total, "files": files, "capped": False}


# ---------------------------------------------------------------
# 三、提示文案（全大白话，这是整个功能的说服力所在）
# ---------------------------------------------------------------

def explain(app: dict, size: int, dirs, trigger: str) -> str:
    """生成提示正文：先说现象 → 再说方案 → 再说好处 → 最后说兜底。"""
    name = app.get("name") or "这个软件"
    size_txt = human_size(size) if size else "一些"
    if trigger == "首次使用":
        head = f"你第一次打开「{name}」，它开始往 C 盘存东西了。"
    else:
        head = f"「{name}」在 C 盘已经占了 {size_txt}，而且还会继续变大。"
    shown = "\n".join(f"   · {d}" for d in list(dirs)[:3])
    if len(list(dirs)) > 3:
        shown += f"\n   · 等共 {len(list(dirs))} 个位置"
    return (
        f"{head}\n\n"
        f"它占的是这些地方：\n{shown}\n\n"
        "━━━ 要不要给它建一个「联动盘」？ ━━━\n\n"
        "建了之后是这样的：\n"
        f"   • 这些文件实际搬到别的盘（比如 D 盘），C 盘只留一个「门牌号」\n"
        f"   • 「{name}」还是照原来的地址读写，它自己完全不知道搬家了\n"
        "   • 以后它再往里存东西，也自动存到 D 盘，C 盘不会再变大\n"
        "   • 想反悔随时可以一键还原，文件原封不动搬回来\n\n"
        f"好处很直白：C 盘立刻多出 {size_txt if size else '可观的空间'}，"
        "系统不再因为空间紧张变卡、更新失败；软件使用体验一点不变。\n\n"
        "注意：如果软件正在写这些文件，可能搬不动 —— 那就下次再试，"
        "本次会自动放弃并把东西原位还原，不会弄坏软件。"
    )


def benefit_lines() -> list:
    """页面顶部展示的白话好处（三条，避免长篇大论）。"""
    return [
        "文件真正存在别的盘，C 盘只留一个「门牌号」，软件照旧能用",
        "以后新产生的数据也跟着去别的盘，C 盘不会又被慢慢填满",
        "随时能一键还原，文件原样搬回 C 盘，不留后遗症",
    ]


# ---------------------------------------------------------------
# 四、记录与触发规则
# ---------------------------------------------------------------

class Tracker:
    """联动盘档案：哪些软件问过、问过几次、建没建、用户选了啥。

    单独存一个 link_disk.json（在 APP_DIR），换电脑随机器指纹一起重建，
    不会把上一台电脑的软件清单带过来。
    """

    def __init__(self, path=None):
        self.path = Path(path) if path else LINKS_FILE
        self._lock = threading.Lock()
        self.data = self._load()

    def _load(self) -> dict:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                d.setdefault("apps", {})
                return d
        except Exception:
            pass
        return {"apps": {}}

    def save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(self.path)
        except Exception:
            pass

    # ---------- 查询 ----------
    @property
    def apps(self) -> dict:
        return self.data.setdefault("apps", {})

    def get(self, key: str) -> dict:
        return self.apps.get(key) or {}

    def status(self, key: str) -> str:
        rec = self.get(key)
        if rec.get("linked"):
            return "已建"
        if rec.get("decision") == "never":
            return "不再提示"
        return "未建"

    def linked_all(self) -> list:
        return [{**rec, "key": k} for k, rec in self.apps.items() if rec.get("linked")]

    # ---------- 登记 ----------
    def touch(self, app: dict, now: float = None) -> dict:
        now = now or time.time()
        key = app.get("key") or _norm(app.get("name"))
        rec = self.apps.setdefault(key, {
            "name": app.get("name"), "dir_name": app.get("dir_name"),
            "app_dir": app.get("app_dir"), "exe": app.get("exe"),
            "first_seen": now, "last_seen": now, "dirs": [],
            "size": 0, "prompts": [], "decision": "none", "linked": [],
        })
        rec["last_seen"] = now
        rec["name"] = app.get("name") or rec.get("name")
        rec["exe"] = app.get("exe") or rec.get("exe")
        rec["app_dir"] = app.get("app_dir") or rec.get("app_dir")
        return rec

    def set_dirs(self, key: str, dirs, size: int):
        rec = self.get(key)
        if not rec:
            return
        rec["dirs"] = [str(d) for d in dirs]
        rec["size"] = int(size or 0)
        rec["size_ts"] = time.time()

    def mark_prompted(self, key: str, trigger: str, size: int = 0,
                      silent: bool = False):
        rec = self.get(key)
        if not rec:
            return
        rec.setdefault("prompts", []).append(
            {"ts": time.time(), "trigger": trigger, "size": int(size or 0),
             "silent": bool(silent)})
        rec["prompts"] = rec["prompts"][-30:]
        if silent:
            rec["first_silent"] = True
        if trigger == "超过阈值":
            rec["last_size_prompt"] = int(size or 0)
        if trigger == "首次使用":
            rec["first_prompted"] = True

    def decide(self, key: str, decision: str, days: float = COOLDOWN_DAYS,
               now: float = None):
        rec = self.get(key)
        if not rec:
            return
        now = now or time.time()
        rec["decision"] = decision
        rec["decided_ts"] = now
        if decision == "later":
            rec["snooze_until"] = now + max(1.0, days) * 86400

    def mark_linked(self, key: str, pairs: list):
        rec = self.get(key)
        if not rec:
            return
        rec["linked"] = list(pairs)
        rec["linked_ts"] = time.time()
        rec["decision"] = "accepted"

    def clear_linked(self, key: str):
        rec = self.get(key)
        if not rec:
            return
        rec["linked"] = []
        rec["unlinked_ts"] = time.time()

    # ---------- 触发判定 ----------
    def pending(self, now: float = None, threshold_gb: float = THRESHOLD_GB,
                first_prompt: bool = True, min_mb: float = MIN_PROMPT_MB) -> list:
        """返回此刻「应该提示」的软件列表（已按优先级排序）。

        规则（缺一条都不打扰用户）：
          · 已建联动盘 / 选了「不再提示」→ 不问
          · 首次使用：从没问过 → 占用 ≥ min_mb 才问（太小没必要）
          · 超过阈值：占用 ≥ 阈值，且比上次提示时又涨了一个阈值 → 问
          · 用户选过「先不用」→ 冷却期内不问
          · 同一次运行里两次提示至少间隔 PROMPT_GAP_SEC（由调用方控制）
        """
        now = now or time.time()
        th = max(0.1, float(threshold_gb or THRESHOLD_GB)) * 2 ** 30
        min_b = max(0.0, float(min_mb or 0)) * 2 ** 20
        out = []
        for key, rec in self.apps.items():
            if rec.get("linked"):
                continue
            if rec.get("decision") == "never":
                continue
            if float(rec.get("snooze_until") or 0) > now:
                continue
            size = int(rec.get("size") or 0)
            prompts = rec.get("prompts") or []
            asked_first = bool(rec.get("first_prompted")) or bool(rec.get("first_silent"))
            asked_size = bool(rec.get("last_size_prompt"))
            trigger = ""
            if first_prompt and not asked_first:
                if size >= min_b:
                    trigger = "首次使用"
                else:
                    continue          # 太小，先登记着，等变大了再问
            elif size >= th:
                last = int(rec.get("last_size_prompt") or 0)
                if not asked_size or size >= last + th or last == 0:
                    trigger = "超过阈值"
                else:
                    continue
            else:
                continue
            if len(prompts) >= 12:
                continue              # 别没完没了（正常情况不会到这一步）
            out.append({"key": key, "app": rec, "size": size,
                        "trigger": trigger, "dirs": rec.get("dirs") or []})
        out.sort(key=lambda x: (x["trigger"] != "超过阈值", -x["size"]))
        return out


# 全局单例（引擎与 GUI 共用同一份档案）
tracker = Tracker()


# ---------------------------------------------------------------
# 五、创建 / 还原联动盘
# ---------------------------------------------------------------

def _verify(src: Path, dst: Path) -> tuple:
    """核对目标目录是否完整复制了源目录（文件数 + 字节数）。"""
    s_sz, s_cnt, s_cap = dir_size(src, max_files=2000000, max_seconds=90)
    d_sz, d_cnt, d_cap = dir_size(dst, max_files=2000000, max_seconds=90)
    if s_cap or d_cap:
        return False, f"目录太大，无法在合理时间内完成校验（源 {s_cnt} 个文件）"
    if d_cnt < s_cnt:
        return False, f"复制不完整：源 {s_cnt} 个文件，目标只有 {d_cnt} 个"
    if d_sz < s_sz:
        return False, f"复制不完整：源 {human_size(s_sz)}，目标只有 {human_size(d_sz)}"
    return True, f"{d_cnt} 个文件 / {human_size(d_sz)} 校验通过"


def _target_for(src: Path, root: Path) -> Path:
    """目标路径：<目标盘>\\C盘守卫联动盘\\<相对路径>，保留原始层级便于辨认。"""
    try:
        rel = src.resolve().relative_to(Path(src.anchor))
    except Exception:
        rel = Path(src.name)
    return root / rel


def link_one(src, target_root, log=print) -> dict:
    """把单个目录做成联动盘：复制 → 校验 → 改名 → junction → 清理旧副本。

    返回 {"ok", "src", "dst", "detail"}。任何一步失败都会把源目录还原成原名。
    """
    src = Path(src)
    if not _real_dir(src):
        return {"ok": False, "src": str(src), "dst": "",
                "detail": "不是普通目录（可能已经是链接），跳过"}
    root = Path(target_root)
    dst = _target_for(src, root)
    sys_anchor = (os.environ.get("SystemDrive") or "C:").rstrip("\\").lower() + "\\"
    if (src.anchor or "").lower() == (dst.anchor or "").lower() == sys_anchor:
        return {"ok": False, "src": str(src), "dst": str(dst),
                "detail": "目标盘和 C 盘是同一个盘，搬过去不会省空间（请选另一块盘）"}
    i = 1
    base_dst = dst
    while dst.exists():
        dst = base_dst.with_name(f"{base_dst.name}({i})")
        i += 1
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return {"ok": False, "src": str(src), "dst": str(dst),
                "detail": f"目标盘不可写：{e}"}
    log(f"   复制 {src} → {dst}")
    ok, rc = gwizard.quiet_copy(src, dst)
    if not ok:
        shutil.rmtree(dst, ignore_errors=True)
        return {"ok": False, "src": str(src), "dst": str(dst),
                "detail": f"复制失败（robocopy 返回 {rc}），可能文件正被占用"}
    ok, detail = _verify(src, dst)
    if not ok:
        shutil.rmtree(dst, ignore_errors=True)
        return {"ok": False, "src": str(src), "dst": str(dst), "detail": detail}
    bak = src.with_name(src.name + ".guard_bak")
    j = 1
    while bak.exists():
        bak = src.with_name(f"{src.name}.guard_bak{j}")
        j += 1
    try:
        os.rename(src, bak)
    except OSError as e:
        shutil.rmtree(dst, ignore_errors=True)
        return {"ok": False, "src": str(src), "dst": str(dst),
                "detail": f"源目录被占用，无法改名：{e}"}
    if not gwizard.make_junction(src, dst):
        try:
            os.rename(bak, src)
        except OSError:
            pass
        shutil.rmtree(dst, ignore_errors=True)
        return {"ok": False, "src": str(src), "dst": str(dst),
                "detail": "创建链接失败，已还原原目录"}
    # 链接可用性检查：能读到目标盘里的东西才算成功
    try:
        next(os.scandir(src), None)
    except OSError as e:
        try:
            os.rmdir(src)
        except OSError:
            pass
        try:
            os.rename(bak, src)
        except OSError:
            pass
        shutil.rmtree(dst, ignore_errors=True)
        return {"ok": False, "src": str(src), "dst": str(dst),
                "detail": f"链接创建后无法访问：{e}，已还原"}
    # 旧副本：链接已可用才删；删不掉就留着让用户手动清（不报错）
    try:
        shutil.rmtree(bak, ignore_errors=True)
    except Exception:
        pass
    return {"ok": True, "src": str(src), "dst": str(dst),
            "detail": detail + "，已建联动盘"}


def normalize_target(root) -> str:
    """把用户选的任意位置统一成「<盘>:\\C盘守卫联动盘」。

    用户可能点的是 D 盘根、D:\\C盘守卫接收、甚至某个深层目录；
    统一收到一个固定目录下，便于以后辨认和整体还原。
    """
    s = str(root or "").strip().strip('"')
    if not s:
        return ""
    if len(s) >= 2 and s[1] == ":":
        anchor = s[:2] + "\\"
    else:
        anchor = Path(s).anchor or ""
    if not anchor:
        return ""
    return str(Path(anchor) / DEFAULT_TARGET_DIRNAME)


def link_app(key: str, target_root: str, log=print,
             cancel_evt=None, t=None) -> dict:
    """为一个软件的所有数据目录建联动盘（逐个执行，失败即止并回滚已成功的）。

    回滚策略：如果第 2 个目录失败，前面已成功的也一并还原 ——
    宁可回到原样，也不留「一半在 C 盘一半在 D 盘」的糊涂状态。
    """
    t = t or tracker
    rec = t.get(key)
    if not rec:
        return {"ok": False, "detail": "没有这个软件的记录", "pairs": []}
    target_root = normalize_target(target_root) or str(target_root)
    dirs = [Path(d) for d in (rec.get("dirs") or []) if d]
    dirs = [d for d in dirs if _real_dir(d)]
    if not dirs:
        return {"ok": False, "detail": "没有找到可搬的数据目录", "pairs": []}
    # 大目录优先，先把最占空间的解决掉
    sized = []
    for d in dirs:
        sz, _, _ = dir_size(d, cap_bytes=512 * 2 ** 20, max_seconds=2.0)
        sized.append((sz, d))
    sized.sort(reverse=True)
    done = []
    for sz, d in sized:
        if cancel_evt is not None and cancel_evt.is_set():
            break
        log(f"  处理 {d}（{human_size(sz)}）")
        r = link_one(d, target_root, log=log)
        if not r["ok"]:
            for prev in reversed(done):
                unlink_one(prev["src"], prev["dst"], log=log)
            return {"ok": False, "detail": r["detail"], "pairs": [],
                    "failed": r}
        done.append({"src": r["src"], "dst": r["dst"]})
    if not done:
        return {"ok": False, "detail": "没有完成任何目录（可能全部被占用）",
                "pairs": []}
    t.mark_linked(key, done)
    t.save()
    return {"ok": True, "detail": f"已为 {len(done)} 个目录建好联动盘",
            "pairs": done}


def unlink_one(src, dst, log=print) -> bool:
    """摘掉 junction 并把数据搬回 C 盘原位。"""
    src, dst = Path(src), Path(dst)
    if not src.exists():
        return False
    if not _is_junction(src) and not os.path.islink(src):
        return False
    try:
        os.rmdir(src)                 # rmdir 对 junction 只摘链接，不碰目标数据
    except OSError:
        subprocess.run(["cmd", "/c", "rmdir", str(src)], capture_output=True)
    if src.exists():
        return False
    if not dst.exists():
        return True                   # 目标盘数据没了，只能摘链接
    try:
        src.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(dst), str(src))
        return True
    except Exception as e:
        log(f"   ⚠️ 数据搬回失败：{e}（文件仍在 {dst}）")
        # 至少把链接恢复，保证软件能继续用
        try:
            gwizard.make_junction(src, dst)
        except Exception:
            pass
        return False


def unlink_app(key: str, log=print, t=None) -> dict:
    t = t or tracker
    rec = t.get(key)
    pairs = list(rec.get("linked") or [])
    if not pairs:
        return {"ok": False, "detail": "这个软件没有联动盘"}
    ok_n = 0
    for p in pairs:
        if unlink_one(p.get("src"), p.get("dst"), log=log):
            ok_n += 1
    if ok_n:
        t.clear_linked(key)
        t.save()
    return {"ok": ok_n == len(pairs),
            "detail": f"已还原 {ok_n}/{len(pairs)} 个目录"}


# ---------------------------------------------------------------
# 六、给引擎用的「一次体检」
# ---------------------------------------------------------------

def scan_running(first_prompt: bool = True, threshold_gb: float = THRESHOLD_GB,
                 min_mb: float = MIN_PROMPT_MB, now: float = None,
                 exes=None, t=None, baseline: bool = False,
                 budget_sec: float = 8.0) -> list:
    """扫一遍正在运行的软件，登记首次使用，返回需要提示的列表。

    这一步只做「发现 + 测算」，绝不做任何文件操作。

    baseline=True 用于「刚装好守卫的第一次扫描」：当时开着的软件全都会被
    当成「首次使用」，一个个问会像开机广告。做法是先把现状静默登记，
    只有真的已经超过阈值的（比如微信已经 5GB）才提醒一次。
    """
    t = t or tracker
    now = now or time.time()
    deadline = time.time() + max(1.0, float(budget_sec))
    exes = running_exes() if exes is None else exes
    th_bytes = int(max(0.1, float(threshold_gb)) * 2 ** 30)
    cap = int(th_bytes * 1.3)
    seen_keys = set()
    for exe in exes:
        app = app_from_exe(exe)
        if not app:
            continue
        key = app["key"]
        if key in seen_keys:
            continue
        seen_keys.add(key)
        rec = t.get(key)
        fresh = not rec
        rec = t.touch(app, now)
        if fresh and baseline:
            # 第一次运行守卫：全部静默登记，不逐个打扰。
            # 已经超过阈值的会在 pending() 里以「超过阈值」的名义提醒一次，
            # 文案是「它已经占了 5GB」，比「第一次打开」更贴合事实。
            t.mark_prompted(key, "首次使用", 0, silent=True)
        if rec.get("linked") or rec.get("decision") == "never":
            continue
        if time.time() > deadline:
            break                   # 单次体检有时间预算，剩下的下一轮继续
        need_dirs = fresh or not rec.get("dirs")
        need_size = (fresh or need_dirs
                     or now - float(rec.get("size_ts") or 0) > SIZE_TTL_SEC)
        if not need_size:
            continue
        dirs = find_data_dirs(app)
        if not dirs:
            # 数据还没落到 C 盘（或全在别的盘）：只登记，不打扰
            if fresh:
                t.set_dirs(key, [], 0)
            continue
        m = measure(dirs, cap_bytes=cap, max_seconds=3.0)
        t.set_dirs(key, dirs, m["size"])
    t.save()
    return t.pending(now, threshold_gb, first_prompt, min_mb)


def scan_installed(threshold_gb: float = THRESHOLD_GB,
                   min_mb: float = MIN_PROMPT_MB, now: float = None,
                   apps=None, t=None, budget_sec: float = 12.0) -> list:
    """体检模式：把「已装但没在跑」的软件也算上（用户手动点「立即体检」时用）。"""
    t = t or tracker
    now = now or time.time()
    deadline = time.time() + max(1.0, float(budget_sec))
    if apps is None:
        try:
            import guard_migrate
            apps = guard_migrate.list_apps(min_mb=min_mb, max_items=80)
        except Exception:
            apps = []
    cap = int(max(0.1, float(threshold_gb)) * 2 ** 30 * 1.3)
    for a in apps or []:
        path = str(a.get("path") or "")
        if not path:
            continue
        name = a.get("name") or Path(path).name
        app = {"name": _clean_app_name(name), "dir_name": Path(path).name,
               "app_dir": path, "exe": "", "key": _norm(Path(path).name)}
        if app["key"] in t.apps:
            continue
        if time.time() > deadline:
            break
        t.touch(app, now)
        dirs = find_data_dirs(app)
        if not dirs:
            continue
        m = measure(dirs, cap_bytes=cap, max_seconds=3.0)
        t.set_dirs(app["key"], dirs, m["size"])
    t.save()
    return t.pending(now, threshold_gb, True, min_mb)


def reset_cache(t=None):
    """换盘 / 用户手动刷新时清掉占用缓存与目录索引，下次重新测算。"""
    t = t or tracker
    for rec in t.apps.values():
        rec["size_ts"] = 0
    t.save()
    _INDEX["ts"] = 0.0
    _INDEX["items"] = []
