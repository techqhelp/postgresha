"""
ha/switchover.py — Planned Switchover Engine

A switchover is an *ordered*, zero-data-loss hand-off of the PRIMARY role
from the current primary to the standby node.  It is initiated manually
via `pgha-ctl switchover` and requires both nodes to be healthy and
communicating.

Switchover sequence (runs on the PRIMARY node)
----------------------------------------------
Step 1  PRE_CHECK   — Verify peer is alive and healthy before starting.
Step 2  PG_STOP     — Stop PostgreSQL cleanly ("fast" shutdown).
Step 3  UMOUNT      — Unmount the RPD filesystem.
Step 4  DETACH      — Gracefully detach the RPD from this node.
Step 5  VIP_RELEASE — Remove the alias IP (VIP) from this node.
Step 6  SIGNAL_PEER — Send a SWITCHOVER_REQUEST command to the standby
                       over the management TCP socket so it can start
                       attaching the disk immediately.
Step 7  DEMOTE      — Update local state to STANDBY.

The peer standby receives the SWITCHOVER_REQUEST and runs the same
attach/mount/PG_start/VIP steps as failover (but without fencing).

The calling code (daemon / API handler) is responsible for verifying
that the peer completed the switchover successfully.
"""

import json
import logging
import queue
import socket
import time

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

# TCP command sent to peer to trigger it to acquire resources
_CMD_SWITCHOVER_REQUEST = "SWITCHOVER_REQUEST"
_CMD_SWITCHOVER_ACCEPT  = "SWITCHOVER_ACCEPT"
_CMD_SWITCHOVER_DONE    = "SWITCHOVER_DONE"

# Port used for management TCP commands (separate from heartbeat UDP)
_MGMT_PORT = 7778
_MGMT_TIMEOUT_SECS = 30


