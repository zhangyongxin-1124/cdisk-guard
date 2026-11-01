# -*- coding: utf-8 -*-
"""
guard_theme.py — C盘守卫 界面主题（v6.7 新 UI）
==============================================
把设计稿（HTML/CSS）翻译成 PySide6 能用的东西：

  · COLORS      —— 设计稿的配色变量
  · APP_QSS     —— 全局样式表（卡片、按钮、表格、输入框、滚动条…）
  · svg_icon()  —— 把内联 SVG 渲染成 QIcon（设计稿图标原样复用）
  · ICONS       —— 侧栏导航等图标
  · ToggleSwitch / SwitchRow —— 设计稿里的滑块开关行
  · Gauge       —— 概况页的环形仪表盘
  · NavButton   —— 侧栏导航项
  · EmptyState  —— 空列表时的插画式提示

只管外观，不碰任何业务逻辑。
"""

from PySide6.QtCore import (Qt, QSize, QRect, QRectF, Signal, QTimer,
                            QVariantAnimation, QPointF)
from PySide6.QtGui import (QColor, QPainter, QPen, QFont, QIcon, QPixmap,
                           QPainterPath, QImage)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                               QPushButton, QSizePolicy, QApplication)

# ============================================================
# 配色（对应设计稿 :root 变量）
# ============================================================
C = {
    "green":       "#2E9E5B",
    "green_deep":  "#20744A",
    "green_soft":  "#EAF5EE",
    "green_mist":  "#F2F8F3",
    "ink":         "#1C2B22",
    "ink2":        "#4E5F55",
    "ink3":        "#8A988F",
    "amber":       "#E8A13D",
    "amber_soft":  "#FDF3E2",
    "red":         "#D65454",
    "red_soft":    "#FBEDED",
    "blue":        "#3B7EC2",
    "blue_soft":   "#EAF2FA",
    "paper":       "#F6F9F5",
    "card":        "#FFFFFF",
    "line":        "#E3EBE4",
    "line2":       "#D2DED3",
    "row_alt":     "#FAFCFA",
    "row_line":    "#EEF3EE",
    "track":       "#E8EFE8",
    "sw_off":      "#CBD8CD",
}

FONT_FAMILY = "Microsoft YaHei"
SIDEBAR_W = 208


def human_font(size_px):
    """设计稿用 px，Qt 用 pt（96dpi 下 1pt ≈ 1.333px）。"""
    return max(7, int(round(size_px / 1.333)))


