# -*- coding: utf-8 -*-
"""
C 盘守卫 (C Disk Guard) v5.0
=============================
**我们不承诺「绝对不往 C 盘写」。**

先说清楚为什么做不到，也说清楚我们到底承诺什么：

  Windows 系统本身、驱动、更新、页面文件、休眠文件、注册表、系统日志、
  杀毒软件、浏览器组件，都必须写 C 盘。要在"写入之前"拦下来，得上文件系统
  微过滤驱动：内核态代码 + EV 签名 + WHQL 认证 + 各家杀软白名单 + 长期兼容性
  测试。一旦误拦系统进程，轻则软件报错，重则蓝屏、更新装不上、开不了机。
  对一款面向普通人的工具来说，这个代价换来的是灾难性的售后成本。

  另一条路——逐文件弹窗——同样不成立：C 盘每分钟有成百上千次写入，
  弹窗会淹没每个人，最后的结果只有一个：你不看它，然后关掉它。

所以我们承诺的是另一件事，也是真正有用的那件事：

  **把不该进 C 盘的用户数据、安装包、缓存，尽量挡在 C 盘外；
    每一步都说人话，每一步都可以撤回。**

具体是四件事：
  1. 首次启动向导 —— 扫描桌面/下载/文档/图片/视频，以及微信、QQ、网盘、
     WPS、浏览器、音乐软件的缓存位置，一次性搬到别的盘：
       · 用户文件夹走系统正式的「新内容保存位置」（Known Folder 重定向）
       · 应用数据走「搬家 + 原位留目录联接」，软件完全无感
     注意：微软那个"改位置"只影响新文件，**已有文件必须单独迁移**，
     这正是向导干的活。
  2. 聚合监控 —— 不盯单个文件，盯"某个地方一共涨了多少"。
     「检测到 微信缓存 在 C 盘近期增长 8.2 GB，要搬到 D 盘吗？」
     给你【迁移 / 忽略 / 不再提醒】三个选择，一天最多问你一次。
  3. 危险删除保护 —— 只保护你自己的目录和指定的保护文件夹，不碰系统目录
     （干扰系统维护是帮倒忙）。删除发生时把数据搬进**自建隔离区**，
     清空回收站也不丢，一键还原。
  4. 空间仪表盘与周报 —— C 盘增长来源 Top10、大文件、可迁移缓存、
     可卸载软件、开机启动项。只报告，不擅自卸载、不擅自关启动项。

安全底线：
  · 搬迁一律"先复制、校验一致、再删源"，任何一步失败都不动你的数据
  · 卸载软件、关启动项这类操作，报告给的是 Windows 自己的设置入口，不代庖
  · Shift+Delete 不经过回收站，守卫拿不到东西，只能提醒，救不回来——这是实话
"""

import os
import os
import re
import sys
import json
import csv
import struct
import shutil
import time
import ctypes
import queue
import threading
import datetime
import argparse
import subprocess
from pathlib import Path
from ctypes import wintypes

import guard_paths
import guard_growth
import guard_quarantine
import guard_wizard
import guard_dashboard

# ============================================================
# 路径与配置
# ============================================================

# 数据目录由 guard_appdir 统一决定：优先 exe 同目录（便携模式），
# 装在 Program Files 等只读位置时自动回退到 %LOCALAPPDATA%；
# 并且会清理掉从别的电脑带过来的旧数据（否则会显示别人的扫描结果）。
from guard_appdir import APP_DIR
CONFIG_PATH = APP_DIR / "config.json"
LOG_DIR = APP_DIR / "guard_log"
# 当前登录用户的家目录。绝不写死成某个具体用户名——那样换台电脑
# 监控的就是一个不存在的目录，拦截永远不会触发。
C_USER = Path(os.environ.get("USERPROFILE") or str(Path.home()))

DEFAULT_CONFIG = {
    # ---- 重点目录：这里出现的新文件，不论大小都会询问 ----
    "监控目录": {
        "下载": str(C_USER / "Downloads"),
        "桌面": str(C_USER / "Desktop"),
        "文档": str(C_USER / "Documents"),
        "图片": str(C_USER / "Pictures"),
        "视频": str(C_USER / "Videos"),
    },
    # ---- 全盘写入拦截：监听整个用户目录（含 AppData 里的软件数据）----
    "全盘拦截": True,
    "全盘监控根": [str(C_USER)],
    # 这些路径下的写入不打扰（缓存/临时/系统自动生成，问也问不完）
    "排除路径关键词": [
        "\\appdata\\",
        "\\.namiwork",
        "\\.workbuddy",
        "\\$recycle.bin",
        "\\grshadercache",
        "\\shadercache",
        "\\dawncache",
        "\\crashpad",
        "\\cache",
        "\\code cache",
        "\\gpucache",
        "\\cacheddata",
        "\\blob_storage",
        "\\logs",
        "\\log\\",
        "\\node_modules\\",
    ],
    # 全盘模式下：非重点目录里，单个文件超过多少 MB 才询问（0 = 全部询问）
    "单文件询问阈值MB": 10,
    # 全盘模式下：非重点目录里，累计新增超过多少 MB 就合并询问一次
    "批量询问阈值MB": 50,
    # ---- 删除保护 ----
    "删除保护": True,
    # 删除哪些位置的提示（空 = 所有位置）
    "删除保护目录": [
        str(C_USER / "Desktop"),
        str(C_USER / "Documents"),
        str(C_USER / "Downloads"),
        str(C_USER / "Pictures"),
        str(C_USER / "Videos"),
    ],
    # 这些扩展名删除时静默（临时文件、缓存）
    "删除静默扩展名": [".tmp", ".temp", ".log", ".crdownload", ".part", ".partial", ".bak"],
    # ---- 聚合监控（v5 主推：按来源问，不按文件问）----
    "聚合提示": True,
    # 这些目录里的新文件不再逐个弹窗，改成"攒够一定量一次性问"
    "聚合监控目录": [str(C_USER / "Downloads")],
    "聚合检查间隔秒": 600,      # 多久体检查一次增长
    "聚合增长阈值MB": 500,      # 某个目录涨够这么多才打扰你
    "聚合提醒冷却秒": 14400,    # 同一个地方最长 4 小时问一次
    "聚合最小关注MB": 30,       # 低于这个体积的目录不纳入聚合监控
    "安装包阈值MB": 300,        # 下载目录里攒够这么多安装包才问
    "安装包最小MB": 30,
    # ---- 隔离区（删除保护）----
    "隔离区": True,
    "隔离区保留天数": 30,
    "隔离区上限GB": 20,
    # ---- 空间告警 ----
    "空间告警": True,
    "告警剩余GB": 10,          # 剩余低于此值时主动弹窗提醒
    "告警间隔小时": 12,        # 同一告警最短间隔
    # ---- 转移目标 ----
    "转移目标": {},
    "自动转移扩展名": [".exe", ".msi", ".msix", ".zip", ".rar", ".7z", ".iso"],
    "忽略扩展名": [".crdownload", ".part", ".tmp", ".partial", ".download"],
    "轮询间隔秒": 20,
    # ---- 安装哨兵 ----
    "监控软件安装": True,
    "安装体积阈值MB": 150,      # 单个体积达到此值的软件目录才会提醒
    "安装体检间隔秒": 10,       # 多久扫一次安装目录（只读目录名，很轻）
    "安装监控目录": [
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        str(C_USER / "AppData" / "Local" / "Programs"),
    ],
    "安装白名单": [
        "WindowsApps", "Common Files", "ModifiableWindowsApps", "Windows Defender",
        "Windows Defender Advanced Threat Protection", "Windows NT", "Windows Mail",
        "Windows Media Player", "Windows Portable Devices", "Windows Sidebar",
        "Internet Explorer", "Uninstall Information", "Reference Assemblies",
        "MSBuild", "dotnet", "NVIDIA Corporation", "Microsoft", "Windows", "temp", "Temp",
    ],
    # ---- 用户根目录新文件夹拦截（v5.1 新增）----
    # C:\用户\用户名 下出现任何新文件夹都会询问（不看点开头、不看大小）
    "根目录拦截": True,
    "根目录检查间隔秒": 30,
    # 这些名字的文件夹不问（系统固定目录 / 重点监控目录 / 同步盘）
    "根目录白名单": [
        "onedrive",
        # 中文系统固定目录
        "桌面", "文档", "下载", "图片", "视频", "音乐", "3d 对象", "保存的游戏",
        "链接", "收藏夹", "搜索", "联系人", "桌面(已固定)",
        # 英文系统固定目录
        "desktop", "documents", "downloads", "pictures", "videos", "music",
        "3d objects", "saved games", "links", "favorites", "searches", "contacts",
    ],
}

INSTALL_SKIP = {"windowsapps", "common files", "modifiablewindowsapps",
                "uninstall information", "reference assemblies", "msbuild"}

# ---- 文件类型（用于删除隐患评估）----
DOC_EXTS = {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".pdf", ".txt", ".md",
            ".csv", ".wps", ".et", ".dps", ".rtf", ".odt", ".ods", ".pages", ".numbers"}
IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic", ".raw", ".psd", ".svg"}
VID_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".m4v", ".mp3", ".wav", ".flac"}
ARC_EXTS = {".zip", ".rar", ".7z", ".tar", ".gz", ".iso"}
CODE_EXTS = {".py", ".js", ".ts", ".java", ".c", ".cpp", ".h", ".cs", ".go", ".rs",
             ".php", ".html", ".css", ".json", ".xml", ".yml", ".yaml", ".sql", ".sh"}
DB_EXTS = {".db", ".sqlite", ".sqlite3", ".mdb", ".accdb", ".bak"}
APP_EXTS = {".exe", ".dll", ".sys", ".msi", ".dat", ".pak", ".bin"}

MB_TOPMOST = 0x40000
MB_SETFOREGROUND = 0x10000
MB_ICONQUESTION = 0x20
MB_ICONINFO = 0x40
MB_ICONWARNING = 0x30
MB_ICONERROR = 0x10
MB_YESNOCANCEL = 0x3
MB_YESNO = 0x4
MB_OK = 0x0
MB_OKCANCEL = 0x1
MB_DEFBUTTON1 = 0x0
MB_DEFBUTTON2 = 0x100
MB_DEFBUTTON3 = 0x200
IDYES, IDNO, IDCANCEL, IDOK = 6, 7, 2, 1

user32 = ctypes.windll.user32
k32 = ctypes.WinDLL("kernel32", use_last_error=True)

# 打包后控制台可能是 GBK，强制 UTF-8 并容错，避免中文/emoji 崩溃
for _stream in (sys.stdout, sys.stderr):
    if _stream:
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# ============================================================
# 基础工具
# ============================================================

def alert_box(title: str, text: str, style: int, beep: int = 0x30) -> int:
    """置顶系统弹窗，返回按钮 ID。"""
    try:
        if beep:
            user32.MessageBeep(beep)
    except Exception:
        pass
    return user32.MessageBoxW(0, text, title, style | MB_TOPMOST | MB_SETFOREGROUND)


def human_size(n) -> str:
    n = float(n)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {u}" if u != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def drive_free(path: str) -> int:
    try:
        free = ctypes.c_ulonglong(0)
        ctypes.windll.kernel32.GetDiskFreeSpaceExW(str(Path(path).anchor or path + "\\"),
                                                   None, None, ctypes.byref(free))
        return free.value
    except Exception:
        return 0


