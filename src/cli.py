"""
TG Web Auth CLI
Командный интерфейс для миграции Telegram сессий в browser profiles.

Команды:
    migrate  - Мигрировать аккаунт(ы) из session в browser profile
    open     - Открыть существующий browser profile
    list     - Список профилей и аккаунтов
    check    - Проверить безопасность (fingerprint, proxy leaks)
"""
import asyncio
import atexit
import logging
import random
import sys
import os
from pathlib import Path
from typing import Optional

import psutil

from .logger import setup_logging, get_logger

logger = get_logger(__name__)


def _kill_orphan_children() -> None:
    """Kill any orphaned child processes (pproxy, camoufox) on CLI exit.

    pproxy runs as `python.exe -m pproxy`, so we check cmdline, not just name.
    Camoufox/Firefox are matched by process name.
    """
    try:
        current = psutil.Process()
        children = current.children(recursive=True)
        targets = []
        for c in children:
            try:
                name = c.name().lower()
                if any(n in name for n in ('camoufox', 'firefox')):
                    targets.append(c)
                elif 'pproxy' in ' '.join(c.cmdline()).lower():
                    targets.append(c)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        for child in targets:
            try:
                child.kill()
                logger.debug("Killed orphan process: PID=%d (%s)", child.pid, child.name())
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except Exception:
        pass  # Best-effort cleanup on exit


atexit.register(_kill_orphan_children)

# Suppress verbose logging from third-party libraries
logging.getLogger("telethon").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)

# FIX #7: WINDOWS ENCODING
if sys.platform == 'win32':
    # Устанавливаем UTF-8 для консоли Windows
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    # Также устанавливаем переменную окружения для Python
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')

try:
    import click
except ImportError as e:
    raise ImportError("click not installed. Run: pip install click") from e


from .paths import ACCOUNTS_DIR, DATA_DIR, PROFILES_DIR

# FIX #6: Default cooldown между аккаунтами (safe: 60-120s = 10-20 logins/hour)
DEFAULT_COOLDOWN = 90  # секунд (центр безопасного диапазона 60-120)


def find_account_dirs() -> list[Path]:
    """Находит все директории с аккаунтами"""
    if not ACCOUNTS_DIR.exists():
        return []

    account_dirs = []

    # Рекурсивно ищем директории с .session файлами
    for session_file in ACCOUNTS_DIR.rglob("*.session"):
        account_dirs.append(session_file.parent)

    return list(set(account_dirs))


def get_account_dir(name: str) -> Optional[Path]:
    """Находит директорию аккаунта по имени"""
    # Точное совпадение
    exact = ACCOUNTS_DIR / name
    if exact.exists() and list(exact.glob("*.session")):
        return exact

    # Поиск по части имени
    for account_dir in find_account_dirs():
        if name.lower() in account_dir.name.lower():
            return account_dir

    return None


def get_2fa_password(account_name: str, provided_password: Optional[str]) -> Optional[str]:
    """
    FIX #8: Безопасное получение 2FA пароля.

    Приоритет:
    1. Переданный через --password
    2. Переменная окружения TG_2FA_PASSWORD
    3. Интерактивный ввод (скрытый)
    """
    if provided_password:
        return provided_password

    # Проверяем env
    env_password = os.environ.get('TG_2FA_PASSWORD')
    if env_password:
        click.echo(f"[2FA] Using password from TG_2FA_PASSWORD env var")
        return env_password

    # Не спрашиваем интерактивно - возвращаем None
    # Пользователь сможет ввести вручную в браузере
    return None


@click.group()
def cli():
    """TG Web Auth - Миграция Telegram сессий в browser profiles"""
    pass


@cli.command()
@click.option("--account", "-a", help="Имя аккаунта для миграции")
@click.option("--all", "migrate_all", is_flag=True, help="Мигрировать все аккаунты")
@click.option("--password", "-p", help="2FA пароль (или используйте env TG_2FA_PASSWORD)")
@click.option("--headless", is_flag=True, help="Запуск без GUI")
@click.option("--cooldown", "-c", default=DEFAULT_COOLDOWN, type=int,
              help=f"Секунды между аккаунтами при --all (default: {DEFAULT_COOLDOWN})")
@click.option("--parallel", "-j", type=int, default=0,
              help="Параллельные браузеры (0=последовательно, 10=рекомендуется)")
@click.option("--auto-scale", is_flag=True,
              help="Автоматически подбирать параллельность по ресурсам")
@click.option("--resume", is_flag=True,
              help="Продолжить прерванную миграцию (только pending аккаунты)")
@click.option("--retry-failed", is_flag=True,
              help="Повторить только упавшие аккаунты")
@click.option("--fresh", is_flag=True,
              help="Удалить browser_data перед retry (сбросить кэш 2FA)")
@click.option("--password-file", type=click.Path(exists=True),
              help="JSON файл с паролями 2FA: {\"account_name\": \"password\"}")
@click.option("--status", "show_status", is_flag=True,
              help="Показать статус текущего batch и выйти")
@click.option("--no-proxy", is_flag=True,
              help="Игнорировать все прокси (для тестирования без VPN/proxy)")
