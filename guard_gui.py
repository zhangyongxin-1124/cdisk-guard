# -*- coding: utf-8 -*-
"""
guard_gui.py — C 盘守卫 · 桌面版 v6.7
======================================
在原有守护引擎之上提供真正的桌面软件体验：

  1. 图形主界面 —— C 盘概况 / 今日新增 / 守卫记录 / 隔离区 / 设置
  2. 关闭（×）不退出 —— 缩到系统托盘，后台继续守护；托盘右键才真正退出
  3. 今日新增监测 —— 今天 C 盘多了什么、来自哪个软件、占多少空间，
     勾选后一键移动到其他盘
  4. 写入询问改版 ——
       【是】  仍然保存到 C 盘
       【否】  弹出二级选择：选其他本地盘 / 浏览文件夹 /
               取消（不安装、不下载、不存储 —— 立即删除刚写入的文件）

原有能力全部保留：聚合监控、删除保护（隔离区）、安装哨兵、空间告警、
周报仪表盘、开机自启、单实例互斥。
"""

import os
import sys
import csv
import json
import time
import shutil
import subprocess
import threading
import datetime
from pathlib import Path

from PySide6.QtCore import (Qt, QTimer, Signal, QObject, QSize, QRectF, QPointF,
                            QDate)
from PySide6.QtGui import QIcon, QAction, QColor, QFont, QPixmap, QPainter, QPen, QFontMetrics
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QPushButton, QVBoxLayout,
    QHBoxLayout, QTabWidget, QTreeWidget, QTreeWidgetItem, QComboBox,
    QCheckBox, QSpinBox, QGroupBox, QProgressBar,
    QMessageBox, QDialog, QListWidget, QListWidgetItem, QFileDialog,
    QSystemTrayIcon, QMenu, QHeaderView, QAbstractItemView, QStyle,
    QGridLayout, QLineEdit, QStackedWidget, QFrame, QScrollArea, QSizePolicy,
    QDateEdit, QTableWidget, QTableWidgetItem,
)

APP_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
sys.path.insert(0, str(APP_DIR))

import c_disk_guard as engine
import guard_today
import guard_quarantine
import guard_dashboard
import guard_growth
import guard_drives
import guard_rules
import guard_cleaner
import guard_legacy
import guard_theme as GT
from guard_bridge import bridge, IDYES, IDNO, IDCANCEL, IDOK

APP_NAME = "C盘守卫"
ICON_PATH = APP_DIR / "guard_icon.ico"
MB = engine  # 常量速记

# ============================================================
# Qt 侧弹窗桥（主线程执行）
# ============================================================

_BTN_MAP = {"yesnocancel": (IDYES, IDNO, IDCANCEL),
            "yesno": (IDYES, IDNO),
            "okcancel": (IDOK, IDCANCEL),
            "ok": (IDOK,)}
_BTN_TEXT = {IDYES: "是", IDNO: "否", IDCANCEL: "取消", IDOK: "确定"}
_ICON_MAP = {"info": QMessageBox.Icon.Information,
             "warning": QMessageBox.Icon.Warning,
             "question": QMessageBox.Icon.Question,
             "error": QMessageBox.Icon.Critical}


