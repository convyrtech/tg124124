"""
Tests for resource_monitor module.
"""
import pytest
from src.resource_monitor import ResourceMonitor


class TestResourceMonitor:
    def test_get_system_resources(self):
        """Test basic resource reading."""
        monitor = ResourceMonitor()
        resources = monitor.get_current()

        assert 'cpu_percent' in resources
        assert 'memory_percent' in resources
        assert 'memory_available_gb' in resources
        assert 0 <= resources['cpu_percent'] <= 100
        assert 0 <= resources['memory_percent'] <= 100

    def test_can_launch_more(self):
        """Test launch decision logic."""
        monitor = ResourceMonitor(
            max_memory_percent=80,
            max_cpu_percent=90
        )

        # Mock low usage - should allow
        monitor._get_resources = lambda: {
            'cpu_percent': 50,
            'memory_percent': 60,
            'memory_available_gb': 4.0
        }
        assert monitor.can_launch_more() is True

        # Mock high memory - should block
        monitor._get_resources = lambda: {
            'cpu_percent': 50,
            'memory_percent': 85,
            'memory_available_gb': 1.0
        }
        assert monitor.can_launch_more() is False

    def test_can_launch_more_high_cpu(self):
        """Test CPU limit blocks launching."""
        monitor = ResourceMonitor(max_cpu_percent=90)

        monitor._get_resources = lambda: {
            'cpu_percent': 95,
            'memory_percent': 50,
            'memory_available_gb': 4.0
        }
        assert monitor.can_launch_more() is False

    def test_can_launch_more_low_memory(self):
        """Test low available memory blocks launching."""
        monitor = ResourceMonitor(min_memory_available_gb=2.0)

        monitor._get_resources = lambda: {
            'cpu_percent': 30,
            'memory_percent': 50,
            'memory_available_gb': 1.0
        }
        assert monitor.can_launch_more() is False

    def test_recommended_concurrency(self):
        """Test concurrency recommendation."""
        monitor = ResourceMonitor()
        # With 8GB available, should recommend ~8 browsers (1GB each estimate)
        monitor._get_resources = lambda: {
            'memory_available_gb': 8.0,
            'cpu_percent': 30,
            'memory_percent': 50
        }
        rec = monitor.recommended_concurrency()
        assert 4 <= rec <= 16  # Reasonable range

    def test_recommended_concurrency_low_memory(self):
        """Test recommendation with very low memory."""
        monitor = ResourceMonitor()
        monitor._get_resources = lambda: {
            'memory_available_gb': 2.5,
            'cpu_percent': 30,
            'memory_percent': 80
        }
        rec = monitor.recommended_concurrency()
        assert rec >= 1  # Should be at least 1

    def test_format_status(self):
        """Test status formatting."""
        monitor = ResourceMonitor()
        monitor._get_resources = lambda: {
            'cpu_percent': 50.5,
            'memory_percent': 60.3,
            'memory_available_gb': 4.2
        }
        status = monitor.format_status()
        assert 'CPU: 50.5%' in status
        assert 'Memory: 60.3%' in status
        assert 'Available: 4.2GB' in status