def migrate(account: Optional[str], migrate_all: bool, password: Optional[str],
            headless: bool, cooldown: int, parallel: int, auto_scale: bool,
            resume: bool, retry_failed: bool, fresh: bool,
            password_file: Optional[str], show_status: bool, no_proxy: bool):
    """Мигрировать аккаунт(ы) из session в browser profile"""
    import json
    import shutil
    import signal
    import sqlite3
    from .telegram_auth import migrate_account, migrate_accounts_batch, ParallelMigrationController
    from .database import Database

    DB_PATH = Path("data/tgwebauth.db")

    # FIX-5.2: Load password file for 2FA
    passwords_map: dict = {}
    if password_file:
        try:
            with open(password_file, 'r', encoding='utf-8') as f:
                passwords_map = json.load(f)
            click.echo(f"Loaded {len(passwords_map)} passwords from {password_file}")
        except Exception as e:
            click.echo(f"Error loading password file: {e}")
            sys.exit(1)

    # FIX-2.4: --fresh flag is processed after _resolve_accounts() so we know which accounts failed
    if fresh and not retry_failed:
        click.echo("Warning: --fresh has no effect without --retry-failed")

    async def _connect_db() -> Database:
        """Initialize and connect database (async)."""
        db = Database(DB_PATH)
        await db.initialize()
        await db.connect()
        return db

    # Handle --status flag
    if show_status:
        async def _show_status():
            db = await _connect_db()
            try:
                batch_status = await db.get_batch_status()
                stats = await db.get_migration_stats()

                if batch_status is None:
                    click.echo("No active migration batch")
                else:
                    click.echo(f"Batch: {batch_status['batch_id']}")
                    click.echo(f"Started: {batch_status['started_at']}")
                    click.echo(f"Progress: {batch_status['completed']}/{batch_status['total']} completed")
                    click.echo(f"  - Completed: {batch_status['completed']}")
                    click.echo(f"  - Failed: {batch_status['failed']}")
                    click.echo(f"  - Pending: {batch_status['pending']}")
                    if batch_status["is_finished"]:
                        click.echo(f"Finished: {batch_status['finished_at']}")
                    else:
                        click.echo("Status: IN PROGRESS")

                click.echo(f"\nDB Stats: {stats['total']} accounts, "
                           f"{stats['healthy']} healthy, {stats['error']} error, "
                           f"{stats['pending']} pending")
            finally:
                await db.close()

        asyncio.run(_show_status())
        sys.exit(0)

    if not account and not migrate_all and not resume and not retry_failed:
        click.echo("Error: Укажите --account, --all, --resume, или --retry-failed")
        sys.exit(1)

    if account:
        account_dir = get_account_dir(account)
        if not account_dir:
            click.echo(f"Error: Аккаунт '{account}' не найден")
            click.echo(f"Доступные аккаунты:")
            for d in find_account_dirs():
                click.echo(f"  - {d.name}")
            sys.exit(1)

        click.echo(f"Мигрирую: {account_dir.name}")

        # FIX #8: Безопасное получение пароля (CLI arg > password-file > env)
        password_2fa = password or passwords_map.get(account_dir.name) or get_2fa_password(account_dir.name, None)

        # Load proxy from DB for this account (overrides ___config.json)
        single_proxy = None
        if no_proxy:
            single_proxy = "NONE"  # Special value: strip all proxies
            click.echo("No-proxy mode: все прокси отключены")
        else:
            async def _get_single_proxy():
                db = await _connect_db()
                try:
                    pmap = await db.get_proxy_map()
                    return pmap.get(account_dir.name)
                finally:
                    await db.close()

            single_proxy = asyncio.run(_get_single_proxy())

        try:
            result = asyncio.run(migrate_account(
                account_dir=account_dir,
                password_2fa=password_2fa,
                headless=headless,
                proxy_override=single_proxy,
            ))
        except KeyboardInterrupt:
            click.echo("\nПрервано пользователем (Ctrl+C).")
            return

        if result.success:
            click.echo(click.style(f"\n✓ Успешно: {result.profile_name}", fg="green"))
            click.echo(f"  Telethon session alive: {result.telethon_alive}")
        else:
            click.echo(click.style(f"\n✗ Ошибка: {result.error}", fg="red"))
            sys.exit(1)

    elif migrate_all or resume or retry_failed:
        # Initialize database for batch tracking
        batch_db_id = None  # Internal DB row ID for batch

        async def _resolve_accounts():
            """Resolve which accounts to migrate and start/resume batch."""
            nonlocal batch_db_id
            db = await _connect_db()
            try:
                # Reset accounts stuck in "migrating" from previous crash
                await db.reset_interrupted_migrations()

                if resume:
                    active = await db.get_active_batch()
                    if not active:
                        click.echo("Нет прерванной миграции для продолжения")
                        click.echo("Используйте --all для новой миграции")
                        return None, None

                    batch_db_id = active["id"]
                    pending_names = await db.get_batch_pending(batch_db_id)
                    click.echo(f"Продолжение batch: {active['batch_id']}")
                    click.echo(f"Осталось: {len(pending_names)} аккаунтов")

                    all_accounts = find_account_dirs()
                    found = [d for d in all_accounts if d.name in pending_names]
                    if not found:
                        click.echo("Все pending аккаунты уже обработаны")
                        return None, None
                    return found, batch_db_id

                elif retry_failed:
                    batch_status = await db.get_batch_status()
                    if not batch_status:
                        # Fallback: get last completed batch
                        last_batch = await db.get_last_batch()
                        if not last_batch:
                            click.echo("Нет упавших аккаунтов для повтора")
                            return None, None
                        batch_db_id = last_batch["id"]
                    else:
                        batch_db_id = batch_status["batch_db_id"]

                    failed_entries = await db.get_batch_failed(batch_db_id)
                    if not failed_entries:
                        click.echo("Нет упавших аккаунтов для повтора")
                        return None, None

                    failed_names = [f["account"] for f in failed_entries]
                    click.echo(f"Повтор {len(failed_names)} упавших аккаунтов:")
                    for name in failed_names:
                        click.echo(f"  - {name}")

                    all_accounts = find_account_dirs()
                    found = [d for d in all_accounts if d.name in failed_names]

                    # Start a new batch for retry
                    names = [d.name for d in found]
                    new_batch_id = await db.start_batch(names)
                    # Get the new batch's db id
                    new_active = await db.get_active_batch()
                    batch_db_id = new_active["id"] if new_active else None
                    return found, batch_db_id

                else:
                    # Normal --all
                    found = find_account_dirs()
                    if not found:
                        click.echo("Нет аккаунтов для миграции")
                        return None, None

                    names = [d.name for d in found]
                    # Ensure all accounts exist in DB
                    for d in found:
                        try:
                            await db.add_account(
                                name=d.name,
                                session_path=str(d / "session.session"),
                            )
                        except sqlite3.IntegrityError:
                            pass  # Account already exists (unique session_path)

                    new_batch_id = await db.start_batch(names)
                    new_active = await db.get_active_batch()
                    batch_db_id = new_active["id"] if new_active else None
                    click.echo(f"Новый batch: {new_batch_id}")
                    return found, batch_db_id
            finally:
                await db.close()

        resolved = asyncio.run(_resolve_accounts())
        if resolved is None or resolved[0] is None:
            sys.exit(0)
        accounts, batch_db_id = resolved

        click.echo(f"Аккаунтов для миграции: {len(accounts)}")

        # FIX-2.4: --fresh flag deletes browser_data ONLY for resolved (failed) accounts
        if fresh and retry_failed and accounts:
            profiles_dir = PROFILES_DIR
            failed_names = {d.name for d in accounts}
            deleted = 0
            if profiles_dir.exists():
                for profile_dir in profiles_dir.iterdir():
                    if (profile_dir.is_dir()
                            and profile_dir.name in failed_names
                            and (profile_dir / "browser_data").exists()):
                        try:
                            shutil.rmtree(profile_dir / "browser_data")
                            deleted += 1
                        except Exception as e:
                            logger.warning(f"Failed to delete {profile_dir / 'browser_data'}: {e}")
            click.echo(f"Fresh mode: deleted {deleted} browser_data directories (of {len(failed_names)} failed)")

        # FIX #8: Безопасное получение пароля (CLI arg or env)
        password_2fa = get_2fa_password("all", password)

        # Load proxy map from DB (overrides ___config.json proxies)
        proxy_map = None

        if no_proxy:
            # Special proxy_map: all accounts get "NONE" → strip all proxies
            proxy_map = {d.name: "NONE" for d in accounts}
            click.echo(f"No-proxy mode: все прокси отключены для {len(accounts)} аккаунтов")
        else:
            async def _load_proxy_map():
                db = await _connect_db()
                try:
                    return await db.get_proxy_map()
                finally:
                    await db.close()

            try:
                proxy_map = asyncio.run(_load_proxy_map())
                if proxy_map:
                    click.echo(f"DB прокси загружены для {len(proxy_map)} аккаунтов")
            except Exception as e:
                logger.warning(f"Could not load proxy map from DB: {e}")

        if parallel > 0 or auto_scale:
            # Параллельный режим
            from .resource_monitor import ResourceMonitor

            monitor = None
            if auto_scale:
                monitor = ResourceMonitor()
                if parallel == 0:
                    parallel = monitor.recommended_concurrency()
                click.echo(f"Ресурсы: {monitor.format_status()}")
                click.echo(f"Рекомендуемая параллельность: {monitor.recommended_concurrency()}")

            click.echo(f"Режим: ПАРАЛЛЕЛЬНЫЙ (max {parallel} браузеров)")
            click.echo(f"Cooldown между запусками: {cooldown}s")

            controller = ParallelMigrationController(
                max_concurrent=parallel,
                cooldown=cooldown,
                resource_monitor=monitor if auto_scale else None
            )

            # Signal handler для graceful shutdown
            def handle_signal(signum, frame):
                click.echo("\nПолучен сигнал прерывания...")
                controller.request_shutdown()

            signal.signal(signal.SIGINT, handle_signal)
            if hasattr(signal, 'SIGTERM'):
                signal.signal(signal.SIGTERM, handle_signal)

            # FIX-4.2 + FIX-C5: DB update per-account via progress callback (crash-safe)
            # Use a shared DB connection for the batch, closed in finally
            _parallel_db = None

            async def _init_parallel_db():
                nonlocal _parallel_db
                _parallel_db = await _connect_db()

            async def _close_parallel_db():
                nonlocal _parallel_db
                if _parallel_db:
                    await _parallel_db.close()
                    _parallel_db = None

            async def _on_progress_async(completed, total, result):
                """Update DB immediately for each completed account."""
                status_str = click.style("OK", fg="green") if result and result.success else click.style("FAIL", fg="red")
                name = result.profile_name if result else "?"
                click.echo(f"  [{completed}/{total}] {status_str} {name}")
                if batch_db_id and result and _parallel_db:
                    try:
                        if result.success:
                            await _parallel_db.mark_batch_account_completed(batch_db_id, result.profile_name)
                        else:
                            await _parallel_db.mark_batch_account_failed(batch_db_id, result.profile_name, result.error or "Unknown error")
                    except Exception as e:
                        logger.warning(f"DB update error: {e}")

            async def _run_parallel():
                await _init_parallel_db()
                try:
                    return await controller.run(
                        account_dirs=accounts,
                        password_2fa=password_2fa,
                        headless=headless,
                        on_progress=_on_progress_async,
                        passwords_map=passwords_map if passwords_map else None,
                        proxy_map=proxy_map,
                    )
                finally:
                    await _close_parallel_db()

            try:
                results = asyncio.run(_run_parallel())
            except KeyboardInterrupt:
                click.echo("\nПрервано пользователем (Ctrl+C).")
                return
        else:
            # Последовательный режим (существующий)
            click.echo(f"Режим: ПОСЛЕДОВАТЕЛЬНЫЙ")
            click.echo(f"Cooldown между аккаунтами: {cooldown}s")

            # FIX-4.2: Per-account crash-safe DB update callback
            # Use shared DB connection for entire batch (avoid 1000 open/close cycles)
            _seq_db = None

            async def _run_sequential():
                nonlocal _seq_db
                _seq_db = await _connect_db()
                try:
                    async def _on_result_sequential(result):
                        status_str = click.style("OK", fg="green") if result.success else click.style("FAIL", fg="red")
                        click.echo(f"  {status_str} {result.profile_name}")
                        if batch_db_id and _seq_db:
                            try:
                                if result.success:
                                    await _seq_db.mark_batch_account_completed(batch_db_id, result.profile_name)
                                else:
                                    await _seq_db.mark_batch_account_failed(batch_db_id, result.profile_name, result.error or "Unknown error")
                            except Exception as e:
                                logger.warning(f"DB update error: {e}")

                    # FIX #6: Используем batch функцию с cooldown
                    return await migrate_accounts_batch(
                        account_dirs=accounts,
                        password_2fa=password_2fa,
                        headless=headless,
                        cooldown=cooldown,
                        on_result=_on_result_sequential,
                        passwords_map=passwords_map if passwords_map else None,
                        proxy_map=proxy_map,
                    )
                finally:
                    if _seq_db:
                        await _seq_db.close()

            try:
                results = asyncio.run(_run_sequential())
            except KeyboardInterrupt:
                click.echo("\nПрервано пользователем (Ctrl+C).")
                return

        # Итог
        success = [r for r in results if r.success]
        failed = [r for r in results if not r.success]

        click.echo(f"\n{'='*50}")
        click.echo("BATCH COMPLETE")
        click.echo(f"{'='*50}")
        click.echo(f"Total:      {len(results)}")
        click.echo(click.style(f"Success:    {len(success)} ({len(success)*100//max(len(results),1)}%)", fg="green"))
        click.echo(click.style(f"Failed:     {len(failed)}", fg="red" if failed else "green"))

        # FIX-4.3: Error breakdown by category (uses auto-classified error_category from AuthResult)
        if failed:
            error_categories: dict = {}
            for result in failed:
                category = result.error_category or "unknown"
                error_categories.setdefault(category, []).append(result)

            click.echo("\nError breakdown:")
            category_hints = {
                "dead_session": "Replace session files",
                "bad_proxy": "Run: proxy-refresh",
                "qr_decode_fail": "Auto-retry recommended (--retry-failed)",
                "2fa_required": "Provide passwords via --password-file",
                "rate_limited": "Wait and retry",
                "timeout": "Check proxy speed",
                "browser_crash": "Retry (--retry-failed)",
                "unknown": "Manual investigation needed",
            }
            for cat, items in sorted(error_categories.items(), key=lambda x: -len(x[1])):
                hint = category_hints.get(cat, "")
                click.echo(f"  {cat:20s} {len(items):>4d}  — {hint}")

            click.echo("\nFailed accounts:")
            for result in failed:
                click.echo(f"  - {result.profile_name}: {result.error}")
            click.echo("\nИспользуйте --retry-failed для повтора упавших")

        # Show batch summary from DB
        async def _show_batch_summary():
            db = await _connect_db()
            try:
                status_info = await db.get_batch_status()
                if status_info:
                    click.echo(f"\nСтатус batch: {status_info['batch_id']}")
                    if status_info["pending"] > 0:
                        click.echo(f"Осталось pending: {status_info['pending']}")
                        click.echo("Используйте --resume для продолжения")
                    else:
                        await db.finish_batch(status_info["batch_db_id"])
                        click.echo("Batch завершён")
            finally:
                await db.close()

        asyncio.run(_show_batch_summary())


