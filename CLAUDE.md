# TG Web Auth Migration Tool

## Project Goal
Автоматическая миграция Telegram session файлов (Telethon/Pyrogram) в браузерные профили web.telegram.org с сохранением сессий между запусками.

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

### Telegram Web Architecture
- web.telegram.org использует GramJS (MTProto over WebSocket)
- Auth data хранится в IndexedDB, не localStorage
- Два способа входа: QR-код или phone+code
- QR login API: auth.exportLoginToken → auth.acceptLoginToken

### Programmatic QR Login Flow
1. Telethon client с существующей сессией вызывает `auth.exportLoginToken`
2. Получаем token, кодируем в base64url
3. Playwright открывает web.telegram.org, показывает QR
4. Telethon client вызывает `auth.acceptLoginToken` для подтверждения
5. Браузер получает авторизацию
6. Сохраняем browser profile (storageState + userDataDir)

## Requirements

### Functional
- Полностью автоматический вход без ручного QR сканирования
- Поддержка прокси (SOCKS5 из config)
- Сохранение профилей между запусками (через неделю открыл - работает)
- Batch операции для множества аккаунтов
- CLI интерфейс: migrate, open, list

### Security
- НЕ хранить auth_key в plaintext логах
- Изолированные browser profiles для каждого аккаунта
- Прокси обязателен для каждого аккаунта

### Technical Stack
- Python 3.11+
- Telethon (MTProto client)
- Playwright (browser automation)
- Click или Typer (CLI)

## File Structure
```
tg-web-auth/
├── accounts/           # Исходные session файлы
│   └── account_name/
│       ├── session.session
│       ├── api.json
│       └── ___config.json
├── profiles/           # Сохранённые browser profiles
│   └── account_name/
│       ├── storage_state.json
│       └── user_data/
├── src/
│   ├── session_reader.py
│   ├── qr_auth.py
│   ├── browser_manager.py
│   └── cli.py
├── requirements.txt
└── README.md
```

## Commands
- `pip install -r requirements.txt` - установка
- `playwright install chromium` - установка браузера
- `python -m src.cli migrate --account "Name"` - миграция одного
- `python -m src.cli migrate --all` - миграция всех
- `python -m src.cli open --account "Name"` - открыть профиль
- `python -m src.cli list` - список профилей

## Constraints
- НИКОГДА не логировать auth_key или api_hash в консоль
- Всегда использовать прокси из конфига
- Graceful shutdown - корректно закрывать Telethon client
- Playwright userDataDir для персистентности, не только storageState

## Testing
- Сначала тестируем на тестовом аккаунте (session.session в uploads)
- Только после успешного теста - batch операции
- Run `pytest` before any real account testing

## Quality Gates (ОБЯЗАТЕЛЬНО)

### Перед завершением любой задачи:
1. [ ] Self-review на типичные ошибки
2. [ ] Unit тесты написаны и проходят
3. [ ] Нет секретов в логах (grep -r "api_hash\|auth_key\|password")
4. [ ] Все ресурсы закрываются (async with, try/finally)
5. [ ] Type hints на всех функциях
6. [ ] pytest проходит без ошибок

### Запрещено:
- Hardcoded credentials
- print() вместо logging
- Bare except: (только except Exception as e:)
- Игнорирование возвращаемых ошибок

## Code Quality Rules

### Before marking task complete:
- [ ] Run `pytest` - all tests must pass
- [ ] Self-review checklist completed (see below)
- [ ] Integration test passes
- [ ] No new bare `except:` blocks

### Self-review checklist:
- [ ] **Error handling**: No bare `except:`, use specific exceptions
- [ ] **Resource cleanup**: All browsers/clients closed in finally blocks
- [ ] **No secrets in logs**: Never log api_hash, auth_key, passwords, tokens
- [ ] **Type hints**: All public functions have type hints
- [ ] **Docstrings**: All public functions have docstrings with Args/Returns

### Code standards:
- Type hints required for all function parameters and return values
- Use `Optional[T]` for nullable types
- Prefer `Path` over string paths
- Use dataclasses for structured data
- Async context managers for resource management

### Test requirements:
- Unit tests for all utility functions
- Mock tests for external dependencies (Camoufox, Telethon)
- Integration test must validate real accounts structure
- Run `pytest -v` to see detailed output

### Commands:
```bash
# Run all tests
pytest

# Run with verbose output
pytest -v

# Run specific test file
pytest tests/test_utils.py

# Run integration tests only
pytest tests/test_integration.py
```
