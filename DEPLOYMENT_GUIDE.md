# pgha Deployment Guide
## PostgreSQL HA Heartbeat Manager — GCP Regional Persistent Disk

---

## Overview

```
┌─────────────────────────┐          ┌─────────────────────────┐
│  pg-primary  (zone-a)   │          │  pg-standby  (zone-b)   │
│                         │◄────────►│                         │
│  pgha daemon (PRIMARY)  │  UDP     │  pgha daemon (STANDBY)  │
│  EPAS 15 running        │  :7777   │  EPAS 15 stopped        │
│  RPD attached RW        │          │  RPD detached           │
│  VIP: 10.0.0.100        │          │  monitoring heartbeat   │
└─────────────────────────┘          └─────────────────────────┘
           │                                      │
           └──────────────────────────────────────┘
                    Regional Persistent Disk
                    (zone-replicated block device)
```

- **PRIMARY** owns the RPD (read-write), runs EPAS 15, holds the alias IP (VIP).  
- **STANDBY** monitors heartbeat. On peer failure it first **verifies peer state via the GCP Compute API** (independent of VPC). Only if the peer is confirmed dead or successfully fenced does it attach the disk, start EPAS 15, and move the VIP.  
- **Network partition (both VMs alive, VPC only)**: standby queries GCP API to confirm peer is `RUNNING`, then executes STONITH (disk force-detach). Primary loses disk I/O → PostgreSQL crashes → standby takes over. If the GCP API itself is unreachable, **no failover happens** — primary keeps running safely.  
- Clients always connect to the **VIP (10.0.0.100)**; they are unaffected by which VM owns it.

---

## Quick Install (3 Steps per Node)

Once GCP infrastructure is provisioned (Steps 1–3 below), installation on each VM is:

**Step 1 — Run the installer** (on both pg-primary and pg-standby)
```bash
# Upload the project to the VM first (see Step 4a), then:
sudo bash ~/pgha/scripts/install.sh
```

**Step 2 — Edit the config** (different value on each node)
```bash
sudo vi /etc/pgha/pgha.conf
```
Minimum changes:
| Setting | pg-primary | pg-standby |
|---|---|---|
| `node_name` | `pg-primary` | `pg-standby` |
| `peer_ip` | `10.0.0.3` *(standby IP)* | `10.0.0.2` *(primary IP)* |
| `project_id` | your GCP project | same |
| `region` | your region | same |

**Step 3 — Start the service** (standby first, then primary)
```bash
sudo systemctl enable pgha
sudo systemctl start pgha
```

> The full step-by-step walkthrough for GCP infrastructure and replication setup follows below.

---

## Prerequisites

### GCP Infrastructure

| Resource | Requirement |
|---|---|
| Two Compute Engine VMs | Same region, different zones (e.g., `us-central1-a`, `us-central1-b`) |
| Regional Persistent Disk | Created in the same region, formatted XFS or EXT4 |
| VPC Subnet | Both VMs in the same subnet; VIP address (`10.0.0.100`) must be within the subnet CIDR |
| Firewall rule | Allow UDP port **7777** between the two VM internal IPs (heartbeat) |
| Firewall rule | Allow TCP port **7778** between the two VM internal IPs (switchover + PG fail handoff) |
| Firewall rule (multi-cluster) | Additional UDP/TCP port pairs for each extra cluster (e.g., 7779/7780, 7781/7782) |
| IAM / Service Account | Compute Instance Admin or custom role (see step 2) |

### Both VMs

- RHEL / Rocky Linux / AlmaLinux **8 or 9**
- Python **3.6.8+** (already on RHEL 8)
- EDB Postgres Advanced Server **15** installed
- `git` installed (to clone/upload the package)

---

## Step 1 — Create the Regional Persistent Disk

Run from your local machine or Cloud Shell. **Do this once.**

```bash
gcloud compute disks create pg-regional-disk \
  --type=pd-ssd \
  --size=100GB \
  --region=us-central1 \
  --replica-zones=us-central1-a,us-central1-b \
  --project=my-gcp-project
```

Format the disk **once** from the primary node (before pgha is installed):

