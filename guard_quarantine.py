# -*- coding: utf-8 -*-
"""
guard_quarantine.py — 删除保护：自建隔离区
==========================================
为什么不用回收站凑合：

  1. 回收站会被「清空」。你以为删了还有后悔药，其实别人（或你自己）一清就没了。
  2. 回收站超过容量上限会自动挤掉最老的条目，不声不响。
  3. Shift+Delete 压根不进回收站。
  4. 回收站里混着成百上千个垃圾条目，真要找回某个重要文件时很难翻。

所以守卫在每个受保护的删除发生时，把回收站里的真实数据文件（$R）**搬到自己的
隔离区**，并记录它原来的完整路径。搬这一步是在 C 盘内部做的**重命名**，
10GB 的文件也是瞬间完成，不会复制。

之后：
  · 一键还原 —— 回到原来的路径，目录结构自动重建
  · 保留期（默认 30 天）到了自动清理，或超过容量上限时先挤最老的
  · 你能随时看到「这里还有多少东西等你处置」，周报里也会提醒

边界（说实话）：
  如果删除发生时守卫没在运行，或者你用了 Shift+Delete / 命令行直接删除，
  数据不经过回收站，守卫拿不到东西，只能在发生的那一刻提醒你，救不回来。
"""

import os
import re
import json
import shutil
import datetime
from pathlib import Path


def human_size(n) -> str:
    n = float(n)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {u}" if u != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def default_root() -> Path:
    """隔离区放在 C 盘自己的目录里 —— 和回收站同盘，搬动是瞬间重命名。"""
    base = os.environ.get("LOCALAPPDATA", "") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "C盘守卫" / "隔离区"


def _safe(nm: str) -> str:
    nm = re.sub(r'[\\/:*?"<>|]', "_", nm)
    return nm[:80] if len(nm) > 80 else nm


