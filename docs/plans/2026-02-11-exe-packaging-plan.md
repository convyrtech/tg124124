# План: Упаковка в EXE и отправка клиенту для тестов

**Дата:** 2026-02-11
**Цель:** Собрать надёжный дистрибутив, который клиент может запустить и при ошибке прислать диагностику

---

## Аудит: Найденные проблемы

### P0 — Блокеры для EXE

| # | Проблема | Файл:строка | Описание |
|---|----------|-------------|----------|
| 1 | `print()` в security_check.py | security_check.py:301-336 | 15 вызовов print(). В EXE console=False — вывод пропадёт |
| 2 | `zxingcpp` отсутствует в spec | TGWebAuth.spec:32-50 | Имя `zxing` — неправильное, пакет `zxingcpp` |
| 3 | `camoufox_path()` vs `launch_path()` | build_exe.py:63 | Разные функции определения пути — могут не совпадать |

### P1 — Важно для диагностики у клиента

| # | Проблема | Файл | Описание |
|---|----------|------|----------|
| 4 | Нет DEBUG-режима | logger.py | Клиент не может включить подробные логи. Нужен env var `TGWA_DEBUG=1` |
| 5 | asyncio exception handler не ставится | exception_handler.py | Ставит только sys.excepthook, asyncio loop создаётся позже — краши в async коде не ловятся |
| 6 | Diagnostics ZIP неполный | gui/app.py | Не хватает: кол-во профилей, camoufox version, список ошибок из DB |

### P2 — Желательно

| # | Проблема | Файл | Описание |
|---|----------|------|----------|
| 7 | `print()` в logger.py:71 | logger.py | Fallback print при ошибке file logging — в EXE пропадёт (edge case, допустимо) |
| 8 | Нет smoke test для EXE | — | После сборки нет проверки что EXE запускается |

---

## План работ

### Шаг 1: Мелкие фиксы — руками (5 мин)

Быстрые правки не стоят затрат на агентов:

- **print() → logger** в security_check.py (15 строк)
- **zxingcpp** в TGWebAuth.spec hiddenimports
- **camoufox path** в build_exe.py — унифицировать с app.py

### Шаг 2: Team из 4 агентов (параллельно)

Один кодер + три ревьюера. Ревьюеры проверяют СУЩЕСТВУЮЩИЙ код параллельно с кодером — не ждут его.

**Coder-1: Diagnostics + DEBUG + asyncio handler**
- `src/logger.py`: env var `TGWA_DEBUG=1` → level=DEBUG
- `src/gui/app.py`: diagnostics ZIP — добавить profiles count/sizes, camoufox version
- `src/gui/app.py` + `src/exception_handler.py`: установить asyncio exception handler после создания event loop
- Владение файлами: `logger.py`, `gui/app.py`, `exception_handler.py`

**Reviewer-1: Except-блоки + error logging**
- Прочесать ВСЕ except-блоки в src/*.py
- Найти silent `pass` — классифицировать: допустимо / нужен logging
- Проверить уровни: error vs warning vs debug — адекватны ли
- Результат: список проблем с file:line

**Reviewer-2: Security — credentials в логах**
- Proxy credentials в diagnostics DB — обнуляются? Проверить код
- phone/api_hash/auth_key — могут попасть в app.log через traceback?
- last_crash.txt — traceback может содержать чувствительные данные?
- Результат: список утечек с file:line

**Reviewer-3: Frozen-mode + AI-косяки**
- Все `Path(__file__)` и `os.path.dirname(__file__)` — защищены sys.frozen?
- paths.py импортируется везде где нужны пути? Нет ли забытых `Path("profiles")`?
- Типичные AI-ошибки: несуществующие методы, неправильные import paths, copy-paste
- Результат: список проблем с file:line

### Шаг 3: Фиксы по ревью + pytest

- Собрать замечания от 3 ревьюеров
- Исправить P0/P1, задокументировать P2 как known issues
- `pytest` — 332+ теста должны пройти

### Шаг 4: Коммит + сборка

- Финальный коммит
- `python build_exe.py` → dist/TGWebAuth.zip
- Smoke test: запустить EXE, проверить GUI, Collect Logs

---

## Критерии готовности

- [ ] Все print() в src/ заменены на logging (кроме build_exe.py)
- [ ] DEBUG-режим включается через `TGWA_DEBUG=1`
- [ ] Diagnostics ZIP достаточен для удалённой отладки
- [ ] Asyncio crashes ловятся и пишутся в лог
- [ ] Нет утечки credentials в логах/diagnostics
- [ ] pytest проходит
- [ ] EXE запускается и показывает GUI
