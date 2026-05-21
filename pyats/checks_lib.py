#!/usr/bin/env python3
"""
FILE    : checks_lib.py
PURPOSE : Shared library imported by both pre_check.py and post_check.py.

          Provides:
            DIFF_EXCLUDE              — fields excluded from every structural diff
            load_checks_config()      — parse checks.yaml; returns
                                        (health_checks, critical_counts,
                                         operational_checks)
            collect_state()           — run health-check commands against a device
            collect_operational_state() — run operational-check commands
            compare_operational_checks() — diff pre vs post operational snapshots;
                                           any diff is a hard failure
            extract_metric()          — generic dict traversal + aggregation engine
                                        (powers YAML-configured test_cases)
            run_test_cases()          — evaluate all test_cases from the config
            derive_counts()           — hardcoded standard metrics + YAML test_cases

          Keeping shared logic here means neither pre_check.py nor post_check.py
          imports from the other, which avoids the circular/broken import that
          occurs when calling the scripts from the repo root.
"""

import difflib
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from genie.utils.diff import Diff

log = logging.getLogger(__name__)

# Default checks config lives alongside this module
_DEFAULT_CHECKS_FILE = Path(__file__).parent / "checks.yaml"


# ---------------------------------------------------------------------------
# Diff exclusion list  (shared by both structural diff and operational diff)
# ---------------------------------------------------------------------------

# Fields that legitimately change on every IOS-XR reload and would produce
# noise rather than signal in a diff.  Any key matching one of these regexes
# is silently excluded from all Genie structural comparisons.
DIFF_EXCLUDE = [
    # BGP / OSPF session timers reset on reload
    r".*up_time.*",
    r".*uptime.*",
    r".*last_read.*",
    r".*last_write.*",
    r".*last_state_change.*",
    r".*bgp_table_version.*",
    r".*routing_table_version.*",
    r".*msg_rcvd.*",
    r".*msg_sent.*",
    r".*tbl_ver.*",
    r".*inq.*",
    r".*outq.*",
    # Interface counters reset on reload
    r".*in_octets.*",
    r".*out_octets.*",
    r".*in_pkts.*",
    r".*out_pkts.*",
    r".*in_errors.*",
    r".*out_errors.*",
    r".*in_discards.*",
    r".*out_discards.*",
    r".*last_clear.*",
    r".*counters.*",
    # System / platform timers
    r".*system_uptime.*",
    r".*processor_uptime.*",
    r".*image_text_base.*",       # memory addresses vary between boots
    # MPLS LDP hello/hold timers
    r".*hello_interval.*",
    r".*hold_time.*",
    r".*session_hold_time.*",
    # IS-IS sequence numbers increment on every LSP flood
    r".*sequence_number.*",
]


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_checks_config(config_file=None):
    """
    Load health_checks, critical_counts, and operational_checks from YAML.

    Parameters
    ----------
    config_file : str or Path, optional
        Path to checks.yaml.  Defaults to pyats/checks.yaml next to this file.

    Returns
    -------
    health_checks : list[dict]
        Only entries with ``enabled != false`` are returned.
    critical_counts : dict[str, str]
        Mapping of metric_key → human-readable label.
        Includes auto-merged entries from test_cases.
    operational_checks : list[dict]
        Only entries with ``enabled != false`` are returned.
        Each entry has ``name`` and ``command``.
    """
    config_path = Path(config_file) if config_file else _DEFAULT_CHECKS_FILE

    if not config_path.exists():
        raise FileNotFoundError(
            f"Checks config not found: {config_path}\n"
            f"Expected pyats/checks.yaml alongside this module."
        )

    with open(config_path) as fh:
        config = yaml.safe_load(fh)

    # Filter out disabled entries
    all_checks    = config.get("health_checks", [])
    health_checks = [c for c in all_checks if c.get("enabled", True)]

    # Start with explicit critical_counts, then auto-merge test_case metrics.
    # This means users only need to define a test_case — the metric is
    # automatically tracked in the post-check comparison table.
    critical_counts = dict(config.get("critical_counts", {}))
    for check in health_checks:
        for tc in check.get("test_cases", []):
            mk = tc.get("metric_key")
            if mk and mk not in critical_counts:
                critical_counts[mk] = tc.get("name", mk)

    # Operational checks — simple before/after diff, any diff = hard failure
    all_op   = config.get("operational_checks", [])
    op_checks = [c for c in all_op if c.get("enabled", True)]

    tc_count = sum(len(c.get("test_cases", [])) for c in health_checks)
    log.debug(
        "Loaded %d health checks (%d disabled), %d test cases, "
        "%d critical counts, %d operational checks (%d disabled) from %s",
        len(health_checks),
        len(all_checks) - len(health_checks),
        tc_count,
        len(critical_counts),
        len(op_checks),
        len(all_op) - len(op_checks),
        config_path,
    )

    return health_checks, critical_counts, op_checks


# ---------------------------------------------------------------------------
# State collection
# ---------------------------------------------------------------------------

