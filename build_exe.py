#!/usr/bin/env python3
"""
Build script for TG Web Auth distributable.

Usage:
    python build_exe.py

Creates:
    dist/TGWebAuth/          — one-folder distribution
    dist/TGWebAuth.zip       — ready-to-ship archive

Structure:
    TGWebAuth/
    ├── TGWebAuth.exe
    ├── _internal/           # PyInstaller deps
    ├── camoufox/            # Browser binary (~300MB)
    ├── accounts/            # Empty (user fills)
    ├── profiles/            # Empty (created by app)
    ├── data/                # Empty (SQLite created at runtime)
    ├── logs/                # Empty (logs created at runtime)
    └── README.txt
"""

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).parent
DIST_DIR = ROOT / "dist"
APP_DIR = DIST_DIR / "TGWebAuth"


def run_pyinstaller() -> None:
    """Run PyInstaller with the spec file."""
    print("[1/4] Running PyInstaller...")
    spec = ROOT / "TGWebAuth.spec"
    if not spec.exists():
        print(f"ERROR: {spec} not found")
        sys.exit(1)

    result = subprocess.run(
        [sys.executable, "-m", "PyInstaller", str(spec), "--noconfirm"],
        cwd=str(ROOT),
    )
    if result.returncode != 0:
        print("ERROR: PyInstaller failed")
        sys.exit(1)

    if not APP_DIR.exists():
        print(f"ERROR: Expected output dir not found: {APP_DIR}")
        sys.exit(1)

    print(f"  PyInstaller output: {APP_DIR}")


def copy_camoufox() -> None:
    """Copy Camoufox browser binary into dist."""
    print("[2/4] Copying Camoufox browser...")
    try:
        from camoufox.pkgman import launch_path
        src = Path(launch_path()).parent
    except Exception as e:
        print(f"  WARNING: Could not find Camoufox: {e}")
        print("  Install with: python -m camoufox fetch")
        return

    dest = APP_DIR / "camoufox"
    if dest.exists():
        shutil.rmtree(dest)

    shutil.copytree(src, dest)
    print(f"  Copied: {src} -> {dest}")

    # Count size
    size_mb = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file()) / (1024 * 1024)
    print(f"  Camoufox size: {size_mb:.0f} MB")


def create_dirs_and_readme() -> None:
    """Create empty directories and README."""
    print("[3/4] Creating directories and README...")

    for dirname in ("accounts", "profiles", "data", "logs"):
        d = APP_DIR / dirname
        d.mkdir(exist_ok=True)
        # Add .gitkeep so ZIP preserves empty dirs
        (d / ".gitkeep").touch()

    readme_text = """\
TG Web Auth v0.1.0
==================

Автоматическая миграция Telegram-аккаунтов в браузерные профили.

Быстрый старт:
1. Положите папки с session-файлами в папку accounts/
   (или импортируйте через GUI: Import Sessions)
2. Запустите TGWebAuth.exe
3. Импортируйте прокси (Import Proxies) — файл .txt, по одному на строку:
   host:port:user:pass
4. Нажмите Auto-Assign Proxies
5. Нажмите Migrate All
6. После миграции — Fragment All для авторизации на fragment.com
7. Open — открыть браузер с профилем аккаунта

Формат аккаунтов:
    accounts/
    └── account_name/
        ├── session.session    (обязательно)
        ├── api.json           (опционально)
        └── ___config.json     (опционально, прокси/имя)

Формат прокси (один на строку):
    host:port:user:pass
    socks5:host:port:user:pass
    user:pass@host:port

Если что-то не работает:
    1. Откройте вкладку Logs
    2. Нажмите Collect Logs
    3. Отправьте созданный ZIP-файл разработчику
"""

    (APP_DIR / "README.txt").write_text(readme_text, encoding='utf-8')
    print("  Created README.txt")


def create_zip() -> None:
    """Package everything into a ZIP."""
    print("[4/4] Creating ZIP archive...")
    zip_path = DIST_DIR / "TGWebAuth.zip"

    if zip_path.exists():
        zip_path.unlink()

    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for file in APP_DIR.rglob("*"):
            if file.is_file():
                arcname = file.relative_to(DIST_DIR)
                zf.write(file, arcname)

    size_mb = zip_path.stat().st_size / (1024 * 1024)
    print(f"  Created: {zip_path} ({size_mb:.0f} MB)")


def main() -> None:
    """Build distributable."""
    print("=" * 60)
    print("TG Web Auth — Build Script")
    print("=" * 60)

    run_pyinstaller()
    copy_camoufox()
    create_dirs_and_readme()
    create_zip()

    print()
    print("=" * 60)
    print("BUILD COMPLETE")
    print(f"  Distribution: {APP_DIR}")
    print(f"  ZIP archive:  {DIST_DIR / 'TGWebAuth.zip'}")
    print("=" * 60)


if __name__ == "__main__":
    main()
