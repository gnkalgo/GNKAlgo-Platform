import time
from collections import defaultdict, deque
from threading import Lock
from fastapi import HTTPException, Request

class RateLimiter:
    def __init__(self):
        self.events = defaultdict(deque)
        self.lock = Lock()

    def check(self, key: str, limit: int, window: int) -> None:
        now = time.monotonic()
        with self.lock:
            bucket = self.events[key]
            while bucket and bucket[0] <= now - window: bucket.popleft()
            if len(bucket) >= limit: raise HTTPException(status_code=429, detail="Too many requests")
            bucket.append(now)

limiter = RateLimiter()

def limit_login(request: Request) -> None:
    host = request.client.host if request.client else "unknown"
    limiter.check(f"login:{host}", 10, 60)

def limit_sensitive(request: Request) -> None:
    host = request.client.host if request.client else "unknown"
    limiter.check(f"sensitive:{host}", 20, 60)