def collect_state(device, checks: list) -> dict:
    """
    Run each health check against *device*.

    Returns a dict keyed by check['key'] containing either a parsed dict
    (when parser=True and Genie has a parser) or raw CLI stdout (fallback).
    Errors are caught per-check and stored as {"error": "<msg>"} so that a
    single failing command does not abort the entire collection run.
    """
    snapshot = {}

    for check in checks:
        key        = check["key"]
        command    = check["command"]
        use_parser = check.get("parser", True)

        log.info("  [%s] Collecting: %s", device.name, command)
        try:
            if use_parser:
                result = device.parse(command)
            else:
                result = device.execute(command)
        except Exception as exc:
            log.warning("  [%s] WARN: '%s' failed: %s", device.name, command, exc)
            result = {"error": str(exc)}

        snapshot[key] = result

    return snapshot


# ---------------------------------------------------------------------------
# Operational checks — collect + compare
# ---------------------------------------------------------------------------

def collect_operational_state(device, op_checks: list,
                               command_cache: Optional[Dict] = None) -> dict:
    """
    Run each operational check command against *device*.

    Tries the Genie structured parser first (so the diff is semantic and
    DIFF_EXCLUDE can filter noise).  Falls back to raw CLI text if Genie
    has no parser for the command.

    Parameters
    ----------
    command_cache
        Optional ``{command_str: result}`` mapping of already-collected data
        (e.g. from the health-check phase).  When a command is found here it
        is reused directly, avoiding a duplicate round-trip to the device.

    Returns ``{name: parsed_dict_or_raw_str}``.
    """
    results = {}

    for check in op_checks:
        name    = check["name"]
        command = check["command"]

        # Reuse result already collected during the health-check phase
        if command_cache and command in command_cache:
            log.debug("  [%s] '%s' — reused from health-check snapshot",
                      device.name, command)
            results[name] = command_cache[command]
            continue

        log.info("  [%s] Operational check: %s", device.name, command)

        try:
            result = device.parse(command)
            log.debug("  [%s] '%s' — Genie parser OK", device.name, command)
        except Exception:
            # No Genie parser for this command; fall back to raw CLI
            try:
                result = device.execute(command)
                log.debug("  [%s] '%s' — raw CLI fallback", device.name, command)
            except Exception as exc:
                log.warning("  [%s] '%s' failed: %s", device.name, command, exc)
                result = {"error": str(exc)}

        results[name] = result

    return results


def compare_operational_checks(
    pre_ops: dict,
    post_ops: dict,
    op_checks: list,
) -> list:
    """
    Diff pre-upgrade vs post-upgrade operational state for each check.

    - Parsed dicts  → Genie semantic Diff with DIFF_EXCLUDE applied.
    - Raw CLI text  → unified text diff (line-by-line).

    Returns a list of failure dicts, one per check that has differences:
        {"name": ..., "command": ..., "diff": "<diff text>"}
    An empty list means all checks passed.
    """
    failures = []

    for check in op_checks:
        name    = check["name"]
        command = check["command"]

        pre_val  = pre_ops.get(name)
        post_val = post_ops.get(name)

        # ── Missing snapshot ─────────────────────────────────────────────────
        if pre_val is None:
            log.warning("  Operational check '%s': no pre-check snapshot", name)
            failures.append({
                "name":    name,
                "command": command,
                "diff":    "SKIP — no pre-check data (did pre_check.py run?)",
            })
            continue

        if post_val is None:
            failures.append({
                "name":    name,
                "command": command,
                "diff":    "SKIP — post-check collection returned nothing",
            })
            continue

        # ── Either side had a collection error ───────────────────────────────
        pre_err  = isinstance(pre_val,  dict) and set(pre_val)  == {"error"}
        post_err = isinstance(post_val, dict) and set(post_val) == {"error"}
        if pre_err or post_err:
            log.warning("  Operational check '%s': skipped (collection error)", name)
            continue

        # ── Both parsed → Genie semantic diff ────────────────────────────────
        if isinstance(pre_val, dict) and isinstance(post_val, dict):
            try:
                d = Diff(pre_val, post_val, exclude=DIFF_EXCLUDE)
                d.findDiff()
                diff_str = str(d).strip()
            except Exception as exc:
                diff_str = f"DIFF ERROR: {exc}"

            if diff_str:
                failures.append({"name": name, "command": command, "diff": diff_str})

        # ── Raw CLI text → unified text diff ─────────────────────────────────
        else:
            pre_lines  = str(pre_val).splitlines()
            post_lines = str(post_val).splitlines()
            delta      = list(difflib.unified_diff(
                pre_lines, post_lines,
                fromfile="pre-upgrade",
                tofile="post-upgrade",
                lineterm="",
            ))
            if delta:
                failures.append({
                    "name":    name,
                    "command": command,
                    "diff":    "\n".join(delta),
                })

    return failures


# ---------------------------------------------------------------------------
# Generic metric extraction engine  (powers YAML test_cases)
# ---------------------------------------------------------------------------

