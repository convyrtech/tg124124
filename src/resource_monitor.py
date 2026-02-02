"""
Resource Monitor for Parallel Migration.

Monitors system resources to prevent overload when running
many browser instances in parallel.
"""
import psutil
from dataclasses import dataclass
from typing import Dict


@dataclass
class ResourceLimits:
    """Resource usage limits."""
    max_memory_percent: float = 80.0  # Stop launching if memory > 80%
    max_cpu_percent: float = 90.0      # Stop launching if CPU > 90%
    min_memory_available_gb: float = 2.0  # Need at least 2GB free
    memory_per_browser_gb: float = 0.5    # Estimate per Camoufox instance


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
        self,
        max_memory_percent: float = 80.0,
        max_cpu_percent: float = 90.0,
        min_memory_available_gb: float = 2.0
    ):
        self.limits = ResourceLimits(
            max_memory_percent=max_memory_percent,
            max_cpu_percent=max_cpu_percent,
            min_memory_available_gb=min_memory_available_gb
        )

    def _get_resources(self) -> Dict[str, float]:
        """Get current resource usage (internal, mockable for tests)."""
        return self.get_current()

    def get_current(self) -> Dict[str, float]:
        """Get current system resource usage."""
        memory = psutil.virtual_memory()
        cpu = psutil.cpu_percent(interval=0.1)

        return {
            'cpu_percent': cpu,
            'memory_percent': memory.percent,
            'memory_available_gb': memory.available / (1024 ** 3),
            'memory_total_gb': memory.total / (1024 ** 3),
        }

    def can_launch_more(self) -> bool:
        """Check if system can handle more browser instances."""
        resources = self._get_resources()

        if resources['memory_percent'] > self.limits.max_memory_percent:
            return False
        if resources['cpu_percent'] > self.limits.max_cpu_percent:
            return False
        if resources['memory_available_gb'] < self.limits.min_memory_available_gb:
            return False

        return True

    def recommended_concurrency(self) -> int:
        """
        Recommend number of concurrent browsers based on available resources.

        Returns a conservative estimate based on available memory.
        """
        resources = self._get_resources()
        available_gb = resources['memory_available_gb']

        # Reserve 2GB for system, rest for browsers
        usable_gb = max(0, available_gb - 2.0)
        recommended = int(usable_gb / self.limits.memory_per_browser_gb)

        # Clamp to reasonable range
        return max(1, min(recommended, 50))

    def format_status(self) -> str:
        """Format current status for display."""
        r = self._get_resources()
        return (
            f"CPU: {r['cpu_percent']:.1f}% | "
            f"Memory: {r['memory_percent']:.1f}% | "
            f"Available: {r['memory_available_gb']:.1f}GB"
        )
