#!/usr/bin/env bash
# =============================================================================
# FILE    : run_upgrade.sh
# PURPOSE : Full end-to-end IOS-XR upgrade orchestration script.
#           Runs: pre_check → ansible upgrade → post_check
#
# USAGE   : ./run_upgrade.sh [options]
#
# OPTIONS :
#   -t  TARGET_VERSION   IOS-XR version to upgrade to (e.g. 25.2.1) [required]
#   -s  IMAGE_SOURCE     SFTP/FTP/SCP URI of the image               [required]
#   -f  IMAGE_FILENAME   Filename on the device                      [required]
#   -b  TESTBED          Path to testbed.yaml (default: ./pyats/testbed.yaml)
#   -i  INVENTORY        Path to Ansible inventory (default: ./inventory.ini)
#   -d  SNAPSHOT_DIR     Snapshot output dir  (default: ./snapshots)
#   -w  WAIT_SECONDS     Protocol convergence wait after reload (default: 180)
#   -n  DEVICES          Comma-separated device names to limit scope
#   -h                   Show this help
#
# EXAMPLE :
#   export NET_USERNAME=admin
#   export NET_PASSWORD=secret
#   ./run_upgrade.sh \
#     -t 25.2.1 \
#     -s "sftp://mgmt-server.local/images/ncs5500-x64-25.2.1.iso" \
#     -f "ncs5500-x64-25.2.1.iso" \
#     -b ./testbed.yaml \
#     -i ./inventory.ini
# =============================================================================

set -euo pipefail

# ─── Defaults ────────────────────────────────────────────────────────────────
TESTBED="./pyats/testbed.yaml"
INVENTORY="./inventory.ini"
SNAPSHOT_DIR="./snapshots"
CONVERGENCE_WAIT=180        # seconds to wait for BGP/OSPF/MPLS to reconverge
TARGET_VERSION=""
IMAGE_SOURCE=""
IMAGE_FILENAME=""
DEVICES_ARG=""

# ─── Colour helpers ──────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()    { echo -e "${GREEN}[INFO]${NC}  $(date '+%Y-%m-%d %H:%M:%S')  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $(date '+%Y-%m-%d %H:%M:%S')  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $(date '+%Y-%m-%d %H:%M:%S')  $*" >&2; }
section() { echo -e "\n${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"; \
            echo -e "${YELLOW}  $*${NC}"; \
            echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"; }

# ─── Usage ───────────────────────────────────────────────────────────────────
usage() {
  grep '^#' "$0" | grep -v '#!/' | sed 's/^# \?//'
  exit 0
}

# ─── Argument parsing ────────────────────────────────────────────────────────
while getopts "t:s:f:b:i:d:w:n:h" opt; do
  case $opt in
    t) TARGET_VERSION="$OPTARG" ;;
    s) IMAGE_SOURCE="$OPTARG"   ;;
    f) IMAGE_FILENAME="$OPTARG" ;;
    b) TESTBED="$OPTARG"        ;;
    i) INVENTORY="$OPTARG"      ;;
    d) SNAPSHOT_DIR="$OPTARG"   ;;
    w) CONVERGENCE_WAIT="$OPTARG" ;;
    n) DEVICES_ARG="$OPTARG"    ;;
    h) usage                    ;;
    *) error "Unknown option -$OPTARG"; exit 1 ;;
  esac
done

# ─── Validate required args ──────────────────────────────────────────────────
ERRORS=0
[[ -z "$TARGET_VERSION" ]] && { error "-t TARGET_VERSION is required"; ERRORS=$((ERRORS+1)); }
[[ -z "$IMAGE_SOURCE"   ]] && { error "-s IMAGE_SOURCE is required";   ERRORS=$((ERRORS+1)); }
[[ -z "$IMAGE_FILENAME" ]] && { error "-f IMAGE_FILENAME is required"; ERRORS=$((ERRORS+1)); }
[[ $ERRORS -gt 0 ]] && exit 1

# ─── Validate environment ────────────────────────────────────────────────────
[[ -z "${NET_USERNAME:-}" ]] && { error "Export NET_USERNAME before running."; exit 1; }
[[ -z "${NET_PASSWORD:-}" ]] && { error "Export NET_PASSWORD before running."; exit 1; }
[[ ! -f "$TESTBED"  ]]       && { error "Testbed not found: $TESTBED";  exit 1; }
[[ ! -f "$INVENTORY" ]]      && { error "Inventory not found: $INVENTORY"; exit 1; }

