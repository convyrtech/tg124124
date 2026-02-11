# TG Web Auth - Prompt Generator & Plugin Guide for Claude Code

---

## Part 1: Prompt Generator Template

Copy this template into any Claude conversation. Describe your task where `[DESCRIBE YOUR TASK]` is.
Claude will generate a ready `/ralph-loop` command.

```
Generate a prompt for Claude Code (ralph-loop:ralph-loop) for the following task:
[DESCRIBE YOUR TASK]

Requirements for generated prompt:

1. FORMAT
   - Use: /ralph-loop:ralph-loop "..." --completion-promise "MEANINGFUL_TEXT" --max-iterations N
   - Usually 50-100 iterations for complex tasks, 20-40 for simple
   - Completion promise must be unique and meaningful (e.g., "WORKER_POOL_DONE", "FRAGMENT_FIXED")

2. LANGUAGE AND STRUCTURE
   - Prompt must be in English
   - Very detailed, do not lose any details from the task description
   - Steps numbered and logically organized into phases
   - Include clear SUCCESS CRITERIA section at the end

3. CONFIGURATION FILES
   - Do NOT specify concrete config file paths
   - Let Claude discover and modify configuration files on its own

4. PLUGINS - Include this exact section in the generated prompt:
   PLUGINS AND AGENTS TO USE ACTIVELY:

   Python Development:
   - python-pro agent: Use for complex Python patterns, async optimization
     Call: "Use python-pro agent to review async resource management"
   - python-development:async-python-patterns skill: For asyncio patterns
   - python-development:python-resource-management skill: For context managers and cleanup
   - python-development:python-error-handling skill: For exception hierarchies
   - python-development:python-testing-patterns skill: For pytest fixtures and mocking
   - python-development:python-resilience skill: For retry/backoff patterns

   Code Quality:
   - code-simplifier agent: Clean up code after changes
     Call: "Use code-simplifier agent to simplify modified code"
   - code-review:code-review skill: Review changes
     Call: "/code-review to review all changes"
   - pr-review-toolkit:review-pr skill: Comprehensive final review
     Call: "/pr-review-toolkit:review-pr for final review"
   - feature-dev:code-reviewer agent: Bug and logic error detection
     Call: "Use feature-dev:code-reviewer to find bugs in modified files"

   Architecture:
   - feature-dev:code-architect agent: Design feature architectures
     Call: "Use feature-dev:code-architect to design module structure"
   - feature-dev:code-explorer agent: Trace execution paths
     Call: "Use feature-dev:code-explorer to trace the auth flow"

   Security:
   - security-scanning:security-sast skill: Static security analysis
     Call: "/security-scanning:security-sast to scan for vulnerabilities"
   - security-auditor agent: Security audit
     Call: "Use security-auditor agent to audit proxy and session handling"

   Research:
   - context7 MCP: Look up library docs (Telethon, Camoufox, Playwright, asyncio)
     Call: "Use context7 to look up Telethon AcceptLoginTokenRequest API"
   - think-through:deep-thinking skill: Complex architectural decisions
     Call: "/think-through:deep-thinking to analyze the best approach"

   Workflow:
   - superpowers:systematic-debugging skill: For any bugs or test failures
   - superpowers:verification-before-completion skill: Before claiming done
   - superpowers:test-driven-development skill: Write tests first

5. PLUGIN USAGE IN STEPS
   - Include explicit plugin/agent calls within the steps themselves
   - Example: "Use python-pro agent to review the worker pool implementation"
   - Example: "Use feature-dev:code-explorer to trace resource lifecycle"
   - Example: "/security-scanning:security-sast to check for leaked credentials"

6. PYTHON ENVIRONMENT
   - Run Python and pytest via venv (let Claude discover the path)
   - Install packages via pip in venv
   - ALWAYS run in venv, never system Python

7. TESTING
   - Run pytest after EVERY significant change
   - ALL existing tests must continue passing
   - Write NEW tests for any new functionality (test-first when possible)
   - Test files go in tests/ directory
   - Use pytest-asyncio for async tests
   - Mock external dependencies (Telethon, Camoufox, network)

8. TELEGRAM SAFETY RULES - Include in RULES section:
   - NEVER use same session file from 2 clients simultaneously
   - NEVER log auth_key, api_hash, passwords, tokens, phone numbers
   - Same proxy for Telethon AND browser per account
   - Max 40 QR logins per hour globally
   - 30-90 second randomized cooldown between operations
   - 1 dedicated proxy per account, never share

9. RESOURCE MANAGEMENT
   - ALL browser instances MUST be closed in finally blocks
   - ALL pproxy processes MUST be killed on exit
   - Use async context managers (async with) for resources
   - Track active processes, kill zombies on shutdown
   - Test cleanup by simulating failures

10. REFACTORING FREEDOM
    - Full freedom to refactor, rename, delete, rewrite
    - Backward compatibility NOT required for internal modules
    - But: preserve all public CLI interface
    - But: preserve all existing test interfaces

11. DOCUMENTATION
    - Read CLAUDE.md before starting
    - Read docs/research_project_audit_2026-02-06.md for known issues
    - Read docs/FIX_PLAN_2026-02-03.md for unfixed bugs
    - Study relevant code thoroughly before making changes

12. PERSISTENCE
    - Do NOT skip anything
    - Do NOT give up
    - Iterate until fully solved
    - If stuck for 3+ attempts on same problem - step back and try completely different approach
    - Check .claude/projects/ memory files for previous session insights

13. VERIFICATION
    - Run pytest after each major change
    - For async code: verify no resource leaks with explicit cleanup tests
    - For browser code: verify browser processes are terminated
    - Use /superpowers:verification-before-completion before claiming done

14. GIT WORKFLOW
    - Commit after each completed phase (not after every small change)
    - Commit message format: "feat/fix/refactor(module): description"
    - Never commit secrets, session files, or profile data
    - git add specific files, never git add -A

15. FINAL REVIEW - At the end, use agents with explicit calls:
    - "Use code-simplifier agent to clean up all modified code"
    - "/code-review to review all changes for quality and bugs"
    - "/security-scanning:security-sast to scan for security issues"
    - "Use feature-dev:code-reviewer to find logic errors"

16. RULES SECTION - Include at the end of the prompt:
    RULES:
    - NEVER skip any step or requirement
    - NEVER give up - if approach doesn't work, try different approach
    - ALWAYS run pytest after significant changes
    - ALWAYS use async context managers for resource cleanup
    - ALWAYS verify no zombie processes after browser/proxy operations
    - NEVER log secrets (auth_key, api_hash, passwords, tokens, phones)
    - NEVER use same session from 2 clients simultaneously
    - Python via venv only, never system Python
    - If stuck for 3+ attempts - step back, try completely different approach
    - Full freedom to refactor - backward compatibility NOT required
    - Use /superpowers:verification-before-completion before claiming done
    - Commit after each completed phase with descriptive message

Output only the ready command, no explanations.
```

