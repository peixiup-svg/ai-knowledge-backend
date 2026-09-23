import math
import threading
import time

import redis
from fastapi import HTTPException

from app.config import get_settings


class RateLimiter:
    """One-minute fixed windows; Redis INCR/EXPIRE is atomic across API processes."""

    _script = """
    local n = redis.call('INCR', KEYS[1])
    if n == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
    return n
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._windows: dict[str, tuple[int, int]] = {}
        self._client = None

    def reset(self):
        with self._lock:
            self._windows.clear()

    def check(self, key: str, limit: int) -> None:
        now = time.time()
        window = int(now // 60)
        if get_settings().rate_limit_backend == "redis":
            if self._client is None:
                self._client = redis.Redis.from_url(
                    get_settings().redis_url, socket_timeout=2, socket_connect_timeout=2
                )
            try:
                count = int(self._client.eval(self._script, 1, f"ratelimit:{key}:{window}", 120))
            except redis.RedisError:
                raise HTTPException(503, "限流服务暂不可用，请稍后重试。") from None
        else:
            with self._lock:
                previous_window, count = self._windows.get(key, (window, 0))
                count = count + 1 if previous_window == window else 1
                if len(self._windows) > 10000:
                    self._windows = {k: v for k, v in self._windows.items() if v[0] == window}
                self._windows[key] = (window, count)
        if count > limit:
            raise HTTPException(
                429,
                "请求过于频繁，请稍后重试。",
                headers={"Retry-After": str(max(1, math.ceil(60 - now % 60)))},
            )


limiter = RateLimiter()
