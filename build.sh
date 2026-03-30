#!/bin/bash
# =============================================================================
# build.sh — Build the pgha RPM package
#
# Usage:
#   ./build.sh                     # build RPM
#   ./build.sh --clean             # remove build artefacts
#   ./build.sh --srpm              # build SRPM only
#
# Prerequisites (on the build host):
#   sudo dnf install -y rpm-build python3-setuptools python3-devel
#
# The resulting RPM will be in:
#   ~/rpmbuild/RPMS/noarch/pgha-1.0.0-1.<dist>.noarch.rpm
# =============================================================================

set -euo pipefail

NAME="pgha"
VERSION="1.0.0"
RELEASE="1"
TARBALL="${NAME}-${VERSION}.tar.gz"

TOPDIR="${HOME}/rpmbuild"
SOURCES="${TOPDIR}/SOURCES"
SPECS="${TOPDIR}/SPECS"

# ---- Parse arguments -------------------------------------------------------
CLEAN=false
SRPM_ONLY=false
for arg in "$@"; do
    case $arg in
        --clean)  CLEAN=true  ;;
        --srpm)   SRPM_ONLY=true ;;
        *)        echo "Unknown argument: $arg"; exit 1 ;;
    esac
done

# ---- Clean -----------------------------------------------------------------
if $CLEAN; then
    echo "Cleaning build artefacts..."
    rm -rf "${TOPDIR}/BUILD/${NAME}-${VERSION}"
    rm -f  "${SOURCES}/${TARBALL}"
    rm -f  "${SPECS}/pgha.spec"
    echo "Done."
    exit 0
fi

# ---- Prepare RPM build tree ------------------------------------------------
echo "=== pgha RPM build starting ==="
echo "Version: ${VERSION}-${RELEASE}"

mkdir -p "${SOURCES}" "${SPECS}" \
    "${TOPDIR}/BUILD" "${TOPDIR}/BUILDROOT" \
    "${TOPDIR}/RPMS"  "${TOPDIR}/SRPMS"

# ---- Create source tarball -------------------------------------------------
echo "Creating source tarball: ${TARBALL}"

TMPDIR=$(mktemp -d)
trap "rm -rf ${TMPDIR}" EXIT

# Copy project tree into a versioned directory for the tarball
mkdir -p "${TMPDIR}/${NAME}-${VERSION}"
cp -r \
    src         \
    bin         \
    conf        \
    systemd     \
    setup.py    \
    requirements.txt \
    "${TMPDIR}/${NAME}-${VERSION}/"

# Create the tarball
tar -czf "${SOURCES}/${TARBALL}" \
    -C "${TMPDIR}" \
    "${NAME}-${VERSION}"

echo "Tarball created: ${SOURCES}/${TARBALL}"

# ---- Copy spec file --------------------------------------------------------
cp pgha.spec "${SPECS}/pgha.spec"

# ---- Build -----------------------------------------------------------------
if $SRPM_ONLY; then
    echo "Building SRPM..."
    rpmbuild -bs \
        --define "_topdir ${TOPDIR}" \
        "${SPECS}/pgha.spec"
    echo "=== SRPM build complete ==="
    ls -lh "${TOPDIR}/SRPMS/"
else
    echo "Building RPM..."
    rpmbuild -bb \
        --define "_topdir ${TOPDIR}" \
        "${SPECS}/pgha.spec"
    echo "=== RPM build complete ==="
    echo ""
    echo "Output RPM:"
    find "${TOPDIR}/RPMS" -name "*.rpm" -newer "${SOURCES}/${TARBALL}" \
        | while read rpm; do
            echo "  ${rpm}"
            rpm -qpi "${rpm}" | grep -E "^(Name|Version|Summary|Size)"
        done
    echo ""
    echo "Install on target host:"
    echo "  sudo rpm -ivh <rpm-file>"
    echo "  sudo vi /etc/pgha/pgha.conf"
    echo "  sudo systemctl enable --now pgha"
fi