@cli.command(name="open")
@click.option("--account", "-a", required=True, help="Имя профиля для открытия")
@click.option("--url", default="https://web.telegram.org/k/", help="URL для открытия")
def open_profile(account: str, url: str):
    """Открыть существующий browser profile"""
    from .browser_manager import BrowserManager
    import json
    import builtins

    manager = BrowserManager()
    profile = manager.get_profile(account)

    if not profile.exists():
        click.echo(f"Error: Профиль '{account}' не найден")
        click.echo("Доступные профили:")
        for p in manager.list_profiles():
            click.echo(f"  - {p.name}")
        sys.exit(1)

    # Загружаем прокси из конфига
    if profile.config_path.exists():
        with builtins.open(profile.config_path, encoding='utf-8') as f:
            config = json.load(f)
            profile.proxy = config.get('proxy')

    click.echo(f"Открываю профиль: {profile.name}")
    click.echo(f"URL: {url}")
    if profile.proxy:
        # Маскируем credentials
        proxy_parts = profile.proxy.split(":")
        if len(proxy_parts) >= 3:
            safe_proxy = f"{proxy_parts[0]}:{proxy_parts[1]}:{proxy_parts[2]}:***"
            click.echo(f"Proxy: {safe_proxy}")

    async def run():
        ctx = None
        try:
            ctx = await manager.launch(profile, headless=False)
            page = await ctx.new_page()
            await page.goto(url)

            click.echo("\nБраузер открыт. Нажмите Ctrl+C для закрытия.")

            try:
                while True:
                    await asyncio.sleep(1)
            except KeyboardInterrupt:
                pass
        finally:
            if ctx:
                try:
                    await ctx.close()
                except Exception:
                    pass
            await manager.close_all()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        click.echo("\nПрервано пользователем (Ctrl+C).")


