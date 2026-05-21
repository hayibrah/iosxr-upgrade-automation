#!/usr/bin/env python3
"""
FILE    : post_check.py
PURPOSE : Collect a post-upgrade state snapshot, compare it against the
          pre_check.py snapshot, and produce a pass/fail report.

          Two comparison passes:
            1. Count comparison  (HARD FAILURE)
               Numeric counts (BGP sessions, OSPF neighbors, etc.) must not
               drop below pre-upgrade values.
            2. Structural Genie Diff  (INFORMATIONAL)
               Full dict comparison with known-noisy fields excluded.
               Differences are reported but do NOT affect the pass/fail verdict.

USAGE   : python3 pyats/post_check.py \
              --testbed        pyats/testbed.yaml \
              --snapshot-dir   ./snapshots \
              --target-version 25.2.1

OPTIONS :
  --testbed           PATH   Path to testbed.yaml                       (required)
  --snapshot-dir      DIR    Directory containing pre_check snapshot files
                             (default: ./snapshots)
  --target-version    VER    Expected IOS-XR version post-upgrade
  --devices           LIST   Space-separated device names to limit scope
  --convergence-wait  SECS   Wait before collecting post-check data (default: 0)
  --checks-config     PATH   Path to checks.yaml
                             (default: checks.yaml next to this script)

REQUIRES: pip install pyats genie PyYAML
"""

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

from genie.testbed import load
from genie.utils.diff import Diff

