"""
cluster/node.py — Local Node State Machine

Tracks the authoritative state for *this* node and exposes a thread-safe
view to all other sub-systems.

State transitions::

    UNKNOWN ──► STANDBY ──► PRIMARY
       │           │            │
       └───────────┴────────────┘
                   ▼
                FENCED

The state machine does not trigger transitions itself; that is the
responsibility of the failover/switchover modules.  Those modules
call set_role() / set_health() / set_pg_state() / set_disk_state().
"""

import logging
import threading
import time
from typing import Optional

from pgha.config import Config
from pgha.models import (
    DiskState,
    NodeHealth,
    NodeRole,
    NodeState,
    PgState,
)

log = logging.getLogger(__name__)


class LocalNode:
    """
    Thread-safe container for this node's mutable runtime state.

    All daemon sub-systems read node state through an instance of this
    class; only the daemon's main loop writes to it.
    """

    def __init__(self, cfg: Config) -> None:
        self._cfg   = cfg
        self._lock  = threading.Lock()
        self._state = NodeState(
            name        = cfg.cluster.node_name,
            role        = NodeRole.UNKNOWN,
            health      = NodeHealth.UNKNOWN,
            pg_state    = PgState.UNKNOWN,
            disk_state  = DiskState.UNKNOWN,
            last_heartbeat_ts = time.time(),
        )

    # ------------------------------------------------------------------
    # Setters (always acquire lock)
    # ------------------------------------------------------------------

    def set_role(self, role: NodeRole) -> None:
        with self._lock:
            old = self._state.role
            self._state.role = role
        if old != role:
            log.info("Node role changed: %s → %s", old, role)

    def set_health(self, health: NodeHealth) -> None:
        with self._lock:
            old = self._state.health
            self._state.health = health
        if old != health:
            log.info("Node health changed: %s → %s", old, health)

    def set_pg_state(self, pg_state: PgState) -> None:
        with self._lock:
            old = self._state.pg_state
            self._state.pg_state = pg_state
        if old != pg_state:
            log.info("PG state changed: %s → %s", old, pg_state)

    def set_disk_state(self, disk_state: DiskState) -> None:
        with self._lock:
            old = self._state.disk_state
            self._state.disk_state = disk_state
        if old != disk_state:
            log.info("Disk state changed: %s → %s", old, disk_state)

    def touch_heartbeat(self) -> None:
        """Update the heartbeat timestamp to now (called by sender loop)."""
        with self._lock:
            self._state.last_heartbeat_ts = time.time()

    # ------------------------------------------------------------------
    # Getters
    # ------------------------------------------------------------------

    def snapshot(self) -> NodeState:
        """Return a shallow copy of the current state (thread-safe)."""
        import copy
        with self._lock:
            return copy.copy(self._state)

    @property
    def role(self) -> NodeRole:
        with self._lock:
            return self._state.role

    @property
    def health(self) -> NodeHealth:
        with self._lock:
            return self._state.health

    @property
    def pg_state(self) -> PgState:
        with self._lock:
            return self._state.pg_state

    @property
    def disk_state(self) -> DiskState:
        with self._lock:
            return self._state.disk_state

    @property
    def name(self) -> str:
        return self._cfg.cluster.node_name

    def is_primary(self) -> bool:
        return self.role == NodeRole.PRIMARY

    def is_standby(self) -> bool:
        return self.role == NodeRole.STANDBY
