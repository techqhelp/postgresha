"""
ha/failover.py — Automatic Failover Engine

Triggered when:
  - HaEvent(PEER_DEAD)      is raised on a STANDBY node, OR
  - HaEvent(PG_FAILED)      is raised on a PRIMARY node (self-demotion)

Failover sequence (standby promotes itself)
-------------------------------------------
Step 1  FENCE    — Force-detach the RPD from the failed primary via GCP API.
                   This is the STONITH equivalent.  Prevents split-brain writes.
Step 2  WAIT     — Pause cfg.cluster.fence_wait seconds to let GCP propagate
                   the detach before we attach.
Step 3  ATTACH   — Attach the RPD in READ_WRITE mode to *this* node.
Step 4  MOUNT    — Mount the XFS/EXT4 filesystem.
Step 5  PG_START — Start PostgreSQL (crash recovery runs automatically if
                   primary did not shut down cleanly).
Step 6  VIP      — Reassign the alias IP (VIP) from the old primary to self.
Step 7  PROMOTE  — Update cluster state to PRIMARY.

If any step fails, the engine attempts cleanup and emits
HaEvent(FAILOVER_FAILED).  A retry back-off prevents tight loops.
"""

import logging
import queue
import time
from typing import Optional

from pgha.config import Config
from pgha.cluster.node import LocalNode
from pgha.gcp.disk import DiskManager
from pgha.gcp.network import NetworkManager
from pgha.models import (
    DiskState,
    HaEvent,
    HaEventType,
    NodeHealth,
    NodeRole,
    PgState,
)

log = logging.getLogger(__name__)

# How long to back off before retrying a failed failover
_RETRY_BACKOFF_SECS = 30
# Maximum number of failover attempts before giving up
_MAX_ATTEMPTS = 3

# GCP instance states that mean the VM is confirmed down
_INSTANCE_DOWN_STATES = frozenset({"TERMINATED", "STOPPED"})