```bash
# SSH into pg-primary
# Attach it manually for the first time
gcloud compute instances attach-disk pg-primary \
  --disk=pg-regional-disk \
  --disk-scope=regional \
  --zone=us-central1-a \
  --project=my-gcp-project

# On pg-primary — format (do this ONLY ONCE)
sudo mkfs.xfs /dev/disk/by-id/google-pg-data-disk

# Create the mount point
sudo mkdir -p /pgdata
sudo mount /dev/disk/by-id/google-pg-data-disk /pgdata

# Initialize EDB data directory on the disk
sudo chown -R enterprisedb:enterprisedb /pgdata
sudo -u enterprisedb /usr/edb/as15/bin/initdb -D /pgdata

# Unmount — pgha will mount it at startup
sudo umount /pgdata

# Detach the disk — pgha will attach it at startup
gcloud compute instances detach-disk pg-primary \
  --disk=pg-regional-disk \
  --disk-scope=regional \
  --zone=us-central1-a \
  --project=my-gcp-project
```

---

## Step 2 — Configure GCP IAM Permissions

pgha calls the GCP Compute API to attach/detach disks and move alias IPs.

### Option A: Use the VM's Default Service Account (Recommended)

Grant the Compute Engine default service account the following roles on your project:

```bash
# Replace PROJECT_NUMBER with your actual project number
SA="PROJECT_NUMBER-compute@developer.gserviceaccount.com"
PROJECT="my-gcp-project"

gcloud projects add-iam-policy-binding $PROJECT \
  --member="serviceAccount:$SA" \
  --role="roles/compute.instanceAdmin.v1"
```

Set `service_account_key =` (leave empty) in `pgha.conf` to use Workload Identity.

### Option B: Use a Dedicated Service Account JSON Key

```bash
# Create service account
gcloud iam service-accounts create pgha-sa \
  --display-name="pgha HA Manager" \
  --project=my-gcp-project

# Grant role
gcloud projects add-iam-policy-binding my-gcp-project \
  --member="serviceAccount:pgha-sa@my-gcp-project.iam.gserviceaccount.com" \
  --role="roles/compute.instanceAdmin.v1"

# Create and download key
gcloud iam service-accounts keys create /etc/pgha/pgha-sa-key.json \
  --iam-account=pgha-sa@my-gcp-project.iam.gserviceaccount.com

chmod 600 /etc/pgha/pgha-sa-key.json
```

Then set in `pgha.conf`:
```ini
service_account_key = /etc/pgha/pgha-sa-key.json
```

---

## Step 3 — Configure Alias IP (Floating VIP) on Both VMs

The VIP (`10.0.0.100`) is an alias IP on the primary's NIC. It must exist as a valid CIDR in the subnet.

```bash
# Add the alias IP to pg-primary's NIC (initial state — primary owns VIP)
gcloud compute instances network-interfaces update pg-primary \
  --zone=us-central1-a \
  --aliases="10.0.0.100/32" \
  --project=my-gcp-project
```

**Do NOT add the alias IP to pg-standby** — pgha moves it automatically during failover.

---

## Step 4 — Install pgha on Both VMs

Perform **Steps 4–6 on both pg-primary and pg-standby** unless noted otherwise.

### 4a. Upload the project to each VM

```bash
# From your local Windows machine — copy the project to each server
gcloud compute scp --recurse d:\landisgyr\healthcheck pg-primary:~/pgha \
  --zone=us-central1-a --project=my-gcp-project

gcloud compute scp --recurse d:\landisgyr\healthcheck pg-standby:~/pgha \
  --zone=us-central1-b --project=my-gcp-project
```

### 4b. Run the installer on each VM

SSH into each node and run:

```bash
sudo bash ~/pgha/scripts/install.sh
```

The script will:
- Install all Python dependencies (`psutil`, `psycopg2-binary`, `google-auth`, `requests`, `dataclasses` backport for Python 3.6)
- Install the pgha Python package via `setup.py`
- Copy binaries to `/usr/bin/pgha-daemon` and `/usr/bin/pgha-ctl`
- Install `/etc/pgha/pgha.conf` (only if not already present)
- Install `/etc/systemd/system/pgha.service` and `/etc/systemd/system/pgha@.service` (template unit for multi-cluster)
- Create `/var/log/pgha/` and `/var/run/pgha/`
- Disable and stop the built-in `edb-as-15` service
- Reload systemd

