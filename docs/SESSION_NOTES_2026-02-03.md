# Session Notes - 2026-02-03

## КРИТИЧЕСКИ ВАЖНО ПОМНИТЬ

### 1. Direct Session Injection НЕ РАБОТАЕТ!

Потратили время на исследование - **прямая инъекция auth_key в localStorage браузера НЕ работает**, потому что:
- Telegram Web K валидирует сессии на сервере
- Просто записать auth_key недостаточно
- authState в IndexedDB сбрасывается приложением
- **Единственный рабочий путь - Programmatic QR Login**

### 2. Programmatic QR Login - РАБОТАЕТ

Уже реализовано в `src/telegram_auth.py`:
1. Браузер открывает web.telegram.org → показывает QR
2. Screenshot QR → decode token (jsQR, OpenCV, pyzbar)
3. Telethon `AcceptLoginTokenRequest(token)` → сервер подтверждает
4. Браузер получает авторизацию

### 3. Архитектура для 1000 аккаунтов

```
┌─────────────────────────┬───────────┬───────────┬────────────┐
│         Метрика         │ 100 акков │ 500 акков │ 1000 акков │
├─────────────────────────┼───────────┼───────────┼────────────┤
│ RAM (batch 10 parallel) │ ~2 GB     │ ~2 GB     │ ~2 GB      │
├─────────────────────────┼───────────┼───────────┼────────────┤
│ Disk (profiles)         │ ~10 GB    │ ~50 GB    │ ~100 GB    │
├─────────────────────────┼───────────┼───────────┼────────────┤
│ Time (45s cooldown)     │ ~1.5 ч    │ ~6 ч      │ ~12 ч      │
└─────────────────────────┴───────────┴───────────┴────────────┘
```

### 4. Решения по архитектуре (СОГЛАСОВАНО)

- **Fragment**: Только Telegram авторизация (вариант A), НЕ TON кошелёк
- **Переносимость**: Гибрид - основной ПК хранит профили, ноут как бэкап
  - Session файлы синхронизируем (~50MB для 1000)
  - Профили создаём локально по требованию
- **GoLogin vs Своё**: Своё решение (Camoufox) - безопаснее, данные у нас

### 5. Приоритеты реализации (СОГЛАСОВАНО)

1. Исправить критические баги (сейчас)
2. Параллельная миграция (10-50 браузеров, 12ч → 1-2ч)
3. Fragment интеграция
4. Health monitoring
5. UI/Dashboard

---

## КРИТИЧЕСКИЕ БАГИ (НУЖНО ИСПРАВИТЬ)

### Bug #1: NumPy/OpenCV конфликт
```
opencv-python          4.8.1.78  (старая, NumPy 1.x)
opencv-python-headless 4.12.0.88 (новая, NumPy 2.x)
numpy                  2.2.6
```
**Решение**: Удалить opencv-python, оставить opencv-python-headless

### Bug #2: Отсутствующие зависимости в requirements.txt
Добавить:
- `opencv-python-headless>=4.10.0`
- `numpy>=2.0.0`
- `pproxy>=2.7.0`

### Bug #3: storage_analysis/ не в .gitignore
Там лежат извлечённые auth_key! Добавить в .gitignore:
```
storage_analysis/
```

### Bug #4: scripts/ с экспериментами
Файлы `scripts/extract_tg_storage.py`, `scripts/inject_session.py`, `scripts/session_converter.py` - это эксперименты с Direct Injection. Можно удалить или переместить в `experiments/`.

---

## ЧТО УЖЕ РЕАЛИЗОВАНО

### src/telegram_auth.py (1266 строк)
- Multi-decoder QR (jsQR, QRCodeDetectorAruco, OpenCV, pyzbar)
- Device sync (device_model Telethon ↔ браузер)
- Retry логика (3 попытки QR)
- 2FA обработка
- Session verification после авторизации
- Batch processing с cooldown 45 сек
- Error recovery

### src/browser_manager.py
- Camoufox с persistent profiles
- Auto geoip для timezone/locale
- WebRTC blocking
- Storage state сохранение

### src/proxy_relay.py
- SOCKS5 с auth через локальный HTTP relay (pproxy)
- Браузеры НЕ поддерживают SOCKS5 auth напрямую - решено!

### decode_qr.js
- Node.js jsQR decoder
- Multiple preprocessing variants для rounded QR

### Тесты (7 файлов)
- tests/test_telegram_auth.py
- tests/test_browser_manager.py
- tests/test_proxy_relay.py
- tests/test_utils.py
- tests/test_integration.py
- tests/conftest.py

---

## ФАЙЛОВАЯ СТРУКТУРА ПРОЕКТА

```
tg-web-auth/
├── accounts/           # Исходные session файлы (в .gitignore)
├── profiles/           # Browser profiles (в .gitignore)
├── storage_analysis/   # ДОБАВИТЬ В .gitignore!
├── src/
│   ├── telegram_auth.py    # Основная логика QR auth
│   ├── browser_manager.py  # Camoufox управление
│   ├── proxy_relay.py      # SOCKS5 → HTTP relay
│   ├── pproxy_wrapper.py   # pproxy wrapper
│   ├── cli.py              # CLI интерфейс
│   ├── utils.py
│   └── security_check.py
├── scripts/            # Эксперименты (можно удалить)
├── tests/
├── decode_qr.js        # Node.js QR decoder
├── package.json
├── requirements.txt    # НУЖНО ОБНОВИТЬ!
├── CLAUDE.md
└── .gitignore          # НУЖНО ОБНОВИТЬ!
```

---

## СЛЕДУЮЩИЕ ШАГИ

1. **СЕЙЧАС**: Исправить requirements.txt и .gitignore
2. **СЕЙЧАС**: Запустить тесты, убедиться что работают
3. **ПОТОМ**: Реализовать параллельную миграцию
4. **ПОТОМ**: Fragment интеграция
5. **ПОТОМ**: Health monitoring

---

## КОМАНДЫ

```bash
# Установка
pip install -r requirements.txt
playwright install chromium
npm install  # для jsQR

# Миграция одного аккаунта
python -m src.telegram_auth --account "accounts/test/Name" --password "2fa"

# Batch миграция
python -m src.cli migrate --all

# Тесты
pytest -v
```

---

## GITHUB

Repository: https://github.com/convyrtech/tg124124
Branch: main
Last commit: 0cf426a feat: improve QR auth with multi-decoder support and proxy relay