@cli.command("list")
def list_cmd():
    """Список аккаунтов и профилей"""
    from .browser_manager import BrowserManager

    click.echo("АККАУНТЫ (session файлы):")
    click.echo("-" * 40)

    accounts = find_account_dirs()
    if accounts:
        for account_dir in accounts:
            session_files = list(account_dir.glob("*.session"))
            has_api = (account_dir / "api.json").exists()
            has_config = (account_dir / "___config.json").exists()

            status = []
            if has_api:
                status.append("api")
            if has_config:
                status.append("config")

            click.echo(f"  {account_dir.name}")
            click.echo(f"    Session: {session_files[0].name if session_files else 'N/A'}")
            click.echo(f"    Files: {', '.join(status) if status else 'minimal'}")
    else:
        click.echo("  (нет аккаунтов)")

    click.echo(f"\nПРОФИЛИ (browser profiles):")
    click.echo("-" * 40)

    manager = BrowserManager()
    profiles = manager.list_profiles()

    if profiles:
        for profile in profiles:
            storage_exists = profile.storage_state_path.exists()
            click.echo(f"  {profile.name}")
            click.echo(f"    Path: {profile.browser_data_path}")
            click.echo(f"    Storage: {'✓' if storage_exists else '✗'}")
            if profile.proxy:
                # Скрываем credentials (FIX #8)
                proxy_parts = profile.proxy.split(":")
                if len(proxy_parts) >= 3:
                    safe_proxy = f"{proxy_parts[0]}:{proxy_parts[1]}:{proxy_parts[2]}:***"
                    click.echo(f"    Proxy: {safe_proxy}")
    else:
        click.echo("  (нет профилей)")