At the end it prints the exact settings to change in Step 5.

---

## Step 5 — Edit pgha.conf on Each VM

`sudo vi /etc/pgha/pgha.conf`

### On pg-primary (`/etc/pgha/pgha.conf`):

```ini
[cluster]
node_name   = pg-primary
peer_node   = pg-standby
peer_ip     = 10.0.0.3          # ← pg-standby's internal private IP
heartbeat_port    = 7777
peer_port         = 7778        # TCP port for inter-node commands
heartbeat_interval = 1
dead_interval = 5
fence_wait    = 3          # brief settle after fence; GCP API is synchronous

[gcp]
project_id        = my-gcp-project
region            = us-central1
zone_primary      = us-central1-a
zone_standby      = us-central1-b
disk_name         = pg-regional-disk
disk_device_name  = pg-data-disk
disk_mount_point  = /pgdata
disk_fs_type      = xfs
instance_primary  = pg-primary
instance_standby  = pg-standby
nic_name          = nic0
vip_address       = 10.0.0.100
vip_cidr          = 10.0.0.100/32
service_account_key =           # empty = use Workload Identity
api_timeout       = 30         # GCP API call timeout in seconds

[postgresql]
host              = /var/run/edb/as15
port              = 5444
user              = enterprisedb
database          = edb
pg_ctl            = /usr/edb/as15/bin/pg_ctl
pg_isready        = /usr/edb/as15/bin/pg_isready
data_dir          = /pgdata
pg_os_user        = enterprisedb
```

### On pg-standby (`/etc/pgha/pgha.conf`):

Change only two lines from the primary config:

```ini
[cluster]
node_name   = pg-standby        # ← this node's name
peer_node   = pg-primary
peer_ip     = 10.0.0.2          # ← pg-primary's internal private IP
```

Everything else stays the same.

---

## Step 6 — Configure EDB EPAS 15 for Streaming Replication

pgha manages disk and VIP. EDB streaming replication is handled separately.

### On pg-primary

Edit `/pgdata/postgresql.conf`:

```
listen_addresses = '*'
wal_level = replica
max_wal_senders = 5
wal_keep_size = 512
hot_standby = on
```

Edit `/pgdata/pg_hba.conf` — add replication entry:

```
host  replication  replicator  10.0.0.0/24  md5
```

Create replication user:

```bash
sudo -u enterprisedb /usr/edb/as15/bin/psql -p 5444 -d edb -c \
  "CREATE USER replicator REPLICATION LOGIN PASSWORD 'replpass';"
```

### On pg-standby

Create the standby using `pg_basebackup` (run while primary is up):

```bash
sudo -u enterprisedb /usr/edb/as15/bin/pg_basebackup \
  -h 10.0.0.2 \
  -U replicator \
  -D /pgdata \
  -P -R -Xs
```

The `-R` flag automatically creates `standby.signal` and writes the `primary_conninfo` into `postgresql.auto.conf`.

---

## Step 7 — Open Firewall Ports

```bash
gcloud compute firewall-rules create pgha-heartbeat \
  --direction=INGRESS \
  --network=default \
  --action=ALLOW \
  --rules=udp:7777,tcp:7778 \
  --source-ranges=10.0.0.0/24 \
  --description="pgha inter-node: heartbeat (UDP 7777), peer commands (TCP 7778)" \
  --project=my-gcp-project
```

> **Multi-cluster**: if running additional clusters, open their ports too
> (e.g., `udp:7779,tcp:7780` for cluster 2). You can add them to the same
> firewall rule or create separate rules per cluster.

---

## Step 8 — Disable Automatic EDB Service Start

> **Already handled by `install.sh`** — the installer disables `edb-as-15` automatically.
> Only run these manually if you skipped the installer:

