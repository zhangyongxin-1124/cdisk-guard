# -*- coding: utf-8 -*-
"""
guard_legacy.py — 「存量盘点」：装 C 盘守卫之前，你的每块盘里已经存了什么
=========================================================================
给小白讲清楚三件事：
  1. 这台电脑一共有几块盘？每块盘多大、用了多少、还剩多少？
  2. 每块盘里，哪些东西是「装上 C 盘守卫之前就有的」老数据，
     哪些是装上守卫之后才新增的？
  3. 每一块内容「是什么」「能不能搬到别的盘」——全部说人话，不用术语。

设计要点（说实话）：
  · 判断「是不是装守卫之前就有的」，依据是文件的创建时间（Windows 的 ctime）。
    文件被复制进来时创建时间会刷新，所以它代表"这份内容最后一次完整落到这里"
    的时间，不是它最初的出生时间；系统里也没有更早的记录可查。
  · 为了让你改「分界日期」时不用重新扫描，扫描结果按「月份」记了账：
    hist = {"2024-03": 12345678, ...}。改日期后立刻就能重算，误差不超过一个月。
  · 只统计文件大小，不算目录本身；跳过系统级噪音目录（回收站、卷信息、
    WindowsApps 等权限禁区），保证又快又不报错。
"""

import datetime
import functools
import json
import os
import stat as _stat
import time
from pathlib import Path

import guard_drives
import guard_fastscan          # v6.12 高性能扫描引擎

INSTALL_FILE = "install_time.json"      # 守卫"生日"存在这个文件里
SNAPSHOT_FMT = "legacy_%s.json"         # 每块盘的存量快照：legacy_C.json


# ============================================================
# 一、守卫的「生日」——哪一天开始守护这台电脑
# ============================================================

def _dir_oldest_ctime(app_dir: Path):
    """扫描本程序数据目录里最早的一个时间，用来推断"第一次安装"是哪天。

    目录自身的创建时间也算进来——程序第一次在本机建目录的那一天，
    通常就是真正的安装日（比里面某个文件被复制/更新的时间更可信）。
    """
    oldest = None
    try:
        oldest = os.stat(str(app_dir)).st_ctime
    except Exception:
        pass
    try:
        for cur, dirs, files in os.walk(str(app_dir)):
            for dn in dirs:
                try:
                    ct = os.stat(os.path.join(cur, dn)).st_ctime
                except OSError:
                    continue
                if oldest is None or ct < oldest:
                    oldest = ct
            for fn in files:
                try:
                    ct = os.stat(os.path.join(cur, fn)).st_ctime
                except OSError:
                    continue
                if oldest is None or ct < oldest:
                    oldest = ct
    except Exception:
        pass
    return oldest


def _exe_ctime():
    """程序本体（exe）的创建时间。数据目录为空时用它推断安装日。"""
    try:
        import sys
        exe = Path(sys.executable) if getattr(sys, "frozen", False) else Path(__file__)
        return float(exe.stat().st_ctime)
    except Exception:
        return None


def load_install_time(app_dir: Path) -> dict:
    """读出守卫生日。

    返回 {"ts": 时间戳, "source": 说明文字}
    第一次用到这个功能时，用「程序数据目录里最早的文件」推断，
    这样即使你是老用户，也能得到一个接近真实的安装日期。
    """
    app_dir = Path(app_dir)
    f = app_dir / INSTALL_FILE
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        if d.get("ts"):
            return {"ts": float(d["ts"]), "source": d.get("source", "已记录")}
    except Exception:
        pass

    now = time.time()
    guess = _dir_oldest_ctime(app_dir)
    # 数据目录可能是刚建的（换电脑、或装在 Program Files 时数据落到
    # %LOCALAPPDATA%），这时用程序本体（exe）的创建时间兜底，
    # 比直接取"今天"更接近真实的安装日期。
    exe_guess = _exe_ctime()
    if exe_guess and (not guess or exe_guess < guess):
        guess = exe_guess
    if guess and guess < now:
        info = {"ts": float(guess),
                "source": "按本程序数据目录里最早的文件推断"}
    else:
        info = {"ts": now, "source": "本次首次运行，从今天开始记录"}
    save_install_time(app_dir, info["ts"], info["source"])
    return info


