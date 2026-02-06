# TG Web Auth

Автоматическая миграция Telegram session файлов (Telethon) в браузерные профили для **web.telegram.org**.

## Что это делает

Программа берёт существующую Telethon-сессию и авторизует браузер через QR-код:

```
Telethon сессия  →  QR Login  →  Browser профиль (Camoufox)
```

После миграции можно открывать web.telegram.org уже авторизованным, с сохранённым профилем.

## Установка

### 1. Python зависимости

```bash
pip install -r requirements.txt
```

### 2. Camoufox браузер

```bash
camoufox fetch
```

### 3. Node.js (опционально, для jsQR)

```bash
npm install
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

### api.json

```json
{
  "api_id": 12345,
  "api_hash": "your_api_hash"
}
```

### ___config.json (опционально)

```json
{
  "Name": "Account Name",
  "Proxy": "socks5:host:port:user:pass"
}
```

## Команды

### Проверить аккаунты

```bash
python -m src.cli check
```

Показывает какие аккаунты есть и их статус.

### Мигрировать один аккаунт

```bash
python -m src.cli migrate --account "Name"

# С 2FA паролем
python -m src.cli migrate --account "Name" --password "2fa_password"
```

### Мигрировать все аккаунты

```bash
python -m src.cli migrate --all

# С 2FA (одинаковый для всех)
python -m src.cli migrate --all --password "2fa_password"
```

### Параллельная миграция

```bash
# 5 браузеров одновременно
python -m src.cli migrate --all --parallel 5 --cooldown 30

# С мониторингом ресурсов (авто-регулировка)
python -m src.cli migrate --all --parallel --cooldown 30
```

### Открыть профиль

```bash
python -m src.cli open --account "Name"
```

Открывает браузер с сохранённым профилем.

### Список профилей

```bash
python -m src.cli list
```

### Проверка здоровья (после миграции)

```bash
python -m src.cli health --account "Name"
```

Проверяет:
- Telethon сессия работает
- Web профиль открывается
- Нет ограничений на аккаунте

## Troubleshooting

### "camoufox not installed"

```bash
pip install camoufox
camoufox fetch
```

### "pyzbar DLL error" (Windows)

pyzbar опционален. Если не работает - будет использоваться OpenCV для QR.

### "Session not authorized"

Сессия истекла. Нужно заново авторизовать Telethon.

### "2FA password required"

Укажите пароль:
```bash
python -m src.cli migrate --account "Name" --password "your_2fa"
```

### "Failed to decode QR"

1. Проверьте что браузер видит QR-код
2. Скриншоты сохраняются в `profiles/debug_*.png`
3. Попробуйте перезапустить

### "Proxy connection failed"

1. Проверьте формат: `socks5:host:port:user:pass`
2. Проверьте что прокси работает
3. Для SOCKS5 с auth автоматически создаётся HTTP relay

### Медленная миграция

```bash
# Увеличьте параллельность
python -m src.cli migrate --all --parallel 10 --cooldown 20
```

## Структура проекта

```
tg-web-auth/
├── accounts/           # Session файлы (не в git)
├── profiles/           # Browser профили (не в git)
├── src/
│   ├── telegram_auth.py    # Основная логика
│   ├── browser_manager.py  # Camoufox управление
│   ├── proxy_relay.py      # SOCKS5 → HTTP relay
│   ├── resource_monitor.py # Мониторинг ресурсов
│   ├── cli.py              # CLI интерфейс
│   └── logger.py           # Логирование
├── tests/              # 95 тестов
├── requirements.txt
└── CLAUDE.md           # Инструкции разработки
```

## Как это работает

1. **Telethon подключается** к существующей сессии
2. **Camoufox открывает** web.telegram.org
3. **QR-код извлекается** со страницы (screenshot → decode)
4. **Telethon принимает** токен через `AcceptLoginTokenRequest`
5. **Браузер авторизуется** автоматически
6. **2FA вводится** если требуется
7. **Профиль сохраняется** для повторного использования

## Тесты

```bash
pytest              # Все тесты
pytest -v           # Verbose
pytest --tb=short   # Короткий traceback
```

## Безопасность

- НЕ логируются: auth_key, api_hash, passwords, tokens
- Каждый аккаунт изолирован в своём профиле
- Прокси обязателен для каждого аккаунта
- Graceful shutdown при Ctrl+C

## License

MIT
