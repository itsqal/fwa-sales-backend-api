"""A small fixed-window counter, used only to throttle ``POST /auth/login``.

AE codes are guessable by design (``AE-BENGKULU1``), so the login endpoint is the one
place a password-spraying attempt is cheap. The counter lives in this process, which is
correct for the single-instance Docker-behind-nginx deployment this project uses. If the
API is ever put behind a load balancer, move this to Redis — a per-pod counter would
multiply the effective limit by the pod count.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class _Window:
    started_at: float
    count: int


@dataclass
class FixedWindowRateLimiter:
    max_attempts: int
    window_seconds: int
    _windows: dict[str, _Window] = field(
        default_factory=lambda: defaultdict(lambda: _Window(0.0, 0))
    )

    def check(self, key: str) -> int | None:
        """Register an attempt. Returns ``None`` if allowed, else seconds to wait."""
        current = time.monotonic()
        window = self._windows[key]

        if current - window.started_at >= self.window_seconds:
            self._windows[key] = _Window(started_at=current, count=1)
            return None

        if window.count >= self.max_attempts:
            return max(1, int(self.window_seconds - (current - window.started_at)))

        window.count += 1
        return None

    def reset(self, key: str) -> None:
        """Clear the counter — called after a successful login."""
        self._windows.pop(key, None)
