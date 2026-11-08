# -*- coding: utf-8 -*-
"""移动说明列 — GUI 离屏集成测试（不启动守护线程，只验界面与数据）"""
import os, sys, datetime
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
import guard_gui, guard_today

PASS = FAIL = 0

def chk(name, ok, extra=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} {extra}")

app = QApplication([])
guard = guard_gui.GuiGuard()
win = guard_gui.MainWindow(guard, minimized=True)

print("== 1. 表格结构与表头 ==")
tree = win.today_tree
chk("列数 = 9", tree.columnCount() == 9, tree.columnCount())
heads = [tree.headerItem().text(c) for c in range(tree.columnCount())]
print("       表头:", heads)
chk("第 8 列（最后）是移动说明", "移到别的盘会怎样" in heads[8], heads[8])
chk("第 3 列是新增日期", heads[3] == "新增日期", heads[3])
chk("第 4 列是新增时间", heads[4] == "新增时间", heads[4])
chk("第 7 列是文件数", heads[7] == "文件数", heads[7])
chk("图例标签已存在", hasattr(win, "lbl_move_legend"), "")
chk("图例内容非空", "移动说明" in win.lbl_move_legend.text(), "")

print("\n== 2. 造数据填入表格 ==")
now = datetime.datetime.now().timestamp()
items = [
    {"path": r"C:\Users\testuser\Downloads\电影.mp4", "name": "电影.mp4",
     "size": 1024 ** 3, "mtime": now, "ctime": now, "app": "未知来源", "is_dir": False},
    {"path": r"C:\Users\testuser\AppData\Roaming\WeChat Files\msg.db", "name": "msg.db",
     "size": 500 * 1024 ** 2, "mtime": now, "ctime": now, "app": "微信", "is_dir": False},
    {"path": r"C:\Windows\Temp\sys.tmp", "name": "sys.tmp",
     "size": 300 * 1024 ** 2, "mtime": now, "ctime": now, "app": "系统", "is_dir": False},
    {"path": r"F:\已有\mp3.mp3", "name": "mp3.mp3",
     "size": 10 * 1024 ** 2, "mtime": now, "ctime": now, "app": "未知来源", "is_dir": False},
]
win._fill_today(items)
n = tree.topLevelItemCount()
chk("父行数 = 4", n == 4, n)

rows = {}
for i in range(n):
    it = tree.topLevelItem(i)
    rows[it.text(1)] = it

print("\n== 3. 每一行的说明文本与颜色 ==")
for i in range(n):
    it = tree.topLevelItem(i)
    print(f"       {it.text(1)[:46]:<46} | {it.text(7):<8} | {it.text(8)}")
    chk(f"第 {i} 行有说明文本", bool(it.text(8).strip()), repr(it.text(8)))
    chk(f"第 {i} 行有 tooltip", len(it.toolTip(8)) > 20, len(it.toolTip(8)))
    chk(f"第 {i} 行有排序键", it.data(8, guard_gui.SortItem.ROLE) is not None, "")
    chk(f"第 {i} 行有颜色", it.foreground(8).color().isValid(), "")

print("\n== 4. 分级写对了 ==")
lv = {}
for i in range(n):
    it = tree.topLevelItem(i)
    lv[it.text(1)] = it.data(0, Qt.UserRole)["risk"]["level"]
chk("Downloads 目录 -> data", lv[r"C:\Users\testuser\Downloads"].startswith("data"),
    lv[r"C:\Users\testuser\Downloads"])
chk("微信 AppData -> appdata", lv[r"C:\Users\testuser\AppData\Roaming\WeChat Files"] == "appdata",
    lv[r"C:\Users\testuser\AppData\Roaming\WeChat Files"])
chk(r"Windows\Temp -> system", lv[r"C:\Windows\Temp"] == "system", lv[r"C:\Windows\Temp"])
chk(r"F:\已有 -> other_disk", lv[r"F:\已有"] == "other_disk", lv[r"F:\已有"])