def save_install_time(app_dir: Path, ts: float, source: str = "手动设置"):
    try:
        Path(app_dir).mkdir(parents=True, exist_ok=True)
        (Path(app_dir) / INSTALL_FILE).write_text(
            json.dumps({"ts": float(ts), "source": source},
                       ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def fmt_ts(ts) -> str:
    """2026.11.01"""
    try:
        return datetime.datetime.fromtimestamp(float(ts)).strftime("%Y.%m.%d")
    except Exception:
        return ""


def fmt_dt(ts) -> str:
    """2026.11.01 18:09"""
    try:
        return datetime.datetime.fromtimestamp(float(ts)).strftime("%Y.%m.%d %H:%M")
    except Exception:
        return ""


# ============================================================
# 二、内容分类 —— 把路径翻译成一句人话
# ============================================================

_SYS_KEYS = ("\\windows\\", "\\windows.old", "\\perflogs",
             "\\programdata\\microsoft\\", "\\programdata\\package cache\\",
             "\\system volume information")
_PROGRAM_KEYS = ("\\program files", "\\programdata\\")
_CLOUD_KEYS = ("onedrive", "dropbox", "nutstore", "jianguoyun",
               "wps cloudfiles", "同步空间", "baidunetdiskworkspace")
_CACHE_KEYS = ("\\cache", "cachedata", "code cache", "gpucache", "shadercache",
               "\\logs", "\\log\\", "\\temp\\", "\\tmp\\", "\\dumps",
               "crashpad", "blob_storage")
_MEDIA_KEYS = ("\\pictures", "\\videos", "\\music", "\\照片", "\\视频")
_USER_KEYS = ("\\desktop", "\\documents", "\\桌面", "\\文档")
_DOWNLOAD_KEYS = ("\\downloads", "\\download", "\\迅雷下载", "\\torrents")

KINDS = {
    "system":   {"name": "Windows 系统文件", "color": "#c62828",
                 "tip": "这是 Windows 自己用的地盘。删错或搬错可能开不了机，"
                        "要瘦身请用系统自带的「磁盘清理」，不要手工搬。"},
    "program":  {"name": "装好的软件", "color": "#ef6c00",
                 "tip": "软件本体装在这里。要搬走，正确做法是先卸载、"
                        "再装到别的盘，直接搬容易出现打不开的情况。"},
    "appdata":  {"name": "软件的数据 / 设置", "color": "#1565c0",
                 "tip": "软件自己存的配置、聊天记录、账号数据。搬之前把软件完全退出"
                        "（右下角小图标也要退），搬完在原位置留「门牌」，软件无感。"},
    "cache":    {"name": "缓存 / 日志 / 临时", "color": "#1565c0",
                 "tip": "运行过程产生的中间文件，不是正经资料。删掉或搬走都不影响，"
                        "软件下次会自己重建。"},
    "userfile": {"name": "我的文件（文档 / 桌面）", "color": "#2e7d32",
                 "tip": "你自己保存的文件，想搬就搬，搬完照常打开。"},
    "media":    {"name": "照片 · 视频 · 音乐", "color": "#2e7d32",
                 "tip": "你自己的影音资料，是最值得搬到别的盘腾空间的内容。"},
    "download": {"name": "下载的东西", "color": "#2e7d32",
                 "tip": "下载文件夹里的安装包、压缩包、电影等，用完可以搬走或删掉。"},
    "cloud":    {"name": "网盘同步的文件", "color": "#c62828",
                 "tip": "正在和网盘自动同步。要换位置请到网盘客户端的"
                        "「设置 → 同步位置」里改，直接搬会造成反复重传和冲突副本。"},
    "other":    {"name": "其他资料", "color": "#777777",
                 "tip": "不好归类的一份内容。点右键「打开所在位置」看看是什么再决定。"},
}

# 非系统盘上的内容：本来就不占 C 盘，给一句更贴心的说明
_OTHER_DISK_NOTE = ("本来就在 {d} 盘，没有挤占 C 盘，不用搬家。")


def kind_of(path: str, sys_letter: str = None) -> dict:
    """给一个路径判定「这是什么」，返回 {"key","name","color","tip"}。"""
    # 统一分隔符（快照里可能存着 C:/Windows 这种写法），再补一个末尾分隔符，
    # 让 "C:\Windows" 也能命中 "\windows\" 这类特征词
    pl = str(path or "").lower().replace("/", "\\") + "\\"
    sysl = (sys_letter or guard_drives.system_letter()).upper()
    drv = ""
    try:
        drv = Path(str(path)).anchor.upper().rstrip(":\\")[:1]
    except Exception:
        pass
    on_system = (not drv) or (drv == sysl)

    if on_system:
        for k in _SYS_KEYS:
            if k in pl:
                return _mk("system")
        if "\\appdata\\" not in pl:
            for k in _CLOUD_KEYS:
                if k in pl:
                    return _mk("cloud")
        if "\\appdata\\" in pl:
            for k in _CACHE_KEYS:
                if k in pl:
                    return _mk("cache")
            return _mk("appdata")
        for k in _CACHE_KEYS:
            if k in pl:
                return _mk("cache")
        for k in _PROGRAM_KEYS:
            if k in pl:
                return _mk("program")
        for k in _DOWNLOAD_KEYS:
            if k in pl:
                return _mk("download")
        for k in _MEDIA_KEYS:
            if k in pl:
                return _mk("media")
        for k in _USER_KEYS:
            if k in pl:
                return _mk("userfile")
        return _mk("other")

    # 非系统盘：只做粗分类，重点是告诉用户"不用搬"
    for k in _DOWNLOAD_KEYS:
        if k in pl:
            return _mk("download")
    for k in _MEDIA_KEYS:
        if k in pl:
            return _mk("media")
    for k in _USER_KEYS:
        if k in pl:
            return _mk("userfile")
    for k in _PROGRAM_KEYS:
        if k in pl:
            return _mk("program")
    return _mk("other")


def _mk(key: str) -> dict:
    m = KINDS[key]
    return {"key": key, "name": m["name"], "color": m["color"], "tip": m["tip"]}


def can_move_text(path: str, sys_letter: str = None) -> dict:
    """「这一块能不能搬到别的盘」——给小白的一句话结论 + 颜色。"""
    pl = str(path or "").lower().replace("/", "\\")
    sysl = (sys_letter or guard_drives.system_letter()).upper()
    drv = ""
    try:
        drv = Path(str(path)).anchor.upper().rstrip(":\\")[:1]
    except Exception:
        pass
    if drv and sysl and drv != sysl:
        return {"text": _OTHER_DISK_NOTE.format(d=drv), "color": "#777777",
                "order": 0}
    k = kind_of(path, sys_letter)
    table = {
        "system":   ("系统文件 · 不要动", 4),
        "cloud":    ("网盘同步中 · 别直接搬", 3),
        "program":  ("软件本体 · 建议卸载重装", 2),
        "appdata":  ("软件数据 · 退出软件后可搬", 1),
        "cache":    ("缓存临时 · 可放心搬", 1),
        "userfile": ("我的文件 · 可放心搬", 0),
        "media":    ("照片视频 · 可放心搬", 0),
        "download": ("下载文件 · 可放心搬", 0),
        "other":    ("先打开看看再决定", 0),
    }
    txt, order = table.get(k["key"], ("先打开看看再决定", 0))
    return {"text": txt, "color": k["color"], "order": order}


# ============================================================
# 三、存量扫描器
# ============================================================

# 扫描时跳过的目录（权限禁区 / 系统噪音，扫了也没意义）
SKIP_NAMES = {
    "$recycle.bin", "system volume information", "windowsapps",
    "node_modules", ".git", "winsxs", "$windows.~bt", "$windows.~ws",
}
# 非系统盘额外跳过（系统残留 / 磁盘元数据 / 旧系统备份）
OTHER_SKIP = {"recovery", "windows.old", "found.000", "msocache",
              "perflogs", ".trashes", ".trashes-1000"}


class LegacyScanner:
    """扫描一块盘上「所有内容」，按文件夹汇总，并按月份记账。

    结果形态：
        {"drive":"C", "ts":..., "baseline":..., "rows":[...],
         "total": 总字节, "count": 文件数}
    每行 row：
        {"path", "size", "count", "hist": {"2024-03": 字节, ...},
         "oldest": 最早创建时间戳, "newest": 最近改动时间戳}

    roots 的设计目的：既看到大块（C:\\Windows 一整块），
    又能看清细节（桌面、下载、AppData\\Local\\某个软件），不重不漏。
    """

    def __init__(self, app_dir: Path, drive: str = None):
        self.app_dir = Path(app_dir)
        self.drive = (str(drive).upper().rstrip(":\\")[:1]
                      if drive else guard_drives.system_letter())

    def set_drive(self, letter: str) -> str:
        self.drive = (str(letter).upper().rstrip(":\\")[:1]
                      or guard_drives.system_letter())
        return self.drive

    def is_system(self) -> bool:
        return self.drive == guard_drives.system_letter()

    @property
    def snapshot_file(self) -> Path:
        return self.app_dir / (SNAPSHOT_FMT % self.drive)

    # ---- 扫描范围 ----
    def roots(self) -> list:
        """[(目录, 汇总深度, 排除的子目录名集合)]"""
        if not self.is_system():
            root = Path(f"{self.drive}:\\")
            return [(root, 2, OTHER_SKIP)] if root.exists() else []

        user = Path(os.environ.get("USERPROFILE", "C:\\Users\\Public"))
        out = []
        for sub in ("Desktop", "Downloads", "Documents",
                    "Pictures", "Videos", "Music"):
            p = user / sub
            if p.exists():
                out.append((p, 2, set()))
        for sub in ("Local", "Roaming", "LocalLow"):
            p = user / "AppData" / sub
            if p.exists():
                out.append((p, 1, set()))
        for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
            v = os.environ.get(env)
            if v and Path(v).exists():
                out.append((Path(v), 1, set()))
        # 用户目录下剩下的（OneDrive、各类工具目录等），避开上面已单独扫过的
        handled = {"desktop", "downloads", "documents", "pictures",
                   "videos", "music", "appdata"}
        if user.exists():
            out.append((user, 2, handled))
        # 盘根目录剩下的（Windows 等），避开 Users / Program Files / ProgramData
        top_ex = {"users", "program files", "program files (x86)",
                  "programdata", "documents and settings"}
        root = Path(f"{self.drive}:\\")
        if root.exists():
            out.append((root, 1, top_ex))
        return out

    def _skip(self, name: str) -> bool:
        nl = name.lower()
        if nl in SKIP_NAMES:
            return True
        if not self.is_system() and nl in OTHER_SKIP:
            return True
        return False

    # ---- 扫描 ----
    def scan(self, baseline_ts: float, progress_cb=None, cancel_evt=None,
             progressive: bool = True) -> dict:
        """v6.14：默认走分步扫描 —— 先统计大目录，再按占用高低逐个展开分支。

        界面能持续看到「阶段 1/2 · 第几个分支」，磁盘压力也更平稳；
        progressive=False 可退回 v6.13 的一次性并发扫描（用于结果比对）。
        两种模式的统计口径完全一致（见 _test_v614.py 的逐项断言）。
        """
        buckets = {}

        # v6.12：原实现单线程串行 scandir，整盘盘点要几分钟。
        # 改走 guard_fastscan 并发分片，实测大目录快 6~17 倍。
        def descend(parent, level, ctx, entry):
            if self._skip(entry.name):
                return None
            if level == 0 and ctx["exclude"] and \
                    entry.name.lower() in ctx["exclude"]:
                return None
            nk = entry.path if (level + 1) <= ctx["depth"] else ctx["key"]
            return {"key": nk, "depth": ctx["depth"],
                    "exclude": ctx["exclude"]}

        def visit(entry, parent, level, ctx):
            if entry.is_dir:
                return
            key = ctx["key"]
            b = buckets.get(key)
            if b is None:
                b = buckets[key] = {"path": key, "size": 0,
                                    "count": 0, "hist": {},
                                    "oldest": None, "newest": None}
            sz = int(entry.size or 0)
            b["size"] += sz
            b["count"] += 1
            ct = entry.ctime or entry.mtime
            mt = entry.mtime
            mk = _month_key(ct)
            b["hist"][mk] = b["hist"].get(mk, 0) + sz
            if b["oldest"] is None or ct < b["oldest"]:
                b["oldest"] = ct
            if mt and (b["newest"] is None or mt > b["newest"]):
                b["newest"] = mt

        roots = []
        for root, depth, exclude in self.roots():
            base = str(root)
            # "C:////" 被 rstrip 成 "C:" 会变成相对路径，scandir 出来的结果
            # 会丢掉分隔符（"C:Windows"），导致分类完全认不出来；盘符根要保住反斜杠
            base = base.rstrip("\\") or base
            if len(base) == 2 and base[1] == ":":
                base += "\\"
            roots.append((base, 0, {"key": base, "depth": depth,
                                    "exclude": exclude}))

        if progressive and roots:
            for base, _lvl, ctx in roots:
                if cancel_evt is not None and cancel_evt.is_set():
                    break

                def _branch_ctx(e, _c=ctx, _b=base, _d=descend):
                    return _d(_b, 0, _c, e)

                def _branch_filter(e, _c=ctx, _s=self):
                    if _s._skip(e.name):
                        return False
                    if _c["exclude"] and e.name.lower() in _c["exclude"]:
                        return False
                    return True

                guard_fastscan.progressive_scan(
                    base, ctx, descend, visit,
                    branch_ctx_fn=_branch_ctx, branch_filter=_branch_filter,
                    phase_cb=(lambda st, i, n, path, _p=progress_cb, _b=buckets:
                              _p(len(_b), "分步 %d/2 · %d/%d · %s"
                                 % (st, i, n,
                                    os.path.basename(str(path)) or str(path))))
                    if progress_cb else None,
                    progress_cb=(lambda n, cur: progress_cb(len(buckets), cur))
                    if progress_cb else None,
                    cancel_evt=cancel_evt, stat_dirs=False, progress_every=300)
        else:
            guard_fastscan.parallel_scan(
                roots, descend, visit, cancel_evt=cancel_evt,
                progress_cb=(lambda n, cur: progress_cb(len(buckets), cur))
                            if progress_cb else None,
                progress_every=300, stat_dirs=False)
        return self._finalize(buckets.values(), baseline_ts)

    def _finalize(self, rows, baseline_ts: float) -> dict:
        rows = [r for r in rows if r.get("size", 0) > 0]
        rows.sort(key=lambda r: -r["size"])
        rows = rows[:600]
        for r in rows:
            r.setdefault("hist", {})
            r["kind"] = kind_of(r["path"])["key"]
        return {"drive": self.drive, "ts": time.time(),
                "baseline": float(baseline_ts),
                "rows": rows,
                "total": sum(r["size"] for r in rows),
                "count": sum(r["count"] for r in rows)}

    # ---- 持久化 ----
    def load(self) -> dict:
        try:
            d = json.loads(self.snapshot_file.read_text(encoding="utf-8"))
            if d.get("rows"):
                for r in d["rows"]:
                    r.setdefault("hist", {})
                    r.setdefault("kind", kind_of(r["path"])["key"])
                return d
        except Exception:
            pass
        return {"rows": [], "total": 0, "count": 0, "ts": 0, "baseline": 0}

    def save(self, data: dict):
        try:
            self.app_dir.mkdir(parents=True, exist_ok=True)
            self.snapshot_file.write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass


def _month_key(ts: float) -> str:
    """v6.15：改用 localtime 直取年月 —— 替代每文件一次的
    fromtimestamp+strftime（约快 5 倍），结果与旧实现完全一致。"""
    try:
        lt = time.localtime(ts)
        return "%04d-%02d" % (lt.tm_year, lt.tm_mon)
    except Exception:
        return "1970-01"


def split_by(row: dict, baseline_ts: float) -> tuple:
    """按「分界日期」把一行拆成 (装守卫前就有, 之后新增)，单位字节。

    依据是扫描时按月份记的账，改日期后立刻生效，不用重新扫描。
    """
    hist = row.get("hist") or {}
    if not hist:
        # 老版本快照没有月度账：退化为「全部算老数据」
        return int(row.get("size", 0)), 0
    try:
        base_mk = datetime.datetime.fromtimestamp(float(baseline_ts)).strftime("%Y-%m")
    except Exception:
        base_mk = "9999-12"
    before = 0
    after = 0
    for mk, sz in hist.items():
        try:
            sz = int(sz)
        except Exception:
            continue
        if str(mk) <= base_mk:
            before += sz
        else:
            after += sz
    return before, after


def summarize(rows: list, baseline_ts: float) -> dict:
    """整块盘的汇总：总大小、装守卫前就有多少、之后新增多少。"""
    total = before = after = 0
    files = 0
    for r in rows:
        total += int(r.get("size", 0))
        files += int(r.get("count", 0))
        b, a = split_by(r, baseline_ts)
        before += b
        after += a
    return {"total": total, "before": before, "after": after, "files": files}


# ============================================================
# 四、全部磁盘总览
# ============================================================

def overview(app_dir: Path, baseline_ts: float, drives: list = None) -> list:
    """每一块盘一行：容量、已用、占用率、装守卫前已有 / 之后新增、状态。

    没有扫描过的盘，存量两列显示 None（界面显示「点扫描」）。
    """
    drives = drives if drives is not None else guard_drives.list_drives()
    out = []
    for d in drives:
        sc = LegacyScanner(app_dir, d["letter"])
        snap = sc.load()
        row = dict(d)
        if snap.get("rows"):
            s = summarize(snap["rows"], baseline_ts)
            row["scanned"] = True
            row["scan_ts"] = snap.get("ts", 0)
            row["found"] = s["total"]
            row["before"] = s["before"]
            row["after"] = s["after"]
            row["files"] = s["files"]
        else:
            row["scanned"] = False
            row["scan_ts"] = 0
            row["found"] = None
            row["before"] = None
            row["after"] = None
            row["files"] = None
        out.append(row)
    return out


def human_size(n) -> str:
    """把字节数说成人话：1.5 GB（和主程序显示保持一致）。"""
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {u}" if u != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def plain_summary(info: dict, baseline_ts: float) -> str:
    """给小白看的一句话总结（界面顶部大字）。

    例：C 盘一共 59.4 GB，已用 39.2 GB（72%）　｜　
        已经看清 35.1 GB 的内容，其中装守卫前就有 31.0 GB（88%），
        装上之后新增 4.1 GB
    """
    if not info.get("scanned"):
        return (f"{info['letter']} 盘一共 {human_size(info['total'])}，"
                f"已用 {human_size(info['used'])}（{info['pct']:.0f}%）　"
                f"还没盘点过，点上面的「扫描」看看里面都是些什么")
    before = info.get("before") or 0
    after = info.get("after") or 0
    found = info.get("found") or (before + after)
    used = info["used"] or 1
    tot = (before + after) or 1
    pb = before / tot * 100
    seen = found / used * 100 if used else 0
    return (f"{info['letter']} 盘一共 {human_size(info['total'])}，"
            f"已用 {human_size(info['used'])}（{info['pct']:.0f}%）　｜　"
            f"已经看清 {human_size(found)} 的内容（占已用 {seen:.0f}%），其中"
            f"装守卫前就有 {human_size(before)}（{pb:.0f}%），"
            f"装上之后新增 {human_size(after)}")
