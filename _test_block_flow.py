# -*- coding: utf-8 -*-
"""软件存储限制 —— 全流程集成测试（离屏）：规则命中 → 弹窗 → 用户选择 → 执行"""
import os
import sys
import time
import tempfile
import shutil

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication, QDialog
from PySide6.QtCore import Qt
import guard_gui
import guard_rules
from guard_bridge import bridge, IDYES

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

# 用临时目录装规则，别污染真实配置
_tmp = tempfile.mkdtemp(prefix="guard_block_")
_rules_file = os.path.join(_tmp, "app_rules.json")
guard_rules.rules.path = _rules_file
guard_rules.rules.rules = []
guard_rules.rules.save()

guard = guard_gui.GuiGuard()
win = guard_gui.MainWindow(guard, minimized=True)

print("== 1. 设置页出现限制入口 ==")
chk("有规则列表标签", hasattr(win, "lab_rules"))
chk("有内置来源开关", hasattr(win, "cb_builtin_sig"))
txt = win.lab_rules.text()
chk("初始提示为'还没有添加'", "还没有添加" in txt, txt)

print("\n== 2. 对话框可构建 ==")
d_block = guard_gui.BlockedAppDialog("标题", "内容", "酷狗音乐")
chk("限制弹窗可构建", d_block is not None)
chk("默认动作为 keep", d_block.action == guard_gui.BlockedAppDialog.KEEP)
d_add = guard_gui.AddRuleDialog("酷狗音乐", ["kugou"], sample_path=r"C:\x\KuGou\a.exe")
chk("添加弹窗可构建", d_add.ed_name.text() == "酷狗音乐")
nm, kw = d_add.values()
chk("取值正确", nm == "酷狗音乐" and kw == ["kugou"], (nm, kw))
d_rules = guard_gui.RulesDialog()
chk("管理弹窗可构建", d_rules.tree.columnCount() == 4)

print("\n== 3. 命中后走限制流程（模拟用户选：搬到别的盘）==")
# 建一个真实的待搬文件
src_dir = os.path.join(_tmp, "kugou_src")
os.makedirs(src_dir, exist_ok=True)
src_file = os.path.join(src_dir, "song.mp3")
with open(src_file, "wb") as f:
    f.write(b"0" * 2048)

from pathlib import Path
rule = guard_rules.rules.add("酷狗音乐", sample_path=src_file)
chk("规则已建立", bool(rule.get("id")))
chk("关键词含 kugou 或 酷狗",
    any(k in ("kugou", "酷狗音乐") for k in rule["keywords"]), rule["keywords"])

dest_root = _tmp + os.sep + "dest"          # 假装这是另一个盘
os.makedirs(dest_root, exist_ok=True)

# 模拟用户：弹窗里点"搬到别的盘"，选盘时返回 dest_root
calls = {"dialog": 0, "ask": 0, "info": []}
orig_dialog = bridge.dialog
orig_ask = bridge.ask
orig_info = bridge.info


def fake_dialog(payload):
    calls["dialog"] += 1
    if payload.get("type") == "app_block":
        return {"action": guard_gui.BlockedAppDialog.MOVE}
    if payload.get("type") in ("secondary", "pick_drive"):
        return {"action": guard_gui.DrivePickDialog.CHOICE_DRIVE, "root": dest_root}
    return {}


def fake_ask(title, text, buttons="ok", icon="info", default=""):
    calls["ask"] += 1
    return IDYES


def fake_info(title, text):
    calls["info"].append(text)


bridge.dialog = fake_dialog
bridge.ask = fake_ask
bridge.info = fake_info

items = [(Path(src_file), 2048)]
guard._handle_blocked(items, rule)

chk("弹出了限制提示", calls["dialog"] >= 1, calls["dialog"])
moved = os.path.join(dest_root, "kugou_src", "song.mp3")
chk("文件已搬到目标位置", os.path.exists(moved), os.listdir(dest_root))
chk("原位置已不存在", not os.path.exists(src_file))

print("\n== 4. 用户选「这次就放 C 盘」==")
src2 = os.path.join(src_dir, "b.mp3")
with open(src2, "wb") as f:
    f.write(b"1" * 1024)
calls["dialog"] = 0
guard._blocked_last.clear()                       # 清掉冷却，确保会弹窗


def fake_dialog_keep(payload):
    calls["dialog"] += 1
    return {"action": guard_gui.BlockedAppDialog.KEEP}


bridge.dialog = fake_dialog_keep
guard._handle_blocked([(Path(src2), 1024)], rule)
chk("弹出提示", calls["dialog"] == 1, calls["dialog"])
chk("文件原样保留", os.path.exists(src2))

