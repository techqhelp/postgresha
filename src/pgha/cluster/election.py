"""
cluster/election.py — Leader Election / Quorum Logic

In a 2-node cluster there is no true quorum (50% cannot be a majority).
We use the GCP Regional Persistent Disk as a tie-breaker:

  "The node that wins the race to attach the disk in RW mode becomes primary."

Election rules
--------------
1. Both nodes start: the one that successfully attaches the disk first wins.
2. Primary fails: standby detects PEER_DEAD, fences peer (force-detach disk),
   attaches disk, becomes primary.  This is automatic failover.
3. Split-brain (both nodes think peer is dead):
   - Both try to attach the disk in RW mode.
   - GCP ensures only one can succeed (the other gets an API error).
   - The winner becomes primary; loser enters STANDBY and retries.
4. Manual switchover: admin runs `pgha-ctl switchover`; this module
   coordinates an ordered hand-off (primary demotes itself first).

Startup election
----------------
When the daemon starts and the node has no established role yet, it calls
elect().  elect() checks who currently holds the disk:
  - If nobody holds it      → we try to grab it (become primary).
  - If we already hold it   → resume PRIMARY.
  - If peer holds it        → become STANDBY and start replication.
"""

import logging
import time

from pgha.config import Config
from pgha.cluster.node import LocalNode
from pgha.gcp.disk import DiskManager
from pgha.gcp.network import NetworkManager
from pgha.models import DiskState, NodeHealth, NodeRole, PgState

log = logging.getLogger(__name__)


class ElectionManager:
    """
    Determines and establishes the role of *this* node at startup and
    after cluster events.
    """

    def __init__(
        self,
        cfg: Config,
        local_node: LocalNode,
        disk_mgr: DiskManager,
        net_mgr: NetworkManager,
    ) -> None:
        self._cfg       = cfg
        self._local     = local_node
        self._disk_mgr  = disk_mgr
        self._net_mgr   = net_mgr

    # ------------------------------------------------------------------
    # Startup election
    # ------------------------------------------------------------------

    def elect(self) -> NodeRole:
        """
        Run startup election and configure the node accordingly.
        Returns the elected NodeRole.
        """
        log.info("Starting cluster election for node %s",
                 self._cfg.cluster.node_name)

        current_holder = self._disk_mgr.current_rw_holder()
        my_name        = self._cfg.cluster.node_name

        if current_holder == my_name:
            log.info("We already hold the disk in RW mode → resuming PRIMARY")
            return self._become_primary(attach_disk=False)

        if current_holder is not None:
            log.info("Peer %s holds the disk → becoming STANDBY", current_holder)
            return self._become_standby()

        # Nobody holds the disk.  Race to attach it.
        log.info("No current RW holder — competing to become PRIMARY")
        return self._compete_for_primary()

    # ------------------------------------------------------------------
    # Role transitions
    # ------------------------------------------------------------------

    def _become_primary(self, attach_disk: bool = True) -> NodeRole:
        """Attach disk (if needed), mount FS, start PG, acquire VIP."""
        my_name = self._cfg.cluster.node_name
        log.info("[ELECTION] Transitioning %s → PRIMARY", my_name)

        try:
            if attach_disk:
                self._disk_mgr.attach_disk_rw()

            self._disk_mgr.mount_disk()
            self._disk_mgr.start_postgres()
            self._net_mgr.acquire_vip()

            self._local.set_role(NodeRole.PRIMARY)
            self._local.set_disk_state(DiskState.ATTACHED_RW)
            self._local.set_pg_state(PgState.RUNNING_PRIMARY)
            self._local.set_health(NodeHealth.HEALTHY)
            log.info("[ELECTION] %s is now PRIMARY", my_name)
            return NodeRole.PRIMARY

        except Exception as exc:
            log.error("[ELECTION] Failed to become PRIMARY: %s", exc)
            self._local.set_health(NodeHealth.FAILED)
            # Fall back to standby rather than leaving in unknown state
            return self._become_standby()

    def _become_standby(self) -> NodeRole:
        """Set up node as STANDBY (PG not running, disk not attached RW)."""
        my_name = self._cfg.cluster.node_name
        log.info("[ELECTION] %s is STANDBY", my_name)

        self._local.set_role(NodeRole.STANDBY)
        self._local.set_disk_state(DiskState.DETACHED)
        self._local.set_pg_state(PgState.STOPPED)
        self._local.set_health(NodeHealth.HEALTHY)
        return NodeRole.STANDBY

    def _compete_for_primary(self) -> NodeRole:
        """
        Try to attach the disk; handle racing with peer.
        A small random jitter is introduced so that both nodes don't
        hammer the API simultaneously.
        """
        import random
        # Jitter 0–2 s based on node name hash so it's deterministic
        jitter = (hash(self._cfg.cluster.node_name) % 20) / 10.0
        log.debug("Election jitter: %.1fs", jitter)
        time.sleep(jitter)

        try:
            self._disk_mgr.attach_disk_rw()
            log.info("[ELECTION] Won disk race → PRIMARY")
            return self._become_primary(attach_disk=False)
        except RuntimeError as exc:
            log.info("[ELECTION] Lost disk race (%s) → STANDBY", exc)
            return self._become_standby()
