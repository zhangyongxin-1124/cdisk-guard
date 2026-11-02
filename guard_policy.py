# -*- coding: utf-8 -*-
"""
guard_policy.py — 可选监控盘 + 拦截白名单（v6.14 新增）
=====================================================
把「守卫监听哪些盘」「哪些写入不打扰你」从写死在代码里，变成用户自己
在设置页里勾、自己填的两组策略。

设计原则：
  1. **完全向后兼容**：新配置项缺省时行为与 v6.13 一模一样（只监控系统盘），
     老 config.json 不需要迁移、不会报错。
  2. **换机自动消毒**：配置里勾选的盘符在另一台电脑上可能根本不存在，
     启动时只保留本机真实存在的盘；一个都不剩就回落到系统盘，
     绝不因为配置来自别的电脑就让拦截彻底失效。
  3. **U 盘动态纳管**：勾选「监控可移动盘」后，插入的 U 盘自动加监听、
     拔出自动摘监听，不需要重启程序。
  4. **白名单只做减法**：白名单命中只是「不弹窗」，不改变文件、不删数据；
     系统目录排除等原有安全底线仍然生效。

纯逻辑模块，不 import PySide6；所有函数都允许显式传入 available 盘符列表，
方便在没有真实磁盘的测试环境里做断言。
"""
from __future__ import annotations

import os
import re
import string
import threading
from pathlib import Path

# ------------------------------------------------------------
# 默认策略（写进 config.json 的四个键）
# ------------------------------------------------------------
DEFAULT_POLICY = {
    # 用户勾选要拦截写入的盘符（大写单字母列表）。
    # 空列表 = 仅监控系统盘（与 v6.13 及之前完全一致）。
    "监控盘": [],
    # U 盘 / 移动硬盘插入后自动纳入监控；拔出自动摘除。
    "监控可移动盘": False,
    # 这些目录下的写入一律不询问（目录前缀或路径关键词，大小写不敏感）。
    "拦截白名单": [],
    # 由这些进程写入时不询问（进程名，如 "OneDrive.exe"）。
    "白名单进程": [],
}

_LETTER_RE = re.compile(r"^[A-Za-z]$")


def system_letter() -> str:
    """系统盘盘符（用 %SystemDrive% 动态取，任何电脑都对）。"""
    return (os.environ.get("SystemDrive") or "C:").rstrip("\\/: ").upper()[:1] or "C"


def system_root() -> str:
    return system_letter() + ":\\"


def available_letters(include_removable: bool = True) -> list:
    """本机当前真实存在的盘符（优先用 guard_drives，异常时退化为 A-Z 扫描）。"""
    try:
        import guard_drives
        return [str(d["letter"]).upper()
                for d in guard_drives.list_drives(include_removable=include_removable)]
    except Exception:
        out = []
        for ch in string.ascii_uppercase:
            if os.path.isdir(f"{ch}:\\"):
                out.append(ch)
        return out


def removable_letters() -> list:
    """当前插入的可移动盘（U 盘 / 移动硬盘）盘符。"""
    try:
        import guard_drives
        return [str(d["letter"]).upper() for d in guard_drives.list_drives()
                if d.get("type") == guard_drives.DRIVE_REMOVABLE]
    except Exception:
        return []


def _clean_letters(raw) -> list:
    """把用户配置里的值规整成大写单字母列表（去掉脏数据）。"""
    if not isinstance(raw, (list, tuple, set)):
        return []
    out = []
    for x in raw:
        s = str(x).strip().upper().rstrip("\\/: ")
        if _LETTER_RE.match(s) and s not in out:
            out.append(s)
    return out


def sanitize_drives(cfg, available=None) -> tuple:
    """消毒「监控盘」：去掉本机不存在的盘符、按字母排序、必要时回落系统盘。

    返回 (有效盘符列表, 是否与原配置不同)。
    available 传 None 时读本机真实磁盘；测试可传入模拟列表。
    """
    cfg = cfg or {}
    raw = _clean_letters(cfg.get("监控盘"))
    avail = set(str(x).upper()[:1] for x in
                (available if available is not None else available_letters()))
    sys_l = system_letter()
    if sys_l not in avail:            # 系统盘一定在，环境异常时兜底
        avail.add(sys_l)

    # 空列表 = 兼容旧行为：只监控系统盘
    wanted = raw or [sys_l]
    kept = [l for l in wanted if l in avail]

    # 勾选了「监控可移动盘」→ 把当前插着的 U 盘一并纳入
    if cfg.get("监控可移动盘"):
        for l in (removable_letters() if available is None else []):
            if l in avail and l not in kept:
                kept.append(l)

    kept = sorted(set(kept))
    if not kept:                     # 一个有效盘都不剩（换机带过来的旧配置）
        kept = [sys_l]
    return kept, kept != raw


def roots(cfg, available=None) -> list:
    """返回要交给 TreeWatcher 的监控根列表，如 ['C:\\\\', 'D:\\\\']。"""
    letters, _ = sanitize_drives(cfg, available)
    return [f"{l}:\\" for l in letters]


def merge_defaults(cfg) -> bool:
    """把 DEFAULT_POLICY 的键补进配置（升级后自动补全），返回是否发生改动。"""
    cfg = cfg or {}
    changed = False
    for k, v in DEFAULT_POLICY.items():
        if k not in cfg:
            cfg[k] = list(v) if isinstance(v, list) else v
            changed = True
    return changed


def _norm_path(p) -> str:
    s = str(p or "").strip().lower().replace("/", "\\")
    s = re.sub(r"\\+", lambda _m: "\\", s)   # 用户可能粘进来 D://Games 或 D:\\Games
    return s.rstrip("\\")


def is_whitelisted(path, cfg, proc_name: str = "") -> bool:
    """路径白名单 / 进程白名单命中判定（大小写不敏感）。

    规则写法：
      · 完整目录（如 `D:\\Games`）→ 该目录及其子目录下所有写入都不问
      · 路径片段（如 `\\SteamLibrary\\`）→ 路径里包含即不问
      · 进程名（如 `OneDrive.exe`）→ 由该进程写入时不问
    """
    cfg = cfg or {}
    pl = _norm_path(path)
    if pl:
        for raw in cfg.get("拦截白名单") or []:
            w = _norm_path(raw)
            if not w:
                continue
            if pl.startswith(w) or w in pl:
                return True
    if proc_name:
        pn = str(proc_name).strip().lower()
        for raw in cfg.get("白名单进程") or []:
            w = str(raw).strip().lower()
            if w and (pn == w or pn.endswith("\\" + w)):
                return True
    return False


class Policy:
    """轻量封装：持有配置引用，供引擎主循环反复查询。

    cfg 是 dict 引用（guard.config），因此设置页保存后无需重建对象。
    """

    def __init__(self, cfg):
        self.cfg = cfg if cfg is not None else {}
        merge_defaults(self.cfg)
        self._lock = threading.Lock()
        self._letters = []
        self.refresh()

    def refresh(self, available=None) -> list:
        """重新计算当前应监控的盘符，返回盘符列表。"""
        letters, _ = sanitize_drives(self.cfg, available)
        with self._lock:
            self._letters = letters
        return letters

    @property
    def letters(self) -> list:
        with self._lock:
            return list(self._letters)

    def roots(self, available=None) -> list:
        return [f"{l}:\\" for l in self.refresh(available)]

    def is_whitelisted(self, path, proc_name: str = "") -> bool:
        return is_whitelisted(path, self.cfg, proc_name)

    def summary(self) -> str:
        return "、".join(f"{l} 盘" for l in self.letters) or "（无）"
