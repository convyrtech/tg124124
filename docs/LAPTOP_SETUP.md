# Перенос среды разработки на ноутбук

> **Цель:** В пару кликов продолжить работу на другом ПК
> **Дата:** 2026-02-04

---

## Что нужно перенести

| Компонент | Источник | Размер |
|-----------|----------|--------|
| Код проекта | git clone | ~5 MB |
| Аккаунты (sessions) | `D:\ТГФРАГ\tg-web-auth\accounts\` | ~50 MB |
| Browser profiles | НЕ переносим (создаются заново) | — |
| Claude Code + плагины | Установка с нуля | ~200 MB |

---

## Шаг 1: Установка базового софта

### 1.1 Python 3.13+
```powershell
# Скачай с https://www.python.org/downloads/
# При установке: ☑ Add to PATH
python --version  # должно быть 3.13.x
```

### 1.2 Node.js 24+ (для jsQR decoder)
```powershell
# Скачай с https://nodejs.org/
node --version  # должно быть 24.x
npm --version   # должно быть 11.x
```

### 1.3 Git
```powershell
# Скачай с https://git-scm.com/
git --version
```

---

## Шаг 2: Клонирование проекта

```powershell
# Выбери папку для проекта
cd D:\ТГФРАГ
git clone https://github.com/convyrtech/tg124124.git tg-web-auth
cd tg-web-auth
```

---

## Шаг 3: Python зависимости

```powershell
cd D:\ТГФРАГ\tg-web-auth

# Создать виртуальное окружение
python -m venv venv
venv\Scripts\activate

# Установить зависимости
pip install -r requirements.txt

# Скачать Camoufox browser binary
python -m camoufox fetch

# Проверить
python -c "import telethon; import camoufox; print('OK')"
```

### Полный список pip пакетов:
```
telethon>=1.36.0
PySocks>=1.7.1
camoufox>=0.4.0
playwright>=1.40.0
pyzbar>=0.1.9
Pillow>=10.0.0
opencv-python-headless>=4.10.0
numpy>=2.0.0
pproxy>=2.7.0
click>=8.0.0
aiofiles>=23.0.0
psutil>=5.9.0
pytest>=7.0.0
pytest-asyncio>=0.21.0
```

---

## Шаг 4: Node.js зависимости

```powershell
cd D:\ТГФРАГ\tg-web-auth
npm install
# Устанавливает: jimp, jsqr, qr-scanner
```

---

## Шаг 5: Перенос аккаунтов (SENSITIVE!)

**ВАЖНО:** Эти файлы содержат session keys. Переносить ТОЛЬКО через зашифрованный канал.

```powershell
# На СТАРОМ ПК — заархивировать:
# Папку accounts/ целиком
# КАЖДЫЙ аккаунт содержит:
#   session.session  — Telethon session (SQLite)
#   api.json         — API credentials
#   ___config.json   — имя + прокси

# На НОВОМ ПК — распаковать в:
D:\ТГФРАГ\tg-web-auth\accounts\
```

Структура должна быть:
```
accounts/
├── Софт 261/
│   ├── session.session
│   ├── api.json
│   └── ___config.json
├── Софт 313/
│   ├── session.session
│   ├── api.json
│   └── ___config.json
└── ...
```

---

## Шаг 6: Проверка что всё работает

```powershell
cd D:\ТГФРАГ\tg-web-auth
venv\Scripts\activate

# 1. Тесты (162 должны пройти)
pytest -v

# 2. Список аккаунтов
python -m src.cli list

# 3. Проверка прокси (БЕЗ VPN!)
python -m src.cli check --proxy "socks5:host:port:user:pass"

# 4. GUI
python -m src.gui.app
```

---

## Шаг 7: Claude Code + все плагины

### 7.1 Установка Claude Code
```powershell
npm install -g @anthropic-ai/claude-code
claude --version
```

### 7.2 Авторизация
```powershell
claude
# Пройти авторизацию через Anthropic аккаунт
```

### 7.3 Глобальные плагины (scope: user)

Эти плагины привязаны к пользователю, работают во всех проектах:

```powershell
claude plugins install github@claude-plugins-official
claude plugins install ralph-loop@claude-plugins-official
claude plugins install pyright-lsp@claude-plugins-official
claude plugins install code-simplifier@claude-plugins-official
claude plugins install serena@claude-plugins-official
```

### 7.4 Проектные плагины (scope: project)

Эти плагины привязаны к проекту tg-web-auth. Запускать из папки проекта:

```powershell
cd D:\ТГФРАГ\tg-web-auth

