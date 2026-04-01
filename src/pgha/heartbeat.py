"""
heartbeat.py — UDP Heartbeat Manager

Responsibilities:
  1. Sender thread  — broadcasts HeartbeatMsg to peer every heartbeat_interval.
  2. Receiver thread — listens for peer HeartbeatMsg, updates peer NodeState.
  3. Watchdog thread — detects peer silence exceeding dead_interval and fires
                       a HaEvent(PEER_DEAD) into the event queue.

Protocol:
  UDP unicast, JSON payload, max 4096 bytes.
  No authentication token — runs inside a private VPC; use GCP VPC firewall
  rules to restrict port 7777 to the two nodes only.

Thread safety:
  _peer_state is updated under _lock.  Callers read it through peer_snapshot().
"""

import json
import logging
import queue
import socket
import threading
import time
from typing import Callable, Optional

from pgha.config import Config
from pgha.models import (
    DiskState,
    HaEvent,
    HaEventType,
    HeartbeatMsg,
    NodeHealth,
    NodeRole,
    NodeState,
    PgState,
)

log = logging.getLogger(__name__)

_MAX_MSG_BYTES = 4096
_SOCK_TIMEOUT  = 1.0   # seconds; recv blocks at most this long


class HeartbeatManager:
    """
    Manages bidirectional heartbeat communication with the peer node.

    Usage::

        hb = HeartbeatManager(cfg, event_q, local_state_fn)
        hb.start()
        # ... daemon loop ...
        hb.stop()

    *local_state_fn* is a zero-argument callable that returns
    the current :class:`~pgha.models.NodeState` for *this* node.
    It is called just before each heartbeat is sent so the message
    always reflects the latest local status.
    """

    def __init__(
        self,
        cfg: Config,
        event_queue: queue.Queue,
        local_state_fn: Callable[[], NodeState],
    ) -> None:
        self._cfg           = cfg
        self._event_q       = event_queue
        self._local_state   = local_state_fn
        self._seq           = 0
        self._lock          = threading.Lock()
        self._stop_evt      = threading.Event()

        # Peer state tracked here
        self._peer_state    = NodeState(name=cfg.cluster.peer_node)
        self._peer_alive    = False

        # Outbound socket (send to peer)
        self._tx_sock: Optional[socket.socket] = None
        # Inbound socket (receive from peer)
        self._rx_sock: Optional[socket.socket] = None

        self._sender_thread   = threading.Thread(
            target=self._sender_loop, name="hb-sender", daemon=True)
        self._receiver_thread = threading.Thread(
            target=self._receiver_loop, name="hb-receiver", daemon=True)
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop, name="hb-watchdog", daemon=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._init_sockets()
        self._sender_thread.start()
        self._receiver_thread.start()
        self._watchdog_thread.start()
        log.info(
            "Heartbeat manager started — sending to %s:%d every %.1fs; "
            "dead interval %.1fs",
            self._cfg.cluster.peer_ip,
            self._cfg.cluster.heartbeat_port,
            self._cfg.cluster.heartbeat_interval,
            self._cfg.cluster.dead_interval,
        )

    def stop(self) -> None:
        self._stop_evt.set()
        for t in (self._sender_thread, self._receiver_thread, self._watchdog_thread):
            t.join(timeout=5)
        for sock in (self._tx_sock, self._rx_sock):
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass
        log.info("Heartbeat manager stopped")

    def peer_snapshot(self) -> NodeState:
        """Return a shallow copy of the current peer NodeState (thread-safe)."""
        with self._lock:
            import copy
            return copy.copy(self._peer_state)

    def is_peer_alive(self) -> bool:
        with self._lock:
            return self._peer_alive

    # ------------------------------------------------------------------
    # Socket initialisation
    # ------------------------------------------------------------------

    def _init_sockets(self) -> None:
        # TX socket — outbound UDP, no bind needed
        self._tx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._tx_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        # RX socket — bind to the heartbeat port
        self._rx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._rx_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._rx_sock.settimeout(_SOCK_TIMEOUT)
        self._rx_sock.bind(("0.0.0.0", self._cfg.cluster.heartbeat_port))
        log.debug("Heartbeat RX socket bound to 0.0.0.0:%d",
                  self._cfg.cluster.heartbeat_port)

    # ------------------------------------------------------------------
    # Sender thread
    # ------------------------------------------------------------------

    def _sender_loop(self) -> None:
        """Send a heartbeat to the peer every heartbeat_interval seconds."""
        interval = self._cfg.cluster.heartbeat_interval
        peer_addr = (
            self._cfg.cluster.peer_ip,
            self._cfg.cluster.heartbeat_port,
        )
        while not self._stop_evt.is_set():
            try:
                self._seq += 1
                local = self._local_state()
                msg = HeartbeatMsg(
                    node        = self._cfg.cluster.node_name,
                    role        = local.role,
                    seq         = self._seq,
                    pg_state    = local.pg_state,
                    disk_state  = local.disk_state,
                    node_health = local.health,
                )
                payload = json.dumps(msg.to_dict()).encode("utf-8")
                self._tx_sock.sendto(payload, peer_addr)
                log.debug("HB → %s seq=%d role=%s", peer_addr[0], self._seq, local.role)
            except OSError as exc:
                log.warning("Heartbeat send error: %s", exc)
            self._stop_evt.wait(timeout=interval)

    # ------------------------------------------------------------------
    # Receiver thread
    # ------------------------------------------------------------------

    def _receiver_loop(self) -> None:
        """Listen for heartbeats from the peer and update peer NodeState."""
        while not self._stop_evt.is_set():
            try:
                data, addr = self._rx_sock.recvfrom(_MAX_MSG_BYTES)
            except socket.timeout:
                continue
            except OSError as exc:
                if not self._stop_evt.is_set():
                    log.warning("Heartbeat recv error: %s", exc)
                continue

            try:
                d = json.loads(data.decode("utf-8"))
                msg = HeartbeatMsg.from_dict(d)
            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                log.warning("Malformed heartbeat from %s: %s", addr, exc)
                continue

            # Ignore our own heartbeats (UDP broadcast scenarios)
            if msg.node == self._cfg.cluster.node_name:
                continue

            was_alive = self._peer_alive
            with self._lock:
                self._peer_state.role             = msg.role
                self._peer_state.health           = msg.node_health
                self._peer_state.pg_state         = msg.pg_state
                self._peer_state.disk_state       = msg.disk_state
                self._peer_state.last_heartbeat_ts = time.time()  # local receive time — avoids clock-skew with peer
                self._peer_state.heartbeat_seq    = msg.seq
                self._peer_alive = True

            log.debug("HB ← %s seq=%d role=%s pg=%s",
                      msg.node, msg.seq, msg.role, msg.pg_state)

            # Peer recovered after a silence period
            if not was_alive:
                self._emit(HaEventType.PEER_RECOVERED,
                           f"Peer {msg.node} is alive again (seq={msg.seq})")

    # ------------------------------------------------------------------
    # Watchdog thread
    # ------------------------------------------------------------------

    def _watchdog_loop(self) -> None:
        """Declare peer dead if no heartbeat received within dead_interval."""
        check_interval = min(1.0, self._cfg.cluster.dead_interval / 2)
        dead_interval  = self._cfg.cluster.dead_interval

        while not self._stop_evt.is_set():
            self._stop_evt.wait(timeout=check_interval)
            if self._stop_evt.is_set():
                break

            with self._lock:
                last_ts    = self._peer_state.last_heartbeat_ts
                was_alive  = self._peer_alive

            if last_ts == 0.0:
                # Never received a heartbeat yet — not yet dead, just unknown
                continue

            age = time.time() - last_ts
            if age > dead_interval and was_alive:
                # Secondary check: TCP probe to peer's PostgreSQL port.
                # If the peer's DB port is still reachable, the UDP heartbeat
                # path is flapping but the peer is alive — do NOT declare dead.
                if self._is_peer_pg_reachable():
                    log.warning(
                        "Peer %s UDP heartbeat silent for %.1fs but PostgreSQL "
                        "port %d is reachable via TCP — treating as heartbeat "
                        "flap, NOT declaring peer dead",
                        self._cfg.cluster.peer_node, age,
                        self._cfg.postgresql.port,
                    )
                    continue

                with self._lock:
                    self._peer_alive = False
                log.warning(
                    "Peer %s declared DEAD — last heartbeat %.1fs ago, "
                    "PostgreSQL TCP probe also failed",
                    self._cfg.cluster.peer_node, age,
                )
                self._emit(
                    HaEventType.PEER_DEAD,
                    f"No heartbeat from {self._cfg.cluster.peer_node} "
                    f"for {age:.1f}s (threshold {dead_interval}s) and "
                    f"TCP probe to port {self._cfg.postgresql.port} failed",
                )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_peer_pg_reachable(self) -> bool:
        """
        TCP probe to peer's PostgreSQL port as a secondary reachability check.

        Returns True if we can open a TCP connection to peer_ip:postgresql.port
        within 2 seconds.  A successful TCP handshake means the peer VM (and
        its network stack) is up — the UDPheartbeat failure is a flap, not a
        real node death.
        """
        try:
            with socket.create_connection(
                (self._cfg.cluster.peer_ip, self._cfg.postgresql.port),
                timeout=2.0,
            ):
                return True
        except OSError:
            return False

    def _emit(self, event_type: HaEventType, message: str) -> None:
        evt = HaEvent(
            event_type = event_type,
            node       = self._cfg.cluster.node_name,
            message    = message,
        )
        try:
            self._event_q.put_nowait(evt)
        except queue.Full:
            log.error("Event queue full — dropping event: %s", evt)
        log.info("EVENT: %s", evt)
