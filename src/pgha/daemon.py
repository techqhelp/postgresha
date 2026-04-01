"""
daemon.py — pgha Main Daemon

The daemon is the top-level orchestrator.  It:

  1. Loads configuration.
  2. Sets up logging.
  3. Initialises all sub-systems:
       HeartbeatManager, PostgresMonitor, OsMonitor,
       DiskManager, NetworkManager, LocalNode,
       ElectionManager, FailoverEngine, SwitchoverEngine.
  4. Runs startup election to determine initial role.
  5. Enters the main event loop:
       - Drains the HA event queue.
       - Refreshes local node state from monitor snapshots.
       - Triggers failover when PEER_DEAD is received on a STANDBY.
       - Triggers self-demotion when PG fails on PRIMARY (hands off to peer).
  6. Listens on a Unix domain socket for management API commands
       (used by pgha-ctl).

Management API commands (JSON over Unix socket)
-----------------------------------------------
  {"cmd": "status"}                    → cluster status JSON
  {"cmd": "switchover"}                → initiate planned switchover
  {"cmd": "SWITCHOVER_REQUEST"}        → peer signals us to take over
"""

import json
import logging
import logging.handlers
import os
import queue
import signal
import socket
import sys
import threading
import time
from typing import Optional

from pgha import config as cfg_module
from pgha.cluster.election import ElectionManager
from pgha.cluster.node import LocalNode
from pgha.gcp.disk import DiskManager
from pgha.gcp.network import NetworkManager
from pgha.ha.failover import FailoverEngine
from pgha.ha.switchover import SwitchoverEngine
from pgha.heartbeat import HeartbeatManager
from pgha.models import (
    DiskState,
    HaEventType,
    NodeHealth,
    NodeRole,
    PgState,
)
from pgha.monitor.os_monitor import OsMonitor
from pgha.monitor.postgres import PostgresMonitor

log = logging.getLogger(__name__)

_DEFAULT_CFG = "/etc/pgha/pgha.conf"
_EVENT_QUEUE_SIZE = 256
# TCP port for inter-node commands (SWITCHOVER_REQUEST, PG_FAIL_HANDOFF).
# Must be open between the two nodes in GCP firewall rules.
_PEER_PORT = 7778