print("\n== 5. 用户选「不下载/安装 → 删除」==")
src3 = os.path.join(src_dir, "c.mp3")
with open(src3, "wb") as f:
    f.write(b"2" * 1024)
calls["dialog"] = 0
guard._blocked_last.clear()
bridge.dialog = lambda p: {"action": guard_gui.BlockedAppDialog.DELETE}
guard._handle_blocked([(Path(src3), 1024)], rule)
chk("文件已删除", not os.path.exists(src3))

print("\n== 6. 用户选「解除限制」==")
rid = rule["id"]
guard._blocked_last.clear()
bridge.dialog = lambda p: {"action": guard_gui.BlockedAppDialog.UNBLOCK}
guard._handle_blocked([(Path(src2), 1024)], rule)
chk("规则已移除", guard_rules.rules.get(rid) is None)

print("\n== 7. 分流：未命中的走常规流程 ==")
calls["dialog"] = 0
normal_called = {"n": 0}
orig_normal = guard._handle_normal_write


def spy_normal(items):
    normal_called["n"] += 1


guard._handle_normal_write = spy_normal
other = os.path.join(_tmp, "other.txt")
with open(other, "wb") as f:
    f.write(b"3" * 512)
guard.handle_write([(Path(other), 512)])
chk("未命中不走限制弹窗", calls["dialog"] == 0, calls["dialog"])
chk("未命中交给常规流程", normal_called["n"] == 1, normal_called)
guard._handle_normal_write = orig_normal

print("\n== 8. 冷却机制：90 秒内同规则不重复刷屏 ==")
guard._blocked_last.clear()
calls["dialog"] = 0
bridge.dialog = fake_dialog_keep               # 选 keep，会被记进冷却
guard._handle_blocked([(Path(src2), 1024)], rule)
first = calls["dialog"]
guard._handle_blocked([(Path(src2), 1024)], rule)
second = calls["dialog"]
chk("第一次弹窗", first == 1, first)
chk("第二次被冷却拦下", second == 1, second)

print("\n== 9. 从电脑里选软件（不盲猜）==")
mock_apps = [
    {"name": "酷狗音乐", "keywords": ["kugou", "酷狗"],
     "path": r"C:\Program Files\KuGou", "publisher": "KuGou", "source": "已安装"},
    {"name": "Steam", "keywords": ["steam"],
     "path": r"D:\Steam", "publisher": "", "source": "已安装"},
    {"name": "SodaMusic", "keywords": ["sodamusic"],
     "path": r"F:\Soda", "publisher": "", "source": "软件数据目录"},
]
orig_list = guard_rules.list_installed_apps
guard_rules.list_installed_apps = lambda: mock_apps
try:
    dlg = guard_gui.AddRuleDialog(parent=win)
    dlg._load_apps()
    chk("列表已填充 3 项", dlg.lst_apps.count() == 3, dlg.lst_apps.count())
    # 已安装的排最前
    first_src = (dlg.lst_apps.item(0).data(Qt.UserRole) or {}).get("source")
    chk("已安装的排最前", first_src == "已安装", first_src)
    # 搜索过滤
    dlg.ed_search.setText("kugou")
    visible = [dlg.lst_apps.item(i).text() for i in range(dlg.lst_apps.count())
               if not dlg.lst_apps.item(i).isHidden()]
    chk("搜索过滤只剩 1 项", len(visible) == 1 and "酷狗" in visible[0], visible)
    dlg.ed_search.clear()
    # 选中后自动填名称和关键词
    dlg.lst_apps.setCurrentRow(0)
    chk("选中后名称自动填", "酷狗" in dlg.ed_name.text(), dlg.ed_name.text())
    chk("选中后关键词自动填 kugou", "kugou" in dlg.ed_kw.text().lower(), dlg.ed_kw.text())
    # values 能取到
    nm2, kw2 = dlg.values()
    chk("values 非空", bool(nm2) and len(kw2) > 0, (nm2, kw2))
finally:
    guard_rules.list_installed_apps = orig_list

print("\n== 10. 真实电脑枚举 ==")
t0 = time.time()
real = guard_rules.list_installed_apps()
dur = time.time() - t0
chk("真实枚举返回非空", len(real) > 0, len(real))
chk("枚举耗时 < 3s", dur < 3, f"{dur:.2f}s")
chk("每项字段齐全", all("name" in a and "keywords" in a and "source" in a for a in real))
chk("没有系统组件/补丁", not any("KB" in a["name"] for a in real),
    [a["name"] for a in real if "KB" in a["name"]][:3])

bridge.dialog = orig_dialog
bridge.ask = orig_ask
bridge.info = orig_info

print(f"\n合计：PASS {PASS} / FAIL {FAIL}")
shutil.rmtree(_tmp, ignore_errors=True)
sys.exit(1 if FAIL else 0)