# Shared library — no import from pre_check (avoids path/circular import issues)
from checks_lib import (
    DIFF_EXCLUDE,
    collect_state,
    collect_operational_state,
    compare_operational_checks,
    derive_counts,
    load_checks_config,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("post_check")


# ---------------------------------------------------------------------------
# Snapshot helpers
# ---------------------------------------------------------------------------

def find_latest_snapshot(device_name: str, snapshot_dir: Path) -> Path:
    """
    Locate the most recent pre_check snapshot for *device_name*.

    Prefers snapshot_index.json written by pre_check.py; falls back to
    globbing the directory.

    FIX: uses exact prefix match (<device_name>_pre_check_…) rather than
    substring containment so that 'router-1' does not accidentally match
    a file belonging to 'router-10'.
    """
    prefix = f"{device_name}_pre_check_"

    index_file = snapshot_dir / "snapshot_index.json"
    if index_file.exists():
        with open(index_file) as fh:
            index = json.load(fh)
        matches = [
            f for f in index.get("files", [])
            if Path(f).name.startswith(prefix)
        ]
        if matches:
            return Path(matches[-1])   # most recent = last appended

    # Fallback: glob
    candidates = sorted(snapshot_dir.glob(f"{prefix}*.json"))
    if not candidates:
        raise FileNotFoundError(
            f"No pre-check snapshot found for '{device_name}' in {snapshot_dir}. "
            f"Did pre_check.py run successfully?"
        )
    return candidates[-1]


def load_pre_snapshot(device_name: str, snapshot_dir: Path) -> dict:
    snap_file = find_latest_snapshot(device_name, snapshot_dir)
    log.info("[%s] Loading pre-check snapshot: %s", device_name, snap_file)
    with open(snap_file) as fh:
        return json.load(fh)


def save_post_snapshot(device_name: str, snapshot: dict, counts: dict,
                       output_dir: Path, operational: dict = None) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    ts       = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    filename = output_dir / f"{device_name}_post_check_{ts}.json"
    payload  = {
        "metadata": {
            "device":    device_name,
            "phase":     "post_check",
            "timestamp": ts,
        },
        "counts":      counts,
        "snapshot":    snapshot,
        "operational": operational or {},
    }
    with open(filename, "w") as fh:
        json.dump(payload, fh, indent=2, default=str)
    log.info("[%s] Post-check snapshot saved → %s", device_name, filename)
    return filename


# ---------------------------------------------------------------------------
# Comparison helpers
# ---------------------------------------------------------------------------

def compare_counts(pre_counts: dict, post_counts: dict,
                   critical_counts: dict) -> list:
    """
    Compare derived numeric counts for every metric in *critical_counts*.
    Returns a list of failure strings (empty list = all passed).
    """
    failures = []

    for key, label in critical_counts.items():
        pre_val  = pre_counts.get(key)
        post_val = post_counts.get(key)

        if pre_val is None or post_val is None:
            log.warning("  Skipping count check '%s' — data unavailable", key)
            continue

        if post_val < pre_val:
            failures.append(
                f"{label}: DROPPED  (pre={pre_val}, post={post_val})"
            )
        else:
            log.info("  %-50s : OK  (pre=%s, post=%s)", label, pre_val, post_val)

    return failures


def run_structural_diff(pre_snapshot: dict, post_snapshot: dict,
                        health_checks: list, device_name: str) -> dict:
    """
    Run Genie Diff on each health-check key individually.

    Returns { check_key: diff_string_or_None }
    None  = no meaningful differences found for this key.
    """
    diff_results = {}

    for check in health_checks:
        key      = check["key"]
        pre_val  = pre_snapshot.get(key)
        post_val = post_snapshot.get(key)

        if pre_val is None or post_val is None:
            diff_results[key] = "SKIP (one or both snapshots missing this key)"
            continue

        # Raw strings (parser: false) — Genie Diff expects dicts; skip
        if isinstance(pre_val, str) or isinstance(post_val, str):
            diff_results[key] = None
            continue

        try:
            diff = Diff(pre_val, post_val, exclude=DIFF_EXCLUDE)
            diff.findDiff()
            diff_str = str(diff)
            diff_results[key] = diff_str if diff_str.strip() else None
        except Exception as exc:
            log.warning("[%s] Diff failed for '%s': %s", device_name, key, exc)
            diff_results[key] = f"DIFF ERROR: {exc}"

    return diff_results


def verify_version(device, target_version: str) -> bool:
    """Confirm the device is running *target_version*."""
    try:
        result = device.execute("show version | include 'Cisco IOS XR Software'")
        if target_version in result:
            log.info("  Version check: PASS — running %s", target_version)
            return True
        log.error("  Version check: FAIL — '%s' not found in output", target_version)
        log.error("  Output: %s", result)
        return False
    except Exception as exc:
        log.error("  Version check: ERROR — %s", exc)
        return False


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def print_report(device_name: str,
                 pre_counts: dict, post_counts: dict,
                 count_failures: list,
                 op_checks: list,
                 op_failures: list,
                 diff_results: dict,
                 critical_counts: dict,
                 version_ok: bool, target_version: str) -> bool:
    """
    Print a human-readable final report.

    Hard failures (affect verdict):
      1. Version check     — device must run target_version
      2. Count comparison  — tracked metrics must not drop
      3. Operational diffs — operational_checks must be identical pre/post

    Informational (never affect verdict):
      4. Structural Genie diff of health_checks

    Returns True if all hard checks passed.
    """
    width = 68
    sep   = "=" * width

    print(f"\n{sep}")
    print(f"  POST-UPGRADE REPORT — {device_name}")
    print(sep)

    # ── 1. Version ───────────────────────────────────────────────────────────
    v_status = "PASS ✓" if version_ok else "FAIL ✗"
    print(f"\n  {'Target Version':<45}: {v_status} ({target_version})")

    # ── 2. Count comparison table ────────────────────────────────────────────
    print(f"\n  {'Metric':<45}  {'Pre':>6}  {'Post':>6}  {'Status'}")
    print(f"  {'-'*45}  {'-'*6}  {'-'*6}  {'-'*6}")
    for key, label in critical_counts.items():
        pre  = pre_counts.get(key, "N/A")
        post = post_counts.get(key, "N/A")
        if isinstance(pre, int) and isinstance(post, int):
            ok = "PASS ✓" if post >= pre else "FAIL ✗"
        else:
            ok = "SKIP"
        print(f"  {label:<45}  {str(pre):>6}  {str(post):>6}  {ok}")

    # ── 3. Operational checks — before/after diff ────────────────────────────
    print(f"\n  Operational Checks  (HARD FAILURE on any diff)")
    print(f"  {'-'*63}")
    if not op_checks:
        print(f"  (no operational checks configured)")
    else:
        op_failed_map = {f["name"]: f for f in op_failures}
        for check in op_checks:
            name = check["name"]
            if name in op_failed_map:
                failure = op_failed_map[name]
                diff    = failure["diff"]
                if diff.startswith("SKIP"):
                    print(f"  {name:<35} : {diff}")
                else:
                    print(f"  {name:<35} : FAIL ✗  ({failure['command']})")
                    for line in diff.splitlines():
                        print(f"      {line}")
            else:
                print(f"  {name:<35} : PASS ✓")

    # ── 4. Structural diff — informational ───────────────────────────────────
    print(f"\n  Structural Diff  (informational — does not affect verdict)")
    print(f"  Expected changes after an upgrade: software version strings in")
    print(f"  'show platform' and route/LSP sequence numbers. Unexpected")
    print(f"  changes: interface state flaps, missing neighbors, VRF changes.")
    print(f"  {'-'*63}")
    any_diff = False
    for key, diff_output in diff_results.items():
        if diff_output is None:
            print(f"  {key:<35} : No meaningful changes")
        elif diff_output.startswith("SKIP") or diff_output.startswith("DIFF ERROR"):
            print(f"  {key:<35} : {diff_output}")
        else:
            any_diff = True
            # Flag platform diffs as expected (software version change)
            note = " (version change expected)" if key == "platform" else ""
            print(f"  {key:<35} : DIFFERENCES FOUND{note} ↓")
            for line in diff_output.splitlines():
                print(f"      {line}")

    if any_diff:
        print(
            f"\n  NOTE: Structural differences above are informational only.\n"
            f"  'platform' diffs show the software version change per slot —\n"
            f"  this is expected and confirmed by the version check above.\n"
            f"  Investigate any diffs in bgp/ospf/isis/mpls/interfaces."
        )

    # ── Verdict ──────────────────────────────────────────────────────────────
    all_passed = version_ok and not count_failures and not op_failures
    verdict    = (
        "✅  UPGRADE VALIDATED — All hard checks passed."
        if all_passed else
        "❌  UPGRADE ISSUES DETECTED — Review failures below."
    )

    print(f"\n{sep}")
    print(f"  VERDICT: {verdict}")
    if count_failures:
        print(f"\n  Count Failures:")
        for f in count_failures:
            print(f"    • {f}")
    if op_failures:
        print(f"\n  Operational Check Failures:")
        for f in op_failures:
            if not f["diff"].startswith("SKIP"):
                print(f"    • {f['name']}  ({f['command']})"
                      f" — diff shown above")
    print(f"{sep}\n")

    return all_passed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="pyATS/Genie post-upgrade health validation for IOS-XR devices"
    )
    parser.add_argument("--testbed",           required=True,
                        help="Path to testbed.yaml")
    parser.add_argument("--snapshot-dir",      default="./snapshots",
                        help="Directory containing pre_check snapshot files")
    parser.add_argument("--target-version",    default=None,
                        help="Expected IOS-XR version post-upgrade (e.g. 25.2.1)")
    parser.add_argument("--devices",           nargs="*",
                        help="Limit to specific device names")
    parser.add_argument("--convergence-wait",  type=int, default=0,
                        help="Seconds to wait before collecting post-check data "
                             "(default: 0 — run_upgrade.sh handles this externally)")
    parser.add_argument("--checks-config",     default=None,
                        help="Path to checks.yaml "
                             "(default: checks.yaml next to this script)")
    args = parser.parse_args()

    snapshot_dir = Path(args.snapshot_dir)

    if args.convergence_wait > 0:
        log.info("Waiting %ds for protocol convergence...", args.convergence_wait)
        time.sleep(args.convergence_wait)

    # Load health checks + operational checks from external config
    log.info("Loading checks config: %s",
             args.checks_config or "default (pyats/checks.yaml)")
    health_checks, critical_counts, op_checks = load_checks_config(args.checks_config)
    log.info("%d health checks, %d critical counts, %d operational checks",
             len(health_checks), len(critical_counts), len(op_checks))

    log.info("Loading testbed: %s", args.testbed)
    testbed = load(args.testbed)

    device_names = args.devices or list(testbed.devices.keys())
    log.info("Devices targeted: %s", device_names)

    overall_passed = True

    for dev_name in device_names:
        if dev_name not in testbed.devices:
            log.error("Device '%s' not found in testbed — skipping", dev_name)
            overall_passed = False
            continue

        device = testbed.devices[dev_name]
        log.info("━━━ Connecting to: %s ━━━", dev_name)

        try:
            device.connect(
                log_stdout=False,
                init_exec_commands=[],
                init_config_commands=[],
            )

            # Load pre-check baseline
            pre_data        = load_pre_snapshot(dev_name, snapshot_dir)
            pre_snapshot    = pre_data["snapshot"]
            pre_counts      = pre_data["counts"]
            pre_operational = pre_data.get("operational", {})

            # Collect post-check state
            log.info("[%s] Collecting post-check state...", dev_name)
            post_snapshot    = collect_state(device, health_checks)
            post_counts      = derive_counts(post_snapshot, health_checks)
            # Build a cache so operational checks don't re-run commands that
            # were already executed during the health-check phase above.
            post_cmd_cache   = {
                c["command"]: post_snapshot[c["key"]]
                for c in health_checks if c["key"] in post_snapshot
            }
            post_operational = collect_operational_state(
                device, op_checks, command_cache=post_cmd_cache
            )

            # Save post-check snapshot for audit trail
            save_post_snapshot(dev_name, post_snapshot, post_counts,
                               snapshot_dir, post_operational)

            # Version check (hard)
            target_version = args.target_version or "not specified"
            version_ok = (
                verify_version(device, args.target_version)
                if args.target_version else True
            )

            # Count comparison (hard — any drop = failure)
            log.info("[%s] Comparing critical counts...", dev_name)
            count_failures = compare_counts(pre_counts, post_counts, critical_counts)

            # Operational checks — before/after diff (hard — any diff = failure)
            log.info("[%s] Running operational check diffs...", dev_name)
            op_failures = compare_operational_checks(
                pre_operational, post_operational, op_checks
            )
            if op_failures:
                log.error("[%s] %d operational check(s) FAILED",
                          dev_name, len(op_failures))

            # Structural diff of health_checks (informational only)
            log.info("[%s] Running structural Genie diff...", dev_name)
            diff_results = run_structural_diff(
                pre_snapshot, post_snapshot, health_checks, dev_name
            )

            # Final report
            passed = print_report(
                dev_name,
                pre_counts, post_counts,
                count_failures,
                op_checks,
                op_failures,
                diff_results,
                critical_counts,
                version_ok, target_version,
            )

            if not passed:
                overall_passed = False

        except FileNotFoundError as exc:
            log.error("[%s] %s", dev_name, exc)
            overall_passed = False

        except Exception as exc:
            log.error("[%s] FAILED: %s", dev_name, exc)
            overall_passed = False

        finally:
            try:
                device.disconnect()
            except Exception:
                pass

    if overall_passed:
        log.info("All post-checks PASSED. Upgrade is validated.")
        sys.exit(0)
    else:
        log.error("One or more post-checks FAILED. Review the report above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
