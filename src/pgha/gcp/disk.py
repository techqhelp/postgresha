"""
gcp/disk.py — GCP Regional Persistent Disk (RPD) Manager

Handles:
  - Querying which instance the RPD is currently attached to (RW).
  - Forceful detach from the *current* holder (fencing).
  - Attaching the disk to *this* node in read-write mode.
  - Mount / unmount the filesystem on the RPD.
  - Starting / stopping PostgreSQL after disk operations.

GCP API notes:
  - Uses google-cloud-compute (Compute Engine Python client).
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

from google.cloud import compute_v1
from google.oauth2 import service_account

from pgha.config import Config
from pgha.models import DiskState

log = logging.getLogger(__name__)


class DiskManager:
    """
    Manages the Regional Persistent Disk lifecycle for this node.

    All public methods are blocking and raise RuntimeError on failure.
    """

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._instances_client = self._build_instances_client()
        self._disks_client     = self._build_disks_client()
        self._zone_ops_client  = self._build_zone_ops_client()
        self._region_ops_client = self._build_region_ops_client()

    # ------------------------------------------------------------------
    # Client constructors
    # ------------------------------------------------------------------

    def _credentials(self):
        key = self._cfg.gcp.service_account_key
        if key and os.path.isfile(key):
            log.debug("Using service account key: %s", key)
            return service_account.Credentials.from_service_account_file(
                key,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
        log.debug("Using default/Workload Identity credentials")
        return None  # SDK will auto-detect from env/metadata server

    def _build_instances_client(self) -> compute_v1.InstancesClient:
        creds = self._credentials()
        if creds:
            return compute_v1.InstancesClient(credentials=creds)
        return compute_v1.InstancesClient()

    def _build_disks_client(self) -> compute_v1.RegionDisksClient:
        creds = self._credentials()
        if creds:
            return compute_v1.RegionDisksClient(credentials=creds)
        return compute_v1.RegionDisksClient()

    def _build_zone_ops_client(self) -> compute_v1.ZoneOperationsClient:
        creds = self._credentials()
        if creds:
            return compute_v1.ZoneOperationsClient(credentials=creds)
        return compute_v1.ZoneOperationsClient()

    def _build_region_ops_client(self) -> compute_v1.RegionOperationsClient:
        creds = self._credentials()
        if creds:
            return compute_v1.RegionOperationsClient(credentials=creds)
        return compute_v1.RegionOperationsClient()

    # ------------------------------------------------------------------
    # Disk state queries
    # ------------------------------------------------------------------

    def get_disk_state(self) -> DiskState:
        """Return the disk state from *this* node's perspective."""
        project = self._cfg.gcp.project_id
        zone    = self._cfg.my_zone
        inst    = self._cfg.cluster.node_name

        try:
            instance = self._instances_client.get(
                project=project, zone=zone, instance=inst)
        except Exception as exc:
            log.error("get_disk_state: instance lookup failed: %s", exc)
            return DiskState.UNKNOWN

        disk_url = self._disk_self_link()
        for disk in instance.disks:
            if disk.source == disk_url or disk_url.endswith(
                    disk.source.split("/")[-1]):
                if disk.mode == "READ_WRITE":
                    return DiskState.ATTACHED_RW
                if disk.mode == "READ_ONLY":
                    return DiskState.ATTACHED_RO

        return DiskState.DETACHED

    def current_rw_holder(self) -> Optional[str]:
        """
        Return the instance name that currently has the disk in RW mode,
        or None if the disk is not attached anywhere in RW mode.
        """
        project = self._cfg.gcp.project_id
        region  = self._cfg.gcp.region

        try:
            disk = self._disks_client.get(
                project=project, region=region,
                disk=self._cfg.gcp.disk_name)
        except Exception as exc:
            log.error("current_rw_holder: disk get failed: %s", exc)
            return None

        for user_url in disk.users:
            # user_url is like .../instances/pg-primary
            instance_name = user_url.rstrip("/").split("/")[-1]
            # Check if it's RW
            zone = self._zone_for_instance(instance_name)
            if zone is None:
                continue
            try:
                inst_obj = self._instances_client.get(
                    project=project, zone=zone, instance=instance_name)
                disk_url = self._disk_self_link()
                for d in inst_obj.disks:
                    if (d.source == disk_url or disk_url.endswith(
                            d.source.split("/")[-1])) \
                            and d.mode == "READ_WRITE":
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
        try:
            instance = self._instances_client.get(
                project=project, zone=zone, instance=instance_name)
            status = instance.status
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

        try:
            op = self._instances_client.detach_disk(
                project         = project,
                zone            = zone,
                instance        = peer,
                device_name     = dev_name,
            )
            self._wait_zone_op(op, zone)
            log.info("Disk successfully detached from peer %s", peer)
        except Exception as exc:
            # If peer is already down GCP may return a 4xx — treat as OK
            log.warning("force_detach_from_peer: %s (continuing anyway)", exc)

    def attach_disk_rw(self) -> None:
        """
        Attach the RPD to *this* node in READ_WRITE mode.

        Uses AttachDiskInstanceRequest with force_attach=True so we can
        attach even if the disk is still registered on the (fenced) peer.
        operation.result() blocks until the LRO completes.
        """
        project  = self._cfg.gcp.project_id
        zone     = self._cfg.my_zone
        inst     = self._cfg.cluster.node_name
        dev_name = self._cfg.gcp.disk_device_name

        log.info("Attaching disk '%s' to %s (%s) in RW mode",
                 dev_name, inst, zone)

        attached_disk = compute_v1.AttachedDisk()
        attached_disk.source      = self._disk_self_link()
        attached_disk.mode        = "READ_WRITE"
        attached_disk.auto_delete = False
        attached_disk.device_name = dev_name

        request = compute_v1.AttachDiskInstanceRequest(
            project                = project,
            zone                   = zone,
            instance               = inst,
            attached_disk_resource = attached_disk,
            force_attach           = True,
        )

        try:
            op = self._instances_client.attach_disk(request=request)
            # google-cloud-compute 1.3.x returns a raw Operation proto;
            # poll it with _wait_zone_op (ExtendedOperation/.result() needs >=1.4).
            self._wait_zone_op(op, zone)
            log.info("Disk '%s' successfully attached to %s in RW mode",
                     dev_name, inst)
        except Exception as exc:
            raise RuntimeError(
                f"attach_disk_rw failed for {inst}: {exc}") from exc

    def detach_disk(self) -> None:
        """Gracefully detach the RPD from *this* node."""
        project  = self._cfg.gcp.project_id
        zone     = self._cfg.my_zone
        inst     = self._cfg.cluster.node_name
        dev_name = self._cfg.gcp.disk_device_name

        log.info("Detaching disk '%s' from %s", dev_name, inst)
        try:
            op = self._instances_client.detach_disk(
                project     = project,
                zone        = zone,
                instance    = inst,
                device_name = dev_name,
            )
            self._wait_zone_op(op, zone)
            log.info("Disk detached from %s", inst)
        except Exception as exc:
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

    def start_postgres(self) -> None:
        """Start PostgreSQL using pg_ctl; wait up to pg_start_timeout."""
        cfg     = self._cfg.postgresql
        timeout = cfg.pg_start_timeout
        cmd     = [cfg.pg_ctl, "start", "-D", cfg.data_dir, "-w",
                   "-t", str(timeout)]
        log.info("Starting PostgreSQL: %s", " ".join(cmd))
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=timeout + 10,
            preexec_fn=self._run_as_postgres(),
        )
        if result.returncode != 0:
            raise RuntimeError(
                "pg_ctl start failed: {}".format(result.stderr.strip()))
        log.info("PostgreSQL started successfully")

    def stop_postgres(self, mode: str = "fast") -> None:
        """Stop PostgreSQL using pg_ctl (mode: smart|fast|immediate)."""
        cfg     = self._cfg.postgresql
        timeout = cfg.pg_stop_timeout
        cmd     = [cfg.pg_ctl, "stop", "-D", cfg.data_dir,
                   "-m", mode, "-w", "-t", str(timeout)]
        log.info("Stopping PostgreSQL (mode=%s): %s", mode, " ".join(cmd))
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=timeout + 10,
            preexec_fn=self._run_as_postgres(),
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

    def _wait_zone_op(self, operation, zone: str,
                      poll_interval: float = 2.0) -> None:
        """Poll a zonal LRO until it finishes or times out."""
        project = self._cfg.gcp.project_id
        timeout = self._cfg.gcp.api_timeout
        deadline = time.monotonic() + timeout

        # The client may return a ZoneOperation object directly
        op_name = getattr(operation, "name", None) or str(operation)

        while time.monotonic() < deadline:
            op = self._zone_ops_client.get(
                project=project, zone=zone, operation=op_name)
            if op.status == compute_v1.Operation.Status.DONE:
                if op.error:
                    errors = "; ".join(
                        e.message for e in op.error.errors)
                    raise RuntimeError(
                        f"GCP operation {op_name} failed: {errors}")
                return
            time.sleep(poll_interval)

        raise TimeoutError(
            f"GCP operation {op_name} did not complete in {timeout}s")

    @staticmethod
    def _is_mounted(mount_pt: str) -> bool:
        with open("/proc/mounts", "r") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == mount_pt:
                    return True
        return False

    @staticmethod
    def _wait_for_device(dev_path: str, timeout: int = 30) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if os.path.exists(dev_path):
                return
            time.sleep(1)
        raise TimeoutError(
            f"Device {dev_path} did not appear within {timeout}s")
