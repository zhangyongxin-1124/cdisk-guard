# -*- coding: utf-8 -*-
"""
guard_paths.py — C 盘「空间去哪了」知识库
=========================================
这个文件只做一件事：把「哪些常见的东西会悄悄吃掉 C 盘」沉淀成一张表。

分三类处理：
  用户文件夹  —— 桌面/下载/文档/图片/视频。这些要用系统正式的「位置」重定向
                （改 Known Folder），不是建联接就完事，否则微软自己的逻辑会分叉。
  应用数据    —— 微信/QQ/企业微信/钉钉/网盘/音乐软件的聊天文件、下载、缓存。
                这些用「搬家 + 目录联接」：软件继续往老路径写，数据落到别的盘。
  纯缓存      —— 浏览器 Cache、包管理器缓存、临时目录。这些删了不影响使用，
                提供「清理」而不是「迁移」。

注意：统计体积时必须跳过 junction / 符号链接，否则会把已经搬到别的盘的数据
      重复算一遍（跑分割点会无限递归）。
"""

import os
from pathlib import Path

REPARSE = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT


# ============================================================
# 工具：体积统计（跳过链接、有上限保护）
# ============================================================

def dir_size(path, max_items=80000):
    """统计目录体积，跳过 junction/符号链接。返回 (字节数, 文件数, 是否含exe)。"""
    total = 0
    count = 0
    has_exe = False
    root = str(path)
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if st.st_file_attributes & REPARSE:
                        continue          # junction / 符号链接：不跟随、不计数
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                        continue
                    count += 1
                    if count > max_items:
                        return total, count, has_exe
                    total += st.st_size
                    if not has_exe and e.name.lower().endswith((".exe", ".msi")):
                        has_exe = True
        except OSError:
            continue
    return total, count, has_exe


def is_junction(path) -> bool:
    try:
        return bool(os.lstat(str(path)).st_file_attributes & REPARSE)
    except OSError:
        return False


# ============================================================
# 知识库
# ============================================================

def _t(key, label, rels, category, action, note=""):
    return {"key": key, "label": label, "rels": rels, "category": category,
            "action": action, "note": note}


