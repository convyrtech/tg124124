#!/usr/bin/env python3
"""
pproxy wrapper для Python 3.14+

Исправляет проблему с asyncio.get_event_loop() в pproxy.
"""

import asyncio


def main():
    """Запускает pproxy с правильным event loop."""
    # Создаём event loop ДО импорта pproxy
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Теперь импортируем и запускаем pproxy
    from pproxy.server import main as pproxy_main

    pproxy_main()


if __name__ == "__main__":
    main()
