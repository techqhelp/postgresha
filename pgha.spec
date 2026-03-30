# =============================================================================
# pgha.spec — RPM Spec for pgha PostgreSQL HA Heartbeat Manager (GCP RPD)
#
# Build:
#   rpmbuild -bb pgha.spec
#
# Or use the build.sh wrapper which sets up the build tree automatically.
#
# Target distros:
#   RHEL / CentOS / Rocky Linux / AlmaLinux 8 or 9
#   Python 3.9+ required (ships in OS base)
# =============================================================================

Name:           pgha
Version:        1.0.0
Release:        1%{?dist}
Summary:        PostgreSQL HA Heartbeat Manager for GCP Regional Persistent Disk
License:        MIT
URL:            https://github.com/yourorg/pgha
Source0:        %{name}-%{version}.tar.gz

BuildArch:      noarch
BuildRequires:  python3-devel
BuildRequires:  python3-setuptools

# ---- Runtime requirements ------------------------------------------------
# python3 base
Requires:       python3 >= 3.9
# psutil — OS monitoring
Requires:       python3-psutil
# psycopg2 — PostgreSQL driver
Requires:       python3-psycopg2
# GCP SDK libraries (installed via pip if not available as RPMs)
# google-cloud-compute and google-auth are pulled from PyPI in %install
# util-linux for mount/umount
Requires:       util-linux
# systemd for service management
Requires(post): systemd
Requires(preun): systemd
Requires(postun): systemd

# ---- Conflicts -----------------------------------------------------------
# pgha replaces the need for pacemaker+corosync in a 2-node GCP deployment
Conflicts:      pacemaker
Conflicts:      corosync

%description
pgha is a lightweight PostgreSQL High Availability heartbeat manager
designed for Google Cloud Platform.

It provides:
  - UDP heartbeat monitoring between two nodes across GCP zones
  - Health monitoring of PostgreSQL and OS resources (CPU/mem/disk)
  - Automatic failover using GCP Regional Persistent Disk (RPD) fencing:
      * Force-detaches the RPD from a failed primary (STONITH equivalent)
      * Attaches RPD to standby, mounts filesystem, starts PostgreSQL
  - Planned switchover with zero-data-loss ordered hand-off
  - Floating internal IP (alias IP) management via GCP Compute API
  - CLI tool (pgha-ctl) for status, switchover, and diagnostics
  - Systemd service with automatic restart

Architecture:
  Two GCP VMs in different zones of the same region share a Regional
  Persistent Disk.  Only the PRIMARY attaches the disk in RW mode and
  runs PostgreSQL.  The STANDBY monitors heartbeats; on failure it fences
  the primary and takes over the disk + VIP within seconds.

%prep
%setup -q

%build
# Nothing to compile — pure Python

%install
# Create directory layout
install -d %{buildroot}/usr/bin
install -d %{buildroot}/etc/pgha
install -d %{buildroot}/var/log/pgha
install -d %{buildroot}/var/run/pgha
install -d %{buildroot}%{_unitdir}
install -d %{buildroot}%{python3_sitelib}/pgha
install -d %{buildroot}%{python3_sitelib}/pgha/monitor
install -d %{buildroot}%{python3_sitelib}/pgha/gcp
install -d %{buildroot}%{python3_sitelib}/pgha/cluster
install -d %{buildroot}%{python3_sitelib}/pgha/ha

# Install Python package
cp -r src/pgha/* %{buildroot}%{python3_sitelib}/pgha/

# Install executables
install -m 0755 bin/pgha-daemon  %{buildroot}/usr/bin/pgha-daemon
install -m 0755 bin/pgha-ctl     %{buildroot}/usr/bin/pgha-ctl

# Install config (marked as noreplace so upgrades don't overwrite it)
# Defaults point to EDB Postgres Advanced Server 15 paths
install -m 0640 conf/pgha.conf   %{buildroot}/etc/pgha/pgha.conf

# Install systemd unit
install -m 0644 systemd/pgha.service  %{buildroot}%{_unitdir}/pgha.service

# Install GCP Python dependencies via pip into the sitelib
# (These are not packaged as OS RPMs in standard repos)
pip3 install --no-deps --target=%{buildroot}%{python3_sitelib} \
    google-cloud-compute \
    google-auth \
    || true

%post
# Reload systemd and enable the service
%systemd_post pgha.service
echo "pgha installed.  Configure /etc/pgha/pgha.conf then run:"
echo "  systemctl enable --now pgha"

%preun
%systemd_preun pgha.service

%postun
%systemd_postun_with_restart pgha.service
# Clean up runtime socket on full uninstall
if [ $1 -eq 0 ]; then
    rm -f /var/run/pgha/pgha.sock
fi

%files
# Executables
%attr(0755, root, root) /usr/bin/pgha-daemon
%attr(0755, root, root) /usr/bin/pgha-ctl

# Config (noreplace preserves admin edits on upgrade)
%config(noreplace) %attr(0640, root, root) /etc/pgha/pgha.conf

# Systemd unit
%{_unitdir}/pgha.service

# Python package
%{python3_sitelib}/pgha/

# Runtime and log directories (owned by root; daemon runs as root)
%dir %attr(0750, root, root) /var/run/pgha
%dir %attr(0755, root, root) /var/log/pgha

%changelog
* Fri Mar 27 2026 pgha contributors <pgha@example.com> - 1.0.0-1
- Initial release
- UDP heartbeat manager between two GCP zones
- RPD-based disk fencing (STONITH equivalent)
- Automatic failover and planned switchover
- GCP alias IP VIP management
- PostgreSQL and OS health monitoring
- systemd service integration
