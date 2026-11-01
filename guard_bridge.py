# -*- coding: utf-8 -*-
"""
guard_bridge.py — 守卫引擎线程 与 GUI 主线程 之间的弹窗桥
=========================================================
引擎（后台线程）需要向用户提问/通知时，不能直接在子线程里创建 Qt 对话框，
必须把请求投递到主线程执行。这里用「请求队列 + 事件等待」实现跨线程问答：

  引擎线程:  bridge.ask(...)   -> 封装请求入队 -> 阻塞等 Event
  GUI 线程:  QTimer 轮询队列  -> 弹 Qt 对话框   -> 写回结果并 set()

GUI 模式未启动（比如命令行方式跑引擎）时，自动回退到原来的 MessageBoxW。
"""

import queue
import threading

# 与 c_disk_guard 中一致的按钮常量
IDYES, IDNO, IDCANCEL, IDOK = 6, 7, 2, 1
MB_ICONINFO = 0x40
MB_ICONWARNING = 0x30
MB_ICONQUESTION = 0x20
MB_ICONERROR = 0x10


class Bridge:
    def __init__(self):
        self.requests = queue.Queue()
        # 引擎线程要弹的自建对话框（QDialog 实例）也经这里转交主线程执行
        self.dialog_queue = queue.Queue()
        self.gui = None          # GUI 侧注入的处理器（BridgeUI）
        self.gui_mode = False
        self.main_thread_id = None

    # ---------- GUI 侧调用 ----------
    def attach(self, handler, main_thread_id=None):
        self.gui = handler
        self.gui_mode = True
        self.main_thread_id = main_thread_id

    def detach(self):
        self.gui_mode = False
        self.gui = None

    # ---------- 引擎侧调用 ----------
    def _call(self, kind: str, payload: dict, timeout: float = 0):
        """把一次问答交给主线程，返回用户选择。"""
        if not self.gui_mode or self.gui is None:
            return None
        # 主线程自己发起的问答：直接就地弹窗，避免"等自己轮询自己"的死锁
        if self.main_thread_id and threading.get_ident() == self.main_thread_id:
            try:
                if kind == "ask":
                    return self.gui.ask_now(payload)
                return self.gui._dialog(payload)
            except Exception:
                return None
        evt = threading.Event()
        req = {"kind": kind, "payload": payload, "event": evt, "result": None}
        try:
            self.requests.put_nowait(req)
        except queue.Full:
            return None
        evt.wait(timeout if timeout > 0 else None)
        return req["result"]

    # 通用消息框：buttons 形如 "ok" | "yesno" | "yesnocancel" | "okcancel"
    def ask(self, title: str, text: str, buttons: str = "ok",
            icon: str = "info", default: str = "") -> int:
        r = self._call("ask", {"title": title, "text": text,
                               "buttons": buttons, "icon": icon, "default": default})
        if r is None:
            return IDOK
        return r

    # 自建对话框：主线程构建执行，返回结果 dict
    def dialog(self, payload: dict):
        r = self._call("dialog", payload)
        return r if r is not None else {}

    def info(self, title: str, text: str):
        self.ask(title, text, "ok", "info")

    def warn(self, title: str, text: str):
        self.ask(title, text, "ok", "warning")


# 模块级单例，c_disk_guard 与 GUI 共用
bridge = Bridge()
