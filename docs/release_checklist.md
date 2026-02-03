# TG Web Auth - Release Checklist

> **Дата:** 2026-02-03
> **Версия:** 1.0.0-beta

## Статус тестирования

### ✅ Unit Tests
- [x] 112 тестов проходят
- [x] test_database.py - CRUD операции
- [x] test_telegram_auth.py - FloodWait handling, randomized cooldown
- [x] test_browser_manager.py - профили, прокси
- [x] test_proxy_relay.py - SOCKS5 relay

### ✅ Функциональные тесты (программные)

| Функция | Статус | Заметки |
|---------|--------|---------|
| Import Sessions | ✅ | Копирует в data/sessions/, создаёт записи в БД |
| Import Proxies | ✅ | Парсит host:port:user:pass формат |
| Auto-Assign Proxies | ✅ | Назначает свободные прокси аккаунтам |
| AccountConfig.load() | ✅ | Загружает session, api.json, ___config.json |
| Database CRUD | ✅ | accounts, proxies, migrations |
| SQL Injection Protection | ✅ | Whitelist полей |

### ⏳ GUI тестирование (требуется ручная проверка)

| Кнопка | Статус | Заметки |
|--------|--------|---------|
| Import Sessions | ⏳ | Tkinter dialog, async import |
| Import Proxies | ⏳ | Tkinter file dialog |
| Auto-Assign Proxies | ⏳ | Non-blocking |
| Migrate Single | ⏳ | Прямой вызов migrate_account() |
| Migrate All | ⏳ | С randomized cooldown |
| Open Profile | ⏳ | Требует существующий профиль |
| Assign Proxy Dialog | ⏳ | Modal window |
| Search | ⏳ | Real-time filtering |

### ⏳ Реальная миграция

- [ ] Подключение Telethon сессии
- [ ] QR code scanning работает
- [ ] 2FA handling работает
- [ ] Browser profile создаётся
- [ ] Telethon сессия остаётся живой после миграции
- [ ] Повторное открытие профиля сохраняет авторизацию

## Security Fixes (реализовано)

- [x] FloodWaitError handling с retry/backoff
- [x] Randomized cooldown (log-normal distribution, 30-135s)
- [x] Human-like 2FA typing (100-200ms между символами)
- [x] Migration state persistence в SQLite
- [x] SQL injection protection (whitelists)

## Архитектурные исправления

- [x] GUI вызывает migrate_account() напрямую (не через CLI)
- [x] GUI использует BrowserManager напрямую для Open
- [x] Sessions хранятся в data/sessions/
- [x] Browser profiles в profiles/
- [x] Database в data/tgwebauth.db

## Известные ограничения

1. **Fragment.com** - не реализовано (Phase 2)
2. **Parallel migration в GUI** - миграция идёт последовательно
3. **PyInstaller build** - не протестирован

## Команды для тестирования

```bash
# Unit tests
pytest -v

# Запуск GUI
python -m src.gui.app

# CLI миграция (для отладки)
python -m src.cli migrate --account "Account Name" --headless

# Открыть профиль
python -m src.cli open --account "Account Name"
```

## Чеклист перед релизом

- [x] Все 112+ тестов проходят
- [ ] GUI открывается без ошибок
- [ ] Import Sessions работает (GUI)
- [ ] Import Proxies работает (GUI)
- [ ] Migrate работает (проверено на реальном аккаунте)
- [ ] Open Profile работает (после миграции)
- [ ] Telethon сессия не инвалидируется
- [x] Cooldown рандомизирован
- [x] FloodWaitError обрабатывается
- [x] Нет утечек секретов в логах

## Следующие шаги

1. **Ручное тестирование GUI** - запустить, проверить все кнопки
2. **Реальная миграция** - протестировать на 1 аккаунте
3. **Fragment интеграция** - Phase 2
4. **PyInstaller build** - финальная сборка
