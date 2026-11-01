# -*- coding: utf-8 -*-
"""
guard_today.py — 「今日新增」监测
================================
回答三个问题：
  1. 今天 C 盘多了哪些内容？（文件 / 文件夹，多大，什么时候出现的）
  2. 是从哪个应用软件产生的？（按路径特征归因到 微信 / QQ / 浏览器 / 网盘…）
  3. 占了 C 盘多少空间？—— 支持勾选后一键移动到其他盘。

技术边界（说实话）：
  Windows 普通应用拿不到"是哪个进程写的这个文件"（那需要内核级事件跟踪），
  我们采用两层归因：
    ① 路径特征库 —— 软件的数据目录都有明显的"门牌号"（WeChat Files、
       BaiduNetdiskDownloads、Tencent…），命中即归因；
    ② 目录归属 —— AppData\\Local\\<软件名>、Program Files\\<软件名> 直接用目录名。
  归因不到的标注「未知来源」，并给出完整路径供用户自行判断。
"""

import os
import re
import json
import time
import datetime
from pathlib import Path

# ============================================================
# 来源应用特征库（按优先级匹配，小写包含匹配）
# ------------------------------------------------------------
# 重要说明：
#   这只是「本地路径关键词 → 中文名称」的一张对照表，用来把
#   C:\...\KuGou\... 这样的路径翻译成人能看懂的名字。
#   它不包含任何 logo、图片、商标标识，不联网、不调用任何厂商接口；
#   与表中涉及的产品厂商没有任何隶属、合作或背书关系。
#   用户可在「设置」里一键关闭；关闭后只按文件夹名显示来源。
#   真正决定「哪些软件不许存 C 盘」的，是用户自己在
#   设置 → 软件存储限制 里手动添加的规则（见 guard_rules.py）。
# ============================================================

# 总开关：False 时完全不查下面这张表
USE_BUILTIN_SIGNATURES = True


def set_builtin_signatures(on: bool):
    global USE_BUILTIN_SIGNATURES
    USE_BUILTIN_SIGNATURES = bool(on)


APP_SIGNATURES = [
    (("wechat files", "xwechat_files", "wechat"), "微信"),
    (("tencent files", "\\tencent\\"), "QQ / 腾讯系"),
    (("wecom files", "wxwork"), "企业微信"),
    (("dingtalk",), "钉钉"),
    (("baidunetdisk", "baidu net disk"), "百度网盘"),
    (("aliyundrive", "alipan"), "阿里云盘"),
    (("quark", "\\quark"), "夸克网盘"),
    (("thunder", "xunlei"), "迅雷"),
    (("115download",), "115网盘"),
    (("\\chrome", "google\\chrome"), "Chrome 浏览器"),
    (("microsoft\\edge", "\\edge user data"), "Edge 浏览器"),
    (("firefox", "mozilla"), "Firefox 浏览器"),
    (("360",), "360 软件"),
    (("kingsoft", "wps cloudfiles", "\\wps"), "WPS Office"),
    (("steam",), "Steam"),
    (("epic games",), "Epic 游戏"),
    (("nvidia",), "NVIDIA"),
    (("workbuddy", ".workbuddy"), "WorkBuddy"),
    (("deepseek",), "DeepSeek"),
    (("doubao",), "豆包"),
    (("netease", "cloudmusic"), "网易云音乐"),
    (("kugou", "kuwo", "qqmusic"), "音乐软件"),
    (("potplayer", "vlc", "daum"), "视频播放器"),
    (("feishu", "lark"), "飞书"),
    (("compressed", "\\archive", "bandizip", "winrar", "7-zip"), "压缩软件"),
]

# 扫描时跳过的目录（噪音大 / 系统关键 / 永远搬不动）
SKIP_DIR_NAMES = {
    "$recycle.bin", "system volume information", "node_modules", ".git",
    "windowsapps", "wbcache", "temp", "tmp",
}
SKIP_PATH_KEYWORDS = [
    "\\cache", "gpucache", "code cache", "shadercache", "crashpad",
    "\\logs", "\\log\\", "blob_storage", "cacheddata",
]