# ============================================================
# 全局样式表
# ============================================================
APP_QSS = f"""
/* ---------- 基础 ---------- */
QWidget {{
    font-family: "{FONT_FAMILY}";
    font-size: {human_font(14)}pt;
    color: {C['ink']};
}}
QMainWindow, QDialog {{ background: {C['paper']}; }}
QToolTip {{
    background: {C['ink']}; color: #fff; border: none;
    padding: 5px 8px; border-radius: 6px; font-size: {human_font(12)}pt;
}}

/* ---------- 卡片 ---------- */
QFrame[card="true"], QGroupBox {{
    background: {C['card']};
    border: 1px solid {C['line']};
    border-radius: 12px;
}}
QGroupBox {{
    margin-top: 14px;
    padding: 14px 16px 14px 16px;
    font-weight: 700;
    font-size: {human_font(15)}pt;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 14px;
    padding: 0 6px;
    color: {C['ink']};
}}

/* ---------- 按钮 ---------- */
QPushButton {{
    background: {C['card']};
    border: 1px solid {C['line2']};
    border-radius: 9px;
    padding: 7px 15px;
    font-size: {human_font(13)}pt;
    color: {C['ink']};
    min-height: 18px;
}}
QPushButton:hover {{ background: #F3F7F3; border-color: #BFCFC2; }}
QPushButton:pressed {{ background: #EAF1EA; }}
QPushButton:disabled {{ color: {C['ink3']}; background: #F4F7F4; border-color: {C['line']}; }}
QPushButton[kind="primary"] {{
    background: {C['green']}; border: 1px solid {C['green']};
    color: #fff; font-weight: 600;
}}
QPushButton[kind="primary"]:hover {{ background: {C['green_deep']}; border-color: {C['green_deep']}; }}
QPushButton[kind="blue"] {{
    background: {C['blue']}; border: 1px solid {C['blue']};
    color: #fff; font-weight: 600;
}}
QPushButton[kind="blue"]:hover {{ background: #316CA6; border-color: #316CA6; }}
QPushButton[kind="ghost"] {{
    background: transparent; border: 1px solid transparent; color: {C['ink2']};
}}
QPushButton[kind="ghost"]:hover {{ background: #EDF3ED; color: {C['ink']}; }}
QPushButton[kind="danger"] {{ color: {C['red']}; }}
QPushButton[kind="danger"]:hover {{ background: {C['red_soft']}; border-color: #EAB6B6; }}
QPushButton[kind="sm"] {{ padding: 4px 10px; font-size: {human_font(12)}pt; border-radius: 8px; }}

/* ---------- 侧栏导航 ---------- */
QPushButton[nav="true"] {{
    border: none; background: transparent; text-align: left;
    padding: 9px 12px; border-radius: 10px;
    font-size: {human_font(14)}pt; font-weight: 500; color: {C['ink2']};
}}
QPushButton[nav="true"]:hover {{ background: #EDF3ED; color: {C['ink']}; }}
QPushButton[nav="true"]:checked {{
    background: {C['green']}; color: #ffffff; font-weight: 600;
}}

/* ---------- 输入框 ---------- */
QLineEdit, QSpinBox, QComboBox {{
    background: {C['card']};
    border: 1px solid {C['line2']};
    border-radius: 8px;
    padding: 6px 9px;
    font-size: {human_font(13)}pt;
    selection-background-color: {C['green']};
}}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{
    border: 1px solid {C['green']};
}}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{
    background: {C['card']}; border: 1px solid {C['line2']};
    border-radius: 8px; padding: 4px;
    selection-background-color: {C['green_soft']};
    selection-color: {C['ink']};
}}
QSpinBox::up-button, QSpinBox::down-button {{ width: 14px; border: none; background: transparent; }}

/* ---------- 表格（今日新增 / 记录 / 隔离区 / 日志清理）---------- */
QTreeWidget, QTableWidget {{
    background: {C['card']};
    border: none;
    alternate-background-color: {C['row_alt']};
    show-decoration-selected: 1;
    outline: none;
}}
QTreeWidget::item, QTableWidget::item {{ padding: 6px 4px; }}
QTreeWidget::item:selected, QTableWidget::item:selected {{
    background: {C['green_soft']}; color: {C['ink']};
}}
QTreeWidget::item:hover, QTableWidget::item:hover {{ background: {C['row_alt']}; }}
QHeaderView {{ background: {C['card']}; }}
QHeaderView::section {{
    background: {C['row_alt']};
    color: {C['ink3']};
    border: none;
    border-top: 1px solid {C['line']};
    border-bottom: 1px solid {C['line']};
    padding: 8px 10px;
    font-weight: 600;
    font-size: {human_font(13)}pt;
}}
QHeaderView::section:hover {{ background: #F1F6F1; color: {C['ink2']}; }}

/* ---------- 进度条 ---------- */
QProgressBar {{
    background: {C['track']}; border: none; border-radius: 5px;
    height: 10px; text-align: center;
}}
QProgressBar::chunk {{ background: {C['amber']}; border-radius: 5px; }}

/* ---------- 复选框（列表里的小方框）---------- */
QCheckBox {{ spacing: 6px; color: {C['ink']}; }}
QCheckBox::indicator {{
    width: 16px; height: 16px; border-radius: 4px;
    border: 1px solid {C['line2']}; background: {C['card']};
}}
QCheckBox::indicator:hover {{ border-color: {C['green']}; }}
QCheckBox::indicator:checked {{
    background: {C['green']}; border: 1px solid {C['green']};
    image: url(_none_);
}}
QCheckBox::indicator:indeterminate {{ background: {C['green_soft']}; }}

/* ---------- 滚动条 ---------- */
QScrollBar:vertical {{
    background: transparent; width: 10px; margin: 0 2px 0 0; border: none;
}}
QScrollBar::handle:vertical {{
    background: #D6E1D8; border-radius: 5px; min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: #C2D2C6; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
QScrollBar:horizontal {{
    background: transparent; height: 10px; margin: 0 2px 0 2px; border: none;
}}
QScrollBar::handle:horizontal {{
    background: #D6E1D8; border-radius: 5px; min-width: 30px;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background: transparent; }}

/* ---------- 标签页（旧式，保留以防其他地方还在用）---------- */
QTabWidget::pane {{ border: none; background: {C['paper']}; }}
QTabBar::tab {{
    background: transparent; border: none; padding: 8px 16px;
    color: {C['ink2']}; font-size: {human_font(13)}pt;
}}
QTabBar::tab:selected {{
    color: {C['green_deep']}; font-weight: 700;
    border-bottom: 2px solid {C['green']};
}}
"""


