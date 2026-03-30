"""
gcp/network.py — GCP Alias IP (Virtual/Floating IP) Manager

Handles:
  - Removing the VIP alias IP from the current holder's NIC.
  - Adding the VIP alias IP to *this* node's NIC.

In GCP, a "floating IP" is implemented as an alias IP range on a
VM's network interface card (NIC).  Moving it requires two API
calls — remove from old, add to new — which together perform the
same role as `ip addr add/del` + gratuitous ARP in traditional Linux HA.

The internal TCP/IP forwarding inside GCP's SDN is instant so
clients see sub-second reconnects after the alias IP moves.

Implementation note:
  Uses the raw Compute REST API via requests.patch() together with the
  NIC fingerprint, matching the proven pattern used in production.
  The SDK's update_network_interface() omits the fingerprint field and
  can cause 412 Precondition Failed errors on some API versions.

Required IAM permissions (service account):
    compute.instances.updateNetworkInterface
    compute.instances.get
"""

import json
import logging
import os
import time
from typing import List, Optional

import google.auth
import requests
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.cloud import compute_v1
from google.oauth2 import service_account

from pgha.config import Config

log = logging.getLogger(__name__)

_COMPUTE_BASE = "https://compute.googleapis.com/compute/v1"


class NetworkManager:
    """
    Manages the VIP alias IP assignment between the two cluster nodes.

    All public methods are blocking and raise RuntimeError on failure.
    """

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._instances_client = self._build_instances_client()
        self._zone_ops_client  = self._build_zone_ops_client()

    # ------------------------------------------------------------------
    # Credential helpers
    # ------------------------------------------------------------------

    def _build_credentials(self):
        """Return google.auth credentials (service-account key or ADC)."""
        key = self._cfg.gcp.service_account_key
        if key and os.path.isfile(key):
            log.debug("NetworkManager: using service account key %s", key)
            return service_account.Credentials.from_service_account_file(
                key,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"])
        return creds

    def _fresh_token(self) -> str:
        """Return a valid Bearer token, refreshing if necessary."""
        creds = self._build_credentials()
        creds.refresh(GoogleAuthRequest())
        return creds.token

    def _build_instances_client(self) -> compute_v1.InstancesClient:
        key = self._cfg.gcp.service_account_key
        if key and os.path.isfile(key):
            sa_creds = service_account.Credentials.from_service_account_file(
                key, scopes=["https://www.googleapis.com/auth/cloud-platform"])
            return compute_v1.InstancesClient(credentials=sa_creds)
        return compute_v1.InstancesClient()

    def _build_zone_ops_client(self) -> compute_v1.ZoneOperationsClient:
        key = self._cfg.gcp.service_account_key
        if key and os.path.isfile(key):
            sa_creds = service_account.Credentials.from_service_account_file(
                key, scopes=["https://www.googleapis.com/auth/cloud-platform"])
            return compute_v1.ZoneOperationsClient(credentials=sa_creds)
        return compute_v1.ZoneOperationsClient()

    # ------------------------------------------------------------------
    # Public VIP management
    # ------------------------------------------------------------------

    def acquire_vip(self) -> None:
        """
        Add the VIP alias IP to *this* node's NIC.

        Removes the VIP from the peer first (if present), then adds it
        to this node.
        """
        log.info("Acquiring VIP %s on %s",
                 self._cfg.gcp.vip_cidr, self._cfg.cluster.node_name)
        self.release_vip_from_peer()
        self._add_alias_ip(
            instance_name = self._cfg.cluster.node_name,
            zone          = self._cfg.my_zone,
        )
        log.info("VIP %s acquired on %s",
                 self._cfg.gcp.vip_cidr, self._cfg.cluster.node_name)

    def release_vip(self) -> None:
        """Remove the VIP alias IP from *this* node's NIC."""
        log.info("Releasing VIP %s from %s",
                 self._cfg.gcp.vip_cidr, self._cfg.cluster.node_name)
        self._remove_alias_ip(
            instance_name = self._cfg.cluster.node_name,
            zone          = self._cfg.my_zone,
        )

    def release_vip_from_peer(self) -> None:
        """Remove the VIP alias IP from the peer's NIC (if present)."""
        peer = self._cfg.cluster.peer_node
        zone = self._cfg.peer_zone
        if self._has_alias_ip(peer, zone):
            log.info("Removing VIP %s from peer %s",
                     self._cfg.gcp.vip_cidr, peer)
            self._remove_alias_ip(peer, zone)

    def vip_holder(self) -> Optional[str]:
        """
        Return the instance name that currently holds the VIP,
        or None if no instance has it.
        """
        for inst_name, zone in [
            (self._cfg.gcp.instance_primary, self._cfg.gcp.zone_primary),
            (self._cfg.gcp.instance_standby, self._cfg.gcp.zone_standby),
        ]:
            if self._has_alias_ip(inst_name, zone):
                return inst_name
        return None

    def i_have_vip(self) -> bool:
        return self._has_alias_ip(
            self._cfg.cluster.node_name, self._cfg.my_zone)

    # ------------------------------------------------------------------
    # NIC / alias helpers
    # ------------------------------------------------------------------

    def _get_nic(self, instance_name: str, zone: str) -> compute_v1.NetworkInterface:
        """Fetch the instance and return the target NIC object."""
        inst = self._instances_client.get(
            project  = self._cfg.gcp.project_id,
            zone     = zone,
            instance = instance_name,
        )
        for nic in inst.network_interfaces:
            if nic.name == self._cfg.gcp.nic_name:
                return nic
        raise RuntimeError(
            f"NIC '{self._cfg.gcp.nic_name}' not found on {instance_name}")

    def _has_alias_ip(self, instance_name: str, zone: str) -> bool:
        try:
            nic = self._get_nic(instance_name, zone)
            return any(
                a.ip_cidr_range == self._cfg.gcp.vip_cidr
                for a in nic.alias_ip_ranges
            )
        except Exception as exc:
            log.warning("_has_alias_ip(%s): %s", instance_name, exc)
            return False

    def _add_alias_ip(self, instance_name: str, zone: str) -> None:
        """Add vip_cidr to the NIC alias list (idempotent)."""
        nic      = self._get_nic(instance_name, zone)
        vip_cidr = self._cfg.gcp.vip_cidr

        existing = [a.ip_cidr_range for a in nic.alias_ip_ranges]
        if vip_cidr in existing:
            log.info("VIP %s already on %s — skipping add",
                     vip_cidr, instance_name)
            return

        log.info("[ADD] %s → %s", vip_cidr, instance_name)
        new_ranges = [{"ipCidrRange": r} for r in existing]
        new_ranges.append({"ipCidrRange": vip_cidr})

        self._patch_nic_aliases(instance_name, zone, nic, new_ranges)
        self._verify_alias_present(instance_name, zone, vip_cidr)

    def _remove_alias_ip(self, instance_name: str, zone: str) -> None:
        """Remove vip_cidr from the NIC alias list (idempotent)."""
        nic      = self._get_nic(instance_name, zone)
        vip_cidr = self._cfg.gcp.vip_cidr

        existing = [a.ip_cidr_range for a in nic.alias_ip_ranges]
        if vip_cidr not in existing:
            log.info("VIP %s not on %s — skipping remove",
                     vip_cidr, instance_name)
            return

        log.info("[REMOVE] %s ← %s", vip_cidr, instance_name)
        new_ranges = [{"ipCidrRange": r} for r in existing if r != vip_cidr]

        log.debug("Final alias payload for %s: %s",
                  instance_name, json.dumps(new_ranges, indent=2))
        self._patch_nic_aliases(instance_name, zone, nic, new_ranges)
        self._verify_alias_absent(instance_name, zone, vip_cidr)

    # ------------------------------------------------------------------
    # Raw REST PATCH — proven approach using fingerprint
    # ------------------------------------------------------------------

    def _patch_nic_aliases(
        self,
        instance_name: str,
        zone: str,
        nic: compute_v1.NetworkInterface,
        alias_ranges: List[dict],
    ) -> None:
        """
        PATCH the NIC alias IP list via the raw Compute REST API.

        Uses the NIC fingerprint (required to avoid 412 errors) and a
        fresh Bearer token from google.auth — matching the tested pattern.
        """
        project  = self._cfg.gcp.project_id
        nic_name = self._cfg.gcp.nic_name

        url = (
            f"{_COMPUTE_BASE}/projects/{project}/zones/{zone}"
            f"/instances/{instance_name}/updateNetworkInterface"
            f"?networkInterface={nic_name}"
        )
        headers = {
            "Authorization": f"Bearer {self._fresh_token()}",
            "Content-Type":  "application/json",
        }
        payload = {
            "aliasIpRanges": alias_ranges,
            "fingerprint":   nic.fingerprint,
        }

        log.debug("PATCH %s  payload=%s", url, json.dumps(payload))
        resp = requests.patch(url, headers=headers, json=payload, timeout=30)

        log.debug("PATCH status=%d  body=%s", resp.status_code, resp.text)
        if resp.status_code != 200:
            raise RuntimeError(
                f"PATCH updateNetworkInterface failed "
                f"(HTTP {resp.status_code}): {resp.text}")

        op_name = resp.json().get("name")
        if op_name:
            self._wait_zone_op(op_name, zone)

    # ------------------------------------------------------------------
    # Operation poller
    # ------------------------------------------------------------------

    def _wait_zone_op(self, op_name: str, zone: str,
                      poll_interval: float = 3.0) -> None:
        """Poll a zonal LRO until DONE or timeout."""
        project  = self._cfg.gcp.project_id
        timeout  = self._cfg.gcp.api_timeout
        deadline = time.monotonic() + timeout

        log.debug("[WAIT] operation %s", op_name)
        while time.monotonic() < deadline:
            op = self._zone_ops_client.get(
                project   = project,
                zone      = zone,
                operation = op_name,
            )
            log.debug("[WAIT] status=%s", op.status)
            if op.status == "DONE":
                if op.error:
                    errors = "; ".join(e.message for e in op.error.errors)
                    raise RuntimeError(
                        f"GCP operation {op_name} failed: {errors}")
                return
            time.sleep(poll_interval)

        raise TimeoutError(
            f"GCP operation {op_name} did not complete in {timeout}s")

    # ------------------------------------------------------------------
    # Post-operation verification
    # ------------------------------------------------------------------

    def _verify_alias_present(
            self, instance_name: str, zone: str, cidr: str) -> None:
        """Confirm the alias IP is visible after the PATCH operation."""
        log.debug("[VERIFY] checking %s present on %s", cidr, instance_name)
        nic = self._get_nic(instance_name, zone)
        actual = [a.ip_cidr_range for a in nic.alias_ip_ranges]
        if cidr in actual:
            log.info("✓ VIP %s confirmed present on %s", cidr, instance_name)
        else:
            log.warning("⚠ VIP %s NOT found on %s after add (got: %s)",
                        cidr, instance_name, actual)

    def _verify_alias_absent(
            self, instance_name: str, zone: str, cidr: str) -> None:
        """Confirm the alias IP has been removed after the PATCH operation."""
        log.debug("[VERIFY] checking %s absent on %s", cidr, instance_name)
        nic = self._get_nic(instance_name, zone)
        actual = [a.ip_cidr_range for a in nic.alias_ip_ranges]
        if cidr not in actual:
            log.info("✓ VIP %s confirmed removed from %s", cidr, instance_name)
        else:
            log.warning("⚠ VIP %s still present on %s after remove",
                        cidr, instance_name)