# 扫描非系统盘时额外跳过的目录（系统残留 / 磁盘元数据）
OTHER_DRIVE_SKIP = {
    "$recycle.bin", "system volume information", "recovery",
    "$windows.~bt", "$windows.~ws", "windows.old", "found.000",
    "perflogs", "msocache", ".trashes", ".trashes-1000",
}

# 归因不到时的目录回退：AppData\Local\<名字> / Program Files\<名字> -> <名字>
_NOISE_DIR = re.compile(r"^[a-z0-9_\-. ]{2,40}$")


def guess_app(path: str) -> str:
    pl = path.lower()
    if USE_BUILTIN_SIGNATURES:
        for keys, name in APP_SIGNATURES:
            for k in keys:
                if k in pl:
                    return name
    # 目录归属回退：AppData\Local\<App>\... 或 Program Files\<App>\...
    m = re.search(r"appdata\\(local|roaming)\\([^\\]+)", pl)
    if m and _NOISE_DIR.match(m.group(2)):
        return m.group(2)
    m = re.search(r"program files( \(x86\))?\\([^\\]+)", pl)
    if m and _NOISE_DIR.match(m.group(2)):
        return m.group(2)
    m = re.search(r"programdata\\([^\\]+)", pl)
    if m and _NOISE_DIR.match(m.group(1)):
        return m.group(1)
    return "未知来源"


def human_size(n) -> str:
    n = float(n)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {u}" if u != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def system_letter() -> str:
    """Windows 所在盘符，例如 'C'。"""
    return Path(os.environ.get("SystemRoot", "C:\\Windows")).anchor.upper().rstrip(":\\") or "C"


# ============================================================
# 搬家风险评估 —— 用大白话告诉小白「搬走之后会不会出事」
# ============================================================
#
# 说明（说实话）：本软件搬东西用的是「先复制过去，成功后在原位留一个
# 目录联接(junction)」，也就是在原地址立一块「请去隔壁找我」的门牌。
# 所以对绝大多数软件来说路径没变、它自己察觉不到，出问题的是少数例外。
# 下面这套规则就是专门挑出那些例外，并把话讲成人话。

_SYS_PATH_KEYS = (
    "\\windows\\", "\\windows.old", "\\$windows.~bt", "\\$windows.~ws",
    "\\$recycle.bin", "\\system volume information", "\\perflogs",
    "\\programdata\\microsoft\\", "\\programdata\\package cache\\",
    "\\windowsapps", "\\winsxs",
)
_CLOUD_KEYS = (
    "onedrive", "dropbox", "nutstore", "jianguoyun",
    "wps cloudfiles", "同步空间", "baidunetdiskworkspace",
)
_CACHE_KEYS = (
    "\\cache", "cachedata", "code cache", "code_cache", "gpucache",
    "shadercache", "crashpad", "blob_storage", "cachestorage",
    "\\logs", "\\log\\", "\\temp", "\\tmp", "service worker",
    "\\dumps", "\\gmp-cache", "\\crashdumps",
)
_USER_DIR_KEYS = (
    "\\desktop", "\\downloads", "\\documents", "\\pictures",
    "\\videos", "\\music",
)
# AppData 里带这些词的，是软件的「更新残留」，走了就走了，软件会重新下载
_UPDATE_KEYS = (
    "updater", "update_download", "\\update\\", "\\updates\\",
    "\\patches\\", "\\patchcache", "pending\\", "\\download\\",
)
_DOWNLOAD_KEYS = (
    "baidunetdiskdownload", "download", "\\down", "xunlei", "thunder",
    "\\torrents", "\\迅雷下载",
)
_MEDIA_EXT = {
    ".mp4", ".mkv", ".avi", ".rmvb", ".flv", ".mov", ".wmv", ".ts",
    ".mp3", ".flac", ".wav", ".m4a", ".ape",
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic", ".tif",
    ".zip", ".rar", ".7z", ".tar", ".gz", ".iso", ".cab",
    ".exe", ".msi", ".dmg", ".apk", ".torrent",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".txt", ".md", ".csv", ".epub", ".psd", ".ai", ".sketch",
}