def detect_drives() -> list:
    """本机所有本地硬盘（排除 C 盘），按剩余空间从大到小排序。"""
    import string
    result = []
    for letter in string.ascii_uppercase:
        if letter == "C":
            continue
        root = f"{letter}:\\"
        if not os.path.exists(root):
            continue
        if ctypes.windll.kernel32.GetDriveTypeW(root) != 3:
            continue
        result.append((letter, drive_free(root)))
    result.sort(key=lambda x: -x[1])
    return result


def ts() -> str:
    return f"[{datetime.datetime.now():%H:%M:%S}]"


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


SECURITY_PROCS = ("360tray", "360safe", "360sd", "qqpcmgr", "huorong", "hips",
                  "wsctrl", "usysdiag", "kxetray", "baidusd")


def detect_security_software() -> list:
    """检测会拦截弹窗的安全软件（360/管家/火绒/金山/百度）。"""
    try:
        out = subprocess.run(["tasklist", "/fo", "csv", "/nh"],
                             capture_output=True, timeout=10)
        txt = out.stdout.decode("gbk", "replace").lower()
        return [n for n in SECURITY_PROCS if f'"{n}.exe"' in txt]
    except Exception:
        return []


# ============================================================
# 磁盘状态 + 风险说明
# ============================================================

def disk_state() -> dict:
    total = ctypes.c_ulonglong(0)
    free = ctypes.c_ulonglong(0)
    avail = ctypes.c_ulonglong(0)
    try:
        k32.GetDiskFreeSpaceExW("C:\\", ctypes.byref(avail), ctypes.byref(total),
                                ctypes.byref(free))
    except Exception:
        pass
    t = total.value or 1
    f = avail.value
    used = max(0, t - f)
    pct = used / t * 100
    free_gb = f / 2 ** 30

    if pct >= 92 or free_gb < 5:
        level, icon = "危险", "🔴"
    elif pct >= 85 or free_gb < 10:
        level, icon = "紧张", "🟠"
    elif pct >= 70 or free_gb < 20:
        level, icon = "偏紧", "🟡"
    else:
        level, icon = "充足", "🟢"
    return {"total": t, "free": f, "used": used, "pct": pct, "level": level, "icon": icon}


def space_report(extra: int = 0) -> str:
    """生成"C 盘现状 + 继续占用会怎样"说明，供弹窗使用。"""
    st = disk_state()
    lines = [
        "━━━ C 盘空间现状 ━━━",
        f"总容量 {human_size(st['total'])}",
        f"已用 {human_size(st['used'])}（{st['pct']:.1f}%）｜ 剩余 {human_size(st['free'])}",
        f"状态：{st['icon']} {st['level']}",
        "",
        "━━━ 继续占用会怎样 ━━━",
    ]
    tips = []
    if st["free"] > 10 * 2 ** 30:
        tips.append("· 剩余低于 10 GB：Windows 更新、软件安装会失败")
    elif st["free"] > 5 * 2 ** 30:
        tips.append("· 你已经进入「低于 10 GB」区间：更新和安装可能失败")
    else:
        tips.append("· 你已经低于 10 GB：Windows 更新、软件安装基本装不上")
    if st["free"] > 5 * 2 ** 30:
        tips.append("· 剩余低于 5 GB：系统明显卡顿（内存不足要靠硬盘顶，切窗口假死）")
    else:
        tips.append("· 你已经低于 5 GB：卡顿、程序无响应会经常发生")
    tips.append("· 剩余低于 1 GB：可能蓝屏、软件崩溃、正在编辑的文档损坏")
    lines.extend(tips)

    if extra:
        after = max(0, st["free"] - extra)
        lines += ["", f"本次将占用 {human_size(extra)} → 写入后剩余约 {human_size(after)}"]
    return "\n".join(lines)


# ============================================================
# 回收站（$Recycle.Bin 解析 + 还原）
# ============================================================

class RecycleBin:
    """解析 C 盘回收站，支持识别"刚被删掉的文件"并一键还原。"""

    def __init__(self):
        self.dirs = []
        self.seen = {}
        self.start_ts = time.time()

    def scan_dirs(self):
        base = Path("C:\\$Recycle.Bin")
        dirs = []
        try:
            with os.scandir(base) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            dirs.append(Path(e.path))
                    except OSError:
                        pass
        except OSError:
            pass
        self.dirs = dirs
        return dirs

    @staticmethod
    def _filetime_to_ts(ft: int) -> float:
        try:
            if ft <= 0:
                return 0.0
            return ft / 10_000_000 - 11644473600
        except Exception:
            return 0.0

    def parse_item(self, i_path: Path):
        try:
            raw = i_path.read_bytes()
        except OSError:
            return None
        if len(raw) < 28:
            return None
        try:
            version = int.from_bytes(raw[0:8], "little")
            size = int.from_bytes(raw[8:16], "little")
            ft = int.from_bytes(raw[16:24], "little")
            if version == 2:
                nlen = int.from_bytes(raw[24:28], "little")
                orig = raw[28:28 + nlen * 2].decode("utf-16-le", "replace")
            else:   # 老格式（XP）：固定 520 字节路径
                orig = raw[24:24 + 520].decode("utf-16-le", "replace").rstrip("\x00")
        except Exception:
            return None
        orig = orig.replace("\x00", "").strip()
        if not orig:
            return None
        r_path = i_path.with_name("$R" + i_path.name[2:])
        return {
            "key": f"{i_path.parent.name}/{i_path.name}",
            "orig": orig,
            "size": size,
            "deleted_ts": self._filetime_to_ts(ft),
            "i_path": i_path,
            "r_path": r_path,
        }

    def list_items(self, d: Path):
        out = []
        try:
            with os.scandir(d) as it:
                for e in it:
                    n = e.name
                    if n.startswith("$I") and len(n) > 2:
                        item = self.parse_item(Path(e.path))
                        if item:
                            out.append(item)
        except OSError:
            pass
        return out

    def seed(self):
        """建立基线：已存在的历史删除项不提示。"""
        for d in self.scan_dirs():
            for item in self.list_items(d):
                self.seen[item["key"]] = item

    def poll_new(self):
        """返回本次新出现的删除项（只含本次运行期间被删的）。"""
        if not self.dirs:
            self.scan_dirs()
        new = []
        for d in self.dirs:
            for item in self.list_items(d):
                if item["key"] in self.seen:
                    continue
                self.seen[item["key"]] = item
                if item["deleted_ts"] < self.start_ts - 180:
                    continue        # 历史项，不打扰
                new.append(item)
        return new

    @staticmethod
    def restore(item) -> str:
        """把回收站里的文件还原回原位置，返回还原后的路径（失败返回空串）。"""
        src = item["r_path"]
        dest = Path(item["orig"])
        if not src.exists():
            return ""
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        if dest.exists():
            dest = dest.with_name(f"{dest.stem}_还原{dest.suffix}")
            i = 1
            while dest.exists():
                dest = dest.with_name(f"{Path(item['orig']).stem}_还原{i}{Path(item['orig']).suffix}")
                i += 1
        try:
            shutil.move(str(src), str(dest))
        except (OSError, shutil.Error):
            return ""
        try:
            item["i_path"].unlink()
        except OSError:
            pass
        return str(dest)


def assess_delete(path: Path, size: int, in_recycle: bool) -> tuple:
    """评估删除隐患，返回 (等级, 图标, 说明列表)。"""
    ext = path.suffix.lower()
    pl = str(path).lower()
    notes = []
    score = 0

    if "\\program files" in pl or "\\appdata\\" in pl or "\\programdata\\" in pl:
        score += 3
        notes.append("这是软件的程序/数据文件，删掉可能导致软件无法启动或功能异常")
    if ext in DOC_EXTS:
        score += 2
        notes.append("文档类文件，通常是工作资料或重要记录，删了很难重建")
    elif ext in CODE_EXTS:
        score += 2
        notes.append("代码/配置文件，删掉可能影响项目或程序运行")
    elif ext in DB_EXTS:
        score += 3
        notes.append("数据库/备份文件，删除可能造成数据永久丢失")
    elif ext in IMG_EXTS or ext in VID_EXTS:
        score += 1
        notes.append("图片/音视频，可能是原始素材，删了无法恢复画质")
    elif ext in ARC_EXTS:
        score += 1
        notes.append("压缩包，可能装着备份或安装包")
    elif ext in APP_EXTS:
        score += 1
        notes.append("程序文件，删除可能导致相关功能失效")

    if "\\desktop\\" in pl:
        score += 1
        notes.append("位于桌面，通常是你正在使用的文件")
    elif "\\documents\\" in pl:
        score += 1
        notes.append("位于「文档」目录，一般是长期保存的资料")

    if size >= 1024 ** 3:
        notes.append(f"体积较大（{human_size(size)}），删除后可释放明显空间")
    elif size < 4096:
        notes.append("体积极小，删了也省不了空间，反而可能是配置或快捷方式")

    if in_recycle:
        notes.append("已进入回收站，随时可以还原（清空回收站后才会永久丢失）")
    else:
        notes.append("未进入回收站（永久删除），无法还原")

    if score >= 5:
        return "高风险", "🔴", notes
    if score >= 3:
        return "较高", "🟠", notes
    if score >= 1:
        return "中等", "🟡", notes
    return "较低", "🟢", notes


# ============================================================
# 目录树事件监听（ReadDirectoryChangesW）
# ============================================================

FILE_LIST_DIRECTORY = 0x0001
FILE_SHARE_READ_WRITE_DELETE = 0x00000007
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_NOTIFY_CHANGE_FILE_NAME = 0x01
FILE_NOTIFY_CHANGE_DIR_NAME = 0x02
FILE_NOTIFY_CHANGE_SIZE = 0x08
FILE_NOTIFY_CHANGE_LAST_WRITE = 0x10
WATCH_FILTER = (FILE_NOTIFY_CHANGE_FILE_NAME | FILE_NOTIFY_CHANGE_DIR_NAME |
                FILE_NOTIFY_CHANGE_SIZE | FILE_NOTIFY_CHANGE_LAST_WRITE)

ACTION_ADDED = 1
ACTION_REMOVED = 2
ACTION_MODIFIED = 3
ACTION_RENAMED_OLD = 4
ACTION_RENAMED_NEW = 5

_CreateFileW = k32.CreateFileW
_CreateFileW.restype = ctypes.c_void_p
_CreateFileW.argtypes = (ctypes.c_wchar_p, wintypes.DWORD, wintypes.DWORD,
                         ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p)

_ReadDirectoryChangesW = k32.ReadDirectoryChangesW
_ReadDirectoryChangesW.restype = wintypes.BOOL
_ReadDirectoryChangesW.argtypes = (ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                                   wintypes.BOOL, wintypes.DWORD,
                                   ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
                                   ctypes.c_void_p)

_CloseHandle = k32.CloseHandle
_CloseHandle.argtypes = (ctypes.c_void_p,)
_CloseHandle.restype = wintypes.BOOL

INVALID_HANDLE = ctypes.c_void_p(-1).value


