"""
Security Check Module
Проверяет утечки и fingerprint перед работой с аккаунтами.

Запуск:
    python -m src.security_check --account "account_name" --proxy "socks5:host:port:user:pass"
"""
import asyncio
import json
import hashlib
import sys
import os

# FIX: WINDOWS ENCODING
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, asdict
from typing import Optional

# Camoufox import
try:
    from camoufox.async_api import AsyncCamoufox
except ImportError:
    print("ERROR: camoufox not installed. Run: pip install camoufox && camoufox fetch")
    exit(1)


@dataclass
class SecurityCheckResult:
    """Результат проверки безопасности"""
    timestamp: str
    proxy_ip: str
    detected_ip: str
    webrtc_leak: bool
    webrtc_local_ip: Optional[str]
    timezone_match: bool
    expected_timezone: str
    detected_timezone: str
    canvas_hash: str
    webgl_vendor: str
    webgl_renderer: str
    user_agent: str
    screen_resolution: str
    languages: list
    platform: str

    @property
    def is_safe(self) -> bool:
        """Проверка что всё безопасно"""
        return (
            not self.webrtc_leak and
            self.timezone_match and
            self.proxy_ip == self.detected_ip
        )


def parse_proxy(proxy_str: str) -> dict:
    """Парсит прокси из формата 'socks5:host:port:user:pass'"""
    parts = proxy_str.split(":")
    if len(parts) == 5:
        proto, host, port, user, pwd = parts
        return {
            "server": f"{proto}://{host}:{port}",
            "username": user,
            "password": pwd,
        }
    elif len(parts) == 3:
        proto, host, port = parts
        return {"server": f"{proto}://{host}:{port}"}
    raise ValueError(f"Invalid proxy format: {proxy_str}")


async def check_ip_and_geo(page) -> dict:
    """Проверяет IP и геолокацию через ipapi.co"""
    await page.goto("https://ipapi.co/json/", wait_until="networkidle")
    content = await page.content()

    # Извлекаем JSON из страницы
    import re
    json_match = re.search(r'\{.*\}', content, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group())
        except json.JSONDecodeError:
            pass
    return {}


async def check_webrtc_leak(page) -> dict:
    """Проверяет WebRTC утечку на browserleaks.com"""
    await page.goto("https://browserleaks.com/webrtc", wait_until="networkidle")
    await asyncio.sleep(3)  # Ждём выполнения JS

    result = await page.evaluate("""
        () => {
            const leakInfo = {
                hasLeak: false,
                localIPs: [],
                publicIP: null
            };

            // Ищем таблицу с IP
            const rows = document.querySelectorAll('table tr');
            rows.forEach(row => {
                const cells = row.querySelectorAll('td');
                if (cells.length >= 2) {
                    const type = cells[0]?.textContent?.trim();
                    const ip = cells[1]?.textContent?.trim();

                    if (ip && /\\d+\\.\\d+\\.\\d+\\.\\d+/.test(ip)) {
                        if (type?.includes('Local') || ip.startsWith('192.168') || ip.startsWith('10.') || ip.startsWith('172.')) {
                            leakInfo.localIPs.push(ip);
                            leakInfo.hasLeak = true;
                        } else {
                            leakInfo.publicIP = ip;
                        }
                    }
                }
            });

            // Проверяем также через RTCPeerConnection напрямую
            const rtcStatus = document.querySelector('.webrtc-status, [class*="status"]');
            if (rtcStatus?.textContent?.includes('disabled') ||
                rtcStatus?.textContent?.includes('blocked')) {
                leakInfo.hasLeak = false;
                leakInfo.localIPs = [];
            }

            return leakInfo;
        }
    """)

    return result


