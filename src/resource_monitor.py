"""
Resource Monitor for Parallel Migration.

Monitors system resources to prevent overload when running
many browser instances in parallel.
"""

import logging
from dataclasses import dataclass

import psutil

logger = logging.getLogger(__name__)


@dataclass
class ResourceLimits:
    """
    Resource usage limits for parallel migration.

    Attributes:
        max_memory_percent: Stop launching if memory usage exceeds this (0-100)
        max_cpu_percent: Stop launching if CPU usage exceeds this (0-100)
        min_memory_available_gb: Minimum free memory required in GB
        memory_per_browser_gb: Estimated memory per Camoufox instance in GB
    """

    max_memory_percent: float = 85.0
    max_cpu_percent: float = 95.0
    min_memory_available_gb: float = 0.5
    memory_per_browser_gb: float = 0.5


class ResourceMonitor:
    """
    Monitors system resources for parallel migration.

    Usage:
        monitor = ResourceMonitor()
        if monitor.can_launch_more():
            # Start another browser
        recommended = monitor.recommended_concurrency()
    """

    def __init__(
        self, max_memory_percent: float = 85.0, max_cpu_percent: float = 95.0, min_memory_available_gb: float = 0.5
    ) -> None:
        """
        Initialize resource monitor with limits.

        Args:
            max_memory_percent: Stop launching if memory > this percent (default 85)
            max_cpu_percent: Stop launching if CPU > this percent (default 95)
            min_memory_available_gb: Minimum free memory in GB (default 0.5)
        """
        self.limits = ResourceLimits(
            max_memory_percent=max_memory_percent,
            max_cpu_percent=max_cpu_percent,
            min_memory_available_gb=min_memory_available_gb,
        )
        # Warm up psutil CPU cache — first cpu_percent(interval=0) always
        # returns 0.0 because there's no previous snapshot to compare against.
        psutil.cpu_percent(interval=0)

    def _get_resources(self) -> dict[str, float]:
        """
        Get current resource usage (internal, mockable for tests).

        Returns:
            Dict with cpu_percent, memory_percent, memory_available_gb, memory_total_gb
        """
        return self.get_current()

    def get_current(self) -> dict[str, float]:
        """
        Get current system resource usage.

        Returns:
            Dict with keys: cpu_percent, memory_percent, memory_available_gb, memory_total_gb

        Note:
            Returns conservative defaults if psutil fails.
        """
        try:
            memory = psutil.virtual_memory()
            # interval=0 returns cached value since last call (non-blocking).
            # interval=0.1 blocks the event loop for 100ms — unacceptable
            # when called from async workers (5 workers × 100ms = 500ms blocked).
            cpu = psutil.cpu_percent(interval=0)

            return {
                "cpu_percent": cpu,
                "memory_percent": memory.percent,
                "memory_available_gb": memory.available / (1024**3),
                "memory_total_gb": memory.total / (1024**3),
            }
        except (OSError, AttributeError) as e:
            logger.warning("Could not read system resources: %s. Using conservative defaults.", e)
            # Return conservative defaults that will limit concurrency
            return {
                "cpu_percent": 50.0,
                "memory_percent": 50.0,
                "memory_available_gb": 4.0,
                "memory_total_gb": 8.0,
            }

    def can_launch_more(self) -> bool:
        """
        Check if system can handle more browser instances.

        Returns:
            True if resources are available, False if limits exceeded.
        """
        resources = self._get_resources()

        if resources["memory_percent"] > self.limits.max_memory_percent:
            logger.debug(
                "Memory limit reached: %.1f%% > %.1f%%", resources["memory_percent"], self.limits.max_memory_percent
            )
            return False
        if resources["cpu_percent"] > self.limits.max_cpu_percent:
            logger.debug("CPU limit reached: %.1f%% > %.1f%%", resources["cpu_percent"], self.limits.max_cpu_percent)
            return False
        if resources["memory_available_gb"] < self.limits.min_memory_available_gb:
            logger.debug(
                "Available memory too low: %.1fGB < %.1fGB",
                resources["memory_available_gb"],
                self.limits.min_memory_available_gb,
            )
            return False

        return True

    def recommended_concurrency(self) -> int:
        """
        Recommend number of concurrent browsers based on available resources.

        Returns:
            Recommended number of concurrent browsers (1-50).

        Note:
            Returns conservative estimate based on available memory,
            reserving 2GB for system operations.
        """
        resources = self._get_resources()
        available_gb = resources["memory_available_gb"]

        # Reserve 2GB for system, rest for browsers
        usable_gb = max(0, available_gb - 2.0)
        recommended = int(usable_gb / self.limits.memory_per_browser_gb)

        # Clamp to reasonable range
        return max(1, min(recommended, 50))

    def format_status(self) -> str:
        """
        Format current resource status for display.

        Returns:
            Human-readable string with CPU, memory percent, and available GB.
        """
        r = self._get_resources()
        return (
            f"CPU: {r['cpu_percent']:.1f}% | "
            f"Memory: {r['memory_percent']:.1f}% | "
            f"Available: {r['memory_available_gb']:.1f}GB"
        )