class TreeWatcher(threading.Thread):
    """在独立线程里阻塞等待目录变化事件，投递到队列。"""

    def __init__(self, root: str, out_q: queue.Queue, stop_evt: threading.Event,
                 filter_fn=None):
        super().__init__(daemon=True, name="TreeWatcher")
        self.root = root
        self.q = out_q
        self.stop_evt = stop_evt
        self.filter_fn = filter_fn
        self.ok = False
        self.dropped = 0
        self.error = ""

    def run(self):
        h = _CreateFileW(self.root, FILE_LIST_DIRECTORY, FILE_SHARE_READ_WRITE_DELETE,
                         None, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, None)
        if not h or h == INVALID_HANDLE:
            self.error = f"无法监听 {self.root}（错误码 {ctypes.get_last_error()}）"
            return
        self.ok = True
        buf = ctypes.create_string_buffer(64 * 1024)
        try:
            while not self.stop_evt.is_set():
                n = wintypes.DWORD(0)
                ok = _ReadDirectoryChangesW(h, buf, len(buf), True, WATCH_FILTER,
                                            ctypes.byref(n), None, None)
                if not ok:
                    self.error = f"监听中断（错误码 {ctypes.get_last_error()}）"
                    break
                if n.value == 0:
                    continue
                raw = buf.raw[:n.value]
                pos = 0
                while pos + 12 <= len(raw):
                    nxt, action, nlen = struct.unpack_from("<III", raw, pos)
                    name = raw[pos + 12:pos + 12 + nlen].decode("utf-16-le", "replace")
                    if name:
                        full = os.path.join(self.root, name)
                        if self.filter_fn and self.filter_fn(full):
                            continue
                        try:
                            self.q.put_nowait((time.time(), full, action))
                        except queue.Full:
                            self.dropped += 1
                    if nxt == 0:
                        break
                    pos += nxt
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
        finally:
            try:
                _CloseHandle(h)
            except Exception:
                pass


# ============================================================
# 主控
# ============================================================