print("\n== 5. 子行继承说明（v6.17 起子行懒加载，展开后才创建）==")
wechat = rows[r"C:\Users\testuser\AppData\Roaming\WeChat Files"]
chk("填充后子行未提前创建（懒加载）", wechat.childCount() == 0, wechat.childCount())
wechat.setExpanded(True)          # 展开 → 触发 itemExpanded 懒加载
app.processEvents()
chk("展开后子行补齐", wechat.childCount() >= 1, wechat.childCount())
c = wechat.child(0)
chk("子行也有说明文本", bool(c.text(8).strip()), repr(c.text(8)))
chk("子行说明与父行一致", c.text(8) == wechat.text(8), f"{c.text(8)} vs {wechat.text(8)}")

print("\n== 6. 按风险排序：危险的排最前 ==")
win.combo_sort.setCurrentIndex([win.combo_sort.itemData(i)
                                for i in range(win.combo_sort.count())].index("risk"))
win._apply_sort()
first = tree.topLevelItem(0)
chk("第一行是最危险的 Windows\\Temp", first.text(1) == r"C:\Windows\Temp", first.text(1))
chk("第一行 order 最大", first.data(8, guard_gui.SortItem.ROLE) ==
    max(tree.topLevelItem(i).data(8, guard_gui.SortItem.ROLE) for i in range(n)), "")

print("\n== 7. 全选会自动跳过红色危险项 ==")
win._check_all_today(True)
checked = [tree.topLevelItem(i) for i in range(n)
           if tree.topLevelItem(i).checkState(0) == Qt.CheckState.Checked]
chk(f"勾选数 = 3（跳过 Windows\\Temp）", len(checked) == 3, len(checked))
chk("危险项未被勾选",
    rows[r"C:\Windows\Temp"].checkState(0) == Qt.CheckState.Unchecked, "")
chk("底部给出了跳过提示", "自动跳过" in win.lbl_scan_state.text(),
    win.lbl_scan_state.text())

print("\n== 8. 日期 / 时间分两列显示 ==")
import re as _re
win._cur_drive = "C"
win._fill_today(items)
it0 = None
for i in range(tree.topLevelItemCount()):
    if tree.topLevelItem(i).text(1) == r"C:\Users\testuser\Downloads":
        it0 = tree.topLevelItem(i)
chk("日期列格式 2026.11.01", bool(_re.match(r"^\d{4}\.\d{2}\.\d{2}$", it0.text(3))),
    repr(it0.text(3)))
chk("时间列格式 18:09", bool(_re.match(r"^\d{2}:\d{2}( ~ \d{2}:\d{2})?$", it0.text(4))),
    repr(it0.text(4)))
it0.setExpanded(True)          # v6.17 懒加载：展开后才创建子行
app.processEvents()
c0 = it0.child(0)
chk("子行日期列格式正确", bool(_re.match(r"^\d{4}\.\d{2}\.\d{2}$", c0.text(3))),
    repr(c0.text(3)))
chk("子行时间列格式正确", bool(_re.match(r"^\d{2}:\d{2}$", c0.text(4))), repr(c0.text(4)))
chk("日期/时间列有排序键", it0.data(3, guard_gui.SortItem.ROLE) is not None and
    it0.data(4, guard_gui.SortItem.ROLE) is not None, "")

print("\n== 9. 切到非系统盘时说明列不报错 ==")
try:
    win._cur_drive = "F"
    win._fill_today([items[3]])
    chk("F 盘视图渲染正常", win.today_tree.topLevelItemCount() == 1, "")
    chk("F 盘项显示无需搬", "无需搬" in win.today_tree.topLevelItem(0).text(8),
        win.today_tree.topLevelItem(0).text(8))
except Exception as e:
    chk("F 盘视图渲染正常", False, f"Exception: {e}")

print(f"\n{'='*50}\n合计：PASS {PASS} / FAIL {FAIL}\n{'='*50}")
sys.exit(1 if FAIL else 0)