MOVE_LEVELS = {
    "system":     {"order": 4, "text": "系统文件 · 不要搬",   "color": "#c62828"},
    "cloud":      {"order": 3, "text": "云盘同步中 · 不建议搬", "color": "#c62828"},
    "program":    {"order": 2, "text": "程序安装位置 · 谨慎",  "color": "#ef6c00"},
    "appdata":    {"order": 1, "text": "软件数据 · 先退出软件", "color": "#1565c0"},
    "cache":      {"order": 1, "text": "缓存/日志 · 可放心搬",  "color": "#1565c0"},
    "data":       {"order": 0, "text": "个人文件 · 可放心搬",   "color": "#2e7d32"},
    "other_disk": {"order": 0, "text": "已不在系统盘 · 无需搬",  "color": "#777777"},
}

_MOVE_TIPS = {
    "system": (
        "这是 Windows 系统自己的地盘，搬不得。\n"
        "搬走可能造成更新失败、软件打不开，极端情况下甚至开不了机。\n"
        "系统文件要瘦身，请用系统自带的「磁盘清理」或 Windows 更新清理，别手工搬。"
    ),
    "cloud": (
        "这个位置正在和你的云盘自动同步。\n"
        "一旦搬走，同步会中断，可能出现反复重传或一堆冲突副本。\n"
        "想换地方，请打开对应网盘的客户端，在「设置 → 同步位置」里改。"
    ),
    "program": (
        "这是软件的安装位置，不是普通数据。\n"
        "搬之前请先把这个软件完全退出（右下角任务栏的图标也要退）。\n"
        "个别带驱动、开机服务的软件，搬完可能需要重新安装才能正常使用。"
    ),
    "appdata": (
        "这是「{app}」自己的资料目录。\n"
        "建议先完全退出该软件再搬：包括右下角任务栏里的小图标。\n"
        "搬好后本程序会在原位置留一块「门牌」指向新家，"
        "软件重新打开后照常读写，它自己感觉不到搬家。"
    ),
    "cache": (
        "这是软件运行时产生的缓存、日志或临时文件，不是正经资料。\n"
        "搬走甚至删掉都不影响软件本身，下次它会自动重建。\n"
        "搬之前把对应软件关掉即可，省得它正在写。"
    ),
    "data": (
        "这是你自己保存的文件（文档、照片、视频、安装包之类），"
        "不属于任何软件的核心部分。\n"
        "搬走之后，原来打开它的软件会顺着这块「门牌」去新位置读取，"
        "你几乎察觉不到变化，程序也不会出错。"
    ),
    "other_disk": (
        "这个文件本来就没堆在系统盘上，不用搬家。\n"
        "「今日新增」里列出来只是让你知道它今天来了。"
    ),
}


def _mk_risk(level: str, app: str = "未知来源") -> dict:
    meta = MOVE_LEVELS[level]
    tip = _MOVE_TIPS[level].replace("{app}", app or "未知来源")
    return {"level": level, "order": meta["order"],
            "text": meta["text"], "color": meta["color"], "tip": tip}


