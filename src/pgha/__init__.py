"""
pgha — PostgreSQL High Availability Heartbeat Manager for GCP
Provides automated failover and switchover using:
  - Regional Persistent Disk (RPD) for storage replication
  - Private IP UDP heartbeat between nodes
  - GCP Compute API for disk fencing and alias-IP VIP management
"""

__version__ = "1.0.0"
__author__  = "pgha contributors"
