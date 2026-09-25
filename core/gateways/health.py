"""Health and telemetry tracker for multi-gateway download architecture.

Tracks success/failure metrics, latency, fallback trigger counts, and gateway availability
without leaking sensitive user information (like cookies or tokens).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass
class GatewayStats:
    name: str
    enabled: bool = True
    available: bool = True
    version: str = ""
    success_count: int = 0
    failure_count: int = 0
    fallback_triggered_count: int = 0
    total_latency_ms: float = 0.0
    last_status: str = "idle"  # "idle" | "healthy" | "degraded" | "error"
    last_error_msg: str = ""
    last_error_time: float | None = None
    last_success_time: float | None = None

    @property
    def total_requests(self) -> int:
        return self.success_count + self.failure_count

    @property
    def avg_latency_ms(self) -> float:
        if self.total_requests == 0:
            return 0.0
        return round(self.total_latency_ms / self.total_requests, 1)

    @property
    def success_rate(self) -> float:
        if self.total_requests == 0:
            return 100.0
        return round((self.success_count / self.total_requests) * 100.0, 1)

    def record_success(self, latency_ms: float) -> None:
        self.success_count += 1
        self.total_latency_ms += latency_ms
        self.last_status = "healthy"
        self.last_success_time = time.time()

    def record_failure(self, error_msg: str, latency_ms: float = 0.0) -> None:
        self.failure_count += 1
        self.total_latency_ms += latency_ms
        self.last_status = "degraded" if self.success_count > 0 else "error"
        # Sanitize internal error messages to prevent leakage of cookies/paths
        sanitized = self._sanitize_error(error_msg)
        self.last_error_msg = sanitized
        self.last_error_time = time.time()

    def record_fallback_triggered(self) -> None:
        self.fallback_triggered_count += 1

    @staticmethod
    def _sanitize_error(msg: str) -> str:
        if not msg:
            return "Unknown error"
        # Truncate and strip sensitive tokens/cookies
        cleaned = msg.split("\n")[0][:150]
        for token in ("cookie", "sessionid", "bearer", "token", "password"):
            if token in cleaned.lower():
                return "Authentication or network error"
        return cleaned

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "available": self.available,
            "version": self.version,
            "total_requests": self.total_requests,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "success_rate_percent": self.success_rate,
            "avg_latency_ms": self.avg_latency_ms,
            "fallback_triggered_count": self.fallback_triggered_count,
            "last_status": self.last_status,
            "last_error_msg": self.last_error_msg,
            "last_error_time": self.last_error_time,
            "last_success_time": self.last_success_time,
        }


class GatewayHealthTracker:
    def __init__(self):
        self.start_time = time.time()
        self.gateway1 = GatewayStats(name="yt-dlp", version="latest")
        self.gateway2 = GatewayStats(name="tiktok_downloader", version="v5.8")

    def get_health_status(self) -> Dict[str, Any]:
        now = time.time()
        return {
            "ok": True,
            "uptime_seconds": int(now - self.start_time),
            "timestamp": now,
            "gateways": {
                "primary": self.gateway1.to_dict(),
                "tiktok_fallback": self.gateway2.to_dict(),
            },
        }

    def reset_metrics(self) -> None:
        self.gateway1.success_count = 0
        self.gateway1.failure_count = 0
        self.gateway1.total_latency_ms = 0.0
        self.gateway2.success_count = 0
        self.gateway2.failure_count = 0
        self.gateway2.fallback_triggered_count = 0
        self.gateway2.total_latency_ms = 0.0


# Global singleton instance
health_tracker = GatewayHealthTracker()
