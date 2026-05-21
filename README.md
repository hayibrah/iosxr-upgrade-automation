# iosxr-upgrade-automation

End-to-end Cisco IOS-XR software upgrade automation with structured pre/post health validation.

The upgrade itself is handled by **Ansible** (`playbooks/upgrade_iosxr.yml`).  
Health checks are handled by **pyATS / Genie** (`pyats/pre_check.py` and `pyats/post_check.py`), which use Cisco's own structured parsers to collect and compare device state — rather than raw CLI text — so you get real signal, not noise from timer and counter resets.

---

## Project Structure

```
iosxr-upgrade-automation/
│
├── playbooks/
│   └── upgrade_iosxr.yml         # Ansible: install add → activate → commit
│
├── pyats/
│   ├── testbed.yaml              # Device topology (IPs, OS, credentials)
│   ├── checks.yaml               # ← Edit this to add/remove checks (no Python needed)
│   ├── checks_lib.py             # Shared library (collection, diff, count logic)
│   ├── pre_check.py              # Collect + save pre-upgrade state snapshot
│   └── post_check.py             # Load snapshot, compare, pass/fail report
│
├── collections/
│   └── requirements.yml          # Ansible Galaxy collection dependencies
│
├── snapshots/                    # Auto-created; stores JSON state snapshots
│   └── .gitkeep
│
├── ansible.cfg                   # Ansible settings (timeouts, SSH args)
├── inventory.ini                 # Ansible inventory (hosts + group vars)
├── requirements.txt              # Python dependencies
├── run_upgrade.sh                # One-shot orchestration script
└── .gitignore
```

---

## Prerequisites

### System requirements
- Python 3.9 or later
- Ansible 8.0 or later
- SSH access to all target IOS-XR devices

### Install Python dependencies

```bash
pip install -r requirements.txt
```

### Install Ansible collections

```bash
ansible-galaxy collection install -r collections/requirements.yml
```

### Set credentials as environment variables

Credentials are **never** stored in files. Export them before running anything:

```bash
export NET_USERNAME=admin
export NET_PASSWORD=yourpassword
```

---

## Configuration

### 1. Edit `pyats/testbed.yaml`

Add one entry per router under `devices:`. Set the correct `ip`, `os`, and `platform`:

```yaml
devices:
  router-1:
    os:       iosxr
    platform: ncs5500        # or asr9k, xrv9k, etc.
    connections:
      cli:
        protocol: ssh
        ip:       192.168.1.1
```

### 2. Edit `inventory.ini`

Mirror the same devices in the Ansible inventory:

```ini
[iosxr_routers]
router-1  ansible_host=192.168.1.1
router-2  ansible_host=192.168.1.2
```

### 3. Set upgrade variables

Either edit the defaults in `playbooks/upgrade_iosxr.yml`:

```yaml
vars:
  # Example: NCS 5500 upgrade from 24.2.1 → 25.2.1
  target_version: "25.2.1"
  image_source:   "sftp://mgmt-server.local/images/ncs5500-x64-25.2.1.iso"
  image_filename: "ncs5500-x64-25.2.1.iso"
```

Or pass them on the CLI with `-e` (see usage below).

---

## Usage

### Option A — Full end-to-end (recommended)

```bash
chmod +x run_upgrade.sh

# Example: upgrade NCS 5500 from 24.2.1 → 25.2.1
./run_upgrade.sh \
  -t 25.2.1 \
  -s "sftp://mgmt-server.local/images/ncs5500-x64-25.2.1.iso" \
  -f "ncs5500-x64-25.2.1.iso"
```

`run_upgrade.sh` runs all three phases in sequence and aborts if any phase fails.

**All options:**

| Flag | Description | Default |
|------|-------------|---------|
| `-t` | Target IOS-XR version | *(required)* |
| `-s` | Image source URI (sftp/ftp/scp) | *(required)* |
| `-f` | Image filename on device | *(required)* |
| `-b` | Path to testbed.yaml | `./pyats/testbed.yaml` |
| `-i` | Path to Ansible inventory | `./inventory.ini` |
| `-d` | Snapshot output directory | `./snapshots` |
| `-w` | Protocol convergence wait (seconds) | `180` |
| `-n` | Limit to specific devices (comma-separated) | all |

---

### Option B — Run each phase independently

This is useful for maintenance windows where pre-checks happen during business hours and the upgrade runs overnight.

**Step 1 — Pre-check (run before the upgrade window)**

```bash
python3 pyats/pre_check.py \
  --testbed    pyats/testbed.yaml \
  --output-dir ./snapshots
```

Saves one JSON snapshot per device to `./snapshots/`.

**Step 2 — Upgrade (run during the maintenance window)**