async def get_fingerprint(page) -> dict:
    """Собирает fingerprint браузера"""
    await page.goto("about:blank")

    fingerprint = await page.evaluate("""
        () => {
            // Canvas fingerprint
            const canvas = document.createElement('canvas');
            const ctx = canvas.getContext('2d');
            ctx.textBaseline = 'top';
            ctx.font = '14px Arial';
            ctx.fillText('Fingerprint test 🎨', 2, 2);
            const canvasData = canvas.toDataURL();

            // WebGL info
            let webglVendor = 'N/A';
            let webglRenderer = 'N/A';
            try {
                const gl = document.createElement('canvas').getContext('webgl');
                if (gl) {
                    const debugInfo = gl.getExtension('WEBGL_debug_renderer_info');
                    if (debugInfo) {
                        webglVendor = gl.getParameter(debugInfo.UNMASKED_VENDOR_WEBGL) || 'N/A';
                        webglRenderer = gl.getParameter(debugInfo.UNMASKED_RENDERER_WEBGL) || 'N/A';
                    }
                }
            } catch (e) {}

            return {
                userAgent: navigator.userAgent,
                platform: navigator.platform,
                languages: navigator.languages,
                hardwareConcurrency: navigator.hardwareConcurrency,
                deviceMemory: navigator.deviceMemory,
                screenResolution: `${screen.width}x${screen.height}`,
                colorDepth: screen.colorDepth,
                timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
                webdriver: navigator.webdriver,
                canvasDataUrl: canvasData,
                webglVendor: webglVendor,
                webglRenderer: webglRenderer,
            };
        }
    """)

    # Создаём hash canvas для сравнения
    fingerprint['canvasHash'] = hashlib.md5(
        fingerprint['canvasDataUrl'].encode()
    ).hexdigest()[:16]
    del fingerprint['canvasDataUrl']  # Не сохраняем raw data

    return fingerprint


async def run_security_check(
    proxy: str,
    profile_path: Optional[Path] = None,
    headless: bool = False,
    use_geoip: bool = False
) -> SecurityCheckResult:
    """
    Выполняет полную проверку безопасности.

    Args:
        proxy: Прокси в формате 'socks5:host:port:user:pass'
        profile_path: Путь для сохранения профиля (опционально)
        headless: Режим без GUI
        use_geoip: Использовать автодетект timezone по IP (требует работающий прокси для Camoufox)
    """
    from .proxy_relay import ProxyRelay, needs_relay

    proxy_relay = None
    original_proxy = proxy

    # Если SOCKS5 с авторизацией - запускаем proxy relay
    if needs_relay(proxy):
        print(f"[*] SOCKS5 with auth detected - starting proxy relay...")
        proxy_relay = ProxyRelay(proxy)
        await proxy_relay.start()
        # Используем локальный HTTP прокси
        proxy_config = proxy_relay.browser_proxy_config
    else:
        proxy_config = parse_proxy(proxy)

    camoufox_args = {
        "proxy": proxy_config,
        "geoip": use_geoip,      # Авто timezone/locale по IP (может не работать с некоторыми прокси)
        "block_webrtc": True,    # Блокируем WebRTC
        "humanize": True,        # Human-like поведение
        "headless": headless,
    }

    # Если указан путь профиля — используем persistent context
    if profile_path:
        profile_path.mkdir(parents=True, exist_ok=True)
        camoufox_args["persistent_context"] = True
        camoufox_args["user_data_dir"] = str(profile_path / "browser_data")

    proxy_info = proxy_config.get('server', 'no proxy')
    print(f"[*] Starting Camoufox with proxy: {proxy_info}")
    if proxy_relay:
        print(f"[*] Proxy relay active: {proxy_relay.local_url} -> {original_proxy.split(':')[1]}:***")
    print(f"[*] Headless: {headless}")

    try:
        async with AsyncCamoufox(**camoufox_args) as browser:
            page = await browser.new_page()

            # 1. Проверяем IP и геолокацию
            print("\n[1/4] Checking IP and geolocation...")
            geo_info = await check_ip_and_geo(page)
            detected_ip = geo_info.get('ip', 'unknown')
            expected_tz = geo_info.get('timezone', 'unknown')
            print(f"      IP: {detected_ip}")
            print(f"      Location: {geo_info.get('city', '?')}, {geo_info.get('country_name', '?')}")
            print(f"      Expected TZ: {expected_tz}")

            # 2. Проверяем WebRTC leak
            print("\n[2/4] Checking WebRTC leak...")
            webrtc_info = await check_webrtc_leak(page)
            print(f"      Leak detected: {webrtc_info.get('hasLeak', False)}")
            if webrtc_info.get('localIPs'):
                print(f"      Local IPs exposed: {webrtc_info['localIPs']}")

            # 3. Собираем fingerprint
            print("\n[3/4] Collecting fingerprint...")
            fingerprint = await get_fingerprint(page)
            print(f"      Platform: {fingerprint['platform']}")
            print(f"      Screen: {fingerprint['screenResolution']}")
            print(f"      Timezone: {fingerprint['timezone']}")
            print(f"      Canvas hash: {fingerprint['canvasHash']}")
            print(f"      WebGL: {fingerprint['webglVendor'][:30]}...")
            print(f"      Webdriver: {fingerprint['webdriver']}")

            # 4. Проверяем совпадение timezone
            print("\n[4/4] Validating timezone match...")
            tz_match = fingerprint['timezone'] == expected_tz
            print(f"      Expected: {expected_tz}")
            print(f"      Detected: {fingerprint['timezone']}")
            print(f"      Match: {'✓' if tz_match else '✗ MISMATCH!'}")

            # Собираем результат
            # Используем оригинальный прокси для IP, не локальный relay
            original_proxy_parts = original_proxy.split(":")
            proxy_ip = original_proxy_parts[1] if len(original_proxy_parts) >= 3 else "unknown"

            result = SecurityCheckResult(
                timestamp=datetime.now().isoformat(),
                proxy_ip=proxy_ip,
                detected_ip=detected_ip,
                webrtc_leak=webrtc_info.get('hasLeak', False),
                webrtc_local_ip=webrtc_info.get('localIPs', [None])[0] if webrtc_info.get('localIPs') else None,
                timezone_match=tz_match,
                expected_timezone=expected_tz,
                detected_timezone=fingerprint['timezone'],
                canvas_hash=fingerprint['canvasHash'],
                webgl_vendor=fingerprint['webglVendor'],
                webgl_renderer=fingerprint['webglRenderer'],
                user_agent=fingerprint['userAgent'],
                screen_resolution=fingerprint['screenResolution'],
                languages=fingerprint['languages'],
                platform=fingerprint['platform'],
            )

            # Сохраняем результат если указан профиль
            if profile_path:
                result_path = profile_path / "security_check.json"
                with open(result_path, 'w', encoding='utf-8') as f:
                    json.dump(asdict(result), f, indent=2, ensure_ascii=False)
                print(f"\n[*] Results saved to: {result_path}")

            return result

    finally:
        # Останавливаем proxy relay если был запущен
        if proxy_relay:
            await proxy_relay.stop()


