"""Prometheus client for metric queries."""

from __future__ import annotations

from typing import Any, Optional

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)
settings = get_settings()


class PrometheusClient:
    def __init__(self, base_url: str = None):
        self.base_url = (base_url or settings.prometheus_url).rstrip("/")
        self.timeout = settings.prometheus_timeout

    async def query_instant(self, query: str) -> Optional[list[dict]]:
        """Execute an instant query."""
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(f"{self.base_url}/api/v1/query", params={"query": query})
                resp.raise_for_status()
                data = resp.json()
                if data.get("status") == "success":
                    return data.get("data", {}).get("result", [])
        except Exception as e:
            logger.error(f"Prometheus query failed: {e}")
        return None

    async def query_range(self, query: str, start: str, end: str, step: str = "60s") -> Optional[list[dict]]:
        """Execute a range query."""
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(
                    f"{self.base_url}/api/v1/query_range",
                    params={"query": query, "start": start, "end": end, "step": step},
                )
                resp.raise_for_status()
                data = resp.json()
                if data.get("status") == "success":
                    return data.get("data", {}).get("result", [])
        except Exception as e:
            logger.error(f"Prometheus range query failed: {e}")
        return None

    async def collect_host_snapshot(self, instance: str) -> dict[str, Any]:
        """Collect current host metrics snapshot."""
        metrics = {}
        queries = {
            "cpu_usage": f'100 - (avg by(instance) (rate(node_cpu_seconds_total{{instance="{instance}",mode="idle"}}[5m])) * 100)',
            "cpu_iowait": f'avg by(instance) (rate(node_cpu_seconds_total{{instance="{instance}",mode="iowait"}}[5m])) * 100',
            "cpu_steal": f'avg by(instance) (rate(node_cpu_seconds_total{{instance="{instance}",mode="steal"}}[5m])) * 100',
            "load1": f'node_load1{{instance="{instance}"}}',
            "load5": f'node_load5{{instance="{instance}"}}',
            "load15": f'node_load15{{instance="{instance}"}}',
            "memory_used_pct": f'(1 - node_memory_MemAvailable_bytes{{instance="{instance}"}} / node_memory_MemTotal_bytes{{instance="{instance}"}}) * 100',
            "memory_total": f'node_memory_MemTotal_bytes{{instance="{instance}"}}',
            "memory_available": f'node_memory_MemAvailable_bytes{{instance="{instance}"}}',
            "swap_used": f'node_memory_SwapTotal_bytes{{instance="{instance}"}} - node_memory_SwapFree_bytes{{instance="{instance}"}}',
        }

        for name, query in queries.items():
            result = await self.query_instant(query)
            if result and len(result) > 0:
                try:
                    metrics[name] = float(result[0]["value"][1])
                except (KeyError, IndexError, ValueError):
                    pass

        return metrics

    async def collect_disk_snapshot(self, instance: str) -> list[dict]:
        """Collect filesystem usage for all mountpoints."""
        query = f"""
            (1 - node_filesystem_avail_bytes{{instance="{instance}",fstype!~"tmpfs|overlay|devtmpfs"}}
            / node_filesystem_size_bytes{{instance="{instance}",fstype!~"tmpfs|overlay|devtmpfs"}}) * 100
        """
        result = await self.query_instant(query)
        if not result:
            return []
        disks = []
        for r in result:
            disks.append({
                "mountpoint": r.get("metric", {}).get("mountpoint", ""),
                "device": r.get("metric", {}).get("device", ""),
                "usage_pct": float(r["value"][1]) if r.get("value") else 0,
            })
        return disks

    async def collect_trends(self, instance: str, duration: str = "1h") -> dict[str, list]:
        """Collect trending data over time."""
        import time
        end = str(int(time.time()))
        start = str(int(time.time()) - 3600)

        trends = {}
        queries = {
            "cpu_trend": f'100 - (avg by(instance) (rate(node_cpu_seconds_total{{instance="{instance}",mode="idle"}}[5m])) * 100)',
            "memory_trend": f'(1 - node_memory_MemAvailable_bytes{{instance="{instance}"}} / node_memory_MemTotal_bytes{{instance="{instance}"}}) * 100',
        }

        for name, query in queries.items():
            result = await self.query_range(query, start, end, "120s")
            if result and len(result) > 0:
                trends[name] = result[0].get("values", [])

        return trends
