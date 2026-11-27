# nb-ci runbook

Laptop-free CI + AutoQA. One always-on e2-micro `nbci-controller` (us-central1-a, ephemeral IP, project
`gen-lang-client-0521938031`) runs systemd `nbci` (loop + webhook receiver) and `nbci-guard` (churn guard) behind
Caddy, plus 2 `nb-ctl` runners. Job VMs: Spot `c2d-highcpu-8` (gate), standard (deploy), Spot e2 (AQA).

| What | Where |
|---|---|
| Webhook | `https://nbci.moonhive.in/gh` (DNS-only A record, TTL 60). Fallback `https://<ip-with-dashes>.sslip.io/gh` |
| Health | `https://nbci.moonhive.in/healthz` (200 while the loop heartbeat is < 10 min old) |
| Alerts | Pulse task **[nbci] alerts** (`71b8be5b`, Neonbee project): one comment per incident |
| Config | instance metadata `nbci-*` -> `/etc/nbci.env` at boot; code `/opt/nbci/src` = `nbci-ref` of neonbee-devops |
| State | `/var/lib/nbci/state.json` (atomic write each pass), heartbeat `/var/lib/nbci/heartbeat` |
| Logs | `sudo journalctl -u nbci -u nbci-guard -n 200` (no token or secret is ever logged) |

SSH is IAP-only (firewall: 443 open, 22 only from 35.235.240.0/20, everything else denied):
`gcloud compute ssh nbci-controller --zone us-central1-a --project gen-lang-client-0521938031 --tunnel-through-iap`

## Stop everything (one command)

```
runner/controller/nbci-stop-all.sh            # stop the controller VM, delete every nbci/aqa VM and orphan disk
runner/controller/nbci-stop-all.sh --legacy   # same + flip every NB_CI_MODE=auto repo to legacy
runner/controller/nbci-stop-all.sh --dry      # print only
```
Start again: `gcloud compute instances start nbci-controller --zone us-central1-a` (DNS + webhook follow
automatically within ~2 min; until then the sslip.io URL is used and the startup full poll covers queued jobs).

## Roll back to the legacy runners

1. Fastest: `nbci-stop-all.sh --legacy` (repo variables `NB_CI_MODE=legacy`; legacy repos never depend on nb-ci).
2. Code: revert the merge of neonbee-devops #172 on main: `git revert -m 1 <merge sha> && git push` (via a PR
   if main is protected). Callers on `@main` then get the pre-nb-ci workflows; leave the controller stopped.
3. Per repo: `gh variable delete NB_CI_MODE -R Neonbee-ai/<repo>` (unset = org default = legacy).

## Read the alerts

Every alert is one comment on Pulse `71b8be5b` starting with `[nbci]`. Deduped per incident (6 h window, daily for caps).