```bash
sudo systemctl disable edb-as-15
sudo systemctl stop edb-as-15
```

---

## Step 9 — Start pgha

Start **pg-standby first**, then **pg-primary**. This is important — both nodes race to attach the disk; whichever wins becomes PRIMARY.

```bash
# On pg-standby first
sudo systemctl enable pgha
sudo systemctl start pgha

# Wait 3 seconds, then start on pg-primary
sudo systemctl enable pgha
sudo systemctl start pgha
```

pg-primary will win the disk election and become PRIMARY.

---

## Step 10 — Verify the Cluster

### Check status on either node

```bash
pgha-ctl status
```

Expected output on primary:

```
NODE:    pg-primary
ROLE:    PRIMARY
HEALTH:  HEALTHY
PEER:    pg-standby (ALIVE)

POSTGRES:
  Status:       running
  Replication:  streaming (1 replicas connected)
  Lag:          0 bytes

DISK:    attached (RW) at /pgdata
VIP:     10.0.0.100 — HELD

HEARTBEAT: last rx 0.4s ago
```

Expected output on standby:

```
NODE:    pg-standby
ROLE:    STANDBY
HEALTH:  HEALTHY
PEER:    pg-primary (ALIVE)

POSTGRES:
  Status:       stopped (standby mode)

DISK:    detached
VIP:     10.0.0.100 — NOT HELD
```

### Check the daemon log

```bash
sudo journalctl -u pgha -f
# or
sudo tail -f /var/log/pgha/pgha.log
```

---

## Step 11 — Test Failover

> **Warning**: This will interrupt PostgreSQL briefly. Do it in a maintenance window.

### Simulate primary failure (power off pg-primary)

```bash
# From Cloud Console or gcloud — stop the primary VM
gcloud compute instances stop pg-primary \
  --zone=us-central1-a --project=my-gcp-project
```

Watch on pg-standby:

```bash
sudo journalctl -u pgha -f
```

Expected sequence on standby log:

**Case A — peer VM is stopped/dead:**
```
[INFO]  heartbeat: pg-primary missed 3 beats — marking DEAD
[INFO]  failover: starting automatic failover (attempt 1/3)
[INFO]  failover: VERIFY — querying GCP API for pg-primary instance status
[INFO]  failover: VERIFY — pg-primary is TERMINATED (confirmed DOWN via GCP API)
[INFO]  failover: FENCE — detaching orphaned disk (best-effort)
[INFO]  failover: fence settle wait: 3 seconds
[INFO]  failover: ATTACH — pg-regional-disk attached to pg-standby (RW)
[INFO]  failover: MOUNT — /pgdata mounted
[INFO]  failover: PG_START — EDB EPAS 15 started (crash recovery if needed)
[INFO]  failover: VIP — 10.0.0.100 moved to pg-standby
[INFO]  failover: PROMOTE — pg-standby is now PRIMARY
```

**Case B — network partition (peer VM is still RUNNING):**
```
[INFO]  heartbeat: pg-primary missed 3 beats — marking DEAD
[INFO]  failover: starting automatic failover (attempt 1/3)
[INFO]  failover: VERIFY — querying GCP API for pg-primary instance status
[WARN]  failover: VERIFY — pg-primary is RUNNING — network partition detected
[WARN]  failover: FENCE (STONITH) — force-detaching disk, waiting for GCP confirmation
         (primary loses disk I/O → PostgreSQL crashes → self-demotes)
[INFO]  failover: fence settle wait: 3 seconds
[INFO]  failover: ATTACH — pg-regional-disk attached to pg-standby (RW)
[INFO]  failover: MOUNT — /pgdata mounted
[INFO]  failover: PG_START — EDB EPAS 15 started
[INFO]  failover: VIP — 10.0.0.100 moved to pg-standby
[INFO]  failover: PROMOTE — pg-standby is now PRIMARY
```

