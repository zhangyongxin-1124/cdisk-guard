# -*- coding: utf-8 -*-
"""为 README 生成界面截图。

- 用 WA_DontShowOnScreen 真实渲染（解决 offscreen 无中文字体的问题）
- 「今日新增」页注入演示数据（虚构路径，无隐私）
- 运行前把 config.json 里的本机用户名替换为「用户名」，跑完恢复原文件
"""
import json
import os
import shutil
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
import guard_gui
import guard_theme as GT

OUT = os.path.join(BASE, "docs", "screenshots")
CFG = os.path.join(BASE, "config.json")
CFG_BAK = CFG + ".shotbak"

# ---- 演示数据（完全虚构，不指向任何真实文件）----
DEMO_USER = "C:\\Users\\演示\\"
NOW = time.time()


def _item(app, rel_dir, name, size_mb, hours_ago, is_dir=False):
    ts = NOW - hours_ago * 3600
    path = os.path.join(DEMO_USER, rel_dir, name)
    return {"path": path, "name": name, "app": app, "size": int(size_mb * 2 ** 20),
            "is_dir": is_dir, "ctime": ts, "mtime": ts}


DEMO_ITEMS = [
    _item("Steam", "AppData\\Local\\Steam", "游戏更新缓存", 3480.0, 1.2, is_dir=False),
    _item("百度网盘", "AppData\\Roaming\\BaiduNetdisk", "离线下载数据", 2150.0, 2.5, is_dir=False),
    _item("Chrome", "Downloads", "ChromeSetup.exe", 96.0, 3.1),
    _item("微信", "AppData\\Roaming\\Tencent\\WeChat\\Files", "聊天图片缓存", 823.0, 4.6, is_dir=False),
    _item("微信", "Documents\\WeChat Files", "视频号缓存", 512.0, 5.3, is_dir=False),
    _item("NVIDIA", "AppData\\Local\\NVIDIA", "驱动安装缓存", 641.0, 6.8, is_dir=False),
    _item("酷狗音乐", "AppData\\Local\\KuGou", "歌曲缓存", 458.0, 8.2, is_dir=False),
    _item("QQ", "Documents\\Tencent Files", "接收的文件", 182.0, 9.4),
    _item("迅雷", "Downloads", "迅雷下载", 1204.0, 10.7, is_dir=False),
    _item("剪映专业版", "AppData\\Local\\JianyingPro", "视频草稿", 376.0, 12.1, is_dir=False),
]


# ---- 存量盘点演示快照（虚构数据，跑完即删）----
import datetime
import guard_gui

DEMO_BASE_TS = datetime.datetime(2026, 10, 15).timestamp()
LEGACY_FILE = os.path.join(str(guard_gui.APP_DIR), "legacy_C.json")
INSTALL_FILE = os.path.join(str(guard_gui.APP_DIR), "install_time.json")


def _hist(old_mb, new_mb):
    """把 (老, 新) MB 摊进 2026-07 ~ 2026-11 的月份账本。"""
    h = {}
    if old_mb:
        for mk, share in [("2026-07", 0.4), ("2026-08", 0.35), ("2026-09", 0.25)]:
            h[mk] = int(old_mb * 2 ** 20 * share)
    if new_mb:
        for mk, share in [("2026-10", 0.5), ("2026-11", 0.5)]:
            h[mk] = int(new_mb * 2 ** 20 * share)
    return h


def _mk_ts(s):
    return datetime.datetime.strptime(s, "%Y-%m-%d").timestamp()


def _row(path, kind, total_mb, old_mb, count, oldest, newest):
    return {"path": path, "kind": kind, "size": int(total_mb * 2 ** 20),
            "count": count, "hist": _hist(old_mb, total_mb - old_mb),
            "oldest": _mk_ts(oldest), "newest": _mk_ts(newest)}


DEMO_ROWS = [
    _row("C:/Windows", "system", 12800, 12800, 860, "2026-07-03", "2026-09-18"),
    _row("C:/Program Files", "program", 6500, 6500, 520, "2026-07-05", "2026-09-20"),
    _row("C:/Program Files (x86)", "program", 2600, 2600, 380, "2026-07-06", "2026-08-30"),
    _row("C:/Users/演示/AppData/Roaming/Tencent/WeChat", "appdata", 5800, 3600, 980, "2026-07-08", "2026-11-01"),
    _row("C:/Users/演示/Videos", "media", 4200, 4200, 420, "2026-07-10", "2026-09-12"),
    _row("C:/Users/演示/Downloads", "download", 2400, 700, 960, "2026-07-12", "2026-11-01"),
    _row("C:/Users/演示/AppData/Local/Google/Chrome", "appdata", 1800, 1350, 990, "2026-07-09", "2026-11-01"),
    _row("C:/Users/演示/AppData/Local/Packages", "appdata", 1200, 1200, 880, "2026-07-04", "2026-10-08"),
    _row("C:/Users/演示/Desktop", "userfile", 900, 550, 420, "2026-07-15", "2026-11-01"),
    _row("C:/Windows/Temp", "cache", 600, 120, 900, "2026-07-20", "2026-11-01"),
]


def write_legacy_demo():
    with open(INSTALL_FILE, "w", encoding="utf-8") as f:
        json.dump({"ts": DEMO_BASE_TS, "source": "手动设置"}, f, ensure_ascii=False)
    with open(LEGACY_FILE, "w", encoding="utf-8") as f:
        json.dump({"ts": time.time(), "rows": DEMO_ROWS}, f, ensure_ascii=False)


def remove_legacy_demo():
    for p in (LEGACY_FILE, INSTALL_FILE):
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass


