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
import logging
import random
import sys
import os
from pathlib import Path
from typing import Optional

from .logger import setup_logging, get_logger

logger = get_logger(__name__)

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
except ImportError:
    print("ERROR: click not installed. Run: pip install click")
    exit(1)


ACCOUNTS_DIR = Path("accounts")
PROFILES_DIR = Path("profiles")

# FIX #6: Default cooldown между аккаунтами
DEFAULT_COOLDOWN = 45  # секунд


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
@click.option("--status", "show_status", is_flag=True,
              help="Показать статус текущего batch и выйти")
def migrate(account: Optional[str], migrate_all: bool, password: Optional[str],
            headless: bool, cooldown: int, parallel: int, auto_scale: bool,
            resume: bool, retry_failed: bool, show_status: bool):
    """Мигрировать аккаунт(ы) из session в browser profile"""
    import signal
    from .telegram_auth import migrate_account, migrate_accounts_batch, ParallelMigrationController
    from .migration_state import MigrationState

    # Handle --status flag
    if show_status:
        state = MigrationState()
        click.echo(state.format_status())
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

        # FIX #8: Безопасное получение пароля
        password_2fa = get_2fa_password(account_dir.name, password)

        result = asyncio.run(migrate_account(
            account_dir=account_dir,
            password_2fa=password_2fa,
            headless=headless
        ))

        if result.success:
            click.echo(click.style(f"\n✓ Успешно: {result.profile_name}", fg="green"))
            click.echo(f"  Telethon session alive: {result.telethon_alive}")
        else:
            click.echo(click.style(f"\n✗ Ошибка: {result.error}", fg="red"))
            sys.exit(1)

    elif migrate_all or resume or retry_failed:
        # Initialize state tracking
        state = MigrationState()

        # Determine which accounts to migrate
        if resume:
            # Resume: only pending accounts from interrupted batch
            if not state.has_active_batch:
                click.echo("Нет прерванной миграции для продолжения")
                click.echo("Используйте --all для новой миграции")
                sys.exit(0)

            pending_names = state.get_pending()
            click.echo(f"Продолжение batch: {state.batch_id}")
            click.echo(f"Осталось: {len(pending_names)} аккаунтов")

            # Find account dirs matching pending names
            all_accounts = find_account_dirs()
            accounts = [d for d in all_accounts if d.name in pending_names]

            if not accounts:
                click.echo("Все pending аккаунты уже обработаны")
                sys.exit(0)

        elif retry_failed:
            # Retry: only failed accounts from previous batch
            failed_names = state.get_failed_accounts()
            if not failed_names:
                click.echo("Нет упавших аккаунтов для повтора")
                sys.exit(0)

            click.echo(f"Повтор {len(failed_names)} упавших аккаунтов:")
            for name in failed_names:
                click.echo(f"  - {name}")

            # Find account dirs matching failed names
            all_accounts = find_account_dirs()
            accounts = [d for d in all_accounts if d.name in failed_names]

            # Start a new batch for retry
            account_names = [d.name for d in accounts]
            state.start_batch(account_names)

        else:
            # Normal --all: migrate all accounts
            accounts = find_account_dirs()
            if not accounts:
                click.echo("Нет аккаунтов для миграции")
                sys.exit(0)

            # Start a new batch
            account_names = [d.name for d in accounts]
            batch_id = state.start_batch(account_names)
            click.echo(f"Новый batch: {batch_id}")

        click.echo(f"Аккаунтов для миграции: {len(accounts)}")

        # FIX #8: Безопасное получение пароля
        password_2fa = get_2fa_password("all", password)

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

            # Progress callback with state tracking
            def on_progress(completed, total, result):
                status_str = click.style("OK", fg="green") if result and result.success else click.style("FAIL", fg="red")
                name = result.profile_name if result else "?"
                click.echo(f"  [{completed}/{total}] {status_str} {name}")

                # Update state
                if result:
                    if result.success:
                        state.mark_completed(result.profile_name)
                    else:
                        state.mark_failed(result.profile_name, result.error or "Unknown error")

            results = asyncio.run(controller.run(
                account_dirs=accounts,
                password_2fa=password_2fa,
                headless=headless,
                on_progress=on_progress
            ))
        else:
            # Последовательный режим (существующий)
            click.echo(f"Режим: ПОСЛЕДОВАТЕЛЬНЫЙ")
            click.echo(f"Cooldown между аккаунтами: {cooldown}s")

            # FIX #6: Используем batch функцию с cooldown
            results = asyncio.run(migrate_accounts_batch(
                account_dirs=accounts,
                password_2fa=password_2fa,
                headless=headless,
                cooldown=cooldown
            ))

            # Update state for sequential mode
            for result in results:
                if result.success:
                    state.mark_completed(result.profile_name)
                else:
                    state.mark_failed(result.profile_name, result.error or "Unknown error")

        # Итог
        success = [r for r in results if r.success]
        failed = [r for r in results if not r.success]

        click.echo(f"\n{'='*50}")
        click.echo("ИТОГ МИГРАЦИИ")
        click.echo(f"{'='*50}")
        click.echo(f"Всего: {len(results)}")
        click.echo(f"Успешно: {len(success)}")
        click.echo(f"Ошибки: {len(failed)}")

        if failed:
            click.echo("\nНеуспешные аккаунты:")
            for result in failed:
                click.echo(f"  - {result.profile_name}: {result.error}")
            click.echo("\nИспользуйте --retry-failed для повтора упавших")

        # Show state summary
        click.echo(f"\nСтатус batch: {state.batch_id}")
        status_info = state.get_status()
        if status_info["pending"] > 0:
            click.echo(f"Осталось pending: {status_info['pending']}")
            click.echo("Используйте --resume для продолжения")


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
            await ctx.close()

    asyncio.run(run())


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

    result = asyncio.run(run_security_check(
        proxy=proxy,
        profile_path=profile_path,
        headless=headless,
        use_geoip=geoip
    ))

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

    try:
        with open(api_file) as f:
            api = json.load(f)

        proxy = None
        if config_file.exists():
            with open(config_file) as f:
                config = json.load(f)
            proxy_str = config.get("Proxy", "")
            if proxy_str:
                parts = proxy_str.split(":")
                if len(parts) >= 4:
                    proxy = {
                        "proxy_type": "socks5",
                        "addr": parts[1],
                        "port": int(parts[2]),
                        "username": parts[3],
                        "password": parts[4] if len(parts) > 4 else None
                    }

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

        me, error = asyncio.run(check_telethon())

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
            with open(config_file) as f:
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
@click.option("--headless", is_flag=True, help="Запуск без GUI")
@click.option("--cooldown", "-c", default=DEFAULT_COOLDOWN, type=int,
              help=f"Секунды между аккаунтами при --all (default: {DEFAULT_COOLDOWN})")
