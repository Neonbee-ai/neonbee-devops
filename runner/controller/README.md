# nb-ci controller (design v3)

Always-on free-tier e2-micro `nbci-controller` (us-central1-a) running systemd `nbci` + Caddy. One VM per
job from image family `nb-ci-runner`; Spot for tests, standard for `nb-deploy`; 90-min DELETE limit; deleted
at job end / 5 min without a job; stopped VMs and orphan disks swept. route/notify run on 2 local runners.

| File | Role |
|---|---|
| `nbci.py` | controller loop, webhook receiver, VM lifecycle, spend cap, fallback, heartbeat, daily report |
| `nbci_logic.py` | pure decisions (tested in `tests/test_nbci.py`) |
| `nbci_io.py` | GitHub App / token, GCE + Secret Manager via the attached SA, Teams |
| `nb_release.py` | merge train, manifest order, migration hold, develop promotion, Pulse `released` |
| `provision.sh` | one-time: custom role, SA (no keys), secrets, firewall, the e2-micro |
| `bootstrap.sh` | VM boot: Caddy, code checkout via the App, systemd units, 2 local runners, daily self-update |
| `watchdog.sh` | dead-man check, restarts only `nbci` |
| `../image/provision.sh` | runner image contents (Node 22, runner, Playwright Chromium, gitleaks, semgrep, npm cache) |

Config: `ci/runner-labels.json` (classes, prices, minis, fallback, quota), `ci/release-manifest.json`.
Off switch: org variable `NB_CI_MODE=legacy|auto`. `DRY_RUN=1` (default) logs every action, changes nothing.
Ops: `nbci.py status` (read-only) · `once` · `drain` · `build-image` · `set-hook`.

GitHub App permissions: org self-hosted runners (write), org variables (write); repo actions, checks,
metadata (read), contents, pull requests, issues (write). Events: workflow_job, push, pull_request,
check_suite, workflow_run. Measured: a reconcile pass costs 29 calls cold, 1 call warm (+28 free 304s).
