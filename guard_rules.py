# -*- coding: utf-8 -*-
"""
guard_rules.py — 用户自定义「软件存储限制」规则
=================================================
设计原则（按用户要求）：
  · 本模块**不预置任何品牌名单**，内置表里一个 logo、一个厂商都没有；
  · 哪些软件不准存 C 盘，完全由用户自己手动添加、自己决定；
  · 规则就是一组「关键词」，落盘成本地 JSON，用户随时可改可删。

匹配方式：把写入路径和来源应用名都转小写，只要**包含**任一关键词即命中。
用户填什么就匹配什么，软件不做任何"智能猜测"。

数据文件：app_rules.json（与 exe 同目录，纯本地，不联网）
"""
import json
import os
import re
import time
from pathlib import Path
import sys

from guard_appdir import APP_DIR
DATA_FILE = APP_DIR / "app_rules.json"

# 用户输入软件名时，自动剥掉的通用后缀（避免"酷狗音乐"只剩"音乐"这种泛词）
_GENERIC = ("软件", "音乐", "播放器", "客户端", "浏览器", "输入法", "下载",
            "安装", "官方", "最新版", "电脑版", "app", "application")

# 从样本路径里挑目录名时，要排除的系统通用目录（否则会误伤一大片）
_SYS_SEG = {
    "users", "user", "appdata", "local", "locallow", "roaming", "programdata",
    "program files", "program files (x86)", "windows", "win32", "system32",
    "desktop", "documents", "downloads", "pictures", "videos", "music",
    "onedrive", "public", "default", "temp", "tmp", "cache", "data", "cache2",
    "微软", "用户", "桌面", "文档", "下载", "图片", "视频", "音乐",
}


def _sys_segments() -> set:
    """系统通用目录 + 当前用户名（用户名必须排除，否则整盘文件都会误命中）。"""
    s = set(_SYS_SEG)
    for v in (os.environ.get("USERNAME", ""), os.environ.get("USER", "")):
        if v and len(v) >= 2:
            s.add(v.strip().lower())
    try:                      # USERPROFILE 的最后一段（可能与 USERNAME 不同）
        prof = os.environ.get("USERPROFILE", "")
        last = re.split(r"[\\/]+", prof)[-1].lower() if prof else ""
        if len(last) >= 2:
            s.add(last)
    except Exception:
        pass
    return s


_FILE_EXT_RE = re.compile(r"\.[a-z0-9]{2,6}$")


def _keywords_from_sample(path: str, limit: int = 2) -> list:
    """从一条真实路径里挑出「像软件名」的目录名。

    策略（避开误伤）：
      1. 先找路径里最后一个「系统通用段」（users / appdata / local /
         program files / 用户名 …），取它**后面那一段**——这正是
         AppData\\Local\\<软件名> 的位置；
      2. 找不到系统段时，从后往前取第一个非文件名、非通用词的段；
      3. 全程排除盘符、用户名、系统目录、通用词，最多 limit 个。

    例：C:\\Users\\testuser\\AppData\\Local\\KuGou\\music\\a.mp3 -> ['kugou']
        D:\\Games\\SteamLibrary\\steamapps\\x.dat      -> ['steamapps']
    """
    out = []
    if not path:
        return out
    try:
        segs = [s.strip().lower() for s in re.split(r"[\\/]+", str(path)) if s.strip()]
    except Exception:
        return out
    skip = _sys_segments() | set(_GENERIC)

    def clean(s: str) -> str:
        if len(s) < 3 or len(s) > 40 or s.endswith(":"):
            return ""
        if _FILE_EXT_RE.search(s):          # 文件名：剥掉扩展名
            s = _FILE_EXT_RE.sub("", s)
            if len(s) < 3:
                return ""
        if s in skip or s.isdigit():
            return ""
        return s

    # 1) 最后一个系统段之后的那一段
    sys_idx = -1
    for i, s in enumerate(segs):
        if s in skip or s.endswith(":"):
            sys_idx = i
    if sys_idx >= 0 and sys_idx + 1 < len(segs):
        c = clean(segs[sys_idx + 1])
        if c:
            out.append(c)
    # 2) 从后往前补，凑到 limit（跳过最后一段，它通常是文件名）
    for s in reversed(segs[:-1]):
        c = clean(s)
        if c and c not in out:
            out.append(c)
        if len(out) >= limit:
            break
    return out[:limit]