def extract_metric(
    data: Any,
    path: List[str],
    filter_cfg: Optional[Dict] = None,
    aggregate: str = "count",
) -> Optional[int]:
    """
    Walk *data* (a Genie-parsed dict) following *path*, then aggregate.

    path
        List of dict keys.  ``"*"`` expands to every child value at that level,
        so the traversal fans out across all VRFs, processes, interfaces, etc.

    filter_cfg
        Optional dict with keys ``field`` and ``regex``.  After traversal,
        only leaf dicts whose ``field`` value matches the compiled ``regex``
        are included in the result.  Ignored for ``aggregate: sum``.

    aggregate
        ``"count"`` — count of matching leaf nodes  (default)
        ``"sum"``   — sum of numeric values at the end of the path

    Returns an integer, or None if the data was missing / unusable.
    """

    def _walk(node: Any, steps: List[str]):
        """Recursively yield leaf nodes reached by following steps."""
        if not steps:
            yield node
            return
        key, *rest = steps
        if not isinstance(node, dict):
            return
        if key == "*":
            for child in node.values():
                yield from _walk(child, rest)
        elif key in node:
            yield from _walk(node[key], rest)

    # Guard: if data indicates a prior collection error, skip silently
    if isinstance(data, dict) and "error" in data and len(data) == 1:
        return None

    leaves = list(_walk(data, path))

    if aggregate == "sum":
        total = 0
        for leaf in leaves:
            if isinstance(leaf, (int, float)):
                total += leaf
        return total

    # aggregate == "count"
    if filter_cfg:
        f_field   = filter_cfg.get("field", "")
        f_pattern = re.compile(filter_cfg.get("regex", ".*"), re.IGNORECASE)
        return sum(
            1 for leaf in leaves
            if isinstance(leaf, dict)
            and f_pattern.search(str(leaf.get(f_field, "")))
        )
    return len(leaves)


def run_test_cases(snapshot: dict, health_checks: list) -> dict:
    """
    Evaluate every ``test_cases`` entry defined in *health_checks*.

    Returns ``{metric_key: int_or_None}`` for every configured test case.
    Results are merged into the counts dict produced by ``derive_counts()``.
    """
    results: Dict[str, Optional[int]] = {}

    for check in health_checks:
        snap_key = check["key"]
        raw      = snapshot.get(snap_key)

        for tc in check.get("test_cases", []):
            metric_key = tc.get("metric_key", "")
            tc_name    = tc.get("name", metric_key)

            if raw is None:
                log.warning("Test case '%s': snapshot key '%s' not found",
                            tc_name, snap_key)
                results[metric_key] = None
                continue

            extract_cfg = tc.get("extract", {})
            path        = extract_cfg.get("path", [])
            filter_cfg  = extract_cfg.get("filter")
            aggregate   = extract_cfg.get("aggregate", "count")

            try:
                val = extract_metric(raw, path,
                                     filter_cfg=filter_cfg,
                                     aggregate=aggregate)
                results[metric_key] = val
                log.debug("Test case '%s' → %s", tc_name, val)
            except Exception as exc:
                log.warning("Test case '%s' failed during extraction: %s",
                            tc_name, exc)
                results[metric_key] = None

    return results


# ---------------------------------------------------------------------------
# Count derivation
# ---------------------------------------------------------------------------

def derive_counts(snapshot: dict, health_checks: Optional[list] = None) -> dict:
    """
    Return ``{metric_key: int_or_None}`` for all tracked metrics.

    All metrics except ``platform_cards_running`` are driven entirely by the
    ``test_cases`` entries in checks.yaml via ``run_test_cases()``.

    ``platform_cards_running`` is still derived here because it requires
    traversing both the ``lc`` and ``rp`` sub-keys of every slot simultaneously,
    which cannot be expressed as a single YAML path expression.

    Returns a dict of { metric_key: int_or_None }.
    None means the data was unavailable (parse error / command not collected).
    """
    counts = {}

    # ── Platform cards in IOS XR RUN ─────────────────────────────────────────
    # Traverses both 'lc' (line cards) and 'rp' (route processors) in every
    # slot and counts how many are in "IOS XR RUN" state.
    try:
        platform    = snapshot.get("platform", {})
        slot_states = []
        for slot_data in platform.get("slot", {}).values():
            for card_data in slot_data.get("lc", {}).values():
                slot_states.append(card_data.get("state", "").upper())
            for rp_data in slot_data.get("rp", {}).values():
                slot_states.append(rp_data.get("state", "").upper())
        counts["platform_cards_running"] = slot_states.count("IOS XR RUN")
    except Exception as exc:
        log.warning("Could not derive platform-cards-running count: %s", exc)
        counts["platform_cards_running"] = None

    # ── All other metrics — driven by YAML test_cases in checks.yaml ─────────
    if health_checks:
        tc_results = run_test_cases(snapshot, health_checks)
        for key, val in tc_results.items():
            if key not in counts:   # platform_cards_running takes precedence
                counts[key] = val

    return counts
