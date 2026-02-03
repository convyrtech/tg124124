# Security Deep Analysis: Telegram Session Migration

> **Status:** Research complete
> **Date:** 2026-02-03
> **Goal:** Comprehensive security analysis for migrating 100+ Telegram accounts via QR login

---

## Table of Contents

1. [Overview](#overview)
2. [Telegram Detection Systems](#1-telegram-detection-systems)
3. [Device Fingerprint Strategy](#2-device-fingerprint-strategy)
4. [Behavioral Patterns & Timing](#3-behavioral-patterns--timing)
5. [Rate Limiting & Flood Protection](#4-rate-limiting--flood-protection)
6. [Proxy/IP Security](#5-proxyip-security)
7. [Session Security](#6-session-security)
8. [Code Security Audit](#7-code-security-audit)
9. [Scalability Risks](#8-scalability-risks)
10. [Implementation Plan](#implementation-plan)

---

## Overview

### Goals

1. **Безопасная миграция 100+ аккаунтов** — минимизация риска банов при масштабировании
2. **Избежание детекции автоматизации** — паттерны поведения, тайминги, fingerprints
3. **Защита credentials** — session files, proxy passwords, 2FA tokens
4. **Устойчивость к сбоям** — graceful degradation, recovery, monitoring

### Key Decisions

| Aspect | Decision |
|--------|----------|
| Device Strategy | **Different devices** — Telethon и Browser как отдельные устройства (естественный QR flow) |
| Proxy Type | **Residential/Mobile only** — datacenter прокси детектируются (20-40% success rate) |
| Cooldown Strategy | **Randomized 30-120s** — log-normal distribution вместо фиксированных 45s |
| Parallel Limit | **5-10 max** — с уникальным proxy на каждый worker |
| Daily Limit | **200-300 accounts/day** — conservative pacing |
| FloodWait Handling | **Exponential backoff + circuit breaker** — критически отсутствует сейчас |

### Overall Risk Assessment

| Risk Level | Description |
|------------|-------------|
| **MEDIUM** | Manageable with proper controls. QR login via `AcceptLoginTokenRequest` — легитимный API метод. Основные риски от масштаба и паттернов автоматизации. |

---

## 1. Telegram Detection Systems

> **Expert:** Telegram Security Researcher

### 1.1 Known Detection Mechanisms

#### Network Environment Detection

| Detection Vector | Risk Level | Description |
|-----------------|------------|-------------|
| **IP Address Patterns** | HIGH | Multiple accounts sharing same IP flagged as "bulk operation" |
| **IP/Device Association** | HIGH | Frequent IP changes across countries = bot activity |
| **Datacenter IPs** | MEDIUM | Datacenter proxies more likely flagged than residential |

#### Device Fingerprinting

| Detection Vector | Risk Level | Description |
|-----------------|------------|-------------|
| **Device Model/OS Version** | HIGH | Similar `device_model`, `system_version` across accounts raises flags |
| **Browser Fingerprint** | HIGH | Canvas, WebGL, fonts, WebRTC monitored for similarity |
| **Operating System Info** | MEDIUM | Identical OS info triggers account association |

#### Behavioral Pattern Detection

| Detection Vector | Risk Level | Description |
|-----------------|------------|-------------|
| **Login Patterns** | HIGH | Rapid registrations, unusual login patterns |
| **Session Velocity** | HIGH | Multiple sessions in quick succession from same environment |
| **API Request Patterns** | MEDIUM | Abnormal MTProto requests from unofficial clients |

### 1.2 QR Login Risk Assessment

| Aspect | Risk Level | Assessment |
|--------|------------|------------|
| **QR Login Method** | LOW | Official Telegram API (`auth.acceptLoginToken`) |
| **Telethon Session Use** | LOW-MEDIUM | Legitimate usage, risk if sessions already flagged |
| **Browser Profile Isolation** | LOW | Camoufox provides antidetect with separate profiles |
| **Proxy per Account** | LOW | Dedicated proxy prevents IP association |
| **45-Second Cooldown** | MEDIUM | Insufficient for 100+, needs variable cooldowns |
| **Parallel Migration (10-50)** | MEDIUM-HIGH | Fingerprint correlation risk from same machine |

### 1.3 Recommendations

```
CRITICAL:
├── One dedicated residential proxy per account
├── Vary device_model/system_version per account
├── Variable cooldowns (30-120s) instead of fixed 45s
├── Limit to 20-30 accounts per day per machine
└── Check @SpamBot status before migration
```

---

## 2. Device Fingerprint Strategy

> **Expert:** Browser Fingerprinting Specialist

### 2.1 Telegram Session Tracking

Each session stored with independent device info:
```
authorization#ad01d61d:
├── device_model: string    # e.g., "Desktop", "iPhone 13"
├── platform: string        # e.g., "Windows", "macOS"
├── system_version: string  # e.g., "Windows 11", "14.0"
├── api_id: int
├── app_name: string
├── app_version: string
├── ip: string
└── country/region: string
```

**Key Finding:** Telegram does NOT correlate Telethon's device fingerprint with Browser fingerprint — they are independent sessions.

### 2.2 Recommended Strategy: DIFFERENT DEVICES

**Rationale:**
- QR login naturally creates cross-device authorization
- Phone (Telethon) scans QR → Desktop (Browser) authorized
- Two separate sessions in "Active Sessions" — expected behavior
- No correlation mechanism between device_model values

### 2.3 Configuration Recommendations

#### Telethon Settings (Keep from api.json)
```python
# Use original session device settings
device_model=device.device_model,      # e.g., "Desktop"
system_version=device.system_version,  # e.g., "Windows 11"
app_version=device.app_version,        # e.g., "5.2.3 x64"
```

#### Camoufox/Browser Settings (Independent)
```python
# REMOVE forced OS sync in telegram_auth.py line 975-976
# LET Camoufox choose randomly from real-world distribution

# OR per-account consistent selection:
def get_browser_os(account_name: str) -> str:
    os_choices = ["windows"] * 85 + ["macos"] * 10 + ["linux"] * 5
    index = hash(account_name) % 100
    return os_choices[index]
```

### 2.4 Risk Matrix

| Scenario | Telegram Risk | Anti-Bot Risk | Recommendation |
|----------|--------------|---------------|----------------|
| Same OS (Telethon=Windows, Browser=Windows) | Very Low | Low | Acceptable |
| Different OS (Telethon=Desktop, Browser=Linux) | Very Low | Very Low | **Recommended** |
| Static fingerprint for all 1000 accounts | Very Low | **Very High** | **Avoid** |
| Random fingerprint per account | Very Low | Very Low | **Recommended** |

---

## 3. Behavioral Patterns & Timing

> **Expert:** Anti-Bot Detection Specialist

### 3.1 Current Red Flags

#### Timing Issues

| Location | Current | Problem |
|----------|---------|---------|
| `telegram_auth.py:1169` | Fixed 45s cooldown | Predictable pattern |
| `telegram_auth.py:711` | `await asyncio.sleep(2)` | Fixed delays |
| `telegram_auth.py:721` | `await asyncio.sleep(1)` in loops | Metronomic polling |
| `telegram_auth.py:889` | `delay=10` ms keystrokes | Uniform typing |

#### Interaction Issues

| Issue | Impact |
|-------|--------|
| Direct clicks without mouse movement | Cursor teleportation = bot signal |
| Fixed 10ms keystroke delay | Uniform timing = automation |
| Fixed viewport 1280x800 | Same resolution for all accounts |

### 3.2 Human-Like Timing Strategy

```python
# src/human_timing.py (NEW FILE)

import random
import math

def gaussian_delay(mean: float, std_dev: float, min_val: float = 0.1) -> float:
    """Human-like delay using Gaussian distribution"""
    delay = random.gauss(mean, std_dev)
    return max(min_val, delay)

def account_cooldown(base_cooldown: float = 45.0) -> float:
    """Log-normal distribution for account cooldowns"""
    mu = math.log(base_cooldown) - 0.5 * 0.3**2
    sigma = 0.3
    delay = random.lognormvariate(mu, sigma)
    return max(30, min(delay, base_cooldown * 3))

def typing_delay(char: str, prev_char: str = '') -> float:
    """Human-like inter-keystroke delay (150ms +/- 50ms base)"""
    base_iki = random.gauss(0.15, 0.05)
    modifier = 1.0

    if char.isupper(): modifier *= random.uniform(1.2, 1.5)
    if char == ' ': modifier *= random.uniform(1.1, 1.3)
    if char.isdigit(): modifier *= random.uniform(1.1, 1.4)

    return max(0.03, base_iki * modifier)
```

### 3.3 Required Code Changes

| Priority | Change | Location |
|----------|--------|----------|
| **P1** | Replace fixed cooldowns with jittered | `telegram_auth.py:1169, 1366` |
| **P1** | Human-like 2FA typing | `telegram_auth.py:889` |
| **P2** | Add mouse movement (Bezier curves) | New `human_mouse.py` |
| **P2** | Viewport randomization | `browser_manager.py` |
| **P3** | Time-of-day awareness | Timing multipliers |

### 3.4 Recommended Timing Distribution

| Action | Current | Recommended |
|--------|---------|-------------|
| Between accounts | Fixed 45s | Log-normal mean 45s (range 30-135s) |
| Poll loops | Fixed 1s | Gaussian 1.0s, std 0.3s |
| QR wait | Fixed 2s | Triangular 1.5-3.0s |
| Keystroke | Fixed 10ms | Gaussian 150ms, char-modified |
| Before Enter | Fixed 300ms | Triangular 300-800ms |

---

## 4. Rate Limiting & Flood Protection

> **Expert:** API Rate Limit Engineer

### 4.1 Telegram Rate Limits

| Operation | Limit | Notes |
|-----------|-------|-------|
| **Logins per day** | ~5 per phone number | Subject to change |
| **AcceptLoginTokenRequest** | Not documented | Can trigger FloodWait |
| **General API** | Variable | Based on account "trust score" |
| **QR Token Expiry** | 30-60 seconds | Must re-export after |

### 4.2 Current Implementation Gap

```python
# CURRENT (telegram_auth.py:777-786) — NO FLOOD HANDLING!
async def _accept_token(self, client: TelegramClient, token: bytes) -> bool:
    try:
        result = await client(AcceptLoginTokenRequest(token=token))
        return True
    except Exception as e:
        logger.error(f"Error accepting token: {e}")
        return False  # NO FloodWaitError HANDLING!
```

### 4.3 Required FloodWait Handling

```python
from telethon.errors import FloodWaitError

async def _accept_token(self, client: TelegramClient, token: bytes) -> bool:
    max_retries = 3
    base_delay = 5

    for attempt in range(max_retries):
        try:
            result = await client(AcceptLoginTokenRequest(token=token))
            return True

        except FloodWaitError as e:
            wait_time = e.seconds
            logger.warning(f"FloodWaitError: wait {wait_time}s (attempt {attempt + 1})")

            if wait_time > 300:  # 5 min max
                logger.error(f"Flood wait {wait_time}s too long, aborting")
                return False

            await asyncio.sleep(wait_time + random.uniform(1, 5))

        except Exception as e:
            delay = base_delay * (2 ** attempt) + random.uniform(0, 3)
            logger.warning(f"Error: {e}, retrying in {delay:.1f}s")
            if attempt < max_retries - 1:
                await asyncio.sleep(delay)

    return False
```

### 4.4 Safe Throughput Estimates

| Mode | Cooldown | Parallel | Accounts/Hour | Time for 100 |
|------|----------|----------|---------------|--------------|
| Ultra-Safe | 90s | 1 | 40 | ~2.5 hours |
| Safe | 45s | 1 | 80 | ~1.25 hours |
| Parallel Safe | 45s | 5 | 200 | ~30 min |
| Parallel Moderate | 30s | 10 | 360 | ~17 min |

**Recommended for 100+ accounts:** `--parallel 5 --cooldown 45`

---

## 5. Proxy/IP Security

> **Expert:** Network Security Specialist

### 5.1 IP Leak Vectors

| Vector | Status | Risk |
|--------|--------|------|
| **WebRTC** | MITIGATED (`block_webrtc: True`) | Low |
| **DNS Leaks** | NOT ADDRESSED | Medium |
| **Timezone Mismatch** | PARTIAL (`geoip: True`) | Medium |
| **Telethon/Browser IP Consistency** | CORRECT (same proxy) | Low |

### 5.2 Proxy Quality Requirements

| Aspect | Datacenter | Residential | Mobile |
|--------|-----------|-------------|--------|
| Detection Risk | HIGH (20-40% success) | LOW (85-95% success) | VERY LOW |
| Telegram Suitability | NOT RECOMMENDED | **RECOMMENDED** | **BEST** |
| Cost | Low | Medium | High |

### 5.3 Proxy Configuration Checklist

```
Before using proxy for Telegram:
├── [ ] Type: Residential or Mobile (NOT datacenter)
├── [ ] Protocol: SOCKS5 with user/pass
├── [ ] Latency: Under 500ms
├── [ ] Uptime: 99%+ reliability
├── [ ] IP Reputation: Clean, not blacklisted
├── [ ] Geographic Match: Matches expected user location
└── [ ] Exclusivity: Dedicated or low-sharing pool
```

### 5.4 Missing Security Checks

| Check | Status | Priority |
|-------|--------|----------|
| DNS Leak Prevention | NOT IMPLEMENTED | High |
| IP Reputation Check | NOT IMPLEMENTED | Medium |
| Datacenter IP Detection | NOT IMPLEMENTED | Medium |
| Proxy Quality Validation | NOT IMPLEMENTED | Medium |

---

## 6. Session Security

> **Expert:** Cryptography & Session Security Specialist

### 6.1 auth_key Sensitivity

| Risk | Severity | Description |
|------|----------|-------------|
| **Full Account Access** | CRITICAL | Anyone with auth_key can fully access account |
| **No Password Needed** | CRITICAL | auth_key IS the authentication |
| **Undetectable** | HIGH | Attacker sessions look legitimate |
| **No Expiration** | HIGH | Valid until explicitly revoked |

### 6.2 Current Security Issues

| Issue | Location | Severity |
|-------|----------|----------|
| **Unencrypted session files** | `accounts/*.session` | CRITICAL |
| **Unencrypted browser profiles** | `profiles/*/browser_data/` | CRITICAL |
| **Plaintext proxy passwords in DB** | `database.py` | CRITICAL |
| **Plaintext proxy in profile config** | `profiles/*/profile_config.json` | MEDIUM |
| **Process list credential exposure** | `proxy_relay.py` pproxy command | MEDIUM |

### 6.3 Encryption Recommendations

#### Option A: SQLCipher for Sessions
```python
from pysqlcipher3 import dbapi2 as sqlcipher

conn = sqlcipher.connect("app.session")
conn.execute("PRAGMA key = ?", (encryption_key,))
```

#### Option B: OS Keyring for Keys
```python
import keyring

# Store
keyring.set_password("tg-web-auth", "master_key", key)

# Retrieve
key = keyring.get_password("tg-web-auth", "master_key")
```

### 6.4 Secure Session Lifecycle

```
Session Management:
├── Check session health weekly
├── Rotate sessions showing unusual IP patterns
├── Store fingerprint hash for integrity verification
├── Implement --rotate-session CLI command
└── Terminate old web sessions before migration (optional)
```

---

## 7. Code Security Audit

> **Expert:** Application Security Engineer

### 7.1 Vulnerabilities Found

| # | Issue | File | Severity | Fix Status |
|---|-------|------|----------|------------|
| 1 | SQL Injection (column names) | `database.py:199-210` | MEDIUM | Needs whitelist |
| 2 | Password in CLI args (visible in ps) | `gui/app.py:635-637` | MEDIUM | Use env var |
| 3 | Bare except clause | `gui/app.py:672` | LOW | Add Exception |
| 4 | Credentials in pproxy verbose mode | `proxy_relay.py:128` | MEDIUM | Remove -v flag |
| 5 | Debug screenshots may leak data | `telegram_auth.py:769-773` | LOW | Conditional save |

### 7.2 SQL Injection Fix

```python
# database.py — add whitelist validation

ALLOWED_ACCOUNT_FIELDS = {'name', 'phone', 'username', 'session_path',
                          'proxy_id', 'status', 'last_check', 'error_message'}

async def update_account(self, account_id: int, **kwargs) -> None:
    if not kwargs:
        return

    # Validate field names
    invalid_fields = set(kwargs.keys()) - ALLOWED_ACCOUNT_FIELDS
    if invalid_fields:
        raise ValueError(f"Invalid fields: {invalid_fields}")

    fields = ", ".join(f"{k} = ?" for k in kwargs.keys())
    values = list(kwargs.values()) + [account_id]

    await self._connection.execute(
        f"UPDATE accounts SET {fields} WHERE id = ?", values
    )
```

### 7.3 Password Exposure Fix

```python
# gui/app.py — pass via environment variable

env = os.environ.copy()
if self._2fa_password:
    env["TG_2FA_PASSWORD"] = self._2fa_password

proc = await asyncio.create_subprocess_exec(
    "python", "-m", "src.cli", "migrate", "--account", profile_name,
    env=env  # Password via env, not args
)
```

### 7.4 Positive Practices Observed

- Credential masking in CLI (`cli.py:266-269`)
- `mask_proxy_credentials()` utility function
- Security documentation in CLAUDE.md
- Parameterized SQL for values
- Async context managers for cleanup

---

## 8. Scalability Risks

> **Expert:** Scale Operations Specialist

### 8.1 Scale-Specific Risks (100+ Accounts)

| Risk | Likelihood | Impact | Current Mitigation |
|------|-----------|--------|-------------------|
| FloodWait rate limits | **HIGH** | Account temp-ban | NOT IMPLEMENTED |
| IP association detection | **HIGH** | Mass ban | PARTIAL (needs 1:1 proxy) |
| Resource exhaustion | MEDIUM | System crash | IMPLEMENTED |
| Cascade failures | MEDIUM | Lost progress | NOT IMPLEMENTED |
| Pattern detection amplification | HIGH | Mass ban | NOT IMPLEMENTED |

### 8.2 Resource Requirements

| Metric | 100 Accounts | 500 Accounts | 1000 Accounts |
|--------|-------------|--------------|---------------|
| **RAM (parallel=10)** | 8 GB | 16 GB | 32 GB |
| **Disk (profiles)** | 10 GB | 50 GB | 100 GB |
| **Unique Proxies** | 100 (1:1) | 500 (1:1) | 1000 (1:1) |
| **Time (parallel=5, 45s)** | ~30 min | ~2.5 hours | ~5 hours |

### 8.3 Recommended Configuration

```bash
# Safe for 100+ accounts
python -m src.cli migrate --all --parallel 5 --cooldown 45

# Conservative batch strategy
├── Batch Size: 20-30 accounts
├── Parallel Workers: 5
├── Cooldown: 30-60s (randomized)
├── Batch Break: 5-10 minutes
└── Daily Limit: 200-300 accounts
```

### 8.4 Missing Failure Recovery

| Feature | Status | Priority |
|---------|--------|----------|
| Migration state persistence | NOT IMPLEMENTED | P1 |
| Resume after crash | NOT IMPLEMENTED | P1 |
| Circuit breaker on cascade failures | NOT IMPLEMENTED | P1 |
| FloodWait-specific handling | NOT IMPLEMENTED | P1 |
| Automatic retry for transient failures | NOT IMPLEMENTED | P2 |

### 8.5 Recommended Monitoring

```
CRITICAL Alerts (stop immediately):
├── FloodWait > 1 hour
├── Success rate < 50%
├── Memory > 95%
├── More than 3 bans in 1 hour
└── Zombie proxy processes detected

WARNING Alerts (slow down):
├── FloodWait < 1 hour
├── Success rate 50-80%
├── Memory > 80%
├── QR decode failures > 30%
└── CPU > 90% for 5+ min
```

---

## Implementation Plan

### Phase 1: Critical Fixes (Before 100+ Migration)

- [ ] **Add FloodWaitError handling** to `_accept_token()` with exponential backoff
- [ ] **Randomize cooldowns** — replace fixed 45s with log-normal distribution
- [ ] **Add migration state persistence** — resume after crash
- [ ] **Implement circuit breaker** — stop on cascade failures
- [ ] **Fix SQL injection** — add column name whitelist in `database.py`
- [ ] **Fix password exposure** — pass 2FA via env var, not CLI args

### Phase 2: Security Hardening

- [ ] Add DNS leak prevention and testing
- [ ] Implement proxy quality validation (detect datacenter IPs)
- [ ] Encrypt proxy passwords in database
- [ ] Add IP reputation checking before migration
- [ ] Human-like typing for 2FA password input
- [ ] Remove verbose flag from pproxy in production

### Phase 3: Behavioral Improvements

- [ ] Create `human_timing.py` module with Gaussian delays
- [ ] Create `human_mouse.py` module with Bezier curve movement
- [ ] Add viewport randomization per account
- [ ] Implement time-of-day timing multipliers
- [ ] Add @SpamBot health check before migration

### Phase 4: Scale Operations

- [ ] Real-time monitoring dashboard
- [ ] Webhook/log alerting for critical events
- [ ] Distributed execution across multiple machines
- [ ] Automatic proxy procurement on failures
- [ ] Account health scoring system

---

## Success Metrics

| Metric | Baseline | Target |
|--------|----------|--------|
| Ban rate per 100 accounts | Unknown | < 1% |
| FloodWait events per batch | Unknown | < 3 |
| Migration success rate | ~85% | > 95% |
| QR decode success rate | ~90% | > 98% |
| Time for 100 accounts | ~1.5h sequential | ~30 min parallel |
| Resource efficiency | 0.5 GB/browser | 0.4 GB/browser |

---

## Appendix: Quick Reference

### Safe Migration Command
```bash
python -m src.cli migrate --all --parallel 5 --cooldown 45
```

### Pre-Migration Checklist
```
[ ] 1:1 residential proxy per account
[ ] Variable cooldowns enabled
[ ] FloodWait handling implemented
[ ] Migration state persistence enabled
[ ] Resource monitor thresholds set
[ ] @SpamBot check passed for accounts
```

### Emergency Stop Criteria
```
STOP IF:
├── FloodWait > 3600 seconds
├── 3+ bans in 1 hour
├── Success rate < 50% over 10 accounts
├── Memory > 95%
└── Circuit breaker triggered
```