def _auto_keywords(name: str) -> list:
    """从用户填的软件名自动拆出关键词：中文按连续汉字，英文按单词。"""
    out = []
    low = (name or "").strip().lower()
    if not low:
        return out
    # 中文片段
    for seg in re.findall(r"[\u4e00-\u9fa5]{2,}", low):
        if seg not in _GENERIC and seg not in out:
            out.append(seg)
    # 英文/数字片段
    for seg in re.findall(r"[a-z0-9]{2,}", low):
        if seg not in _GENERIC and seg not in out:
            out.append(seg)
    # 整体去空格（例如 "ku gou" 或 "酷狗 音乐"）
    compact = re.sub(r"[\s._\-]+", "", low)
    if len(compact) >= 2 and compact not in out:
        out.append(compact)
    return out[:8]


class AppRules:
    """用户自定义限制规则的增删改查 + 命中判断。"""

    def __init__(self, path: Path = None):
        self.path = Path(path) if path else DATA_FILE
        self.rules = []
        self.load()

    # ---------- 持久化 ----------
    def load(self):
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.rules = data.get("rules", []) if isinstance(data, dict) else []
            else:
                self.rules = []
        except Exception:
            self.rules = []
        # 兼容旧数据 / 补全字段
        for r in self.rules:
            r.setdefault("id", f"r{int(time.time() * 1000)}")
            r.setdefault("keywords", _auto_keywords(r.get("name", "")))
            r.setdefault("enabled", True)
            r.setdefault("hits", 0)
            r.setdefault("last_hit", 0)
        return self.rules

    def save(self):
        try:
            self.path.write_text(
                json.dumps({"version": 1, "rules": self.rules},
                           ensure_ascii=False, indent=2),
                encoding="utf-8")
            return True
        except Exception:
            return False

    # ---------- 增删改 ----------
    def add(self, name: str, keywords=None, sample_path: str = "") -> dict:
        """添加规则。sample_path 是可选的样本路径，用来补上英文目录关键词。"""
        name = (name or "").strip()
        if not name:
            return {}
        # 同名去重
        for r in self.rules:
            if r["name"].lower() == name.lower():
                return r
        kws = [k.strip().lower() for k in (keywords or []) if k and k.strip()]
        if not kws:
            kws = _auto_keywords(name)
        # 样本路径补关键词：解决「中文名 vs 英文目录」匹配不上的问题
        for k in _keywords_from_sample(sample_path):
            if k not in kws:
                kws.append(k)
        kws = kws[:10]
        rule = {
            "id": f"r{int(time.time() * 1000)}_{len(self.rules)}",
            "name": name,
            "keywords": kws,
            "enabled": True,
            "created": time.strftime("%Y-%m-%d %H:%M"),
            "hits": 0,
            "last_hit": 0,
        }
        self.rules.append(rule)
        self.save()
        return rule

    def remove(self, rid: str) -> bool:
        before = len(self.rules)
        self.rules = [r for r in self.rules if r.get("id") != rid]
        if len(self.rules) != before:
            self.save()
            return True
        return False

    def set_enabled(self, rid: str, on: bool) -> bool:
        for r in self.rules:
            if r.get("id") == rid:
                r["enabled"] = bool(on)
                self.save()
                return True
        return False

    def get(self, rid: str):
        for r in self.rules:
            if r.get("id") == rid:
                return r
        return None

    # ---------- 命中判断 ----------
    def match(self, path: str, app: str = "") -> dict:
        """给出写入路径（和可选的来源应用名），返回命中的规则，没有则 None。"""
        if not self.rules:
            return None
        hay = f"{path or ''}\n{app or ''}".lower()
        for r in self.rules:
            if not r.get("enabled", True):
                continue
            for k in r.get("keywords", []):
                if k and k in hay:
                    r["hits"] = int(r.get("hits", 0)) + 1
                    r["last_hit"] = time.time()
                    self.save()
                    return r
        return None

    def active_count(self) -> int:
        return len([r for r in self.rules if r.get("enabled", True)])


# 模块级单例，供引擎与界面共用
rules = AppRules()


# ============================================================
# 已安装/已下载软件枚举 —— 让用户「从电脑里挑」，而不是盲猜名字
# ============================================================
#
# 数据来源（全是 Windows 公开、只读的标准位置，不修改任何东西）：
#   1. 注册表「卸载程序」表（已安装软件的标准清单，微软公开 API）
#   2. AppData\Local / Roaming 下的软件目录（补绿色/便携软件）
#   3. 桌面、下载 目录里的安装包（.exe/.msi，补"刚下载还没装"的）
#
# 返回：[{"name": 显示名, "keywords": [...], "path": 安装/安装包路径,
#          "publisher": 发布者, "source": 来源标签}]

