"""Run one bounded telemetry job outside the D-Bus/GLib main loop."""

import logging
import queue
import threading
import time

logger = logging.getLogger("dbus-evcharger")


class PollWorker:
    """Coalesce polls and deliver results only through the supplied dispatcher."""

    def __init__(self, collect, dispatch, close):
        self.collect = collect
        self.dispatch = dispatch
        self.close = close
        self._queue = queue.Queue(maxsize=1)
        self._pending = False  # owned by the main loop
        self._stopping = threading.Event()
        self._thread = threading.Thread(target=self._run, name="charger-poll", daemon=True)
        self._thread.start()

    def poll(self, callback):
        if self._pending or self._stopping.is_set():
            return False
        self._pending = True
        self._queue.put_nowait(callback)
        return True

    def _deliver(self, callback, snapshot, started):
        self._pending = False
        if not self._stopping.is_set():
            callback(snapshot, started)
        return False

    def _run(self):
        try:
            while not self._stopping.is_set():
                callback = self._queue.get()
                if callback is None or self._stopping.is_set():
                    break
                started = time.monotonic()
                try:
                    snapshot = self.collect()
                except Exception:
                    logger.exception("Telemetry poll failed")
                    snapshot = {"ok": False}
                self.dispatch(self._deliver, callback, snapshot, started)
        finally:
            self.close()

    def stop(self, timeout=1.0):
        self._stopping.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._thread.join(timeout)
        return not self._thread.is_alive()
