# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for TG Web Auth — one-folder distribution."""

import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None

# Collect all data/binaries for key packages
datas = []
binaries = []
hiddenimports = []

# Camoufox: playwright-based, needs its driver
for pkg in ('playwright', 'camoufox', 'browserforge'):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# DearPyGui: OpenGL shared libs
d, b, h = collect_all('dearpygui')
datas += d
binaries += b
hiddenimports += h

# Additional hidden imports that PyInstaller may miss
hiddenimports += collect_submodules('telethon')
hiddenimports += collect_submodules('pproxy')
hiddenimports += collect_submodules('aiosqlite')
hiddenimports += [
    'cv2',
    'pyzbar',
    'pyzbar.pyzbar',
    'zxingcpp',
    'PIL',
    'PIL.Image',
    'numpy',
    'psutil',
    'screeninfo',
    'aiofiles',
    'click',
    'tkinter',
    'tkinter.filedialog',
    'tkinter.messagebox',
    'socks',  # PySocks package imports as 'socks'
    'sqlite3',
]

a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'matplotlib', 'scipy', 'pandas', 'IPython', 'jupyter',
        'notebook', 'sphinx', 'docutils', 'pytest', 'pytest_asyncio',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='TGWebAuth',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # GUI app, no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    icon=None,  # TODO: add icon
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='TGWebAuth',
)