def apply(app: QApplication):
    """给整个应用套上主题。"""
    app.setStyleSheet(APP_QSS)
    f = QFont(FONT_FAMILY, human_font(14))
    app.setFont(f)
    return app


# ============================================================
# SVG 图标（设计稿里的图标原样搬过来）
# ============================================================

def svg_icon(body: str, size=17, color=None, viewbox="0 0 17 17"):
    """把 SVG 片段渲染成 QIcon。body 里的 currentColor 会替换成实际颜色。"""
    col = color or C["ink2"]
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{viewbox}" '
           f'width="{size}" height="{size}">'
           + body.replace("currentColor", col) + "</svg>")
    try:
        r = QSvgRenderer(bytearray(svg.encode("utf-8")))
        if not r.isValid():
            return QIcon()
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        r.render(p)
        p.end()
        return QIcon(pm)
    except Exception:
        return QIcon()


# 侧栏导航图标（viewBox 0 0 17 17，stroke currentColor）
_I_HOME = ('<path d="M2.5 8.5L8.5 2.5l6 6M4 7.5V14h9V7.5" stroke="currentColor" '
           'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>')
_I_TODAY = ('<rect x="2.5" y="3.5" width="12" height="11" rx="2" stroke="currentColor" '
            'stroke-width="1.6" fill="none"/>'
            '<path d="M2.5 7h12M5.5 1.8v3M11.5 1.8v3" stroke="currentColor" '
            'stroke-width="1.6" stroke-linecap="round"/>')
_I_CLEAN = ('<path d="M3.2 14.5h10.6M5 11.8V6.6a3.5 3.5 0 017 0v5.2" '
            'stroke="currentColor" stroke-width="1.6" stroke-linecap="round" fill="none"/>'
            '<path d="M6.9 6.6h3.2M12.6 8.2l1.6 1.6" stroke="currentColor" '
            'stroke-width="1.6" stroke-linecap="round"/>')
_I_LOG = ('<path d="M4 2.5h7l3 3V14.5H4z" stroke="currentColor" stroke-width="1.6" '
          'stroke-linejoin="round" fill="none"/>'
          '<path d="M6.5 8h4M6.5 11h4" stroke="currentColor" stroke-width="1.6" '
          'stroke-linecap="round"/>')
_I_QUAR = ('<rect x="2.5" y="7" width="12" height="7.5" rx="2" stroke="currentColor" '
           'stroke-width="1.6" fill="none"/>'
           '<path d="M5.5 7V5.2A2.2 2.2 0 017.7 3h1.6a2.2 2.2 0 012.2 2.2V7" '
           'stroke="currentColor" stroke-width="1.6" fill="none"/>'
           '<circle cx="8.5" cy="10.7" r="1.2" fill="currentColor"/>')
_I_SET = ('<circle cx="8.5" cy="8.5" r="2.4" stroke="currentColor" stroke-width="1.6" fill="none"/>'
          '<path d="M8.5 1.8v2M8.5 13.2v2M1.8 8.5h2M13.2 8.5h2M3.7 3.7l1.4 1.4'
          'M11.9 11.9l1.4 1.4M13.3 3.7l-1.4 1.4M5.1 11.9l-1.4 1.4" '
          'stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>')
