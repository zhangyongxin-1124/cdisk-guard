# -*- mode: python ; coding: utf-8 -*-
# C盘守卫 · 桌面版 打包配置（无控制台窗口，带图标）

a = Analysis(
    ['guard_gui.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['guard_cleaner', 'guard_rules', 'guard_today', 'guard_drives',
                   'guard_dashboard', 'guard_growth', 'guard_quarantine',
                   'guard_paths', 'guard_wizard', 'guard_bridge', 'guard_theme',
                   'guard_legacy', 'guard_appdir', 'guard_fastscan',
                   'guard_migrate', 'guard_attribution', 'guard_policy',
                   'guard_linkdisk',
                   'PySide6.QtSvg'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='C盘守卫',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['guard_icon.ico'],
)