---

## Part 2: Recommended MCP Servers

### Tier 1: Install Now (High Impact)

#### SQLite MCP Server
Directly query Telethon `.session` files and project database without writing Python scripts.

```json
// Add to .claude/settings.json under "mcpServers":
{
  "sqlite": {
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-sqlite"]
  }
}
```

**Use cases:**
- Inspect session auth_key metadata (dc_id, server_address) without scripts
- Query migration database directly
- Debug "why did account X fail" by checking DB state

#### Filesystem MCP Server
Safe batch operations on 1000 account/profile directories with path sandboxing.

```json
{
  "filesystem": {
    "command": "npx",
    "args": [
      "-y", "@modelcontextprotocol/server-filesystem",
      "D:/ТГФРАГ/tg-web-auth/accounts",
      "D:/ТГФРАГ/tg-web-auth/profiles",
      "D:/ТГФРАГ/tg-web-auth/data"
    ]
  }
}
```

**Use cases:**
- List all account directories, find missing configs
- Batch check profile existence/sizes
- Safe move/rename operations (can't accidentally touch system files)

### Tier 2: Install When Needed

#### Custom FastMCP Server (Build It Yourself)
Wrap your Python functions as Claude-callable tools.

```bash
pip install fastmcp
```

Then create `mcp_server.py` at project root that exposes:
- `inspect_session(account_name)` - read session metadata
- `list_unmigrated()` - accounts without browser profiles
- `check_proxy(host, port)` - proxy health check
- `migration_status()` - current state summary

**When:** Phase 2 (parallel migration), when you need Claude to interact with running processes.

#### GitHub MCP Server
For issue/PR management if repo goes public.

```json
{
  "github": {
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-github"],
    "env": { "GITHUB_TOKEN": "<token>" }
  }
}
```

### What NOT to Install
| Server | Why Skip |
|--------|----------|
| Docker MCP | No Docker in this project |
| Brave Search | Already have Tavily |
| Puppeteer MCP | Already have Playwright MCP |
| Memory MCP | Built-in .claude/projects/ memory is sufficient |
| Notion/Jira MCP | No PM tools used |

---

## Part 3: Currently Installed Plugins

### Already Active (settings.json)

| Plugin | What It Does | How to Use |
|--------|-------------|------------|
| **superpowers** | Workflow skills (debugging, TDD, verification, planning, git worktrees) | `/superpowers:systematic-debugging`, `/superpowers:verification-before-completion` |
| **playwright** | Browser MCP (navigate, click, screenshot, evaluate) | `mcp__plugin_playwright_playwright__browser_*` tools |
| **context7** | Library docs lookup | "Use context7 to look up Telethon API" |
| **feature-dev** | Code architecture, exploration, review agents | "Use feature-dev:code-architect agent" |
| **python-development** | Python skills (async, testing, error handling, resources) | `/python-development:async-python-patterns` |
| **security-scanning** | SAST, threat modeling, security audit | `/security-scanning:security-sast` |
| **developer-essentials** | Git workflows, debugging, SQL, auth patterns | `/developer-essentials:debugging-strategies` |
| **code-review** | Code review for PRs | `/code-review:code-review` |
| **pr-review-toolkit** | Comprehensive PR analysis (6 specialized agents) | `/pr-review-toolkit:review-pr` |
| **frontend-design** | Frontend UI design | Not needed for this project |
| **think-through** | Deep structured analysis | `/think-through:deep-thinking` |

### Most Useful for This Project

**Daily use:**
- `python-development:async-python-patterns` - asyncio is everywhere in this code
- `python-development:python-resource-management` - #1 problem is resource leaks
- `python-development:python-testing-patterns` - test coverage is 3/10
- `superpowers:systematic-debugging` - for any test failures
- `superpowers:verification-before-completion` - before claiming done
- `context7` - look up Telethon/Camoufox/Playwright APIs

**Per-feature:**
- `feature-dev:code-architect` - design new modules (worker pool, profile lifecycle)
- `security-scanning:security-sast` - ensure no credential leaks
- `think-through:deep-thinking` - architectural decisions

**Before merge:**
- `code-review:code-review` - quality check
- `pr-review-toolkit:review-pr` - comprehensive analysis
- `code-simplifier` agent - clean up

### Not Useful (Can Disable)
- `frontend-design` - this is a backend/automation project
- `developer-essentials:monorepo-management` - single project, not monorepo

---

## Part 4: Example Prompts for Key Tasks

### Fix Resource Leaks (Priority 0)

```
/ralph-loop:ralph-loop "
TASK: Fix all resource leaks in browser_manager.py and proxy_relay.py

CONTEXT:
- docs/research_project_audit_2026-02-06.md section 1.2 and 1.3
- Critical: zombie pproxy processes on timeout, leaked browser contexts
- FIX-003: Lock files remain after browser crash
- FIX-007: Browser launch hangs without timeout

PLUGINS AND AGENTS TO USE ACTIVELY:
[full plugin section]

PHASE 1: ANALYZE (1-5)
1. Read audit report section 1.2 (browser_manager) and 1.3 (proxy_relay)
2. Use feature-dev:code-explorer to trace browser lifecycle: launch -> use -> close
3. Use feature-dev:code-explorer to trace proxy relay lifecycle: start -> use -> stop
4. Identify all exit paths where resources are NOT cleaned up
5. Read existing tests for both modules

PHASE 2: FIX BROWSER (6-20)
6. Add timeout to all browser launch operations (max 60s)
7. Wrap proxy_relay.start() in try/except, ensure cleanup on failure
8. Fix _active_browsers dict leak on re-init
9. Add process PID tracking for all child processes
10. Add force-kill for lingering processes in close()
11. Use python-development:python-resource-management for patterns
12. Write tests: timeout during launch, crash during auth, cleanup on exception
13. Run pytest - all must pass

PHASE 3: FIX PROXY RELAY (21-35)
14. Fix pproxy process leak on returncode race
15. Add heartbeat health check before usage
16. Fix stop() deadlock (force kill after 5s)
17. Add zombie detection with psutil
18. Write tests: relay crash, timeout, parallel management
19. Run pytest - all must pass

PHASE 4: INTEGRATION (36-45)
20. Add shutdown handler killing ALL child processes
21. Test: launch browser with proxy -> crash -> verify cleanup
22. Use python-pro agent to review async resource management
23. /security-scanning:security-sast for security check
24. Run pytest - all 162+ tests must pass

PHASE 5: REVIEW (46-50)
25. Use code-simplifier agent to clean modified code
26. Use feature-dev:code-reviewer for bug detection
27. /superpowers:verification-before-completion
28. Commit: fix(resources): eliminate zombie processes and resource leaks

SUCCESS CRITERIA:
- No zombie pproxy processes after any operation
- No leaked browser contexts
- All operations have explicit timeouts
- Cleanup works on: normal exit, exception, timeout, crash
- All tests pass + new cleanup tests added

RULES:
[full rules section]
" --completion-promise "RESOURCE_LEAKS_FIXED" --max-iterations 50
```

### Implement Worker Pool (Priority 0)

```
/ralph-loop:ralph-loop "
TASK: Replace ParallelMigrationController with asyncio.Queue worker pool

CONTEXT:
- Current: creates 1000 coroutines gated by semaphore (wasteful)
- Target: 5-8 workers pulling from asyncio.Queue (constant memory)
- See docs/research_project_audit_2026-02-06.md section 5.2
- Hardware: 16GB RAM, max 8 concurrent browsers

[phases: design with code-architect, implement, test, review]

SUCCESS CRITERIA:
- Memory constant regardless of total accounts (tested with 100 mock jobs)
- Built-in retry (max 2 retries per account)
- Graceful shutdown (stop accepting, finish current, cleanup)
- Resource monitor integration (adaptive worker count)
- Global rate limit: max 40 QR logins/hour
- All existing tests pass + new worker pool tests

" --completion-promise "WORKER_POOL_DONE" --max-iterations 80
```

### Consolidate State to SQLite (Priority 1)

```
/ralph-loop:ralph-loop "
TASK: Migrate migration_state.py (JSON) into database.py (SQLite WAL)

CONTEXT:
- migration_state.py uses JSON file with file locking - race conditions under parallel writes
- database.py already has accounts/proxies/migrations tables
- Need to merge functionality, deprecate JSON state

[phases: analyze both, design schema changes, migrate, test, deprecate old]

SUCCESS CRITERIA:
- All state tracking via SQLite WAL mode
- No JSON file for state (migration_state.py deprecated)
- Concurrent writes work without contention
- Crash recovery via WAL journal
- All existing tests updated to use new state
- migration_state.py tests migrated to test_database.py

" --completion-promise "SQLITE_CONSOLIDATED" --max-iterations 40
```

### Fix Fragment.com (Priority 0 for Fragment)

```
/ralph-loop:ralph-loop "
TASK: Fix all 11 critical bugs in fragment_auth.py, verify CSS selectors on real site

CONTEXT:
- docs/research_project_audit_2026-02-06.md section 1.4
- Fragment uses Telegram Login Widget (separate auth from web.telegram.org)
- ALL CSS selectors unverified - use Playwright MCP to check real fragment.com

[phases: verify selectors with Playwright, fix bugs, add retry, test]

SUCCESS CRITERIA:
- All CSS selectors verified on real fragment.com (screenshots as proof)
- asyncio.Event race condition fixed
- Regex only catches actual verification codes
- Retry logic with exponential backoff
- Phone validation added
- All tests pass + new Fragment tests

" --completion-promise "FRAGMENT_FIXED" --max-iterations 60
```

---

## Part 5: Quick Reference

### Run Tests
```bash
pytest -v --tb=short
```

### Key Files to Read First
```
CLAUDE.md                                    # Project instructions
docs/research_project_audit_2026-02-06.md    # Full audit with all issues
docs/FIX_PLAN_2026-02-03.md                  # Unfixed bugs FIX-001..007
src/telegram_auth.py                         # Core QR auth (1266 lines)
src/browser_manager.py                       # Browser lifecycle
src/proxy_relay.py                           # SOCKS5 relay
src/fragment_auth.py                         # Fragment auth (11 bugs)
src/database.py                              # SQLite state
src/migration_state.py                       # JSON state (to deprecate)
```

### Safety Checklist Before Any Migration Run
- [ ] Each account has unique proxy in ___config.json
- [ ] No other process using same .session files
- [ ] Proxy geo matches account expectations
- [ ] Cooldown set to 30-90s between accounts
- [ ] Max concurrent browsers <= 8
- [ ] Max QR logins/hour <= 40
