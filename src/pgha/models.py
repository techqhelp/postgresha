"""
models.py — Shared data models / enumerations for pgha.

All inter-module communication uses these types so that the rest of the
code stays loosely coupled to any single transport or storage format.
"""

import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional


# ---------------------------------------------------------------------------
# Node roles
# ---------------------------------------------------------------------------

class NodeRole(str, Enum):
    PRIMARY  = "primary"
    STANDBY  = "standby"
    UNKNOWN  = "unknown"


# ---------------------------------------------------------------------------
# Node & peer health
# ---------------------------------------------------------------------------

class NodeHealth(str, Enum):
    HEALTHY   = "healthy"    # all checks passing
    DEGRADED  = "degraded"   # some checks failing but still running
    FAILED    = "failed"     # node or PostgreSQL is down
    FENCED    = "fenced"     # node has been fenced (disk detached)
    UNKNOWN   = "unknown"


class PgState(str, Enum):
    RUNNING_PRIMARY  = "running_primary"   # accepting read-write
    RUNNING_STANDBY  = "running_standby"   # streaming replication standby
    STOPPED          = "stopped"
    STARTING         = "starting"
    STOPPING         = "stopping"
    CRASHED          = "crashed"
    UNKNOWN          = "unknown"


class DiskState(str, Enum):
    ATTACHED_RW  = "attached_rw"
    ATTACHED_RO  = "attached_ro"
    DETACHED     = "detached"
    UNKNOWN      = "unknown"


# ---------------------------------------------------------------------------
# Heartbeat message (serialised over UDP)
# ---------------------------------------------------------------------------

@dataclass
class HeartbeatMsg:
    """Sent every heartbeat_interval seconds to the peer."""
    node: str
    role: NodeRole
    seq: int
    pg_state: PgState
    disk_state: DiskState
    node_health: NodeHealth
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "node":         self.node,
            "role":         self.role.value,
            "seq":          self.seq,
            "pg_state":     self.pg_state.value,
            "disk_state":   self.disk_state.value,
            "node_health":  self.node_health.value,
            "ts":           self.ts,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "HeartbeatMsg":
        return cls(
            node        = d["node"],
            role        = NodeRole(d["role"]),
            seq         = int(d["seq"]),
            pg_state    = PgState(d["pg_state"]),
            disk_state  = DiskState(d["disk_state"]),
            node_health = NodeHealth(d["node_health"]),
            ts          = float(d["ts"]),
        )


# ---------------------------------------------------------------------------
# Monitoring snapshots
# ---------------------------------------------------------------------------

@dataclass
class PgHealthSnapshot:
    ok: bool
    state: PgState
    is_in_recovery: Optional[bool]  # True = standby, False = primary
    replication_lag_bytes: Optional[int]
    connections: Optional[int]
    message: str
    ts: float = field(default_factory=time.time)


@dataclass
class OsHealthSnapshot:
    cpu_pct: float
    mem_pct: float
    disk_pct: float
    load_avg_1m: float
    degraded: bool
    message: str
    ts: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# HA events
# ---------------------------------------------------------------------------

class HaEventType(str, Enum):
    PEER_DEAD            = "peer_dead"
    PEER_RECOVERED       = "peer_recovered"
    PG_FAILED            = "pg_failed"
    PG_RECOVERED         = "pg_recovered"
    FAILOVER_STARTED     = "failover_started"
    FAILOVER_COMPLETED   = "failover_completed"
    FAILOVER_FAILED      = "failover_failed"
    SWITCHOVER_STARTED   = "switchover_started"
    SWITCHOVER_COMPLETED = "switchover_completed"
    SWITCHOVER_FAILED    = "switchover_failed"
    DISK_ATTACHED        = "disk_attached"
    DISK_DETACHED        = "disk_detached"
    VIP_ACQUIRED         = "vip_acquired"
    VIP_RELEASED         = "vip_released"
    NODE_FENCED          = "node_fenced"
    ROLE_CHANGED         = "role_changed"


@dataclass
class HaEvent:
    event_type: HaEventType
    node: str
    message: str
    ts: float = field(default_factory=time.time)
    details: dict = field(default_factory=dict)

    def __str__(self) -> str:
        return f"[{self.event_type.value}] {self.node}: {self.message}"


# ---------------------------------------------------------------------------
# Cluster state (shared between all sub-systems via ClusterState object)
# ---------------------------------------------------------------------------

@dataclass
class NodeState:
    """Mutable state of a single node (self or peer)."""
    name: str
    role: NodeRole          = NodeRole.UNKNOWN
    health: NodeHealth      = NodeHealth.UNKNOWN
    pg_state: PgState       = PgState.UNKNOWN
    disk_state: DiskState   = DiskState.UNKNOWN
    last_heartbeat_ts: float = 0.0
    heartbeat_seq: int      = 0
    pg_snapshot: Optional[PgHealthSnapshot]  = None
    os_snapshot: Optional[OsHealthSnapshot]  = None