```bash
ansible-playbook playbooks/upgrade_iosxr.yml \
  -e "target_version=25.2.1" \
  -e "image_source=sftp://mgmt-server.local/images/ncs5500-x64-25.2.1.iso" \
  -e "image_filename=ncs5500-x64-25.2.1.iso"
```

**Step 3 — Post-check (run after protocols converge)**

```bash
python3 pyats/post_check.py \
  --testbed        pyats/testbed.yaml \
  --snapshot-dir   ./snapshots \
  --target-version 25.2.1
```

---

### Option C — Scope to specific devices

```bash
# pyATS
python3 pyats/pre_check.py --testbed pyats/testbed.yaml --devices router-1 router-2

# Ansible
ansible-playbook playbooks/upgrade_iosxr.yml --limit router-1

# run_upgrade.sh
./run_upgrade.sh -t 25.2.1 -s sftp://... -f ncs5500.iso -n router-1,router-2
```

---

## How the Health Checks Work

### What pyATS / Genie collects

`pre_check.py` and `post_check.py` run two categories of checks, both fully configurable in `pyats/checks.yaml` — no Python changes needed.

#### health_checks — numeric count assertions (7 minimum checks)

Each entry runs a show command, parses it with Genie, and asserts a numeric count post-upgrade is **≥ pre-upgrade**. Any drop = hard failure.

| # | Command | What it asserts |
|---|---------|----------------|
| 1 | `show platform` | Cards in `IOS XR RUN` ≥ pre |
| 2 | `show interfaces` | Interfaces UP ≥ pre |
| 3 | `show bgp summary` | BGP Established sessions ≥ pre |
| 4 | `show ospf neighbor` | OSPF FULL neighbors ≥ pre *(disable if IS-IS only)* |
| 5 | `show isis neighbor` | IS-IS UP neighbors ≥ pre *(disable if OSPF only)* |
| 6 | `show mpls ldp neighbor` | LDP neighbors ≥ pre |
| 7 | `show route summary` | Total IPv4 routes ≥ pre |

#### operational_checks — before/after diff (fail on any change)

Simpler checks where the full output must be identical pre/post:

| Command | Purpose |
|---------|---------|
| `show install active summary` | Confirms new image is active |
| `show redundancy summary` | Confirms RP redundancy state is stable |

Additional commands (disabled by default, opt-in via `enabled: true`):
`show ipv4 interface brief`, `show bgp vrf all summary`, `show mpls forwarding summary`

### Adding your own check

The simplest way — add one line to `operational_checks` in `pyats/checks.yaml`:

```yaml
- name:    my_check
  command: "show bgp neighbors"
  enabled: true
```

That's it. No Python needed.

### Two comparison passes in post_check.py

**1. Count comparison (hard failure)**  
Numeric counts (BGP sessions, OSPF neighbors, etc.) must not drop below baseline.

**2. Operational diff (hard failure)**  
`operational_checks` output is diffed using Genie semantic diff when a parser exists. Known-volatile fields (uptime, counters, sequence numbers, timers) are automatically excluded so only real changes surface.

### Why not raw CLI diff?

After any reload, these values **always** change:
- BGP `Up/Down` uptime resets to `00:00:xx`
- Interface `Last clearing of counters` timestamp changes
- OSPF dead timers reset
- IS-IS sequence numbers increment

Genie Diff excludes these known-noisy fields so you only see meaningful changes.

---

## Troubleshooting

**`pyats[full]` install is slow / large**  
Use targeted packages instead — see the commented section in `requirements.txt`.

**`genie` can't parse a command**  
Some commands may not have a Genie parser for your specific platform/version. `pre_check.py` / `post_check.py` catch this gracefully and record the error in the snapshot rather than aborting. Check the `error` key in the JSON output.

**Ansible `command_timeout` hit during image transfer**  
Increase `command_timeout` in `ansible.cfg` and the `install_add_timeout` variable in `upgrade_iosxr.yml`. On slow SFTP links, 45–60 minutes may be needed.

**Device doesn't return after `install activate`**  
The `wait_for_connection` task polls for up to 15 minutes. If the device has a hardware issue or boot loop, the rescue block will attempt `install rollback to committed`. Check OOB/console access.

**`No pre-check snapshot found` error in post_check.py**  
`pre_check.py` must run successfully before the upgrade. Snapshot files are stored in `./snapshots/` by default — ensure the path matches the `--snapshot-dir` argument passed to `post_check.py`.

---

## Security Notes

- Credentials are loaded from environment variables (`NET_USERNAME` / `NET_PASSWORD`) — never hardcoded.
- `snapshots/` is excluded from git (contains device state that may include sensitive routing info).
- For production use, consider Ansible Vault for credential management.
- Review `StrictHostKeyChecking=no` in `ansible.cfg` — replace with a populated `known_hosts` file in security-hardened environments.
