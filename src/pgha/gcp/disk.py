"""
gcp/disk.py — GCP Regional Persistent Disk (RPD) Manager

Handles:
  - Querying which instance the RPD is currently attached to (RW).
  - Forceful detach from the *current* holder (fencing).
  - Attaching the disk to *this* node in read-write mode.
  - Mount / unmount the filesystem on the RPD.
  - Starting / stopping PostgreSQL after disk operations.

GCP API notes:
  - Uses the raw Compute Engine REST API (compute/v1) with
    google-auth for credential management.
  - Regional disk attach/detach is done via the Instances service.
  - "Force attach" bypasses the check that the disk is still attached
    elsewhere — essential for split-brain recovery.
  - All disk operations are synchronous (we wait for the LRO to finish).

Security:
  - Service account credentials are loaded from the instance's Workload
    Identity (or from the key file in cfg.gcp.service_account_key).
  - The service account needs:
      compute.instances.attachDisk
      compute.instances.detachDisk
      compute.disks.use
      compute.disks.get
"""

import logging
import os
import pwd
import subprocess
import time
from typing import Optional

import google.auth
import requests
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account

from pgha.config import Config
from pgha.models import DiskState

log = logging.getLogger(__name__)

_COMPUTE_BASE = "https://compute.googleapis.com/compute/v1"


