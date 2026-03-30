"""
monitor/os_monitor.py — Operating System Resource Monitor

Tracks CPU, memory, and disk utilisation using psutil.
Runs in its own thread and exposes the latest OsHealthSnapshot.

Threshold violations are counted; only when the count exceeds
cfg.monitor.os_fail_count is the node marked as DEGRADED so that
transient spikes don't trigger false failovers.
"""

import logging
import threading
from typing import Optional

import psutil

from pgha.config import Config
from pgha.models import OsHealthSnapshot

log = logging.getLogger(__name__)


class OsMonitor:
    """
    Continuously polls OS resources and exposes OsHealthSnapshot.

    Usage::

        mon = OsMonitor(cfg)
        mon.start()
        snap = mon.snapshot()   # thread-safe
        mon.stop()
    """

    def __init__(self, cfg: Config) -> None:
        self._cfg        = cfg
        self._lock       = threading.Lock()
        self._stop_evt   = threading.Event()
        self._snapshot: Optional[OsHealthSnapshot] = None
        self._violation_count = 0

        self._thread = threading.Thread(
            target=self._loop, name="os-monitor", daemon=True)

    def start(self) -> None:
        self._thread.start()
        log.info("OS monitor started (interval=%.1fs, cpu_thr=%.0f%% "
                 "mem_thr=%.0f%% disk_thr=%.0f%%)",
                 self._cfg.monitor.check_interval,
                 self._cfg.monitor.os_cpu_threshold,
                 self._cfg.monitor.os_mem_threshold,
                 self._cfg.monitor.os_disk_threshold)

    def stop(self) -> None:
        self._stop_evt.set()
        self._thread.join(timeout=10)
        log.info("OS monitor stopped")

    def snapshot(self) -> Optional[OsHealthSnapshot]:
        with self._lock:
            return self._snapshot

    def is_degraded(self) -> bool:
        snap = self.snapshot()
        return snap is not None and snap.degraded

    # ------------------------------------------------------------------
    # Internal loop
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        interval = self._cfg.monitor.check_interval
        while not self._stop_evt.is_set():
            snap = self._collect()
            with self._lock:
                self._snapshot = snap
            if snap.degraded:
                log.warning("OS degraded: %s", snap.message)
            self._stop_evt.wait(timeout=interval)

    def _collect(self) -> OsHealthSnapshot:
        cfg = self._cfg.monitor

        cpu_pct  = psutil.cpu_percent(interval=None)
        mem      = psutil.virtual_memory()
        mem_pct  = mem.percent
        load1, _, _ = psutil.getloadavg()

        # Disk check on the RPD mount point (and root fallback)
        mount = self._cfg.gcp.disk_mount_point
        try:
            disk = psutil.disk_usage(mount)
            disk_pct = disk.percent
        except (OSError, FileNotFoundError):
            # Mount may not be present on standby
            try:
                disk = psutil.disk_usage("/")
                disk_pct = disk.percent
            except OSError:
                disk_pct = 0.0

        violations = []
        if cpu_pct  >= cfg.os_cpu_threshold:
            violations.append(f"CPU {cpu_pct:.1f}% >= {cfg.os_cpu_threshold}%")
        if mem_pct  >= cfg.os_mem_threshold:
            violations.append(f"MEM {mem_pct:.1f}% >= {cfg.os_mem_threshold}%")
        if disk_pct >= cfg.os_disk_threshold:
            violations.append(f"DISK {disk_pct:.1f}% >= {cfg.os_disk_threshold}%")

        if violations:
            self._violation_count += 1
        else:
            self._violation_count = 0

        degraded = self._violation_count >= cfg.os_fail_count
        message  = "; ".join(violations) if violations else "ok"

        return OsHealthSnapshot(
            cpu_pct    = cpu_pct,
            mem_pct    = mem_pct,
            disk_pct   = disk_pct,
            load_avg_1m= load1,
            degraded   = degraded,
            message    = message,
        )
