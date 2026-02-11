"""
Security Check Module
Проверяет утечки и fingerprint перед работой с аккаунтами.

Запуск:
    python -m src.security_check --account "account_name" --proxy "socks5:host:port:user:pass"
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

# FIX: WINDOWS ENCODING
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')

from .utils import parse_proxy_for_camoufox as parse_proxy

logger = logging.getLogger(__name__)

# Camoufox import
try:
    from camoufox.async_api import AsyncCamoufox
except ImportError as e:
    raise ImportError("camoufox not installed. Run: pip install camoufox && camoufox fetch") from e


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


async def check_ip_and_geo(page) -> dict:
    """Проверяет IP и геолокацию через ipapi.co"""
    await page.goto("https://ipapi.co/json/", wait_until="networkidle")
    content = await page.content()

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

    # Если SOCKS5 с авторизацией - запускаем proxy relay
    if needs_relay(proxy):
        logger.info("SOCKS5 with auth detected - starting proxy relay")
        proxy_relay = ProxyRelay(proxy)
        await proxy_relay.start()
        proxy_config = proxy_relay.browser_proxy_config
    else:
        proxy_config = parse_proxy(proxy)

    camoufox_args = {
        "proxy": proxy_config,
        "geoip": use_geoip,
        "block_webrtc": True,
        "humanize": True,
        "headless": headless,
    }

    # Если указан путь профиля — используем persistent context
    if profile_path:
        profile_path.mkdir(parents=True, exist_ok=True)
        camoufox_args["persistent_context"] = True
        camoufox_args["user_data_dir"] = str(profile_path / "browser_data")

    proxy_info = proxy_config.get('server', 'no proxy')
    logger.info("Starting Camoufox with proxy: %s, headless: %s", proxy_info, headless)
    if proxy_relay:
        logger.info("Proxy relay active: %s", proxy_relay.local_url)

    try:
        async with AsyncCamoufox(**camoufox_args) as browser:
            page = await browser.new_page()

            # 1. Проверяем IP и геолокацию
            logger.info("[1/4] Checking IP and geolocation...")
            geo_info = await check_ip_and_geo(page)
            detected_ip = geo_info.get('ip', 'unknown')
            expected_tz = geo_info.get('timezone', 'unknown')
            logger.info("IP: %s, Location: %s %s, Expected TZ: %s",
                        detected_ip, geo_info.get('city', '?'),
                        geo_info.get('country_name', '?'), expected_tz)

            # 2. Проверяем WebRTC leak
            logger.info("[2/4] Checking WebRTC leak...")
            webrtc_info = await check_webrtc_leak(page)
            logger.info("WebRTC leak detected: %s", webrtc_info.get('hasLeak', False))
            if webrtc_info.get('localIPs'):
                logger.warning("Local IPs exposed via WebRTC: %s", webrtc_info['localIPs'])

            # 3. Собираем fingerprint
            logger.info("[3/4] Collecting fingerprint...")
            fingerprint = await get_fingerprint(page)
            logger.info("Platform: %s, Screen: %s, Timezone: %s",
                        fingerprint['platform'], fingerprint['screenResolution'],
                        fingerprint['timezone'])
            logger.info("Canvas hash: %s, WebGL: %s..., Webdriver: %s",
                        fingerprint['canvasHash'], fingerprint['webglVendor'][:30],
                        fingerprint['webdriver'])

            # 4. Проверяем совпадение timezone
            logger.info("[4/4] Validating timezone match...")
            tz_match = fingerprint['timezone'] == expected_tz
            logger.info("TZ match: %s (expected=%s, detected=%s)",
                        tz_match, expected_tz, fingerprint['timezone'])

            proxy_parts = proxy.split(":")
            proxy_ip = proxy_parts[1] if len(proxy_parts) >= 3 else "unknown"

            result = SecurityCheckResult(
                timestamp=datetime.now().isoformat(),
                proxy_ip=proxy_ip,
                detected_ip=detected_ip,
                webrtc_leak=webrtc_info.get('hasLeak', False),
                webrtc_local_ip=(webrtc_info.get('localIPs') or [None])[0],
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
                logger.info("Results saved to: %s", result_path)

            return result

    finally:
        if proxy_relay:
            await proxy_relay.stop()


def print_summary(result: SecurityCheckResult):
    """Выводит итоговый отчёт."""
    lines = [
        "",
        "=" * 60,
        "SECURITY CHECK SUMMARY",
        "=" * 60,
        "",
        f"Status: {'✓ SAFE' if result.is_safe else '✗ UNSAFE'}",
        "",
        f"{'Check':<25} {'Result':<15} {'Details'}",
        "-" * 60,
    ]

    # IP check
    ip_ok = result.proxy_ip in result.detected_ip or result.detected_ip != "unknown"
    lines.append(f"{'IP Match':<25} {'✓ OK' if ip_ok else '✗ FAIL':<15} {result.detected_ip}")

    # WebRTC check
    webrtc_ok = not result.webrtc_leak
    lines.append(f"{'WebRTC Leak':<25} {'✓ Blocked' if webrtc_ok else '✗ LEAK!':<15} {result.webrtc_local_ip or 'None'}")

    # Timezone check
    tz_ok = result.timezone_match
    lines.append(f"{'Timezone Match':<25} {'✓ OK' if tz_ok else '✗ MISMATCH':<15} {result.detected_timezone}")

    # Fingerprint info
    lines.extend([
        "",
        f"{'Canvas Hash':<25} {result.canvas_hash}",
        f"{'Screen':<25} {result.screen_resolution}",
        f"{'Platform':<25} {result.platform}",
        "=" * 60,
    ])

    if not result.is_safe:
        lines.append("")
        lines.append("WARNING: Security issues detected!")
        lines.append("   Do NOT proceed with account login until fixed.")
        if result.webrtc_leak:
            lines.append("   -> WebRTC is leaking your real IP")
        if not result.timezone_match:
            lines.append("   -> Timezone doesn't match proxy location")

    summary = "\n".join(lines)
    logger.info("Security check result:\n%s", summary)


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
        sys.exit(0 if result.is_safe else 1)

    except Exception as e:
        logger.error("Security check failed: %s", e, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