_I_SEARCH = ('<circle cx="6" cy="6" r="4.4" stroke="currentColor" stroke-width="1.6" fill="none"/>'
             '<path d="M9.4 9.4L13 13" stroke="currentColor" stroke-width="1.6" '
             'stroke-linecap="round"/>')
_I_PLUS = '<path d="M6.5 1.5v10M1.5 6.5h10" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>'
_I_SHIELD = ('<path d="M8.5 1.6l5.6 2v4.3c0 3.5-2.4 6.2-5.6 7.5-3.2-1.3-5.6-4-5.6-7.5V3.6l5.6-2z" '
             'fill="currentColor" opacity=".16"/>'
             '<path d="M8.5 2.6l4.7 1.7v3.6c0 2.9-2 5.2-4.7 6.3-2.7-1.1-4.7-3.4-4.7-6.3V4.3L8.5 2.6z" '
             'stroke="currentColor" stroke-width="1.5" fill="none" stroke-linejoin="round"/>'
             '<path d="M6.4 8.3l1.6 1.6 2.9-3" stroke="currentColor" stroke-width="1.7" '
             'fill="none" stroke-linecap="round" stroke-linejoin="round"/>')
_I_DOC = ('<rect x="3" y="2" width="10" height="12" rx="2" stroke="currentColor" '
          'stroke-width="1.6" fill="none"/>'
          '<path d="M5.5 6h5M5.5 9h5M5.5 12h3" stroke="currentColor" stroke-width="1.5" '
          'stroke-linecap="round"/>')
_I_BOX = ('<rect x="2.6" y="5.4" width="11.8" height="8.6" rx="2" stroke="currentColor" '
          'stroke-width="1.6" fill="none"/>'
          '<path d="M2.6 9h11.8M6.4 5.4V4.2A1.9 1.9 0 018.3 2.3h.4a1.9 1.9 0 011.9 1.9v1.2" '
          'stroke="currentColor" stroke-width="1.6" fill="none"/>')
# 「存量盘点」：三层叠起来的盘（历史存量）
_I_LEGACY = ('<ellipse cx="8.5" cy="4.1" rx="5.4" ry="2.1" stroke="currentColor" '
             'stroke-width="1.6" fill="none"/>'
             '<path d="M3.1 4.1v4.2c0 1.2 2.4 2.1 5.4 2.1s5.4-.9 5.4-2.1V4.1" '
             'stroke="currentColor" stroke-width="1.6" fill="none"/>'
             '<path d="M3.1 8.3v4.2c0 1.2 2.4 2.1 5.4 2.1s5.4-.9 5.4-2.1V8.3" '
             'stroke="currentColor" stroke-width="1.6" fill="none"/>')

NAV_ICONS = {
    "overview": _I_HOME,
    "today": _I_TODAY,
    "legacy": _I_LEGACY,
    "clean": _I_CLEAN,
    "guardlog": _I_LOG,
    "quarantine": _I_QUAR,
    "settings": _I_SET,
}

ICON_SEARCH = _I_SEARCH
ICON_PLUS = _I_PLUS
ICON_SHIELD = _I_SHIELD
ICON_DOC = _I_DOC
ICON_BOX = _I_BOX


def brand_pixmap(size=34):
    """侧栏顶部的品牌图标（盾牌 + 对勾，设计稿原样）。"""
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="34" height="34" viewBox="0 0 34 34">'
           '<rect width="34" height="34" rx="9" fill="#2E9E5B"/>'
           '<path d="M17 7l8 3v6.2c0 5-3.4 8.9-8 10.8-4.6-1.9-8-5.8-8-10.8V10l8-3z" '
           'fill="#ffffff" opacity=".95"/>'
           '<path d="M13.4 16.6l2.6 2.6 4.8-5" stroke="#2E9E5B" stroke-width="2.2" '
           'fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>')
    try:
        r = QSvgRenderer(bytearray(svg.encode("utf-8")))
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        r.render(p)
        p.end()
        return pm
    except Exception:
        pm = QPixmap(size, size)
        pm.fill(QColor(C["green"]))
        return pm


# ============================================================
# 滑块开关
# ============================================================