@cli.command()
@click.option("--proxy", "-p", required=True, help="Прокси: socks5:host:port:user:pass")
@click.option("--profile", help="Имя профиля для сохранения результатов")
@click.option("--headless", is_flag=True, help="Запуск без GUI")
@click.option("--geoip", is_flag=True, help="Использовать автодетект timezone по IP")
def check(proxy: str, profile: Optional[str], headless: bool, geoip: bool):
    """Проверить безопасность браузера (fingerprint, WebRTC leaks)"""
    from .security_check import run_security_check, print_summary

    profile_path = None
    if profile:
        profile_path = PROFILES_DIR / profile

    click.echo("Запускаю проверку безопасности...")

    try:
        result = asyncio.run(run_security_check(
            proxy=proxy,
            profile_path=profile_path,
            headless=headless,
            use_geoip=geoip
        ))
    except KeyboardInterrupt:
        click.echo("\nПрервано пользователем (Ctrl+C).")
        return

    print_summary(result)

    if result.is_safe:
        click.echo(click.style("\n✓ Безопасно для использования", fg="green"))
    else:
        click.echo(click.style("\n✗ Обнаружены проблемы безопасности!", fg="red"))
        sys.exit(1)


@cli.command()
@click.option("--account", "-a", required=True, help="Имя аккаунта для проверки")
def health(account: str):
    """
    Проверить здоровье аккаунта после миграции.

    Проверяет:
    - Telethon сессия работает (get_me)
    - Web профиль существует
    - Нет ограничений на аккаунте
    """
    import json
    from telethon import TelegramClient

    # Найти аккаунт
    account_dir = get_account_dir(account)
    if not account_dir:
        click.echo(click.style(f"Аккаунт '{account}' не найден", fg="red"))
        sys.exit(1)

    click.echo(f"Проверяю здоровье: {account_dir.name}")
    click.echo("=" * 50)

    # 1. Проверка Telethon сессии
    click.echo("\n[1/3] Telethon сессия...")

    session_file = account_dir / "session.session"
    api_file = account_dir / "api.json"
    config_file = account_dir / "___config.json"

    if not session_file.exists():
        click.echo(click.style("  ✗ session.session не найден", fg="red"))
        sys.exit(1)

    if not api_file.exists():
        click.echo(click.style("  ✗ api.json не найден", fg="red"))
        sys.exit(1)

    me = None
    try:
        with open(api_file, encoding='utf-8') as f:
            api = json.load(f)

        proxy = None
        if config_file.exists():
            with open(config_file, encoding='utf-8') as f:
                config = json.load(f)
            proxy_str = config.get("Proxy", "")
            if proxy_str:
                from .telegram_auth import parse_telethon_proxy
                proxy = parse_telethon_proxy(proxy_str)

        async def check_telethon():
            client = TelegramClient(
                str(session_file.with_suffix("")),
                api["api_id"],
                api["api_hash"],
                proxy=proxy
            )
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    return None, "NOT_AUTHORIZED"
                me = await client.get_me()
                return me, None
            except Exception as e:
                return None, str(e)
            finally:
                await client.disconnect()

        try:
            me, error = asyncio.run(check_telethon())
        except KeyboardInterrupt:
            click.echo("\nПрервано пользователем (Ctrl+C).")
            return

        if error:
            click.echo(click.style(f"  ✗ Ошибка: {error}", fg="red"))
        else:
            click.echo(click.style("  ✓ Сессия активна", fg="green"))
            click.echo(f"    ID: {me.id}")
            click.echo(f"    Username: @{me.username}" if me.username else "    Username: нет")

    except Exception as e:
        click.echo(click.style(f"  ✗ Ошибка: {e}", fg="red"))

    # 2. Проверка Web профиля
    click.echo("\n[2/3] Web профиль...")

    from .browser_manager import BrowserManager
    manager = BrowserManager(profiles_dir=PROFILES_DIR)

    # Ищем профиль по имени из config или имени папки
    profile_name = account_dir.name
    if config_file.exists():
        try:
            with open(config_file, encoding='utf-8') as f:
                config = json.load(f)
            profile_name = config.get("Name", profile_name)
        except Exception:
            pass

    profile = manager.get_profile(profile_name)

    if profile.exists:
        click.echo(click.style("  ✓ Профиль существует", fg="green"))
        click.echo(f"    Path: {profile.browser_data_path}")

        storage_exists = profile.storage_state_path.exists()
        if storage_exists:
            click.echo(click.style("  ✓ Storage state сохранён", fg="green"))
        else:
            click.echo(click.style("  ✗ Storage state отсутствует", fg="yellow"))
    else:
        click.echo(click.style("  ✗ Профиль не найден (нужна миграция)", fg="yellow"))

    # 3. Итог
    click.echo("\n[3/3] Итог")
    click.echo("=" * 50)

    if me and profile.exists:
        click.echo(click.style("✓ Аккаунт здоров!", fg="green"))
        click.echo("  Telethon и Web профиль работают.")
    elif me and not profile.exists:
        click.echo(click.style("⚠ Требуется миграция", fg="yellow"))
        click.echo("  Telethon работает, но Web профиль не создан.")
        click.echo(f"  Запустите: python -m src.cli migrate --account \"{account}\"")
    else:
        click.echo(click.style("✗ Проблемы с аккаунтом", fg="red"))
        click.echo("  Проверьте сессию и прокси.")


@cli.command()
@click.option("--account", "-a", help="Имя аккаунта для авторизации на Fragment")
@click.option("--all", "fragment_all", is_flag=True, help="Авторизовать все аккаунты")
@click.option("--retry-failed", "retry_failed", is_flag=True,
              help="Повторить упавшие fragment авторизации (fragment_status='error')")
@click.option("--headed", is_flag=True, help="Запуск с GUI (по умолчанию headless)")
@click.option("--cooldown", "-c", default=DEFAULT_COOLDOWN, type=int,
              help=f"Секунды между аккаунтами при --all (default: {DEFAULT_COOLDOWN})")