class DiskManager:
    """
    Manages the Regional Persistent Disk lifecycle for this node.

    All public methods are blocking and raise RuntimeError on failure.
    """

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._creds = self._build_credentials()

    # ------------------------------------------------------------------
    # Credential helpers
    # ------------------------------------------------------------------

    def _build_credentials(self):
        """Return google.auth credentials (service-account key or ADC)."""
        key = self._cfg.gcp.service_account_key
        if key and os.path.isfile(key):
            log.debug("Using service account key: %s", key)
            return service_account.Credentials.from_service_account_file(
                key,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
        log.debug("Using default/Workload Identity credentials")
        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"])
        return creds

    def _auth_headers(self) -> dict:
        """Return Authorization + Content-Type headers with a fresh token."""
        self._creds.refresh(GoogleAuthRequest())
        return {
            "Authorization": f"Bearer {self._creds.token}",
            "Content-Type":  "application/json",
        }

    # ------------------------------------------------------------------
    # Disk state queries
    # ------------------------------------------------------------------

    def get_disk_state(self) -> DiskState:
        """Return the disk state from *this* node's perspective."""
        project = self._cfg.gcp.project_id
        zone    = self._cfg.my_zone
        inst    = self._cfg.cluster.node_name

        url = (f"{_COMPUTE_BASE}/projects/{project}/zones/{zone}"
               f"/instances/{inst}")
        try:
            resp = requests.get(url, headers=self._auth_headers(), timeout=30)
            resp.raise_for_status()
            instance = resp.json()
        except Exception as exc:
            log.error("get_disk_state: instance lookup failed: %s", exc)
            return DiskState.UNKNOWN

        disk_name = self._cfg.gcp.disk_name
        for disk in instance.get("disks", []):
            source = disk.get("source", "")
            if source.endswith(f"/disks/{disk_name}"):
                mode = disk.get("mode", "")
                if mode == "READ_WRITE":
                    return DiskState.ATTACHED_RW
                if mode == "READ_ONLY":
                    return DiskState.ATTACHED_RO

        return DiskState.DETACHED

    def current_rw_holder(self) -> Optional[str]:
        """
        Return the instance name that currently has the disk in RW mode,
        or None if the disk is not attached anywhere in RW mode.

        Uses a single regional disk GET — the 'users' field lists all
        attached instances. Then checks the mode on the disk metadata
        without additional per-instance API calls.
        """
        project   = self._cfg.gcp.project_id
        region    = self._cfg.gcp.region
        disk_name = self._cfg.gcp.disk_name

        url = (f"{_COMPUTE_BASE}/projects/{project}/regions/{region}"
               f"/disks/{disk_name}")
        try:
            resp = requests.get(url, headers=self._auth_headers(), timeout=30)
            resp.raise_for_status()
            disk = resp.json()
        except Exception as exc:
            log.error("current_rw_holder: disk get failed: %s", exc)
            return None

        users = disk.get("users", [])
        if not users:
            return None

        # For each user, check by fetching the instance
        for user_url in users:
            instance_name = user_url.rstrip("/").split("/")[-1]
            zone = self._zone_for_instance(instance_name)
            if zone is None:
                continue
            inst_url = (f"{_COMPUTE_BASE}/projects/{project}/zones/{zone}"
                        f"/instances/{instance_name}")
            try:
                resp = requests.get(inst_url, headers=self._auth_headers(),
                                    timeout=30)
                resp.raise_for_status()
                inst_data = resp.json()
                for d in inst_data.get("disks", []):
                    source = d.get("source", "")
                    if source.endswith(f"/disks/{disk_name}") \
                            and d.get("mode") == "READ_WRITE":
                        return instance_name
            except Exception as exc:
                log.warning("current_rw_holder: could not inspect %s: %s",
                            instance_name, exc)

        return None

    def get_instance_status(self, instance_name: str, zone: str) -> str:
        """
        Return the GCP instance status for the given instance.

        Possible values returned by GCP:
          RUNNING, STAGING, STOPPING, STOPPED, TERMINATED, SUSPENDED

        Returns 'UNKNOWN' if the API call fails (network or auth error).
        This is used as the quorum/tie-breaker check before fencing.
        """
        project = self._cfg.gcp.project_id
        url = (f"{_COMPUTE_BASE}/projects/{project}/zones/{zone}"
               f"/instances/{instance_name}")
        try:
            resp = requests.get(url, headers=self._auth_headers(), timeout=30)
            resp.raise_for_status()
            status = resp.json().get("status", "UNKNOWN")
            log.info("GCP quorum check: instance %s status = %s",
                     instance_name, status)
            return status
        except Exception as exc:
            log.error("get_instance_status(%s) failed — GCP API unreachable: %s",
                      instance_name, exc)
            return "UNKNOWN"

    # ------------------------------------------------------------------
    # Disk operations
    # ------------------------------------------------------------------

    def force_detach_from_peer(self) -> None:
        """
        FENCE: Forcibly detach the RPD from the peer node.

        Uses force=True so the operation succeeds even if the peer is
        unresponsive.  This is the STONITH equivalent for GCP.
        """
        project  = self._cfg.gcp.project_id
        peer     = self._cfg.cluster.peer_node
        zone     = self._cfg.peer_zone
        dev_name = self._cfg.gcp.disk_device_name

        log.warning("FENCING: force-detaching disk '%s' from %s (%s)",
                    dev_name, peer, zone)

        url = (f"{_COMPUTE_BASE}/projects/{project}/zones/{zone}"
               f"/instances/{peer}/detachDisk?deviceName={dev_name}")
        try:
            resp = requests.post(url, headers=self._auth_headers(), timeout=30)
            if resp.status_code in (200, 204):
                op_name = resp.json().get("name")
                if op_name:
                    self._wait_zone_op(op_name, zone)
                log.info("Disk successfully detached from peer %s", peer)
            elif resp.status_code in (400, 404):
                # Peer already down or disk not attached — treat as OK
                log.warning("force_detach_from_peer: HTTP %d (peer likely "
                            "already fenced): %s", resp.status_code, resp.text)
            else:
                raise RuntimeError(
                    f"force_detach_from_peer failed (HTTP {resp.status_code}): "
                    f"{resp.text}")
        except requests.exceptions.RequestException as exc:
            log.warning("force_detach_from_peer network error: %s "
                        "(continuing anyway)", exc)

    def attach_disk_rw(self) -> None:
        """
        Attach the RPD to *this* node in READ_WRITE mode.

        Uses forceAttach=true so we can attach even if the disk is still
        registered on the (fenced) peer.
        """
        project  = self._cfg.gcp.project_id
        zone     = self._cfg.my_zone
        inst     = self._cfg.cluster.node_name
        dev_name = self._cfg.gcp.disk_device_name

        log.info("Attaching disk '%s' to %s (%s) in RW mode",
                 dev_name, inst, zone)

        url = (f"{_COMPUTE_BASE}/projects/{project}/zones/{zone}"
               f"/instances/{inst}/attachDisk?forceAttach=true")
        payload = {
            "source":     self._disk_self_link(),
            "mode":       "READ_WRITE",
            "autoDelete": False,
            "deviceName": dev_name,
        }

        try:
            resp = requests.post(url, headers=self._auth_headers(),
                                 json=payload, timeout=30)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"attachDisk failed (HTTP {resp.status_code}): {resp.text}")
            op_name = resp.json().get("name")
            if op_name:
                self._wait_zone_op(op_name, zone)
            log.info("Disk '%s' successfully attached to %s in RW mode",
                     dev_name, inst)
        except requests.exceptions.RequestException as exc:
            raise RuntimeError(
                f"attach_disk_rw failed for {inst}: {exc}") from exc

    def detach_disk(self) -> None:
        """Gracefully detach the RPD from *this* node."""
        project  = self._cfg.gcp.project_id
        zone     = self._cfg.my_zone
        inst     = self._cfg.cluster.node_name
        dev_name = self._cfg.gcp.disk_device_name

        log.info("Detaching disk '%s' from %s", dev_name, inst)

        url = (f"{_COMPUTE_BASE}/projects/{project}/zones/{zone}"
               f"/instances/{inst}/detachDisk?deviceName={dev_name}")
        try:
            resp = requests.post(url, headers=self._auth_headers(), timeout=30)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"detachDisk failed (HTTP {resp.status_code}): {resp.text}")
            op_name = resp.json().get("name")
            if op_name:
                self._wait_zone_op(op_name, zone)
            log.info("Disk detached from %s", inst)
        except requests.exceptions.RequestException as exc:
            raise RuntimeError(
                f"detach_disk failed for {inst}: {exc}") from exc

    # ------------------------------------------------------------------
    # Filesystem operations
    # ------------------------------------------------------------------

    def mount_disk(self) -> None:
        """Mount the RPD filesystem at the configured mount point."""
        mount_pt = self._cfg.gcp.disk_mount_point
        dev_name = self._cfg.gcp.disk_device_name
        dev_path = f"/dev/disk/by-id/google-{dev_name}"

        # Wait for the device node to appear (udev can lag after attach)
        self._wait_for_device(dev_path, timeout=30)

        if self._is_mounted(mount_pt):
            log.info("Filesystem already mounted at %s — skipping mount",
                     mount_pt)
            return

        # Create mount point directory if it does not exist
        if not os.path.exists(mount_pt):
            log.info("Creating mount point directory %s", mount_pt)
            result = subprocess.run(
                ["mkdir", "-p", mount_pt],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                universal_newlines=True,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    "mkdir -p {} failed: {}".format(mount_pt, result.stderr.strip()))

        # Mount — OS auto-detects the filesystem type (xfs/ext4)
        log.info("Mounting %s -> %s", dev_path, mount_pt)
        result = subprocess.run(
            ["mount", dev_path, mount_pt],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                "mount {} {} failed: {}".format(
                    dev_path, mount_pt, result.stderr.strip()))
        log.info("Disk mounted successfully at %s", mount_pt)

    def unmount_disk(self) -> None:
        """Unmount the RPD filesystem."""
        mount_pt = self._cfg.gcp.disk_mount_point
        if not self._is_mounted(mount_pt):
            log.info("Filesystem not mounted at %s — skipping umount", mount_pt)
            return

        cmd = ["umount", "-f", mount_pt]
        log.info("Unmounting: %s", " ".join(cmd))
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                "umount failed: {}".format(result.stderr.strip()))
        log.info("Filesystem unmounted from %s", mount_pt)

    # ------------------------------------------------------------------
    # PostgreSQL start / stop
    # ------------------------------------------------------------------

    def _pg_is_running(self) -> bool:
        """Return True if pg_ctl status reports PostgreSQL is running."""
        cfg = self._cfg.postgresql
        try:
            result = subprocess.run(
                ["sudo", "-u", cfg.pg_os_user, cfg.pg_ctl, "status",
                 "-D", cfg.data_dir],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=10,
            )
            return result.returncode == 0
        except Exception:
            return False

    def start_postgres(self) -> None:
        """Start PostgreSQL using pg_ctl; wait up to pg_start_timeout.

        Skips start if PostgreSQL is already running (pg_ctl status == 0).

        Uses DEVNULL for stdout/stderr instead of PIPE.  pg_ctl start
        forks the postgres daemon which inherits pipe fds — keeping the
        pipe open forever, causing subprocess.run() to hang until the
        timeout fires (~70 s) and then crash with
        "Invalid file object: <_io.TextIOWrapper>" on Python 3.6.
        """
        if self._pg_is_running():
            log.info("PostgreSQL is already running — skipping start")
            return

        cfg     = self._cfg.postgresql
        timeout = cfg.pg_start_timeout
        os_user = cfg.pg_os_user
        cmd     = ["sudo", "-u", os_user, cfg.pg_ctl, "start", "-D", cfg.data_dir, "-w",
                   "-t", str(timeout)]
        log.info("Starting PostgreSQL: %s", " ".join(cmd))
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=timeout + 10,
        )
        if result.returncode != 0:
            raise RuntimeError(
                "pg_ctl start failed (exit code {})".format(result.returncode))
        log.info("PostgreSQL started successfully")

    def stop_postgres(self, mode: str = "fast") -> None:
        """Stop PostgreSQL using pg_ctl (mode: smart|fast|immediate)."""
        cfg     = self._cfg.postgresql
        timeout = cfg.pg_stop_timeout
        os_user = cfg.pg_os_user
        cmd     = ["sudo", "-u", os_user, cfg.pg_ctl, "stop", "-D", cfg.data_dir,
                   "-m", mode, "-w", "-t", str(timeout)]
        log.info("Stopping PostgreSQL (mode=%s): %s", mode, " ".join(cmd))
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=timeout + 10,
        )
        if result.returncode != 0:
            log.warning("pg_ctl stop returned %d: %s",
                        result.returncode, result.stderr.strip())
        else:
            log.info("PostgreSQL stopped successfully")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run_as_postgres(self):
        """
        Return a preexec_fn that switches the subprocess to the EDB/PG
        OS user (enterprisedb for EDB EPAS, postgres for community PG).
        Python 3.6-compatible replacement for subprocess user=.
        The daemon runs as root; pg_ctl must run as the database OS user.
        """
        os_user = self._cfg.postgresql.pg_os_user
        def _switch():
            pw = pwd.getpwnam(os_user)
            os.setgid(pw.pw_gid)
            os.setuid(pw.pw_uid)
        return _switch

    def _disk_self_link(self) -> str:
        """Build the self-link URL for the regional disk."""
        cfg = self._cfg.gcp
        return (
            f"https://www.googleapis.com/compute/v1/projects/{cfg.project_id}"
            f"/regions/{cfg.region}/disks/{cfg.disk_name}"
        )

    def _zone_for_instance(self, instance_name: str) -> Optional[str]:
        """Return the zone for a known instance name."""
        cfg = self._cfg.gcp
        if instance_name == cfg.instance_primary:
            return cfg.zone_primary
        if instance_name == cfg.instance_standby:
            return cfg.zone_standby
        return None

    def _wait_zone_op(self, op_name: str, zone: str,
                      poll_interval: float = 2.0) -> None:
        """Poll a zonal LRO until it finishes or times out."""
        project  = self._cfg.gcp.project_id
        timeout  = self._cfg.gcp.api_timeout
        deadline = time.monotonic() + timeout

        url = (f"{_COMPUTE_BASE}/projects/{project}/zones/{zone}"
               f"/operations/{op_name}")

        while time.monotonic() < deadline:
            resp = requests.get(url, headers=self._auth_headers(), timeout=30)
            resp.raise_for_status()
            op = resp.json()
            status = op.get("status", "")
            log.debug("[WAIT] operation %s status=%s", op_name, status)
            if status == "DONE":
                error = op.get("error")
                if error:
                    errors = "; ".join(
                        e.get("message", "") for e in error.get("errors", []))
                    raise RuntimeError(
                        f"GCP operation {op_name} failed: {errors}")
                return
            time.sleep(poll_interval)

        raise TimeoutError(
            f"GCP operation {op_name} did not complete in {timeout}s")

    def _is_mounted(self, mount_pt: str) -> bool:
        with open("/proc/mounts", "r") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == mount_pt:
                    return True
        return False

    def _wait_for_device(self, dev_path: str, timeout: int = 30) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if os.path.exists(dev_path):
                return
            time.sleep(1)
        raise TimeoutError(
            f"Device {dev_path} did not appear within {timeout}s")
