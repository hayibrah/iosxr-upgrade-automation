#!/usr/bin/env python3
"""
FILE    : pre_check.py
PURPOSE : Collect a structured network-state snapshot from all IOS-XR devices
          in the testbed BEFORE the upgrade.  Snapshots are saved as JSON files
          in ./snapshots/ and are consumed by post_check.py for comparison.

USAGE   : python3 pyats/pre_check.py --testbed pyats/testbed.yaml [options]

OPTIONS :
  --testbed       PATH   Path to testbed.yaml                         (required)
  --output-dir    DIR    Directory to save snapshot JSON files
                         (default: ./snapshots)
  --devices       LIST   Space-separated device names to limit scope
                         (default: all devices in testbed)
  --checks-config PATH   Path to checks.yaml
                         (default: checks.yaml next to this script)

REQUIRES: pip install pyats genie PyYAML
"""

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from genie.testbed import load

# Shared library
from checks_lib import (collect_state, collect_operational_state,
                        derive_counts, load_checks_config)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("pre_check")


# ---------------------------------------------------------------------------
# Snapshot helpers
# ---------------------------------------------------------------------------

def save_snapshot(device_name: str, snapshot: dict, counts: dict,
                  output_dir: Path, operational: dict = None) -> Path:
    """Write the full snapshot + derived counts + operational state to JSON."""
    output_dir.mkdir(parents=True, exist_ok=True)
    ts       = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    filename = output_dir / f"{device_name}_pre_check_{ts}.json"

    payload = {
        "metadata": {
            "device":    device_name,
            "phase":     "pre_check",
            "timestamp": ts,
        },
        "counts":      counts,
        "snapshot":    snapshot,
        "operational": operational or {},   # keyed by op_check name
    }

    with open(filename, "w") as fh:
        json.dump(payload, fh, indent=2, default=str)

    log.info("[%s] Snapshot saved → %s", device_name, filename)
    return filename


def print_summary(device_name: str, counts: dict) -> None:
    width = 55
    log.info("=" * width)
    log.info("  PRE-CHECK SUMMARY — %s", device_name)
    log.info("=" * width)
    for k, v in counts.items():
        label = k.replace("_", " ").title()
        value = str(v) if v is not None else "N/A (parse error)"
        log.info("  %-40s : %s", label, value)
    log.info("=" * width)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="pyATS/Genie pre-upgrade health snapshot for IOS-XR devices"
    )
    parser.add_argument("--testbed",        required=True,
                        help="Path to testbed.yaml")
    parser.add_argument("--output-dir",     default="./snapshots",
                        help="Directory to save snapshot JSON files "
                             "(default: ./snapshots)")
    parser.add_argument("--devices",        nargs="*",
                        help="Limit to specific device names "
                             "(default: all devices in testbed)")
    parser.add_argument("--checks-config",  default=None,
                        help="Path to checks.yaml "
                             "(default: checks.yaml next to pre_check.py)")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)

    # Load health checks + operational checks from external config
    log.info("Loading checks config: %s",
             args.checks_config or "default (pyats/checks.yaml)")
    health_checks, _critical_counts, op_checks = load_checks_config(args.checks_config)
    log.info("%d health checks, %d operational checks enabled",
             len(health_checks), len(op_checks))

    # Load the testbed
    log.info("Loading testbed: %s", args.testbed)
    testbed = load(args.testbed)

    device_names = args.devices or list(testbed.devices.keys())
    log.info("Devices targeted: %s", device_names)

    overall_ok     = True
    snapshot_files = []

    for dev_name in device_names:
        if dev_name not in testbed.devices:
            log.error("Device '%s' not found in testbed — skipping", dev_name)
            overall_ok = False
            continue

        device = testbed.devices[dev_name]
        log.info("━━━ Connecting to: %s ━━━", dev_name)

        try:
            device.connect(
                log_stdout=False,
                init_exec_commands=[],
                init_config_commands=[],
            )

            snapshot    = collect_state(device, health_checks)
            counts      = derive_counts(snapshot, health_checks)
            # Reuse any health-check results so overlapping commands (e.g.
            # 'show route summary') are not sent to the device a second time.
            cmd_cache   = {
                c["command"]: snapshot[c["key"]]
                for c in health_checks if c["key"] in snapshot
            }
            operational = collect_operational_state(
                device, op_checks, command_cache=cmd_cache
            )
            snap_file   = save_snapshot(dev_name, snapshot, counts,
                                        output_dir, operational)
            snapshot_files.append(str(snap_file))
            print_summary(dev_name, counts)

        except Exception as exc:
            log.error("[%s] FAILED: %s", dev_name, exc)
            overall_ok = False

        finally:
            try:
                device.disconnect()
            except Exception:
                pass

    # Write an index file that post_check.py uses to locate snapshots
    index_file = output_dir / "snapshot_index.json"
    index_data = {
        "phase":     "pre_check",
        "timestamp": datetime.utcnow().strftime("%Y%m%dT%H%M%SZ"),
        "files":     snapshot_files,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    with open(index_file, "w") as fh:
        json.dump(index_data, fh, indent=2)
    log.info("Snapshot index saved → %s", index_file)

    if not overall_ok:
        log.error("One or more devices encountered errors during pre-check.")
        sys.exit(1)

    log.info("Pre-check complete. Run the upgrade, then execute post_check.py.")


if __name__ == "__main__":
    main()