| Alert | Meaning / action |
|---|---|
| `ALERT OPEN: nbci controller down` | GCP uptime check (3 regions, 5 min) failing 10 min. Check the VM is RUNNING; `systemctl status nbci`; `resolved` follows |
| `controller heartbeat Ns old` | watchdog restarted `nbci` (loop hung) |
| `churn guard tripped (n/3 ...)` | VMs outnumbered queued jobs: the guard deleted the excess idle VMs (+ disks) and nbci keeps running. Tunables `GUARD_MAX_TRIPS` (3), `GUARD_TRIP_WINDOW_S` (1800) |
| `nbci STOPPED by the churn guard` | `GUARD_MAX_TRIPS` trips within `GUARD_TRIP_WINDOW_S` (deleting did not cure it); inspect, then `sudo rm -f /run/nbci-stopped-by-guard && sudo systemctl start nbci` |
| `guard deleted <vm>: no runner online for Ns` | Backstop (runs even while nbci is stopped): a VM had no runner online/busy for `GUARD_UNREG_S` (default 1200 s, always > the 900 s boot grace), so it could never take a job. Check `journalctl -u nbci` for 401s / gce list failures and that nbci is running |
| `gce list failed ... 401` | GCP token expired early. Tokens are now cached for `expires_in - 300 s` and a 401 drops the token and retries once (`nbci_io.GCP`); persistent 401 = service-account scope/key problem |
| `job ... queued N min` | a job waited > 15 min: quota, stock-out or labels with no class |
| `VM create failed` | GCE refused in every zone (quota/stockout/billing/api); jobs wait (no fallback runner, never a Mac Mini) and retry every pass |
| `daily VM-minutes cap reached` | `NB_DAILY_VM_MIN` (default 3000, above the $ cap so it is the hard backstop) hit: no VM starts until 00:00 UTC. Raise via metadata `nbci-daily-vm-min` + reboot |
| `CI spend today ... (capped)` | `$NB_DAILY_CAP_USD` hit: only deploy VMs start |
| `budget "nbci project budget": N%` | monthly project budget (INR 6000) 50/90/100 % |
| `AQA failed (exit N)` / `ended without a result` | the AQA VM's `<evidence>/result.json` says exit != 0, or none 15 min after the VM went; evidence.tgz next to it |
| `AQA VM ... reaped` | an aqa-* VM outlived `AQA_MAX_S` (2 h) |
| `controller restarted (ref ...)` | `nbci` started (crash, update, reboot); once per start |
| `controller updated A -> B` / `update to B FAILED health check: rolled back` | auto-update result; a failed sha is written to `/var/lib/nbci/bad-ref` and not retried |
| `VM ... idle > N s and not deleted` | a packed VM had no busy slot for `IDLE_S` + 2 min yet is RUNNING: the delete failed |
| `memory pressure on VM` | packed VM: available memory < 10 % or swap used > 2048 MB (sampled every 10 min from the serial console) |
| `orphan disk ...` / `orphan VM ...` | the sweeper found (and, outside DRY_RUN, deleted) an nbci-/aqa- disk or VM nothing owns |
| `daily VM-minutes at 80%` | 80 % of `NB_DAILY_VM_MIN` used today (the $ cap warns at 50/80/100 %) |
| `weekly image build failed` | runner VMs keep the previous `nb-ci-runner` image; retried after 20 h |

CI failures of a PR (not infra) go to the PR's Pulse task(s) from the commit trailers: comment, status `fixing`,
one bug per task+check; the same check green again closes the bug and moves the task to `reverify`.

## Merge train + promotion (continuous, laptop-free)

`merge-train.timer` (every 5 min) -> `merge-train.service` -> `merge-train-run.py` -> `nbci_train.py`. Metadata `nbci-merge-train` =
`develop` (default, ON) | `off`. Every open non-draft PR to develop in Neonbee-ai whose checks are all green and that is mergeable
merges with `gh pr merge --squash --match-head-commit <sha>` as the nb-ci App (no label, no deadline). Up to 10 per tick in parallel
(`MT_CAP`), one per repo, `so360-shell-fe` first. Skipped: neonbee-devops, drafts, conflicts, red/pending checks, labels `hold` /
`do-not-merge` / `needs-migration`, any `database/migrations/**` or `*.sql` file. Pause one PR: label it `hold`. Pause all:
`--metadata nbci-merge-train=off` + bootstrap re-run.

Promotion develop->main, metadata `nbci-promote-main` = `off` (default) | `on`. When on, a repo's develop head D is promoted (one
develop->main PR, merged with `--match-head-commit D`) only if `gs://neonbee-aqa-0521938031/replay/dev/<D>/result.json` says passed for
exactly D; not_tested, failed, missing or another sha never promotes. After the merge the Live result `replay/prod/<main sha>` must
pass, else one alert (Pulse alert task 71b8be5b + Teams; also when no result 90 min after the merge). State `/var/lib/merge-train/state.json`.
Turn promotion on:

    gcloud compute instances add-metadata nbci-controller --zone us-central1-a --project gen-lang-client-0521938031 --metadata nbci-promote-main=on
    sudo /opt/nbci/src/runner/controller/bootstrap.sh

## Night train

`night-train.timer` (every 15 min) -> `night-train.service` (oneshot, user nbci) -> `night-train-run.py` (GitHub App token +
`nbci-pulse-token` into the child env) -> `night-train.mjs`. State `/var/lib/night-train/state-<night>.json`, logs
`sudo journalctl -u night-train -n 100`. Arm a night: `gcloud compute instances add-metadata nbci-controller --zone us-central1-a
--metadata nbci-night-train=live,nbci-nt-deadline=2026-11-28T06:00:00Z` then `sudo /opt/nbci/src/runner/controller/bootstrap.sh`.
Stop now: `sudo -u nbci touch /var/lib/night-train/STOP` (final summary next tick) or `sudo systemctl disable --now night-train.timer`.
Merges are `gh pr merge --merge --match-head-commit` as the nb-ci App (never --admin); PRs with SQL/migrations and neonbee-devops are skipped.