class ToggleSwitch(QWidget):
    """设计稿里的 iOS 风滑块开关，接口与 QCheckBox 保持一致。"""

    toggled = Signal(bool)

    def __init__(self, checked=False, parent=None):
        super().__init__(parent)
        self.setFixedSize(40, 22)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._checked = bool(checked)
        self._pos = 1.0 if self._checked else 0.0
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(140)
        self._anim.valueChanged.connect(self._on_anim)
        self.setAccessibleName("开关")

    def _on_anim(self, v):
        try:
            self._pos = float(v)
        except Exception:
            pass
        self.update()

    def isChecked(self):
        return self._checked

    def setChecked(self, v):
        v = bool(v)
        if v == self._checked:
            return
        self._checked = v
        try:
            self._anim.stop()
            self._anim.setStartValue(self._pos)
            self._anim.setEndValue(1.0 if v else 0.0)
            self._anim.start()
        except Exception:
            self._pos = 1.0 if v else 0.0
        self.update()
        self.toggled.emit(v)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.setChecked(not self._checked)
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        e.accept()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        # 轨道
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(C["green"] if self._checked else C["sw_off"]))
        p.drawRoundedRect(QRectF(0, 0, w, h), h / 2.0, h / 2.0)
        # 滑块
        d = h - 6
        x = 3 + self._pos * (w - 6 - d)
        p.setBrush(QColor("#FFFFFF"))
        p.setPen(QPen(QColor(0, 0, 0, 40), 1))
        p.drawEllipse(QRectF(x, 3, d, d))
        p.end()


class SwitchRow(QWidget):
    """开关行：[滑块] 标题 / 说明"""

    def __init__(self, title, desc="", checked=False, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 8, 0, 8)
        lay.setSpacing(12)
        self.sw = ToggleSwitch(checked, self)
        lay.addWidget(self.sw, 0, Qt.AlignmentFlag.AlignTop)
        box = QVBoxLayout()
        box.setSpacing(1)
        t = QLabel(title)
        t.setStyleSheet(f"font-weight:600; font-size:{human_font(13.5)}pt;")
        box.addWidget(t)
        if desc:
            d = QLabel(desc)
            d.setStyleSheet(f"color:{C['ink3']}; font-size:{human_font(12)}pt;")
            d.setWordWrap(True)
            box.addWidget(d)
        lay.addLayout(box, 1)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        # 兜底：任何情况下都不允许布局把这行压扁，压扁会盖住文字
        self.setMinimumHeight(max(40, self.sizeHint().height()))

    def isChecked(self):
        return self.sw.isChecked()

    def setChecked(self, v):
        self.sw.setChecked(v)


# ============================================================
# 环形仪表盘
# ============================================================

class Gauge(QWidget):
    """概况页的大圆环：中间百分比，下面一行小字。"""

    def __init__(self, size=168, thickness=14, parent=None):
        super().__init__(parent)
        self._size = size
        self._th = thickness
        self._pct = 0.0
        self._color = QColor(C["amber"])
        self._label = "已用"
        self.setFixedSize(size, size)

    def set_value(self, pct, color=None, label=None):
        self._pct = max(0.0, min(100.0, float(pct)))
        if color:
            self._color = QColor(color)
        if label:
            self._label = label
        self.update()

    def paintEvent(self, _e):
        s, th = self._size, self._th
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = (s - th) / 2.0 - 1
        box = QRectF(th / 2.0 + 1, th / 2.0 + 1, r * 2, r * 2)

        # 底环
        pen = QPen(QColor(C["track"]), th)
        pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        p.setPen(pen)
        p.drawEllipse(box)

        # 进度弧（12 点方向起，顺时针）
        if self._pct > 0:
            pen = QPen(self._color, th)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.drawArc(box, int(90 * 16), int(-self._pct * 3.6 * 16))

        # 中心文字
        p.setPen(QColor(C["ink"]))
        f = QFont(FONT_FAMILY, human_font(32), QFont.Weight.Bold)
        p.setFont(f)
        p.drawText(QRectF(0, s / 2.0 - 26, s, 30), Qt.AlignmentFlag.AlignCenter,
                   f"{self._pct:.0f}%")
        p.setPen(QColor(C["ink3"]))
        f2 = QFont(FONT_FAMILY, human_font(12))
        p.setFont(f2)
        p.drawText(QRectF(0, s / 2.0 + 4, s, 20), Qt.AlignmentFlag.AlignCenter,
                   self._label)
        p.end()


