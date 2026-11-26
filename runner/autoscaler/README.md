# nb-autoscaler — CI capacity + zero-agent release, one controller

Runs on the Internal Tools VM as systemd `nb-autoscaler` (`install-itvm.sh`; Restart=always, WatchdogSec,
cron `nb-autoscaler-watchdog.sh` dead-man check). Every 30 s it reconciles from GitHub + GCE state.

- **CI runs only on GCP Spot VMs** (stock ubuntu-2404 + `startup.sh`, no SA, ephemeral IP, auto-delete
  disk, 4 h max). 2 vCPU per job slot, up to MAX_VCPU=32, zones tried in order on stock-outs. One JIT
  (ephemeral) runner per job. A VM is deleted after 180 s idle, never while a runner is busy.
- **Mac Minis carry no CI labels** (`ci/runner-labels.json`): pre-push gate over ssh only; macmini-preview
  and -2 keep aqa-coord (AutoQA). **Fallback:** `fallback.runner` gets the CI labels only while GCP
  cannot start VMs (billing/quota/API error, or stock-out in 2+ zones) and a job waited 2 min; Teams
  alert; labels removed after GCP starts a VM.
- **Merge train** (`nb_release.py`, TRAIN_REPOS): PRs labelled `nb-merge` merge one at a time per
  repo+base once up to date with the base and every check is green (the Free plan has no merge queue).
- **After a green main run**: develop PR from the same branch is queued; Pulse ids in the PR title/body
  move to PULSE_RELEASED_STATUS.
- Heartbeat: file each pass, org var NB_AUTOSCALER_HEARTBEAT every 5 min, Teams every 6 h.

Ops: `nb_autoscaler.py status` · `once` · `drain`. DRY_RUN=1 (default) logs, changes nothing.
Tests: `python3 -m unittest tests.test_autoscaler`.
