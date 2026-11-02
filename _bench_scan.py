# -*- coding: utf-8 -*-
"""磁盘枚举基准：对比 os.walk+stat / os.scandir / Win32 FindFirstFileExW / 多线程"""
import os, sys, time, ctypes, concurrent.futures
from ctypes import wintypes
sys.stdout.reconfigure(encoding='utf-8')

TARGETS = [
    r"C:/Windows/WinSxS",
    r"C:/Users/yongx/AppData/Local",
    r"C:/Program Files",
    r"C:/Users/yongx/Desktop",
]

def bench_walk_stat(root, limit=120000):
    n = size = 0
    t0 = time.time()
    for cur, dirs, files in os.walk(root):
        for fn in files:
            try:
                size += os.stat(os.path.join(cur, fn)).st_size
            except OSError:
                pass
            n += 1
            if n >= limit: return n, size, time.time()-t0, True
    return n, size, time.time()-t0, False

def bench_scandir(root, limit=120000):
    n = size = 0
    t0 = time.time()
    stack=[root]
    while stack:
        cur=stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        else:
                            size += e.stat(follow_symlinks=False).st_size
                            n += 1
                    except OSError: continue
        except OSError: continue
        if n >= limit: return n, size, time.time()-t0, True
    return n, size, time.time()-t0, False

class WIN32_FIND_DATAW(ctypes.Structure):
    _fields_ = [("dwFileAttributes", wintypes.DWORD),
                ("ftCreationTime", wintypes.FILETIME),
                ("ftLastAccessTime", wintypes.FILETIME),
                ("ftLastWriteTime", wintypes.FILETIME),
                ("nFileSizeHigh", wintypes.DWORD),
                ("nFileSizeLow", wintypes.DWORD),
                ("dwReserved0", wintypes.DWORD),
                ("dwReserved1", wintypes.DWORD),
                ("cFileName", ctypes.c_wchar * 260),
                ("cAlternateFileName", ctypes.c_wchar * 14)]
k32 = ctypes.windll.kernel32
k32.FindFirstFileExW.argtypes = [ctypes.c_wchar_p, ctypes.c_int, ctypes.c_void_p,
                                  ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
k32.FindFirstFileExW.restype = ctypes.c_void_p
k32.FindNextFileW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
k32.FindNextFileW.restype = wintypes.BOOL
k32.FindClose.argtypes = [ctypes.c_void_p]
k32.FindClose.restype = wintypes.BOOL
INVALID = ctypes.c_void_p(-1).value
def _entries(root):
    fd = WIN32_FIND_DATAW()
    h = k32.FindFirstFileExW(os.path.join(root, "*"), 0,
                            ctypes.byref(fd), 0, None, 0x2)  # FindExInfoBasic + LARGE_FETCH
    if not h or h == INVALID: return
    try:
        while True:
            name = fd.cFileName
            if name not in (".", ".."):
                yield (name, (fd.nFileSizeHigh << 32) | fd.nFileSizeLow,
                       fd.dwFileAttributes, fd.ftLastWriteTime)
            if not k32.FindNextFileW(h, ctypes.byref(fd)): break
    finally:
        k32.FindClose(h)

def bench_win32(root, limit=120000):
    n = size = 0; t0=time.time(); stack=[root]
    while stack:
        cur=stack.pop()
        for name, sz, attr, mt in _entries(cur):
            if attr & 0x10:
                if not (attr & 0x400):
                    stack.append(os.path.join(cur, name))
            else:
                size += sz; n += 1
        if n >= limit: return n, size, time.time()-t0, True
    return n, size, time.time()-t0, False

def _subtree(p):
    n=0; size=0
    stack=[p]
    while stack:
        cur=stack.pop()
        for name, sz, attr, mt in _entries(cur):
            if attr & 0x10:
                if not (attr & 0x400): stack.append(os.path.join(cur, name))
            else: size+=sz; n+=1
    return n,size

def bench_threaded(root, workers=8, limit=120000):
    t0=time.time()
    try: subs=[e.path for e in os.scandir(root) if e.is_dir(follow_symlinks=False)]
    except OSError: return 0,0,0,False
    n=size=0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for a,b in ex.map(_subtree, subs): n+=a; size+=b
    return n,size,time.time()-t0,False

if __name__ == "__main__":
    for t in TARGETS:
        if not os.path.isdir(t): continue
        print("\n==", t)
        res={}
        for label, fn in [("os.walk+stat", bench_walk_stat), ("os.scandir", bench_scandir),
                          ("Win32 FindFirstFileExW", bench_win32), ("Win32+8线程", bench_threaded)]:
            n,s,el,trunc = fn(t)
            res[label]=el
            print(f"  {label:24s} {el:7.2f}s  files={n:7d}  size={s/2**30:.2f}GB" + (" (截断)" if trunc else ""))
        base=res.get("os.walk+stat",0) or 1
        print("  → 相对 os.walk+stat 提速:", {k: round(base/v,1) for k,v in res.items()})