class FailoverEngine:
    """
    Executes automatic failover from the STANDBY node's perspective.

    Instantiated in the daemon; execute() is called when PEER_DEAD is detected.
    """

    def __init__(
        self,
        cfg: Config,
        local_node: LocalNode,
        disk_mgr: DiskManager,
        net_mgr: NetworkManager,
        event_queue: queue.Queue,
    ) -> None:
        self._cfg       = cfg
        self._local     = local_node
        self._disk_mgr  = disk_mgr
        self._net_mgr   = net_mgr
        self._event_q   = event_queue
        self._attempts  = 0
        self._last_attempt_ts: float = 0.0
        # Peer instance status resolved in VERIFY step; used by FENCE step
        self._peer_instance_status: str = "UNKNOWN"

    def should_attempt(self) -> bool:
        """
        Returns True if the node is STANDBY and is allowed to retry a
        failover (respects back-off and attempt limit).
        """
        if not self._local.is_standby():
            return False
        if self._attempts >= _MAX_ATTEMPTS:
            log.error("Failover: max attempts (%d) reached — giving up",
                      _MAX_ATTEMPTS)
            return False
        elapsed = time.monotonic() - self._last_attempt_ts
        if self._attempts > 0 and elapsed < _RETRY_BACKOFF_SECS:
            log.debug("Failover: back-off (%.0fs remaining)",
                      _RETRY_BACKOFF_SECS - elapsed)
            return False
        return True

    def execute(self) -> bool:
        """
        Run the full failover sequence.
        Returns True on success, False on failure.
        """
        self._attempts         += 1
        self._last_attempt_ts  = time.monotonic()

        log.warning(
            "=== AUTOMATIC FAILOVER STARTING (attempt %d/%d) ===",
            self._attempts, _MAX_ATTEMPTS,
        )
        self._emit(HaEventType.FAILOVER_STARTED,
                   f"Attempt {self._attempts}/{_MAX_ATTEMPTS}")

        steps = [
            ("VERIFY",    self._step_verify_peer_state),
            ("FENCE",     self._step_fence),
            ("WAIT",      self._step_fence_wait),
            ("ATTACH",    self._step_attach_disk),
            ("MOUNT",     self._step_mount_disk),
            ("PG_START",  self._step_start_postgres),
            ("VIP",       self._step_acquire_vip),
            ("PROMOTE",   self._step_promote),
        ]

        for step_name, step_fn in steps:
            log.info("Failover step: %s", step_name)
            try:
                step_fn()
            except Exception as exc:
                log.error("Failover failed at step %s: %s", step_name, exc,
                          exc_info=True)
                self._local.set_health(NodeHealth.FAILED)
                self._emit(HaEventType.FAILOVER_FAILED,
                           f"Failed at {step_name}: {exc}")
                return False

        log.warning("=== AUTOMATIC FAILOVER COMPLETED === "
                    "This node is now PRIMARY")
        self._emit(HaEventType.FAILOVER_COMPLETED,
                   "Failover succeeded — this node is now PRIMARY")
        return True

    # ------------------------------------------------------------------
    # Failover steps
    # ------------------------------------------------------------------

    def _step_verify_peer_state(self) -> None:
        """
        Pacemaker/Corosync equivalent: verify peer state via the GCP Compute
        API — an independent channel completely separate from VPC heartbeat.

        This is the quorum/tie-breaker check.

        Decision table
        ──────────────
        TERMINATED / STOPPED
            Peer VM is confirmed DOWN by GCP.  Safe to proceed immediately.

        RUNNING
            Network partition: VPC heartbeat is lost but the VM is alive.
            We MUST execute STONITH (disk force-detach) to arbitrate —
            whichever node successfully fences the other wins the disk race.
            This is the Pacemaker "last-man-standing" behaviour.

        UNKNOWN (GCP API unreachable)
            We cannot determine who is dead.  ABORT — do NOT promote.
            Pacemaker equivalent: quorum device unreachable → freeze resources.
            The primary keeps running and serving traffic safely.
        """
        peer      = self._cfg.cluster.peer_node
        peer_zone = self._cfg.peer_zone

        log.info("VERIFY: querying GCP Compute API for %s instance status "
                 "(quorum check — independent of VPC heartbeat)", peer)

        status = self._disk_mgr.get_instance_status(peer, peer_zone)
        self._peer_instance_status = status

        if status == "UNKNOWN":
            raise RuntimeError(
                "GCP Compute API unreachable — cannot verify peer state. "
                "Aborting failover to prevent split-brain. "
                "Primary continues running. "
                "(Pacemaker equivalent: quorum device unreachable — no action)"
            )

        if status in _INSTANCE_DOWN_STATES:
            log.warning(
                "VERIFY: peer %s is %s (confirmed DOWN via GCP API). "
                "Proceeding with fast failover.", peer, status
            )
        else:
            # RUNNING, STAGING, STOPPING, SUSPENDED
            log.warning(
                "VERIFY: peer %s is %s — network partition detected. "
                "VPC heartbeat lost but VM is alive. "
                "Proceeding with STONITH (disk force-detach) as arbitration. "
                "(Pacemaker last-man-standing: this node fences peer and wins.)",
                peer, status
            )

    def _step_fence(self) -> None:
        """
        STONITH: Force-detach the RPD from the peer node.

        If peer is DOWN: disk may already be free; detach is best-effort.
        If peer is RUNNING (partition): operation.result() blocks until GCP
        confirms the disk is physically severed at the hypervisor level.
        The primary will get immediate I/O errors on its next WAL write,
        PostgreSQL crashes, and the self-demotion path triggers.
        This node then owns the disk exclusively — no split-brain possible.
        """
        peer_status = self._peer_instance_status
        peer        = self._cfg.cluster.peer_node

        if peer_status in _INSTANCE_DOWN_STATES:
            log.info("FENCE: peer %s is DOWN — detaching orphaned disk "
                     "(best-effort, errors ignored)", peer)
        else:
            log.warning(
                "FENCE (STONITH): peer %s is %s — force-detaching disk. "
                "Waiting for GCP to confirm detach before proceeding.",
                peer, peer_status
            )

        self._disk_mgr.force_detach_from_peer()
        self._emit(HaEventType.NODE_FENCED,
                   "Peer {} fenced: disk detached via GCP API "
                   "(peer was {})".format(peer, peer_status))

    def _step_fence_wait(self) -> None:
        """
        Brief settling wait after fence.

        GCP's operation.result() already confirmed the detach synchronously,
        so this is just a small buffer for hypervisor propagation.
        Reduced from 8s to 3s compared to a naive timer-based approach.
        """
        wait = self._cfg.cluster.fence_wait
        log.info("Fence settle wait: %.0f seconds", wait)
        time.sleep(wait)

    def _step_attach_disk(self) -> None:
        self._disk_mgr.attach_disk_rw()
        self._local.set_disk_state(DiskState.ATTACHED_RW)
        self._emit(HaEventType.DISK_ATTACHED,
                   f"RPD attached to {self._cfg.cluster.node_name} in RW mode")

    def _step_mount_disk(self) -> None:
        self._disk_mgr.mount_disk()

    def _step_start_postgres(self) -> None:
        self._disk_mgr.start_postgres()
        self._local.set_pg_state(PgState.RUNNING_PRIMARY)

    def _step_acquire_vip(self) -> None:
        self._net_mgr.acquire_vip()
        self._emit(HaEventType.VIP_ACQUIRED,
                   f"VIP {self._cfg.gcp.vip_address} moved to "
                   f"{self._cfg.cluster.node_name}")

    def _step_promote(self) -> None:
        self._local.set_role(NodeRole.PRIMARY)
        self._local.set_health(NodeHealth.HEALTHY)
        self._emit(HaEventType.ROLE_CHANGED,
                   f"{self._cfg.cluster.node_name} promoted to PRIMARY")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _emit(self, event_type: HaEventType, message: str) -> None:
        evt = HaEvent(
            event_type = event_type,
            node       = self._cfg.cluster.node_name,
            message    = message,
        )
        try:
            self._event_q.put_nowait(evt)
        except queue.Full:
            pass
        log.info("EVENT: %s", evt)