**Case C — network partition AND GCP API unreachable (full outage):**
```
[INFO]  heartbeat: pg-primary missed 3 beats — marking DEAD
[INFO]  failover: starting automatic failover (attempt 1/3)
[INFO]  failover: VERIFY — querying GCP API for pg-primary instance status
[ERROR] failover: VERIFY — GCP API unreachable — cannot verify peer state
         Aborting failover to prevent split-brain.
         Primary continues running.
[ERROR] failover: failed at step VERIFY — attempt aborted
```
No failover happens. Primary keeps serving traffic safely.

**Case D — PostgreSQL crashes on primary, server VM still running:**

To test this, SSH into pg-primary and kill EPAS:
```bash
sudo -u enterprisedb /usr/edb/as15/bin/pg_ctl stop -D /pgdata -m immediate
```

Expected on pg-primary log (self-demotion):
```
[WARN]  monitor: PostgreSQL check FAILED (1/3)
[WARN]  monitor: PostgreSQL check FAILED (2/3)
[WARN]  monitor: PostgreSQL check FAILED (3/3)
[ERROR] daemon: PostgreSQL failed 3 times on PRIMARY — self-demoting
[WARN]  daemon: self-demoting PRIMARY after PostgreSQL failure
[INFO]  daemon: stop_postgres (best-effort — already dead)
[INFO]  daemon: unmount_disk /pgdata
[INFO]  disk: detaching pg-regional-disk from pg-primary
[INFO]  network: releasing VIP 10.0.0.100
[INFO]  daemon: self-demotion complete — now STANDBY
[INFO]  daemon: sending PG_FAIL_HANDOFF to peer 10.0.0.3:7778
```

Expected on pg-standby log (automatic failover via FailoverEngine):
```
[WARN]  peer-tcp: PG_FAIL_HANDOFF from pg-primary — starting FailoverEngine
[INFO]  failover: starting automatic failover (attempt 1/3)
[INFO]  failover: VERIFY — querying GCP API for pg-primary instance status
[INFO]  failover: VERIFY — pg-primary is RUNNING (VM alive, only PG crashed)
         Proceeding: disk already released by primary's self-demotion.
[INFO]  failover: FENCE — disk already detached (best-effort, skipped)
[INFO]  failover: fence settle wait: 3 seconds
[INFO]  failover: ATTACH — pg-regional-disk attached to pg-standby (RW)
[INFO]  failover: MOUNT — /pgdata mounted
[INFO]  failover: PG_START — EDB EPAS 15 started
[INFO]  failover: VIP — 10.0.0.100 moved to pg-standby
[INFO]  failover: PROMOTE — pg-standby is now PRIMARY
```

> **Note**: If the TCP notification is lost (transient network issue at that moment), the
> heartbeat fallback in the standby's main loop detects that pg-primary's heartbeat now
> shows `role=STANDBY + disk=DETACHED` and starts FailoverEngine automatically within
> one check cycle (2 seconds).

Verify from a client using the VIP:

```bash
/usr/edb/as15/bin/psql -h 10.0.0.100 -p 5444 -U enterprisedb -d edb -c "SELECT pg_is_in_recovery();"
```

Expected: `f` (false — not in recovery, meaning it is now primary).

---

## Step 12 — Run a Planned Switchover

Use this for maintenance (zero-data-loss, ordered hand-off):

```bash
# Run from EITHER node
pgha-ctl switchover
```

The daemon on the current primary will:
1. Wait for replication to catch up (zero lag)
2. Stop EPAS 15 on primary and hand off to standby
3. Release the disk and VIP
4. Standby attaches disk, starts EDB, acquires VIP, promotes
5. Old primary transitions to STANDBY role

To switch back (move primary back to pg-primary):

```bash
pgha-ctl switchover
```

---

## Common Operational Commands

```bash
# Check cluster status
pgha-ctl status

# Check cluster status as JSON (for monitoring scripts)
pgha-ctl status --json

# Initiated planned switchover
pgha-ctl switchover

# Force switchover even if replication isn't caught up (data risk)
pgha-ctl switchover --force

# Restart the daemon
sudo systemctl restart pgha

# Stop the daemon (does NOT stop EPAS 15 — postgres keeps running)
sudo systemctl stop pgha

# View live daemon log
sudo journalctl -u pgha -f

# View log file
sudo tail -200 /var/log/pgha/pgha.log
```

