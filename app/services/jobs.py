from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("cfsmcp.jobs")

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="cfsmcp")


def submit(fn, *args, **kwargs):
    fut = _executor.submit(fn, *args, **kwargs)

    def _done(f):
        try:
            f.result()
        except Exception:
            log.exception("background job failed")

    fut.add_done_callback(_done)
    return fut
