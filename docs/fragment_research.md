# Fragment.com Integration Research

> **Date:** 2026-02-04
> **Status:** Research complete
> **Goal:** Understand Fragment.com auth flow for automated integration

---

## 1. What is Fragment.com?

Fragment is a **non-custodial auction platform** owned by Fragment Corp. (Seychelles). It allows:
- Buying/selling **collectible Telegram usernames** (NFTs on TON blockchain)
- Buying/selling **anonymous phone numbers** (TON collectibles)
- Purchasing **Telegram Stars** and **Telegram Premium**
- Depositing funds for **Telegram Ads**
- Withdrawing **content creator ad rewards**

All transactions use **TON** (The Open Network) cryptocurrency.

---

## 2. Fragment Authentication: TWO Separate Methods

Fragment has **two independent** login methods. They are NOT linked to each other (by design, per Fragment's privacy policy).

### 2.1 "Connect Telegram" (Telegram Login Widget)

**Purpose:** Identity verification, receive notifications about bids/offers, link Telegram account.

**Flow:**
1. User clicks "Connect Telegram" button on fragment.com
2. Fragment shows the standard **Telegram Login Widget**
3. User enters phone number
4. Telegram sends a **verification code** as a service notification to all logged-in sessions (NOT SMS)
   - Code type: `auth.sentCodeTypeApp` - delivered as Telegram internal message
   - Alternative: `auth.sentCodeTypeFragmentSms` - view code on fragment.com itself (for collectible numbers)
5. User enters the code on fragment.com
6. Fragment receives: `id, first_name, last_name, username, photo_url, auth_date, hash`
7. Phone number remains hidden from Fragment

**Key insight:** This is the standard Telegram Login Widget (`core.telegram.org/widgets/login`), NOT OAuth2. It's a server-side verification flow.

**Session storage:** Fragment stores a session cookie on `fragment.com` domain. This is completely separate from `web.telegram.org` sessions.

### 2.2 "Connect TON" (TON Wallet via TonConnect)

**Purpose:** Wallet connection for buying/selling, transactions, auction participation.

**Flow:**
1. User clicks "Connect TON" button
2. Fragment shows a QR code (TonConnect protocol)
3. User scans QR with TON wallet app (Tonkeeper, MyTonWallet, etc.)
4. Wallet confirms connection
5. Fragment links the wallet address to the browser session

**Key insight:** This requires a TON wallet. Our accounts already have TON wallets in `___config.json` (TonWalletAddress + TonMnemonic).

---

## 3. Can We Reuse web.telegram.org Sessions?

### Answer: NO

- `web.telegram.org` uses MTProto-over-WebSocket with its own auth_key stored in IndexedDB
- `fragment.com` uses Telegram Login Widget which creates a completely separate session
- Being logged into web.telegram.org does **NOT** automatically log you into fragment.com
- They are on different domains with different session mechanisms
- Fragment's privacy policy explicitly states: "Company does not store any data that links these two login methods to your IP address, browser agent, hardware, other identifiable information or to each other"

### What IS shared:
- The **browser profile** (Camoufox) provides the same fingerprint, cookies, proxy
- The **Telethon client** can intercept verification codes sent to the account
- The same proxy is used for both (IP consistency)

---

## 4. Automation Strategy

### 4.1 Telegram Login on Fragment (Priority)

```
┌─────────────┐     ┌──────────────┐     ┌─────────────────┐
│   Telethon  │     │   Camoufox   │     │  fragment.com   │
│   Client    │     │   Browser    │     │    Server       │
└──────┬──────┘     └──────┬───────┘     └────────┬────────┘
       │                   │                      │
       │  1. Already connected (from QR migration)│
       │                   │                      │
       │                   │  2. Open fragment.com │
       │                   │─────────────────────>│
       │                   │                      │
       │                   │  3. Click "Connect Telegram"
       │                   │─────────────────────>│
       │                   │                      │
       │                   │  4. Enter phone number│
       │                   │─────────────────────>│
       │                   │                      │
       │  5. Receive verification code            │
       │<─────────────────────────────────────────│
       │                   │                      │
       │  6. Extract code  │                      │
       │──────────────────>│                      │
       │                   │  7. Enter code        │
       │                   │─────────────────────>│
       │                   │                      │
       │                   │  8. Authenticated!    │
       │                   │<─────────────────────│
```

**Steps to automate:**
1. Open `fragment.com` in existing Camoufox profile (same proxy)
2. Click "Connect Telegram" button
3. Wait for phone number input field
4. Enter phone number (get from Telethon `client.get_me()`)
5. Wait for Telegram to send verification code
6. Intercept code using Telethon event handler (`events.NewMessage` from Telegram service)
7. Enter code in browser
8. Verify authorization succeeded
9. Save browser profile (session persists in cookies)

### 4.2 TON Wallet Connection (Stretch Goal)

This is significantly more complex because TonConnect requires a real wallet app interaction:

**Option A: TonConnect SDK injection**
- Inject TonConnect JS SDK into page
- Programmatically approve connection using mnemonic from ___config.json
- Complex but possible

**Option B: Browser extension simulation**
- Install Tonkeeper browser extension in Camoufox profile
- Import wallet using mnemonic
- Click "Connect TON" and approve via extension
- Requires extension compatibility with Camoufox (Firefox-based)

**Option C: Skip wallet connection**
- Only connect Telegram account
- Wallet connection can be done manually later when needed
- Sufficient for most use cases (viewing assets, receiving notifications)

**Recommendation:** Start with Telegram-only auth (4.1). TON wallet is only needed for actual transactions.

---

## 5. Security & Risk Analysis

### 5.1 Ban risk

| Risk | Level | Notes |
|------|-------|-------|
| Fragment ban from Telegram | LOW | Fragment is a separate platform, different ban system |
| Account flagging from rapid logins | MEDIUM | Telegram Login Widget sends codes - rapid requests could trigger rate limits |
| IP consistency | IMPORTANT | Use same proxy for fragment.com as for web.telegram.org |
| Fingerprint linking | LOW | Fragment doesn't store fingerprint data (per privacy policy) |

### 5.2 Rate Limits

- Telegram Login Widget verification codes have the same rate limits as regular Telegram login
- `auth.sendCode` has undocumented rate limits, typically ~5 attempts per phone per hour
- Cooldown between accounts recommended: **60+ seconds**
- If FloodWait is triggered, respect the wait time

### 5.3 Code Interception

The verification code arrives as a Telegram service notification (`auth.sentCodeTypeApp`). We can intercept it using:

```python
# Listen for verification code from Telegram
@client.on(events.NewMessage(from_users=777000))  # 777000 = Telegram official
async def code_handler(event):
    # Extract code from message text
    code = extract_code(event.raw_text)
```

User 777000 is the official Telegram service account that sends login codes.

---

## 6. Technical Requirements

### Dependencies
- Existing: Camoufox, Playwright, Telethon, proxy relay
- New: None (all existing infrastructure is sufficient)

### Phone Number Extraction
Telethon client can provide the phone number:
```python
me = await client.get_me()
phone = me.phone  # e.g., "79991234567"
```

### Fragment Page Selectors (to investigate during implementation)
- "Connect Telegram" button
- Phone number input field
- Verification code input field
- Success state detection

### Browser Profile Reuse
Fragment session will be stored in the same Camoufox profile:
- Same `profiles/<account>/browser_data/` directory
- Cookies for `fragment.com` will persist alongside `web.telegram.org`
- No additional profile management needed

---

## 7. Implementation Plan

### Phase 1: Fragment Telegram Auth (`src/fragment_auth.py`)
- [ ] Open fragment.com in existing profile
- [ ] Detect current auth state (already logged in?)
- [ ] Click "Connect Telegram"
- [ ] Enter phone number
- [ ] Intercept verification code via Telethon
- [ ] Enter code in browser
- [ ] Verify success
- [ ] Save profile

### Phase 2: CLI Integration
- [ ] `python -m src.cli fragment --account "Name"`
- [ ] `python -m src.cli fragment --all`
- [ ] Batch mode with cooldown

### Phase 3: GUI Integration
- [ ] Fragment button in account actions
- [ ] Status column for Fragment auth

### Phase 4: TON Wallet (stretch)
- [ ] Research TonConnect programmatic connection
- [ ] Implement wallet injection or extension approach

---

## 8. Open Questions

1. **Fragment Login Widget exact selectors** - Need to inspect fragment.com live to get CSS selectors
2. **Does Fragment detect headless browsers?** - Need to test with Camoufox
3. **TonConnect automation feasibility** - Needs deeper research
4. **Fragment session longevity** - How long do sessions last? Do they expire?
5. **KYC requirements** - Fragment now requires KYC for some operations (wallet verification). Does this affect Telegram login?

---

## Sources

- Fragment About page: https://fragment.com/about
- Fragment Privacy Policy: https://fragment.com/privacy
- Fragment Terms of Service: https://fragment.com/terms
- Telegram Login Widget docs: https://core.telegram.org/widgets/login
- Telegram User Authorization: https://core.telegram.org/api/auth
- Telegram API Methods (Fragment collectibles): https://core.telegram.org/methods
- Fragment unofficial Python API: https://github.com/iw4p/Ton-Fragment
- TonConnect protocol: via Tonkeeper integration
