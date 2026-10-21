"""Überwachung im Betrieb: Gesundheitsprüfung, Statistik im Log, Sensorwerte für HA."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from . import diagnose

if TYPE_CHECKING:
    from .server import ApiServer

log = logging.getLogger(__name__)

SENSOR_INTERVAL = 30
HINT_REPEAT = 600  # Hinweise höchstens alle 10 Minuten wiederholen


class Supervisor:
    """Prüft regelmäßig Scanner und BlueZ. Bleibt ein Problem bestehen, beendet sich der Prozess,
    damit systemd (Restart=on-failure) ihn sauber neu startet."""

    def __init__(self, server: "ApiServer") -> None:
        self.server = server
        self.failures = 0
        self._tasks: list[asyncio.Task] = []
        self._last_stats = server.stats.snapshot()
        self._hinted: dict[str, float] = {}

    def start(self) -> None:
        cfg = self.server.config
        loop = asyncio.get_running_loop()
        if cfg.watchdog_interval > 0:
            self._tasks.append(loop.create_task(self._every(cfg.watchdog_interval, self.check)))
        if cfg.stats_interval > 0:
            self._tasks.append(loop.create_task(self._every(cfg.stats_interval, self.log_stats)))
        self._tasks.append(loop.create_task(self._every(SENSOR_INTERVAL, self.push_sensors)))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.server.config.stats_interval > 0:
            self.log_stats()

    @staticmethod
    async def _every(seconds: float, action) -> None:
        while True:
            await asyncio.sleep(seconds)
            try:
                await action()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Überwachung: Fehler in %s", action.__name__)

    async def find_problem(self) -> str | None:
        srv = self.server
        for source in (srv.scanner, srv.gatt):
            if source is None:
                continue
            try:
                problem = await source.check()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # eine kaputte Prüfung ist selbst eine Störung
                problem = f"Prüfung fehlgeschlagen: {exc!r}"
            if problem:
                return problem
        silence = srv.stats.seconds_since_advertisement()
        timeout = srv.config.silence_timeout
        if timeout and silence > timeout:
            # Alles meldet "ok", aber es kommt nichts mehr an: Scan neu anstoßen
            if srv.gatt is not None:
                try:
                    await srv.gatt.start_discovery()
                except Exception as exc:  # GattError u. a.; das Problem bleibt ohnehin bestehen
                    log.warning("Scan-Neustart fehlgeschlagen: %s", exc)
            return f"seit {silence:.0f} s keine Advertisements empfangen"
        return None

    async def check(self) -> None:
        srv = self.server
        problem = await self.find_problem()
        if problem is None:
            if self.failures:
                log.info("Überwachung: wieder in Ordnung")
            self.failures = 0
        else:
            self.failures += 1
            log.warning("Überwachung: %s (%d/%d)", problem, self.failures, srv.config.watchdog_failures)
            self.log_hints()
            if self.failures >= srv.config.watchdog_failures:
                srv.fail(f"Bluetooth dauerhaft gestört: {problem}")
        srv.set_scanner_problem(problem)
        if diagnose.undervoltage_hint():
            self.log_hints()  # Unterspannung ist auch ohne erkanntes Problem eine Warnung wert

    def log_hints(self) -> None:
        now = time.monotonic()
        for hint in diagnose.hints():
            if now - self._hinted.get(hint, -HINT_REPEAT) >= HINT_REPEAT:
                self._hinted[hint] = now
                log.warning("Hinweis: %s", hint)

    def log_stats(self) -> None:
        now = self.server.stats.snapshot()
        delta = {k: now[k] - self._last_stats[k] for k in now}
        self._last_stats = now
        log.info("Statistik: %d Advertisements empfangen, %d gesendet, %d verworfen; %d Verbindungen "
                 "(%d fehlgeschlagen), %d Notifications", delta["received"], delta["sent"], delta["dropped"],
                 delta["connects"], delta["connect_failures"], delta["notifications"])

    async def push_sensors(self) -> None:
        for session in list(self.server.sessions):
            session.send_states()