def fragment(account: Optional[str], fragment_all: bool, retry_failed: bool,
             headed: bool, cooldown: int):
    """Авторизация на fragment.com через Telegram Login Widget"""
    from .telegram_auth import AccountConfig
    from .fragment_auth import FragmentAuth
    from .browser_manager import BrowserManager
    from .database import Database

    setup_logging(level=logging.INFO)

    # Headless by default — headed Camoufox hangs on some systems,
    # and Fragment auth works fully in headless (popup is intercepted by Playwright).
    headless = not headed

    if not account and not fragment_all and not retry_failed:
        click.echo("Укажите --account, --all, или --retry-failed")
        return

    async def _run_single(account_dir: Path, browser_manager: "BrowserManager") -> Optional["FragmentResult"]:
        """Запускает fragment auth для одного аккаунта. Returns result or None on config error."""
        try:
            config = AccountConfig.load(account_dir)
        except Exception as e:
            click.echo(click.style(f"  SKIP {account_dir.name}: {e}", fg="yellow"))
            return None

        auth = FragmentAuth(config, browser_manager)
        result = await auth.connect(headless=headless)

        if result.success:
            status = "already authorized" if result.already_authorized else "connected"
            click.echo(click.style(f"  OK {config.name}: {status}", fg="green"))
        else:
            click.echo(click.style(f"  FAIL {config.name}: {result.error}", fg="red"))
        return result

    async def _get_filtered_account_dirs() -> Optional[list[Path]]:
        """FIX-B1/B2: Filter account dirs using DB fragment_status."""
        all_dirs = find_account_dirs()
        if not all_dirs:
            click.echo(f"Нет аккаунтов в {ACCOUNTS_DIR}/")
            return None

        # Open DB to check fragment_status
        db = Database(DATA_DIR / "tgwebauth.db")
        await db.initialize()
        await db.connect()
        try:
            accounts = await db.list_accounts()
        finally:
            await db.close()

        # Build name → fragment_status map
        status_map = {a.name: a.fragment_status for a in accounts}

        if retry_failed:
            # FIX-B2: Only accounts with fragment_status = 'error'
            filtered = [d for d in all_dirs if status_map.get(d.name) == "error"]
            if not filtered:
                click.echo("Нет упавших fragment авторизаций для повтора")
                return None
            click.echo(f"Повтор {len(filtered)} упавших fragment авторизаций:")
            for d in filtered:
                click.echo(f"  - {d.name}")
        else:
            # FIX-B1: Skip already authorized (like GUI does at app.py:1469)
            already_auth = [d.name for d in all_dirs if status_map.get(d.name) == "authorized"]
            filtered = [d for d in all_dirs if status_map.get(d.name) != "authorized"]
            if already_auth:
                click.echo(click.style(
                    f"Пропущено {len(already_auth)} уже авторизованных аккаунтов",
                    fg="cyan"
                ))
            if not filtered:
                click.echo("Все аккаунты уже авторизованы на fragment.com!")
                return None

        return filtered

    async def _run_batch(account_dirs: list[Path], browser_manager: "BrowserManager") -> None:
        """Run fragment auth for a batch of accounts with cooldown and summary."""
        label = "retry" if retry_failed else "batch"
        click.echo(f"Fragment auth ({label}) для {len(account_dirs)} аккаунтов (headless={headless})")
        results = []

        for i, account_dir in enumerate(account_dirs):
            click.echo(f"\n[{i+1}/{len(account_dirs)}] {account_dir.name}")
            result = await _run_single(account_dir, browser_manager)
            results.append((account_dir.name, result))

            # Cooldown between accounts (skip after last)
            if i < len(account_dirs) - 1:
                actual_cooldown = cooldown + random.randint(-10, 10)
                actual_cooldown = max(10, actual_cooldown)
                await asyncio.sleep(actual_cooldown)

        # Categorized summary
        ok_new = []
        ok_existing = []
        fail_session = []
        fail_ip = []
        fail_timeout = []
        fail_other = []
        skipped = []

        for name, r in results:
            if r is None:
                skipped.append(name)
            elif r.success and r.already_authorized:
                ok_existing.append(name)
            elif r.success:
                ok_new.append(name)
            elif r.error and "not authorized" in r.error.lower():
                fail_session.append(name)
            elif r.error and "AuthKeyDuplicated" in (r.error or ""):
                fail_ip.append(name)
            elif r.error and ("timeout" in r.error.lower() or "Timeout" in (r.error or "")):
                fail_timeout.append(name)
            else:
                fail_other.append((name, r.error))

        total_ok = len(ok_new) + len(ok_existing)
        total_fail = len(fail_session) + len(fail_ip) + len(fail_timeout) + len(fail_other)

        click.echo(f"\n{'=' * 50}")
        click.echo("ИТОГ FRAGMENT AUTH")
        click.echo(f"{'=' * 50}")
        click.echo(click.style(f"  OK: {total_ok}/{len(results)}", fg="green"))
        if ok_new:
            click.echo(f"    Новых: {len(ok_new)}")
        if ok_existing:
            click.echo(f"    Уже авторизованы: {len(ok_existing)}")
        if total_fail > 0:
            click.echo(click.style(f"  FAIL: {total_fail}", fg="red"))
            if fail_session:
                click.echo(f"    Мёртвые сессии ({len(fail_session)}): {', '.join(fail_session)}")
            if fail_ip:
                click.echo(f"    Конфликт IP ({len(fail_ip)}): {', '.join(fail_ip)}")
            if fail_timeout:
                click.echo(f"    Таймауты ({len(fail_timeout)}): {', '.join(fail_timeout)}")
            if fail_other:
                for name, err in fail_other:
                    click.echo(f"    {name}: {err}")
        if skipped:
            click.echo(click.style(f"  SKIP: {len(skipped)}", fg="yellow"))
        if total_fail > 0:
            click.echo(f"\nИспользуйте --retry-failed для повтора упавших")

    async def _run():
        # Fix #21: Shared BrowserManager with global LRU eviction
        browser_manager = BrowserManager()
        try:
            if account:
                account_dir = get_account_dir(account)
                if not account_dir:
                    click.echo(f"Аккаунт '{account}' не найден в {ACCOUNTS_DIR}/")
                    return
                await _run_single(account_dir, browser_manager)
            elif fragment_all or retry_failed:
                account_dirs = await _get_filtered_account_dirs()
                if not account_dirs:
                    return
                await _run_batch(account_dirs, browser_manager)
        finally:
            await browser_manager.close_all()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        click.echo("\nПрервано пользователем")


