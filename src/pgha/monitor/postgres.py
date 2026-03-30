"""
monitor/postgres.py — PostgreSQL Health Monitor

Checks PostgreSQL health by:
  1. pg_isready — confirms the server accepts connections (fast path)
  2. psycopg2 query — connects and runs diagnostic SQL to confirm R/W, WAL,
     replication state, and connection count.

The monitor runs in its own thread and writes results into a shared
PgHealthSnapshot that the daemon reads between check cycles.
"""

import logging
import subprocess
import threading
import time
from typing import Optional

import psycopg2
import psycopg2.extras

from pgha.config import Config
from pgha.models import PgHealthSnapshot, PgState

log = logging.getLogger(__name__)


_CHECK_SQL = """
SELECT
    pg_is_in_recovery()            AS in_recovery,
    (SELECT count(*) FROM pg_stat_activity
     WHERE state <> 'idle')        AS active_connections,
    (
      SELECT CASE
        WHEN pg_is_in_recovery() THEN
            COALESCE(
              pg_wal_lsn_diff(pg_last_wal_receive_lsn(),
                              pg_last_wal_replay_lsn()), 0)
        ELSE 0
      END
    )                              AS replication_lag_bytes;
"""


class PostgresMonitor:
    """
    Periodically probe PostgreSQL and expose the latest PgHealthSnapshot.

    Usage::

        mon = PostgresMonitor(cfg)
        mon.start()
        snap = mon.snapshot()   # thread-safe
        mon.stop()
    """

    def __init__(self, cfg: Config) -> None:
        self._cfg        = cfg
        self._lock       = threading.Lock()
        self._stop_evt   = threading.Event()
        self._snapshot: Optional[PgHealthSnapshot] = None
        self._fail_count = 0

        self._thread = threading.Thread(
            target=self._loop, name="pg-monitor", daemon=True)

    def start(self) -> None:
        self._thread.start()
        log.info("PostgreSQL monitor started (interval=%.1fs)",
                 self._cfg.monitor.check_interval)

    def stop(self) -> None:
        self._stop_evt.set()
        self._thread.join(timeout=10)
        log.info("PostgreSQL monitor stopped")

    def snapshot(self) -> Optional[PgHealthSnapshot]:
        """Return the most recent health snapshot (thread-safe)."""
        with self._lock:
            return self._snapshot

    def is_healthy(self) -> bool:
        snap = self.snapshot()
        return snap is not None and snap.ok

    # ------------------------------------------------------------------
    # Internal loop
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        interval = self._cfg.monitor.check_interval
        while not self._stop_evt.is_set():
            snap = self._check()
            with self._lock:
                self._snapshot = snap
            if not snap.ok:
                self._fail_count += 1
                log.warning("PostgreSQL check FAILED (%d/%d): %s",
                            self._fail_count,
                            self._cfg.monitor.pg_fail_threshold,
                            snap.message)
            else:
                if self._fail_count > 0:
                    log.info("PostgreSQL recovered after %d failed checks",
                             self._fail_count)
                self._fail_count = 0
            self._stop_evt.wait(timeout=interval)

    def _check(self) -> PgHealthSnapshot:
        """Run all checks and return a snapshot."""
        # --- Step 1: pg_isready ------------------------------------------
        if not self._pg_isready():
            return PgHealthSnapshot(
                ok=False,
                state=PgState.STOPPED,
                is_in_recovery=None,
                replication_lag_bytes=None,
                connections=None,
                message="pg_isready: server not accepting connections",
            )

        # --- Step 2: SQL probe -------------------------------------------
        try:
            result = self._sql_probe()
        except Exception as exc:  # noqa: BLE001
            log.debug("SQL probe exception: %s", exc)
            return PgHealthSnapshot(
                ok=False,
                state=PgState.CRASHED,
                is_in_recovery=None,
                replication_lag_bytes=None,
                connections=None,
                message=f"SQL probe failed: {exc}",
            )

        in_recovery, connections, lag_bytes = result
        state = PgState.RUNNING_STANDBY if in_recovery else PgState.RUNNING_PRIMARY
        return PgHealthSnapshot(
            ok=True,
            state=state,
            is_in_recovery=in_recovery,
            replication_lag_bytes=lag_bytes,
            connections=connections,
            message="ok",
        )

    def _pg_isready(self) -> bool:
        """Invoke pg_isready binary; returns True iff exit code is 0."""
        cfg = self._cfg.postgresql
        cmd = [
            cfg.pg_isready,
            "-h", cfg.host,
            "-p", str(cfg.port),
            "-U", cfg.user,
            "-d", cfg.database,
            "-t", str(int(self._cfg.monitor.pg_response_timeout)),
        ]
        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=self._cfg.monitor.pg_response_timeout + 2,
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            log.debug("pg_isready error: %s", exc)
            return False

    def _sql_probe(self):
        """Connect via psycopg2 and run the diagnostic SQL."""
        cfg = self._cfg.postgresql
        # Build DSN; use Unix socket if host starts with /
        if cfg.host.startswith("/"):
            dsn = (
                f"host={cfg.host} port={cfg.port} "
                f"user={cfg.user} dbname={cfg.database} "
                f"connect_timeout={int(self._cfg.monitor.pg_response_timeout)}"
            )
        else:
            dsn = (
                f"host={cfg.host} port={cfg.port} "
                f"user={cfg.user} dbname={cfg.database} "
                f"connect_timeout={int(self._cfg.monitor.pg_response_timeout)}"
            )

        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute(_CHECK_SQL)
                row = cur.fetchone()
                return (
                    bool(row["in_recovery"]),
                    int(row["active_connections"]),
                    int(row["replication_lag_bytes"]),
                )
        finally:
            conn.close()

    @property
    def consecutive_failures(self) -> int:
        return self._fail_count
