# TG Web Auth Migration Tool

## Project Goal
Автоматическая миграция Telegram session файлов (Telethon/Pyrogram) в браузерные профили для:
- **web.telegram.org** - основной веб-клиент
- **fragment.com** - NFT usernames, Telegram Stars (планируется)

Масштаб: **1000 аккаунтов**, переносимость между ПК.

## Current Status

### Что работает
- Programmatic QR Login (без ручного сканирования)
- Multi-decoder QR (jsQR, OpenCV, pyzbar)
- Camoufox antidetect browser с persistent profiles
- SOCKS5 proxy с auth через pproxy relay
- Batch миграция с cooldown 45 сек
- 81 тест проходит

### Что НЕ работает
- **Direct Session Injection** - Telegram Web валидирует сессии на сервере, просто записать auth_key в localStorage/IndexedDB недостаточно

## Architecture

### Programmatic QR Login Flow
```
┌─────────────┐     ┌──────────────┐     ┌─────────────────┐
│   Telethon  │     │   Camoufox   │     │  Telegram Web   │
│   Client    │     │   Browser    │     │    Server       │
└──────┬──────┘     └──────┬───────┘     └────────┬────────┘
       │                   │                      │
       │  1. Connect with existing session        │
       │─────────────────────────────────────────>│
       │                   │                      │
       │                   │  2. Open web.telegram.org
       │                   │─────────────────────>│
       │                   │                      │
       │                   │  3. Show QR code     │
       │                   │<─────────────────────│
       │                   │                      │
       │  4. Screenshot QR │                      │
       │<──────────────────│                      │
       │                   │                      │
       │  5. Decode token from QR                 │
       │  6. AcceptLoginTokenRequest(token)       │
       │─────────────────────────────────────────>│
       │                   │                      │
       │                   │  7. Session authorized
       │                   │<─────────────────────│
       │                   │                      │
       │  8. Save browser profile                 │
       │<──────────────────│                      │
```

### Proxy Relay (для SOCKS5 auth)
```
Browser ──HTTP──> pproxy (localhost:random) ──SOCKS5+auth──> Remote Proxy
```
Браузеры не поддерживают SOCKS5 с auth напрямую, поэтому pproxy создаёт локальный HTTP relay.

## Technical Context

### Session File Format (Telethon v7)
```
SQLite database:
- sessions: dc_id, server_address, port, auth_key (256 bytes BLOB)
- entities: id, hash, username, phone, name
- version: 7
```

### API Config Format (api.json)
```json
{
  "api_id": 2040,
  "api_hash": "...",
  "device_model": "Desktop",
  "system_version": "Windows 10"
}
```

### Account Config Format (___config.json)
```json
{
  "Name": "Account Name",
  "Proxy": "socks5:host:port:user:pass"
}
```

## File Structure
```
tg-web-auth/
├── accounts/               # Исходные session файлы (в .gitignore)
│   └── account_name/
│       ├── session.session
│       ├── api.json
│       └── ___config.json
├── profiles/               # Browser profiles (в .gitignore)
│   └── account_name/
│       ├── config.json
│       └── browser_data/
├── src/
│   ├── telegram_auth.py    # Основная логика QR auth (1266 строк)
│   ├── browser_manager.py  # Camoufox управление
│   ├── proxy_relay.py      # SOCKS5 → HTTP relay
│   ├── pproxy_wrapper.py   # pproxy process wrapper
│   ├── cli.py              # CLI интерфейс
│   ├── utils.py            # Утилиты
│   └── security_check.py   # Проверка безопасности
├── tests/                  # 81 тест
│   ├── test_telegram_auth.py
│   ├── test_browser_manager.py
│   ├── test_proxy_relay.py
│   ├── test_utils.py
│   ├── test_integration.py
│   └── conftest.py
├── scripts/                # Эксперименты (не в git)
│   ├── extract_tg_storage.py
│   ├── session_converter.py
│   └── inject_session.py
├── docs/
│   └── SESSION_NOTES_*.md  # Заметки сессий разработки
├── decode_qr.js            # Node.js jsQR decoder
├── package.json
├── requirements.txt
├── CLAUDE.md
└── .gitignore
```

## Technical Stack
- **Python 3.11+**
- **Telethon** - MTProto client для AcceptLoginToken
- **Camoufox** - Antidetect browser (Firefox-based)
- **Playwright** - Browser automation fallback
- **pproxy** - SOCKS5 auth relay
- **OpenCV/pyzbar/jsQR** - QR decoding
- **Click** - CLI

## Commands