## Rotate secrets (Secret Manager, project gen-lang-client-0521938031)

| Secret | Rotate | Then |
|---|---|---|
| `nbci-github-app-key` | GitHub App nb-ci -> Private keys -> generate; `gcloud secrets versions add nbci-github-app-key --data-file=key.pem`; delete the old key in GitHub | `sudo systemctl restart nbci nbci-guard` |
| `nbci-webhook-secret` | `openssl rand -hex 32 \| tee /dev/stderr \| gcloud secrets versions add nbci-webhook-secret --data-file=-`; paste the same value into the App's webhook secret | restart `nbci` |
| `nbci-pulse-token` | new Pulse MCP token -> `gcloud secrets versions add nbci-pulse-token --data-file=-` | restart `nbci`; redeploy the relay (`runner/alert-relay/deploy.sh`) |
| `nbci-teams-webhook` | optional; empty = Pulse only | restart `nbci` |
| Cloudflare (`CF_PREVIEW_API_TOKEN`) | GitHub org secret only (used by the `ops/nbci-dns` workflow) | none |
| `aqa-*` | AQA secrets, read only by SA `aqa-runner` (IAM condition `aqa-` prefix) | none |

## Self-heal, auto-update, metrics (Pulse 652ffcd8)

* Auto-update: `nbci-update.timer` (every 10 min) runs `nbci_update.py`: when instance metadata `nbci-ref` is a branch
  (`main` = follow main) and it has a new sha, `bootstrap.sh` moves `/opt/nbci/src` and restarts `nbci`/`nbci-guard`;
  health = `nbci` active + `/healthz` 200 + loop heartbeat written after the restart and < 3 min old (waits up to 5 min).
  Failure: sha -> `/var/lib/nbci/bad-ref`, automatic rollback to the previous sha, alert. A 40-char sha in `nbci-ref` pins it.
  Logs: `journalctl -u nbci-update`. Manual move: `nbci-set-ref.sh <sha|main>` (also sets the scheduling below).
* Controller VM survives GCE events (run once, idempotent; also run by `nbci-set-ref.sh`, never by the controller):
  `gcloud compute instances set-scheduling nbci-controller --zone us-central1-a --project gen-lang-client-0521938031 --restart-on-failure --maintenance-policy=MIGRATE`
* Stuck jobs: a queued job > 20 min (`NBCI_STUCK_JOB_S`) of a served class with a VM bought but none live is re-bought
  next pass (served mark cleared + full queue re-poll). Failed webhook deliveries are redelivered every 30 min.
* Metrics (groundwork, no UI): JSONL in `gs://neonbee-aqa-0521938031/metrics/nbci/<YYYY-MM-DD>/<kind>-<HHMM>.jsonl`, batched,
  one object per kind per flush (<= every 5 min, in a thread; create-only, no overwrite). Kinds: `vm` (vm, class, slots,
  created, deleted, busy_min, idle_min, jobs, cost_usd, spot_lost, peak_mem_mb, peak_load), `job` (repo, job, labels, class,
  runner, queued_s, run_s, conclusion), `queue` (t, queued_by_class, oldest_s, live_vms, free_slots), `pass` (t, dur_s).
  Needs the controller SA to create objects under `metrics/` (it already creates `aqa/*.tgz` bundles in that bucket).
  Not written while `DRY_RUN=1`. Records buffered in memory are lost on a restart.

## Moving parts and their guards

* Resilience: `Restart=always` (nbci, nbci-guard), watchdog cron (heartbeat), full queued-job poll on every start,
  state on disk, failed webhook deliveries redelivered at start and every 30 min.
