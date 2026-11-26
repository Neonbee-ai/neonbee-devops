# nb-autoscaler — one controller for burst CI capacity

Replaces: `gcp-burst.sh up/hold/down`, NB_BURST toggling, gb* merge-gate VMs, the Cloud Run scaler, manual swaps.

- Runs on the Internal Tools VM as systemd `nb-autoscaler` (`install-itvm.sh`), polls every 30 s.
- Demand = queued jobs whose labels fit the burst pool (`ci/runner-labels.json`) and that no idle
  runner (Mac Mini or VM) can take after 20 s. Capacity = 2 vCPU per job slot, up to MAX_VCPU=32.
- VMs: Spot, stock ubuntu-2404, `startup.sh`, ephemeral IP, no service account, boot disk auto-delete,
  maxRunDuration 4 h. Each slot runs one JIT (ephemeral) runner per job; the controller refills slots
  while there is demand and deletes the VM after 180 s with no busy slot.
- Routing is unchanged: keep NB_BURST=off. Minis and VMs both carry `runner-vm`, first free wins.
- Alerts (TEAMS_WEBHOOK): Mini runner offline 5+ min, job queued 15+ min.
- PROMOTE_REPOS: after a release branch's main merge has all push runs green, enable auto-merge on the
  develop PR from the same branch (only where develop has required checks).

Ops: `nb_autoscaler.py status` (read-only) · `once` · `drain` (delete all its VMs). DRY_RUN=1 logs only.
Tests: `python3 -m unittest tests.test_autoscaler`.