class Guard:
    def __init__(self, auto_choice: str = None):
        self.auto_choice = auto_choice
        self.config = self.load_config()
        self.log_path = LOG_DIR / f"守卫记录_{datetime.date.today():%Y%m%d}.csv"
        LOG_DIR.mkdir(exist_ok=True)
        if not self.log_path.exists():
            self.log_path.write_text("时间,类型,对象,大小,决定,去向\n", encoding="utf-8-sig")

        self.known = {}                     # 重点目录基线
        self.pending = {}                   # 下载中间态
        self.pending_installs = {}          # 新安装目录待观察
        self.install_known = self.load_install_known()   # 已登记软件（持久化，重启不丢）
        self.install_dirty = False
        self.last_install_check = 0.0
        self.growth = guard_growth.GrowthMonitor(self.config)   # 聚合监控（按来源问）
        self.quar = guard_quarantine.Quarantine(
            retention_days=float(self.config.get("隔离区保留天数", 30)),
            max_gb=float(self.config.get("隔离区上限GB", 20)))
        self.agg_dirs = [str(d).lower() for d in self.config.get("聚合监控目录", [])]
        self.last_growth_check = 0.0
        self.pending_writes = {}            # path -> {size, first, last_change, stable_since}
        self.pending_removed = {}           # path -> 首次发现时间
        self.batch = {}                     # 非重点目录累计
        self.batch_since = 0.0
        self.muted_delete_exts = set()
        self.self_actions = {}              # 自己搬动过的路径 -> 时间戳，避免误判为用户删除/新写入
        self.handled = {}                   # path -> (size, 处理时间)，防止同一文件反复询问

        self.q = queue.Queue(maxsize=20000)
        self.stop_evt = threading.Event()
        self.watchers = []

        self.recycle = RecycleBin()
        self.start_ts = time.time()
        self.last_recycle = 0.0
        self.last_poll = 0.0
        self.last_space_alert = 0.0
        self.last_ask_ts = 0.0
        self.ASK_COOLDOWN = 8.0
        self.blocked_count = 0
        self.started_hint = False

        self.exclude_kw = [k.lower() for k in self.config.get("排除路径关键词", [])]
        self.ignore_exts = {e.lower() for e in self.config.get("忽略扩展名", [])}

        # ---- 用户根目录新文件夹拦截（v5.1）----
        self.root_known = self._load_root_known()   # 已登记的根目录文件夹名
        self.root_skip = {}                         # "稍后再问" 的冷却：name -> 下次提醒时间
        self.last_root_check = 0.0

    # ---------- 配置 / 日志 ----------
    def load_config(self):
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        need_write = False
        if CONFIG_PATH.exists():
            try:
                user_cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                for k, v in user_cfg.items():
                    cfg[k] = v
                if any(k not in user_cfg for k in DEFAULT_CONFIG):
                    need_write = True       # 升级后自动补全新配置项
            except Exception:
                need_write = True
        else:
            need_write = True

        # 换电脑适配：配置里若还写着上一台电脑的用户目录（C:\Users\别人\...），
        # 自动改指当前登录用户。不改的话监控的是一个不存在的目录，拦截永不触发。
        try:
            from guard_appdir import relocate_user_paths
            if relocate_user_paths(cfg).get("changed"):
                need_write = True
        except Exception:
            pass

        if need_write:
            try:
                CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
            except Exception:
                pass
        return cfg

    def save_config(self):
        try:
            CONFIG_PATH.write_text(json.dumps(self.config, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        except Exception:
            pass

    def log(self, kind, obj, size, decision, dest="-"):
        try:
            with open(self.log_path, "a", newline="", encoding="utf-8-sig") as f:
                csv.writer(f).writerow([datetime.datetime.now().strftime("%H:%M:%S"),
                                        kind, obj, human_size(size), decision, dest])
        except Exception:
            pass

    # ---------- 弹窗拦截判定 ----------
    def _suspected_block(self, tick0: int, r: int) -> bool:
        """判断弹窗是不是被安全软件的「弹窗拦截」自动关掉的。
        依据：弹窗期间完全没有键鼠输入，按钮却被"点"了 —— 那就不是人点的。
        此时按最安全的默认处理（保留原样、不擅自搬文件）。"""
        if r == IDYES:
            return False
        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        try:
            user32.GetLastInputInfo(ctypes.byref(lii))
        except Exception:
            return False
        elapsed = (lii.dwTime - tick0) & 0xFFFFFFFF
        if elapsed < 0x7FFFFFFF:        # 弹窗期间有真实键鼠操作 = 用户亲自点的
            return False
        self.blocked_count += 1
        print(f"{ts()}    ⚠️ 弹窗期间没有任何键鼠操作却被关闭 —— 判定为被安全软件拦截，已按安全默认处理")
        if self.blocked_count == 1:
            print("      👉 请打开 360/电脑管家 →「弹窗拦截 / 弹窗过滤」→ 把 C盘守卫.exe 加入信任，")
            print("         否则你看不到询问框（软件仍运行，但只能做保守处理）")
            self.log("提示", "弹窗疑似被拦截", 0, "请把 C盘守卫.exe 加入安全软件信任")
        return True

    # ---------- 过滤 ----------
    DOT_DIR_RE = re.compile(r"\\\.[^\\/]+(\\|$)")

    def _self_action(self, path: str) -> bool:
        """是不是自己刚刚搬动/还原的路径（2 分钟内有效）。"""
        t = self.self_actions.get(path)
        if t is None:
            return False
        if time.time() - t > 120:
            self.self_actions.pop(path, None)
            return False
        return True

    def is_excluded(self, path: str) -> bool:
        pl = path.lower()
        for kw in self.exclude_kw:
            if kw in pl:
                return True
        if self.DOT_DIR_RE.search(pl):        # .namiwork / .cache / .config 等工具运行时目录
            return True
        return self._self_action(path)

    def _in_agg_dirs(self, path: Path) -> bool:
        pl = str(path).lower()
        return any(pl.startswith(d) for d in self.agg_dirs)

    def in_focus_dirs(self, path: Path) -> bool:
        pl = str(path).lower()
        for root in self.config.get("监控目录", {}).values():
            if root and pl.startswith(str(root).lower()):
                return True
        return False

    # ---------- 用户根目录新文件夹拦截（v5.1）----------
    ROOT_KNOWN_PATH = APP_DIR / "root_known.json"

    def _load_root_known(self) -> set:
        """读取已登记的根目录文件夹名；首次运行把现有文件夹全部登记（不追溯旧文件夹）。"""
        p = self.ROOT_KNOWN_PATH
        if p.exists():
            try:
                return set(json.loads(p.read_text(encoding="utf-8")))
            except Exception:
                pass
        names = set()
        try:
            for ch in C_USER.iterdir():
                if ch.is_dir():
                    names.add(ch.name)
        except OSError:
            pass
        self._save_root_known(names)
        return names

    def _save_root_known(self, names: set):
        try:
            self.ROOT_KNOWN_PATH.write_text(
                json.dumps(sorted(names), ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    @staticmethod
    def _is_reparse(p: Path) -> bool:
        """是否已经是符号链接/junction（之前搬移过，不再问）。"""
        try:
            os.readlink(str(p))
            return True
        except (OSError, ValueError):
            return False

    def check_root_dirs(self, now: float):
        r"""定期扫描 C:\Users\用户名 根目录，发现新文件夹就询问。"""
        if not self.config.get("根目录拦截", True):
            return
        if now - self.last_root_check < float(self.config.get("根目录检查间隔秒", 30)):
            return
        self.last_root_check = now
        try:
            entries = [ch for ch in C_USER.iterdir() if ch.is_dir()]
        except OSError:
            return
        wl = [w.lower() for w in self.config.get("根目录白名单", [])]
        dirty = False
        for ch in entries:
            name = ch.name
            if name in self.root_known:
                continue
            low = name.lower()
            if any(w == low or (len(w) > 4 and w in low) for w in wl):
                self.root_known.add(name)
                dirty = True
                continue
            if self._is_reparse(ch):
                self.root_known.add(name)
                dirty = True
                continue
            if now < self.root_skip.get(name, 0):
                continue
            # 先登记再处理，避免弹窗期间反复触发
            self.root_known.add(name)
            dirty = True
            self._handle_root_folder(ch)
        if dirty:
            self._save_root_known(self.root_known)

    def _handle_root_folder(self, p: Path):
        try:
            size = Guard.dir_size(p)[0]
        except Exception:
            size = 0
        decision = self.ask_root_folder(p, size)

        if decision == "skip":
            self.root_known.discard(p.name)          # 保持未知，冷却结束后再问
            self.root_skip[p.name] = time.time() + 3600
            self.log("根目录", p.name, size, "稍后再问(1小时)")
            return
        if decision == "keep":
            self.log("根目录", p.name, size, "同意保留")
            return
        # decision = 目标盘根路径 → 搬移 + 创建链接
        dest = self.move_dir_junction(p, decision)
        if dest:
            self.log("根目录", p.name, size, "搬移并创建链接", dest)
            alert_box("C 盘守卫 — 搬移完成",
                      f"文件夹 {p.name} 已搬到：\n{dest}\n\n"
                      "原位置已创建「链接」，相关软件照常可用，C 盘空间已释放。",
                      MB_ICONINFO)
        else:
            self.log("根目录", p.name, size, "搬移失败-保留原地")
            alert_box("C 盘守卫 — 搬移失败",
                      f"{p.name} 搬移失败（可能正被软件占用）。\n"
                      "已保留在 C 盘原位置，软件不受影响。可稍后在「今日新增」页重试。",
                      MB_ICONWARNING)

    def ask_root_folder(self, p: Path, size: int) -> str:
        """返回 'keep' | 'skip' | 目标盘根路径。GUI 版会覆写此方法。"""
        if self.auto_choice:
            return "keep"
        if not self.target_roots():
            return "keep"
        body = (f"你的用户目录里出现一个新文件夹：\n\n"
                f"📁 文件夹：{p.name}\n"
                f"📦 已占用：{human_size(size)}\n"
                f"📍 位置：{p.parent}\n\n"
                "【是】保留在 C 盘（以后不再问这个文件夹）\n"
                "【否】搬到其他盘，并在原位置创建链接（软件照常可用）\n"
                "【取消】暂时不动，1 小时后再提醒")
        r = alert_box("C 盘守卫 — 用户目录出现新文件夹", body,
                      MB_YESNOCANCEL | MB_ICONQUESTION | MB_DEFBUTTON2)
        if r == IDYES:
            return "keep"
        if r == IDNO:
            root = self.pick_drive("C 盘守卫 — 选择搬移位置",
                                   f"把文件夹 {p.name} 搬到哪个盘？")
            return root if root else "skip"
        return "skip"

    def move_dir_junction(self, src: Path, dest_root: str) -> str:
        """把整个文件夹搬到其他盘，在原位置创建 junction 链接；失败则回滚。"""
        dest_root_p = Path(dest_root)
        dest = dest_root_p / src.name
        i = 1
        while dest.exists():
            dest = dest_root_p / f"{src.name}({i})"
            i += 1
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
        except (PermissionError, OSError, shutil.Error):
            return ""
        try:
            r = subprocess.run(["cmd", "/c", "mklink", "/J", str(src), str(dest)],
                               capture_output=True, creationflags=0x08000000)
            if r.returncode != 0 or not src.exists():
                raise OSError("junction 创建失败")
        except Exception:
            try:                                   # 回滚：把文件夹搬回原位
                shutil.move(str(dest), str(src))
            except Exception:
                pass
            return ""
        self.self_actions[str(src)] = time.time()
        return str(dest)

    # ---------- 目标盘 ----------
    def target_roots(self) -> list:
        out = []
        for name, p in self.config.get("转移目标", {}).items():
            if p and len(p) >= 2 and p[1] == ":":
                out.append((f"{name}（{p}，剩余 {human_size(drive_free(p))}）", p))
        for letter, free in detect_drives():
            out.append((f"{letter} 盘（剩余 {human_size(free)}）", f"{letter}:\\C盘守卫接收"))
        return out

    def move_file(self, src: Path, dest_root: str) -> str:
        """先复制、成功后再删源；失败则原文件留在原地。"""
        dest_root_p = Path(dest_root)
        rel = src.parent.name if src.parent.name else "文件"
        dest_dir = dest_root_p / rel
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return ""
        dest = dest_dir / src.name
        i = 1
        while dest.exists():
            dest = dest_dir / f"{src.stem}({i}){src.suffix}"
            i += 1
        try:
            shutil.copy2(src, dest)
            self.self_actions[str(src)] = time.time()
            os.remove(src)
            return str(dest)
        except (PermissionError, OSError):
            try:
                if dest.exists():
                    os.remove(dest)
            except OSError:
                pass
            return ""

    def pick_drive(self, title: str, hint: str):
        roots = self.target_roots()
        if not roots:
            alert_box("C 盘守卫", "未检测到 C 盘以外的本地硬盘。", MB_ICONINFO)
            return None
        for i, (name, root) in enumerate(roots):
            body = (f"{hint}\n\n是 = 选 {name}\n否 = 看下一个盘\n取消 = 放弃" if i == 0
                    else f"是 = 选 {name}\n否 = 看下一个盘\n取消 = 放弃")
            r = alert_box(title, body, MB_YESNOCANCEL | MB_ICONQUESTION)
            if r == IDYES:
                return root
            if r == IDCANCEL:
                return None
        return None

    # ========================================================
    # 一、写入拦截
    # ========================================================
    def ask_write(self, items: list) -> str:
        """items: [(Path, size)]；返回 'keep' | 'mute' | 目标根路径。"""
        total = sum(s for _, s in items)
        roots = self.target_roots()

        if self.auto_choice:
            if self.auto_choice.lower() == "keep":
                return "keep"
            for _, root in roots:
                if root[:1].upper() == self.auto_choice[:1].upper():
                    return root
            return "keep"

        if not roots:
            alert_box("C 盘守卫",
                      f"检测到 {len(items)} 个新文件写入 C 盘，但本机没有其他本地硬盘，\n"
                      f"文件只能保留在 C 盘。\n\n" + space_report(total),
                      MB_ICONWARNING)
            return "keep"

        rec_name, rec_root = roots[0]
        if len(items) == 1:
            path, size = items[0]
            head = (f"有 1 个新文件要存进 C 盘，需要你同意：\n\n"
                    f"📄 文件：{path.name}\n"
                    f"💾 大小：{human_size(size)}\n"
                    f"📁 位置：{path.parent}\n")
        else:
            head = f"有 {len(items)} 个新文件要存进 C 盘，合计 {human_size(total)}：\n\n"
            for p, s in items[:6]:
                head += f"  · {p.name}（{human_size(s)}）\n"
            if len(items) > 6:
                head += f"  · …… 等共 {len(items)} 个\n"

        body = (head + "\n" + space_report(total) + "\n\n"
                "━━━ 请选择 ━━━\n"
                f"【是】同意，保留在 C 盘\n"
                f"【否】不占 C 盘，转移到 {rec_name}\n"
                "【取消】我再挑别的盘 / 以后不再问这个文件")

        tick0 = ctypes.windll.kernel32.GetTickCount()
        r = alert_box("C 盘守卫 — 有新文件要写入 C 盘", body,
                      MB_YESNOCANCEL | MB_ICONQUESTION | MB_DEFBUTTON2)
        if self._suspected_block(tick0, r):
            return "keep"
        if r == IDYES:
            return "keep"
        if r == IDNO:
            return rec_root
        if r == IDCANCEL:
            return self._ask_browse_or_mute(items)
        return "keep"

    def _ask_browse_or_mute(self, items: list) -> str:
        roots = self.target_roots()
        body = ("要转移到哪个盘？\n\n是 = 看下一个可选盘\n否 = 全部永久同意（以后不再提示这些文件）\n取消 = 这次算了，保留在 C 盘")
        for name, root in roots[1:]:
            r = alert_box("C 盘守卫 — 选择目标盘", f"{body}\n\n当前：{name}",
                          MB_YESNOCANCEL | MB_ICONQUESTION)
            if r == IDYES:
                return root
            if r == IDNO:
                return "mute"
            if r == IDCANCEL:
                return "keep"
        return "keep"

    def handle_write(self, items: list):
        """一批新文件的处理：询问 → 保留/转移。"""
        known_items = []
        for p, s in items:
            try:
                if p.exists():
                    known_items.append((p, p.stat().st_size))
            except OSError:
                continue
        if not known_items:
            return
        total = sum(s for _, s in known_items)
        print(f"{ts()} 📥 新文件 {len(known_items)} 个（{human_size(total)}）"
              f"：{known_items[0][0].parent}")

        decision = self.ask_write(known_items)
        if decision == "mute":
            for p, _ in known_items:
                self.log("写入", p.name, 0, "永久同意(不再提示)", str(p))
            print(f"{ts()}    ✅ 已永久同意")
            return

        if decision == "keep":
            for p, s in known_items:
                self.log("写入", p.name, s, "同意保留")
            print(f"{ts()}    ✅ 同意保留在 C 盘")
            return

        moved = 0
        for p, s in known_items:
            dest = self.move_file(p, decision)
            if dest:
                moved += 1
                self.log("写入", p.name, s, f"转移到{Path(decision).anchor}", dest)
            else:
                self.log("写入", p.name, s, "转移失败-保留原地")
        if moved:
            print(f"{ts()}    📦 已转移 {moved} 个文件到 {Path(decision).anchor}")
            if not self.auto_choice:
                alert_box("C 盘守卫 — 转移完成",
                          f"{moved} 个文件已移到 {Path(decision).anchor}，C 盘不再占用。\n\n"
                          f"位置：{Path(decision)}\\{known_items[0][0].parent.name}",
                          MB_ICONINFO, beep=0x40)

    # ========================================================
    # 二、删除保护
    # ========================================================
    def _delete_protected(self, path: Path) -> bool:
        """只对 C 盘、且在「删除保护目录」里的文件问；其余只记日志不打扰。"""
        try:
            drive = os.path.splitdrive(str(path))[0].lower()
        except Exception:
            drive = ""
        if drive and drive != "c:":
            return False
        dirs = [str(x).lower() for x in self.config.get("删除保护目录", []) if str(x).strip()]
        if not dirs:
            return True
        pl = str(path).lower()
        return any(pl.startswith(d.rstrip("\\")) for d in dirs)

    # ========================================================
    # 五、聚合监控（v5）：按「来源」问，不按「文件」问
    # ========================================================
    def check_growth(self):
        """某个来源涨够了就问一次；一天最多问一回，绝不刷屏。"""
        if not self.config.get("聚合提示", True) or self.auto_choice:
            return
        if not self.growth.state["apps"]:
            if not getattr(self, "_growth_synced", False):
                try:
                    self.growth.sync_targets()
                    self._growth_synced = True
                except Exception as e:
                    print(f"{ts()} ⚠️ 聚合目标初始化失败: {type(e).__name__}: {e}")
        try:
            alerts = self.growth.check()
        except Exception as e:
            print(f"{ts()} ⚠️ 聚合检查异常(已忽略): {type(e).__name__}: {e}")
            return
        for al in alerts:
            try:
                self.handle_growth(al)
            except Exception as e:
                print(f"{ts()} ⚠️ 聚合处理异常: {type(e).__name__}: {e}")

    def handle_growth(self, al: dict):
        label = al["label"]
        delta, cur = al["delta"], al["cur"]
        when = guard_growth.age_text(al.get("days", 1.0))
        st = disk_state()

        if al["kind"] == "installers":
            files = al.get("files", [])
            detail = (f"   下载目录里攒了 {len(files)} 个文件（安装包/压缩包之类），"
                      f"共 {human_size(cur)}")
            action = "搬到别的盘，原位留快捷方式"
        else:
            detail = f"   这个目录现在一共 {human_size(cur)}"
            action = "搬到别的盘，原位留目录联接（软件照常用，随时可撤回）"

        body = [
            f"「{label}」在 C 盘 {when}增长了 {human_size(delta)}",
            detail,
            "",
            "━━━ C 盘现状 ━━━",
            f"已用 {human_size(st['used'])} / {human_size(st['total'])}"
            f"（{st['pct']:.1f}%）｜剩余 {human_size(st['free'])}  {st['icon']} {st['level']}",
            "",
            "━━━ 怎么处理 ━━━",
            f"【是】{action}",
            "【否】先别管（把新体积累起来，再涨才提醒你）",
            "【取消】这个位置以后别再问了",
        ]
        tick0 = ctypes.windll.kernel32.GetTickCount()
        r = alert_box("C 盘守卫 — 有个地方长得有点快",
                      "\n".join(body),
                      MB_YESNOCANCEL | MB_ICONQUESTION | MB_DEFBUTTON2)
        if self._suspected_block(tick0, r):
            self.log("聚合", label, cur, "弹窗疑似被拦截-已忽略本次")
            self.growth.settle(al["key"])
            return

        if r == IDYES:
            drive = self.pick_drive("C 盘守卫 — 选择目标盘",
                                    f"把「{label}」（{human_size(cur)}）搬到哪个盘？")
            if not drive:
                self.growth.settle(al["key"])
                return
            base = Path(drive)
            if al["kind"] == "installers":
                res = guard_growth.relocate_files(al["files"], base / "已迁移" / "安装包")
                ok = res["moved"] > 0
                msg = (f"搬走 {res['moved']} 个文件，释放 {human_size(res['freed'])}\n"
                       f"原位留下的快捷方式仍指向新位置。")
                if res["failed"]:
                    msg += f"\n{len(res['failed'])} 个搬运失败（多半是被占用），原文件没动。"
                self.log("聚合", label, cur, "已搬安装包", str(base))
            else:
                dst = base / "已迁移" / guard_wizard.safe_name(label)
                self.self_actions[str(al["path"])] = time.time()
                res = guard_wizard.migrate_app_dir(al["path"], dst)
                ok = res["ok"]
                msg = (f"「{label}」已搬到：\n{dst}\n\n"
                       f"C 盘原位留了目录联接，软件还按老路径读写，完全无感。\n"
                       f"以后想撤回：C盘守卫.exe --undo"
                       if ok else f"搬家失败：{res['stage']} — {res['detail']}")
                self.log("聚合", label, cur, "已搬家" if ok else "搬家失败", str(dst))
            alert_box("C 盘守卫 — 搬家完成" if ok else "C 盘守卫 — 搬家未完成",
                      msg, MB_ICONINFO if ok else MB_ICONWARNING)
            guard_wizard.save_migrations(guard_wizard.load_migrations() + [{
                "时间": datetime.datetime.now().isoformat(timespec="seconds"),
                "名称": label, "key": al["key"], "类型": "聚合搬家",
                "原位置": str(al["path"]) if al["path"] else "下载目录安装包",
                "新位置": str(base), "大小": cur,
                "结果": "完成" if ok else "失败", "说明": "用户从聚合提示选择搬家",
            }])
        elif r == IDCANCEL:
            self.growth.mute(al["key"])
            self.log("聚合", label, cur, "用户选择不再提醒")
            print(f"{ts()}   🔇 以后不再提醒：{label}")
        else:
            self.growth.settle(al["key"])
            self.log("聚合", label, cur, "用户选择先不管")

    def handle_deletes(self, items: list):
        """检测到删除 → 告知 + 隐患评估 + 可还原。"""
        quiet = [it for it in items if not self._delete_protected(Path(it["orig"]))]
        if quiet:
            for it in quiet:
                self.log("删除", Path(it["orig"]).name, it["size"], "非保护位置-仅记录")
            items = [it for it in items if self._delete_protected(Path(it["orig"]))]
            if not items:
                return
        print(f"{ts()} 🗑️ 检测到删除 {len(items)} 项: {Path(items[0]['orig']).name}"
              f"（{human_size(items[0]['size'])}）")
        if self.auto_choice:
            for it in items:
                print(f"{ts()} 🗑️ 检测到删除: {it['orig']}（{human_size(it['size'])}）")
                self.log("删除", Path(it["orig"]).name, it["size"], "检测到删除(自动模式仅记录)")
            return
        self.last_ask_ts = time.time()

        # 先落袋：把回收站里的数据搬进隔离区。
        # 必须在弹窗之前做完 —— 否则你中途顺手清空回收站就救不回来了。
        moved_q = 0
        if self.config.get("隔离区", True):
            for it in items:
                if self.quar.quarantine(it):
                    moved_q += 1
        if moved_q:
            print(f"{ts()}    📦 已移入隔离区 {moved_q} 项（清空回收站也不会丢）")
            self.quar.auto_purge()

        for it in items[:3]:
            orig = Path(it["orig"])
            size = it["size"]
            level, icon, notes = assess_delete(orig, size, in_recycle=True)
            keep_days = int(self.config.get("隔离区保留天数", 30))
            qid = ""
            for e in self.quar.list():
                if Path(e["orig"]) == orig:
                    qid = e["id"]
                    break
            body = [
                "刚刚检测到 C 盘有文件被删除：",
                "",
                f"📄 文件：{orig.name}",
                f"💾 大小：{human_size(size)}",
                f"📁 原位置：{orig.parent}",
                f"🕐 时间：{datetime.datetime.now():%H:%M:%S}",
                "",
                f"━━━ 删除隐患评估：{icon} {level} ━━━",
            ]
            body += ["· " + n for n in notes[:5]]
            if qid:
                body += [
                    "",
                    "━━━ 我替你做了什么 ━━━",
                    "· 数据已从回收站搬进「守卫隔离区」",
                    "· 你清空回收站也不会把它删掉",
                    f"· 最多帮你留 {keep_days} 天，届时自动清理；想提前处理可跑 --quarantine",
                ]
            else:
                body += [
                    "",
                    "⚠️ 这次没能抢出来：数据没经过回收站（Shift+Delete / 命令行删除），",
                    "   守卫拿不到副本，救不回来。以后尽量用普通删除。",
                ]
            body += [
                "",
                "━━━ 请确认 ━━━",
                "【是】误删了 —— 立刻还原到原来的位置",
                "【否】先放在隔离区，回头再说（默认，最稳妥）",
                "【取消】彻底删除（会再问你一次，删了找不回）",
            ]
            tick0 = ctypes.windll.kernel32.GetTickCount()
            r = alert_box("C 盘守卫 — 检测到文件被删除（删除保护）",
                          "\n".join(body),
                          MB_YESNOCANCEL | MB_ICONWARNING | MB_DEFBUTTON2)
            if self._suspected_block(tick0, r):
                self.log("删除", orig.name, size, "弹窗疑似被拦截-已留在隔离区")
                continue

            if r == IDYES:
                if not qid:
                    alert_box("C 盘守卫 — 还原失败",
                              "这份数据当时没进回收站（可能是 Shift+Delete），\n"
                              "守卫没有副本，无法还原。",
                              MB_ICONERROR)
                    continue
                self.self_actions[str(orig)] = time.time()   # 先登记，避免还原动作被当成新写入
                dest = self.quar.restore(qid)
                if dest:
                    self.self_actions[str(dest)] = time.time()
                    self.log("删除", orig.name, size, "已还原", dest)
                    print(f"{ts()}    ↩️ 已还原: {dest}")
                    alert_box("C 盘守卫 — 已还原",
                              f"文件已还原到：\n{dest}", MB_ICONINFO, beep=0x40)
                else:
                    self.log("删除", orig.name, size, "还原失败")
                    alert_box("C 盘守卫 — 还原失败",
                              "隔离区里找不到这份数据了，无法还原。", MB_ICONERROR)
            elif r == IDCANCEL:
                if not qid:
                    continue
                c = alert_box("C 盘守卫 — 确认彻底删除",
                              f"确定要把「{orig.name}」彻底删掉吗？\n\n"
                              "这是不可恢复的操作，删完就没了。",
                              MB_OKCANCEL | MB_ICONWARNING | MB_DEFBUTTON2)
                if c == 1 or c == IDOK:
                    self.quar.purge(qid)
                    self.log("删除", orig.name, size, "已彻底删除(用户确认)")
                else:
                    self.log("删除", orig.name, size, "取消-留在隔离区")
            else:
                self.log("删除", orig.name, size, "留在隔离区(待定)")

    # ========================================================
    # 三、空间告警
    # ========================================================
    def maybe_space_alert(self):
        if not self.config.get("空间告警", True) or self.auto_choice:
            return
        st = disk_state()
        if st["free"] / 2 ** 30 > float(self.config.get("告警剩余GB", 10)):
            return
        gap = float(self.config.get("告警间隔小时", 12)) * 3600
        if time.time() - self.last_space_alert < gap:
            return
        self.last_space_alert = time.time()
        alert_box("C 盘守卫 — C 盘空间不足提醒",
                  "━━━ 空间告警 ━━━\n"
                  f"C 盘只剩 {human_size(st['free'])}（已用 {st['pct']:.1f}%），{st['icon']} {st['level']}\n\n"
                  "━━━ 继续下去会怎样 ━━━\n"
                  "· Windows 更新、软件安装会失败\n"
                  "· 系统会明显卡顿：内存不够时靠硬盘顶，切窗口假死、程序无响应\n"
                  "· 剩不到 1 GB 时可能蓝屏、软件崩溃，正在编辑的文档会损坏\n\n"
                  "━━━ 建议 ━━━\n"
                  "1. 把桌面/下载/文档里的大文件挪到 D/E/F 盘\n"
                  "2. 清空回收站（先确认没有误删的重要文件）\n"
                  "3. 用我的监测报告看哪些缓存占地方\n\n"
                  "（此提醒每 " + str(self.config.get("告警间隔小时", 12)) + " 小时最多一次）",
                  MB_ICONWARNING)
        self.log("告警", "C盘剩余空间", st["free"], "已弹窗提醒")

    # ========================================================
    # 四、安装哨兵
    # ========================================================
    # ---- 已登记软件清单：落盘持久化，守卫重启也不会把老软件当"新装的" ----
    KNOWN_FILE = "install_known.json"

    def load_install_known(self) -> dict:
        try:
            d = json.loads((APP_DIR / self.KNOWN_FILE).read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def save_install_known(self):
        if not self.install_dirty:
            return
        try:
            (APP_DIR / self.KNOWN_FILE).write_text(
                json.dumps(self.install_known, ensure_ascii=False, indent=1, sort_keys=True),
                encoding="utf-8")
            self.install_dirty = False
        except Exception:
            pass

    def list_install_dirs(self) -> dict:
        """安装目录的一级子目录：name_lower -> (path, mtime)。只 scandir，不递归，很轻。"""
        out = {}
        wl = [str(w).lower() for w in self.config.get("安装白名单", [])]
        for d in self.config.get("安装监控目录", []):
            root = Path(d)
            if not root.exists():
                continue
            try:
                with os.scandir(root) as it:
                    for e in it:
                        try:
                            if not e.is_dir(follow_symlinks=False):
                                continue
                            k = e.name.lower()
                            # 白名单支持包含匹配：让 "Microsoft" 挡住
                            # "Microsoft Update Health Tools" 这类系统组件
                            if (k in INSTALL_SKIP or k in wl
                                    or any(len(w) >= 5 and w in k for w in wl)):
                                continue
                            out[k] = (e.path, e.stat(follow_symlinks=False).st_mtime)
                        except OSError:
                            pass
            except OSError:
                pass
        return out

    def snapshot_installs(self) -> dict:
        return {k: (p, mt) for k, (p, mt) in self.list_install_dirs().items()}

    @staticmethod
    def create_junction(link: Path, target: Path) -> bool:
        ps = ('$ErrorActionPreference="Stop"; '
              f'New-Item -ItemType Junction -Path "{link}" -Target "{target}" | Out-Null')
        try:
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True)
            return Path(link).exists()
        except Exception:
            return False

    @staticmethod
    def dir_size(path: Path):
        total, count, has_exe = 0, 0, False
        for cur, dirs, files in os.walk(path):
            for fn in files:
                count += 1
                if count > 30000:
                    return total, has_exe
                try:
                    total += os.path.getsize(os.path.join(cur, fn))
                except OSError:
                    pass
                if not has_exe and fn.lower().endswith(".exe"):
                    has_exe = True
        return total, has_exe

    def handle_install(self, name: str, path: Path):
        total, has_exe = self.dir_size(path)
        if self.auto_choice:
            print(f"{ts()} 🆕 新软件目录: {path.name}（{human_size(total)}）自动模式仅记录")
            self.log("安装", path.name, total, "自动模式仅记录")
            return
        # 体积没到阈值（默认 150MB）：当作系统小组件/零星目录，静默登记不弹窗
        threshold_mb = float(self.config.get("安装体积阈值MB", 150))
        if total < threshold_mb * 2 ** 20:
            self.install_known[name] = {"path": str(path), "size": total,
                                        "ts": time.time()}
            self.install_dirty = True
            self.log("安装", path.name, total, f"小于{threshold_mb:g}MB-静默登记")
            return

        body = (f"有软件装到了 C 盘：\n\n"
                f"📦 软件目录：{path.name}\n"
                f"💾 占用空间：{human_size(total)}\n"
                f"📁 安装位置：{path.parent}\n\n"
                + space_report(total) + "\n\n"
                "━━━ 请选择 ━━━\n"
                "【是】搬到其他盘（C 盘原位留一个门牌链接，软件照常能启动）\n"
                "【否】只提醒，我自己处理（卸载后重装到别的盘）\n"
                "【取消】这个软件以后不再提示")
        tick0 = ctypes.windll.kernel32.GetTickCount()
        r = alert_box("C 盘守卫 — 发现软件装进了 C 盘", body,
                      MB_YESNOCANCEL | MB_ICONQUESTION | MB_DEFBUTTON2)
        if self._suspected_block(tick0, r):
            self.log("安装", path.name, total, "弹窗疑似被拦截-未处理")
            self.install_known[name] = {"path": str(path), "size": total, "ts": time.time()}
            self.install_dirty = True
            return

        if r == IDCANCEL:
            wl = self.config.setdefault("安装白名单", [])
            if path.name not in wl:
                wl.append(path.name)
                self.save_config()
            self.log("安装", path.name, total, "加入白名单")
            self.install_known[name] = {"path": str(path), "size": total, "ts": time.time()}
            self.install_dirty = True
            return
        if r == IDNO:
            self.log("安装", path.name, total, "仅提醒")
            self.install_known[name] = {"path": str(path), "size": total,
                                        "ts": time.time()}   # 登记后不再反复提醒
            self.install_dirty = True
            return

        sure = alert_box("C 盘守卫 — 确认搬家",
                         f"要把「{path.name}」（{human_size(total)}）搬到别的盘吗？\n\n"
                         f"搬家期间请先完全退出这个软件，否则会失败。\n"
                         f"C 盘原位置会留一个目录联接，快捷方式照常能用。",
                         MB_OKCANCEL | MB_ICONQUESTION | MB_DEFBUTTON2)
        if sure != IDOK:
            self.log("安装", path.name, total, "放弃搬家")
            self.install_known[name] = {"path": str(path), "size": total, "ts": time.time()}
            self.install_dirty = True
            return

        drive = self.pick_drive("C 盘守卫 — 选择迁移目标盘", f"把「{path.name}」搬到哪个盘？")
        if not drive:
            self.log("安装", path.name, total, "放弃迁移")
            self.install_known[name] = {"path": str(path), "size": total,
                                        "ts": time.time()}
            self.install_dirty = True
            return

        dest = Path(drive) / "已迁移软件" / path.name
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        i = 1
        while dest.exists():
            dest = Path(drive) / "已迁移软件" / f"{path.name}({i})"
            i += 1
        try:
            self.self_actions[str(path)] = time.time()
            shutil.move(str(path), str(dest))
        except (PermissionError, OSError) as e:
            alert_box("C 盘守卫 — 迁移失败",
                      f"文件被占用，搬不动。\n\n请先完全退出「{path.name}」"
                      f"（包括右下角托盘图标）再试一次。\n\n原因：{type(e).__name__}",
                      MB_ICONWARNING)
            self.log("安装", path.name, total, "迁移失败(被占用)-已登记不再提示")
            self.install_known[name] = {"path": str(path), "size": total,
                                        "ts": time.time()}
            self.install_dirty = True
            return

        if self.create_junction(path, dest):
            self.log("安装", path.name, total, "已迁移并建联接", str(dest))
            alert_box("C 盘守卫 — 迁移完成",
                      f"「{path.name}」已搬到：\n{dest}\n\n"
                      f"C 盘原位置留了一个目录联接，软件照常启动，"
                      f"但实际数据不再占 C 盘空间。",
                      MB_ICONINFO, beep=0x40)
        else:
            self.log("安装", path.name, total, "已迁移(联接失败)", str(dest))
            alert_box("C 盘守卫 — 部分完成",
                      f"文件已搬到：\n{dest}\n\n但 C 盘原位置的联接没建成，快捷方式可能失效。\n"
                      f"建议卸载后重新装到其他盘。", MB_ICONWARNING)

    def check_installs(self, now):
        if not self.config.get("监控软件安装", True):
            return
        if now - self.last_install_check < float(self.config.get("安装体检间隔秒", 10)):
            return
        self.last_install_check = now
        cur = self.list_install_dirs()
        first_run = not self.install_known        # 首次运行（无清单文件）

        for k, (pdir, mt) in cur.items():
            if k in self.install_known:
                continue
            if first_run:
                # 第一次运行：已存在的软件记为"存量"，只登记不打扰，避免开机弹一堆窗
                self.install_known[k] = {"path": pdir, "size": 0, "ts": now}
                self.install_dirty = True
                continue
            first = self.pending_installs.setdefault(k, now)
            if now - first < 15:                  # 等安装目录长出内容，别问到半成品
                continue
            self.handle_install(k, Path(pdir))
            self.pending_installs.pop(k, None)

        for k in list(self.pending_installs):
            if k not in cur:
                self.pending_installs.pop(k, None)

        self.save_install_known()

    # ========================================================
    # 五、事件处理
    # ========================================================
    def start_watchers(self):
        roots = []
        if self.config.get("全盘拦截", True):
            roots = [r for r in self.config.get("全盘监控根", []) if os.path.isdir(r)]
        for r in roots:
            w = TreeWatcher(r, self.q, self.stop_evt, filter_fn=self.is_excluded)
            w.start()
            self.watchers.append(w)
        return roots

    def consume_events(self, now):
        drained = 0
        while drained < 800:
            try:
                evt_time, path, action = self.q.get_nowait()
            except queue.Empty:
                break
            drained += 1
            if self.is_excluded(path):
                continue
            p = Path(path)
            ext = p.suffix.lower()

            if action in (ACTION_ADDED, ACTION_MODIFIED, ACTION_RENAMED_NEW):
                if ext in self.ignore_exts:
                    self.pending[path] = now
                    continue
                rec = self.pending_writes.get(path)
                if rec is None:
                    self.pending_writes[path] = {"first": now, "size": -1, "stable": 0.0}
                else:
                    rec["stable"] = 0.0
            elif action in (ACTION_REMOVED, ACTION_RENAMED_OLD):
                if ext in self.ignore_exts:
                    continue
                # 只对"删除保护目录"内的删除做后续判断，避免 AppData 噪音占内存
                pl = path.lower()
                dirs = self.config.get("删除保护目录", [])
                if dirs and not any(pl.startswith(str(d).lower()) for d in dirs):
                    continue
                self.pending_removed.setdefault(path, evt_time)

    def flush_writes(self, now):
        """把稳定的新文件按"重点目录立即问 / 其他目录累计问"处理。"""
        focus_ready, other_ready = [], []
        for path, rec in list(self.pending_writes.items()):
            p = Path(path)
            try:
                if not p.exists() or p.is_dir():
                    self.pending_writes.pop(path, None)
                    continue
                size = p.stat().st_size
            except OSError:
                self.pending_writes.pop(path, None)
                continue
            if size != rec["size"]:
                rec["size"] = size
                rec["stable"] = 0.0
                continue
            if rec["stable"] == 0.0:
                rec["stable"] = now
                continue
            if now - rec["stable"] < 1.5:      # 1.5 秒内大小没变 = 写完了
                continue
            self.pending_writes.pop(path, None)
            if self._is_handled(p, size):
                continue
            if self._in_agg_dirs(p):
                # 下载这类目录走"聚合"：攒够一定量一次性问，不逐个文件弹窗
                self.handled[str(p)] = (size, time.time())
                self.log("聚合", p.name, size, "纳入聚合统计（不逐文件打扰）")
                continue
            if self.in_focus_dirs(p):
                focus_ready.append((p, size))
            else:
                other_ready.append((p, size))

        # 重点目录 + 超阈值的单独问；其余并入批次
        single_mb = float(self.config.get("单文件询问阈值MB", 10))
        batch_mb = float(self.config.get("批量询问阈值MB", 50))
        singles = [(p, s) for p, s in focus_ready]
        for p, size in other_ready:
            if single_mb > 0 and size >= single_mb * 2 ** 20:
                singles.append((p, size))
            else:
                if not self.batch:
                    self.batch_since = now
                self.batch[str(p)] = size

        # 弹窗冷却：刚问过一次就把新请求并为一批，避免连续弹窗轰炸
        for p, size in singles:
            if time.time() - self.last_ask_ts < self.ASK_COOLDOWN:
                if not self.batch:
                    self.batch_since = time.time()
                self.batch[str(p)] = size
            else:
                self._run_ask([(p, size)])

        if self.batch:
            total = sum(self.batch.values())
            elapsed = now - (self.batch_since or now)
            if total >= batch_mb * 2 ** 20 or elapsed > 25:
                items = []
                for sp, s in list(self.batch.items())[:50]:
                    pp = Path(sp)
                    if pp.exists():
                        items.append((pp, s))
                self.batch.clear()
                self.batch_since = 0.0
                if items:
                    self._run_ask(items)

    def _run_ask(self, items):
        """执行一次询问并处理去向；弹窗期间的新事件保留。"""
        self.last_ask_ts = time.time()
        modal_start = time.time()
        self.handle_write(items)
        self._mark_handled(items)
        if not self.auto_choice:
            self.drain_after_modal(modal_start)

    def _is_handled(self, path: Path, size: int) -> bool:
        rec = self.handled.get(str(path))
        if rec and rec[0] == size and time.time() - rec[1] < 90:
            return True
        return False

    def _mark_handled(self, items):
        now = time.time()
        for p, s in items:
            self.handled[str(p)] = (s, now)
        if len(self.handled) > 5000:
            self.handled = {k: v for k, v in self.handled.items() if now - v[1] < 300}

    def drain_after_modal(self, modal_start):
        """弹窗期间积压的旧事件（多为自己搬文件产生）丢弃；新事件保留继续处理。"""
        keep = []
        dropped = 0
        while True:
            try:
                item = self.q.get_nowait()
            except queue.Empty:
                break
            if item[0] >= modal_start - 0.5:
                keep.append(item)
            else:
                dropped += 1
        for item in keep:
            try:
                self.q.put_nowait(item)
            except queue.Full:
                break
        if dropped > 20:
            print(f"{ts()}    （弹窗期间积压的 {dropped} 条旧事件已忽略）")

    def check_removed(self, now):
        """永久删除（不进回收站）的告知。"""
        for path, first in list(self.pending_removed.items()):
            if now - first < 2.5:
                continue
            self.pending_removed.pop(path, None)
            p = Path(path)
            if p.exists():
                continue                      # 只是重命名/移动
            if not self.config.get("删除保护", True):
                continue
            ext = p.suffix.lower()
            if ext in {e.lower() for e in self.config.get("删除静默扩展名", [])}:
                continue
            if ext in self.muted_delete_exts:
                continue
            pl = str(p).lower()
            dirs = self.config.get("删除保护目录", [])
            if dirs and not any(pl.startswith(str(d).lower()) for d in dirs):
                continue
            if ext not in (DOC_EXTS | IMG_EXTS | VID_EXTS | ARC_EXTS | CODE_EXTS | DB_EXTS):
                continue
            if self.recently_recycled(p):
                continue                      # 已在回收站里，走正常删除提示
            if self.auto_choice:
                self.log("删除", p.name, 0, "检测到永久删除")
                continue
            self.log("删除", p.name, 0, "检测到永久删除")
            r = alert_box("C 盘守卫 — 检测到文件被永久删除\n（删除保护）",
                          f"文件从 C 盘消失了，而且没有进回收站：\n\n"
                          f"📄 {p.name}\n📁 {p.parent}\n\n"
                          f"━━━ 隐患评估：🔴 高风险 ━━━\n"
                          f"· 没有进回收站（Shift+Delete 或程序删除），**无法还原**\n"
                          f"· 请确认这不是你误操作的\n\n"
                          f"如果这是误删，请立刻停止往 C 盘写入新数据，"
                          f"并尽快用数据恢复工具（如 Recuva）尝试恢复。",
                          MB_ICONWARNING)
            _ = r

    def recently_recycled(self, p: Path) -> bool:
        try:
            for d in self.recycle.dirs:
                for item in self.recycle.list_items(d):
                    if item["orig"].lower() == str(p).lower():
                        return True
        except Exception:
            pass
        return False

    # ========================================================
    # 六、兜底轮询（重点目录）
    # ========================================================
    def scan(self) -> dict:
        snap = {}
        for root in self.config.get("监控目录", {}).values():
            root_p = Path(root)
            if not root_p.exists():
                continue
            for cur, dirs, files in os.walk(root_p):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for fn in files:
                    p = Path(cur) / fn
                    try:
                        st = p.stat()
                        snap[str(p)] = (st.st_size, st.st_mtime)
                    except OSError:
                        pass
        return snap

    def poll_fallback(self):
        current = self.scan()
        for p, (size, mtime) in current.items():
            if p in self.known:
                continue
            path = Path(p)
            if path.suffix.lower() in self.ignore_exts:
                continue
            if self._is_handled(path, size):
                continue
            if path.exists():
                self._run_ask([(path, size)])
        self.known = self.scan()

    # ========================================================
    # 自启动自修复
    # ========================================================
    @staticmethod
    def ensure_autostart():
        exe = sys.executable if getattr(sys, "frozen", False) else None
        if not exe:
            return
        want = f'"{exe}" --silent'
        try:
            import winreg
            path = r"Software\Microsoft\Windows\CurrentVersion\Run"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0,
                                winreg.KEY_READ | winreg.KEY_SET_VALUE) as k:
                try:
                    cur, _ = winreg.QueryValueEx(k, "CDiskGuard")
                except FileNotFoundError:
                    cur = ""
                if cur != want:
                    winreg.SetValueEx(k, "CDiskGuard", 0, winreg.REG_SZ, want)
                    print("    🔧 已自动注册/修复开机自启项")
        except Exception as e:
            print(f"    ⚠️ 自启动注册失败（不影响本次监控）: {type(e).__name__}")

    # ========================================================
    # 主循环
    # ========================================================
    def run(self):
        cfg = self.config
        if cfg.get("聚合提示", True):
            try:
                self.growth.sync_targets()
                n = sum(1 for v in self.growth.state["apps"].values() if not v.get("muted"))
                print(f"    聚合监控：已盯上 {n} 个会长大的目录")
            except Exception as e:
                print(f"    ⚠️ 聚合监控初始化失败: {type(e).__name__}")
        st = disk_state()
        print("=" * 62)
        print("🛡️  C 盘守卫 v5.0 已启动")
        print("    把不该进 C 盘的东西挡在外面 —— 每一步都说人话，每一步都可撤回")
        print(f"    C 盘：{human_size(st['used'])} / {human_size(st['total'])}"
              f"（已用 {st['pct']:.1f}%，剩余 {human_size(st['free'])}）{st['icon']} {st['level']}")
        print(f"    ① 聚合监控："
              f"{'开启' if cfg.get('聚合提示') else '关闭'}"
              f"（单个来源涨够 {cfg.get('聚合增长阈值MB')} MB 才问一次）")
        print(f"    ② 删除保护："
              f"{'开启（隔离区）' if cfg.get('隔离区') else '关闭'}"
              f"（保留 {cfg.get('隔离区保留天数')} 天，当前 {self.quar.summary()}）")
        print(f"    ③ 安装哨兵：{'开启' if cfg.get('监控软件安装') else '关闭'}")
        print(f"    ⑤ 用户目录新文件夹拦截："
              f"{'开启' if cfg.get('根目录拦截') else '关闭'}"
              f"（C:\\Users\\用户名 下出现新文件夹就问）")
        print(f"    ④ 重点目录（新文件仍会逐个问）：{', '.join(cfg['监控目录'].keys())}")
        print(f"       下载目录已改为聚合模式，不再逐文件打扰")
        sec = detect_security_software()
        if sec:
            print(f"    ⚠️ 检测到安全软件：{', '.join(sec)}")
            print("       若询问框没弹出来，请到它的「弹窗拦截」里把 C盘守卫.exe 加入信任")
        print("    按 Ctrl+C 停止")
        print("=" * 62)

        try:
            with open(LOG_DIR / "启动记录.log", "a", encoding="utf-8") as f:
                f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  启动 v5.0\n")
        except Exception:
            pass

        self.known = self.scan()
        self.recycle.seed()
        self.ensure_autostart()
        roots = self.start_watchers()
        for w in self.watchers:
            time.sleep(0.15)
            if w.ok:
                print(f"    ✅ 已监听：{w.root}")
            else:
                print(f"    ⚠️ 监听失败：{w.error}（改用轮询兜底）")
        print(f"基线：{len(self.known)} 个文件 / {len(self.install_known)} 个已登记软件，开始守护...\n")
        _ = roots

        while True:
            try:
                time.sleep(1)
                now = time.time()
                self.consume_events(now)
                self.flush_writes(now)
                self.check_removed(now)

                if now - self.last_recycle > 3:
                    self.last_recycle = now
                    if self.config.get("删除保护", True):
                        new_items = self.recycle.poll_new()
                        if new_items:
                            modal_start = time.time()
                            self.handle_deletes(new_items)
                            if not self.auto_choice:
                                self.drain_after_modal(modal_start)

                self.check_installs(now)
                self.check_root_dirs(now)
                self.check_growth()

                if now - self.last_poll > float(self.config.get("轮询间隔秒", 20)):
                    self.last_poll = now
                    self.poll_fallback()

                self.maybe_space_alert()
            except KeyboardInterrupt:
                break
            except Exception as e:
                print(f"{ts()} ⚠️ 循环异常(已忽略): {type(e).__name__}: {e}")
                time.sleep(2)


# ============================================================
# 自检 / 测试
# ============================================================

def sweep_report():
    """软件体检：把 C 盘安装目录里的软件按体积排序，说明各自占多少。"""
    print("—— C 盘软件体检 ——")
    g = Guard()
    rows = []
    for k, (pdir, mt) in g.list_install_dirs().items():
        size, has_exe = Guard.dir_size(Path(pdir))
        if size > 0:
            rows.append((size, Path(pdir).name, pdir))
    rows.sort(reverse=True)
    total = sum(r[0] for r in rows)
    st = disk_state()
    print(f"C 盘：已用 {human_size(st['used'])} / {human_size(st['total'])}"
          f"，剩余 {human_size(st['free'])}（{st['icon']} {st['level']}）")
    print(f"安装目录共 {len(rows)} 个，合计 {human_size(total)}\n")
    for size, name, pdir in rows:
        print(f"  {human_size(size):>9}  {name}")
    # 落一份报告
    try:
        out = LOG_DIR / f"软件体检_{datetime.date.today():%Y%m%d}.txt"
        with open(out, "w", encoding="utf-8") as f:
            f.write(f"C 盘软件体检 {datetime.datetime.now():%Y-%m-%d %H:%M}\n")
            f.write(f"已用 {human_size(st['used'])} / {human_size(st['total'])}，剩余 {human_size(st['free'])}\n")
            f.write(f"安装目录 {len(rows)} 个，合计 {human_size(total)}\n\n")
            for size, name, pdir in rows:
                f.write(f"{human_size(size):>9}  {name}    {pdir}\n")
        print(f"\n报告：{out}")
    except Exception as e:
        print(f"报告写入失败：{e}")
    return rows


def self_test(auto: str):
    print("—— C 盘守卫 v4.0 自检 ——")
    g = Guard(auto_choice=auto)
    test_dir = Path(g.config["监控目录"]["下载"])
    test_dir.mkdir(parents=True, exist_ok=True)

    f1 = test_dir / "守卫自检_普通文档.txt"
    f1.write_text("自检测试文件", encoding="utf-8")
    g.handle_write([(f1, f1.stat().st_size)])

    f2 = test_dir / "守卫自检_安装包.exe"
    f2.write_bytes(b"MZ" + b"\x00" * 100)
    g.handle_write([(f2, f2.stat().st_size)])

    print("\n结果：")
    for f in (f1, f2):
        print(f"  {f.name} -> {'仍在C盘' if f.exists() else '已转移出C盘'}")
    print(f"日志: {g.log_path}")


def test_space_report():
    print("—— 风险说明文案预览 ——")
    print(space_report(1.5 * 2 ** 30))
    print()
    print("—— 删除隐患评估预览 ——")
    demo = [
        (Path(str(C_USER / "Documents" / "报价单.xlsx")), 240_000),
        (Path(str(C_USER / "Desktop" / "照片.jpg")), 5_000_000),
        (Path(str(C_USER / "AppData" / "Local" / "Foo" / "foo.dll")), 120_000),
    ]
    for p, s in demo:
        level, icon, notes = assess_delete(p, s, in_recycle=True)
        print(f"{icon} {level}  {p}")
        for n in notes:
            print(f"    · {n}")


def test_delete():
    """模拟删除一个文件（进回收站），验证检测与还原。"""
    print("—— 删除保护测试 ——")
    g = Guard(auto_choice="keep")
    g.recycle.seed()
    target = Path(g.config["监控目录"]["文档"]) / "守卫删除测试.docx"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("这是删除保护测试文件", encoding="utf-8")
    print(f"1. 已创建测试文件：{target}")
    p = move_to_recycle(target)
    print(f"2. 移入回收站：{'成功' if p else '失败'}")
    new = g.recycle.poll_new()
    print(f"3. 检测到新删除项：{len(new)} 个")
    for it in new:
        print(f"   · {it['orig']}（{human_size(it['size'])}）")
    hit = [it for it in new if "守卫删除测试" in it["orig"]]
    if hit:
        restored = RecycleBin.restore(hit[0])
        print(f"4. 还原结果：{restored or '失败'}")
        print(f"5. 文件已回到原位：{Path(restored).exists() if restored else False}")
        if restored:
            try:
                Path(restored).unlink()
                print("6. 测试文件已清理")
            except OSError:
                pass
    else:
        print("4. ⚠️ 未命中测试文件")


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", ctypes.c_void_p), ("wFunc", ctypes.c_uint),
                ("pFrom", ctypes.c_wchar_p), ("pTo", ctypes.c_wchar_p),
                ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", ctypes.c_int),
                ("hNameMappings", ctypes.c_void_p),
                ("lpszProgressTitle", ctypes.c_wchar_p)]


def move_to_recycle(path: Path) -> bool:
    """用 Shell API 把文件移到回收站（等同资源管理器里按 Delete）。"""
    FO_DELETE = 3
    FOF_ALLOWUNDO = 0x40
    FOF_NOCONFIRMATION = 0x10
    FOF_SILENT = 0x4
    FOF_NOERRORUI = 0x400
    op = SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = FO_DELETE
    op.pFrom = str(path) + "\x00\x00"
    op.pTo = None
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
    try:
        r = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        return r == 0 and not path.exists()
    except Exception:
        return False


_MUTEX_HANDLE = [None]   # 模块级引用：句柄必须活到进程结束，否则互斥形同虚设


def single_instance_lock():
    """保证只有一个守卫在跑（多开会重复弹窗）。返回句柄；None 表示已有实例。

    两个坑：
      1. CreateMutexW 的句柄如果不保存到模块级变量，函数一返回就被回收，
         互斥体随即失效，第二个实例照样能开起来。
      2. Local\\ 命名空间按会话 + 完整性级别隔离。计划任务/服务工作在高 IL，
         普通双击在中 IL，两者互相看不见；所以必须用 Global\\。
    """
    # 用 Global\：跨会话、跨完整性级别都能互相发现
    names = ("Global\\CDiskGuard_SingleInstance_v5",
             "Local\\CDiskGuard_SingleInstance_v5")
    last = None
    for name in names:
        try:
            k32.CreateMutexW.restype = ctypes.c_void_p
            k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
            h = k32.CreateMutexW(None, 0, name)
            err = ctypes.get_last_error()
            if err == 183:            # ERROR_ALREADY_EXISTS：已有实例持有
                if h:
                    try:
                        k32.CloseHandle(ctypes.c_void_p(h))
                    except Exception:
                        pass
                return None
            if h:
                _MUTEX_HANDLE.append(ctypes.c_void_p(h))   # 保持存活
            last = h
        except Exception:
            continue
    return last or -1


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def _count_self_processes():
    """数一数「C盘守卫.exe」还有几个别的实例在跑（不含自己）。

    两个必须绕开的坑：
      1. PyInstaller 的 onefile 是「父 bootloader + 子 Python」双进程模型，
         同一个 exe 名天然就是 2 个进程 —— 所以必须先把「自己的 PID」和
        「自己的父进程 PID」排除掉，剩下的才算第二个实例。
      2. 打包环境里 subprocess 调 tasklist 行为不稳定（父子进程名字一样），
         这里直接用 Toolhelp API 枚举进程，不走外部命令。
    """
    try:
        my = k32.GetCurrentProcessId()
    except Exception:
        return 1
    try:
        snap = k32.CreateToolhelp32Snapshot(ctypes.c_uint32(0x00000002), 0)  # SNAPPROCESS
        if snap in (None, -1):
            return 1
    except Exception:
        return 1

    mine = {my}
    n = 0
    try:
        k32.Process32FirstW.restype = ctypes.c_int
        k32.Process32FirstW.argtypes = [ctypes.c_void_p,
                                        ctypes.POINTER(_PROCESSENTRY32W)]
        k32.Process32NextW.restype = ctypes.c_int
        k32.Process32NextW.argtypes = [ctypes.c_void_p,
                                       ctypes.POINTER(_PROCESSENTRY32W)]
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)

        # 第一遍：找出自己的父进程（bootloader），待会儿要排除
        ok = k32.Process32FirstW(ctypes.c_void_p(snap), ctypes.byref(entry))
        while ok:
            if entry.th32ProcessID == my:
                mine.add(entry.th32ParentProcessID)
                break
            ok = k32.Process32NextW(ctypes.c_void_p(snap), ctypes.byref(entry))

        # 第二遍：数排除自己父子之后还剩几个同名进程
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        ok = k32.Process32FirstW(ctypes.c_void_p(snap), ctypes.byref(entry))
        while ok:
            name = (entry.szExeFile or "").lower()
            if "守卫" in name and entry.th32ProcessID not in mine:
                n += 1
            ok = k32.Process32NextW(ctypes.c_void_p(snap), ctypes.byref(entry))
    except Exception:
        return 1
    finally:
        try:
            k32.CloseHandle(ctypes.c_void_p(snap))
        except Exception:
            pass
    return n


def _plan_only():
    """只算迁移计划，不动任何文件。"""
    print("\n正在扫描 C 盘（只看不改）…")
    plan = guard_wizard.build_plan()
    guard_wizard.print_plan(plan)
    print("\n这是试算结果，什么都没动。要执行请跑：C盘守卫.exe --wizard")


def _undo():
    ms = guard_wizard.load_migrations()
    if not ms:
        print("从来没有搬过东西，没有可撤回的记录。")
        return
    print("\n搬家记录：")
    for i, m in enumerate(ms, 1):
        flag = "可撤回" if m.get("可撤回") else "不可撤回"
        print(f"  {i:2d}. [{flag}] {m.get('类型','')}  {m.get('名称','')}  "
              f"{human_size(m.get('大小', 0))}")
        print(f"        {m.get('原位置','')}  →  {m.get('新位置','')}")
    raw = input("\n输入编号只撤这一项，输入 all 全部撤回，回车退出：").strip()
    if not raw:
        return
    if raw.lower() == "all":
        guard_wizard.undo_all()
        return
    try:
        m = ms[int(raw) - 1]
    except Exception:
        print("没看懂。")
        return
    if not m.get("可撤回"):
        print("这项不可撤回（缓存清理是永久的）。")
        return
    guard_wizard.undo_all(only=m.get("名称", ""))


def _install_weekly():
    """注册一个每周跑一次的周报计划任务（用 Windows 自己的任务计划，不用第三方常驻）。"""
    exe = sys.executable if getattr(sys, "frozen", False) else f'"{sys.executable}" "{Path(__file__).resolve()}"'
    cmd = f'"{exe}" --report' if getattr(sys, "frozen", False) else f'{exe} --report'
    try:
        r = subprocess.run(
            ["schtasks", "/Create", "/TN", "CDiskGuardWeekly", "/TR", cmd,
             "/SC", "WEEKLY", "/D", "SUN", "/ST", "10:00", "/RL", "LIMITED", "/F"],
            capture_output=True, timeout=60)
        txt = (r.stdout or b"").decode("gbk", "replace")
        print(txt.strip() or ("注册完成" if r.returncode == 0 else f"失败 code={r.returncode}"))
        print("以后每周日 10:00 会自动生成周报到 guard_log 目录。")
    except Exception as e:
        print(f"注册计划任务失败：{type(e).__name__}: {e}")


def main():
    ap = argparse.ArgumentParser(
        description="C 盘守卫 v5.0 —— 不该进 C 盘的东西，尽量挡在外面，且随时可撤回")
    ap.add_argument("--test", action="store_true", help="自检模式（写入转移链路）")
    ap.add_argument("--test-delete", action="store_true", help="测试删除保护与还原")
    ap.add_argument("--test-text", action="store_true", help="预览风险说明文案")
    ap.add_argument("--auto", metavar="KEEP|盘符", help="无人值守：自动决定（keep=全部保留）")
    ap.add_argument("--silent", action="store_true", help="静默启动（隐藏控制台，开机自启用）")
    ap.add_argument("--sweep", action="store_true", help="软件体检：列出装到 C 盘的软件")
    # ---- v5 四项核心 ----
    ap.add_argument("--wizard", action="store_true", help="首次设置向导：一次性把东西搬出 C 盘")
    ap.add_argument("--wizard-plan", action="store_true", help="只生成迁移计划，不执行")
    ap.add_argument("--wizard-all", action="store_true", help="向导采用推荐项直接执行（不交互）")
    ap.add_argument("--dashboard", action="store_true", help="空间仪表盘（含 HTML 报告）")
    ap.add_argument("--report", action="store_true", help="生成本周周报（HTML）")
    ap.add_argument("--quarantine", action="store_true", help="查看/还原/清理隔离区")
    ap.add_argument("--undo", action="store_true", help="撤销应用数据搬家（搬回 C 盘原位）")
    ap.add_argument("--restore-folders", action="store_true", help="还原系统文件夹的保存位置")
    ap.add_argument("--unmute", action="store_true", help="恢复所有「不再提醒」的聚合提醒")
    ap.add_argument("--install-weekly", action="store_true",
                    help="注册每周自动生成周报的计划任务")
    a = ap.parse_args()
    if a.silent:
        try:
            ctypes.windll.user32.ShowWindow(ctypes.windll.kernel32.GetConsoleWindow(), 0)
        except Exception:
            pass

    if a.wizard:
        guard_wizard.run_wizard()
    elif a.wizard_plan:
        _plan_only()
        return
    elif a.wizard_all:
        guard_wizard.run_wizard(yes=True)
    elif a.dashboard:
        guard_dashboard.dashboard(open_html=True)
    elif a.report:
        out = guard_dashboard.weekly_report()
        print(f"周报已生成：{out}")
    elif a.quarantine:
        guard_quarantine.interactive(
            retention_days=int(json.loads(
                CONFIG_PATH.read_text(encoding="utf-8")).get("隔离区保留天数", 30)
            ) if CONFIG_PATH.exists() else 30)
    elif a.undo:
        _undo()
    elif a.restore_folders:
        guard_wizard.restore_folders()
    elif a.unmute:
        guard_growth.GrowthMonitor(
            json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if CONFIG_PATH.exists() else {}).unmute_all()
        print("已恢复所有聚合提醒。")
    elif a.install_weekly:
        _install_weekly()
    elif a.test:
        self_test(a.auto or "keep")
    elif a.test_delete:
        test_delete()
    elif a.test_text:
        test_space_report()
    elif a.sweep:
        sweep_report()
    else:
        if single_instance_lock() is None:
            print("已经有 C 盘守卫在运行了，本次启动自动跳过（不会重复弹窗）。")
            print("如果你确实想再开一个，请先在任务管理器里结束现有的「C盘守卫.exe」。")
            time.sleep(3)
            return
        # 互斥失效时的最后一道防线：发现自己不是唯一实例也退出
        if _count_self_processes() > 1:
            print("检测到已有另一个 C 盘守卫在运行，为避免重复弹窗，本次启动退出。")
            time.sleep(3)
            return
        if not CONFIG_PATH.exists() and not a.silent and sys.stdin:
            print("=" * 62)
            print("第一次使用：要不要先做一次性设置？")
            print("  把桌面/下载/文档/视频、微信 QQ 网盘 WPS 浏览器缓存搬到其他盘，")
            print("  并改好「新内容保存位置」，之后就不用一个个文件地问了。")
            raw = input("  回车=直接开始守护 ｜ 输入 w = 打开设置向导：").strip().lower()
            if raw == "w":
                guard_wizard.run_wizard()
                input("\n设置完成，按回车开始守护…")
        Guard(auto_choice=a.auto).run()


if __name__ == "__main__":
    main()