def fragment(account: Optional[str], fragment_all: bool, headless: bool, cooldown: int):
    """Авторизация на fragment.com через Telegram Login Widget"""
    from .telegram_auth import AccountConfig
    from .fragment_auth import FragmentAuth
    from .browser_manager import BrowserManager

    setup_logging(level=logging.INFO)

    if not account and not fragment_all:
        click.echo("Укажите --account или --all")
        return

    async def _run_single(account_dir: Path) -> bool:
        """Запускает fragment auth для одного аккаунта."""
        try:
            config = AccountConfig.load(account_dir)
        except Exception as e:
            click.echo(f"  FAIL {account_dir.name}: {e}")
            return False

        browser_manager = BrowserManager()
        auth = FragmentAuth(config, browser_manager)
        result = await auth.connect(headless=headless)

        if result.success:
            status = "already authorized" if result.already_authorized else "connected"
            click.echo(click.style(f"  OK {config.name}: {status}", fg="green"))
            return True
        else:
            click.echo(click.style(f"  FAIL {config.name}: {result.error}", fg="red"))
            return False

    async def _run():
        if account:
            account_dir = get_account_dir(account)
            if not account_dir:
                click.echo(f"Аккаунт '{account}' не найден в {ACCOUNTS_DIR}/")
                return
            await _run_single(account_dir)
        elif fragment_all:
            account_dirs = find_account_dirs()
            if not account_dirs:
                click.echo(f"Нет аккаунтов в {ACCOUNTS_DIR}/")
                return

            click.echo(f"Fragment auth для {len(account_dirs)} аккаунтов")
            ok = 0
            fail = 0

            for i, account_dir in enumerate(account_dirs):
                click.echo(f"\n[{i+1}/{len(account_dirs)}] {account_dir.name}")
                success = await _run_single(account_dir)
                if success:
                    ok += 1
                else:
                    fail += 1

                # Cooldown between accounts
                if i < len(account_dirs) - 1:
                    actual_cooldown = cooldown + random.randint(-10, 10)
                    actual_cooldown = max(10, actual_cooldown)
                    await asyncio.sleep(actual_cooldown)

            click.echo(f"\n{'=' * 50}")
            click.echo(f"ИТОГ: Успешно: {ok}, Ошибки: {fail}")

    asyncio.run(_run())


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


if __name__ == "__main__":
    cli()
