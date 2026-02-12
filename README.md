# TG Web Auth

Автоматическая миграция Telegram session файлов (Telethon) в браузерные профили для **web.telegram.org** и **fragment.com**.

## Что это делает

Программа берёт существующую Telethon-сессию и авторизует браузер через QR-код:

```
Telethon сессия  →  QR Login  →  Browser профиль (Camoufox)
```

После миграции можно открывать web.telegram.org уже авторизованным, с сохранённым профилем.
Fragment.com авторизуется через отдельный OAuth popup flow.

## Установка

```bash
# 1. Создаём виртуальное окружение
python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # Linux/macOS

# 2. Устанавливаем зависимости
pip install -r requirements.txt

# 3. Скачиваем Camoufox browser
python -m camoufox fetch

# 4. Инициализация структуры директорий
python -m src.cli init
```

## Структура аккаунтов

Положите аккаунты в папку `accounts/`:

```
accounts/
├── account_name/
│   ├── session.session     # Telethon session файл (обязательно)
│   ├── api.json            # API credentials (обязательно)
│   └── ___config.json      # Прокси и настройки (опционально)
```

## Команды

### GUI (основной режим)

```bash
python -m src.gui.app
```

### CLI

```bash
# Миграция
python -m src.cli migrate --account "Name"             # Один аккаунт
python -m src.cli migrate --all                        # Все аккаунты
python -m src.cli migrate --all --parallel 5           # Параллельно
python -m src.cli migrate --account "Name" -p "2fa"    # С 2FA паролем

# Fragment
python -m src.cli fragment --account "Name"            # Один аккаунт
python -m src.cli fragment --all                       # Все аккаунты

# Прокси
python -m src.cli check-proxies                        # Проверить все
python -m src.cli proxy-refresh -f proxies.txt         # Заменить мёртвые

# Утилиты
python -m src.cli open --account "Name"                # Открыть профиль
python -m src.cli list                                 # Список аккаунтов
python -m src.cli health --account "Name"              # Проверка здоровья
python -m src.cli check -p "socks5:h:p:u:p"           # Fingerprint check
```

## Как это работает

1. **Telethon подключается** к существующей сессии
2. **Camoufox открывает** web.telegram.org
3. **QR-код извлекается** со страницы (screenshot + zxing-cpp decode)
4. **Telethon принимает** токен через `AcceptLoginTokenRequest`
5. **Браузер авторизуется** автоматически
6. **2FA вводится** если требуется
7. **Auth TTL** устанавливается на 365 дней
8. **Профиль сохраняется** для повторного использования

## Тесты

```bash
pytest              # Все 326 тестов
pytest -v           # Verbose
pytest --tb=short   # Короткий traceback
```

## Безопасность

- НЕ логируются: auth_key, api_hash, passwords, tokens, phone numbers
- Каждый аккаунт изолирован в своём профиле
- 1 выделенный прокси на аккаунт (никогда не шарить)
- Graceful shutdown при Ctrl+C
- PID-based zombie browser cleanup (psutil)

## Troubleshooting

### Camoufox не запускается

```bash
python -m camoufox remove
python -m camoufox fetch
```

### "Session not authorized"

Сессия истекла. Нужно заново авторизовать Telethon.

### "2FA password required"

```bash
python -m src.cli migrate --account "Name" --password "your_2fa"
```

### "Proxy connection failed"

1. Формат: `socks5:host:port:user:pass`
2. Для SOCKS5 с auth автоматически создаётся HTTP relay через pproxy

## License

MIT