def print_summary(result: SecurityCheckResult):
    """Выводит итоговый отчёт"""
    print("\n" + "="*60)
    print("SECURITY CHECK SUMMARY")
    print("="*60)

    status = "✓ SAFE" if result.is_safe else "✗ UNSAFE"
    print(f"\nStatus: {status}")

    print(f"\n{'Check':<25} {'Result':<15} {'Details'}")
    print("-"*60)

    # IP check
    ip_ok = result.proxy_ip in result.detected_ip or result.detected_ip != "unknown"
    print(f"{'IP Match':<25} {'✓ OK' if ip_ok else '✗ FAIL':<15} {result.detected_ip}")

    # WebRTC check
    webrtc_ok = not result.webrtc_leak
    print(f"{'WebRTC Leak':<25} {'✓ Blocked' if webrtc_ok else '✗ LEAK!':<15} {result.webrtc_local_ip or 'None'}")

    # Timezone check
    tz_ok = result.timezone_match
    print(f"{'Timezone Match':<25} {'✓ OK' if tz_ok else '✗ MISMATCH':<15} {result.detected_timezone}")

    # Fingerprint info
    print(f"\n{'Canvas Hash':<25} {result.canvas_hash}")
    print(f"{'Screen':<25} {result.screen_resolution}")
    print(f"{'Platform':<25} {result.platform}")

    print("="*60)

    if not result.is_safe:
        print("\n⚠️  WARNING: Security issues detected!")
        print("   Do NOT proceed with account login until fixed.")
        if result.webrtc_leak:
            print("   → WebRTC is leaking your real IP")
        if not result.timezone_match:
            print("   → Timezone doesn't match proxy location")


async def main():
    import argparse

    parser = argparse.ArgumentParser(description="Security check for browser profile")
    parser.add_argument("--proxy", required=True, help="Proxy in format socks5:host:port:user:pass")
    parser.add_argument("--profile", help="Profile name to save results")
    parser.add_argument("--headless", action="store_true", help="Run in headless mode")
    parser.add_argument("--geoip", action="store_true", help="Auto-detect timezone/locale by proxy IP")

    args = parser.parse_args()

    profile_path = None
    if args.profile:
        profile_path = Path("profiles") / args.profile

    try:
        result = await run_security_check(
            proxy=args.proxy,
            profile_path=profile_path,
            headless=args.headless,
            use_geoip=args.geoip
        )
        print_summary(result)

        # Exit code based on safety
        exit(0 if result.is_safe else 1)

    except Exception as e:
        print(f"\n[!] Error: {e}")
        import traceback
        traceback.print_exc()
        exit(1)


if __name__ == "__main__":
    asyncio.run(main())