class BridgeUI(QObject):
    """轮询桥队列，把引擎线程的问答请求 / 自建对话框在主线程执行。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(120)

    def _poll(self):
        # 1) 通用问答 / 对话框请求（对话框由主线程构建，避免跨线程创建 QWidget）
        while True:
            try:
                req = bridge.requests.get_nowait()
            except Exception:
                break
            try:
                if req["kind"] == "ask":
                    req["result"] = self._ask(req["payload"])
                elif req["kind"] == "dialog":
                    req["result"] = self._dialog(req["payload"])
            except Exception:
                req["result"] = None
            req["event"].set()

    def ask_now(self, p: dict) -> int:
        return self._ask(p)

    def _dialog(self, p: dict) -> dict:
        """在主线程构建并执行自建对话框，返回选择结果。"""
        t = p.get("type")
        if t == "ask_write":
            dlg = AskWriteDialog(p["title"], p["text"])
            dlg.exec()
            return {"choice": dlg.result_id}
        if t in ("secondary", "pick_drive"):
            dlg = DrivePickDialog(p["title"], p["hint"])
            dlg.exec()
            return {"action": dlg.choice, "root": dlg.picked_root}
        if t == "root_folder":
            dlg = RootFolderDialog(p["title"], p["text"])
            dlg.exec()
            return {"choice": dlg.result_id}
        if t == "app_block":
            dlg = BlockedAppDialog(p["title"], p["text"], p.get("rule_name", ""))
            dlg.exec()
            return {"action": dlg.action}
        return {}

    def _ask(self, p: dict) -> int:
        box = QMessageBox()
        box.setWindowTitle(p["title"])
        box.setText(p["text"].replace("\\n", "\n") if "\\n" in p["text"] else p["text"])
        box.setIcon(_ICON_MAP.get(p.get("icon", "info"), QMessageBox.Icon.Information))
        ids = _BTN_MAP.get(p.get("buttons", "ok"), (IDOK,))
        buttons = []
        for bid in ids:
            b = box.addButton(_BTN_TEXT.get(bid, "确定"),
                              QMessageBox.ButtonRole.ActionRole)
            buttons.append((b, bid))
            if p.get("default") == _BTN_TEXT.get(bid, ""):
                box.setDefaultButton(b)
        if not box.defaultButton() and buttons:
            box.setDefaultButton(buttons[-1][0])
        box.exec()
        clicked = box.clickedButton()
        for b, bid in buttons:
            if b is clicked:
                return bid
        return ids[-1]


def qt_alert_box(title, text, style=0, beep=0):
    """替换 engine.alert_box：把原有所有系统弹窗变成 Qt 窗口。"""
    if style & engine.MB_YESNOCANCEL == engine.MB_YESNOCANCEL:
        buttons = "yesnocancel"
    elif style & engine.MB_YESNO == engine.MB_YESNO:
        buttons = "yesno"
    elif style & engine.MB_OKCANCEL == engine.MB_OKCANCEL:
        buttons = "okcancel"
    else:
        buttons = "ok"
    icon = "info"
    if style & engine.MB_ICONWARNING:
        icon = "warning"
    elif style & engine.MB_ICONQUESTION:
        icon = "question"
    elif style & engine.MB_ICONERROR:
        icon = "error"
    r = bridge.ask(title, text, buttons, icon)
    if buttons == "okcancel":
        return IDOK if r == IDOK else IDCANCEL
    return r


# ============================================================
# 二级选择对话框
# ============================================================

class AskWriteDialog(QDialog):
    """写入询问（改版）：是 = 仍保存C盘；否 = 进入二级选择。"""

    def __init__(self, title, text, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.resize(560, 420)
        self.result_id = IDCANCEL
        lay = QVBoxLayout(self)
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lbl.setStyleSheet("font-size:13px; line-height:150%;")
        lay.addWidget(lbl)
        lay.addSpacing(8)
        row = QHBoxLayout()
        yes = QPushButton("是：仍然保存到 C 盘")
        yes.setMinimumHeight(34)
        yes.setStyleSheet("background:#2f7d32; color:white; font-weight:bold;")
        no = QPushButton("否：换个盘存 / 不存储")
        no.setMinimumHeight(34)
        no.setStyleSheet("background:#c62828; color:white; font-weight:bold;")
        yes.clicked.connect(lambda: self._done(IDYES))
        no.clicked.connect(lambda: self._done(IDNO))
        row.addWidget(yes)
        row.addWidget(no)
        lay.addLayout(row)

    def _done(self, rid):
        self.result_id = rid
        self.accept()


class RootFolderDialog(QDialog):
    """用户根目录新文件夹拦截：保留 / 搬移并创建链接 / 稍后再问。"""

    def __init__(self, title, text, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.resize(560, 380)
        self.result_id = IDCANCEL
        lay = QVBoxLayout(self)
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lbl.setStyleSheet("font-size:13px; line-height:150%;")
        lay.addWidget(lbl)
        lay.addSpacing(10)
        keep = QPushButton("保留在 C 盘（以后不再问）")
        keep.setMinimumHeight(34)
        keep.setStyleSheet("background:#2f7d32; color:white; font-weight:bold;")
        move = QPushButton("搬到其他盘并创建链接（推荐，C 盘立刻释放）")
        move.setMinimumHeight(34)
        move.setStyleSheet("background:#1565c0; color:white; font-weight:bold;")
        later = QPushButton("稍后再问（1 小时后提醒）")
        later.setMinimumHeight(30)
        keep.clicked.connect(lambda: self._done(IDYES))
        move.clicked.connect(lambda: self._done(IDNO))
        later.clicked.connect(lambda: self._done(IDCANCEL))
        lay.addWidget(keep)
        lay.addWidget(move)
        lay.addWidget(later)

    def _done(self, rid):
        self.result_id = rid
        self.accept()


class BlockedAppDialog(QDialog):
    """命中用户自定义限制规则：搬到别的盘 / 这次放C盘 / 删除 / 解除限制。"""

    MOVE = "move"
    KEEP = "keep"
    DELETE = "delete"
    UNBLOCK = "unblock"

    def __init__(self, title, text, rule_name="", parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.resize(580, 400)
        self.action = self.KEEP
        lay = QVBoxLayout(self)
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lbl.setStyleSheet("font-size:13px; line-height:150%;")
        lay.addWidget(lbl)
        lay.addSpacing(10)

        b_move = QPushButton("搬到别的盘（推荐，C 盘立刻腾出来）")
        b_move.setMinimumHeight(36)
        b_move.setStyleSheet("background:#1565c0; color:white; font-weight:bold;")
        b_keep = QPushButton("这次就放 C 盘（仅此一次）")
        b_keep.setMinimumHeight(32)
        b_del = QPushButton("不下载 / 不安装（删除刚写入的内容）")
        b_del.setMinimumHeight(32)
        b_del.setStyleSheet("color:#c62828;")
        nm = rule_name or "这个软件"
        b_unblock = QPushButton(f"解除限制：以后允许「{nm}」存到 C 盘")
        b_unblock.setMinimumHeight(30)
        b_move.clicked.connect(lambda: self._done(self.MOVE))
        b_keep.clicked.connect(lambda: self._done(self.KEEP))
        b_del.clicked.connect(lambda: self._done(self.DELETE))
        b_unblock.clicked.connect(lambda: self._done(self.UNBLOCK))
        lay.addWidget(b_move)
        lay.addWidget(b_keep)
        lay.addWidget(b_del)
        lay.addSpacing(6)
        lay.addWidget(b_unblock)

    def _done(self, act):
        self.action = act
        self.accept()


class AddRuleDialog(QDialog):
    """添加一条软件限制规则：从电脑里已安装/已下载的软件中选，或手动输入。"""

    def __init__(self, preset_name="", preset_keywords=None, sample_path="", parent=None):
        super().__init__(parent)
        self.setWindowTitle("添加软件限制")
        self.setModal(True)
        self.resize(600, 560)
        self.sample_path = sample_path or ""
        lay = QVBoxLayout(self)

        # ---- 第一步：从电脑里选（不盲猜）----
        lay.addWidget(QLabel("第一步：从你的电脑里选要限制的软件（列表里有就直接选）："))
        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText("输入名字过滤，比如 kugou / 酷狗 / steam")
        self.ed_search.textChanged.connect(self._filter_list)
        lay.addWidget(self.ed_search)

        self.lst_apps = QListWidget()
        self.lst_apps.setMinimumHeight(200)
        self.lst_apps.setAlternatingRowColors(True)
        self.lst_apps.currentItemChanged.connect(self._on_pick)
        lay.addWidget(self.lst_apps, 1)
        self.lab_src = QLabel("（正在扫描你电脑里的软件…）")
        self.lab_src.setStyleSheet("color:#888; font-size:12px;")
        self.lab_src.setWordWrap(True)
        lay.addWidget(self.lab_src)

        lay.addSpacing(8)
        # ---- 第二步：名称 + 关键词（自动填好，可改）----
        lay.addWidget(QLabel("第二步：确认名称和识别关键词（自动填好，可改）："))
        lay.addWidget(QLabel("软件名称："))
        self.ed_name = QLineEdit(preset_name)
        self.ed_name.setPlaceholderText("选好上面一个软件后自动填；也可以自己写，比如「酷狗音乐」")
        lay.addWidget(self.ed_name)
        lay.addWidget(QLabel("识别关键词（写入路径里含任一个就算命中，逗号分隔）："))
        self.ed_kw = QLineEdit(", ".join(preset_keywords or []))
        self.ed_kw.setPlaceholderText("选好上面一个软件后自动填")
        lay.addWidget(self.ed_kw)
        self.lab_preview = QLabel("")
        self.lab_preview.setStyleSheet("color:#666; font-size:12px;")
        self.lab_preview.setWordWrap(True)
        lay.addWidget(self.lab_preview)
        self.ed_name.textChanged.connect(self._preview)
        self._preview()

        lay.addSpacing(6)
        row = QHBoxLayout()
        ok = QPushButton("添加")
        ok.setMinimumHeight(36)
        ok.setStyleSheet("background:#2f7d32; color:white; font-weight:bold;")
        cancel = QPushButton("取消")
        cancel.setMinimumHeight(36)
        ok.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)
        row.addWidget(ok)
        row.addWidget(cancel)
        lay.addLayout(row)

        # 异步填充软件列表（注册表 + AppData + 下载的安装包）
        QTimer.singleShot(30, self._load_apps)

    # ---------- 软件列表 ----------
    def _load_apps(self):
        """从注册表 / AppData / 下载 读出已安装软件，填充列表。"""
        try:
            apps = guard_rules.list_installed_apps()
        except Exception:
            apps = []
        self.lst_apps.clear()
        if not apps:
            self.lab_src.setText("没扫描到已安装的软件，你可以在下面手动输入软件名。")
            return
        for a in apps:
            text = f"{a['name']}　—　{a['source']}"
            it = QListWidgetItem(text)
            it.setData(Qt.UserRole, a)
            tip_parts = [f"名称：{a['name']}"]
            if a.get("path"):
                tip_parts.append(f"位置：{a['path']}")
            if a.get("publisher"):
                tip_parts.append(f"发布者：{a['publisher']}")
            it.setToolTip("\n".join(tip_parts))
            self.lst_apps.addItem(it)
        self.lab_src.setText(f"已列出 {len(apps)} 个（点一下选中，名称关键词自动填好）")
        # 如果一开始带了预设（右键添加），尝试在列表里高亮匹配项
        if self.sample_path:
            self._prefill_from_sample()

    def _filter_list(self, text):
        t = (text or "").strip().lower()
        for i in range(self.lst_apps.count()):
            it = self.lst_apps.item(i)
            a = it.data(Qt.UserRole) or {}
            hay = (a.get("name", "") + " " + " ".join(a.get("keywords", []))).lower()
            it.setHidden(bool(t) and t not in hay)

    def _on_pick(self, cur, _prev):
        """点选列表中的一个软件 → 自动填名称和关键词。"""
        if cur is None:
            return
        a = cur.data(Qt.UserRole) or {}
        nm = a.get("name", "").replace("（安装包）", "").strip()
        self.ed_name.setText(nm)
        kws = list(a.get("keywords", []))
        # 手动预设的关键词（比如右键添加带来的）也并进来
        for k in [x.strip() for x in self.ed_kw.text().replace("，", ",").split(",") if x.strip()]:
            if k.lower() not in [x.lower() for x in kws]:
                kws.append(k)
        self.ed_kw.setText(", ".join(kws[:10]))
        self.lab_src.setText(f"已选中：{a.get('name','')}　（{a.get('source','')}）"
                             + (f"　位置：{a.get('path','')}" if a.get('path') else ""))

    def _prefill_from_sample(self):
        """右键添加时，按样本路径/名称在列表里找最像的一项并选中。"""
        want = (self.ed_name.text() or "").lower()
        kw0 = [k.lower() for k in guard_rules._keywords_from_sample(self.sample_path)]
        best = None
        for i in range(self.lst_apps.count()):
            it = self.lst_apps.item(i)
            a = it.data(Qt.UserRole) or {}
            hay = (a.get("name", "") + " " + " ".join(a.get("keywords", []))).lower()
            if want and want in hay:
                best = it
                break
            if any(k and k in hay for k in kw0):
                best = it
                break
        if best is not None:
            self.lst_apps.setCurrentItem(best)

    def _preview(self):
        try:
            kws = guard_rules._auto_keywords(self.ed_name.text())
            self.lab_preview.setText(
                "自动关键词：" + ("、".join(kws) if kws else "（先选一个软件或输入名称）"))
        except Exception:
            self.lab_preview.setText("")

    def values(self):
        kws = [k.strip() for k in self.ed_kw.text().replace("，", ",").split(",") if k.strip()]
        return self.ed_name.text().strip(), kws


class RulesDialog(QDialog):
    """软件限制规则管理：添加 / 删除 / 启用停用。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("软件存储限制 — 我自己定的规则")
        self.setModal(True)
        self.resize(640, 420)
        lay = QVBoxLayout(self)
        tip = QLabel(
            "下面这些规则全部由你自己添加，软件不预置任何品牌名单。\n"
            "命中后：写入 C 盘的内容会先问你，再由你选一个盘搬走。")
        tip.setStyleSheet("color:#555; font-size:12px;")
        tip.setWordWrap(True)
        lay.addWidget(tip)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["软件名称", "识别关键词", "命中次数", "启用"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setColumnWidth(0, 150)
        self.tree.setColumnWidth(1, 260)
        self.tree.setColumnWidth(2, 80)
        self.tree.setColumnWidth(3, 50)
        lay.addWidget(self.tree, 1)

        row = QHBoxLayout()
        b_add = QPushButton("➕ 添加软件")
        b_add.setMinimumHeight(32)
        b_add.setStyleSheet("background:#2f7d32; color:white; font-weight:bold;")
        b_del = QPushButton("🗑 删除选中")
        b_del.setMinimumHeight(32)
        b_toggle = QPushButton("启用 / 停用")
        b_toggle.setMinimumHeight(32)
        b_close = QPushButton("关闭")
        b_close.setMinimumHeight(32)
        b_add.clicked.connect(self._add)
        b_del.clicked.connect(self._del)
        b_toggle.clicked.connect(self._toggle)
        b_close.clicked.connect(self.accept)
        row.addWidget(b_add)
        row.addWidget(b_del)
        row.addWidget(b_toggle)
        row.addStretch(1)
        row.addWidget(b_close)
        lay.addLayout(row)
        self._reload()

    def _reload(self):
        import guard_rules
        self.tree.clear()
        for r in guard_rules.rules.rules:
            it = QTreeWidgetItem([
                r.get("name", ""),
                "、".join(r.get("keywords", [])),
                str(r.get("hits", 0)),
                "✅" if r.get("enabled", True) else "—",
            ])
            it.setData(0, Qt.UserRole, r.get("id"))
            self.tree.addTopLevelItem(it)

    def _add(self):
        import guard_rules
        dlg = AddRuleDialog(parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        name, kws = dlg.values()
        if not name:
            return
        guard_rules.rules.add(name, kws)
        self._reload()

    def _del(self):
        import guard_rules
        it = self.tree.currentItem()
        if not it:
            return
        rid = it.data(0, Qt.UserRole)
        if bridge.ask(APP_NAME, f"删除这条限制吗？\n\n{it.text(0)}", "yesno",
                      "question", default="否") == IDYES:
            guard_rules.rules.remove(rid)
            self._reload()

    def _toggle(self):
        import guard_rules
        it = self.tree.currentItem()
        if not it:
            return
        rid = it.data(0, Qt.UserRole)
        cur = it.text(3) == "✅"
        guard_rules.rules.set_enabled(rid, not cur)
        self._reload()


class DrivePickDialog(QDialog):
    """二级选择：选其他本地盘 / 浏览文件夹 / 取消(不存储) / 仍放C盘。"""

    CHOICE_DRIVE = "drive"
    CHOICE_BROWSE = "browse"
    CHOICE_DELETE = "delete"
    CHOICE_KEEP = "keep"
    CHOICE_CANCEL = "cancel"

    def __init__(self, title, hint, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.resize(520, 420)
        self.choice = self.CHOICE_CANCEL
        self.picked_root = None

        lay = QVBoxLayout(self)
        head = QLabel(hint)
        head.setWordWrap(True)
        lay.addWidget(head)
        lay.addWidget(QLabel("请选择存放位置（按剩余空间排序）："))

        self.lst = QListWidget()
        targets = self._targets()
        for name, root in targets:
            QListWidgetItem(f"💾  {name}", self.lst)
            item = self.lst.item(self.lst.count() - 1)
            item.setData(Qt.UserRole, root)
        if not targets:
            QListWidgetItem("（未检测到 C 盘以外的本地硬盘）", self.lst)
            self.lst.setEnabled(False)
        lay.addWidget(self.lst, 1)

        btns = QVBoxLayout()
        b_pick = QPushButton("存到这里")
        b_pick.setStyleSheet("background:#2f7d32; color:white; font-weight:bold;")
        b_pick.setMinimumHeight(32)
        b_browse = QPushButton("浏览文件夹…（自选任意位置）")
        b_browse.setMinimumHeight(32)
        b_delete = QPushButton("取消：不安装、不下载、不存储（删除刚写入的内容）")
        b_delete.setStyleSheet("color:#c62828;")
        b_delete.setMinimumHeight(32)
        b_keep = QPushButton("返回：还是留在 C 盘")
        b_keep.setMinimumHeight(28)
        for b in (b_pick, b_browse, b_delete, b_keep):
            btns.addWidget(b)
        lay.addLayout(btns)

        def cur_root():
            it = self.lst.currentItem()
            return it.data(Qt.UserRole) if it else None

        b_pick.clicked.connect(lambda: self._finish(self.CHOICE_DRIVE, cur_root()))
        b_browse.clicked.connect(self._browse)
        b_delete.clicked.connect(lambda: self._finish(self.CHOICE_DELETE, None))
        b_keep.clicked.connect(lambda: self._finish(self.CHOICE_KEEP, None))

    def _targets(self):
        import string, ctypes
        out = []
        for letter in string.ascii_uppercase:
            if letter == "C":
                continue
            root = f"{letter}:\\"
            if not os.path.exists(root):
                continue
            if ctypes.windll.kernel32.GetDriveTypeW(root) != 3:
                continue
            out.append((f"{letter} 盘（剩余 {engine.human_size(engine.drive_free(root))}）",
                        f"{letter}:\\C盘守卫接收"))
        # 用户在 config.json 里配置过的自定义目标排前面
        cfg_targets = []
        g = _guard_ref
        if g:
            for name, p in g.config.get("转移目标", {}).items():
                if p and len(p) >= 2 and p[1] == ":":
                    cfg_targets.append((f"{name}（{p}）", p))
        return cfg_targets + out

    def _browse(self):
        start = self._targets()[0][1][:2] + "\\" if self._targets() else ""
        d = QFileDialog.getExistingDirectory(self, "选择保存位置", start)
        if d:
            self._finish(self.CHOICE_DRIVE, d)

    def _finish(self, choice, root):
        if choice == self.CHOICE_DRIVE and not root:
            QMessageBox.information(self, APP_NAME, "请先在列表里选中一个位置。")
            return
        self.choice = choice
        self.picked_root = root
        self.accept()


# ============================================================
# GUI 版守卫引擎（覆写交互流程）
# ============================================================

_guard_ref = None


class GuiGuard(engine.Guard):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.today = guard_today.TodayScanner(APP_DIR)
        self._blocked_last = {}      # 用户限制规则的冷却记录：rule_id -> (时间, 上次选择)

    # ---------- 写入询问（改版）----------
    def ask_write(self, items):
        total = sum(s for _, s in items)
        if self.auto_choice:
            return "keep"
        if len(items) == 1:
            p, s = items[0]
            head = (f"有 1 个新文件要存进 C 盘，需要你同意：\n\n"
                    f"📄 文件：{p.name}\n"
                    f"💾 大小：{engine.human_size(s)}\n"
                    f"📁 位置：{p.parent}\n"
                    f"🏷️ 来源：{guard_today.guess_app(str(p))}\n")
        else:
            head = f"有 {len(items)} 个新文件要存进 C 盘，合计 {engine.human_size(total)}：\n\n"
            for pp, ss in items[:6]:
                head += f"  · {pp.name}（{engine.human_size(ss)}）\n"
            if len(items) > 6:
                head += f"  · …… 等共 {len(items)} 个\n"
        text = head + "\n" + engine.space_report(total)
        res = bridge.dialog({"type": "ask_write",
                             "title": "C盘守卫 — 有新文件要写入 C 盘", "text": text})
        if res.get("choice") == IDYES:
            return "keep"
        if res.get("choice") == IDNO:
            # ---- 二级选择 ----
            return self._secondary_choice(items, total)
        return "keep"

    def _secondary_choice(self, items, total):
        names = "、".join(p.name for p, _ in items[:3])
        if len(items) > 3:
            names += " 等"
        hint = (f"这些内容将不存入 C 盘（共 {len(items)} 项，{engine.human_size(total)}）：\n{names}\n\n"
                "选择存放位置，或取消本次存储。")
        res = bridge.dialog({"type": "secondary",
                             "title": "C盘守卫 — 选择存放位置", "hint": hint})
        if res.get("action") == DrivePickDialog.CHOICE_DRIVE:
            return res.get("root")
        if res.get("action") == DrivePickDialog.CHOICE_DELETE:
            return "DELETE"
        return "keep"

    # ---------- 用户自定义限制：命中的先走这里 ----------
    def handle_write(self, items):
        """分流：命中用户自己加的限制规则 → 走限制流程；其余走常规询问。"""
        known = []
        for p, s in items:
            try:
                if p.exists():
                    known.append((p, p.stat().st_size))
            except OSError:
                continue
        if not known:
            return
        try:
            import guard_rules
            blocked, rest = {}, []
            for p, s in known:
                app = guard_today.guess_app(str(p))
                r = guard_rules.rules.match(str(p), app)
                if r:
                    blocked.setdefault(r["id"], []).append((p, s))
                else:
                    rest.append((p, s))
            for rid, group in blocked.items():
                self._handle_blocked(group, guard_rules.rules.get(rid))
            if rest:
                self._handle_normal_write(rest)
        except Exception:
            self._handle_normal_write(known)

    def _handle_blocked(self, items, rule):
        """命中用户自定义规则：提示 → 用户选择（含选盘）→ 执行。"""
        if not rule:
            return self._handle_normal_write(items)
        name = rule.get("name", "这个软件")
        rid = rule.get("id", "")
        now = time.time()
        # 冷却：同一条规则 90 秒内只问一次，其余沿用上次的选择
        last = self._blocked_last.get(rid)
        if last and now - last[0] < 90 and last[1] != BlockedAppDialog.MOVE:
            return self._apply_block_choice(last[1], items, rule)

        total = sum(s for _, s in items)
        if len(items) == 1:
            p, s = items[0]
            detail = (f"📄 内容：{p.name}\n"
                      f"💾 大小：{engine.human_size(s)}\n"
                      f"📁 位置：{p.parent}\n")
        else:
            detail = f"共 {len(items)} 项，合计 {engine.human_size(total)}\n"
            for pp, ss in items[:5]:
                detail += f"  · {pp.name}（{engine.human_size(ss)}）\n"
            if len(items) > 5:
                detail += f"  · …… 等共 {len(items)} 项\n"
        text = (f"你自己设置过：不允许「{name}」存到 C 盘。\n"
                f"现在检测到它正在往 C 盘写入：\n\n{detail}\n"
                "想怎么处理？选一个：")
        res = bridge.dialog({"type": "app_block",
                             "title": f"C盘守卫 — 你限制过「{name}」",
                             "text": text, "rule_name": name})
        act = res.get("action") or BlockedAppDialog.KEEP
        self._blocked_last[rid] = (now, act)
        self._apply_block_choice(act, items, rule)

    def _apply_block_choice(self, act, items, rule):
        """执行用户在限制提示里的选择。"""
        name = rule.get("name", "这个软件")
        if act == BlockedAppDialog.UNBLOCK:
            import guard_rules
            guard_rules.rules.remove(rule.get("id", ""))
            self._blocked_last.pop(rule.get("id", ""), None)
            for p, s in items:
                self.log("限制", p.name, s, f"解除限制-{name}")
            bridge.info(APP_NAME, f"已解除限制，以后「{name}」可以正常存到 C 盘。")
            return

        if act == BlockedAppDialog.KEEP:
            for p, s in items:
                self.log("限制", p.name, s, f"本次放行-{name}")
                try:
                    self.today.record_event(str(p), s)
                except Exception:
                    pass
            return

        if act == BlockedAppDialog.DELETE:
            ok = bridge.ask(APP_NAME,
                            f"确定不让「{name}」把内容存进 C 盘吗？\n\n"
                            f"这 {len(items)} 项会立即从 C 盘删除（相当于拦截下载/安装）。",
                            "yesno", "warning", default="否")
            if ok != IDYES:
                return
            deleted, failed = 0, 0
            for p, s in items:
                try:
                    if p.is_dir():
                        shutil.rmtree(p, ignore_errors=True)
                        if p.exists():
                            raise OSError
                    else:
                        p.unlink()
                    deleted += 1
                    self.log("限制", p.name, s, f"按限制删除-{name}")
                except OSError:
                    failed += 1
                    self.log("限制", p.name, s, f"删除失败-保留C盘-{name}")
            msg = f"已拦截 {deleted} 项，C 盘没有被占用。"
            if failed:
                msg += f"\n{failed} 项删除失败（正被占用），已留在 C 盘。"
            bridge.info(APP_NAME, msg)
            return

        if act == BlockedAppDialog.MOVE:
            total = sum(s for _, s in items)
            root = self.pick_drive(
                "C盘守卫 — 选择搬移位置",
                f"把「{name}」的这些内容（{len(items)} 项，"
                f"{engine.human_size(total)}）搬到哪个盘？\n\n"
                "搬走后原位置会留一个链接，软件照常使用，C 盘空间立即释放。")
            if not root:
                for p, s in items:
                    self.log("限制", p.name, s, f"未选盘-保留C盘-{name}")
                return
            moved, failed = 0, 0
            for p, s in items:
                try:
                    dest = (self.move_dir_junction(p, root) if p.is_dir()
                            else self.move_file(p, root))
                except Exception:
                    dest = ""
                if dest:
                    moved += 1
                    self.log("限制", p.name, s, f"按限制搬到{Path(root).anchor}", dest)
                else:
                    failed += 1
                    self.log("限制", p.name, s, f"搬移失败-保留C盘-{name}")
            msg = f"已按你的限制，把 {moved} 项「{name}」的内容移到 {Path(root).anchor}"
            if failed:
                msg += f"\n{failed} 项搬移失败（可能在被软件占用），已保留在 C 盘。"
            bridge.info(APP_NAME, msg)
            return

        self._handle_normal_write(items)

    # ---------- 写入处理（常规询问）----------
    def _handle_normal_write(self, items):
        known = []
        for p, s in items:
            try:
                if p.exists():
                    known.append((p, p.stat().st_size))
            except OSError:
                continue
        if not known:
            return
        total = sum(s for _, s in known)
        decision = self.ask_write(known)

        if decision == "DELETE":
            ok = bridge.ask(APP_NAME,
                            f"确定不存储这 {len(known)} 项吗？\n\n"
                            "它们会立即从 C 盘删除（相当于拦截下载/安装）。\n"
                            "注意：如果对方软件还在写入，可能删除失败。",
                            "yesno", "warning", default="否")
            if ok == IDYES:
                deleted, failed = 0, 0
                for p, s in known:
                    try:
                        p.unlink()
                        deleted += 1
                        self.log("写入", p.name, s, "用户选择不存储-已删除")
                    except OSError:
                        failed += 1
                        self.log("写入", p.name, s, "删除失败-保留C盘")
                msg = f"已拦截删除 {deleted} 项，未占用 C 盘。"
                if failed:
                    msg += f"\n{failed} 项删除失败（正被占用），已保留在 C 盘。"
                bridge.info(APP_NAME, msg)
            return

        if decision == "mute":
            for p, _ in known:
                self.log("写入", p.name, 0, "永久同意(不再提示)", str(p))
            return
        if decision == "keep":
            for p, s in known:
                self.log("写入", p.name, s, "同意保留")
                try:
                    self.today.record_event(str(p), s)
                except Exception:
                    pass
            return

        moved = 0
        for p, s in known:
            dest = self.move_file(p, decision)
            if dest:
                moved += 1
                self.log("写入", p.name, s, f"转移到{Path(decision).anchor}", dest)
            else:
                self.log("写入", p.name, s, "转移失败-保留原地")
        if moved:
            bridge.info(APP_NAME,
                        f"{moved} 个文件已移到 {Path(decision).anchor}，C 盘不再占用。\n\n"
                        f"位置：{Path(decision)}\\{known[0][0].parent.name}")

    # ---------- 选盘对话框（聚合/安装搬家用）----------
    def pick_drive(self, title, hint):
        res = bridge.dialog({"type": "pick_drive", "title": title, "hint": hint})
        if res.get("action") == DrivePickDialog.CHOICE_DRIVE:
            return res.get("root")
        return None

    # ---------- 用户根目录新文件夹拦截（v5.1）----------
    def ask_root_folder(self, p, size):
        if self.auto_choice:
            return "keep"
        text = (f"你的用户目录里出现一个新文件夹，可能又有一个软件要把东西存进 C 盘：\n\n"
                f"📁 文件夹：{p.name}\n"
                f"📦 已占用：{engine.human_size(size)}\n"
                f"📍 位置：{p.parent}\n"
                f"🏷️ 疑似来源：{guard_today.guess_app(str(p) + p.name)}\n\n"
                "选择「搬移并创建链接」后：文件夹实体移到其他盘，原位置留一个透明链接，\n"
                "相关软件照常使用，C 盘空间立即释放。")
        res = bridge.dialog({"type": "root_folder",
                             "title": "C盘守卫 — 用户目录出现新文件夹", "text": text})
        c = res.get("choice")
        if c == IDYES:
            return "keep"
        if c == IDNO:
            root = self.pick_drive("C 盘守卫 — 选择搬移位置",
                                   f"把文件夹 {p.name} 搬到哪个盘？")
            return root if root else "skip"
        return "skip"

    # ---------- 自启动注册（桌面版启动后缩在托盘）----------
    @staticmethod
    def ensure_autostart():
        exe = sys.executable if getattr(sys, "frozen", False) else None
        if not exe:
            return
        want = f'"{exe}" --minimized'
        try:
            import winreg
            path = r"Software\Microsoft\Windows\CurrentVersion\Run"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0,
                                winreg.KEY_READ | winreg.KEY_SET_VALUE) as k:
                try:
                    cur, _ = winreg.QueryValueEx(k, "CDiskGuard")
                except FileNotFoundError:
                    cur = ""
                if cur != want:
                    winreg.SetValueEx(k, "CDiskGuard", 0, winreg.REG_SZ, want)
        except Exception:
            pass


# ============================================================
# 主窗口
# ============================================================

# ============================================================
# 可排序的树节点
# ============================================================

class SortItem(QTreeWidgetItem):
    """支持按任意列正确排序的行。

    每一列都可以塞一个"排序键"（数字按数值、文本按字符串），
    存在 Qt.UserRole+1 上；0 列（复选框）之外的列也能点表头排序。
    """

    ROLE = Qt.UserRole + 1

    def set_key(self, col: int, key):
        self.setData(col, self.ROLE, key)

    def __lt__(self, other):
        tw = self.treeWidget()
        col = tw.sortColumn() if tw is not None else 0
        if isinstance(other, SortItem):
            a = self.data(col, self.ROLE)
            b = other.data(col, self.ROLE)
            if a is not None and b is not None:
                try:
                    return a < b
                except TypeError:
                    return str(a) < str(b)
        # 注意：不能调用 super().__lt__()——PySide6 里基类实现会重新进入
        # 本 Python 重写，造成无限递归。降级为按显示文本比较。
        try:
            return str(self.text(col)) < str(other.text(col))
        except Exception:
            return False


class _ElasticTree(QTreeWidget):
    """表格：让指定的一列随窗口宽度自动伸缩，其余列保持固定宽度，
    整表绝不超出可用宽度（也就不会出现右侧被裁掉 / 横向滚动条）。

    为什么需要它：QHeaderView 自带的 Stretch 列有个坑——它的最小宽度等于
    内容自然宽度。路径一长，"自适应列"反而把整张表撑出右侧边界。
    改成普通固定列 + 在 resize 时手动分配剩余宽度，行为就完全可控了。
    """

    def __init__(self, elastic_col=1, fixed_sum=0, min_elastic=120, parent=None):
        super().__init__(parent)
        self._elastic_col = elastic_col
        self._fixed_sum = int(fixed_sum)
        self._min_elastic = int(min_elastic)

    def sizeHint(self):
        """宽度方向不受内容影响（否则一条长路径就能把整页撑出右侧边界）。"""
        s = super().sizeHint()
        return QSize(min(s.width(), 320), s.height())

    def minimumSizeHint(self):
        s = super().minimumSizeHint()
        return QSize(min(s.width(), 120), s.height())

    def resizeEvent(self, e):
        super().resizeEvent(e)
        try:
            avail = self.viewport().width()
            want = max(self._min_elastic, avail - self._fixed_sum)
            if abs(self.columnWidth(self._elastic_col) - want) > 2:
                self.setColumnWidth(self._elastic_col, want)
        except Exception:
            pass


class MainWindow(QMainWindow):
    scan_progress = Signal(int, str)
    scan_done = Signal(dict)
    move_done = Signal(int, int, int, str, str)   # moved_files, freed, failed, msgs, dest_root
    clean_scan_done = Signal(object)              # 日志清理扫描结果 dict
    clean_done = Signal(int, int, int, str)       # freed, ok_count, fail_count, msgs
    legacy_progress = Signal(str)                 # 存量盘点进度
    legacy_done = Signal(dict)                    # 存量盘点完成

    def __init__(self, guard: GuiGuard, minimized=False):
        super().__init__()
        self.guard = guard
        self.scanner = guard.today
        self.setWindowTitle(f"{APP_NAME} · 桌面版 v6.7 — 正在守护 C 盘")
        self.resize(1180, 720)
        self.setMinimumSize(1000, 640)
        self.scan_thread = None
        self._tray_hinted = False
        # ---- 存量盘点 ----
        self.legacy = guard_legacy.LegacyScanner(APP_DIR)
        self._install = guard_legacy.load_install_time(APP_DIR)
        self.legacy_thread = None
        self._legacy_cancel = None

        # ============ 左侧栏 + 右侧页面（新 UI）============
        root = QWidget()
        self.setCentralWidget(root)
        root_lay = QHBoxLayout(root)
        root_lay.setContentsMargins(0, 0, 0, 0)
        root_lay.setSpacing(0)

        # ---- 侧栏 ----
        side = QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(GT.SIDEBAR_W)
        side.setStyleSheet(
            f"QFrame#sidebar{{background:{GT.C['paper']};"
            f"border-right:1px solid {GT.C['line']};}}")
        s = QVBoxLayout(side)
        s.setContentsMargins(14, 18, 14, 14)
        s.setSpacing(4)

        brand = QHBoxLayout()
        bl = QLabel()
        bl.setPixmap(GT.brand_pixmap(34))
        bl.setFixedSize(34, 34)
        bt = QVBoxLayout()
        bt.setSpacing(0)
        t1 = QLabel("C盘守卫")
        t1.setStyleSheet(f"font-weight:700;font-size:{GT.human_font(16)}pt;")
        t2 = QLabel("桌面版 v6.7")
        t2.setStyleSheet(f"color:{GT.C['ink3']};font-size:{GT.human_font(11)}pt;")
        bt.addWidget(t1)
        bt.addWidget(t2)
        brand.addWidget(bl)
        brand.addSpacing(9)
        brand.addLayout(bt)
        brand.addStretch(1)
        s.addLayout(brand)
        s.addSpacing(16)

        # 导航 + 页面
        self.nav = {}
        self._nav_index = {}
        self.stack = QStackedWidget()
        pages = (
            ("overview",   "概况",     self._build_home_tab()),
            ("today",      "今日新增", self._build_today_tab()),
            ("legacy",     "存量盘点", self._build_legacy_tab()),
            ("clean",      "日志清理", self._build_clean_tab()),
            ("guardlog",   "守卫记录", self._build_log_tab()),
            ("quarantine", "隔离区",   self._build_quar_tab()),
            ("settings",   "设置",     self._build_settings_tab()),
        )
        for i, (key, title, page_w) in enumerate(pages):
            nb = GT.NavButton(key, title)
            nb.clicked.connect(lambda _c=False, k=key: self._goto_page(k))
            s.addWidget(nb)
            self.nav[key] = nb
            self._nav_index[key] = i
            # 每个页面都套一层滚动区：窗口高度不够时可以滚动查看，
            # 布局不再压缩控件，保证所有文字完整可见、不被遮挡。
            self.stack.addWidget(self._scroll_wrap(page_w))
        s.addStretch(1)

        # 守护徽标
        badge = QFrame()
        badge.setStyleSheet(
            f"QFrame{{background:{GT.C['green_soft']};"
            f"border:1px solid #CFE8D8;border-radius:12px;}}")
        bl2 = QHBoxLayout(badge)
        bl2.setContentsMargins(12, 10, 12, 10)
        bl2.setSpacing(9)
        dot = QLabel()
        pdot = QPixmap(9, 9)
        pdot.fill(Qt.GlobalColor.transparent)
        _p = QPainter(pdot)
        _p.setRenderHint(QPainter.RenderHint.Antialiasing)
        _p.setBrush(QColor(GT.C["green"]))
        _p.setPen(Qt.PenStyle.NoPen)
        _p.drawEllipse(0, 0, 9, 9)
        _p.end()
        dot.setPixmap(pdot)
        tx = QVBoxLayout()
        tx.setSpacing(0)
        g1 = QLabel("正在守护 C 盘")
        g1.setStyleSheet(f"font-weight:600;font-size:{GT.human_font(13)}pt;")
        g2 = QLabel("本地运行 · 不联网")
        g2.setStyleSheet(f"color:{GT.C['ink3']};font-size:{GT.human_font(11)}pt;")
        tx.addWidget(g1)
        tx.addWidget(g2)
        bl2.addWidget(dot)
        bl2.addLayout(tx)
        s.addWidget(badge)

        # ---- 右侧页面容器 ----
        right = QWidget()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(24, 18, 24, 20)
        right_lay.setSpacing(0)
        right_lay.addWidget(self.stack)
        root_lay.addWidget(side)
        root_lay.addWidget(right, 1)

        self._goto_page("overview")

        self.scan_progress.connect(self._on_scan_progress)
        self.scan_done.connect(self._on_scan_done)
        self.move_done.connect(self._after_move)
        self.clean_scan_done.connect(self._on_clean_scan_done)
        self.clean_done.connect(self._on_clean_done)
        self.legacy_progress.connect(self._on_legacy_progress)
        self.legacy_done.connect(self._on_legacy_done)

        # 枚举本机磁盘，初始化"检测位置"下拉
        self._reload_drive_combo()

        # 托盘
        self.tray = QSystemTrayIcon(self)
        icon = QIcon(str(ICON_PATH)) if ICON_PATH.exists() else \
            self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        self.tray.setIcon(icon)
        menu = QMenu()
        act_show = QAction("显示主界面", self)
        act_show.triggered.connect(self.show_up)
        act_scan = QAction("扫描今日新增", self)
        act_scan.triggered.connect(self.start_scan)
        act_quit = QAction("退出守卫（停止保护）", self)
        act_quit.triggered.connect(self.real_quit)
        menu.addAction(act_show)
        menu.addAction(act_scan)
        menu.addSeparator()
        menu.addAction(act_quit)
        self.tray.setContextMenu(menu)
        self.tray.setToolTip(f"{APP_NAME} — 正在后台守护 C 盘")
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

        if minimized:
            self.hide()
        else:
            self.show()

        # 启动即扫一遍今日新增（后台）
        QTimer.singleShot(1200, self.start_scan)
        # 每 60 秒自动刷新记录/今日列表
        self.auto_timer = QTimer(self)
        self.auto_timer.timeout.connect(self._auto_refresh)
        self.auto_timer.start(60000)
        # 每 30 分钟检查一次：是否到了自动清理日志的时间
        self.clean_timer = QTimer(self)
        self.clean_timer.timeout.connect(self._auto_clean_check)
        self.clean_timer.start(1800000)
        QTimer.singleShot(6000, self._auto_clean_check)

        # 存量盘点：进入就有上次的结果可看（没有就显示一句引导）
        QTimer.singleShot(1500, self._fill_legacy_overview)
        QTimer.singleShot(1600, self._fill_legacy_rows)

    # ---------- 概况 ----------
    @staticmethod
    def _scroll_wrap(w):
        """把页面装进滚动区：窗口不够高时改成滚动，绝不让布局把文字压没。"""
        sa = QScrollArea()
        sa.setWidget(w)
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.Shape.NoFrame)
        # 页面只允许竖向滚动；横向靠表格 stretch 列自适应，长路径省略号截断
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        sa.setStyleSheet(
            "QScrollArea{background:transparent;border:none;}"
            "QScrollArea>QWidget>QWidget{background:transparent;}")
        # 所有自动折行的文字标签都允许水平收缩——
        # wordWrap 只管"显示时折行"，不管最小宽度；
        # 不设这一项，一句带长路径的提示就能把整页撑出右侧边界。
        for lab in w.findChildren(QLabel):
            if lab.wordWrap():
                lab.setSizePolicy(QSizePolicy.Policy.Ignored,
                                  QSizePolicy.Policy.Preferred)
        return sa

    def _page_head(self, title, sub, right=None):
        """设计稿风格的页头：大标题 + 灰字副标题（右侧可放按钮/药丸）。"""
        head = QHBoxLayout()
        hv = QVBoxLayout()
        hv.setSpacing(2)
        h1 = QLabel(title)
        h1.setStyleSheet(f"font-size:{GT.human_font(20)}pt;font-weight:700;")
        s = QLabel(sub)
        s.setStyleSheet(f"color:{GT.C['ink3']};font-size:{GT.human_font(13)}pt;")
        hv.addWidget(h1)
        hv.addWidget(s)
        head.addLayout(hv)
        head.addStretch(1)
        if right is not None:
            head.addWidget(right)
        return head

    def _build_home_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)

        # ---- 页头 ----
        head = QHBoxLayout()
        hv = QVBoxLayout()
        hv.setSpacing(2)
        h1 = QLabel("概况")
        h1.setStyleSheet(f"font-size:{GT.human_font(20)}pt;font-weight:700;")
        sub = QLabel("C 盘今天的样子，一眼看完")
        sub.setStyleSheet(f"color:{GT.C['ink3']};font-size:{GT.human_font(13)}pt;")
        hv.addWidget(h1)
        hv.addWidget(sub)
        head.addLayout(hv)
        head.addStretch(1)
        head.addWidget(GT.pill("守护引擎运行中", "ok"))
        lay.addLayout(head)

        # ---- 大卡片：环形仪表盘 + 数值 ----
        hero = QFrame()
        hero.setProperty("card", True)
        hl = QHBoxLayout(hero)
        hl.setContentsMargins(28, 20, 28, 20)
        hl.setSpacing(32)
        self.gauge = GT.Gauge()
        hl.addWidget(self.gauge, 0, Qt.AlignmentFlag.AlignVCenter)

        info = QVBoxLayout()
        info.setSpacing(5)
        ht = QHBoxLayout()
        h2 = QLabel("C 盘 · 系统")
        h2.setStyleSheet(f"font-size:{GT.human_font(18)}pt;font-weight:700;")
        ht.addWidget(h2)
        self.home_pill = QLabel("良好")
        self._style_home_pill("ok")
        ht.addWidget(self.home_pill)
        ht.addStretch(1)
        info.addLayout(ht)

        nums = QHBoxLayout()
        nums.setSpacing(30)

        def _num(key):
            v = QLabel("—")
            f = v.font()
            f.setPointSize(GT.human_font(22))
            f.setBold(True)
            v.setFont(f)
            # 大数字必须完整显示：按 "888.8" 的实际字体宽度预留最小宽度，
            # 窗口再窄也不允许被压缩成 "42" / "59." 这种残字
            v.setMinimumWidth(QFontMetrics(f).horizontalAdvance("888.8") + 18)
            k = QLabel(key)
            k.setStyleSheet(f"color:{GT.C['ink3']};font-size:{GT.human_font(12)}pt;")
            c = QVBoxLayout()
            c.setSpacing(0)
            c.addWidget(v)
            c.addWidget(k)
            return c, v

        c1, self.home_used = _num("已用")
        c2, self.home_total = _num("总容量")
        c3, self.home_free = _num("剩余")
        for c in (c1, c2, c3):
            nums.addLayout(c)
        nums.addStretch(1)
        info.addLayout(nums)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(10)
        info.addWidget(self.bar)
        self.lbl_disk = QLabel()
        self.lbl_disk.setStyleSheet(
            f"color:{GT.C['ink3']};font-size:{GT.human_font(12)}pt;")
        self.lbl_disk.setWordWrap(True)     # 长文本折行，不撑宽页面
        info.addWidget(self.lbl_disk)

        self.lbl_engine = QLabel("写入询问、删除保护、安装哨兵都在正常值班")
        self.lbl_engine.setStyleSheet(
            f"color:{GT.C['ink2']};font-size:{GT.human_font(13)}pt;")
        self.lbl_engine.setWordWrap(True)
        info.addWidget(self.lbl_engine)
        hl.addLayout(info, 1)
        lay.addWidget(hero)

        # ---- 全部磁盘总览 ----
        lay.addWidget(GT.section_title("全部磁盘总览"))
        self.drive_box = QFrame()
        self.drive_box.setProperty("card", True)
        self.drive_lay = QVBoxLayout(self.drive_box)
        self.drive_lay.setContentsMargins(0, 0, 0, 0)
        self.drive_lay.setSpacing(0)
        lay.addWidget(self.drive_box)

        # ---- 提示 ----
        self.home_tip = QLabel()
        self.home_tip.setStyleSheet(
            f"background:{GT.C['green_mist']};border:1px solid #D8EADF;"
            f"border-radius:10px;padding:11px 15px;color:{GT.C['ink2']};"
            f"font-size:{GT.human_font(13)}pt;")
        self.home_tip.setWordWrap(True)
        lay.addWidget(self.home_tip)

        # ---- 快捷操作 ----
        quick = QHBoxLayout()
        quick.setSpacing(12)
        for text, kind, fn in (("立体体检（生成报告）", "primary", self._do_dashboard),
                               ("本周周报", None, self._do_weekly),
                               ("打开日志文件夹", None, self._open_logdir)):
            b = GT.style_button(QPushButton(text), kind)
            b.setMinimumHeight(42)
            b.clicked.connect(fn)
            quick.addWidget(b)
        lay.addLayout(quick)

        # ---- 脚注 ----
        notes = QLabel(
            "· 弹出写入询问时：选「是」＝仍存 C 盘；选「否」＝换其他盘 / 不存储。\n"
            "· 点右上角 × 只是最小化到托盘，守卫仍在后台保护 C 盘。\n"
            "· 真正退出：托盘图标右键 →「退出守卫」。")
        notes.setStyleSheet(
            f"color:{GT.C['ink3']};font-size:{GT.human_font(12)}pt;"
            f"border-top:1px dashed {GT.C['line2']};padding-top:12px;")
        notes.setWordWrap(True)
        lay.addWidget(notes)
        lay.addStretch(1)

        self._refresh_home()          # 内部会一并刷新「全部磁盘总览」
        return w

    def _style_home_pill(self, kind):
        colors = {"ok": (GT.C["green_soft"], GT.C["green_deep"]),
                  "warn": (GT.C["amber_soft"], "#9A6A14"),
                  "bad": (GT.C["red_soft"], "#A03030"),
                  "info": (GT.C["blue_soft"], "#2A5C8F")}
        bg, fg = colors.get(kind, colors["ok"])
        self.home_pill.setStyleSheet(
            f"background:{bg};color:{fg};font-weight:600;"
            f"font-size:{GT.human_font(12)}pt;border-radius:10px;padding:3px 12px;")

    def _refresh_all_drives(self):
        """概况页「全部磁盘总览」：每块盘一行 + 健康色条 + 一键查看今日新增。"""
        if not hasattr(self, "drive_lay"):
            return
        # 清掉旧内容
        while self.drive_lay.count():
            item = self.drive_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
            elif item.layout() is not None:
                lay = item.layout()
                while lay.count():
                    sub = lay.takeAt(0)
                    if sub.widget() is not None:
                        sub.widget().deleteLater()

        try:
            drives = guard_drives.list_drives()
        except Exception:
            drives = []
        # 让概况页总是能看到最新的盘清单
        self._drive_infos_for_home = drives

        if not drives:
            self.drive_lay.addWidget(QLabel("没有检测到可用磁盘。"))
            return

        grid = QGridLayout()
        grid.setContentsMargins(16, 6, 16, 6)
        grid.setVerticalSpacing(4)
        grid.setColumnMinimumWidth(0, 100)
        grid.setColumnStretch(1, 1)
        grid.setColumnMinimumWidth(2, 150)
        for i, d in enumerate(drives):
            name = QLabel(f"{d['health']['icon']} {d['name']}")
            name.setStyleSheet("font-weight:bold;")
            grid.addWidget(name, i, 0)

            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(int(d["pct"]))
            bar.setTextVisible(False)
            bar.setFixedHeight(8)
            bar.setStyleSheet(
                f"QProgressBar::chunk {{ background:{d['health']['color']}; }}")
            bar.setToolTip(f"占用 {d['pct']:.1f}%")
            grid.addWidget(bar, i, 1)

            txt = QLabel(
                f"已用 {engine.human_size(d['used'])} / {engine.human_size(d['total'])}"
                f"　剩 {engine.human_size(d['free'])}　{d['pct']:.0f}%")
            txt.setStyleSheet(f"color:{GT.C['ink2']};font-size:{GT.human_font(12.5)}pt;")
            txt.setWordWrap(True)          # 允许折行，窗口变窄时不撑宽页面
            grid.addWidget(txt, i, 2)

            b1 = GT.style_button(QPushButton("看它今天多了什么"), "sm")
            b1.setMinimumWidth(b1.fontMetrics().horizontalAdvance(b1.text()) + 30)
            b1.setToolTip(f"切到「今日新增」并检测 {d['letter']} 盘")
            b1.clicked.connect(lambda _c=False, L=d["letter"]: self._goto_drive(L))
            b2 = GT.style_button(QPushButton("打开该盘"), "ghost")
            b2.setMinimumWidth(b2.fontMetrics().horizontalAdvance(b2.text()) + 30)
            b2.setToolTip(f"在资源管理器里打开 {d['root']}")
            b2.clicked.connect(lambda _c=False, R=d["root"]: self._open_root(R))
            btns = QHBoxLayout()
            btns.addStretch(1)
            btns.addWidget(b1)
            btns.addWidget(b2)
            grid.addLayout(btns, i, 3)

        wrap = QWidget()
        wrap.setLayout(grid)
        self.drive_lay.addWidget(wrap)

        best = max(drives, key=lambda x: x["free"])
        if hasattr(self, "home_tip"):
            self.home_tip.setText(
                f"💡 空间最宽裕的是 {best['letter']} 盘"
                f"（剩 {engine.human_size(best['free'])}）。"
                f"把软件、下载、微信文件放到它上面，C 盘就不容易告急。")

    def _goto_page(self, key):
        """切到指定页面并同步侧栏高亮。"""
        idx = self._nav_index.get(key)
        if idx is None:
            return
        self.stack.setCurrentIndex(idx)
        for k, b in self.nav.items():
            b.setChecked(k == key)

    def _goto_drive(self, letter):
        """概况页按钮：切到今日新增页并把检测位置切到指定盘。"""
        try:
            idx = self.combo_drive.findData(letter)
            if idx >= 0:
                self.combo_drive.setCurrentIndex(idx)
            self._goto_page("today")
            self.show_up()
        except Exception:
            pass

    def _open_root(self, root):
        try:
            subprocess.Popen(["explorer", root])
        except Exception:
            pass

    def _refresh_home(self):
        st = engine.disk_state()
        pct = st["pct"]
        if pct >= 88:
            kind, col = "bad", GT.C["red"]
        elif pct >= 75:
            kind, col = "warn", GT.C["amber"]
        else:
            kind, col = "ok", GT.C["green"]
        self.gauge.set_value(pct, col, "C 盘已用")
        self.home_used.setText(engine.human_size(st["used"]))
        self.home_total.setText(engine.human_size(st["total"]))
        self.home_free.setText(engine.human_size(st["free"]))
        self.home_free.setStyleSheet(
            f"font-size:{GT.human_font(22)}pt;font-weight:700;"
            f"color:{GT.C['green_deep']};")
        self.home_pill.setText(st["level"])
        self._style_home_pill(kind)
        self.lbl_disk.setText(
            f"已用 {engine.human_size(st['used'])} / {engine.human_size(st['total'])}"
            f"（{st['pct']:.1f}%）｜ 剩余 {engine.human_size(st['free'])}")
        self.bar.setValue(int(pct))
        self.bar.setStyleSheet(
            f"QProgressBar::chunk {{ background:{col}; border-radius:5px; }}")
        self._refresh_all_drives()

    # ---------- 今日新增 ----------
    def _build_today_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        lay.addLayout(self._page_head("今日新增", "今天写进 C 盘的东西，看清楚再决定搬不搬"))

        # ---- 第一行：检测哪个盘 + 怎么排序 ----
        top = QHBoxLayout()
        top.addWidget(QLabel("检测位置："))
        self.combo_drive = QComboBox()
        self.combo_drive.setMinimumWidth(240)
        # 下拉框跟着窗口伸展、但绝不允许被压缩：盘名文字必须完整可读
        self.combo_drive.setSizePolicy(QSizePolicy.Policy.Expanding,
                                       QSizePolicy.Policy.Fixed)
        self.combo_drive.setToolTip(
            "这里会列出你这台电脑上所有能用的盘：本地硬盘、U 盘都会自动出现。\n"
            "换一个盘，下面的列表就变成那个盘今天新增了什么。")
        self.combo_drive.currentIndexChanged.connect(self._on_drive_changed)
        top.addWidget(self.combo_drive, 1)

        top.addSpacing(12)
        top.addWidget(QLabel("排序："))
        self.combo_sort = QComboBox()
        self.combo_sort.setMinimumWidth(120)
        self.combo_sort.addItem("占用大的排前面", "size_desc")
        self.combo_sort.addItem("占用小的排前面", "size_asc")
        self.combo_sort.addItem("最新出现的排前面", "time_desc")
        self.combo_sort.addItem("最早出现的排前面", "time_asc")
        self.combo_sort.addItem("按来源应用排序", "app")
        self.combo_sort.addItem("按所在位置排序", "dir")
        self.combo_sort.addItem("要注意的排前面（搬移风险）", "risk")
        self.combo_sort.setToolTip("也可以直接点列表的表头排序（点一下升序、再点一下降序）")
        self.combo_sort.currentIndexChanged.connect(self._apply_sort)
        top.addWidget(self.combo_sort)

        top.addStretch(1)
        self.btn_scan = QPushButton("🔄 扫描今日新增")
        self.btn_scan.setMinimumHeight(30)
        self.btn_scan.clicked.connect(self.start_scan)
        top.addWidget(self.btn_scan)
        lay.addLayout(top)

        # ---- 第二行：这块盘的健康条 ----
        self.bar_drive = QProgressBar()
        self.bar_drive.setRange(0, 100)
        self.bar_drive.setTextVisible(False)
        self.bar_drive.setFixedHeight(12)
        lay.addWidget(self.bar_drive)
        self.lbl_drive = QLabel("")
        self.lbl_drive.setStyleSheet("color:#444; font-size:12px;")
        self.lbl_drive.setWordWrap(True)    # 长文本折行，不撑宽页面
        lay.addWidget(self.lbl_drive)

        self.lbl_today_summary = QLabel("今日新增：尚未扫描，点击「🔄 扫描今日新增」")
        self.lbl_today_summary.setStyleSheet("font-weight:bold;")
        self.lbl_today_summary.setWordWrap(True)
        lay.addWidget(self.lbl_today_summary)

        # 主要来源单独一行：不再和总量挤在同一行里折行重叠
        self.lbl_top_apps = QLabel("")
        self.lbl_top_apps.setStyleSheet("color:#555;")
        self.lbl_top_apps.setWordWrap(True)
        lay.addWidget(self.lbl_top_apps)

        self.lbl_scan_state = QLabel("")
        self.lbl_scan_state.setStyleSheet("color:#666;")
        self.lbl_scan_state.setWordWrap(True)
        lay.addWidget(self.lbl_scan_state)

        # ---- 列表 ----
        # 第 1 列（所在位置）随窗口宽度弹性伸缩；其余列按当前字体实际宽度计算，
        # 保证任何系统字号/DPI 下「新增日期 2026.11.01」「新增时间 00:00~23:59」
        # 都完整显示、绝不被省略号截断
        self.today_tree = _ElasticTree(elastic_col=1, fixed_sum=656, min_elastic=60)
        self.today_tree.setColumnCount(9)
        self.today_tree.setHeaderLabels(
            ["选择", "所在位置 / 内容", "来源应用", "新增日期", "新增时间",
             "大小", "占盘", "文件数", "移到别的盘会怎样？"])
        self.today_tree.setColumnWidth(1, 240)
        self.today_tree.setRootIsDecorated(True)
        self.today_tree.setAlternatingRowColors(True)
        self.today_tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.today_tree.setSortingEnabled(True)
        # 长路径/长文本省略号截断，保证所有列都在可视宽度内、不挤出横向滚动条
        self.today_tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.today_tree.header().setMinimumSectionSize(36)
        _fm = self.today_tree.fontMetrics()

        def _tw(sample, pad=30):
            return _fm.horizontalAdvance(sample) + pad

        _w0 = _tw("选择", 26)
        _w2 = _tw("microsoft", 24)
        _w3 = _tw("2026.11.01")
        _w4 = _tw("00:00~23:59")
        _w5 = _tw("999.9 GB", 24)
        _w6 = _tw("100.0%", 24)
        _w7 = _tw("999 个文件", 24)
        _w8 = max(_tw("移到别的盘会怎样？"), _tw("系统文件·千万别动", 24))
        for _ci, _cw in ((0, _w0), (2, _w2), (3, _w3), (4, _w4),
                         (5, _w5), (6, _w6), (7, _w7), (8, _w8)):
            self.today_tree.setColumnWidth(_ci, _cw)
        # 弹性组件里的固定列总宽同步成真实值
        self.today_tree._fixed_sum = _w0 + _w2 + _w3 + _w4 + _w5 + _w6 + _w7 + _w8
        self.today_tree.headerItem().setToolTip(
            8, "这一列是给不懂电脑的朋友看的：告诉你把这块内容搬到别的盘之后，\n"
               "会不会让软件用不了。绿色＝放心搬，红色＝千万别动。")
        self.today_tree.header().setToolTip(
            "点表头可以排序：占用 / 新增时间 / 来源应用，想怎么排就怎么排")
        self.today_tree.itemDoubleClicked.connect(self._on_today_double_click)
        self.today_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.today_tree.customContextMenuRequested.connect(self._today_menu)
        lay.addWidget(self.today_tree, 1)

        # ---- 列表正下方：移动说明图例 ----
        self.lbl_move_legend = QLabel(guard_today.move_legend())
        self.lbl_move_legend.setWordWrap(True)
        self.lbl_move_legend.setStyleSheet(
            "background:#f7f9fb; border:1px solid #dde4ea; border-radius:4px;"
            "padding:6px 8px; font-size:12px;")
        self.lbl_move_legend.setToolTip(
            "看不懂没关系，记住两条就够了：\n"
            "绿色和蓝色＝可以搬；橙色＝先退出软件再搬；红色＝不要碰。")
        lay.addWidget(self.lbl_move_legend)

        # ---- 底部操作条 ----
        bot = QHBoxLayout()
        self.combo_target = QComboBox()
        self.combo_target.setMinimumWidth(170)
        self.combo_target.setToolTip("把勾选的内容搬到哪个盘（列表里会自动排掉当前这个盘）")
        b_sel_all = QPushButton("全选")
        b_sel_all.clicked.connect(lambda: self._check_all_today(True))
        b_sel_none = QPushButton("全不选")
        b_sel_none.clicked.connect(lambda: self._check_all_today(False))
        b_open = QPushButton("📂 打开所在位置")
        b_open.setMinimumHeight(30)
        b_open.clicked.connect(self.open_selected_location)
        b_open.setToolTip("在资源管理器中打开选中行的真实位置（也可双击行）")
        b_move = QPushButton("📦 一键移动所选")
        b_move.setMinimumHeight(34)
        b_move.setStyleSheet("background:#1565c0; color:white; font-weight:bold;")
        b_move.clicked.connect(self.move_selected_today)
        bot.addWidget(QLabel("移动到："))
        bot.addWidget(self.combo_target)
        bot.addWidget(b_sel_all)
        bot.addWidget(b_sel_none)
        bot.addWidget(b_open)
        bot.addStretch(1)
        bot.addWidget(b_move)
        lay.addLayout(bot)
        return w

    # ---------- 存量盘点：装守卫之前，各块盘里已经存了什么 ----------
    def _build_legacy_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        lay.addLayout(self._page_head(
            "存量盘点", "装 C 盘守卫之前，各块盘里已经存了什么"))

        # ---- 小白提示卡：先讲清楚这个页面是干什么的 ----
        tip = QLabel(
            "💡 这个页面帮你做三件事："
            "① 看清这台电脑一共有几块盘、每块盘用了多少；"
            "② 分清哪些是「装上守卫之前就有」的老数据、哪些是装上之后新增的；"
            "③ 告诉你每一块内容是什么、能不能搬到别的盘。")
        tip.setWordWrap(True)
        tip.setStyleSheet(
            "background:#eef6ff;border:1px solid #cfe3fa;border-radius:6px;"
            "padding:8px 10px;font-size:12.5px;color:#1b3f66;")
        lay.addWidget(tip)

        # ---- 第一行：分界日期 + 看哪块盘 + 按钮 ----
        top = QHBoxLayout()
        top.setSpacing(8)
        top.addWidget(QLabel("分界日期："))
        self.date_legacy = QDateEdit()
        self.date_legacy.setCalendarPopup(True)
        self.date_legacy.setDisplayFormat("yyyy.MM.dd")
        self.date_legacy.setFixedWidth(118)
        d0 = datetime.date.fromtimestamp(self._install["ts"])
        self.date_legacy.setDate(QDate(d0.year, d0.month, d0.day))
        self.date_legacy.setToolTip(
            "「装守卫之前」是按这个日期算的：文件放进来的时间早于这一天，"
            "就算「装守卫前就有的老数据」。\n"
            "默认填的是本程序第一次出现在这台电脑上的日期"
            f"（{guard_legacy.fmt_ts(self._install['ts'])}，{self._install['source']}）。\n"
            "想换个分界点，直接改这个日期就行，下面的数字会立刻重算，不用重新扫描。")
        self.date_legacy.dateChanged.connect(self._on_baseline_changed)
        top.addWidget(self.date_legacy)
        lab_b = QLabel("之前 = 老数据")
        lab_b.setStyleSheet(f"color:{GT.C['ink3']};font-size:{GT.human_font(12)}pt;")
        lab_b.setToolTip("文件的创建时间早于这个日期，就算「装守卫之前就有的老数据」")
        top.addWidget(lab_b)

        top.addSpacing(10)
        top.addWidget(QLabel("看哪块盘："))
        self.combo_legacy_drive = QComboBox()
        # 盘符很短，固定一个稳妥的最小宽度即可保证文字完整
        self.combo_legacy_drive.setMinimumWidth(110)
        self.combo_legacy_drive.setSizePolicy(QSizePolicy.Policy.Preferred,
                                              QSizePolicy.Policy.Fixed)
        self.combo_legacy_drive.setToolTip(
            "列出这台电脑上所有能用的盘。换一个盘，下面显示的就是那块盘的家底。")
        self.combo_legacy_drive.currentIndexChanged.connect(self._on_legacy_drive_changed)
        top.addWidget(self.combo_legacy_drive)

        top.addStretch(1)
        self.btn_legacy_scan = QPushButton("盘点这块盘")
        self.btn_legacy_scan.setMinimumHeight(30)
        self.btn_legacy_scan.setToolTip("只盘点上面选中的这一块盘，一般一两分钟")
        self.btn_legacy_scan.clicked.connect(lambda: self._start_legacy_scan(False))
        self.btn_legacy_scan_all = QPushButton("盘点所有盘")
        self.btn_legacy_scan_all.setMinimumHeight(30)
        self.btn_legacy_scan_all.setToolTip(
            "把本机所有盘都盘点一遍。盘多、文件多的时候会慢一些，需要耐心等几分钟")
        self.btn_legacy_scan_all.clicked.connect(lambda: self._start_legacy_scan(True))
        self.btn_legacy_stop = QPushButton("停止")
        self.btn_legacy_stop.setMinimumHeight(30)
        self.btn_legacy_stop.setEnabled(False)
        self.btn_legacy_stop.clicked.connect(self._stop_legacy_scan)
        top.addWidget(self.btn_legacy_scan)
        top.addWidget(self.btn_legacy_scan_all)
        top.addWidget(self.btn_legacy_stop)
        lay.addLayout(top)

        # ---- 一句话总结 ----
        self.lbl_legacy_summary = QLabel("")
        self.lbl_legacy_summary.setWordWrap(True)
        self.lbl_legacy_summary.setStyleSheet(
            f"font-size:{GT.human_font(14)}pt;font-weight:700;color:{GT.C['ink']};")
        lay.addWidget(self.lbl_legacy_summary)

        self.lbl_legacy_state = QLabel("")
        self.lbl_legacy_state.setWordWrap(True)
        self.lbl_legacy_state.setStyleSheet(f"color:{GT.C['ink3']};font-size:{GT.human_font(12)}pt;")
        lay.addWidget(self.lbl_legacy_state)

        # ---- 全盘总览：每块盘一行 ----
        lay.addWidget(GT.section_title("各块盘的总账"))
        self.legacy_overview = QTableWidget(0, 6)
        self.legacy_overview.setHorizontalHeaderLabels(
            ["盘", "已用 / 总共", "占用", "装守卫前就有", "装上后新增", "说明"])
        self.legacy_overview.verticalHeader().setVisible(False)
        self.legacy_overview.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.legacy_overview.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.legacy_overview.setAlternatingRowColors(True)
        self.legacy_overview.setFixedHeight(150)
        # 窗口变窄时，多余文字用省略号截断，不把页面撑出横向裁切
        self.legacy_overview.setTextElideMode(Qt.TextElideMode.ElideRight)
        hh = self.legacy_overview.horizontalHeader()
        hh.setMinimumSectionSize(28)
        hh.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        for i, wd in enumerate((150, 132, 52, 104, 100)):
            self.legacy_overview.setColumnWidth(i, wd)
        self.legacy_overview.setToolTip(
            "每一行是一块盘。点某一行，下面的明细就切到那块盘。\n"
            "「装守卫前就有 / 装上后新增」这两列要盘点过才有数字。")
        self.legacy_overview.cellClicked.connect(self._on_overview_clicked)
        lay.addWidget(self.legacy_overview)

        # ---- 明细：这块盘里具体是什么 ----
        det_head = QHBoxLayout()
        det_head.addWidget(GT.section_title("这块盘里都有什么（大的排前面）"))
        det_head.addStretch(1)
        b_open = QPushButton("📂 打开所在位置")
        b_open.setMinimumHeight(28)
        b_open.clicked.connect(self._legacy_open)
        b_open.setToolTip("在资源管理器里打开选中这一行所在的文件夹（也可以直接双击）")
        b_copy = QPushButton("📋 复制路径")
        b_copy.setMinimumHeight(28)
        b_copy.clicked.connect(self._legacy_copy)
        b_copy.setToolTip("把选中这一行的完整路径复制下来")
        b_go = QPushButton("➡️ 去看今天新增了什么")
        b_go.setMinimumHeight(28)
        b_go.setToolTip("切到「今日新增」页面，看看装上守卫之后又多了些什么")
        b_go.clicked.connect(lambda: self._goto_page("today"))
        det_head.addWidget(b_open)
        det_head.addWidget(b_copy)
        det_head.addWidget(b_go)
        lay.addLayout(det_head)
        # 固定列宽合计 634，第 0 列（在哪儿）随窗口宽度弹性伸缩
        self.legacy_tree = _ElasticTree(elastic_col=0, fixed_sum=634)
        self.legacy_tree.setColumnCount(8)
        self.legacy_tree.setHeaderLabels(
            ["在哪儿（文件夹）", "是什么", "多大", "占这盘多少", "多少个文件",
             "什么时候放进来的", "装守卫前就有？", "能搬到别的盘吗"])
        self.legacy_tree.setColumnWidth(0, 240)
        self.legacy_tree.setRootIsDecorated(False)
        self.legacy_tree.setAlternatingRowColors(True)
        self.legacy_tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.legacy_tree.setSortingEnabled(True)
        self.legacy_tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.legacy_tree.header().setMinimumSectionSize(36)
        for i, wd in enumerate((0, 104, 72, 52, 62, 108, 100, 136)):
            if i:
                self.legacy_tree.setColumnWidth(i, wd)
        self.legacy_tree.headerItem().setToolTip(
            1, "把路径翻译成一句人话：这是系统文件、软件、还是你自己的资料")
        self.legacy_tree.headerItem().setToolTip(
            6, "按「分界日期」算：这一块里有多少是装守卫之前就有的")
        self.legacy_tree.headerItem().setToolTip(
            7, "搬走会不会出问题。绿色＝放心搬，红色＝不要动")
        self.legacy_tree.itemDoubleClicked.connect(self._legacy_open)
        self.legacy_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.legacy_tree.customContextMenuRequested.connect(self._legacy_menu)
        lay.addWidget(self.legacy_tree, 1)

        # 分类图例
        self.lbl_legacy_legend = QLabel(self._legacy_legend())
        self.lbl_legacy_legend.setWordWrap(True)
        self.lbl_legacy_legend.setStyleSheet(
            "background:#f7f9fb;border:1px solid #dde4ea;border-radius:4px;"
            "padding:6px 8px;font-size:12px;")
        lay.addWidget(self.lbl_legacy_legend)
        return w

    @staticmethod
    def _legacy_legend() -> str:
        """「是什么」这一列的颜色图例，一句话讲清各种颜色。"""
        parts = []
        for k, m in guard_legacy.KINDS.items():
            parts.append(f"<span style='color:{m['color']};font-weight:bold'>"
                         f"{m['name']}</span>")
        return ("是什么： " + "　｜　".join(parts) +
                "　　（绿色＝你自己的资料，放心搬；红色＝不要动）")

    # ---- 基准日期 ----
    def _legacy_baseline(self) -> float:
        """分界日期当天 0 点的时间戳。"""
        d = self.date_legacy.date()
        try:
            return datetime.datetime(d.year(), d.month(), d.day()).timestamp()
        except Exception:
            return time.time()

    def _legacy_drive(self) -> str:
        return (self.combo_legacy_drive.currentData()
                or getattr(self, "_cur_drive", None)
                or guard_drives.system_letter())

    def _on_baseline_changed(self, _d=None):
        """改了分界日期：用已存的盘点结果立刻重算，不用重新扫描。"""
        # 记住用户自己定的分界日，下次打开还是这个
        try:
            guard_legacy.save_install_time(APP_DIR, self._legacy_baseline(),
                                           "手动设置的分界日期")
        except Exception:
            pass
        self._fill_legacy_overview()
        self._fill_legacy_rows()

    def _reload_legacy_drive_combo(self):
        keep = getattr(self, "_legacy_cur", None) or guard_drives.system_letter()
        self.combo_legacy_drive.blockSignals(True)
        self.combo_legacy_drive.clear()
        idx = 0
        for i, d in enumerate(getattr(self, "_drive_infos", [])):
            self.combo_legacy_drive.addItem(f"{d['letter']} 盘", d["letter"])
            if d["letter"] == keep:
                idx = i
        if self.combo_legacy_drive.count() == 0:
            self.combo_legacy_drive.addItem("（没有检测到可用磁盘）", "")
        self.combo_legacy_drive.setCurrentIndex(idx)
        self.combo_legacy_drive.blockSignals(False)
        self._legacy_cur = self.combo_legacy_drive.currentData() or keep

    def _on_legacy_drive_changed(self, _i=None):
        self._legacy_cur = self.combo_legacy_drive.currentData() or self._legacy_cur
        self._fill_legacy_overview()
        self._fill_legacy_rows()

    def _on_overview_clicked(self, row, _col):
        """点总览里某一行 -> 明细切到那块盘。"""
        it = self.legacy_overview.item(row, 0)
        if it is None:
            return
        letter = it.data(Qt.ItemDataRole.UserRole)
        if not letter:
            return
        for i in range(self.combo_legacy_drive.count()):
            if self.combo_legacy_drive.itemData(i) == letter:
                self.combo_legacy_drive.setCurrentIndex(i)
                break

    # ---- 填充 ----
    def _fill_legacy_overview(self):
        """上面那张「各块盘的总账」。"""
        base = self._legacy_baseline()
        rows = guard_legacy.overview(APP_DIR, base, getattr(self, "_drive_infos", None))
        t = self.legacy_overview
        t.setRowCount(len(rows))
        for i, d in enumerate(rows):
            name = f"{d['name']}（{d['type_name']}）"
            vals = [
                name,
                f"{engine.human_size(d['used'])} / {engine.human_size(d['total'])}",
                f"{d['pct']:.0f}%",
                ("—" if d["before"] is None else engine.human_size(d["before"])),
                ("—" if d["after"] is None else engine.human_size(d["after"])),
            ]
            if not d["scanned"]:
                note = "还没盘点"
            else:
                note = f"{d['health']['icon']}{d['health']['level']} 已看清 {engine.human_size(d['found'])}"
                if d["pct"] >= 88:
                    note += " · 告急"
                elif d["pct"] >= 75:
                    note += " · 偏高"
            vals.append(note)
            for j, v in enumerate(vals):
                cell = QTableWidgetItem(v)
                if j == 0:
                    cell.setData(Qt.ItemDataRole.UserRole, d["letter"])
                    cell.setToolTip(f"{d['name']}　{d['type_name']}")
                if j == 2:
                    cell.setForeground(QColor(d["health"]["color"]))
                if j == 3 and d["before"]:
                    cell.setForeground(QColor("#1565c0"))
                if j == 4 and d["after"]:
                    cell.setForeground(QColor("#c62828"))
                if j == 5:
                    cell.setForeground(QColor("#888") if not d["scanned"]
                                       else QColor(d["health"]["color"]))
                if d["letter"] == self._legacy_drive():
                    cell.setBackground(QColor(GT.C["green_soft"]))
                t.setItem(i, j, cell)
        # 当前盘的一句话总结
        cur = None
        for d in rows:
            if d["letter"] == self._legacy_drive():
                cur = d
                break
        if cur is not None:
            self.lbl_legacy_summary.setText(
                guard_legacy.plain_summary(cur, base))
        if rows:
            never = [d["letter"] for d in rows if not d["scanned"]]
            if never and all(not d["scanned"] for d in rows):
                self.lbl_legacy_state.setText(
                    "还没有盘点过任何一块盘。点右上角的「🔍 盘点这块盘」，"
                    "第一次大概需要一到三分钟，扫完就一直能看了。")
            elif never:
                self.lbl_legacy_state.setText(
                    f"已盘点：{'、'.join(d['letter'] + ' 盘' for d in rows if d['scanned'])}　｜　"
                    f"还没盘点：{'、'.join(x + ' 盘' for x in never)}（想看就点「所有盘都盘一遍」）")
            else:
                self.lbl_legacy_state.setText(
                    "所有盘都盘点过了。改上面的日期可以随时换分界点，数字立刻重算。")

    def _fill_legacy_rows(self):
        """下面的明细：这块盘里每一块内容是什么。"""
        t = self.legacy_tree
        t.clear()
        letter = self._legacy_drive()
        base = self._legacy_baseline()
        sc = guard_legacy.LegacyScanner(APP_DIR, letter)
        snap = sc.load()
        rows = snap.get("rows", []) or []
        dinfo = None
        for d in getattr(self, "_drive_infos", []):
            if d["letter"] == letter:
                dinfo = d
                break
        used = (dinfo["used"] if dinfo else 0) or 0

        for r in rows:
            path = r["path"]
            if os.sep == "\\":                 # 快照里可能存着 C:/xxx 的写法
                path = path.replace("/", "\\")
            # 盘根目录下的散文件，给一个一看就懂的名字
            show = path
            if len(path.rstrip("\\")) == 2 and path.rstrip("\\").endswith(":"):
                show = f"{path}（这块盘根目录下散落的文件）"
            size = int(r.get("size", 0))
            before, after = guard_legacy.split_by(r, base)
            k = guard_legacy.KINDS.get(r.get("kind") or "other", guard_legacy.KINDS["other"])
            cm = guard_legacy.can_move_text(path, guard_drives.system_letter())
            pct = (size / used * 100) if used else 0.0
            old, new = r.get("oldest"), r.get("newest")
            if old and new and guard_legacy.fmt_ts(old) == guard_legacy.fmt_ts(new):
                when = guard_legacy.fmt_ts(old)
            elif old and new:
                when = f"{guard_legacy.fmt_ts(old)} ~ {guard_legacy.fmt_ts(new)}"
            else:
                when = guard_legacy.fmt_ts(old or new)
            if before and not after:
                age = "全是老数据"
            elif after and not before:
                age = "全是新数据"
            else:
                tot = before + after or 1
                age = f"老 {before/tot*100:.0f}% ＋ 新 {after/tot*100:.0f}%"

            it = SortItem([show, k["name"], engine.human_size(size),
                           f"{pct:.1f}%" if pct >= 0.1 else "",
                           f"{int(r.get('count', 0))} 个",
                           when, age, cm["text"]])
            it.setData(0, Qt.UserRole, path)
            it.set_key(0, path.lower())
            it.set_key(1, k["name"])
            it.set_key(2, size)
            it.set_key(3, pct)
            it.set_key(4, int(r.get("count", 0)))
            it.set_key(5, float(old or 0))
            it.set_key(6, (before / size) if size else 0)
            it.set_key(7, cm["order"])
            it.setToolTip(0, path + "\n（双击打开这个文件夹）")
            it.setToolTip(1, k["tip"])
            it.setToolTip(2, f"{engine.human_size(size)}（{size:,} 字节）")
            it.setToolTip(3, f"占 {letter} 盘已用空间的 {pct:.2f}%")
            it.setToolTip(5, f"最早放进来的：{guard_legacy.fmt_dt(old) if old else '—'}\n"
                             f"最近一次改动：{guard_legacy.fmt_dt(new) if new else '—'}")
            it.setToolTip(6, f"装守卫前就有 {engine.human_size(before)}\n"
                             f"装上之后新增 {engine.human_size(after)}\n"
                             f"（分界日期 {guard_legacy.fmt_ts(base)}，按月份统计）")
            it.setToolTip(7, cm["text"] + "\n" + k["tip"])
            it.setForeground(1, QColor(k["color"]))
            it.setForeground(7, QColor(cm["color"]))
            if before and not after:
                it.setForeground(6, QColor("#1565c0"))
            elif after and not before:
                it.setForeground(6, QColor("#c62828"))
            t.addTopLevelItem(it)

        if not rows:
            self.lbl_legacy_state.setText(
                f"{letter} 盘还没盘点过。点上面的「🔍 盘点这块盘」，"
                f"扫完就能看清里面都是些什么。")
        t.sortItems(2, Qt.SortOrder.DescendingOrder)

    # ---- 扫描 ----
    def _start_legacy_scan(self, all_drives=False):
        if self.legacy_thread and self.legacy_thread.is_alive():
            return
        base = self._legacy_baseline()
        letters = ([d["letter"] for d in getattr(self, "_drive_infos", [])]
                   if all_drives else [self._legacy_drive()])
        if not letters:
            return
        self.btn_legacy_scan.setEnabled(False)
        self.btn_legacy_scan_all.setEnabled(False)
        self.btn_legacy_stop.setEnabled(True)
        self.lbl_legacy_state.setText(
            f"正在盘点 {'、'.join(letters)} 盘（后台进行，不影响守护）…"
            "盘里文件多的时候需要几分钟，可以先做别的事")
        cancel = threading.Event()
        self._legacy_cancel = cancel

        def work():
            done = []
            for L in letters:
                if cancel.is_set():
                    break
                sc = guard_legacy.LegacyScanner(APP_DIR, L)
                try:
                    data = sc.scan(
                        base,
                        progress_cb=lambda n, cur, _L=L:
                            self.legacy_progress.emit(f"{_L} 盘：已发现 {n} 处 | {cur}"),
                        cancel_evt=cancel)
                except Exception as e:
                    data = {"rows": [], "total": 0, "count": 0, "err": str(e),
                            "drive": L, "baseline": base, "ts": time.time()}
                sc.save(data)
                done.append(L)
            self.legacy_done.emit({"letters": done, "all": all_drives,
                                   "canceled": cancel.is_set()})

        self.legacy_thread = threading.Thread(target=work, daemon=True)
        self.legacy_thread.start()

    def _stop_legacy_scan(self):
        if self._legacy_cancel is not None:
            self._legacy_cancel.set()
        self.lbl_legacy_state.setText("正在停止…（已经盘完的盘会保留）")

    def _on_legacy_progress(self, text):
        self.lbl_legacy_state.setText("盘点中… " + text)

    def _on_legacy_done(self, res):
        self.btn_legacy_scan.setEnabled(True)
        self.btn_legacy_scan_all.setEnabled(True)
        self.btn_legacy_stop.setEnabled(False)
        letters = res.get("letters", [])
        if res.get("canceled"):
            self.lbl_legacy_state.setText(
                "已停止。这次盘完："
                + ("、".join(x + " 盘" for x in letters) if letters else "（无）"))
        else:
            self.lbl_legacy_state.setText(
                f"盘点完成 {datetime.datetime.now():%H:%M:%S}｜"
                f"这次盘了 {'、'.join(x + ' 盘' for x in letters) if letters else '—'}")
        self._reload_drive_combo()
        self._fill_legacy_overview()
        self._fill_legacy_rows()

    # ---- 行操作 ----
    def _legacy_selected_path(self):
        it = self.legacy_tree.currentItem()
        if it is None:
            return None
        return it.data(0, Qt.UserRole) or it.text(0)

    def _legacy_open(self, *_a):
        p = self._legacy_selected_path()
        if not p:
            return
        try:
            if os.path.isdir(p):
                os.startfile(p)
            else:
                subprocess.Popen(["explorer", "/select,", os.path.normpath(p)])
        except Exception as e:
            QMessageBox.information(self, APP_NAME, f"打不开这个位置：\n{p}\n\n{e}")

    def _legacy_copy(self):
        p = self._legacy_selected_path()
        if not p:
            return
        QApplication.clipboard().setText(p)
        self.lbl_legacy_state.setText(f"已复制路径：{p}")

    def _legacy_menu(self, pos):
        it = self.legacy_tree.itemAt(pos)
        if it is None:
            return
        self.legacy_tree.setCurrentItem(it)
        menu = QMenu(self)
        a1 = QAction("📂 打开所在位置", self)
        a1.triggered.connect(self._legacy_open)
        a2 = QAction("📋 复制完整路径", self)
        a2.triggered.connect(self._legacy_copy)
        menu.addAction(a1)
        menu.addAction(a2)
        menu.exec(self.legacy_tree.viewport().mapToGlobal(pos))

    # ---------- 磁盘下拉 ----------
    @staticmethod
    def _fit_combo_width(combo, cap=460, extra=46):
        """按下拉框当前字体量出最长一项的宽度并设为最小宽，
        保证任何机器、任何字号下文字都完整显示、不被裁切。"""
        try:
            fm = combo.fontMetrics()
            need = max(
                (fm.horizontalAdvance(combo.itemText(i))
                 for i in range(combo.count())),
                default=0) + extra
            combo.setMinimumWidth(min(need, cap))
        except Exception:
            pass

    def _reload_drive_combo(self):
        """重新枚举本机磁盘，填进下拉框（并把当前选择保留下来）。"""
        self._drive_infos = guard_drives.list_drives()
        keep = getattr(self, "_cur_drive", None) or guard_drives.system_letter()
        self.combo_drive.blockSignals(True)
        self.combo_drive.clear()
        idx = 0
        for i, d in enumerate(self._drive_infos):
            # 下拉框只放短文本（详细容量见下方健康行），保证任何窗口宽度下文字完整不裁切
            gb = 2 ** 30
            tag = f" · {d['tag']}" if d.get("tag") else ""
            sys_tag = " · 系统" if d.get("is_system") else ""
            short = (f"{d['letter']} 盘{tag}{sys_tag}"
                     f"（剩 {d['free']/gb:.1f} GB / 共 {d['total']/gb:.1f} GB）")
            self.combo_drive.addItem(short, d["letter"])
            self.combo_drive.setItemData(
                i, guard_drives.brief(d), Qt.ItemDataRole.ToolTipRole)
            if d["letter"] == keep:
                idx = i
        if self.combo_drive.count() == 0:
            self.combo_drive.addItem("（没有检测到可用磁盘）", "")
        self.combo_drive.setCurrentIndex(idx)
        self.combo_drive.blockSignals(False)
        # 按当前字体量出最长一项的宽度，设成下拉框最小宽：
        # 任何机器、任何字号下，盘名文字都完整显示、不被裁切
        self._fit_combo_width(self.combo_drive)
        self._cur_drive = self.combo_drive.currentData() or keep
        if self.scanner is not None:
            self.scanner.set_drive(self._cur_drive)
        self._refresh_drive_bar()
        self._refresh_target_combo()
        # 存量盘点页面的盘符下拉同步刷新
        try:
            self._reload_legacy_drive_combo()
        except Exception:
            pass

    def _on_drive_changed(self, _idx=None):
        letter = self.combo_drive.currentData()
        if not letter:
            return
        self._cur_drive = letter
        self.scanner.set_drive(letter)
        self._refresh_drive_bar()
        self._refresh_target_combo()
        # 立刻显示这块盘上一次的扫描结果（没有也不报错）
        try:
            data = self.scanner.load()
            self._fill_today(data.get("items", []) or [])
            n = len(data.get("items", []) or [])
            self.lbl_scan_state.setText(
                f"已切换到 {letter} 盘｜显示上次结果（{n} 项），"
                f"点「🔄 扫描今日新增」可重新扫一遍")
        except Exception:
            pass

    def _cur_drive_info(self):
        for d in getattr(self, "_drive_infos", []):
            if d["letter"] == getattr(self, "_cur_drive", None):
                return d
        return None

    def _refresh_drive_bar(self):
        d = self._cur_drive_info()
        if d is None:
            self.bar_drive.setValue(0)
            self.lbl_drive.setText("")
            return
        self.bar_drive.setValue(int(d["pct"]))
        color = d["health"]["color"]
        self.bar_drive.setStyleSheet(f"QProgressBar::chunk {{ background:{color}; }}")
        self.lbl_drive.setText(
            f"{d['name']}　{d['type_name']}　"
            f"已用 {engine.human_size(d['used'])} / 共 {engine.human_size(d['total'])}"
            f"（{d['pct']:.1f}%）　剩余 {engine.human_size(d['free'])}　"
            f"{d['health']['icon']}{d['health']['level']}"
            + ("　⚠️ 空间告急，建议清理或搬移" if d["pct"] >= 88 else
               "　（占用偏高，留意一下）" if d["pct"] >= 75 else ""))

    def _refresh_target_combo(self):
        """目标盘下拉：列出除当前盘外的所有盘，按剩余空间从大到小。"""
        cur = getattr(self, "_cur_drive", None)
        alts = [d for d in getattr(self, "_drive_infos", [])
                if d["letter"] != cur and d["free"] > 0]
        alts.sort(key=lambda x: -x["free"])
        self._drives = []
        self.combo_target.blockSignals(True)
        self.combo_target.clear()
        for d in alts:
            dest = f"{d['root']}C盘守卫接收"
            self._drives.append(dest)
            self.combo_target.addItem(
                f"{d['letter']} 盘（剩余 {engine.human_size(d['free'])}）", dest)
        if not self._drives:
            self.combo_target.addItem("（没有其他可用盘）", "")
        self._fit_combo_width(self.combo_target, cap=340)

    # ---------- 排序 ----------
    def _apply_sort(self, _idx=None):
        mode = self.combo_sort.currentData()
        if not mode:
            return
        mapping = {
            "size_desc": (5, Qt.SortOrder.DescendingOrder),
            "size_asc": (5, Qt.SortOrder.AscendingOrder),
            "time_desc": (4, Qt.SortOrder.DescendingOrder),
            "time_asc": (4, Qt.SortOrder.AscendingOrder),
            "app": (2, Qt.SortOrder.AscendingOrder),
            "dir": (1, Qt.SortOrder.AscendingOrder),
            "risk": (8, Qt.SortOrder.DescendingOrder),
        }
        col, order = mapping.get(mode, (5, Qt.SortOrder.DescendingOrder))
        self.today_tree.sortItems(col, order)

    def start_scan(self):
        if self.scan_thread and self.scan_thread.is_alive():
            return
        self.btn_scan.setEnabled(False)
        drv = getattr(self, "_cur_drive", "C")
        self.lbl_scan_state.setText(
            f"正在扫描 {drv} 盘（后台进行，不影响守护）…" +
            ("（整盘扫描可能需要几分钟，可以先做别的事）" if drv != guard_drives.system_letter() else ""))
        cancel = threading.Event()
        self._scan_cancel = cancel

        def work():
            try:
                data = self.scanner.scan(progress_cb=lambda n, cur:
                                         self.scan_progress.emit(n, cur),
                                         cancel_evt=cancel)
            except Exception as e:
                data = {"items": [], "total_size": 0, "count": 0,
                        "err": str(e)}
            data = dict(data)
            data["day"] = datetime.date.today().isoformat()
            self.scanner.save(data)
            self.scan_done.emit(data)

        self.scan_thread = threading.Thread(target=work, daemon=True)
        self.scan_thread.start()

    def _on_scan_progress(self, n, cur):
        # 路径太长时中间省略，避免把整页撑宽、把别的文字挤出屏幕
        cur = str(cur or "")
        if len(cur) > 66:
            cur = cur[:30] + " … " + cur[-30:]
        self.lbl_scan_state.setText(f"扫描中… 已发现 {n} 项 | {cur}")

    def _on_scan_done(self, data):
        self.btn_scan.setEnabled(True)
        drv = getattr(self, "_cur_drive", "")
        self.lbl_scan_state.setText(
            f"{drv} 盘扫描完成 {datetime.datetime.now():%H:%M:%S}｜共 {data['count']} 项，"
            f"合计 {engine.human_size(data['total_size'])}")
        self._refresh_drive_bar()
        self._fill_today(data.get("items", []))

    @staticmethod
    def _fmt_time(ts):
        """完整时间：2026.11.01  00:30"""
        try:
            return datetime.datetime.fromtimestamp(float(ts)).strftime("%Y.%m.%d  %H:%M")
        except Exception:
            return ""

    @staticmethod
    def _fmt_date(ts):
        """仅日期：2026.11.01"""
        try:
            return datetime.datetime.fromtimestamp(float(ts)).strftime("%Y.%m.%d")
        except Exception:
            return ""

    @staticmethod
    def _fmt_hm(ts):
        """仅时分：00:30"""
        try:
            return datetime.datetime.fromtimestamp(float(ts)).strftime("%H:%M")
        except Exception:
            return ""

    def _group_date_text(self, r):
        """分组的「新增日期」列：同一天只显示一个日期；跨天显示日期区间。"""
        f, l = r.get("first_ts"), r.get("last_ts")
        if not (f or l):
            return ""
        if not (f and l):
            return self._fmt_date(f or l)
        try:
            d1 = datetime.datetime.fromtimestamp(float(f)).date()
            d2 = datetime.datetime.fromtimestamp(float(l)).date()
        except Exception:
            return self._fmt_date(f or l)
        if d1 == d2:
            return self._fmt_date(f)
        # 跨天区间：同年时后段省略年份，紧凑显示（2026.10.30~11.01）
        if d1.year == d2.year:
            return (f"{self._fmt_date(f)}~"
                    f"{datetime.datetime.fromtimestamp(float(l)).strftime('%m.%d')}")
        return f"{self._fmt_date(f)}~{self._fmt_date(l)}"

    def _group_hm_text(self, r):
        """分组的「新增时间」列：同一时刻只显示一个；否则显示 最早~最晚（紧凑写法）。"""
        f, l = r.get("first_ts"), r.get("last_ts")
        if not (f or l):
            return ""
        if not (f and l) or abs(float(l) - float(f)) < 60:
            return self._fmt_hm(f or l)
        return f"{self._fmt_hm(f)}~{self._fmt_hm(l)}"

    def _fill_today(self, items):
        self.today_tree.clear()
        self._today_items = items
        by_path = {it["path"]: it for it in items}
        rows = guard_today.TodayScanner.aggregate(items)

        # 占盘百分比的分母：这块盘已用掉的空间
        dinfo = self._cur_drive_info()
        denom = (dinfo["used"] if dinfo else 0) or 0
        denom_gb = denom / 2 ** 30 if denom else 0

        apps = {}
        for it in items:
            apps[it["app"]] = apps.get(it["app"], 0) + it.get("size", 0)
        top = sorted(apps.items(), key=lambda x: -x[1])[:5]
        drv = getattr(self, "_cur_drive", "")
        total = sum(i.get("size", 0) for i in items)
        head = f"{drv} 盘今日新增：" if drv else "今日新增："
        self.lbl_today_summary.setText(
            f"{head}{len(items)} 项 / {engine.human_size(total)}"
            f"　（占该盘已用空间的 "
            f"{(total/denom*100 if denom else 0):.1f}%）")
        # 主要来源单独一行显示，避免和总量挤在一起折行重叠
        self.lbl_top_apps.setText(
            ("主要来源：" + "、".join(
                f"{a}（{engine.human_size(s)}）" for a, s in top)) if top else "")

        for r in rows:
            pct = (r["size"] / denom * 100) if denom else 0.0
            pct_txt = f"{pct:.1f}%" if pct >= 0.1 else (
                "<0.1%" if r["size"] > 0 else "")
            ts = r.get("last_ts") or r.get("first_ts") or 0
            dstr = self._group_date_text(r)
            tstr = self._group_hm_text(r)
            risk = r.get("risk") or guard_today.move_risk(
                r["dir"], r["app"], bool(r.get("is_dir")),
                guard_drives.system_letter())

            node = SortItem(["", r["dir"], r["app"],
                             dstr,
                             tstr,
                             engine.human_size(r["size"]),
                             pct_txt,
                             f"{r['count']} 个文件",
                             risk["text"]])
            node.setFlags(node.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            node.setCheckState(0, Qt.CheckState.Unchecked)
            node.setData(0, Qt.UserRole, r)
            # 排序键：数值按数值，文本按文本
            node.set_key(1, r["dir"].lower())
            node.set_key(2, r["app"])
            node.set_key(3, float(ts or 0))
            node.set_key(4, float(ts or 0))
            node.set_key(5, int(r["size"]))
            node.set_key(6, pct)
            node.set_key(7, int(r["count"]))
            node.set_key(8, int(risk["order"]))
            node.setToolTip(1, r["dir"] + "\n（双击打开所在位置）")
            _tm_tip = (f"最早 {self._fmt_time(r.get('first_ts'))}"
                       f"\n最晚 {self._fmt_time(r.get('last_ts'))}")
            node.setToolTip(3, _tm_tip)
            node.setToolTip(4, _tm_tip)
            node.setToolTip(5, f"{engine.human_size(r['size'])}"
                               f"（{int(r['size']):,} 字节）")
            node.setToolTip(6, f"占 {drv} 盘已用空间 {denom_gb:.1f} GB 的 {pct:.2f}%")
            node.setToolTip(8, risk["tip"])
            node.setForeground(3, QColor("#1565c0"))
            node.setForeground(4, QColor("#1565c0"))
            node.setForeground(8, QColor(risk["color"]))
            if risk["order"] >= 3:
                node.setBackground(8, QColor("#fdecea"))
            if pct >= 10:
                node.setForeground(6, QColor("#c62828"))
            elif pct >= 2:
                node.setForeground(6, QColor("#ef6c00"))
            self.today_tree.addTopLevelItem(node)

            names = r.get("paths", [])
            all_paths = r.get("all_paths", [])
            for i, nm in enumerate(names):
                full = all_paths[i] if i < len(all_paths) else r["dir"]
                it = by_path.get(full, {})
                csize = int(it.get("size", 0) or 0)
                cpct = (csize / denom * 100) if denom else 0.0
                cts = guard_today.TodayScanner.display_ts(it) if it else 0
                child = SortItem(
                    ["", nm, r["app"],
                     self._fmt_date(cts) if it else "",
                     self._fmt_hm(cts) if it else "",
                     engine.human_size(csize) if it else "",
                     f"{cpct:.1f}%" if cpct >= 0.1 else "",
                     "",
                     risk["text"]])
                child.setFlags(child.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                child.setForeground(1, QColor("#888"))
                child.setForeground(4, QColor("#888"))
                child.setForeground(8, QColor(risk["color"]))
                child.setData(0, Qt.UserRole,
                              {"path": full,
                               "is_dir": bool(it.get("is_dir", r.get("is_dir")))})
                child.set_key(1, nm.lower())
                child.set_key(2, r["app"])
                child.set_key(3, float(cts or 0))
                child.set_key(4, float(cts or 0))
                child.set_key(5, csize)
                child.set_key(6, cpct)
                child.set_key(7, 0)
                child.set_key(8, int(risk["order"]))
                child.setToolTip(1, full)
                child.setToolTip(8, risk["tip"])
                node.addChild(child)

        self._apply_sort()

    # ---------- 右键：把选中的软件加入「不准存 C 盘」 ----------
    def _today_menu(self, pos):
        item = self.today_tree.itemAt(pos)
        if item is None:
            return
        data = item.data(0, Qt.UserRole) or {}
        path = data.get("path") or data.get("dir") or ""
        app = (item.text(2) or "").strip()
        if not path and not app:
            return
        menu = QMenu(self)
        act_limit = menu.addAction(f"🚫 禁止「{app or os.path.basename(path)}」存到 C 盘")
        act_open = menu.addAction("📂 打开所在位置")
        act = menu.exec(self.today_tree.viewport().mapToGlobal(pos))
        if act is act_limit:
            self._add_rule_from_item(app, path)
        elif act is act_open:
            self._open_item_location(item)

    def _add_rule_from_item(self, app, path):
        """用选中行的来源应用名 + 路径，快速建一条限制规则。"""
        name = (app or "").strip()
        if not name or name in ("未知来源",):
            name = os.path.basename(os.path.normpath(path)) or ""
        if not name:
            QMessageBox.information(self, APP_NAME, "没能认出这是哪个软件，请到设置页手动添加。")
            return
        kws = guard_rules._auto_keywords(name)
        # 从真实路径里补英文目录关键词（解决「中文名 vs 英文目录」匹配不上）
        for k in guard_rules._keywords_from_sample(path):
            if k not in kws:
                kws.append(k)
        dlg = AddRuleDialog(name, kws, sample_path=path, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        nm, kw = dlg.values()
        if not nm:
            return
        guard_rules.rules.add(nm, kw, sample_path=path)
        try:
            self._refresh_rules_label()
        except Exception:
            pass
        QMessageBox.information(
            self, APP_NAME,
            f"已添加限制：不允许「{nm}」存到 C 盘。\n\n"
            "以后它再往 C 盘写东西，会先问你，再由你选一个盘搬走。\n"
            "（可在「设置 → 软件存储限制」里随时修改或删除）")

    # ---------- 定位到 C 盘真实位置 ----------
    def _on_today_double_click(self, item, _col):
        self._open_item_location(item)

    def open_selected_location(self):
        item = self.today_tree.currentItem()
        if item is None:
            QMessageBox.information(self, APP_NAME,
                                    "请先在列表里选中一行（父行或子文件都可以）。")
            return
        self._open_item_location(item)

    def _open_item_location(self, item):
        data = item.data(0, Qt.UserRole)
        path, is_dir = None, False
        if isinstance(data, dict) and data.get("path"):
            path, is_dir = data["path"], bool(data.get("is_dir"))
        elif isinstance(data, dict) and data.get("dir"):
            path, is_dir = data["dir"], True
        if not path:
            return
        path = os.path.normpath(path)
        try:
            if is_dir or os.path.isdir(path):
                if not os.path.exists(path):
                    QMessageBox.warning(self, APP_NAME,
                                        f"这个位置已经不在了（可能已被移动或删除）：\n{path}")
                    return
                subprocess.Popen(["explorer", path])
            else:
                if os.path.exists(path):
                    subprocess.Popen(["explorer", "/select," + path])
                else:
                    parent = os.path.dirname(path)
                    if os.path.isdir(parent):
                        subprocess.Popen(["explorer", parent])
                    else:
                        QMessageBox.warning(self, APP_NAME,
                                            f"这个位置已经不在了（可能已被移动或删除）：\n{path}")
        except Exception as e:
            QMessageBox.warning(self, APP_NAME, f"无法打开位置：{e}")

    def _check_all_today(self, on):
        skipped = 0
        for i in range(self.today_tree.topLevelItemCount()):
            it = self.today_tree.topLevelItem(i)
            if on:
                # 「全选」不会替你勾上危险的：系统文件、云盘同步目录一律跳过
                rk = (it.data(0, Qt.UserRole) or {}).get("risk") or {}
                if int(rk.get("order", 0)) >= 3:
                    skipped += 1
                    continue
            it.setCheckState(
                0, Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
        if on and skipped:
            self.lbl_scan_state.setText(
                f"已全选安全项｜自动跳过 {skipped} 组不建议搬动的内容"
                f"（红色标记的那几行），它们需要你自己判断后再手动勾选")

    def move_selected_today(self):
        if not getattr(self, "_today_items", None):
            return
        targets = []
        for i in range(self.today_tree.topLevelItemCount()):
            it = self.today_tree.topLevelItem(i)
            if it.checkState(0) == Qt.CheckState.Checked:
                targets.append(it.data(0, Qt.UserRole))
        if not targets:
            QMessageBox.information(self, APP_NAME, "请先勾选要移动的行（勾选第一列）。")
            return
        dest_root = self.combo_target.currentData()
        if not dest_root:
            QMessageBox.warning(self, APP_NAME, "没有可移动到的目标盘。")
            return

        # ---- 安全兜底：勾选里混着「不建议搬」的，先说清楚再动 ----
        dangerous = [r for r in targets
                     if int((r.get("risk") or {}).get("order", 0)) >= 3]
        if dangerous:
            lines = []
            for r in dangerous[:6]:
                lines.append(f"· {r['dir']}\n　　{(r.get('risk') or {}).get('text','')}")
            more = f"\n…… 还有 {len(dangerous)-6} 组" if len(dangerous) > 6 else ""
            ret = QMessageBox.warning(
                self, APP_NAME,
                f"你勾选的内容里，有 {len(dangerous)} 组是【不建议搬动】的：\n\n"
                + "\n".join(lines) + more +
                "\n\n搬动它们可能导致软件出错、云盘同步中断，甚至系统异常。\n"
                "确定还要继续搬这些吗？（建议先取消勾选，只搬绿色/蓝色的安全项）",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if ret != QMessageBox.StandardButton.Yes:
                return

        ok = bridge.ask(APP_NAME,
                        f"把选中的 {len(targets)} 组内容移动到：{dest_root} ？\n\n"
                        "采用「先复制、成功后删源」，失败项原地保留。\n"
                        "搬好后会在原位置留一块「门牌」指向新家，软件路径不变、正常使用。",
                        "yesno", "question")
        if ok != IDYES:
            return

        self.btn_scan.setEnabled(False)
        self.lbl_scan_state.setText("正在移动…")

        def work():
            import guard_wizard
            moved_files, freed, failed, msgs = 0, 0, 0, []
            for r in targets:
                if r.get("is_dir"):
                    src = Path(r["dir"])
                    if not src.exists():
                        continue
                    dst = Path(dest_root) / "已迁移" / src.name
                    try:
                        dst.parent.mkdir(parents=True, exist_ok=True)
                    except OSError:
                        failed += 1
                        continue
                    res = guard_wizard.migrate_app_dir(src, dst)
                    if res.get("ok"):
                        msgs.append(f"目录已搬：{src.name} → {dst}")
                    else:
                        failed += 1
                        msgs.append(f"目录搬移失败：{src.name}（{res.get('stage','')}）")
                    continue
                # 文件组：逐个先复制后删源
                for fp in r.get("all_paths", []):
                    f = Path(fp)
                    if not f.exists():
                        continue
                    try:
                        sz = f.stat().st_size
                    except OSError:
                        continue
                    dest = self.guard.move_file(f, dest_root)
                    if dest:
                        moved_files += 1
                        freed += sz
                        self.guard.log("今日新增", f.name, sz,
                                       f"一键移动到{Path(dest_root).anchor}", dest)
                    else:
                        failed += 1
            self.move_done.emit(moved_files, freed, failed,
                                "\n".join(msgs[:6]), dest_root)

        self._mv_thread = threading.Thread(target=work, daemon=True)
        self._mv_thread.start()

    def _after_move(self, moved, freed, failed, msgs, dest_root):
        self.btn_scan.setEnabled(True)
        self.lbl_scan_state.setText(f"移动完成：{moved} 个文件 / 释放 {engine.human_size(freed)}")
        src_txt = getattr(self, "_cur_drive", "") + " 盘"
        msg = f"已移动 {moved} 个文件到 {dest_root}\n{src_txt}腾出 {engine.human_size(freed)}"
        if failed:
            msg += f"\n{failed} 项失败（多半被占用），原样保留。"
        if msgs:
            msg += "\n\n" + "\n".join(msgs[:6])
        QMessageBox.information(self, APP_NAME, msg)
        self._refresh_home()
        self.start_scan()

    def _auto_refresh(self):
        self._refresh_home()
        # 切到别的盘时不要自动重刷列表，免得把用户正在看的结果冲掉
        if getattr(self, "_cur_drive", None) not in (None, guard_drives.system_letter()):
            return
        try:
            self._reload_drive_if_changed()
            data = self.scanner.load()
            if data.get("items") and not (self.scan_thread and self.scan_thread.is_alive()):
                self._fill_today(data["items"])
        except Exception:
            pass

    def _reload_drive_if_changed(self):
        """磁盘增减（插拔 U 盘）时刷新下拉，保持当前选择不变。"""
        try:
            now = [d["letter"] for d in guard_drives.list_drives()]
        except Exception:
            return
        if now != [d["letter"] for d in getattr(self, "_drive_infos", [])]:
            self._reload_drive_combo()

    # ---------- 日志清理 ----------
    def _clean_cfg(self):
        """自动清理配置（存在 config.json 的「自动清理」里，缺项用默认值补）。"""
        cfg = dict(guard_cleaner.DEFAULT_CFG)
        try:
            cfg.update(self.guard.config.get("自动清理") or {})
        except Exception:
            pass
        return cfg

    def _build_clean_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        lay.addLayout(self._page_head("日志清理", "偷偷长大的日志与临时文件，该删就删"))
        cfg = self._clean_cfg()

        tip = QLabel(
            "软件日志、崩溃记录、临时文件会一直偷偷长大，这是「什么都没干 C 盘也在变少」的主因。\n"
            "这类东西不该搬走 —— 直接删掉最合理。下面只列「多天没动过」或「大到离谱」的，"
            "正在被写入的文件一律跳过。")
        tip.setStyleSheet("color:#555; font-size:12px;")
        tip.setWordWrap(True)
        lay.addWidget(tip)

        top = QHBoxLayout()
        self.b_clean_scan = QPushButton("🔍 立即扫描")
        self.b_clean_scan.setMinimumHeight(34)
        self.b_clean_scan.clicked.connect(self._start_clean_scan)
        b_clean = QPushButton("🧹 清理勾选的内容")
        b_clean.setMinimumHeight(34)
        b_clean.setStyleSheet("background:#2f7d32; color:white; font-weight:bold;")
        b_clean.clicked.connect(self._clean_selected)
        b_all = QPushButton("全选")
        b_all.clicked.connect(lambda: self._check_all_clean(True))
        b_none = QPushButton("全不选")
        b_none.clicked.connect(lambda: self._check_all_clean(False))
        for b in (self.b_clean_scan, b_clean, b_all, b_none):
            top.addWidget(b)
        top.addStretch(1)
        lay.addLayout(top)

        # ---- 列表 ----
        # 固定列宽合计 526，第 1 列（所在位置）随窗口宽度弹性伸缩
        self.clean_tree = _ElasticTree(elastic_col=1, fixed_sum=526)
        self.clean_tree.setColumnCount(6)
        self.clean_tree.setHeaderLabels(
            ["选择", "所在位置 / 文件", "类型", "大小", "最后写入", "为什么可以清"])
        self.clean_tree.setColumnWidth(1, 240)
        self.clean_tree.setRootIsDecorated(True)
        self.clean_tree.setAlternatingRowColors(True)
        self.clean_tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.clean_tree.setSortingEnabled(True)
        self.clean_tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.clean_tree.header().setMinimumSectionSize(36)
        self.clean_tree.setColumnWidth(0, 46)
        self.clean_tree.setColumnWidth(2, 76)
        self.clean_tree.setColumnWidth(3, 84)
        self.clean_tree.setColumnWidth(4, 130)
        self.clean_tree.setColumnWidth(5, 190)
        self.clean_tree.header().setToolTip("点表头可以排序：大小 / 最后写入时间 / 类型")
        self.clean_tree.itemDoubleClicked.connect(
            lambda item, _col: self._open_item_location(item))
        lay.addWidget(self.clean_tree, 1)

        self.lbl_clean = QLabel("点「立即扫描」看看能清出多少空间。")
        self.lbl_clean.setStyleSheet("color:#333; font-size:12px;")
        self.lbl_clean.setWordWrap(True)
        lay.addWidget(self.lbl_clean)

        # ---- 自动清理设置 ----
        g = QGroupBox("⚙️ 自动清理设置")
        gl = QGridLayout(g)
        self.cb_clean_on = QCheckBox("开启自动清理（到点自动扫一遍、按下面的条件清）")
        self.cb_clean_on.setChecked(bool(cfg.get("启用")))
        self.cb_clean_on.setToolTip(
            "默认关闭。开启后，每隔设定的天数自动跑一次；\n"
            "如果可清的量太小就不打扰你；清理前可以选择先问你一次。")
        gl.addWidget(self.cb_clean_on, 0, 0, 1, 2)

        self.sp_clean = {}
        for i, (key, label, lo, hi) in enumerate((
                ("日志保留天数", "超过多少天没动过就可以清", 1, 365),
                ("超大日志MB", "单个文件大到多少 MB 就可以清", 5, 5000),
                ("自动周期天", "每隔多少天自动清理一次", 1, 90),
                ("最小清理量MB", "可清量少于多少 MB 就不打扰", 0, 2000)), start=1):
            sp = QSpinBox()
            sp.setRange(lo, hi)
            sp.setValue(int(cfg.get(key, lo)))
            self.sp_clean[key] = sp
            gl.addWidget(QLabel(label), i, 0)
            gl.addWidget(sp, i, 1)

        r = 5
        self.cb_clean_ask = QCheckBox("自动清理前先问我一次（推荐）")
        self.cb_clean_ask.setChecked(bool(cfg.get("清理前询问", True)))
        self.cb_clean_crash = QCheckBox("清理崩溃记录（.dmp，软件崩溃时生成的）")
        self.cb_clean_crash.setChecked(bool(cfg.get("清崩溃转储", True)))
        self.cb_clean_temp = QCheckBox("清理临时文件夹里的旧文件（Temp）")
        self.cb_clean_temp.setChecked(bool(cfg.get("清临时文件", True)))
        gl.addWidget(self.cb_clean_ask, r, 0, 1, 2)
        gl.addWidget(self.cb_clean_crash, r + 1, 0, 1, 2)
        gl.addWidget(self.cb_clean_temp, r + 2, 0, 1, 2)

        b_save = QPushButton("💾 保存自动清理设置")
        b_save.setMinimumHeight(32)
        b_save.clicked.connect(self._save_clean_settings)
        gl.addWidget(b_save, r + 3, 0, 1, 2)
        lay.addWidget(g)

        st = guard_cleaner.load_state()
        last = st.get("上次清理") or 0
        self.lbl_clean_last = QLabel(
            "上次清理：" + (self._fmt_time(last) if last else "还没有清理过"))
        self.lbl_clean_last.setStyleSheet("color:#666; font-size:12px;")
        lay.addWidget(self.lbl_clean_last)

        self.clean_rows = []
        return w

    def _check_all_clean(self, on):
        for i in range(self.clean_tree.topLevelItemCount()):
            self.clean_tree.topLevelItem(i).setCheckState(
                0, Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)

    def _start_clean_scan(self):
        if getattr(self, "_clean_scanning", False):
            return
        self._clean_scanning = True
        self.b_clean_scan.setEnabled(False)
        self.b_clean_scan.setText("🔍 扫描中…")
        self.lbl_clean.setText("正在扫描日志与临时文件，请稍等几秒…")
        cfg = self._clean_cfg()

        def work():
            try:
                res = guard_cleaner.scan(cfg)
            except Exception as e:
                res = {"items": [], "total": 0, "scanned": 0, "elapsed": 0,
                       "skipped_active": 0, "truncated": False, "error": str(e)}
            self.clean_scan_done.emit(res)

        threading.Thread(target=work, daemon=True).start()

    def _on_clean_scan_done(self, res):
        self._clean_scanning = False
        self.b_clean_scan.setEnabled(True)
        self.b_clean_scan.setText("🔍 立即扫描")
        self.clean_rows = guard_cleaner.aggregate(res.get("items", []))
        self._fill_clean(res)

    def _fill_clean(self, res):
        self.clean_tree.setSortingEnabled(False)
        self.clean_tree.clear()
        total = res.get("total", 0)
        for g in self.clean_rows:
            note = f"{g['count']} 个文件可以清"
            if g.get("active"):
                note += "（部分正在被写入，建议先退出对应软件）"
            node = SortItem(["", g["dir"], g["cat_text"],
                             engine.human_size(g["size"]),
                             self._fmt_time(g["last_ts"]),
                             note])
            node.setFlags(node.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            node.setCheckState(0, Qt.CheckState.Unchecked)
            node.setData(0, Qt.UserRole, g)
            node.set_key(1, g["dir"].lower())
            node.set_key(2, g["cat_text"])
            node.set_key(3, int(g["size"]))
            node.set_key(4, float(g["last_ts"]))
            node.set_key(5, int(g["count"]))
            node.setToolTip(1, g["dir"] + "\n（双击打开这个位置）")
            node.setForeground(3, QColor("#c62828") if g["size"] >= 200 * 1024 * 1024
                               else QColor("#333"))
            if g.get("active"):
                node.setForeground(5, QColor("#b26a00"))
                node.setToolTip(5, "这里面有文件正在被软件写入。\n"
                                   "直接删多半删不掉（软件占用中），\n"
                                   "建议先退出对应软件再来清理。")
            for it in g["items"][:40]:
                nm = os.path.basename(it["path"])
                child = SortItem(["", nm, it["cat"],
                                  engine.human_size(it["size"]),
                                  self._fmt_time(it["mtime"]),
                                  it["reason"]])
                child.setFlags(child.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                child.setForeground(1, QColor("#888"))
                child.setData(0, Qt.UserRole, {"path": it["path"]})
                child.set_key(1, nm.lower())
                child.set_key(2, it["cat"])
                child.set_key(3, int(it["size"]))
                child.set_key(4, float(it["mtime"]))
                child.set_key(5, it["reason"])
                child.setToolTip(1, it["path"])
                node.addChild(child)
            if g["count"] > 40:
                more = SortItem(["", f"…还有 {g['count'] - 40} 个文件", "", "", "", ""])
                more.setFlags(more.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                more.setForeground(1, QColor("#999"))
                node.addChild(more)
            self.clean_tree.addTopLevelItem(node)
        self.clean_tree.setSortingEnabled(True)
        self.clean_tree.sortItems(3, Qt.SortOrder.DescendingOrder)

        n_groups = len(self.clean_rows)
        n_files = res.get("items") and len(res["items"]) or 0
        extra = ""
        if res.get("skipped_active"):
            extra += (f"；其中 {res['skipped_active']} 个还在被软件写入"
                      f"（约 {engine.human_size(total - res.get('total_safe', total))}），"
                      f"不会自动删，建议退出对应软件后再清")
        if res.get("truncated"):
            extra += "（条目过多，只显示了一部分）"
        if total <= 0:
            self.lbl_clean.setText(
                f"扫了 {res.get('scanned', 0)} 个文件（{res.get('elapsed', 0):.1f} 秒），"
                f"暂时没有该清的东西{extra}。C 盘很干净 👍")
        else:
            self.lbl_clean.setText(
                f"扫了 {res.get('scanned', 0)} 个文件（{res.get('elapsed', 0):.1f} 秒）："
                f"找到 {n_files} 个可清文件 / 共 {engine.human_size(total)}，"
                f"分在 {n_groups} 个位置{extra}。\n"
                "勾选要清的位置，然后点「清理勾选的内容」。日志删掉不会影响软件运行。")

    def _selected_clean_items(self):
        out = []
        for i in range(self.clean_tree.topLevelItemCount()):
            node = self.clean_tree.topLevelItem(i)
            if node.checkState(0) == Qt.CheckState.Checked:
                g = node.data(0, Qt.UserRole)
                if g:
                    out.extend(g.get("items", []))
        return out

    def _clean_selected(self):
        items = self._selected_clean_items()
        if not items:
            QMessageBox.information(self, APP_NAME,
                                    "先勾选要清的位置（点左边的小方框），再点清理。")
            return
        total = sum(i["size"] for i in items)
        n_active = sum(1 for i in items if i.get("active"))
        warn = ""
        if n_active:
            warn = (f"\n\n注意：有 {n_active} 个文件正在被软件写入，可能删不掉。\n"
                    "建议先退出对应的软件再来清理。")
        r = bridge.ask(APP_NAME,
                       f"确定清理这 {len(items)} 个文件吗？共 {engine.human_size(total)}。\n\n"
                       "这些都是日志 / 崩溃记录 / 临时文件，删掉不会影响软件运行，\n"
                       "但删了就找不回来了。" + warn, "okcancel", "warning")
        if r != IDOK:
            return
        self._run_clean(items)

    def _run_clean(self, items):
        def work():
            res = guard_cleaner.clean(items)
            msgs = ""
            if res["fail"]:
                msgs = "\n".join(f"{p}（{e}）" for p, e in res["fail"][:5])
            guard_cleaner.mark_cleaned(res["freed"], len(res["ok"]))
            self.clean_done.emit(res["freed"], len(res["ok"]), len(res["fail"]), msgs)
        threading.Thread(target=work, daemon=True).start()

    def _on_clean_done(self, freed, ok_n, fail_n, msgs):
        txt = f"清理完成：删掉 {ok_n} 个文件，腾出 {engine.human_size(freed)}。"
        if fail_n:
            txt += f"\n\n有 {fail_n} 个删不掉（多半是软件正在用，不影响使用）：\n{msgs}"
        QMessageBox.information(self, APP_NAME, txt)
        st = guard_cleaner.load_state()
        self.lbl_clean_last.setText(
            "上次清理：" + (self._fmt_time(st.get("上次清理")) if st.get("上次清理")
                         else "还没有清理过"))
        self._start_clean_scan()
        try:
            self.tray.showMessage(APP_NAME, f"已清理日志，腾出 {engine.human_size(freed)}",
                                  QSystemTrayIcon.MessageIcon.Information, 4000)
        except Exception:
            pass

    def _save_clean_settings(self):
        cfg = self._clean_cfg()
        cfg["启用"] = self.cb_clean_on.isChecked()
        cfg["清理前询问"] = self.cb_clean_ask.isChecked()
        cfg["清崩溃转储"] = self.cb_clean_crash.isChecked()
        cfg["清临时文件"] = self.cb_clean_temp.isChecked()
        for k, sp in self.sp_clean.items():
            cfg[k] = sp.value()
        self.guard.config["自动清理"] = cfg
        self.guard.save_config()
        QMessageBox.information(
            self, APP_NAME,
            ("自动清理已开启：每隔 %d 天自动跑一次，超过 %d 天没动过、或大于 %d MB 的"
             "日志/临时文件会自动清掉。\n%s"
             % (int(cfg["自动周期天"]), int(cfg["日志保留天数"]), int(cfg["超大日志MB"]),
                "清理前会先问你一次。" if cfg["清理前询问"] else "到点直接清，不再打扰你。")))

    # ---------- 自动清理调度 ----------
    def _auto_clean_check(self):
        try:
            cfg = self._clean_cfg()
            due, _ = guard_cleaner.due(cfg)
            if not due:
                return
            threading.Thread(target=self._auto_clean_work, args=(cfg,),
                             daemon=True).start()
        except Exception:
            pass

    def _auto_clean_work(self, cfg):
        try:
            res = guard_cleaner.scan(cfg)
            # 自动清理只动「没在被写入」的，正在写的留给用户手动决定
            items = [i for i in res.get("items", []) if not i.get("active")]
            total = sum(i["size"] for i in items)
            if total < int(cfg.get("最小清理量MB", 20)) * 1024 * 1024:
                guard_cleaner.mark_checked()
                return
            do_it = True
            if cfg.get("清理前询问", True):
                ans = bridge.ask(
                    APP_NAME,
                    f"到了自动清理的时间：找到 {len(items)} 个日志/临时文件，"
                    f"共 {guard_cleaner.human(total)}。\n\n现在清理吗？",
                    "yesno", "question")
                do_it = (ans == IDYES)
            if do_it:
                r = guard_cleaner.clean(items)
                guard_cleaner.mark_cleaned(r["freed"], len(r["ok"]))
                if r["freed"] > 0:
                    self.clean_done.emit(r["freed"], len(r["ok"]), len(r["fail"]), "")
            else:
                guard_cleaner.mark_checked()
        except Exception:
            guard_cleaner.mark_checked()

    # ---------- 守卫记录 ----------
    def _build_log_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        lay.addLayout(self._page_head("守卫记录", "每次询问与决定都留痕，随时可回查"))
        top = QHBoxLayout()
        top.addWidget(QLabel("今天的守卫记录："))
        top.addStretch(1)
        b = QPushButton("刷新")
        b.clicked.connect(self._refresh_log)
        top.addWidget(b)
        b2 = QPushButton("打开日志文件夹")
        b2.clicked.connect(self._open_logdir)
        top.addWidget(b2)
        lay.addLayout(top)
        self.log_tree = QTreeWidget()
        self.log_tree.setColumnCount(6)
        self.log_tree.setHeaderLabels(["时间", "类型", "对象", "大小", "决定", "去向"])
        self.log_tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.log_tree.header().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.log_tree.setRootIsDecorated(False)
        self.log_tree.setAlternatingRowColors(True)
        # 空状态：没记录时显示插画提示
        self.log_stack = QStackedWidget()
        self.log_stack.addWidget(self.log_tree)
        self.log_empty = GT.EmptyState(
            "今天还没有记录",
            "守卫每次询问你「存不存 C 盘」，以及你的决定，都会出现在这里。")
        self.log_stack.addWidget(self.log_empty)
        lay.addWidget(self.log_stack, 1)
        self._refresh_log()
        return w

    def _refresh_log(self):
        self.log_tree.clear()
        p = engine.LOG_DIR / f"守卫记录_{datetime.date.today():%Y%m%d}.csv"
        if p.exists():
            try:
                with open(p, newline="", encoding="utf-8-sig") as f:
                    for row in list(csv.reader(f))[-500:][::-1]:
                        if len(row) < 6 or row[0] == "时间":
                            continue
                        QTreeWidgetItem(row[:6], self.log_tree)
            except Exception:
                pass
        if hasattr(self, "log_stack"):
            self.log_stack.setCurrentIndex(
                0 if self.log_tree.topLevelItemCount() else 1)

    # ---------- 隔离区 ----------
    def _build_quar_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        lay.addLayout(self._page_head("隔离区", "误删的文件先躺在这里，30 天内随时还原"))
        top = QHBoxLayout()
        self.lbl_quar = QLabel()
        self.lbl_quar.setWordWrap(True)     # 长文本折行，不撑宽页面
        top.addWidget(self.lbl_quar, 1)
        b_restore = QPushButton("↩️ 还原所选到原位置")
        b_restore.clicked.connect(self._quar_restore)
        b_purge = QPushButton("🗑️ 彻底删除所选")
        b_purge.clicked.connect(self._quar_purge)
        b_refresh = QPushButton("刷新")
        b_refresh.clicked.connect(self._refresh_quar)
        for b in (b_restore, b_purge, b_refresh):
            top.addWidget(b)
        lay.addLayout(top)
        self.quar_tree = QTreeWidget()
        self.quar_tree.setColumnCount(4)
        self.quar_tree.setHeaderLabels(["原文件", "大小", "删除时间", "保留至"])
        self.quar_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.quar_tree.setRootIsDecorated(False)
        self.quar_tree.setAlternatingRowColors(True)
        # 空状态：隔离区为空时显示插画提示
        self.quar_stack = QStackedWidget()
        self.quar_stack.addWidget(self.quar_tree)
        self.quar_empty = GT.EmptyState(
            "隔离区是空的",
            "被删除的文件会先放进这里冷静 30 天，确认不要了才会真正清掉。")
        self.quar_stack.addWidget(self.quar_empty)
        lay.addWidget(self.quar_stack, 1)
        self._refresh_quar()
        return w

    def _refresh_quar(self):
        self.quar_tree.clear()
        rows = self.guard.quar.list()
        self.lbl_quar.setText(
            f"隔离区（删除保护）：{len(rows)} 项 / "
            f"{engine.human_size(self.guard.quar.total_size())}，"
            f"保留 {int(self.guard.config.get('隔离区保留天数', 30))} 天后自动清理")
        for e in rows:
            t1 = datetime.datetime.fromtimestamp(e.get("deleted", 0)).strftime("%m-%d %H:%M")
            t2 = datetime.datetime.fromtimestamp(
                e.get("taken", 0) + self.guard.quar.retention_days * 86400).strftime("%Y-%m-%d")
            node = QTreeWidgetItem([e["orig"], engine.human_size(e.get("size", 0)), t1, t2])
            node.setData(0, Qt.UserRole, e["id"])
            self.quar_tree.addTopLevelItem(node)
        if hasattr(self, "quar_stack"):
            self.quar_stack.setCurrentIndex(
                0 if self.quar_tree.topLevelItemCount() else 1)

    def _cur_quar_id(self):
        it = self.quar_tree.currentItem()
        return it.data(0, Qt.UserRole) if it else None

    def _quar_restore(self):
        qid = self._cur_quar_id()
        if not qid:
            return
        dest = self.guard.quar.restore(qid)
        QMessageBox.information(self, APP_NAME,
                                f"已还原到：\n{dest}" if dest else "还原失败（文件可能已不在隔离区）。")
        self._refresh_quar()

    def _quar_purge(self):
        qid = self._cur_quar_id()
        if not qid:
            return
        r = bridge.ask(APP_NAME, "彻底删除后无法恢复，确定吗？", "okcancel", "warning")
        if r == IDOK:
            self.guard.quar.purge(qid)
            self._refresh_quar()

    # ---------- 设置 ----------
    def _card_title(self, text, hint="", red=False):
        row = QHBoxLayout()
        t = QLabel(text)
        t.setStyleSheet(
            f"font-weight:700;font-size:{GT.human_font(15)}pt;"
            f"color:{GT.C['red'] if red else GT.C['ink']};")
        row.addWidget(t)
        if hint:
            h = QLabel(hint)
            h.setStyleSheet(
                f"color:{GT.C['ink3']};font-size:{GT.human_font(12)}pt;font-weight:400;")
            row.addWidget(h)
        row.addStretch(1)
        return row

    def _card(self, red=False):
        f = QFrame()
        f.setProperty("card", True)
        if red:
            f.setStyleSheet("QFrame[card='true']{border:1px solid #F2D4D4;}")
        lay = QVBoxLayout(f)
        lay.setContentsMargins(20, 14, 20, 14)
        lay.setSpacing(4)
        return f, lay

    def _build_settings_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)
        cfg = self.guard.config

        # ---- 页头 ----
        head = QHBoxLayout()
        hv = QVBoxLayout()
        hv.setSpacing(2)
        h1 = QLabel("设置")
        h1.setStyleSheet(f"font-size:{GT.human_font(20)}pt;font-weight:700;")
        sub = QLabel("盯多严、问多勤，都由你说了算")
        sub.setStyleSheet(f"color:{GT.C['ink3']};font-size:{GT.human_font(13)}pt;")
        hv.addWidget(h1)
        hv.addWidget(sub)
        head.addLayout(hv)
        head.addStretch(1)
        b_save_top = GT.style_button(QPushButton("保存设置"), "primary")
        b_save_top.clicked.connect(self._save_settings)
        head.addWidget(b_save_top)
        lay.addLayout(head)

        # ---- 守护开关 ----
        g1, gl = self._card()
        gl.addLayout(self._card_title("守护开关"))
        self.sw = {}
        for key, title, desc in (
                ("全盘拦截", "全部写入拦截", "AppData 等位置也盯"),
                ("删除保护", "删除保护", "回收站删除时提醒，并先送隔离区"),
                ("聚合提示", "聚合监控", "同一来源的零散写入合并成一次询问，不刷屏"),
                ("监控软件安装", "安装哨兵", "软件装进 C 盘时提醒"),
                ("空间告警", "C 盘剩余空间不足告警", "低于告警线时在托盘提醒")):
            row = GT.SwitchRow(title, desc, bool(cfg.get(key, True)))
            self.sw[key] = row.sw
            gl.addWidget(row)
        lay.addWidget(g1)

        # ---- 阈值 ----
        g2, g2l = self._card()
        g2l.addLayout(self._card_title("阈值", "数值越小，询问越勤快"))
        gg = QGridLayout()
        gg.setSpacing(10)
        self.sp = {}
        for i, (key, label, maximum) in enumerate((
                ("单文件询问阈值MB", "非重点目录单文件询问下限（MB）", 10000),
                ("批量询问阈值MB", "批量合并询问阈值（MB）", 10000),
                ("聚合增长阈值MB", "聚合监控增长阈值（MB）", 100000),
                ("告警剩余GB", "剩余空间告警线（GB）", 500))):
            lab = QLabel(label)
            lab.setStyleSheet(f"color:{GT.C['ink2']};font-size:{GT.human_font(13)}pt;")
            sp = QSpinBox()
            sp.setRange(0, maximum)
            sp.setValue(int(cfg.get(key, 0)))
            sp.setFixedWidth(110)
            self.sp[key] = sp
            gg.addWidget(lab, i, 0)
            gg.addWidget(sp, i, 1, Qt.AlignmentFlag.AlignRight)
        g2l.addLayout(gg)
        lay.addWidget(g2)

        # ---- 启动与显示 ----
        g3, g3l = self._card()
        g3l.addLayout(self._card_title("启动与显示"))
        row_a = GT.SwitchRow("开机自动启动 C 盘守卫", "启动后缩在托盘，不打扰",
                             self._autostart_on())
        self.cb_autostart = row_a.sw
        g3l.addWidget(row_a)
        row_s = GT.SwitchRow("用内置对照表把路径翻译成软件名",
                             "仅本地文字关键词，无任何图标",
                             bool(cfg.get("内置来源识别", True)))
        self.cb_builtin_sig = row_s.sw
        self.cb_builtin_sig.setToolTip(
            "关掉后，「来源应用」这一列只显示文件夹名，不再出现任何软件名称。\n"
            "注意：无论这个开关如何，真正决定限制哪些软件的，都是你在下面\n"
            "「软件存储限制」里自己添加的规则。")
        g3l.addWidget(row_s)
        lay.addWidget(g3)

        # ---- 软件存储限制 ----
        g_rule, gr = self._card(red=True)
        gr.addLayout(self._card_title("软件存储限制（我自己定的规则）", red=True))
        desc = QLabel(
            "把不想让它往 C 盘存东西的软件加进来（软件名你自己起，例如「酷狗音乐」）。\n"
            "以后这个软件一往 C 盘写东西，就会先问你、再由你选一个盘搬走。\n"
            "规则只存本地，不预置任何品牌名单。")
        desc.setStyleSheet(f"color:{GT.C['ink2']};font-size:{GT.human_font(13)}pt;")
        desc.setWordWrap(True)
        gr.addWidget(desc)
        self.lab_rules = QLabel()
        self.lab_rules.setWordWrap(True)
        self.lab_rules.setStyleSheet(
            f"background:{GT.C['amber_soft']};color:#9A6A14;border-radius:8px;"
            f"padding:3px 12px;font-size:{GT.human_font(12.5)}pt;")
        gr.addWidget(self.lab_rules, 0, Qt.AlignmentFlag.AlignLeft)
        self._refresh_rules_label()
        b_rules = GT.style_button(QPushButton("管理限制的软件…"), "blue")
        b_rules.setMinimumHeight(34)
        b_rules.clicked.connect(self._open_rules)
        gr.addWidget(b_rules, 0, Qt.AlignmentFlag.AlignLeft)
        lay.addWidget(g_rule)

        # ---- 关于与声明 ----
        g_about, ga = self._card()
        ga.addLayout(self._card_title("关于与声明"))
        lbl_about = QLabel(
            f"{APP_NAME} · 本地运行，不联网、不上传任何数据。\n"
            "软件图标与全部代码均为原创；界面中提及的第三方应用名称\n"
            "（如微信、QQ、Chrome 等）仅用于识别文件来源，商标归各自权利人所有。")
        lbl_about.setStyleSheet(f"color:{GT.C['ink3']};font-size:{GT.human_font(12.5)}pt;")
        lbl_about.setWordWrap(True)
        ga.addWidget(lbl_about)
        b_notice = GT.style_button(QPushButton("查看第三方组件声明"), "sm")
        b_notice.setMinimumWidth(
            b_notice.fontMetrics().horizontalAdvance(b_notice.text()) + 30)
        b_notice.clicked.connect(self._open_notices)
        ga.addWidget(b_notice, 0, Qt.AlignmentFlag.AlignLeft)
        lay.addWidget(g_about)

        lay.addStretch(1)
        return w

    def _refresh_rules_label(self):
        try:
            rs = guard_rules.rules
            act = [r for r in rs.rules if r.get("enabled", True)]
            if not rs.rules:
                self.lab_rules.setText("当前：还没有添加任何限制。")
                return
            names = "、".join(r.get("name", "") for r in act[:6])
            if len(act) > 6:
                names += f" 等 {len(act)} 个"
            off = len(rs.rules) - len(act)
            extra = f"（另有 {off} 条已停用）" if off else ""
            self.lab_rules.setText(f"当前已限制 {len(act)} 个：{names}{extra}")
        except Exception:
            self.lab_rules.setText("")

    def _open_rules(self):
        dlg = RulesDialog(self)
        dlg.exec()
        self._refresh_rules_label()
        try:
            guard_rules.rules.save()
        except Exception:
            pass

    def _open_notices(self):
        """打开第三方组件声明文件（没有则现场生成一份基础版）。"""
        notice = APP_DIR / "THIRD_PARTY_NOTICES.txt"
        if not notice.exists():
            try:
                notice.write_text(
                    "C盘守卫 第三方组件声明\n\n"
                    "1. Python 3（PSF License）\n"
                    "2. PySide6 / Qt 6（LGPLv3，动态链接，用户可替换 Qt 库）\n"
                    "3. PyInstaller（GPLv2 + bootloader 例外，仅开发期打包工具）\n"
                    "4. Pillow（MIT-CMU，仅开发期生成图标）\n\n"
                    "商标声明：界面中提及的第三方应用名称（Windows、微信、QQ、\n"
                    "Chrome、Steam、NVIDIA 等）仅用于识别文件来源，商标归各自\n"
                    "权利人所有，本软件与上述厂商无任何隶属或背书关系。\n\n"
                    "本软件图标与代码均为原创，未使用外部图片或图标素材。\n",
                    encoding="utf-8")
            except Exception:
                pass
        try:
            os.startfile(str(notice))          # noqa: S606 本机打开文本文件
        except Exception as e:
            QMessageBox.information(self, APP_NAME, f"声明文件位于：\n{notice}\n\n({e})")

    @staticmethod
    def _autostart_on() -> bool:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Run",
                                0, winreg.KEY_READ) as k:
                winreg.QueryValueEx(k, "CDiskGuard")
            return True
        except Exception:
            return False

    def _set_autostart(self, on: bool):
        try:
            import winreg
            exe = sys.executable if getattr(sys, "frozen", False) else None
            if not exe:
                return
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Run",
                                0, winreg.KEY_SET_VALUE) as k:
                if on:
                    winreg.SetValueEx(k, "CDiskGuard", 0, winreg.REG_SZ,
                                      f'"{exe}" --minimized')
                else:
                    try:
                        winreg.DeleteValue(k, "CDiskGuard")
                    except FileNotFoundError:
                        pass
        except Exception:
            pass

    def _save_settings(self):
        cfg = self.guard.config
        for key, cb in self.sw.items():
            cfg[key] = cb.isChecked()
        for key, sp in self.sp.items():
            cfg[key] = sp.value()
        # 内置来源对照表开关：立即生效
        try:
            on = self.cb_builtin_sig.isChecked()
            cfg["内置来源识别"] = on
            guard_today.set_builtin_signatures(on)
        except Exception:
            pass
        self.guard.save_config()
        self._set_autostart(self.cb_autostart.isChecked())
        self._refresh_rules_label()
        QMessageBox.information(self, APP_NAME,
                                "设置已保存。部分阈值类设置即刻生效，\n"
                                "监听范围类设置会在下次启动守卫时生效。")

    # ---------- 工具 ----------
    def _do_dashboard(self):
        def work():
            try:
                guard_dashboard.dashboard(open_html=True)
            except Exception:
                pass
        threading.Thread(target=work, daemon=True).start()

    def _do_weekly(self):
        def work():
            try:
                out = guard_dashboard.weekly_report()
                os.startfile(out)
            except Exception:
                pass
        threading.Thread(target=work, daemon=True).start()

    def _open_logdir(self):
        try:
            os.startfile(engine.LOG_DIR)
        except Exception:
            pass

    # ---------- 托盘 / 关闭行为 ----------
    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.show_up()

    def show_up(self):
        self.showNormal()
        self.activateWindow()
        self.raise_()

    def closeEvent(self, ev):
        # × = 缩到托盘，不退出
        ev.ignore()
        self.hide()
        self.tray.show()
        if not self._tray_hinted:
            self._tray_hinted = True
            self.tray.showMessage(
                "C盘守卫仍在后台运行",
                "点这里打开主界面；右键托盘图标 →「退出守卫」才会停止保护。",
                QSystemTrayIcon.MessageIcon.Information, 5000)

    def real_quit(self):
        r = bridge.ask(APP_NAME,
                       "退出后 C 盘将不再受保护（新文件写入不会提醒、删除不会隔离）。\n\n确定退出吗？",
                       "yesno", "warning")
        if r != IDYES:
            return
        try:
            self.guard.stop_evt.set()
        except Exception:
            pass
        self.tray.hide()
        QApplication.quit()


# ============================================================
# 入口
# ============================================================

def main():
    global _guard_ref

    # 高 DPI 下按实际缩放逐像素渲染，避免系统位图拉伸造成的文字发虚
    try:
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    except Exception:
        pass

    minimized = ("--minimized" in sys.argv) or ("--silent" in sys.argv)

    # 单实例
    if engine.single_instance_lock() is None:
        app = QApplication.instance() or QApplication(sys.argv)
        QMessageBox.warning(None, APP_NAME,
                            "已经有 C盘守卫 在运行了（可能在托盘里）。\n"
                            "请先退出已运行的实例再启动。")
        return

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(QIcon(str(ICON_PATH)) if ICON_PATH.exists() else
                      app.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon))
    # 统一主题（设计稿配色 + 字体）
    GT.apply(app)

    # 桥接：所有引擎弹窗走 Qt
    bridge_ui = BridgeUI()
    engine.alert_box = qt_alert_box
    bridge.attach(bridge_ui, main_thread_id=threading.get_ident())

    guard = GuiGuard()
    _guard_ref = guard
    # 内置来源对照表开关（用户可在设置里关掉）
    try:
        guard_today.set_builtin_signatures(
            bool(guard.config.get("内置来源识别", True)))
    except Exception:
        pass

    # 守护引擎放后台线程（与 GUI 同进程，弹窗经桥接回到主线程）
    t = threading.Thread(target=guard.run, daemon=True,
                         name="GuardEngine")
    t.start()

    win = MainWindow(guard, minimized=minimized)
    app.exec()


if __name__ == "__main__":
    main()