def move_risk(path: str, app: str = "未知来源", is_dir: bool = False,
              sys_letter: str = None) -> dict:
    """判断「把这里搬去别的盘」会不会影响程序正常使用。

    返回 {"level","order","text","color","tip"}，
    order 越大越危险（0=放心搬，4=千万别动）。
    """
    try:
        pl = str(path or "").lower()
    except Exception:
        pl = ""
    if not pl:
        return _mk_risk("appdata", app)

    # 1) 不在系统盘上的东西，本来就不用搬
    sysl = (sys_letter or system_letter()).upper()
    drv = ""
    try:
        drv = Path(str(path)).anchor.upper().rstrip(":\\")[:1]
    except Exception:
        pass
    if drv and sysl and drv != sysl:
        return _mk_risk("other_disk", app)

    # 2) 系统重地
    for k in _SYS_PATH_KEYS:
        if k in pl:
            return _mk_risk("system", app)
    # 3) 云盘同步目录
    #    只认「用户自己的同步根目录」（如 C:\Users\xxx\OneDrive\文档）。
    #    AppData 里的 OneDrive / Dropbox 目录是客户端程序自己的数据，不是同步内容，
    #    若也标红会让用户以为「到处都不能搬」，反而失去参考价值。
    if "\\appdata\\" not in pl:
        for k in _CLOUD_KEYS:
            if k in pl:
                return _mk_risk("cloud", app)
    # 4) 缓存 / 日志 / 临时：本来就该搬走
    for k in _CACHE_KEYS:
        if k in pl:
            return _mk_risk("cache", app)
    # 5) 程序安装位置
    if "\\program files" in pl or "\\programdata\\" in pl:
        return _mk_risk("program", app)
    # 6) AppData 一律算「软件自己的数据」——即使文件名看着像下载/媒体也不放松
    #    （实测：豆包的 update_download 有 820MB，落在 AppData 里，
    #     标成"个人文件放心搬"是危险的误判）
    if "\\appdata\\" in pl:
        for k in _UPDATE_KEYS:
            if k in pl:
                return _mk_risk("cache", app)
        return _mk_risk("appdata", app)
    # 7) 用户自己的文件夹与下载目录
    for k in _USER_DIR_KEYS:
        if k in pl:
            return _mk_risk("data", app)
    for k in _DOWNLOAD_KEYS:
        if k in pl:
            return _mk_risk("data", app)
    # 8) 明摆着是媒体/文档/安装包
    ext = os.path.splitext(pl)[1]
    if ext in _MEDIA_EXT:
        return _mk_risk("data", app)
    return _mk_risk("appdata" if is_dir else "cache", app)


def move_legend() -> str:
    """列表下方那行图例（富文本），一句话讲清各种颜色什么意思。"""
    parts = []
    for lv in ("data", "cache", "appdata", "program", "cloud", "system"):
        m = MOVE_LEVELS[lv]
        parts.append(f"<span style='color:{m['color']};font-weight:bold'>"
                     f"{m['text']}</span>")
    return ("移动说明： " + "　｜　".join(parts) +
            "　　（本软件搬家是在原位置留一块「门牌」，路径不变，软件基本无感）")


# ============================================================
# 今日新增扫描器
# ============================================================