# action: migrate=搬家+联接   clean=可直接清理   both=两者皆可
APP_TARGETS = [
    # ---------- 微信 / QQ ----------
    _t("wechat_files", "微信 聊天文件（图片/视频/文档）",
       ["Documents/WeChat Files", "Documents/xwechat_files"], "应用数据", "migrate",
       "最容易被忽略的吞空间大户：群里转发的视频、图片全存在这"),
    _t("wecom_files", "企业微信 文件",
       ["Documents/WXWork", "AppData/Roaming/Tencent/WXWork"], "应用数据", "migrate"),
    _t("wechat_cache", "微信 运行缓存",
       ["AppData/Roaming/Tencent/WeChat", "AppData/Local/Tencent/WeChat"],
       "缓存", "both"),
    _t("qq_files", "QQ / TIM 收到的文件",
       ["Documents/Tencent Files", "Pictures/Tencent Files"], "应用数据", "migrate"),
    _t("qq_cache", "QQ / TIM 缓存",
       ["AppData/Roaming/Tencent/QQ", "AppData/Roaming/Tencent/TIM",
        "AppData/Local/Tencent/QQ"], "缓存", "both"),

    # ---------- 浏览器 ----------
    _t("edge_cache", "Edge 浏览器缓存",
       ["AppData/Local/Microsoft/Edge/User Data/Default/Cache",
        "AppData/Local/Microsoft/Edge/User Data/Default/Code Cache",
        "AppData/Local/Microsoft/Edge/User Data/Default/GPUCache"], "缓存", "clean",
       "清掉不影响收藏和登录状态，只是网页要重新加载一次"),
    _t("chrome_cache", "Chrome 浏览器缓存",
       ["AppData/Local/Google/Chrome/User Data/Default/Cache",
        "AppData/Local/Google/Chrome/User Data/Default/Code Cache"], "缓存", "clean"),
    _t("360chrome_cache", "360 浏览器缓存",
       ["AppData/Roaming/360se6/Application", "AppData/Local/360ChromeX/Chrome/User Data/Default/Cache"],
       "缓存", "clean", "Application 目录本身就有几百 MB，含内核和离线资源"),
    _t("firefox_cache", "Firefox 缓存",
       ["AppData/Local/Mozilla/Firefox/Profiles"], "缓存", "clean"),

    # ---------- 办公 / 网盘 ----------
    _t("wps", "WPS 缓存与云文档",
       ["AppData/Local/Kingsoft/WPS Office", "AppData/Roaming/Kingsoft"],
       "缓存", "migrate", "云文档的自动备份也会累积在这"),
    _t("baidunetdisk", "百度网盘",
       ["AppData/Roaming/baidu/BaiduNetdisk", "Documents/BaiduNetdiskWorkspace"],
       "应用数据", "migrate"),
    _t("dingtalk", "钉钉 文件与缓存",
       ["AppData/Roaming/DingTalk", "Documents/DingTalk"], "应用数据", "migrate"),
    _t("feishu", "飞书 文件与缓存",
       ["AppData/Roaming/Feishu", "AppData/Roaming/Lark"], "应用数据", "migrate"),

    # ---------- 影音 / 下载 ----------
    _t("cloudmusic", "网易云音乐 缓存（含下载歌曲）",
       ["AppData/Local/Netease/CloudMusic"], "缓存", "migrate"),
    _t("qqmusic", "QQ 音乐 缓存",
       ["AppData/Local/QQMusic", "AppData/Roaming/QQMusic"], "缓存", "migrate"),
    _t("thunder", "迅雷 缓存",
       ["AppData/Roaming/Thunder Network", "AppData/Local/Thunder Network"],
       "缓存", "both"),

    # ---------- 开发者 / 包管理器 ----------
    _t("devcaches", "开发工具缓存（pip / npm / uv / NuGet / electron）",
       ["AppData/Local/pip/cache", "AppData/Local/npm-cache", "AppData/Local/uv/cache",
        "AppData/Roaming/npm-cache", "AppData/Local/NuGet/v3-cache",
        "AppData/Local/electron/Cache"], "缓存", "clean",
       "装过 Python/Node 就会有，纯缓存，删了会自动重新下载"),
    _t("gradle", "Gradle 依赖缓存（~/.gradle）",
       [".gradle/caches", ".gradle/wrapper"], "缓存", "clean",
       "每跑一次 Android 构建就可能拉进几百 MB 依赖；根治办法是把环境变量 "
       "GRADLE_USER_HOME 指向别的盘，而不是反复删"),
    _t("jetbrains_kits", "JetBrains 自带 JDK 与运行时",
       [".jdks", ".dsh"], "缓存", "clean",
       "IDE 为了跑 Maven/Gradle 自动下载的运行环境，重装 IDE 会再下一份"),
    _t("jetbrains", "JetBrains IDE 缓存与日志",
       ["AppData/Local/JetBrains", "AppData/Roaming/JetBrains"], "缓存", "clean",
       "PyCharm/IDEA 的索引和本地历史，清掉后下次打开工程会重建索引"),
    _t("wxdevtools", "微信开发者工具 用户数据",
       ["AppData/Local/微信开发者工具"], "缓存", "clean",
       "含模拟器、项目缓存和调试日志，长期不开发小程序可以清"),

    # ---------- AI 客户端 ----------
    _t("doubao", "豆包 AI 客户端缓存",
       ["AppData/Local/Doubao/User Data"], "缓存", "clean",
       "本地日志和模型/资源缓存，删掉不影响账号，重新登录会再生成"),

    # ---------- 录屏 / 截图 ----------
    _t("captures", "录屏与截图（Captures）",
       ["Videos/Captures"], "应用数据", "migrate",
       "Win+G 或显卡录制的视频默认落在这里，几分钟录像就是几百 MB，建议直接搬家"),

    # ---------- 系统级缓存 ----------
    _t("temp_user", "临时文件（%TEMP%）",
       ["AppData/Local/Temp"], "缓存", "clean",
       "注意：正在运行的软件可能正在用其中的文件，清理时失败项会自动跳过"),
    _t("inetcache", "IE / WebView 缓存",
       ["AppData/Local/Microsoft/Windows/INetCache"], "缓存", "clean"),
    _t("thumbnail", "缩略图缓存",
       ["AppData/Local/Microsoft/Windows/Explorer"], "缓存", "clean",
       "清掉后文件夹缩略图会重建，不影响原图"),
    _t("crashdumps", "崩溃转储",
       ["AppData/Local/CrashDumps"], "缓存", "clean", "软件崩溃记录，排查问题用，日常可清"),
]

# 系统级（不在用户目录里，需要管理员权限才能动，只报告不建议自动处理）
SYSTEM_TARGETS = [
    _t("windows_temp", "Windows 临时目录", ["Windows/Temp"], "系统", "clean"),
    _t("win_update", "Windows 更新下载缓存", ["Windows/SoftwareDistribution/Download"],
       "系统", "clean", "磁盘清理工具也能清，需管理员权限"),
    _t("windows_old", "Windows.old（旧系统残留）", ["Windows.old"], "系统", "clean",
       "升级系统留下来的旧版 Windows，确认新系统没问题后可以删，通常几个 GB"),
]