class Quarantine:
    INDEX = "索引.json"

    def __init__(self, root: Path = None, retention_days: int = 30,
                 max_gb: float = 20.0):
        self.root = Path(root) if root else default_root()
        self.retention_days = retention_days
        self.max_gb = max_gb
        self.dir = self.root / "files"
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        self.index = self._load()

    # ---------- 索引 ----------
    def _load(self) -> list:
        try:
            return json.loads((self.root / self.INDEX).read_text(encoding="utf-8"))
        except Exception:
            return []

    def _save(self):
        try:
            (self.root / self.INDEX).write_text(
                json.dumps(self.index, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass

    # ---------- 收入隔离区 ----------
    def quarantine(self, item: dict) -> bool:
        """item 是 RecycleBin.list_items() 里的一条：含 orig / r_path / i_path / size。"""
        rp = item.get("r_path")
        if rp is None:
            return False
        rp = Path(rp)
        if not rp.exists():
            return False
        orig = Path(item["orig"])
        stamp = datetime.datetime.fromtimestamp(
            item.get("deleted_ts") or item.get("time") or time_now()).strftime("%Y%m%d")
        sub = self.dir / stamp
        try:
            sub.mkdir(parents=True, exist_ok=True)
        except OSError:
            return False
        n = 0
        while True:
            suffix = "" if n == 0 else f"_{n}"
            cand = sub / (f"{_safe(orig.name)}{suffix}")
            if not cand.exists():
                break
            n += 1
        try:
            rp.rename(str(cand))          # 同盘重命名，瞬间完成
            ip = Path(item.get("i_path", ""))
            if ip.exists():
                try:
                    ip.unlink()           # 元数据一起撤走，免得回收站里还留个"打不开的影子"
                except OSError:
                    pass
        except OSError:
            return False
        self.index.append({
            "id": f"{stamp}/{cand.name}",
            "orig": str(orig),
            "size": item.get("size", 0),
            "deleted": item.get("deleted_ts", 0),
            "taken": time_now(),
            "stored": str(cand),
        })
        self._save()
        return True

    # ---------- 还原 ----------
    def restore(self, qid: str) -> str:
        for e in self.index:
            if e["id"] == qid:
                src = Path(e["stored"])
                if not src.exists():
                    return ""
                dst = Path(e["orig"])
                if dst.exists():                       # 原位置已经被别的东西占了
                    dst = dst.with_name(f"{dst.stem}_还原_{e['id'].split('/')[0]}{dst.suffix}")
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    src.rename(str(dst))
                except OSError:
                    return ""
                self.index.remove(e)
                self._save()
                return str(dst)
        return ""

    def restore_all(self) -> list:
        return [self.restore(e["id"]) for e in list(self.index)]

    # ---------- 彻底删除 / 自动清理 ----------
    def purge(self, qid: str) -> bool:
        for e in self.index:
            if e["id"] == qid:
                try:
                    Path(e["stored"]).unlink()
                except OSError:
                    pass
                self.index.remove(e)
                self._save()
                return True
        return False

    def purge_all(self) -> int:
        n = 0
        for e in list(self.index):
            n += 1 if self.purge(e["id"]) else 0
        return n

    def auto_purge(self) -> int:
        """按保留期和容量上限清老的。返回清理条数。"""
        now = time_now()
        cut = now - self.retention_days * 86400
        gone = 0
        for e in sorted(self.index, key=lambda x: x.get("taken", 0)):
            if e.get("taken", 0) < cut:
                gone += 1 if self.purge(e["id"]) else 0
        while self.total_size() > self.max_gb * 2 ** 30:
            oldest = min(self.index, key=lambda x: x.get("taken", 0), default=None)
            if oldest is None:
                break
            if not self.purge(oldest["id"]):
                break
            gone += 1
        return gone

    # ---------- 查询 ----------
    def total_size(self) -> int:
        return sum(e.get("size", 0) for e in self.index)

    def list(self) -> list:
        """按时间倒序返回仍在隔离区的条目（自动过滤掉已经不存在的存储）。"""
        rows = []
        lost = []
        for e in self.index:
            if Path(e["stored"]).exists():
                rows.append(e)
            else:
                lost.append(e)
        if lost:
            self.index = rows
            self._save()
        rows.sort(key=lambda x: -x.get("taken", 0))
        return rows

    def summary(self) -> str:
        rows = self.list()
        if not rows:
            return "隔离区是空的"
        return f"{len(rows)} 项 / {human_size(self.total_size())}"

    def expired_soon(self, days: int = 3) -> list:
        cut = time_now() - (self.retention_days - days) * 86400
        return [e for e in self.list() if e.get("taken", 0) < cut]


def time_now() -> float:
    import time
    return time.time()


# ============================================================
# 命令行界面：C盘守卫.exe --quarantine
# ============================================================

def interactive(root: Path = None, retention_days: int = 30):
    q = Quarantine(root, retention_days=retention_days)
    q.auto_purge()
    rows = q.list()
    print("\n" + "═" * 66)
    print("  隔离区 —— 守卫替你留住的删除")
    print("═" * 66)
    print(f"  位置：{q.root}")
    print(f"  现在：{len(rows)} 项 / {human_size(q.total_size())}"
          f"，保留 {retention_days} 天后自动清理")
    if not rows:
        print("\n  空的，最近没有删除被保护的文件。")
        print("═" * 66)
        return
    print()
    for i, e in enumerate(rows[:30], 1):
        when = datetime.datetime.fromtimestamp(e.get("deleted", 0)).strftime("%m-%d %H:%M")
        age = (time_now() - e.get("taken", 0)) / 86400
        print(f"  {i:2d}. {when}删除（{age:.0f} 天前）  {human_size(e['size']):>9}")
        print(f"      {e['orig']}")
    if len(rows) > 30:
        print(f"  … 另有 {len(rows)-30} 项")
    print("\n  操作：输入编号还原该项 ｜ all=全部还原 ｜ del 编号=彻底删除 ｜ 回车=退出")
    raw = input("  > ").strip()
    if not raw:
        return
    if raw.lower() == "all":
        n = len([d for d in q.restore_all() if d])
        print(f"\n  已还原 {n} 项。")
        return
    m = re.match(r"^del\s+(\d+)$", raw.lower())
    if m:
        try:
            e = rows[int(m.group(1)) - 1]
        except Exception:
            return
        if input(f"  确认彻底删除「{e['orig']}」？删了找不回 [y/N]：").strip().lower() == "y":
            print("  已彻底删除。" if q.purge(e["id"]) else "  没删掉。")
        return
    try:
        e = rows[int(raw) - 1]
    except Exception:
        print("  没看懂。")
        return
    dest = q.restore(e["id"])
    print(f"\n  ✅ 已还原到：{dest}" if dest else "\n  ❌ 还原失败（原路径可能已被占用或文件已不存在）")
    print("═" * 66)


if __name__ == "__main__":
    interactive()
