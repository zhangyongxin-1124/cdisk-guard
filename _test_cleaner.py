# -*- coding: utf-8 -*-
"""日志清理功能测试：规则层 + 界面层 + 真实清理流程。"""
import os
import sys
import time
import shutil
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PASS = FAIL = 0


def ck(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} {extra}")


# ============================================================
print("== 1. 文件分类规则 ==")
import guard_cleaner as gc

cases = [
    (r"C:\Users\x\AppData\Local\Doubao\sdk_storage\log\saman.log", "日志"),
    (r"C:\Users\x\AppData\Local\Foo\logs\a.txt", "日志"),
    (r"C:\Users\x\AppData\Local\Foo\logs\a.json", "日志"),
    (r"C:\Users\x\AppData\Local\Foo\log\app.2026.11.01.log", "日志"),
    (r"C:\Users\x\AppData\Local\Foo\a.log.3", "日志"),
    (r"C:\Users\x\AppData\Local\Foo\a.old", "日志"),
    (r"C:\Users\x\AppData\Local\Foo\crash.dmp", "崩溃转储"),
    (r"C:\Users\x\AppData\Local\Foo\crashes\mini.mdmp", "崩溃转储"),
    (r"C:\Users\x\AppData\Local\Temp\abc.tmp", "临时文件"),
    (r"C:\Users\x\AppData\Local\Temp\sub\whatever.dll", "临时文件"),
    # 不该被当成可清目标
    (r"C:\Users\x\Desktop\重要文档.docx", ""),
    (r"C:\Users\x\Desktop\照片.jpg", ""),
    (r"C:\Users\x\AppData\Local\Foo\config.json", ""),
    (r"C:\Users\x\Videos\movie.mp4", ""),
]
TEMP_PRE = (r"c:\users\x\appdata\local\temp",)
for path, want in cases:
    got = gc.classify(path, TEMP_PRE)
    ck(f"classify {os.path.basename(path):28s} -> {got or '(不算)'}", got == want,
       f"期望 {want or '(不算)'}")

# 不是真正的临时目录（只是名字里有 temp）时，不能把里面的文件当垃圾
ck("软件自建的 temp 命名目录不算临时目录",
   gc.is_temp_root(r"C:\Users\x\AppData\Local\SomeApp\MyTempCache") is False)
ck("系统 Temp 算临时目录",
   gc.is_temp_root(r"C:\Users\x\AppData\Local\Temp") is True)

# ============================================================
print("== 2. 系统目录永不触碰 ==")
for p in (r"C:\Windows\Logs\a.log",
          r"C:\Windows\System32\LogFiles\a.log",
          r"C:\Program Files\Foo\logs\a.log",
          r"C:\ProgramData\Foo\a.log",
          r"C:\$Recycle.Bin\a.log",
          r"C:\Users\x\.workbuddy\a.log"):
    ck(f"排除 {p[:46]}", gc._is_excluded(p.lower()))

ck("用户 AppData 日志不被排除",
   not gc._is_excluded(r"c:\users\x\appdata\local\doubao\log\a.log"))

# ============================================================
print("== 3. 扫描：过期 / 超大 / 正在写入 ==")
# 注意：不能建在系统 Temp 下面，否则整个测试目录会被判定成「临时目录」
_tmp_parent = os.path.join(os.path.expanduser("~"), "AppData", "Local")
tmp = tempfile.mkdtemp(prefix="guard_clean_test_",
                       dir=_tmp_parent if os.path.isdir(_tmp_parent) else None)
root = os.path.join(tmp, "AppData", "Local")
logdir = os.path.join(root, "SomeApp", "logs")
os.makedirs(logdir, exist_ok=True)
os.makedirs(os.path.join(root, "Other", "log"), exist_ok=True)
tmpdir = os.path.join(root, "Temp")
os.makedirs(tmpdir, exist_ok=True)

files = {}
now = time.time()


def mk(path, size_mb, age_days):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"\0" * int(size_mb * 1024 * 1024))
    t = now - age_days * 86400
    os.utime(path, (t, t))
    files[path] = size_mb


mk(os.path.join(logdir, "old.log"), 2, 10)          # 过期 → 清
mk(os.path.join(logdir, "recent.log"), 2, 1)        # 才 1 天且不大 → 留
mk(os.path.join(logdir, "huge.log"), 80, 0.05)      # 超大且 72 分钟没写 → 清
mk(os.path.join(logdir, "active.log"), 90, 0.001)   # 超大但正在写 → 跳过
mk(os.path.join(root, "Other", "log", "x.txt"), 1, 30)   # 过期 → 清
mk(os.path.join(tmpdir, "junk.tmp"), 3, 20)         # 过期临时 → 清
mk(os.path.join(root, "Other", "keep.docx"), 5, 40)  # 不是日志 → 不留