# ============================================================
# 动态目标：目录名随软件版本变化，写不死，只能扫描时匹配
# ============================================================
# 每项 = (判断函数, key, 标题模板, 分类, action, 说明)
# 判据用的是 AppData\Local 下的直接子目录名。
DYNAMIC_RULES = [
    (lambda n: n.startswith("@") and n.endswith("-updater"),
     "app_updater", "软件更新安装包残留 · {name}", "安装包", "clean",
     "Electron 系软件（WorkBuddy、豆包等）装完后把安装包留在这，几十到几百 MB，"
     "删掉不影响已经装好的程序"),
    (lambda n: n.startswith("AndroidStudio"),
     "android_studio", "Android Studio 索引与缓存 · {name}", "缓存", "clean",
     "版本号会往上跳，每升一次级就多一份；清掉后重新打开工程只是首次索引慢"),
    (lambda n: n.startswith("ms-playwright") or n.startswith("vscodium"),
     "browser_runtime", "自动化测试浏览器运行时 · {name}", "缓存", "clean",
     "Playwright/Puppeteer 下载的 Chromium，删掉后重新跑脚本会再下载"),
]


def _scan_level(parent: Path):
    """列出某个目录下的子目录（跳过 junction）。"""
    try:
        return [e for e in os.scandir(str(parent))
                if e.is_dir(follow_symlinks=False) and not is_junction(e.path)]
    except OSError:
        return []


def _dynamic_targets(home: Path, depth: int = 2):
    """扫描 AppData\\Local 下面两层，挑出命中动态规则的目录。返回 [(dict, Path)]。

    为什么要看两层：大部分 Electron 软件把自己的目录直接放在 Local 根，
    但 Google / Microsoft 这类厂商会再套一层（Local\\Google\\AndroidStudioXXXX）。
    只做 scandir 目录名匹配，不递归统计体积，开销可以忽略。
    """
    local = home / "AppData" / "Local"
    out = []
    level1 = _scan_level(local)
    for e in level1:
        buckets = [(e.name, local / e.name)]
        if depth >= 2:
            for sub in _scan_level(local / e.name):
                buckets.append((sub.name, local / e.name / sub.name))
        for name, path in buckets:
            for pred, key, title, cat, act, note in DYNAMIC_RULES:
                if pred(name):
                    out.append((_t(key, title.format(name=name), [], cat, act, note), path))
                    break
    return out


# 已知文件夹（"新内容保存位置"）——用 GUID 调 SHSetKnownFolderPath 才是正统做法
KNOWN_FOLDERS = [
    ("桌面", "Desktop", "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}"),
    ("下载", "Downloads", "{374DE290-123F-4565-9164-39C4925E467B}"),
    ("文档", "Documents", "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}"),
    ("图片", "Pictures", "{33E28130-4E1E-4676-835A-98395C3BC3BB}"),
    ("视频", "Videos", "{18989B1D-99B5-455B-841C-AB7C74E4DDFC}"),
    ("音乐", "Music", "{4BD8D571-6D19-48D3-BE97-422220080E43}"),
]

# Registry 值名（HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders）
SHELL_REG_NAMES = {
    "桌面": "Desktop", "下载": "{374DE290-123F-4565-9164-39C4925E467B}",
    "文档": "Personal", "图片": "My Pictures", "视频": "My Video", "音乐": "My Music",
}


# ============================================================
# 扫描
# ============================================================

def _first_existing(home: Path, rels):
    for r in rels:
        p = home / r
        if p.exists() and not is_junction(p):
            return p
    return None


def scan_app_targets(home: Path, min_mb: float = 20.0, progress=None) -> list:
    """扫描知识库里所有应用目标，返回 [(dict, Path, size, files)]，按体积降序。"""
    out = []
    candidates = [(t, _first_existing(home, t["rels"])) for t in APP_TARGETS]
    candidates += _dynamic_targets(home)
    for t, p in candidates:
        if p is None:
            continue
        if progress:
            progress(t["label"])
        size, count, _ = dir_size(p)
        if size >= min_mb * 2 ** 20:
            out.append((t, p, size, count))
    out.sort(key=lambda x: -x[2])
    return out


def scan_system_targets(sysroot: Path, min_mb: float = 200.0) -> list:
    out = []
    for t in SYSTEM_TARGETS:
        p = sysroot / t["rels"][0]
        if not p.exists() or is_junction(p):
            continue
        size, count, _ = dir_size(p)
        if size >= min_mb * 2 ** 20:
            out.append((t, p, size, count))
    out.sort(key=lambda x: -x[2])
    return out


def scan_big_files(home: Path, min_mb: float = 500.0, top: int = 25,
                   skip_dirs=None) -> list:
    """在用户目录里找大文件（跳过 junction，避免把已搬走的数据算进来）。"""
    skip = {s.lower() for s in (skip_dirs or [])}
    rows = []
    for cur, dirs, files in os.walk(str(home)):
        try:
            st = os.lstat(cur)
            if st.st_file_attributes & REPARSE:
                dirs[:] = []
                continue
        except OSError:
            continue
        dirs[:] = [d for d in dirs
                   if (os.path.join(cur, d).lower() not in skip)]
        for fn in files:
            fp = os.path.join(cur, fn)
            try:
                st = os.stat(fp)
                if st.st_size >= min_mb * 2 ** 20:
                    rows.append((st.st_size, fp))
            except OSError:
                continue
    rows.sort(reverse=True)
    return rows[:top]