### Multi-cluster commands

```bash
# Target a specific cluster via its config file
pgha-ctl --config /etc/pgha/pgha-cluster2.conf status
pgha-ctl --config /etc/pgha/pgha-cluster2.conf switchover
pgha-ctl --config /etc/pgha/pgha-cluster2.conf maintenance on

# Manage multi-cluster systemd services
sudo systemctl start  pgha@cluster2
sudo systemctl stop   pgha@cluster2
sudo systemctl status pgha@cluster2
sudo journalctl -u pgha@cluster2 -f
```

---

## Troubleshooting

### pgha fails to start: "config file not found"

```bash
ls -la /etc/pgha/pgha.conf
# If missing, reinstall: sudo install -m 640 ~/pgha/conf/pgha.conf /etc/pgha/pgha.conf
```

### "google.oauth2 could not be resolved" warning in IDE

This is an IDE-only import warning. On the server it will work once dependencies are installed with `pip3 install`.

### Disk attach fails: "RESOURCE_IN_USE_BY_ANOTHER_RESOURCE"

The disk is still attached to the old primary. If the old VM is completely down, force-detach it:

```bash
gcloud compute instances detach-disk pg-primary \
  --disk=pg-regional-disk \
  --disk-scope=regional \
  --zone=us-central1-a \
  --project=my-gcp-project
```

Then restart pgha on the standby.

### VIP not moving — alias IP PATCH returns 400

Verify the VIP CIDR is within the subnet range:

```bash
gcloud compute networks subnets describe default \
  --region=us-central1 --project=my-gcp-project | grep ipCidrRange
```

The `vip_cidr` in `pgha.conf` must be within that range.

### Heartbeat not received — peer always shows DEAD

Check firewall rules allow UDP 7777:

```bash
# From pg-primary, test UDP to pg-standby
nc -u -v 10.0.0.3 7777
```

If blocked, re-run the firewall rule from Step 7.

### EPAS 15 fails to start after failover

Check the PostgreSQL log on the new primary:

```bash
sudo -u enterprisedb /usr/edb/as15/bin/pg_ctl \
  -D /pgdata -l /var/log/pgha/pg-start.log start
cat /var/log/pgha/pg-start.log
```

Most common cause: the data directory ownership is wrong. Fix:

```bash
sudo chown -R enterprisedb:enterprisedb /pgdata
```

---

## File Reference

| File | Server Path | Purpose |
|---|---|---|
| `conf/pgha.conf` | `/etc/pgha/pgha.conf` | Main configuration (single cluster) |
| `conf/pgha.conf` | `/etc/pgha/pgha-<name>.conf` | Per-cluster config (multi-cluster) |
| `bin/pgha-daemon` | `/usr/bin/pgha-daemon` | Background daemon |
| `bin/pgha-ctl` | `/usr/bin/pgha-ctl` | CLI management tool |
| `systemd/pgha.service` | `/etc/systemd/system/pgha.service` | Systemd unit (single cluster) |
| `systemd/pgha@.service` | `/etc/systemd/system/pgha@.service` | Systemd template unit (multi-cluster) |
| *(runtime)* | `/var/log/pgha/pgha.log` | Daemon log file |
| *(runtime)* | `/var/run/pgha/pgha.sock` | Unix socket for pgha-ctl |

---

## Multi-Cluster Setup

pgha supports running **multiple independent DB clusters** on the same pair of VMs. Each cluster gets its own daemon instance, config file, disk, VIP, and ports.

### Architecture (2 clusters example)

```
┌───────────────────────────────────┐    ┌───────────────────────────────────┐
│  pg-primary  (zone-a)           │    │  pg-standby  (zone-b)           │
│                                   │    │                                   │
│  pgha@cluster1  (PRIMARY)         │    │  pgha@cluster1  (STANDBY)        │
│    UDP :7777  TCP :7778           │    │    monitoring heartbeat            │
│    RPD: pg-disk-1  VIP: .100      │    │                                   │
│    PG port: 5444                  │    │                                   │
│                                   │    │                                   │
│  pgha@cluster2  (PRIMARY)         │    │  pgha@cluster2  (STANDBY)        │
│    UDP :7779  TCP :7780           │    │    monitoring heartbeat            │
│    RPD: pg-disk-2  VIP: .101      │    │                                   │
│    PG port: 5445                  │    │                                   │
└───────────────────────────────────┘    └───────────────────────────────────┘
```