@cli.command("check-proxies")
@click.option("--concurrency", "-j", default=50, type=int, help="Concurrent checks")
@click.option("--timeout", "-t", default=5.0, type=float, help="Timeout per check (seconds)")
@click.option("--db-path", default="data/tgwebauth.db", help="Database path")
def check_proxies(concurrency: int, timeout: float, db_path: str):
    """Batch check all proxies for connectivity."""
    from .database import Database
    from .proxy_health import check_proxy_batch

    async def _run() -> None:
        db = Database(Path(db_path))
        await db.initialize()
        await db.connect()
        try:
            proxies = await db.list_proxies()
            count = len(proxies)
            if count == 0:
                click.echo("No proxies in database.")
                return

            click.echo(f"Checking {count} proxies with concurrency={concurrency}...")

            def on_progress(completed: int, total: int, result) -> None:
                if completed % 50 == 0 or completed == total:
                    click.echo(f"  [{completed}/{total}] checked...")

            summary = await check_proxy_batch(
                db, concurrency=concurrency, timeout=timeout,
                progress_callback=on_progress,
            )
            click.echo(
                f"Results: {summary['alive']} alive, {summary['dead']} dead, "
                f"{summary['changed']} changed"
            )
        finally:
            await db.close()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        click.echo("\nПрервано пользователем (Ctrl+C).")


@cli.command("proxy-refresh")
@click.option("--file", "-f", "proxy_file", required=True, type=click.Path(exists=True),
              help="Файл со свежими прокси (по одному на строку)")
@click.option("--auto", is_flag=True, help="Без подтверждения (для автоматизации)")
@click.option("--check-only", is_flag=True, help="Только проверить, не заменять")
@click.option("--db-path", default="data/tgwebauth.db", help="Путь к базе данных")
def proxy_refresh(proxy_file: str, auto: bool, check_only: bool, db_path: str):
    """Проверить прокси аккаунтов и заменить мёртвые из файла."""
    from .database import Database
    from .proxy_manager import ProxyManager, proxy_record_to_string

    async def _run() -> None:
        db = Database(Path(db_path))
        await db.initialize()
        await db.connect()
        try:
            manager = ProxyManager(db, accounts_dir=ACCOUNTS_DIR)

            # Step 1: Sync accounts to DB
            click.echo("1. Синхронизация аккаунтов с БД...")
            sync = await manager.sync_accounts_to_db()
            click.echo(f"   Найдено: {sync['synced']}, новых: {sync['created']}, "
                       f"привязано прокси: {sync['proxy_linked']}")

            # Step 2: Import fresh proxies
            click.echo(f"\n2. Импорт свежих прокси из {proxy_file}...")
            imp = await manager.import_from_file(Path(proxy_file))
            click.echo(f"   Импортировано: {imp['imported']}, дубликатов: {imp['duplicates']}, "
                       f"ошибок: {imp['errors']}")

            # Step 3: Health check assigned proxies
            click.echo("\n3. Проверка прокси аккаунтов...")
            check = await manager.check_assigned_proxies()
            alive_count = len(check["alive"])
            dead_count = len(check["dead"])
            no_proxy_count = len(check["no_proxy"])
            total_checked = alive_count + dead_count

            click.echo(f"   Проверено: {total_checked}")
            click.echo(click.style(f"   Живых: {alive_count}", fg="green"))
            if dead_count > 0:
                click.echo(click.style(f"   Мёртвых: {dead_count}", fg="red"))
            else:
                click.echo(f"   Мёртвых: 0")

            # Count free proxies in pool
            free_proxies = await db.list_proxies(status="active", unassigned_only=True)
            click.echo(f"   Свежих в пуле: {len(free_proxies)}")

            if no_proxy_count > 0:
                click.echo(click.style(f"   Аккаунты без прокси: {no_proxy_count} (пропускаются)",
                                       fg="yellow"))

            if dead_count == 0:
                click.echo(click.style("\nВсе прокси живы!", fg="green"))
                return

            # Step 4: Generate replacement plan
            click.echo(f"\n4. План замены:")
            plan = await manager.generate_replacement_plan(check["dead"])

            if not plan:
                click.echo(click.style("   Нет свободных прокси для замены!", fg="red"))
                return

            # Unreserve helper — only unreserves proxies still in "reserved" status
            async def _unreserve_plan() -> None:
                for entry in plan:
                    proxy = await db.get_proxy(entry["new_proxy"].id)
                    if proxy and proxy.status == "reserved":
                        await db.update_proxy(proxy.id, status="active")

            try:
                for entry in plan:
                    old_p = entry["old_proxy"]
                    new_p = entry["new_proxy"]
                    click.echo(f"   {entry['account_name']}: "
                               f"{old_p.host}:{old_p.port} -> {new_p.host}:{new_p.port}")

                not_replaced = dead_count - len(plan)
                if not_replaced > 0:
                    click.echo(click.style(
                        f"\n   Не хватает прокси для {not_replaced} аккаунтов", fg="yellow"))

                if check_only:
                    click.echo("\n--check-only: замена не выполнена")
                    await _unreserve_plan()
                    return

                # Step 5: Confirm
                if not auto:
                    if not click.confirm(f"\nЗаменить {len(plan)} прокси?"):
                        click.echo("Отменено")
                        await _unreserve_plan()
                        return

                # Step 6: Execute
                click.echo(f"\n5. Замена...")
                result = await manager.execute_replacements(plan)
                click.echo(click.style(
                    f"\n   Заменено: {result['replaced']}, ошибок: {result['errors']}",
                    fg="green" if result["errors"] == 0 else "yellow",
                ))
            except BaseException:
                click.echo("\nОтмена, возвращаю зарезервированные прокси в пул...")
                await _unreserve_plan()
                raise

        finally:
            await db.close()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        click.echo("\nПрервано пользователем (Ctrl+C).")