def sanitize_config():
    """config.json 里的本机用户名换成「用户名」，返回原文件是否存在。"""
    if not os.path.exists(CFG):
        return False
    shutil.copy2(CFG, CFG_BAK)
    try:
        with open(CFG, encoding="utf-8") as f:
            cfg = json.load(f)
        txt = json.dumps(cfg, ensure_ascii=False).replace("HIAPAD", "用户名")
        with open(CFG, "w", encoding="utf-8") as f:
            f.write(txt)
    except Exception as e:
        print("config 处理失败:", e)
    return True


def restore_config():
    if os.path.exists(CFG_BAK):
        shutil.move(CFG_BAK, CFG)


def main():
    os.makedirs(OUT, exist_ok=True)
    had_cfg = sanitize_config()
    write_legacy_demo()
    try:
        app = QApplication([])
        GT.apply(app)
        g = guard_gui.GuiGuard()
        win = guard_gui.MainWindow(g, minimized=False)
        # 关键：不真正显示窗口，但走完整的真实渲染（有字体、有主题）
        win.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        win.resize(1280, 800)
        win.show()
        app.processEvents()

        # 今日新增页：注入演示数据
        win._goto_page("today")
        app.processEvents()
        win._fill_today(DEMO_ITEMS)
        app.processEvents()

        # 软件迁移页：注入演示行（避免切页时触发真实软件扫描）
        from PySide6.QtWidgets import QTreeWidgetItem
        for nm, path, sz, src in [
                ("Steam", "C:\\Program Files (x86)\\Steam", "18.6 GB", "安装目录"),
                ("微信", "C:\\Program Files (x86)\\Tencent\\WeChat", "1.2 GB", "已安装"),
                ("剪映专业版", "C:\\Users\\演示\\AppData\\Local\\JianyingPro",
                 "860 MB", "软件数据目录")]:
            win.mig_tree.addTopLevelItem(QTreeWidgetItem([nm, path, sz, src]))
        win.mig_plan_view.setPlainText(
            "【Steam】18.6 GB / 23641 个文件\n"
            "风险等级：中\n"
            "  · 有 2 个快捷方式指向旧目录（可自动更新）\n"
            "  · 注册表里有 3 处路径引用（可自动替换）\n"
            "  · 没有检测到 Windows 服务\n\n"
            "将执行：\n"
            "  ① 复制 23641 个文件（18.6 GB）到 D:\\C盘守卫迁移\\Steam\n"
            "  ② 逐项校验文件数量一致后才动源目录\n"
            "  ③ 更新 2 个快捷方式的目标 / 起始位置\n"
            "  ④ 替换 3 处注册表路径（先备份，可还原）\n"
            "  ⑥ 原位置留 junction 透明链接，软件按老路径照常读写")
        win.lbl_mig_state.setText("风险等级：中。这只是模拟运行，尚未改动任何文件。")

        # 联动盘页：注入演示行（真实体检要枚举进程，截图没必要跑）
        win.link_tree.clear()
        for nm, sz, st, pos in [
                ("微信", "3.4 GB", "已建",
                 "D:\\C盘守卫联动盘\\Users\\演示\\AppData\\Roaming\\WeChat"),
                ("剪映专业版", "2.1 GB", "未建",
                 "C:\\Users\\演示\\AppData\\Local\\JianyingPro"),
                ("Chrome", "1.3 GB", "未建",
                 "C:\\Users\\演示\\AppData\\Local\\Google\\Chrome"),
                ("Steam", "860 MB", "不再提示",
                 "C:\\Program Files (x86)\\Steam\\steamapps")]:
            win.link_tree.addTopLevelItem(QTreeWidgetItem([nm, sz, st, pos]))
        win.lbl_link.setText(
            "共记录 4 个软件，已建联动盘 1 个，累计腾出约 3.4 GB。"
            "提示会自动出现在托盘，不用一直开这个页面。")

        for key in ["overview", "today", "clean", "guardlog", "quarantine",
                    "settings", "legacy", "migrate", "linkdisk"]:
            win._goto_page(key)
            if key == "legacy":       # 明确重刷，确保读到演示快照
                win._fill_legacy_overview()
                win._fill_legacy_rows()
            if key == "linkdisk":
                # 进页会自动重读档案（会带出本机真实路径），截图必须用演示数据
                win.link_tree.clear()
                for nm, sz, st, pos in [
                        ("微信", "3.4 GB", "已建",
                         "D:\\C盘守卫联动盘\\Users\\演示\\AppData\\Roaming\\WeChat"),
                        ("剪映专业版", "2.1 GB", "未建",
                         "C:\\Users\\演示\\AppData\\Local\\JianyingPro"),
                        ("Chrome", "1.3 GB", "未建",
                         "C:\\Users\\演示\\AppData\\Local\\Google\\Chrome"),
                        ("Steam", "860 MB", "不再提示",
                         "C:\\Program Files (x86)\\Steam\\steamapps")]:
                    win.link_tree.addTopLevelItem(QTreeWidgetItem([nm, sz, st, pos]))
                win.lbl_link.setText(
                    "共记录 4 个软件，已建联动盘 1 个，累计腾出约 3.4 GB。"
                    "提示会自动出现在托盘，不用一直开这个页面。")
                app.processEvents()
            for _ in range(8):
                app.processEvents()
            p = os.path.join(OUT, f"{key}.png")
            ok = win.grab().save(p)
            print(("saved " if ok else "FAIL  ") + key, os.path.getsize(p), "bytes")
        print("DONE")
    finally:
        remove_legacy_demo()
        if had_cfg:
            restore_config()


if __name__ == "__main__":
    main()
