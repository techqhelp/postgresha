"""
config.py — Configuration loader for pgha.

Reads the INI-style pgha.conf and exposes a typed Config dataclass.
All modules receive a single Config instance; never re-read the file at runtime.
"""

import configparser
import os
from dataclasses import dataclass, field
from typing import Optional

DEFAULT_CFG = "/etc/pgha/pgha.conf"


@dataclass
class ClusterCfg:
    name: str
    node_name: str
    peer_node: str
    peer_ip: str
    heartbeat_port: int
    heartbeat_interval: float
    dead_interval: float
    quorum_timeout: float
    fence_wait: float
    peer_auth_token: str
    maintenance_file: str


@dataclass
class GcpCfg:
    project_id: str
    region: str
    zone_primary: str
    zone_standby: str
    disk_name: str
    disk_device_name: str
    disk_mount_point: str
    disk_fs_type: str
    instance_primary: str
    instance_standby: str
    nic_name: str
    vip_address: str
    vip_cidr: str
    service_account_key: Optional[str]
    api_timeout: int


@dataclass
class PostgreSQLCfg:
    host: str
    port: int
    user: str
    database: str
    data_dir: str
    pg_ctl: str
    pg_isready: str
    pg_os_user: str       # OS user that owns the EDB/PG process (e.g. enterprisedb)
    pg_start_timeout: int
    pg_stop_timeout: int


@dataclass
class MonitorCfg:
    check_interval: float
    pg_response_timeout: float
    pg_fail_threshold: int
    os_cpu_threshold: float
    os_mem_threshold: float
    os_disk_threshold: float
    os_fail_count: int


@dataclass
class LoggingCfg:
    level: str
    file: str
    max_bytes: int
    backup_count: int


@dataclass
class EfmCfg:
    enabled: bool
    service_name: str


@dataclass
class ApiCfg:
    socket_path: str


@dataclass
class Config:
    cluster: ClusterCfg
    gcp: GcpCfg
    postgresql: PostgreSQLCfg
    monitor: MonitorCfg
    logging: LoggingCfg
    api: ApiCfg
    efm: EfmCfg

    # Derived helpers
    @property
    def is_primary_zone(self) -> bool:
        """True when this node is configured as the primary zone node."""
        return self.cluster.node_name == self.gcp.instance_primary

    @property
    def my_zone(self) -> str:
        if self.cluster.node_name == self.gcp.instance_primary:
            return self.gcp.zone_primary
        return self.gcp.zone_standby

    @property
    def peer_zone(self) -> str:
        if self.cluster.node_name == self.gcp.instance_primary:
            return self.gcp.zone_standby
        return self.gcp.zone_primary


def load(path: str = DEFAULT_CFG) -> Config:
    """Parse *path* and return a populated Config instance."""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"pgha config not found: {path}")

    p = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    p.read(path)

    def _get(section: str, key: str, fallback=None):
        return p.get(section, key, fallback=fallback)

    cluster = ClusterCfg(
        name=_get("cluster", "name"),
        node_name=_get("cluster", "node_name"),
        peer_node=_get("cluster", "peer_node"),
        peer_ip=_get("cluster", "peer_ip"),
        heartbeat_port=int(_get("cluster", "heartbeat_port", fallback=7777)),
        heartbeat_interval=float(_get("cluster", "heartbeat_interval", fallback=1)),
        dead_interval=float(_get("cluster", "dead_interval", fallback=5)),
        quorum_timeout=float(_get("cluster", "quorum_timeout", fallback=10)),
        fence_wait=float(_get("cluster", "fence_wait", fallback=8)),
        peer_auth_token=_get("cluster", "peer_auth_token", fallback=""),
        maintenance_file=_get("cluster", "maintenance_file",
                              fallback="/var/run/pgha/maintenance"),
    )

    sak = _get("gcp", "service_account_key", fallback="").strip() or None
    gcp = GcpCfg(
        project_id=_get("gcp", "project_id"),
        region=_get("gcp", "region"),
        zone_primary=_get("gcp", "zone_primary"),
        zone_standby=_get("gcp", "zone_standby"),
        disk_name=_get("gcp", "disk_name"),
        disk_device_name=_get("gcp", "disk_device_name"),
        disk_mount_point=_get("gcp", "disk_mount_point"),
        disk_fs_type=_get("gcp", "disk_fs_type", fallback="xfs"),
        instance_primary=_get("gcp", "instance_primary"),
        instance_standby=_get("gcp", "instance_standby"),
        nic_name=_get("gcp", "nic_name", fallback="nic0"),
        vip_address=_get("gcp", "vip_address"),
        vip_cidr=_get("gcp", "vip_cidr"),
        service_account_key=sak,
        api_timeout=int(_get("gcp", "api_timeout", fallback=120)),
    )

    postgresql = PostgreSQLCfg(
        host=_get("postgresql", "host", fallback="/var/run/edb/as15"),
        port=int(_get("postgresql", "port", fallback=5444)),
        user=_get("postgresql", "user", fallback="enterprisedb"),
        database=_get("postgresql", "database", fallback="edb"),
        data_dir=_get("postgresql", "data_dir"),
        pg_ctl=_get("postgresql", "pg_ctl", fallback="/usr/edb/as15/bin/pg_ctl"),
        pg_isready=_get("postgresql", "pg_isready", fallback="/usr/edb/as15/bin/pg_isready"),
        pg_os_user=_get("postgresql", "pg_os_user", fallback="enterprisedb"),
        pg_start_timeout=int(_get("postgresql", "pg_start_timeout", fallback=60)),
        pg_stop_timeout=int(_get("postgresql", "pg_stop_timeout", fallback=30)),
    )

    monitor = MonitorCfg(
        check_interval=float(_get("monitor", "check_interval", fallback=2)),
        pg_response_timeout=float(_get("monitor", "pg_response_timeout", fallback=5)),
        pg_fail_threshold=int(_get("monitor", "pg_fail_threshold", fallback=3)),
        os_cpu_threshold=float(_get("monitor", "os_cpu_threshold", fallback=95)),
        os_mem_threshold=float(_get("monitor", "os_mem_threshold", fallback=95)),
        os_disk_threshold=float(_get("monitor", "os_disk_threshold", fallback=90)),
        os_fail_count=int(_get("monitor", "os_fail_count", fallback=5)),
    )

    logging_cfg = LoggingCfg(
        level=_get("logging", "level", fallback="INFO"),
        file=_get("logging", "file", fallback="/var/log/pgha/pgha.log"),
        max_bytes=int(_get("logging", "max_bytes", fallback=104857600)),
        backup_count=int(_get("logging", "backup_count", fallback=10)),
    )

    api = ApiCfg(
        socket_path=_get("api", "socket_path", fallback="/var/run/pgha/pgha.sock"),
    )

    efm = EfmCfg(
        enabled=_get("efm", "enabled", fallback="false").lower() in ("true", "1", "yes"),
        service_name=_get("efm", "service_name", fallback="edb-efm-4.7"),
    )

    return Config(
        cluster=cluster,
        gcp=gcp,
        postgresql=postgresql,
        monitor=monitor,
        logging=logging_cfg,
        api=api,
        efm=efm,
    )