_UNINSTALL_KEYS = (
    ("HKLM64", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ("HKLM32", r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    ("HKCU", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
)

# 注册表里这些是系统组件/补丁/运行库，不是用户软件，跳过
_SKIP_PAT = (
    "kb", "update for microsoft", "security update", "hotfix",
    "windows sdk", "windows driver", "microsoft .net",
    "microsoft visual c++", "vc_redist", "redistributable",
    "directx", "webview2 runtime", "edge update", "chrome update",
)


def _reg_installed():
    """读注册表卸载表，返回已安装软件清单（只读，异常即跳过）。"""
    out = []
    try:
        import winreg
    except Exception:
        return out
    hives = {"HKLM64": winreg.HKEY_LOCAL_MACHINE,
             "HKLM32": winreg.HKEY_LOCAL_MACHINE,
             "HKCU": winreg.HKEY_CURRENT_USER}
    for tag, sub in _UNINSTALL_KEYS:
        hive = hives[tag]
        try:
            with winreg.OpenKey(hive, sub) as k:
                n = winreg.QueryInfoKey(k)[0]
                for i in range(n):
                    try:
                        name_key = winreg.EnumKey(k, i)
                        with winreg.OpenKey(k, name_key) as sk:
                            def _get(v):
                                try:
                                    return str(winreg.QueryValueEx(sk, v)[0]).strip()
                                except OSError:
                                    return ""
                            disp = _get("DisplayName")
                            if not disp:
                                continue
                            if _get("SystemComponent") in ("1",):
                                continue
                            low = disp.lower()
                            if any(p in low for p in _SKIP_PAT):
                                continue
                            loc = _get("InstallLocation")
                            out.append({
                                "name": disp,
                                "keywords": _auto_keywords(disp),
                                "path": loc,
                                "publisher": _get("Publisher"),
                                "source": "已安装",
                            })
                    except OSError:
                        continue
        except OSError:
            continue
    return out


def _appdata_installed(user_dir: Path):
    """补绿色/便携软件：AppData\\Local 和 Roaming 下的软件目录。"""
    out = []
    for sub in ("Local", "Roaming"):
        base = user_dir / "AppData" / sub
        if not base.is_dir():
            continue
        try:
            for d in base.iterdir():
                if not d.is_dir():
                    continue
                nm = d.name
                if nm.lower() in _sys_segments():
                    continue
                if nm.startswith((".", "$")):
                    continue
                # 太短的目录名多半不是软件
                if len(nm) < 3:
                    continue
                out.append({
                    "name": nm,
                    "keywords": _auto_keywords(nm),
                    "path": str(d),
                    "publisher": "",
                    "source": "软件数据目录",
                })
        except OSError:
            continue
    return out


def _download_installers(user_dir: Path, max_items: int = 60):
    """桌面 / 下载 里的安装包（.exe / .msi），按修改时间新的在前。"""
    out = []
    seen = set()
    for folder in (user_dir / "Downloads", user_dir / "Desktop"):
        if not folder.is_dir():
            continue
        try:
            files = [p for p in folder.iterdir()
                     if p.suffix.lower() in (".exe", ".msi")]
        except OSError:
            continue
        files.sort(key=lambda p: -p.stat().st_mtime)
        for p in files:
            key = p.name.lower()
            if key in seen:
                continue
            seen.add(key)
            stem = re.sub(r"[_\-.\s]+", " ", p.stem).strip()
            out.append({
                "name": f"{stem}（安装包）",
                "keywords": _auto_keywords(p.stem),
                "path": str(p),
                "publisher": "",
                "source": "下载的安装包",
            })
            if len(out) >= max_items:
                return out
    return out


def list_installed_apps() -> list:
    """汇总：已安装 + 软件数据目录 + 下载的安装包，按名称排序、名称去重。"""
    user_dir = Path(os.path.expanduser("~"))
    all_apps = _reg_installed()
    all_apps += _appdata_installed(user_dir)
    all_apps += _download_installers(user_dir)

    # 名称去重（注册表优先），大小写不敏感
    seen, merged = set(), []
    for a in all_apps:
        key = a["name"].lower()
        if key in seen:
            continue
        seen.add(key)
        # 安装位置里也补一个目录关键词（比如 KuGou）
        if a.get("path"):
            for k in _keywords_from_sample(a["path"]):
                if k not in a["keywords"]:
                    a["keywords"].append(k)
        a["keywords"] = a["keywords"][:10]
        merged.append(a)
    # 排序：已安装的排最前（名字友好），数据目录居中，安装包最后；组内按名称
    order = {"已安装": 0, "软件数据目录": 1, "下载的安装包": 2}
    merged.sort(key=lambda a: (order.get(a["source"], 9), a["name"].lower()))
    return merged