Each cluster is **fully independent** — separate failover, switchover, and maintenance mode.

### Step 1 — Create a per-cluster config file

Copy the default config and edit for the new cluster:

```bash
sudo cp /etc/pgha/pgha.conf /etc/pgha/pgha-cluster2.conf
sudo vi /etc/pgha/pgha-cluster2.conf
```

**Settings that MUST be unique per cluster:**

| Setting | Cluster 1 (default) | Cluster 2 |
|---|---|---|
| `[cluster] name` | `pg-ha-cluster` | `pg-ha-cluster2` |
| `[cluster] heartbeat_port` | `7777` | `7779` |
| `[cluster] peer_port` | `7778` | `7780` |
| `[cluster] maintenance_file` | `/var/run/pgha/maintenance` | `/var/run/pgha/cluster2.maint` |
| `[gcp] disk_name` | `pg-regional-disk` | `pg-regional-disk-2` |
| `[gcp] disk_device_name` | `pg-data-disk` | `pg-data-disk-2` |
| `[gcp] disk_mount_point` | `/pgdata` | `/pgdata2` |
| `[gcp] vip_address` | `10.0.0.100` | `10.0.0.101` |
| `[gcp] vip_cidr` | `10.0.0.100/32` | `10.0.0.101/32` |
| `[postgresql] port` | `5444` | `5445` |
| `[postgresql] data_dir` | `/pgdata/as15/data` | `/pgdata2/as15/data` |
| `[logging] file` | `/var/log/pgha/pgha.log` | `/var/log/pgha/cluster2.log` |
| `[api] socket_path` | `/var/run/pgha/pgha.sock` | `/var/run/pgha/cluster2.sock` |
| `[efm] service_name` | `edb-efm-4.7` | `edb-efm-cluster2` |

> `node_name`, `peer_node`, `peer_ip`, zones, and instance names stay the same
> (same two VMs host all clusters).

### Step 2 — Provision GCP resources for the new cluster

```bash
# Create a second Regional Persistent Disk
gcloud compute disks create pg-regional-disk-2 \
  --type=pd-ssd \
  --size=100GB \
  --region=us-central1 \
  --replica-zones=us-central1-a,us-central1-b \
  --project=my-gcp-project

# Add a second alias IP to the primary's NIC
gcloud compute instances network-interfaces update pg-primary \
  --zone=us-central1-a \
  --aliases="10.0.0.100/32,10.0.0.101/32" \
  --project=my-gcp-project

# Open firewall ports for cluster 2
gcloud compute firewall-rules update pgha-heartbeat \
  --rules=udp:7777,tcp:7778,udp:7779,tcp:7780 \
  --project=my-gcp-project
```

Format and initialize the second disk (same as Step 1 in the main guide, using `/pgdata2`).

### Step 3 — Start the cluster

```bash
# On pg-standby first, then pg-primary
sudo systemctl enable pgha@cluster2
sudo systemctl start pgha@cluster2
```

The template unit `pgha@cluster2.service` reads `/etc/pgha/pgha-cluster2.conf` automatically.

### Step 4 — Manage the cluster

```bash
# Status
pgha-ctl --config /etc/pgha/pgha-cluster2.conf status

# Switchover
pgha-ctl --config /etc/pgha/pgha-cluster2.conf switchover

# Maintenance mode
pgha-ctl --config /etc/pgha/pgha-cluster2.conf maintenance on
pgha-ctl --config /etc/pgha/pgha-cluster2.conf maintenance off

# Logs
sudo journalctl -u pgha@cluster2 -f
sudo tail -f /var/log/pgha/cluster2.log
```

> **Note**: Each cluster's failover, switchover, and maintenance mode are fully independent.
> Putting cluster1 in maintenance does NOT affect cluster2.
