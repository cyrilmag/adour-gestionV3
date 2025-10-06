from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable, Optional

from app.settings import get_settings
from .etl import run_folder


class XmlWatcher:
    """Naive polling watcher that triggers ETL when XML files change."""

    def __init__(self, interval_seconds: float = 30.0, table: str = "interventions") -> None:
        self.interval = interval_seconds
        self.table = table
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._last_snapshot: Optional[dict[str, float]] = None

    @staticmethod
    def _snapshot(directory: Path) -> dict[str, float]:
        return {p.name: p.stat().st_mtime for p in directory.glob("*.xml")}

    def _loop(self) -> None:
        settings = get_settings()
        xml_dir = Path(settings.xml_dir)
        db_path = settings.sqlite_path()

        self._last_snapshot = self._snapshot(xml_dir)

        while not self._stop.is_set():
            time.sleep(self.interval)
            current = self._snapshot(xml_dir)
            if current != self._last_snapshot:
                run_folder(db_path, xml_dir, table=self.table)
                self._last_snapshot = current

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="xml-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if not self._thread:
            return
        self._stop.set()
        self._thread.join(timeout=self.interval * 2)
        self._thread = None


def start_xml_watcher(interval_seconds: float = 30.0, table: str = "interventions") -> XmlWatcher:
    watcher = XmlWatcher(interval_seconds=interval_seconds, table=table)
    watcher.start()
    return watcher
