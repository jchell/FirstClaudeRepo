"""Stream worker process: long-lived consumer groups for near-real-time flows.

Phase 0 runs the process skeleton and a heartbeat on the ``dataplat.control``
topic; Phase 1b registers CDC/event stream handlers here.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
from typing import Any

from dataplat.core.context import PlatformContext
from dataplat.core.logging import configure_logging

log = logging.getLogger(__name__)

CONTROL_TOPIC = "dataplat.control"


class StreamWorker:
    def __init__(self, ctx: PlatformContext) -> None:
        self.ctx = ctx
        self.worker_id = socket.gethostname()
        self._stop = threading.Event()

    def stop(self, *_: Any) -> None:
        self._stop.set()

    def run_forever(self) -> None:
        bus = self.ctx.events
        bus.ensure_topic(CONTROL_TOPIC)
        last_beat = 0.0
        for batch in bus.consume([CONTROL_TOPIC], group="dataplat-stream-worker", timeout=1.0):
            for event in batch:
                log.info("control event: %s", event.value.get("type"))
            if batch:
                bus.commit()
            if time.monotonic() - last_beat > 30:
                bus.publish(CONTROL_TOPIC, {"type": "heartbeat", "worker": self.worker_id, "ts": time.time()})
                last_beat = time.monotonic()
            if self._stop.is_set():
                break
        bus.flush()


def main() -> None:
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    ctx = PlatformContext()
    worker = StreamWorker(ctx)
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)
    log.info("stream worker %s started", worker.worker_id)
    worker.run_forever()
    ctx.close()