@cli.command()
def preflight():
    """FIX-5.1: Pre-flight validation before large batch migration."""
    import shutil

    from .database import Database

    DB_PATH = Path("data/tgwebauth.db")

    async def _run_preflight():
        click.echo("=" * 50)
        click.echo("PREFLIGHT REPORT")
        click.echo("=" * 50)

        # 1. Disk space
        usage = shutil.disk_usage(Path(".").resolve())
        free_gb = usage.free / (1024 ** 3)
        disk_ok = free_gb > 50  # Need ~100MB/profile, 50GB minimum
        status = click.style("OK", fg="green") if disk_ok else click.style("LOW", fg="red")
        click.echo(f"\nDisk space:        {free_gb:.1f} GB free  {status}")

        # 2. Session files
        account_dirs = find_account_dirs()
        total_accounts = len(account_dirs)
        sessions_ok = 0
        sessions_missing = 0
        no_api = 0
        for d in account_dirs:
            session = d / "session.session"
            api = d / "api.json"
            if session.exists() and api.exists():
                sessions_ok += 1
            elif not session.exists():
                sessions_missing += 1
            elif not api.exists():
                no_api += 1

        click.echo(f"\nAccounts found:    {total_accounts}")
        click.echo(f"  Session + API:   {sessions_ok}")
        if sessions_missing:
            click.echo(click.style(f"  No session:      {sessions_missing}", fg="red"))
        if no_api:
            click.echo(click.style(f"  No api.json:     {no_api}", fg="red"))

        # 3. DB proxies check
        db = Database(DB_PATH)
        await db.initialize()
        await db.connect()
        try:
            all_proxies = await db.list_proxies()
            active_proxies = [p for p in all_proxies if p.status == "active"]
            assigned_proxies = [p for p in all_proxies if p.assigned_account_id]

            click.echo(f"\nProxies:")
            click.echo(f"  Total:           {len(all_proxies)}")
            click.echo(f"  Active:          {len(active_proxies)}")
            click.echo(f"  Assigned:        {len(assigned_proxies)}")

            # 4. Quick proxy TCP check via DB batch
            if active_proxies:
                from .proxy_health import check_proxy_connection
                click.echo(f"\n  TCP check (sample up to 20)...")
                sample = active_proxies[:20]
                tasks = [check_proxy_connection(p.host, p.port, timeout=10) for p in sample]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                alive = sum(1 for r in results if r is True)
                click.echo(f"  Alive:           {alive}/{len(sample)}")
                if alive < len(sample):
                    click.echo(click.style(
                        f"  Dead:            {len(sample) - alive}", fg="red"))

            # 5. Session alive check (sample up to 5)
            click.echo(f"\nSession alive check (sample 5)...")
            alive_count = 0
            dead_count = 0
            twofa_count = 0
            sample_accounts = account_dirs[:5]
            for acct_dir in sample_accounts:
                try:
                    from .telegram_auth import AccountConfig, parse_telethon_proxy
                    account = AccountConfig.load(acct_dir)
                    from telethon import TelegramClient
                    proxy = parse_telethon_proxy(account.proxy)
                    client = TelegramClient(
                        str(account.session_path.with_suffix('')),
                        account.api_id,
                        account.api_hash,
                        proxy=proxy,
                    )
                    try:
                        await asyncio.wait_for(client.connect(), timeout=15)
                        if await client.is_user_authorized():
                            alive_count += 1
                            # Check 2FA
                            try:
                                from telethon.tl.functions.account import GetPasswordRequest
                                pwd = await client(GetPasswordRequest())
                                if pwd.has_password:
                                    twofa_count += 1
                            except Exception:
                                pass
                        else:
                            dead_count += 1
                    finally:
                        await client.disconnect()
                except Exception as e:
                    dead_count += 1
                    logger.debug(f"Preflight session check failed for {acct_dir.name}: {e}")

            click.echo(f"  Alive:           {alive_count}/{len(sample_accounts)}")
            if dead_count:
                click.echo(click.style(f"  Dead:            {dead_count}", fg="red"))
            if twofa_count:
                click.echo(click.style(f"  2FA:             {twofa_count}", fg="yellow"))

            # 6. Resources
            import psutil
            ram_gb = psutil.virtual_memory().total / (1024 ** 3)
            ram_avail_gb = psutil.virtual_memory().available / (1024 ** 3)
            click.echo(f"\nResources:")
            click.echo(f"  RAM total:       {ram_gb:.1f} GB")
            click.echo(f"  RAM available:   {ram_avail_gb:.1f} GB")

            # Estimate
            recommended_parallel = min(10, max(1, int(ram_avail_gb / 0.5)))
            est_time_hours = (total_accounts * 60) / (recommended_parallel * 3600)
            click.echo(f"\nEstimate:")
            click.echo(f"  Ready accounts:  {sessions_ok}")
            click.echo(f"  Recommended -j:  {recommended_parallel}")
            click.echo(f"  Est. time:       ~{est_time_hours:.1f} hours")

        finally:
            await db.close()

    try:
        asyncio.run(_run_preflight())
    except KeyboardInterrupt:
        click.echo("\nПрервано пользователем (Ctrl+C).")


@cli.command()
def init():
    """Инициализировать структуру директорий"""
    ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)

    click.echo("Созданы директории:")
    click.echo(f"  {ACCOUNTS_DIR}/ - для session файлов")
    click.echo(f"  {PROFILES_DIR}/ - для browser profiles")

    click.echo("\nСтруктура аккаунта:")
    click.echo("  accounts/")
    click.echo("    └── account_name/")
    click.echo("        ├── session.session")
    click.echo("        ├── api.json")
    click.echo("        └── ___config.json (опционально)")

    click.echo("\n2FA пароль можно передать через:")
    click.echo("  1. --password 'your_password'")
    click.echo("  2. export TG_2FA_PASSWORD='your_password'")
    click.echo("  3. Ввод вручную в браузере")


@cli.command()
def dedup():
    """Удалить дубликаты аккаунтов из БД (оставляет самую богатую запись: с прокси, статусом, fragment)"""
    async def _dedup():
        from .database import Database
        db = Database(DATA_DIR / "tgwebauth.db")
        await db.initialize()
        await db.connect()
        try:
            removed = await db.remove_duplicate_accounts()
            if removed:
                click.echo(f"Удалено {removed} дубликатов")
            else:
                click.echo("Дубликатов не найдено")
        finally:
            await db.close()

    try:
        asyncio.run(_dedup())
    except KeyboardInterrupt:
        click.echo("\nПрервано пользователем (Ctrl+C).")


if __name__ == "__main__":
    cli()
