# Установка TG Web Auth

## Требования

- Python 3.10+
- Windows/Linux/macOS

## Установка

```bash
# 1. Создаём виртуальное окружение
python -m venv venv

# Windows
venv\Scripts\activate

# Linux/macOS
source venv/bin/activate

# 2. Устанавливаем зависимости
pip install -r requirements.txt

# 3. Скачиваем Camoufox Firefox binary
camoufox fetch

# 4. Для pyzbar на Windows нужен Visual C++ Redistributable
# Скачать: https://aka.ms/vs/17/release/vc_redist.x64.exe
```

## Проверка установки

```bash
# Проверяем что Camoufox работает
python -c "from camoufox.sync_api import Camoufox; print('Camoufox OK')"

# Проверяем CLI
python -m src.cli --help
```

## CLI Команды

```bash
# Инициализация структуры директорий
python -m src.cli init

# Список аккаунтов и профилей
python -m src.cli list

# Проверка безопасности с прокси
python -m src.cli check --proxy "socks5:host:port:user:pass" --profile "test"

# Миграция одного аккаунта
python -m src.cli migrate --account "Софт 310"

# Миграция с 2FA паролем
python -m src.cli migrate --account "Софт 310" --password "your2fapass"

# Миграция всех аккаунтов
python -m src.cli migrate --all

# Открыть существующий профиль
python -m src.cli open --account "12709623175 (Софт 310)"
```

## Порядок тестирования

### Шаг 1: Security Check

```bash
# Проверяем безопасность профиля с прокси
python -m src.cli check \
    --proxy "socks5:host:port:user:pass" \
    --profile "test_account"

# Если всё OK (Status: SAFE) - переходим к шагу 2
# Если есть проблемы - НЕ продолжаем!
```

### Шаг 2: Telegram Web Login

```bash
# После успешного security check
python -m src.cli migrate --account "Софт 310"

# Или напрямую:
python -m src.telegram_auth \
    --account "accounts/test/12709623175 (Софт 310)"
```

### Шаг 3: Ждём 3-5 дней

- Проверяем что аккаунт живой
- Мониторим сессии в Telegram

### Шаг 4: Fragment OAuth (опционально)

```bash
python -m src.fragment_auth \
    --account "accounts/test/12709623175 (Софт 310)"
```

## Структура проекта

```
tg-web-auth/
├── src/
│   ├── __init__.py
│   ├── cli.py              # Командный интерфейс
│   ├── security_check.py   # Проверка безопасности
│   ├── browser_manager.py  # Camoufox wrapper
│   ├── telegram_auth.py    # Логин в Telegram Web
│   └── fragment_auth.py    # OAuth для Fragment (TODO)
├── profiles/               # Браузерные профили
│   └── account_name/
│       ├── browser_data/   # Camoufox user_data_dir
│       ├── storage_state.json
│       ├── profile_config.json
│       └── security_check.json
├── accounts/               # Telethon сессии
│   └── test/
│       └── account_folder/
│           ├── session.session
│           ├── api.json
│           └── ___config.json
└── requirements.txt
```

## Troubleshooting

### pyzbar не находит zbar.dll (Windows)

```bash
# Установите Visual C++ Redistributable
# https://aka.ms/vs/17/release/vc_redist.x64.exe
```

### Camoufox не запускается

```bash
# Переустановите Camoufox binary
camoufox remove
camoufox fetch
```

### QR код не декодируется

- Убедитесь что страница полностью загрузилась
- Проверьте скриншот в profiles/{name}/debug_qr.png
- Попробуйте без headless режима