# ============================================================
# 侧栏导航按钮
# ============================================================

class NavButton(QPushButton):
    """左侧栏的一项：图标 + 文字，选中时变绿底白字（图标同步变白）。"""

    def __init__(self, key, text, body=None, parent=None):
        super().__init__(parent)
        self.key = key
        self._body = body or NAV_ICONS.get(key, _I_DOC)
        self.setText("  " + text)
        self.setCheckable(True)
        self.setIconSize(QSize(17, 17))
        self.setProperty("nav", True)
        self.setMinimumHeight(40)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._sync(False)

    def _sync(self, on):
        col = "#FFFFFF" if on else C["ink2"]
        self.setIcon(svg_icon(self._body, 17, col))
        # 图标颜色变了要重新套一次样式，否则某些状态下不刷新
        self.style().unpolish(self)
        self.style().polish(self)

    def setChecked(self, on):
        super().setChecked(bool(on))
        self._sync(bool(on))


# ============================================================
# 小组件：药丸标签 / 分区标题 / 空状态
# ============================================================

def pill(text, kind="ok"):
    """kind: ok / warn / bad / info"""
    lab = QLabel(text)
    colors = {"ok": (C["green_soft"], C["green_deep"], C["green"]),
              "warn": (C["amber_soft"], "#9A6A14", C["amber"]),
              "bad": (C["red_soft"], "#A03030", C["red"]),
              "info": (C["blue_soft"], "#2A5C8F", C["blue"])}
    bg, fg, _dot = colors.get(kind, colors["ok"])
    lab.setStyleSheet(
        f"background:{bg}; color:{fg}; font-weight:600;"
        f" font-size:{human_font(12)}pt; border-radius:10px; padding:3px 12px;")
    lab.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    return lab


def section_title(text):
    lab = QLabel("    " + text)
    lab.setStyleSheet(
        f"font-size:{human_font(15)}pt; font-weight:700; color:{C['ink']};"
        f"padding:6px 0 2px 0;")
    return lab


class EmptyState(QWidget):
    """列表没内容时的插画式提示。"""

    def __init__(self, title, desc, icon_body=None, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.setSpacing(6)
        ic = QLabel()
        pm = QPixmap(64, 64)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QColor(C["green_soft"]))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(6, 4, 52, 56), 8, 8)
        p.setPen(QPen(QColor("#B9D8C4"), 3, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(18, 24), QPointF(46, 24))
        p.drawLine(QPointF(18, 34), QPointF(46, 34))
        p.drawLine(QPointF(18, 44), QPointF(36, 44))
        p.setBrush(QColor(C["green"]))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QRectF(36, 34, 20, 20))
        p.setPen(QPen(QColor("#FFFFFF"), 2.6, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(42, 44), QPointF(46, 48))
        p.drawLine(QPointF(46, 48), QPointF(52, 41))
        p.end()
        ic.setPixmap(pm)
        ic.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(ic)
        t = QLabel(title)
        t.setStyleSheet(f"font-weight:600; font-size:{human_font(15)}pt;")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(t)
        d = QLabel(desc)
        d.setStyleSheet(f"color:{C['ink3']}; font-size:{human_font(13)}pt;")
        d.setWordWrap(True)
        # 固定文字区宽度：页面滚动包装会把折行标签设成"可忽略宽度"，
        # 不固定的话说明文字会被压成一条窄列、折行后被竖向裁掉
        d.setFixedWidth(420)
        d.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Minimum)
        d.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(d, 0, Qt.AlignmentFlag.AlignHCenter)
        lay.addSpacing(20)


def style_button(btn, kind=None):
    """按设计稿给按钮套样式（kind: primary / blue / ghost / danger / sm / None）。"""
    if kind:
        btn.setProperty("kind", kind)
    else:
        btn.setProperty("kind", None)
    btn.style().unpolish(btn)
    btn.style().polish(btn)
    return btn
