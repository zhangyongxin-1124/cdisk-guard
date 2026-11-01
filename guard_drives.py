# -*- coding: utf-8 -*-
"""
磁盘枚举与容量信息
------------------
职责：把"这台电脑到底有几块盘、每块盘多大、还剩多少、健康吗"
     变成一份小白也能看懂的清单。

设计要点：
  · 用 Windows API 直接查询，不依赖任何第三方库
  · 自动排除：空的光驱、没有插入的存储卡读卡器、离线的网络盘
  · 每块盘给出：盘符、卷标（"系统" / "数据" 这类名字）、类型、容量、已用/可用、
    占用百分比、健康等级（良好 / 偏紧 / 告急）
"""
import ctypes
import os
import string
from pathlib import Path

# Windows 驱动器类型
DRIVE_UNKNOWN = 0
DRIVE_NO_ROOT_DIR = 1
DRIVE_REMOVABLE = 2      # U 盘 / 移动硬盘
DRIVE_FIXED = 3          # 本地硬盘（含 SSD）
DRIVE_REMOTE = 4         # 网络映射盘
DRIVE_CDROM = 5          # 光驱
DRIVE_RAMDISK = 6

k32 = ctypes.windll.kernel32

# 健康阈值（占用百分比）
WARN_PCT = 75.0
DANGER_PCT = 88.0


def _volume_label(root: str) -> str:
    """读取卷标，例如 C 盘的"系统"、数据盘的"资料"。取不到就返回空串。"""
    try:
        buf = ctypes.create_unicode_buffer(261)
        fs_buf = ctypes.create_unicode_buffer(261)
        serial = ctypes.c_ulong(0)
        maxlen = ctypes.c_ulong(0)
        flags = ctypes.c_ulong(0)
        ok = k32.GetVolumeInformationW(
            ctypes.c_wchar_p(root), buf, ctypes.sizeof(buf),
            ctypes.byref(serial), ctypes.byref(maxlen),
            ctypes.byref(flags), fs_buf, ctypes.sizeof(fs_buf))
        if ok:
            return (buf.value or "").strip()
    except Exception:
        pass
    return ""


def _space(root: str):
    """(总容量, 可用, 用户可用) 三元组，单位字节；失败返回 (0,0,0)。"""
    total = ctypes.c_ulonglong(0)
    avail = ctypes.c_ulonglong(0)
    free = ctypes.c_ulonglong(0)
    try:
        ok = k32.GetDiskFreeSpaceExW(
            ctypes.c_wchar_p(root),
            ctypes.byref(avail), ctypes.byref(total), ctypes.byref(free))
        if ok:
            return total.value, avail.value, free.value
    except Exception:
        pass
    return 0, 0, 0


def system_letter() -> str:
    """Windows 所在盘符，例如 'C'。"""
    return (Path(os.environ.get("SystemRoot", "C:\\Windows")).anchor
            .upper().rstrip(":\\") or "C")


def drive_type(letter: str) -> int:
    root = f"{letter}:\\"
    try:
        return int(k32.GetDriveTypeW(ctypes.c_wchar_p(root)))
    except Exception:
        return DRIVE_UNKNOWN


_TYPE_NAME = {
    DRIVE_FIXED: "本地硬盘",
    DRIVE_REMOVABLE: "U盘/移动硬盘",
    DRIVE_REMOTE: "网络盘",
    DRIVE_CDROM: "光盘",
    DRIVE_RAMDISK: "内存盘",
    DRIVE_NO_ROOT_DIR: "无介质",
    DRIVE_UNKNOWN: "未知",
}


def health_of(pct: float) -> dict:
    """根据占用百分比给出健康等级：文案 + 颜色（给界面用）。"""
    if pct >= DANGER_PCT:
        return {"level": "告急", "icon": "🔴", "color": "#c62828"}
    if pct >= WARN_PCT:
        return {"level": "偏紧", "icon": "🟡", "color": "#ef6c00"}
    return {"level": "良好", "icon": "🟢", "color": "#2f7d32"}


def list_drives(include_removable: bool = True,
                include_remote: bool = False) -> list:
    """枚举本机可用磁盘。

    返回按盘符排序的列表，每项是一个 dict：
      letter   'C'
      root     'C:\\'
      label    卷标（可能为空）
      type     DRIVE_* 常量
      type_name '本地硬盘' / 'U盘/移动硬盘' ...
      is_system 是否为系统盘（Windows 目录所在盘）
      total / free / used   字节
      pct     占用百分比（float）
      health  {"level","icon","color"}
      display 已拼好的一行摘要文本
    """
    system_root = Path(os.environ.get("SystemRoot", "C:\\Windows")).anchor.upper()
    out = []
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        try:
            dtype = drive_type(letter)
        except Exception:
            continue
        # 排除：未知、无根目录（没插卡的读卡器）
        if dtype in (DRIVE_UNKNOWN, DRIVE_NO_ROOT_DIR):
            continue
        if dtype == DRIVE_CDROM:
            # 光驱必须真的有盘才纳入
            if not os.path.exists(root):
                continue
        if dtype == DRIVE_REMOTE and not include_remote:
            continue
        if dtype == DRIVE_REMOVABLE and not include_removable:
            continue
        # 目录不存在（网络盘已断开等）跳过
        if not os.path.exists(root):
            continue

        total, avail, _ = _space(root)
        if total <= 0:                      # 光驱里没盘、或被卸载的卷
            if dtype == DRIVE_REMOVABLE and include_removable:
                # 读卡器空槽：盘符存在但无介质，跳过
                continue
            continue

        used = max(0, total - avail)
        pct = used / total * 100 if total else 0.0
        label = _volume_label(root)
        h = health_of(pct)
        info = {
            "letter": letter,
            "root": root,
            "label": label,
            "type": dtype,
            "type_name": _TYPE_NAME.get(dtype, "未知"),
            "is_system": root.upper() == system_root,
            "total": total,
            "free": avail,
            "used": used,
            "pct": pct,
            "health": h,
        }
        # 卷标叫"系统"时不要和系统标记重复显示
        tag = label if label and not (info["is_system"] and label == "系统") else ""
        info["tag"] = tag
        name = f"{letter} 盘"
        if tag:
            name += f"（{tag}）"
        if info["is_system"]:
            name += " · 系统"
        info["name"] = name
        out.append(info)
    out.sort(key=lambda d: d["letter"])
    return out


def get_drive(letter: str) -> dict:
    """取单个磁盘信息，找不到返回 None。"""
    letter = str(letter).strip().upper().rstrip(":\\")[:1]
    for d in list_drives():
        if d["letter"] == letter:
            return d
    return None


def system_drive_letter() -> str:
    return Path(os.environ.get("SystemRoot", "C:\\Windows")).anchor.upper().rstrip(":\\")


def brief(info: dict) -> str:
    """给下拉框用的一行摘要：C 盘 · 系统　剩 20.9 GB / 59.4 GB（占用 65%）"""
    gb = 2 ** 30
    parts = [f"{info['letter']} 盘"]
    tag = info.get("tag") or ""
    if tag:
        parts[0] += f" · {tag}"
    if info["is_system"]:
        parts[0] += " · 系统"
    return (f"{parts[0]}　剩 {info['free']/gb:.1f} GB / {info['total']/gb:.1f} GB"
            f"（占用 {info['pct']:.0f}%）")