command -v python3         >/dev/null 2>&1 || { error "python3 not found"; exit 1; }
command -v ansible-playbook >/dev/null 2>&1 || { error "ansible-playbook not found"; exit 1; }

python3 -c "import genie" 2>/dev/null  || { error "genie not installed — run: pip install pyats genie"; exit 1; }
python3 -c "import pyats"  2>/dev/null || { error "pyats not installed — run: pip install pyats genie"; exit 1; }

# ─── Build optional device filter args ──────────────────────────────────────
PYATS_DEVICES_ARG=""
ANSIBLE_LIMIT_ARG=""
if [[ -n "$DEVICES_ARG" ]]; then
  # pyATS expects space-separated; Ansible expects comma-separated
  PYATS_DEVICES_ARG="--devices $(echo "$DEVICES_ARG" | tr ',' ' ')"
  ANSIBLE_LIMIT_ARG="--limit $DEVICES_ARG"
fi

# ─── Log file setup ──────────────────────────────────────────────────────────
mkdir -p "$SNAPSHOT_DIR"
LOG_FILE="$SNAPSHOT_DIR/upgrade_$(date '+%Y%m%dT%H%M%S').log"
exec > >(tee -a "$LOG_FILE") 2>&1

# =============================================================================
section "STEP 1 / 3 — PRE-UPGRADE HEALTH CHECK (pyATS/Genie)"
# =============================================================================
info "Testbed     : $TESTBED"
info "Snapshot dir: $SNAPSHOT_DIR"
info "Target ver  : $TARGET_VERSION"

python3 pyats/pre_check.py \
  --testbed     "$TESTBED" \
  --output-dir  "$SNAPSHOT_DIR" \
  ${PYATS_DEVICES_ARG}

info "Pre-check PASSED. Snapshots stored in $SNAPSHOT_DIR"

# =============================================================================
section "STEP 2 / 3 — IOS-XR SOFTWARE UPGRADE (Ansible)"
# =============================================================================
info "Inventory   : $INVENTORY"
info "Playbook    : upgrade_iosxr.yml"

ansible-playbook playbooks/upgrade_iosxr.yml \
  --inventory "$INVENTORY" \
  -e "target_version=$TARGET_VERSION" \
  -e "image_source=$IMAGE_SOURCE" \
  -e "image_filename=$IMAGE_FILENAME" \
  ${ANSIBLE_LIMIT_ARG} \
  --diff

ANSIBLE_EXIT=$?
if [[ $ANSIBLE_EXIT -ne 0 ]]; then
  error "Ansible upgrade FAILED (exit code $ANSIBLE_EXIT)."
  error "Post-check will NOT run. Investigate device state manually."
  error "Full log: $LOG_FILE"
  exit $ANSIBLE_EXIT
fi

info "Upgrade playbook completed successfully."

# =============================================================================
section "STEP 3 / 3 — POST-UPGRADE HEALTH CHECK (pyATS/Genie)"
# =============================================================================
info "Waiting ${CONVERGENCE_WAIT}s for BGP/OSPF/MPLS to converge..."
sleep "$CONVERGENCE_WAIT"

python3 pyats/post_check.py \
  --testbed        "$TESTBED" \
  --snapshot-dir   "$SNAPSHOT_DIR" \
  --target-version "$TARGET_VERSION" \
  ${PYATS_DEVICES_ARG}

POST_EXIT=$?
if [[ $POST_EXIT -ne 0 ]]; then
  error "Post-check FAILED — network state has degraded after upgrade."
  error "Review the diff report above and consider rolling back with:"
  error "  ansible-playbook playbooks/upgrade_iosxr.yml ... -e 'install_rollback=true'"
  error "Full log: $LOG_FILE"
  exit $POST_EXIT
fi

# =============================================================================
section "UPGRADE COMPLETE"
# =============================================================================
info "All three phases completed successfully:"
info "  ✓ Pre-check:  baseline captured"
info "  ✓ Upgrade:    $TARGET_VERSION activated and committed"
info "  ✓ Post-check: network state validated"
info "Full log: $LOG_FILE"