claude plugins install superpowers@claude-plugins-official
claude plugins install playwright@claude-plugins-official
claude plugins install context7@claude-plugins-official
claude plugins install feature-dev@claude-plugins-official
claude plugins install code-review@claude-plugins-official
claude plugins install pr-review-toolkit@claude-plugins-official
claude plugins install frontend-design@claude-plugins-official
claude plugins install python-development@claude-code-workflows
claude plugins install security-scanning@claude-code-workflows
claude plugins install developer-essentials@claude-code-workflows
claude plugins install think-through@ilia-izmailov-plugins
```

### 7.5 Проверить что всё установлено

Файл `.claude/settings.json` в проекте уже содержит список enabled плагинов (в git).
После установки плагинов они должны подхватиться автоматически.

```powershell
cd D:\ТГФРАГ\tg-web-auth
claude
# Проверить: /plugins — должен показать все 16 плагинов
```

### 7.6 Полный список плагинов (16 штук)

| # | Plugin | Scope | Source |
|---|--------|-------|--------|
| 1 | superpowers | project | claude-plugins-official |
| 2 | playwright | project | claude-plugins-official |
| 3 | context7 | project | claude-plugins-official |
| 4 | feature-dev | project | claude-plugins-official |
| 5 | code-review | project | claude-plugins-official |
| 6 | pr-review-toolkit | project | claude-plugins-official |
| 7 | frontend-design | project | claude-plugins-official |
| 8 | python-development | project | claude-code-workflows |
| 9 | security-scanning | project | claude-code-workflows |
| 10 | developer-essentials | project | claude-code-workflows |
| 11 | think-through | project | ilia-izmailov-plugins |
| 12 | github | user/global | claude-plugins-official |
| 13 | ralph-loop | user/global | claude-plugins-official |
| 14 | pyright-lsp | user/global | claude-plugins-official |
| 15 | code-simplifier | user/global | claude-plugins-official |
| 16 | serena | user/global | claude-plugins-official |

---

## Шаг 8: Кастомные команды Claude

Уже в git в `.claude/commands/research.md` — подхватится автоматически.
Команда `/research <тема>` — deep research перед реализацией.

---

## Быстрый скрипт "всё в одном"

Сохрани как `setup_laptop.ps1` и запусти на ноутбуке после установки Python/Node/Git:

```powershell
# setup_laptop.ps1 — Полная настройка среды

Write-Host "=== TG Web Auth: Laptop Setup ===" -ForegroundColor Cyan

# 1. Clone
cd D:\ТГФРАГ
git clone https://github.com/convyrtech/tg124124.git tg-web-auth
cd tg-web-auth

# 2. Python venv
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt
python -m camoufox fetch

# 3. Node.js
npm install

# 4. Tests
pytest -v

# 5. Claude Code plugins (global)
claude plugins install github@claude-plugins-official
claude plugins install ralph-loop@claude-plugins-official
claude plugins install pyright-lsp@claude-plugins-official
claude plugins install code-simplifier@claude-plugins-official
claude plugins install serena@claude-plugins-official

# 6. Claude Code plugins (project)
claude plugins install superpowers@claude-plugins-official
claude plugins install playwright@claude-plugins-official
claude plugins install context7@claude-plugins-official
claude plugins install feature-dev@claude-plugins-official
claude plugins install code-review@claude-plugins-official
claude plugins install pr-review-toolkit@claude-plugins-official
claude plugins install frontend-design@claude-plugins-official
claude plugins install python-development@claude-code-workflows
claude plugins install security-scanning@claude-code-workflows
claude plugins install developer-essentials@claude-code-workflows
claude plugins install think-through@ilia-izmailov-plugins

Write-Host ""
Write-Host "=== DONE ===" -ForegroundColor Green
Write-Host "Осталось:"
Write-Host "  1. Скопировать accounts/ с основного ПК"
Write-Host "  2. Убедиться что VPN ВЫКЛЮЧЕН"
Write-Host "  3. claude  (запустить Claude Code)"
```

---

## Чеклист после настройки

```
[ ] Python 3.13+ установлен
[ ] Node.js 24+ установлен
[ ] Git установлен
[ ] Проект склонирован
[ ] venv создан, pip install OK
[ ] camoufox fetch OK
[ ] npm install OK
[ ] pytest — 162 passed
[ ] accounts/ скопированы
[ ] VPN ВЫКЛЮЧЕН
[ ] Прокси работают: python -m src.cli check --proxy "..."
[ ] Claude Code установлен
[ ] 5 глобальных плагинов установлены
[ ] 11 проектных плагинов установлены
[ ] claude запускается в папке проекта
[ ] python -m src.cli list показывает аккаунты
```