def _setup_logging(lcfg) -> None:
    os.makedirs(os.path.dirname(lcfg.file), exist_ok=True)
    root = logging.getLogger()
    root.setLevel(getattr(logging, lcfg.level.upper(), logging.INFO))
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s [%(threadName)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    # File handler with rotation
    fh = logging.handlers.RotatingFileHandler(
        lcfg.file,
        maxBytes    = lcfg.max_bytes,
        backupCount = lcfg.backup_count,
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)
    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    root.addHandler(ch)


class PgHaDaemon:
    def __init__(self, cfg_path: str = _DEFAULT_CFG) -> None:
        self._cfg       = cfg_module.load(cfg_path)
        _setup_logging(self._cfg.logging)

        self._event_q   : queue.Queue = queue.Queue(maxsize=_EVENT_QUEUE_SIZE)
        self._stop_evt  = threading.Event()
        self._failover_lock = threading.Lock()

        # ---- Sub-system initialisation ----------------------------------
        self._local     = LocalNode(self._cfg)
        self._disk_mgr  = DiskManager(self._cfg)
        self._net_mgr   = NetworkManager(self._cfg)
        self._pg_mon    = PostgresMonitor(self._cfg)
        self._os_mon    = OsMonitor(self._cfg)
        self._hb_mgr    = HeartbeatManager(
            self._cfg, self._event_q, self._local.snapshot)
        self._election  = ElectionManager(
            self._cfg, self._local, self._disk_mgr, self._net_mgr)
        self._failover  = FailoverEngine(
            self._cfg, self._local, self._disk_mgr,
            self._net_mgr, self._event_q)
        self._switchover = SwitchoverEngine(
            self._cfg, self._local, self._disk_mgr,
            self._net_mgr, self._event_q)

        # Unix domain socket server thread (local pgha-ctl CLI only)
        self._mgmt_thread: Optional[threading.Thread] = None
        # TCP peer server thread (inter-node: SWITCHOVER_REQUEST, PG_FAIL_HANDOFF)
        self._peer_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def run(self) -> None:
        log.info("pgha daemon starting — node=%s cluster=%s",
                 self._cfg.cluster.node_name,
                 self._cfg.cluster.name)

        # Signal handlers
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self._handle_signal)

        # Start monitors and heartbeat
        self._pg_mon.start()
        self._os_mon.start()
        self._hb_mgr.start()

        # Start management API listener (local CLI via Unix socket)
        self._start_mgmt_server()
        # Start peer TCP server (inter-node commands on port 7778)
        self._start_peer_server()

        # Startup election
        log.info("Running startup cluster election …")
        role = self._election.elect()
        log.info("Initial role: %s", role)

        # Main event loop
        try:
            self._main_loop()
        finally:
            self._shutdown()

    def _main_loop(self) -> None:
        """Drain event queue, update state, trigger HA actions."""
        interval = self._cfg.monitor.check_interval
        pg_fail_threshold = self._cfg.monitor.pg_fail_threshold

        while not self._stop_evt.is_set():
            # --- Process events -----------------------------------------
            while True:
                try:
                    evt = self._event_q.get_nowait()
                except queue.Empty:
                    break
                self._handle_event(evt)

            # --- Refresh local node state from monitors -----------------
            pg_snap = self._pg_mon.snapshot()
            os_snap = self._os_mon.snapshot()

            if pg_snap:
                self._local.set_pg_state(pg_snap.state)
                if not pg_snap.ok:
                    if self._pg_mon.consecutive_failures >= pg_fail_threshold:
                        if self._local.is_primary():
                            log.error(
                                "PostgreSQL has failed %d times on PRIMARY — "
                                "self-demoting to trigger peer failover",
                                self._pg_mon.consecutive_failures,
                            )
                            self._local.set_health(NodeHealth.FAILED)
                            # Stop PG, unmount, detach, release VIP so peer
                            # can take over cleanly
                            self._self_demote_after_pg_failure()
                else:
                    if self._local.health != NodeHealth.HEALTHY:
                        self._local.set_health(NodeHealth.HEALTHY)

            if os_snap and os_snap.degraded:
                self._local.set_health(NodeHealth.DEGRADED)

            # --- Heartbeat fallback: detect peer self-demotion after PG crash --
            # Primary notifies us via TCP after PG failure.  If that TCP signal
            # was lost, we catch it here: peer heartbeat still arrives (VM alive)
            # but peer reports role=STANDBY and disk=DETACHED — meaning it gave
            # up all resources and is waiting for us to take over.
            if self._local.is_standby() and self._hb_mgr.is_peer_alive():
                peer_snap = self._hb_mgr.peer_snapshot()
                if (
                    peer_snap.role       == NodeRole.STANDBY
                    and peer_snap.disk_state == DiskState.DETACHED
                ):
                    # Non-blocking: if a failover thread is already running, skip.
                    if self._failover_lock.acquire(blocking=False):
                        try:
                            if self._local.is_standby() and self._failover.should_attempt():
                                log.warning(
                                    "Heartbeat fallback: peer %s is STANDBY+DISK_DETACHED "
                                    "— peer self-demoted after PG crash. "
                                    "Starting FailoverEngine.",
                                    peer_snap.name,
                                )
                                threading.Thread(
                                    target=self._failover.execute,
                                    name="pg-fail-failover",
                                    daemon=True,
                                ).start()
                        finally:
                            self._failover_lock.release()

            self._stop_evt.wait(timeout=interval)

    def _handle_event(self, evt) -> None:
        log.debug("Handling event: %s", evt)

        if evt.event_type == HaEventType.PEER_DEAD:
            if self._local.is_standby():
                log.warning("Peer is dead — initiating automatic failover")
                with self._failover_lock:
                    if self._failover.should_attempt():
                        threading.Thread(
                            target=self._failover.execute,
                            name="failover",
                            daemon=True,
                        ).start()

        elif evt.event_type == HaEventType.PEER_RECOVERED:
            log.info("Peer recovered: %s", evt.message)

    def _self_demote_after_pg_failure(self) -> None:
        """
        Primary demotes itself when PG fails unrecoverably:
        stop PG (best effort), unmount, detach disk, release VIP.
        This clears the way for the standby to run failover.
        """
        log.warning("Self-demoting PRIMARY after PostgreSQL failure …")
        for action, fn in [
            ("release_vip",     self._net_mgr.release_vip),       # IP first — stop clients connecting to dying node
            ("stop_postgres",   lambda: self._disk_mgr.stop_postgres("immediate")),
            ("unmount_disk",    self._disk_mgr.unmount_disk),
            ("detach_disk",     self._disk_mgr.detach_disk),
        ]:
            try:
                fn()
            except Exception as exc:
                log.warning("Self-demote %s: %s", action, exc)

        self._local.set_role(NodeRole.STANDBY)
        self._local.set_pg_state(PgState.STOPPED)
        log.info("Self-demotion complete — now STANDBY")

        # Notify standby to trigger FailoverEngine (automatic — not switchover).
        # If TCP fails, standby's heartbeat fallback spots STANDBY+DISK_DETACHED.
        try:
            self._send_pg_fail_handoff()
        except Exception as exc:
            log.warning(
                "TCP PG_FAIL_HANDOFF failed (%s). "
                "Standby heartbeat fallback will handle promotion.", exc
            )

    def _send_pg_fail_handoff(self) -> None:
        """Send PG_FAIL_HANDOFF to peer via TCP, triggering its FailoverEngine."""
        peer_ip = self._cfg.cluster.peer_ip
        log.info("Sending PG_FAIL_HANDOFF to peer %s:%d", peer_ip, _PEER_PORT)
        msg = json.dumps({"cmd": "PG_FAIL_HANDOFF"}).encode()
        with socket.create_connection((peer_ip, _PEER_PORT), timeout=10) as sock:
            sock.sendall(msg + b"\n")
            resp_raw = sock.recv(256).decode().strip()
            resp = json.loads(resp_raw)
            if resp.get("cmd") == "PG_FAIL_HANDOFF_ACK":
                log.info("Peer acknowledged PG_FAIL_HANDOFF")
            else:
                log.warning("Unexpected PG_FAIL_HANDOFF response: %s", resp)

    # ------------------------------------------------------------------
    # Peer TCP server  (inter-node: SWITCHOVER_REQUEST / PG_FAIL_HANDOFF)
    # Port 7778 — must be open in GCP firewall between both node IPs.
    # ------------------------------------------------------------------

    def _start_peer_server(self) -> None:
        self._peer_thread = threading.Thread(
            target=self._peer_server_loop,
            name="peer-tcp",
            daemon=True,
        )
        self._peer_thread.start()
        log.info("Peer TCP server listening on 0.0.0.0:%d", _PEER_PORT)

    def _peer_server_loop(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", _PEER_PORT))
        srv.listen(5)
        srv.settimeout(1.0)
        while not self._stop_evt.is_set():
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(
                target=self._handle_peer_conn,
                args=(conn, addr),
                daemon=True,
            ).start()
        srv.close()

    def _handle_peer_conn(self, conn, addr) -> None:
        """Handle one peer TCP command."""
        with conn:
            try:
                data = conn.recv(4096).decode().strip()
                req  = json.loads(data)
                cmd  = req.get("cmd", "")
            except (json.JSONDecodeError, ValueError, OSError) as exc:
                log.warning("Peer %s: bad payload: %s", addr, exc)
                return

            if cmd == "SWITCHOVER_REQUEST":
                # MANUAL planned switchover: primary released all resources and
                # is asking us to take over via SwitchoverEngine (no fencing needed).
                conn.sendall(
                    json.dumps({"cmd": "SWITCHOVER_ACCEPT"}).encode() + b"\n")
                log.info("SWITCHOVER_REQUEST from %s — starting switchover promotion",
                         addr)
                threading.Thread(
                    target=self._switchover.execute_as_standby,
                    name="switchover-promote",
                    daemon=True,
                ).start()

            elif cmd == "PG_FAIL_HANDOFF":
                # AUTOMATIC failover: primary's PostgreSQL crashed; primary already
                # released all resources.  Use FailoverEngine — NOT SwitchoverEngine.
                conn.sendall(
                    json.dumps({"cmd": "PG_FAIL_HANDOFF_ACK"}).encode() + b"\n")
                log.warning(
                    "PG_FAIL_HANDOFF from %s — peer PG crashed, "
                    "starting FailoverEngine", addr)
                with self._failover_lock:
                    if self._local.is_standby() and self._failover.should_attempt():
                        threading.Thread(
                            target=self._failover.execute,
                            name="pg-fail-failover",
                            daemon=True,
                        ).start()

            else:
                conn.sendall(
                    json.dumps({"error": "unknown peer command"}).encode() + b"\n")
                log.warning("Unknown peer command '%s' from %s", cmd, addr)

    # ------------------------------------------------------------------
    # Management API  (Unix domain socket — local pgha-ctl CLI only)
    # ------------------------------------------------------------------

    def _start_mgmt_server(self) -> None:
        sock_path = self._cfg.api.socket_path
        os.makedirs(os.path.dirname(sock_path), exist_ok=True)
        if os.path.exists(sock_path):
            os.unlink(sock_path)

        self._mgmt_thread = threading.Thread(
            target=self._mgmt_server_loop,
            name="mgmt-api",
            daemon=True,
        )
        self._mgmt_thread.start()
        log.info("Management API listening on %s", sock_path)

    def _mgmt_server_loop(self) -> None:
        """Accept and handle management API connections."""
        sock_path = self._cfg.api.socket_path
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(sock_path)
        os.chmod(sock_path, 0o600)
        srv.listen(5)
        srv.settimeout(1.0)

        while not self._stop_evt.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(
                target=self._handle_mgmt_conn,
                args=(conn,),
                daemon=True,
            ).start()

        srv.close()

    def _handle_mgmt_conn(self, conn: socket.socket) -> None:
        with conn:
            try:
                data = conn.recv(4096).decode().strip()
                req  = json.loads(data)
                resp = self._dispatch_mgmt(req)
            except (json.JSONDecodeError, ValueError) as exc:
                resp = {"error": f"Invalid request: {exc}"}
            conn.sendall(json.dumps(resp).encode() + b"\n")

    def _dispatch_mgmt(self, req: dict) -> dict:
        cmd = req.get("cmd", "")

        if cmd == "status":
            local = self._local.snapshot()
            peer  = self._hb_mgr.peer_snapshot()
            pg    = self._pg_mon.snapshot()
            os_s  = self._os_mon.snapshot()
            return {
                "cluster": self._cfg.cluster.name,
                "local": {
                    "node":        local.name,
                    "role":        local.role.value,
                    "health":      local.health.value,
                    "pg_state":    local.pg_state.value,
                    "disk_state":  local.disk_state.value,
                },
                "peer": {
                    "node":       peer.name,
                    "role":       peer.role.value,
                    "health":     peer.health.value,
                    "pg_state":   peer.pg_state.value,
                    "last_hb_age": round(
                        time.time() - peer.last_heartbeat_ts, 1)
                    if peer.last_heartbeat_ts else None,
                    "alive":      self._hb_mgr.is_peer_alive(),
                },
                "postgres": {
                    "ok":       pg.ok        if pg else None,
                    "state":    pg.state.value if pg else None,
                    "message":  pg.message   if pg else None,
                } if pg else {},
                "os": {
                    "cpu_pct":  os_s.cpu_pct   if os_s else None,
                    "mem_pct":  os_s.mem_pct   if os_s else None,
                    "disk_pct": os_s.disk_pct  if os_s else None,
                    "degraded": os_s.degraded  if os_s else None,
                } if os_s else {},
            }

        if cmd == "switchover":
            if not self._local.is_primary():
                return {"error": "switchover must be run on the PRIMARY node"}
            threading.Thread(
                target=self._switchover.execute_as_primary,
                name="switchover",
                daemon=True,
            ).start()
            return {"result": "switchover initiated"}

        if cmd == "reload":
            log.info("Reload requested — not yet implemented")
            return {"result": "reload not implemented"}

        return {"error": f"Unknown command: {cmd}"}

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def _handle_signal(self, signum, _frame) -> None:
        log.info("Received signal %d — shutting down", signum)
        self._stop_evt.set()

    def _shutdown(self) -> None:
        log.info("pgha daemon shutting down …")
        self._hb_mgr.stop()
        self._pg_mon.stop()
        self._os_mon.stop()
        sock_path = self._cfg.api.socket_path
        if os.path.exists(sock_path):
            try:
                os.unlink(sock_path)
            except OSError:
                pass
        log.info("pgha daemon stopped")