class TodayScanner:
    """扫描某块盘上今天 0 点之后出现/变动过的文件，聚合到"来源位置"粒度。

    支持扫描任意磁盘：
        系统盘 —— 用"重点目录清单"精细扫描（桌面/下载/文档/AppData/Program Files…）
        其他盘 —— 从盘根目录整盘扫描（深度 other_depth 层，自动跳过系统元数据目录）
    每块盘的快照独立存放，互不干扰。
    """

    def __init__(self, app_dir: Path, drive: str = None):
        self.app_dir = Path(app_dir)
        self.drive = (str(drive).upper() if drive else system_letter())
        self.other_depth = 4          # 非系统盘的遍历深度上限

    # ---- 当前盘 ----
    def set_drive(self, letter: str) -> str:
        self.drive = (str(letter).upper().rstrip(":\\")[:1]) or system_letter()
        return self.drive

    def is_system(self) -> bool:
        return self.drive == system_letter()

    @property
    def snapshot_file(self) -> Path:
        """系统盘沿用旧文件名，其他盘用 today_files_<盘符>.json。"""
        if self.is_system():
            return self.app_dir / "today_files.json"
        return self.app_dir / f"today_files_{self.drive}.json"

    # ---- 扫描范围 ----
    def scan_roots(self) -> list:
        if self.is_system():
            return self._system_roots()
        root = Path(f"{self.drive}:\\")
        return [(root, self.other_depth)] if root.exists() else []

    def _system_roots(self) -> list:
        user = Path(os.environ.get("USERPROFILE", "C:\\Users\\Public"))
        roots = [
            (user / "Desktop", 6),
            (user / "Downloads", 6),
            (user / "Documents", 6),
            (user / "Pictures", 6),
            (user / "Videos", 6),
            (user / "Music", 6),
            (user / "AppData" / "Local", 4),
            (user / "AppData" / "Roaming", 4),
            (Path(os.environ.get("ProgramFiles", "C:\\Program Files")), 3),
            (Path(os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")), 3),
            (Path(os.environ.get("ProgramData", "C:\\ProgramData")), 3),
        ]
        return [(r, d) for r, d in roots if r.exists()]

    def _skip(self, dirpath: str, name: str) -> bool:
        nl = name.lower()
        if nl in SKIP_DIR_NAMES:
            return True
        if not self.is_system() and nl in OTHER_DRIVE_SKIP:
            return True
        dp = dirpath.lower()
        for kw in SKIP_PATH_KEYWORDS:
            if kw in dp or f"\\{kw}" in dp:
                return True
        return False

    def scan(self, progress_cb=None, cancel_evt=None) -> dict:
        """返回 {"items": [...], "total_size": int, "count": int, "ts": float}

        item: {"path", "name", "size", "mtime", "app", "is_dir"}
        只统计"今天 0 点后创建/修改过"的文件；目录本身也列出（便于整体搬移）。
        """
        day0 = datetime.datetime.combine(datetime.date.today(),
                                         datetime.time.min).timestamp()
        items = []
        roots = self.scan_roots()
        scanned = 0
        for root, depth in roots:
            base_depth = str(root).rstrip("\\").count("\\")
            for cur, dirs, files in os.walk(root):
                lvl = str(cur).count("\\") - base_depth
                if lvl >= depth:
                    dirs[:] = []
                dirs[:] = [d for d in dirs if not self._skip(cur, d)]
                for d in list(dirs):
                    full = os.path.join(cur, d)
                    try:
                        st = os.stat(full, follow_symlinks=False)
                        if st.st_mtime >= day0:
                            items.append({"path": full, "name": d,
                                          "size": 0, "mtime": st.st_mtime,
                                          "ctime": self._ctime(st),
                                          "app": guess_app(full), "is_dir": True})
                    except OSError:
                        pass
                for fn in files:
                    if cancel_evt is not None and cancel_evt.is_set():
                        return self._finalize(items)
                    full = os.path.join(cur, fn)
                    try:
                        st = os.stat(full, follow_symlinks=False)
                    except OSError:
                        continue
                    if st.st_mtime < day0:
                        continue
                    items.append({"path": full, "name": fn,
                                  "size": st.st_size, "mtime": st.st_mtime,
                                  "ctime": self._ctime(st),
                                  "app": guess_app(full), "is_dir": False})
                scanned += 1
                if progress_cb and scanned % 200 == 0:
                    progress_cb(len(items), cur)
        return self._finalize(items)

    # ---- 时间辅助：新增时间 = 创建时间（今天创建）否则修改时间 ----
    @staticmethod
    def _ctime(st) -> float:
        """Windows 下 st_ctime 即创建时间；取不到时退回修改时间。"""
        ct = getattr(st, "st_ctime", 0) or 0
        mt = getattr(st, "st_mtime", 0) or 0
        return ct if ct > 0 else mt

    @staticmethod
    def display_ts(it: dict) -> float:
        day0 = datetime.datetime.combine(datetime.date.today(),
                                         datetime.time.min).timestamp()
        ct = it.get("ctime") or 0
        mt = it.get("mtime") or 0
        return ct if ct >= day0 else (mt or ct)

    @staticmethod
    def _finalize(items: list) -> dict:
        items.sort(key=lambda x: -x["size"])
        return {"items": items,
                "total_size": sum(i["size"] for i in items),
                "count": len(items),
                "ts": time.time()}

    # ---- 实时事件登记（守卫运行期间捕获的新写入，立刻可见）----
    def record_event(self, path: str, size: int):
        """把一次实时写入登记到"它所在那块盘"的快照里。

        这样即使当前界面正在看 D 盘，一个写进 D 盘的文件也不会被误记到 C 盘清单。
        """
        try:
            drv = Path(path).anchor.upper().rstrip(":\\") or self.drive
        except Exception:
            drv = self.drive
        target = self if drv == self.drive else TodayScanner(self.app_dir, drv)
        target._record_event(path, size)

    def _record_event(self, path: str, size: int):
        try:
            data = self.load()
        except Exception:
            data = {"items": []}
        items = [i for i in data.get("items", []) if i.get("path") != path]
        st = Path(path).stat()
        items.append({"path": path, "name": Path(path).name, "size": size,
                      "mtime": st.st_mtime, "ctime": TodayScanner._ctime(st),
                      "app": guess_app(path), "is_dir": False})
        data["items"] = items
        data["count"] = len(items)
        data["total_size"] = sum(i["size"] for i in items)
        data["ts"] = time.time()
        self.save(data)

    # ---- 持久化 ----
    def load(self) -> dict:
        try:
            d = json.loads(self.snapshot_file.read_text(encoding="utf-8"))
            today = datetime.date.today().isoformat()
            if d.get("day") != today:      # 昨天的数据不再展示
                return {"items": [], "day": today}
            return d
        except Exception:
            return {"items": [], "day": datetime.date.today().isoformat()}

    def save(self, data: dict):
        data["day"] = datetime.date.today().isoformat()
        try:
            self.snapshot_file.write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    # ---- 聚合视图：按「来源应用 + 所在目录」汇总 ----
    @staticmethod
    def aggregate(items: list, max_rows: int = 400) -> list:
        groups = {}
        for it in items:
            key = (it["app"], str(Path(it["path"]).parent))
            g = groups.setdefault(key, {"app": it["app"],
                                        "dir": str(Path(it["path"]).parent),
                                        "size": 0, "count": 0,
                                        "paths": [], "all_paths": [],
                                        "is_dir": False,
                                        "first_ts": None, "last_ts": None})
            ts = TodayScanner.display_ts(it)
            if ts:
                g["first_ts"] = ts if g["first_ts"] is None else min(g["first_ts"], ts)
                g["last_ts"] = ts if g["last_ts"] is None else max(g["last_ts"], ts)
            g["size"] += it.get("size", 0)
            g["count"] += 1
            if len(g["paths"]) < 8:
                g["paths"].append(it["name"])
            if len(g["all_paths"]) < 300:
                g["all_paths"].append(it["path"])
        for it in items:                    # 整目录直接搬的场景
            if it.get("is_dir"):
                key = ("目录（整体搬移）", it["path"])
                ts = TodayScanner.display_ts(it)
                g = groups.setdefault(key, {"app": it["app"],
                                            "dir": it["path"],
                                            "size": 0, "count": 1,
                                            "paths": [it["name"]],
                                            "all_paths": [it["path"]],
                                            "is_dir": True,
                                            "first_ts": ts, "last_ts": ts})
                if not g.get("is_dir"):
                    g["is_dir"] = True
                if ts:
                    if g.get("first_ts") is None:
                        g["first_ts"] = ts
                    if g.get("last_ts") is None:
                        g["last_ts"] = ts
        rows = sorted(groups.values(), key=lambda x: -x["size"])
        rows = rows[:max_rows]
        # 给每组附上一句「搬走会不会影响软件」的大白话说明
        for r in rows:
            r["risk"] = move_risk(r["dir"], r.get("app", "未知来源"),
                                  bool(r.get("is_dir")))
        return rows