class SwitchoverEngine:
    """
    Runs an ordered switchover from PRIMARY to STANDBY.
    Instantiated and called on both nodes:
      - On PRIMARY: execute_as_primary()
      - On STANDBY: execute_as_standby()  (called when TCP command arrives)
    """

    def __init__(
        self,
        cfg: Config,
        local_node: LocalNode,
        disk_mgr: DiskManager,
        net_mgr: NetworkManager,
        event_queue: queue.Queue,
    ) -> None:
        self._cfg      = cfg
        self._local    = local_node
        self._disk_mgr = disk_mgr
        self._net_mgr  = net_mgr
        self._event_q  = event_queue

    # ------------------------------------------------------------------
    # Primary side
    # ------------------------------------------------------------------

    def execute_as_primary(self) -> bool:
        """
        Demote this PRIMARY orderly and signal the peer to take over.
        Returns True on success, False on failure.
        """
        if not self._local.is_primary():
            log.warning("switchover_as_primary called on non-primary node")
            return False

        log.info("=== SWITCHOVER STARTING — demoting %s ===",
                 self._cfg.cluster.node_name)
        self._emit(HaEventType.SWITCHOVER_STARTED,
                   "Planned switchover initiated on primary")

        steps = [
            ("PRE_CHECK",   self._step_pre_check),
            ("VIP_RELEASE", self._step_release_vip),   # IP first — clients stop connecting before disk moves
            ("PG_STOP",     self._step_stop_postgres),
            ("UMOUNT",      self._step_umount),
            ("DETACH",      self._step_detach_disk),
            ("SIGNAL_PEER", self._step_signal_peer),
            ("DEMOTE",      self._step_demote),
        ]

        for step_name, step_fn in steps:
            log.info("Switchover step: %s", step_name)
            try:
                step_fn()
            except Exception as exc:
                log.error("Switchover failed at %s: %s", step_name, exc,
                          exc_info=True)
                self._emit(HaEventType.SWITCHOVER_FAILED,
                           f"Failed at {step_name}: {exc}")
                return False

        log.info("=== SWITCHOVER COMPLETED — this node is now STANDBY ===")
        self._emit(HaEventType.SWITCHOVER_COMPLETED,
                   f"{self._cfg.cluster.node_name} demoted to STANDBY")
        return True

    # ------------------------------------------------------------------
    # Standby side
    # ------------------------------------------------------------------

    def execute_as_standby(self) -> bool:
        """
        Promote this STANDBY to PRIMARY after receiving a switchover signal.
        Returns True on success, False on failure.
        """
        if not self._local.is_standby():
            log.warning("execute_as_standby called on non-standby node")
            return False

        log.info("=== SWITCHOVER PROMOTION — %s becoming PRIMARY ===",
                 self._cfg.cluster.node_name)

        steps = [
            ("ATTACH",   self._step_attach_disk),
            ("MOUNT",    self._step_mount_disk),
            ("PG_START", self._step_start_postgres),
            ("VIP",      self._step_acquire_vip),
            ("PROMOTE",  self._step_promote),
        ]

        for step_name, step_fn in steps:
            log.info("Switchover promotion step: %s", step_name)
            try:
                step_fn()
            except Exception as exc:
                log.error("Switchover promotion failed at %s: %s",
                          step_name, exc, exc_info=True)
                self._emit(HaEventType.SWITCHOVER_FAILED,
                           f"Promotion failed at {step_name}: {exc}")
                return False

        log.info("=== SWITCHOVER PROMOTION COMPLETE — this node is PRIMARY ===")
        self._emit(HaEventType.SWITCHOVER_COMPLETED,
                   "{} promoted to PRIMARY".format(self._cfg.cluster.node_name))
        return True

    # ------------------------------------------------------------------
    # Primary demote steps
    # ------------------------------------------------------------------

    def _step_pre_check(self) -> None:
        """Verify peer is reachable via TCP before starting the hand-off."""
        peer_ip   = self._cfg.cluster.peer_ip
        deadline  = time.monotonic() + _MGMT_TIMEOUT_SECS
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(
                        (peer_ip, _MGMT_PORT), timeout=3):
                    log.info("Pre-check: peer %s is reachable", peer_ip)
                    return
            except OSError:
                time.sleep(2)
        raise RuntimeError(
            f"Pre-check failed: peer {peer_ip}:{_MGMT_PORT} not reachable")

    def _step_stop_postgres(self) -> None:
        self._disk_mgr.stop_postgres(mode="fast")
        self._local.set_pg_state(PgState.STOPPED)

    def _step_umount(self) -> None:
        self._disk_mgr.unmount_disk()

    def _step_detach_disk(self) -> None:
        self._disk_mgr.detach_disk()
        self._local.set_disk_state(DiskState.DETACHED)
        self._emit(HaEventType.DISK_DETACHED,
                   f"RPD detached from {self._cfg.cluster.node_name}")

    def _step_release_vip(self) -> None:
        self._net_mgr.release_vip()
        self._emit(HaEventType.VIP_RELEASED,
                   f"VIP released from {self._cfg.cluster.node_name}")

    def _step_signal_peer(self) -> None:
        """Send SWITCHOVER_REQUEST to the standby node over TCP."""
        peer_ip  = self._cfg.cluster.peer_ip
        msg      = json.dumps({"cmd": _CMD_SWITCHOVER_REQUEST}).encode()
        log.info("Sending %s to peer %s:%d",
                 _CMD_SWITCHOVER_REQUEST, peer_ip, _MGMT_PORT)
        with socket.create_connection(
                (peer_ip, _MGMT_PORT), timeout=_MGMT_TIMEOUT_SECS) as sock:
            sock.sendall(msg + b"\n")
            # Wait for acknowledgement
            resp_raw = sock.recv(256)
            resp     = json.loads(resp_raw.decode())
            if resp.get("cmd") != _CMD_SWITCHOVER_ACCEPT:
                raise RuntimeError(
                    f"Unexpected peer response: {resp}")
        log.info("Peer acknowledged switchover request")

    def _step_demote(self) -> None:
        self._local.set_role(NodeRole.STANDBY)
        self._local.set_health(NodeHealth.HEALTHY)
        self._emit(HaEventType.ROLE_CHANGED,
                   f"{self._cfg.cluster.node_name} demoted to STANDBY")

    # ------------------------------------------------------------------
    # Standby promote steps
    # ------------------------------------------------------------------

    def _step_attach_disk(self) -> None:
        self._disk_mgr.attach_disk_rw()
        self._local.set_disk_state(DiskState.ATTACHED_RW)

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
