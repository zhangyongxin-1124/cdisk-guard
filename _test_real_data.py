# -*- coding: utf-8 -*-
"""用真实 today_files.json 数据跑一遍：性能 + 分级效果"""
import os, sys, json, time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication
from collections import Counter
import guard_gui, guard_today

app = QApplication([])
g = guard_gui.GuiGuard()
win = guard_gui.MainWindow(g, minimized=True)

for drv in ("C", "F"):
    win._cur_drive = drv
    win.scanner.set_drive(drv)
    data = win.scanner.load()
    items = data.get("items", []) or []
    print(f"\n{'='*96}\n【{drv} 盘】真实条目数：{len(items)}")
    if not items:
        print("  （无数据）")
        continue

    t0 = time.time()
    rows = guard_today.TodayScanner.aggregate(items)
    t1 = time.time()
    cnt = Counter(r["risk"]["level"] for r in rows)
    print(f"  聚合耗时 {t1-t0:.3f}s → {len(rows)} 组")
    print(f"  分级统计：{dict(cnt)}")

    t2 = time.time()
    win._fill_today(items)
    t3 = time.time()
    print(f"  渲染耗时 {t3-t2:.3f}s → 表格 {win.today_tree.topLevelItemCount()} 行")
    print(f"  {'文件数':>8} | {'大小':>9} | {'移动说明':<20} | 位置")
    print("  " + "-" * 92)
    for i in range(min(18, win.today_tree.topLevelItemCount())):
        it = win.today_tree.topLevelItem(i)
        print(f"  {it.text(6):>8} | {it.text(4):>9} | {it.text(7):<20} | {it.text(1)[:56]}")

print(f"\n{'='*96}\n真实数据验证完成，无异常\n{'='*96}")