### Установка
```bash
pip install -r requirements.txt
playwright install chromium  # fallback browser
npm install                  # для jsQR decoder
```

### Миграция
```bash
# Один аккаунт
python -m src.cli migrate --account "Name"

# Все аккаунты
python -m src.cli migrate --all

# С паролем 2FA
python -m src.cli migrate --account "Name" --password "2fa_pass"
```

### Управление профилями
```bash
python -m src.cli open --account "Name"  # Открыть профиль
python -m src.cli list                   # Список профилей
```

### Тесты
```bash
pytest              # Все тесты
pytest -v           # Verbose
pytest tests/test_integration.py  # Только интеграционные
```

## Scaling для 1000 аккаунтов

### Ресурсы
```
┌─────────────────────────┬───────────┬───────────┬────────────┐
│         Метрика         │ 100 акков │ 500 акков │ 1000 акков │
├─────────────────────────┼───────────┼───────────┼────────────┤
│ RAM (batch 10 parallel) │ ~2 GB     │ ~2 GB     │ ~2 GB      │
├─────────────────────────┼───────────┼───────────┼────────────┤
│ Disk (profiles)         │ ~10 GB    │ ~50 GB    │ ~100 GB    │
├─────────────────────────┼───────────┼───────────┼────────────┤
│ Time (sequential, 45s)  │ ~1.5 ч    │ ~6 ч      │ ~12 ч      │
├─────────────────────────┼───────────┼───────────┼────────────┤
│ Time (10 parallel)      │ ~10 мин   │ ~40 мин   │ ~1.5 ч     │
└─────────────────────────┴───────────┴───────────┴────────────┘
```

### Переносимость между ПК
- **Session файлы** (~50MB для 1000) - синхронизируем между ПК
- **Browser profiles** (~100GB) - создаём локально по требованию
- Миграция на новом ПК: `python -m src.cli migrate --all`

## Roadmap

### Phase 1: Core (DONE)
- [x] Programmatic QR Login
- [x] Multi-decoder QR
- [x] Camoufox antidetect
- [x] SOCKS5 proxy relay
- [x] Batch миграция
- [x] 81 тест

### Phase 2: Parallel Migration (TODO)
- [ ] 10-50 параллельных браузеров
- [ ] Semaphore для контроля concurrency
- [ ] Progress reporting
- [ ] Error recovery & retry

### Phase 3: Fragment.com (TODO)
- [ ] Исследовать auth flow Fragment
- [ ] Интеграция с существующими профилями
- [ ] TON wallet НЕ нужен (только Telegram auth)

### Phase 4: Health & Monitoring (TODO)
- [ ] Session health check
- [ ] Auto-refresh истекающих сессий
- [ ] Dashboard со статусами

## Security Constraints

### ОБЯЗАТЕЛЬНО
- НЕ логировать auth_key, api_hash, passwords, tokens
- Изолированные browser profiles для каждого аккаунта
- Прокси обязателен для каждого аккаунта
- Graceful shutdown - корректно закрывать все ресурсы

### ЗАПРЕЩЕНО
- Hardcoded credentials
- `print()` вместо `logging`
- Bare `except:` (только `except Exception as e:`)
- Игнорирование возвращаемых ошибок

## GUI Testing Rules (ОБЯЗАТЕЛЬНО)

### После каждой GUI фичи:
1. [ ] Запустить приложение: `python -m src.gui.app`
2. [ ] Протестировать КАЖДУЮ кнопку вручную
3. [ ] Проверить что нет крашей
4. [ ] Все ошибки логируются, не молча падают

### Запрещено:
- Говорить "готово" без ручного тестирования
- Кнопки без try/except
- Краши без понятного сообщения об ошибке

## Quality Gates

### Перед завершением любой задачи
1. [ ] `pytest` проходит без ошибок
2. [ ] Self-review на типичные ошибки
3. [ ] Нет секретов в логах
4. [ ] Все ресурсы закрываются (async with, try/finally)
5. [ ] Type hints на всех публичных функциях
6. [ ] Docstrings с Args/Returns

### Code Standards
- Type hints required for all function parameters and return values
- Use `Optional[T]` for nullable types
- Prefer `Path` over string paths
- Use dataclasses for structured data
- Async context managers for resource management

## Known Issues

### Direct Session Injection (НЕ РАБОТАЕТ)
Попытки напрямую инжектить auth_key в браузер не работают:
- Telegram Web K валидирует сессии на сервере
- authState в IndexedDB сбрасывается приложением
- **Единственный рабочий путь - Programmatic QR Login**

Экспериментальный код в `scripts/` - только для исследования.
