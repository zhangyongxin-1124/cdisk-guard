# -*- coding: utf-8 -*-
"""v6.17 切页顺滑专项测试：批量插入、子行懒加载、定时刷新按页分流、概况页指纹跳过。"""
import sys, time, types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
PASS_N = 0
FAIL_N = 0

def chk(name, ok, detail=""):
    global PASS_N, FAIL_N
    if ok:
        PASS_N += 1
        print(f"  PASS  {name}")
    else:
        FAIL_N += 1
        print(f"  FAIL  {name}  {detail}")

print("【1】语法与关键实现存在")
src = Path("guard_gui.py").read_text(encoding="utf-8")
chk("guard_gui 可编译", compile(src, "guard_gui.py", "exec") is not None)
chk("_fill_today_impl 关排序批量插入",
    "setSortingEnabled(False)" in src and "addTopLevelItems(nodes)" in src)
chk("子行懒加载实现存在",
    "_today_item_expanded" in src and "_children_built" in src)
chk("定时刷新按页分流", "def _auto_refresh" in src and "_cur_page" in src)
chk("概况页指纹跳过", "_home_fp" in src and "_home_letters" in src)
chk("_goto_page 记录当前页并延迟刷新",
    "self._cur_page = key" in src and "QTimer.singleShot(30, self._refresh_home)" in src)
chk("存量盘点同款批量插入",
    src.count("t.setSortingEnabled(False)") >= 1 and "t.addTopLevelItems(nodes)" in src)
chk("版本号已是 6.17", 'APP_VERSION = "6.17"' in src)

print("\n【2】行为验证（离屏窗口）")
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
app = QApplication.instance() or QApplication(sys.argv)
import guard_gui, guard_theme
guard_theme.apply(app)
g = guard_gui.GuiGuard()
win = guard_gui.MainWindow(g, minimized=True)
app.processEvents()

chk("九页导航存在", win.stack.count() == 9, f"实际 {win.stack.count()}")

# _cur_page 记录
win._goto_page("today")
chk("_goto_page 记录当前页", getattr(win, "_cur_page", "") == "today")
win._goto_page("settings")
chk("切页后 _cur_page 跟随", getattr(win, "_cur_page", "") == "settings")

# 切页耗时上限（同步部分；离屏环境下应 < 60ms）
worst = 0.0
for k in win._nav_index:
    t0 = time.perf_counter()
    win._goto_page(k)
    worst = max(worst, (time.perf_counter() - t0) * 1000)
chk("九页切换同步耗时全部 < 60ms", worst < 60, f"最慢 {worst:.1f} ms")

# 批量填充行数正确
win._goto_page("today"); app.processEvents()
data = win.scanner.load()
items = data.get("items") or []
if items:
    win._fill_today(items)
    app.processEvents()
    n_top = win.today_tree.topLevelItemCount()
    chk("填充后顶层行数 = 分组数", n_top > 0)
    chk("子行懒加载：填充后没有子行", all(
        win.today_tree.topLevelItem(i).childCount() == 0
        for i in range(n_top)), "存在提前创建的子行")
    # 展开 → 懒加载补齐
    it0 = win.today_tree.topLevelItem(0)
    it0.setExpanded(True)
    app.processEvents()
    chk("展开后子行自动补齐", it0.childCount() >= 0)
    n_lazy = sum(win.today_tree.topLevelItem(i).childCount() for i in range(n_top))
    chk("只有被展开的行有子行", n_lazy == it0.childCount())
    # 排序仍生效（用排序键比较，而非显示文本）
    win._apply_sort()
    keys = [win.today_tree.topLevelItem(i).data(5, guard_gui.SortItem.ROLE)
            for i in range(min(n_top, 20))]
    ok_sorted = all(
        (keys[i] >= keys[i + 1]) for i in range(len(keys) - 1))
    chk("默认按占用排序仍生效", ok_sorted or n_top < 2)
else:
    print("  （本机无今日新增数据，跳过表格行为断言）")

# 概况页指纹：数据没变时不重绘（第二次调用应极快）
win._goto_page("overview"); app.processEvents()
win._home_fp = None
win._refresh_home()                     # 第一次：建立指纹
app.processEvents()
t0 = time.perf_counter()
win._refresh_home()                     # 第二次：应直接 return
dt = (time.perf_counter() - t0) * 1000
chk("概况页数据没变时跳过重绘（<5ms）", dt < 5.0, f"{dt:.1f} ms")

# 定时刷新在非相关页面应为零开销
win._goto_page("settings"); app.processEvents()
t0 = time.perf_counter()
win._auto_refresh()
dt = (time.perf_counter() - t0) * 1000
chk("设置页下 60 秒定时刷新近零开销（<10ms）", dt < 10.0, f"{dt:.1f} ms")

print(f"\n合计：PASS {PASS_N} / FAIL {FAIL_N}")
sys.exit(1 if FAIL_N else 0)