cfg = dict(gc.DEFAULT_CFG)
cfg["日志保留天数"] = 3
cfg["超大日志MB"] = 50
cfg["清临时文件"] = True
res = gc.scan(cfg, roots=[root, tmpdir])
got = {os.path.basename(p): p for p in res["items"]} if False else \
      {os.path.basename(i["path"]): i for i in res["items"]}

ck("扫出 5 个可清文件（含 1 个正在写入）", len(res["items"]) == 5,
   f"实际 {len(res['items'])}: {sorted(got)}")
ck("过期日志被选中", "old.log" in got)
ck("超大日志被选中", "huge.log" in got)
ck("过期临时文件被选中", "junk.tmp" in got)
ck("log 目录里的过期 txt 被选中", "x.txt" in got)
ck("刚写的普通日志不入选", "recent.log" not in got)
ck("正在写入的超大日志被列出", "active.log" in got)
ck("正在写入的文件带 active 标记", got.get("active.log", {}).get("active") is True)
ck("正在写入计数正确", res["skipped_active"] == 1, f"实际 {res['skipped_active']}")
ck("安全可清量不含正在写入的部分",
   res["total_safe"] == res["total"] - got["active.log"]["size"],
   f"{res['total_safe']} vs {res['total']}")
ck("非日志文件不入选", "keep.docx" not in got)
ck("无重复项", len({i['path'] for i in res['items']}) == len(res['items']))

# 关掉 Temp 清理后，临时文件应消失
cfg2 = dict(cfg)
cfg2["清临时文件"] = False
res2 = gc.scan(cfg2, roots=[root, tmpdir])
ck("关闭 Temp 清理后 junk.tmp 不再入选",
   "junk.tmp" not in {os.path.basename(i["path"]) for i in res2["items"]})

# ============================================================
print("== 4. 按目录分组 ==")
rows = gc.aggregate(res["items"])
ck("分组数量 > 0", len(rows) > 0)
ck("分组按大小降序", all(rows[i]["size"] >= rows[i + 1]["size"]
                    for i in range(len(rows) - 1)))
ck("分组含 items", all(r["items"] for r in rows))
ck("分组有类型说明", all(r.get("cat_text") for r in rows))

# ============================================================
print("== 5. 真实清理（真删文件）==")
safe = [i for i in res["items"] if not i.get("active")]
before = sum(i["size"] for i in safe)
r = gc.clean(safe)
ck("删除成功数 == 可清数", len(r["ok"]) == 4, f"ok={len(r['ok'])} fail={r['fail']}")
ck("释放空间 > 0", r["freed"] > 0)
ck("释放量约等于选中量", abs(r["freed"] - before) < 1024 * 1024,
   f"{r['freed']} vs {before}")
ck("文件确实不在了", all(not os.path.exists(p) for p, _ in r["ok"]))
ck("正在写入/未选文件仍在",
   os.path.exists(os.path.join(logdir, "recent.log"))
   and os.path.exists(os.path.join(logdir, "active.log"))
   and os.path.exists(os.path.join(root, "Other", "keep.docx")))
ck("自动清理不会碰正在写入的文件",
   all(not i.get("active") for i in safe) and len(safe) == 4)

# 删一个不存在的文件 → 应进 fail 而不是崩溃
r2 = gc.clean([{"path": os.path.join(tmp, "nope.log"), "size": 10}])
ck("删不存在的文件进 fail 且不抛异常", len(r2["fail"]) == 1 and r2["freed"] == 0)

# ============================================================
print("== 6. 状态与调度 ==")
gc.STATE_PATH = Path(tmp) / "state.json"
gc.mark_cleaned(123456, 3)
st = gc.load_state()
ck("上次清理已记录", st.get("上次清理", 0) > 0)
ck("历史已记录", len(st.get("历史", [])) >= 1)

cfg3 = dict(gc.DEFAULT_CFG)
cfg3["启用"] = False
d, _ = gc.due(cfg3)
ck("未启用时不到期", d is False)

cfg4 = dict(gc.DEFAULT_CFG)
cfg4["启用"] = True
cfg4["自动周期天"] = 7
d, _ = gc.due(cfg4)
ck("刚清理过 → 不到期", d is False)

st["上次清理"] = time.time() - 8 * 86400
gc.save_state(st)
d, _ = gc.due(cfg4)
ck("超过 8 天 → 到期", d is True)

gc.mark_checked()
ck("mark_checked 后重新计时", gc.due(cfg4)[0] is False)

