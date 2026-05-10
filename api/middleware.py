from __future__ import annotations

import logging
import time

from fastapi import Request

logger = logging.getLogger("local-ai-agent.api")


async def request_logger(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    duration_ms = int((time.perf_counter() - start) * 1000)
    logger.info("%s %s -> %s (%dms)", request.method, request.url.path, response.status_code, duration_ms)
    return response
