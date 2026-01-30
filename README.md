# TG Web Auth Migration Tool

Автоматическая миграция Telegram session файлов в browser profiles для web.telegram.org

## Quick Start с Claude Code

### 1. Подготовка

```bash
# Клонируй/скопируй эту папку к себе
cd tg-web-auth

# Положи тестовый аккаунт
# accounts/test/session.session
# accounts/test/api.json  
# accounts/test/___config.json
```

### 2. Установи плагины Claude Code (один раз)

```bash
# В Claude Code терминале:
/plugin marketplace add obra/superpowers-marketplace
/plugin install superpowers@superpowers-marketplace
```

### 3. Workflow в Claude Code

#### Этап 1: Research (если не понимаешь как)
```
/superpowers:brainstorm

[скопируй промпт из PROMPTS.md → Этап 1]
```

#### Этап 2: Plan (когда понял примерно)
```
Shift+Tab (включить Plan Mode)

[скопируй промпт из PROMPTS.md → Этап 2]
```

#### Этап 3: Implement (по плану)
```
[скопируй промпты из PROMPTS.md → Этап 3]
```

## Структура проекта

```
tg-web-auth/
├── CLAUDE.md           # Контекст для Claude Code (читает автоматом)
├── PROMPTS.md          # Готовые промпты для каждого этапа
├── .claude/
│   └── commands/
│       └── research.md # Кастомная команда /project:research
├── accounts/           # Сюда кладёшь session файлы
│   └── test/
├── profiles/           # Сюда сохраняются browser profiles
├── docs/               # Research документы
├── src/                # Исходники (создаст Claude Code)
└── requirements.txt    # Зависимости
```

## Файлы аккаунта

Для каждого аккаунта нужны 3 файла:

| Файл | Описание |
|------|----------|
| `session.session` | Telethon SQLite session (256 bytes auth_key) |
| `api.json` | api_id, api_hash, device info |
| `___config.json` | Имя аккаунта, прокси |

## Безопасность

⚠️ **НИКОГДА не коммить:**
- `accounts/` - содержит auth_key
- `profiles/` - содержит browser sessions
- `.env` файлы с credentials

Добавь в `.gitignore`:
```
accounts/
profiles/
*.session
.env
```