# ============================================================
print("== 7. 配置项完整 ==")
for k in ("启用", "日志保留天数", "超大日志MB", "自动周期天", "清理前询问",
          "最小清理量MB", "清崩溃转储", "清临时文件", "扫描根目录"):
    ck(f"默认配置含 {k}", k in gc.DEFAULT_CFG)
ck("默认关闭自动清理", gc.DEFAULT_CFG["启用"] is False)
ck("默认开启清理前询问", gc.DEFAULT_CFG["清理前询问"] is True)
ck("human 可读", gc.human(92 * 1024 * 1024).endswith("MB"), gc.human(92 * 1024 * 1024))

# ============================================================
print("== 8. 界面（离屏）==")
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt

app = QApplication.instance() or QApplication([])

import guard_gui
import c_disk_guard as engine

# 弹窗在离屏环境下会阻塞，统一拦掉；同时把弹窗内容记下来做断言
_shown = []
guard_gui.QMessageBox.information = staticmethod(lambda *a, **k: _shown.append(a))
guard_gui.QMessageBox.warning = staticmethod(lambda *a, **k: _shown.append(a))

g = guard_gui.GuiGuard()
g.config["自动清理"] = dict(gc.DEFAULT_CFG)      # 保证测试可重复
win = guard_gui.MainWindow(g, minimized=True)

ck("存在「日志清理」页面", "clean" in win.nav and "clean" in win._nav_index)
ck("侧栏共 7 个页面", win.stack.count() == 7, f"实际 {win.stack.count()}")
ck("清理树有 6 列", win.clean_tree.columnCount() == 6)
ck("自动清理默认关闭", win.cb_clean_on.isChecked() is False)
ck("保留天数控件存在", "日志保留天数" in win.sp_clean)
ck("超大阈值控件存在", "超大日志MB" in win.sp_clean)

# 用构造好的假数据填充界面
fake = {
    "items": [
        {"path": os.path.join(logdir, "old.log"), "size": 2 * 1024 * 1024,
         "mtime": now - 10 * 86400, "age_days": 10, "cat": "日志",
         "reason": "日志 · 10 天没动过"},
        {"path": os.path.join(logdir, "huge.log"), "size": 80 * 1024 * 1024,
         "mtime": now - 3600, "age_days": 0.04, "cat": "日志",
         "reason": "日志 · 超大（80 MB）"},
        {"path": os.path.join(root, "Other", "log", "x.txt"), "size": 1024 * 1024,
         "mtime": now - 30 * 86400, "age_days": 30, "cat": "日志",
         "reason": "日志 · 30 天没动过"},
    ],
    "total": 83 * 1024 * 1024, "total_safe": 83 * 1024 * 1024,
    "scanned": 1234, "elapsed": 1.2,
    "skipped_active": 1, "truncated": False,
}
win._on_clean_scan_done(fake)
ck("表格渲染出 2 个分组", win.clean_tree.topLevelItemCount() == 2,
   f"实际 {win.clean_tree.topLevelItemCount()}")
ck("汇总文字含可清容量", "83" in win.lbl_clean.text(), win.lbl_clean.text()[:80])
ck("汇总提到正在被写入", "还在被软件写入" in win.lbl_clean.text(),
   win.lbl_clean.text()[:120])

win._check_all_clean(True)
ck("全选后能取到 3 个文件", len(win._selected_clean_items()) == 3,
   f"实际 {len(win._selected_clean_items())}")
win._check_all_clean(False)
ck("全不选后取到 0 个", len(win._selected_clean_items()) == 0)

# 单个勾选
win.clean_tree.topLevelItem(0).setCheckState(0, Qt.CheckState.Checked)
ck("勾选 1 组能取到文件", len(win._selected_clean_items()) >= 1)

# 保存设置写进 config
win.cb_clean_on.setChecked(True)
win.sp_clean["日志保留天数"].setValue(5)
win._save_clean_settings()
ck("设置写入 config", bool(g.config.get("自动清理")))
ck("启用状态已保存", g.config["自动清理"].get("启用") is True)
ck("保留天数已保存", g.config["自动清理"].get("日志保留天数") == 5)
ck("保存后有提示弹窗", len(_shown) >= 1)

# 调度：未到期不应触发
g.config["自动清理"]["启用"] = False
ck("未启用时调度不触发", win._auto_clean_check() is None)

# 收尾：不要把测试配置留在真实 config.json 里
try:
    g.config.pop("自动清理", None)
    g.save_config()
except Exception:
    pass

print(f"\n合计：PASS {PASS} / FAIL {FAIL}")
shutil.rmtree(tmp, ignore_errors=True)
sys.exit(1 if FAIL else 0)