* Right-sizing (Pulse 652ffcd8, 2026-11-27): a class with a `small` block also has a small variant `<class>-s` (same
  labels, Spot policy, priority; never picked by `class_for`). Per pass, unmet jobs n = queued - free slots (big + small
  VMs of the class): full big VMs for each complete big load, the remainder r on small VMs while r <= `small.max_jobs`
  and live + new small VMs <= `small.max_vms`; otherwise one more big VM. Today: `legacy` 8 slots e2-standard-8 Spot
  ($0.177/h) / `legacy-s` 2 slots e2-standard-2 Spot ($0.048/h), max_jobs 4, max_vms 2; `legacy-prod` and `deploy` 4 slots
  e2-standard-8 standard ($0.280/h) / `-s` 2 slots e2-standard-4 standard ($0.143/h), max_jobs 2, max_vms 2. Every small
  slot keeps the big VM's vCPU + RAM per slot (legacy 1 + 4 GB, deploy 2 + 8 GB). Small VMs at their caps hold 6 IPs + the
  controller's = 7 of 8. Tune only in `ci/runner-labels.json`; tests: `tests/test_nbci_packing.py` class `Sizing`.
  Check: `journalctl -u nbci | grep 'created nbci-.*-s-'` shows the machine type each VM got.
* Packing (Pulse 652ffcd8): classes with `slots` > 1 (`legacy` 8, `legacy-prod` / `deploy` 4, small variants 2) boot stock ubuntu +
  `runner/image/provision.sh` + `runner/image/slots.sh` (no custom image). Each slot = user `nbs<i>`, own HOME / work dir /
  private /tmp, no sudo, no metadata access; the controller refills a slot after every job with a fresh one-job JIT
  config (metadata `jit-<i>`, runner name `slot-<i>`). The VM is reused while jobs flow, deleted after `IDLE_S` (180 s)
  with no busy slot, hard lifetime `MAX_VM_S` (4 h; no new job after 4 h - `JOB_MAX_S`). Scale-up = live quota
  (CPUS_ALL_REGIONS, CPUS, IN_USE_ADDRESSES, INSTANCES, SSD_TOTAL_GB) minus `quota.reserve_aqa` (1 VM: 4 vCPU + 1 IP) less
  what live aqa-* VMs hold. Deploy lanes (`legacy-prod`, `deploy`) are packed standard (non-Spot) VMs that only carry
  deploy-lane labels. A deleted VM stays listed (STOPPING) ~1 min and the delete re-fires each pass; its `vm` metric
  record is written once (`vm_emitted`).
  Deploy / roll back the controller code: `runner/controller/nbci-set-ref.sh <sha|main>` (see its header).
* Sweeper (every 2 min): nbci-* / aqa-* VMs no pool logic owns (never `nbci-controller` or MIG members): stopped, or a
  registered runner none busy past boot grace + idle window; unattached nbci-labelled or nbci-*/aqa-* disks > 10 min;
  offline nbci-*/aqa-* registrations whose VM is gone.
* Reaper (every pass): nbci VMs TERMINATED / job done / no job 5 min after boot / older than 90 min + 15 min
  (hard, busy or not); builder older than 2 h; every `aqa-*` VM TERMINATED or older than 2 h (MIG members excluded);
  unattached nbci disks after 10 min; offline `nbci-*` runner registrations.
* Cost: Spot for gate and AQA (tested), daily `$` cap, daily VM-minutes cap, monthly project budget -> Pulse.
* Security: job VMs get no SA (gate: read-only cache SA, deploy: read-write cache SA), the one-time JIT runner
  config is removed from instance metadata once the runner registers; webhook HMAC (sha256) checked before parsing;
  `ghr` (local runners) cannot reach the metadata server; unattended security upgrades, reboot 04:40 UTC if needed.
* Image: family `nb-ci-runner`, rebuilt by the loop when the newest is > 7 days old, only the newest kept.
* AQA contract: neonbee-qa `aqa/vm-startup.sh` writes `<evidence>/evidence.tgz` and `<evidence>/result.json`
  (`{"exit": N}`) before self-deleting.

## Controller-down alert path (no laptop, no GitHub-hosted runner)

`runner/alert-relay/deploy.sh` (idempotent): uptime check `nbci-controller-healthz` -> alert policy
`nbci controller down` -> Pub/Sub `nbci-alerts` <- project budget; push subscription -> Cloud Run
`nbci-alert-relay` (private, stock python image, `relay.py` in env, token as secret ref) -> Pulse comment, deduped by a
create-if-absent marker in `gs://nbci-alerts-0521938031`.
