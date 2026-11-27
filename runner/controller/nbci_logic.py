"""Pure decision logic of the nb-ci controller (no I/O). Unit-tested in tests/test_nbci.py."""
import json, re, time


def ts(s):
    """RFC 3339 -> unix seconds, honouring the offset. GCE creationTimestamp is local to the API
    (e.g. 2026-11-26T11:41:13.922-08:00); reading it as UTC aged every new VM by 8 h, so the
    controller deleted runner VMs ~30 s after creating them."""
    import calendar
    base = calendar.timegm(time.strptime(s[:19], '%Y-%m-%dT%H:%M:%S'))
    m = re.search(r'([+-])(\d\d):?(\d\d)$', s[19:])
    return base - (1 if m.group(1) == '+' else -1) * (int(m.group(2)) * 3600 + int(m.group(3)) * 60) if m else base

DEAD = ('TERMINATED', 'STOPPED', 'SUSPENDED')
NO_JOB_S = 300          # a VM whose runner has not started a job 5 min after boot is deleted
NO_JOB_STARTUP_S = 900  # RUNNER_BOOT=startup: provision.sh runs first (apt, Node, runner, Playwright), ~5-8 min
BAD_RUN = {'failure', 'cancelled', 'timed_out', 'action_required', 'startup_failure', 'stale'}


def load(path):
    return json.load(open(path))


def lower(xs):
    return {x.lower() for x in xs}


def class_for(job_labels, classes):
    """First class whose labels contain all of the job's runs-on labels; None = not ours (e.g. macOS)."""
    want = lower(job_labels)
    for c in classes:
        if want <= lower(c['labels']):
            return c
    return None


def hourly_usd(c, prices):
    """Real per-hour price of one VM of class c (cores + RAM + external IPv4 + its disk)."""
    if c.get('host') == 'controller':
        return 0.0
    if c.get('price'):
        core_p, ram_p = c['price']['core'], c['price']['ram']
        return c['vcpu'] * core_p + c['ram_gb'] * ram_p + prices['ip_spot' if c['spot'] else 'ip_std'] + c.get('disk_gb', 30) * prices['pd_balanced_gb_month'] / 730.0
    core, ram, ip = (('spot_core', 'spot_ram_gb', 'ip_spot') if c['spot'] else ('std_core', 'std_ram_gb', 'ip_std'))
    disk = c.get('disk_gb', 30) * prices['pd_balanced_gb_month'] / 730.0
    return c['vcpu'] * prices[core] + c['ram_gb'] * prices[ram] + prices[ip] + disk


def spend_state(spent, cap):
    """'ok' | 'warn50' | 'warn80' | 'capped' for today's spend against the daily cap."""
    if cap <= 0:
        return 'ok'
    r = spent / cap
    return 'capped' if r >= 1 else 'warn80' if r >= 0.8 else 'warn50' if r >= 0.5 else 'ok'


def may_start(c, spend, free_vcpu, reserve_deploy):
    """Priority deploy > gate > AutoQA inside one quota. Deploy may use every free vCPU and ignores the
    spend cap (a pipeline that already passed its gate must be able to finish); other classes keep
    reserve_deploy vCPUs free and stop at the cap."""
    if c['priority'] == 0:
        return free_vcpu >= c['vcpu']
    return spend != 'capped' and free_vcpu - reserve_deploy >= c['vcpu']


def autoqa_allowance(free_vcpu, reserve_deploy, waiting_ci_vcpu):
    return max(0, free_vcpu - reserve_deploy - waiting_ci_vcpu)


def plan(waiting, pending_by_class, classes):
    """One VM per waiting job, minus VMs already booting for that class. Deploy first."""
    need = {}
    for j in waiting:
        c = class_for(j['labels'], classes)
        if c and c.get('host') != 'controller':
            need[c['name']] = need.get(c['name'], 0) + 1
    out = []
    for c in sorted(classes, key=lambda c: c.get('priority', 9)):
        n = need.get(c['name'], 0) - pending_by_class.get(c['name'], 0)
        out += [c] * max(0, n)
    return out


def pending_by_class(vms, runners, now, grace_s=NO_JOB_S):
    """VMs that are supply for waiting jobs: every live VM (PROVISIONING/STAGING/RUNNING) of the class whose
    runner is not busy. A VM's runner is registered at create time (offline until boot finishes), so a
    registered-but-offline VM inside the boot grace counts too; counting only unregistered VMs bought a
    second and third VM for one job."""
    out = {}
    for v in vms:
        if v['status'] not in ('PROVISIONING', 'STAGING', 'RUNNING'):
            continue
        r = runners.get(v['name'])
        if r and r.get('busy'):
            continue
        booting = now - v['created'] < grace_s
        if r is None or booting or (r.get('status') == 'online'):
            out[v['cls']] = out.get(v['cls'], 0) + 1
    return out


SERVE_WINDOW_S = 900


def cap_creates(want, waiting, served, now, window_s=SERVE_WINDOW_S):
    """Hard cap: at most one create per queued job per window; a packed VM (class 'slots' > 1) serves that many
    jobs. served: {job_id: unix time a VM was bought for it}. Returns (want trimmed to the unserved jobs, the job
    ids those creates will serve)."""
    open_ids = [j['id'] for j in waiting if now - served.get(str(j['id']), 0) >= window_s]
    out, ids = [], []
    for c in want:
        if len(ids) >= len(open_ids):
            break
        out.append(c); ids += open_ids[len(ids):len(ids) + slots_of(c)]
    return out, ids


LIFETIME = 'older than max lifetime'


def vm_verdict(vm, runner, job_done, now, no_job_s=NO_JOB_S, max_s=None):
    """Why to delete one runner VM, or None. vm: {status, created}; runner: GitHub runner or None;
    job_done: the controller saw workflow_job completed for this VM's runner. max_s: hard lifetime
    (GCE maxRunDuration should already have deleted it; this is the backstop, busy or not)."""
    if vm['status'] in DEAD:
        return 'stopped (%s): disk still bills' % vm['status']
    if max_s and now - vm['created'] >= max_s:
        return LIFETIME + ' (%d min)' % ((now - vm['created']) // 60)
    if vm.get('cls') == 'aqa':
        # AQA VMs have no runner: the VM deletes itself at exit; the reaper removes a TERMINATED one (above) and any older than 30 min
        return 'aqa VM older than 30 min' if now - vm['created'] >= 1800 else None
    if job_done:
        return 'job completed'
    if runner and runner.get('busy'):
        return None
    if now - vm['created'] >= no_job_s:
        return 'no job %ds after boot' % (now - vm['created'])
    return None


def classify_gce_error(text):
    t = text.lower()
    if 'billing' in t or 'payment' in t or 'accountdisabled' in t or 'account_disabled' in t:
        return 'billing'
    if 'quota' in t:
        return 'quota'
    if 'zone_resource_pool_exhausted' in t or 'does not have enough resources' in t or 'stockout' in t:
        return 'stockout'
    return 'api'


def gcp_failing(attempts):
    kinds = {k for _, k in attempts}
    hard = sorted(kinds & {'billing', 'quota', 'api'})
    if hard:
        return hard[0]
    if len({z for z, k in attempts if k == 'stockout'}) >= 2:
        return 'stockout'
    return None


def fallback_next(on, oldest_wait_s, failing, vm_started, after_s=120):
    if vm_started:
        return False
    if failing and oldest_wait_s >= after_s:
        return True
    return on


def percentile(xs, p):
    if not xs:
        return 0
    s = sorted(xs)
    return s[min(len(s) - 1, int(round(p / 100.0 * (len(s) - 1))))]


# ── release train ──────────────────────────────────────────────────────────────────────────────────
OK = {'success', 'skipped', 'neutral'}
GATE_CHECK = re.compile(r'(^|/ )Gate$')




def checks_verdict(check_runs):
    latest = {}
    for c in check_runs:
        if c['name'] not in latest or c['id'] > latest[c['name']]['id']:
            latest[c['name']] = c
    cs = list(latest.values())
    if any(c['status'] == 'completed' and c['conclusion'] not in OK for c in cs):
        return 'red'
    if any(c['status'] != 'completed' for c in cs):
        return 'pending'
    if not any(GATE_CHECK.search(c['name']) for c in cs):
        return 'no-gate'
    return 'green'


def train_action(pr, behind_by, verdict, group_open=True):
    """merge | update | wait | hold:<why> | eject:<why> for the head-of-line PR."""
    labels = {l['name'] for l in pr.get('labels', [])}
    if pr.get('draft'):
        return 'eject:draft'
    if not pr.get('_pulse_ok'):     # set by nb_release from nb_pulse.task_ids / missing_trailers
        return 'eject:every commit needs a "Pulse: <id>" trailer ("Pulse: none" for chore/docs/ci)'
    if 'needs-migration' in labels and 'migration-applied' not in labels:
        return 'hold:needs-migration (add migration-applied once the migration is applied)'
    if not group_open:
        return 'hold:release order (an earlier manifest group is not merged + green yet)'
    if pr.get('mergeable') is False or pr.get('mergeable_state') == 'dirty':
        return 'eject:merge conflict with base'
    if behind_by > 0:
        return 'update'
    if verdict == 'red':
        return 'eject:a check failed on the head commit'
    if verdict in ('pending', 'no-gate') or pr.get('mergeable') is None:
        return 'wait'
    return 'merge'


def main_green(runs):
    return bool(runs) and all(r['status'] == 'completed' and r['conclusion'] not in BAD_RUN for r in runs)


def group_of(repo, manifest):
    gs = manifest['groups']
    for i, g in enumerate(gs):
        if repo in g.get('repos', []):
            return i
    for i, g in enumerate(gs):
        if g.get('match') and re.search(g['match'], repo):
            return i
    return next(i for i, g in enumerate(gs) if g.get('default'))


def open_group(pending_repos, manifest):
    """Lowest group index that still has work. pending_repos: repos whose release item still blocks
    (see release_blocking)."""
    return min((group_of(r, manifest) for r in pending_repos), default=None)


def bypass(push, train_shas, bot_logins, allowed):
    """A push to main/develop that the train did not make -> reason string, else None. Detection only:
    the Free plan cannot prevent it."""
    ref = push.get('ref', '')
    if ref not in ('refs/heads/main', 'refs/heads/develop') or push.get('deleted'):
        return None
    who = (push.get('sender') or {}).get('login', '')
    if who in bot_logins or who in allowed:
        return None
    if push.get('after') in train_shas:
        return None
    return '%s pushed %s to %s outside the merge train' % (who or '?', (push.get('after') or '')[:9], ref[11:])


# ── isolation, retries, failure reporting (design v3.1) ───────────────────────────────────────────
def repo_mode(repo_var, org_var):
    """NB_CI_MODE per repo: a repo variable overrides the org one; anything but 'auto' is legacy."""
    v = (repo_var or '').strip() or (org_var or '').strip()
    return 'auto' if v == 'auto' else 'legacy'


def fallback_target(current_custom, fb_labels, on):
    """The fallback runner keeps its own labels; only fb_labels are added (on) or taken away (off)."""
    cur = set(current_custom)
    return sorted(cur | set(fb_labels)) if on else sorted(cur - set(fb_labels))


CODE_RE = re.compile(r'error TS\d+|##\[error\]Error:|Test Suites:.*\bfailed|\bTests:\s+\d+ failed|\bFAIL\s+\S+|'
                     r'\bELIFECYCLE\b|error Command failed|Cannot find module|SyntaxError:|AssertionError|npm ERR! Test failed')
INFRA_MARKERS = ('The runner has received a shutdown signal', 'The self-hosted runner lost communication',
                 'Communication with the server has been lost', 'lost communication with the server',
                 'Could not resolve host', 'Temporary failure resolving', 'getaddrinfo EAI_AGAIN', 'ECONNRESET',
                 'Connection reset by peer', 'Connection timed out', 'ssh: connect to host', 'No space left on device',
                 'Cannot allocate memory', '502 Bad Gateway', '503 Service Unavailable', '429 Too Many Requests',
                 'curl: (56)', 'curl: (7)', 'curl: (28)', 'Broken pipe', 'No route to host', 'preempted',
                 'Kex_exchange_identification', 'banner exchange')
ANSI = re.compile('\x1b\\[[0-9;]*[A-Za-z]')


def classify_log(text, conclusion='failure', runner_gone=False):
    """'code' | 'infra' | 'unknown'. A compile/test error anywhere means the code is at fault, whatever
    network noise is also in the log. A job whose runner vanished (Spot preemption, VM deleted) is infra."""
    t = ANSI.sub('', text or '')
    if CODE_RE.search(t):
        return 'code'
    if runner_gone or any(m in t for m in INFRA_MARKERS):
        return 'infra'
    return 'unknown'


def should_retry(kind, run_attempt):
    """Flaky infra is retried exactly once and is not a code failure; a second failure counts."""
    return kind == 'infra' and int(run_attempt or 1) == 1


def log_excerpt(text, n=20):
    """<= n lines around the first error (or the tail), secrets already masked by GitHub as ***."""
    lines = [ANSI.sub('', l)[:300] for l in (text or '').splitlines()]
    idx = next((i for i, l in enumerate(lines) if CODE_RE.search(l) or '##[error]' in l), None)
    if idx is None:
        return '\n'.join(lines[-n:])
    lo = max(0, idx - n // 2)
    return '\n'.join(lines[lo:lo + n])


def bug_key(task_id, check):
    return '%s|%s' % (task_id, check)


def pick_train(cands):
    """Independent per repo+base. cands: [(number, action)] oldest first. Every ejectable PR is ejected
    (never blocks the rest); the oldest merge-ready PR merges; if none is ready, ONLY the oldest PR that
    is behind is updated (one CI re-run at a time). waiting / held PRs block nobody."""
    out = [(n, a) for n, a in cands if a.startswith('eject:')]
    ready = [n for n, a in cands if a == 'merge']
    if ready:
        out.append((ready[0], 'merge'))
    else:
        behind = [n for n, a in cands if a == 'update']
        if behind:
            out.append((behind[0], 'update'))
    return out


def release_blocking(item, now, stale_s=7200):
    """Manifest order only couples PRs of the same release branch (release/*), and a stuck, held,
    ejected or red item stops blocking after stale_s, so one bad repo never freezes the release."""
    return item['branch'].startswith('release/') and item['state'] in ('queued', 'inflight') and now - item['since'] < stale_s


STOCK_IMAGE = 'projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64'


def boot_plan(mode, family_link, provision_sh, jit, slots=None, slots_sh=''):
    """How a job VM boots. 'image': the prebuilt nb-ci-runner family, metadata = the JIT config only.
    'startup': stock ubuntu-2404 (no custom image) that runs runner/image/provision.sh as its
    startup-script, then starts the one-job runner; any provisioning error powers the VM off (the
    controller deletes it). slots = [(i, runner name, JIT config)] -> a packed VM: always stock ubuntu +
    startup script (provision.sh, then runner/image/slots.sh starts one nb-slot@i service per slot), metadata
    jit-<i> (the slot's next one-job config) and slot-<i> (its runner name). Returns (sourceImage, metadata items)."""
    hdr = '#!/bin/bash\nexport NB_STARTUP=1\ntrap poweroff ERR\n'
    if slots:
        items = [{'key': 'jit-%d' % i, 'value': j} for i, _, j in slots] + \
                [{'key': 'slot-%d' % i, 'value': n} for i, n, _ in slots]
        script = hdr + provision_sh + '\nexport NB_SLOTS=%d\n' % len(slots) + slots_sh + '\n'
        return STOCK_IMAGE, items + [{'key': 'startup-script', 'value': script}]
    items = [{'key': 'jit', 'value': jit}]
    if mode != 'startup':
        return family_link, items
    script = (hdr + provision_sh + '\nsystemctl start --no-block nb-runner.service\n')
    return STOCK_IMAGE, items + [{'key': 'startup-script', 'value': script}]


CACHE_SA = {'ro': 'https://www.googleapis.com/auth/devstorage.read_only',
            'rw': 'https://www.googleapis.com/auth/devstorage.read_write'}


def service_accounts(c, project):
    """npm-cache bucket access for a job VM: class sa 'ro' (gate: PR code runs there, so it can never write the
    cache) or 'rw' (deploy: runs only after the gate, on develop/main). No sa = no service account at all."""
    k = c.get('sa')
    if not k:
        return []
    return [{'email': 'nbci-cache-%s@%s.iam.gserviceaccount.com' % (k, project), 'scopes': [CACHE_SA[k]]}]


LIVE = ('PROVISIONING', 'STAGING', 'RUNNING')


def churn_verdict(vms, runners, queued_by_class, since, now, window_s=300, cap=8, grace_s=0):
    """Auto-stop verdict for the controller-side guard. Parallel gate/deploy VMs are normal: a class only
    counts as churning when it has more idle (runner not busy) live VMs than queued jobs of that class, and has
    had for more than one reconcile window. More than `cap` live nbci VMs trips at once. `since` is the guard's
    own state {class: first-seen-over-supplied time}. Returns a reason or None."""
    live = [v for v in vms if v['status'] in LIVE and not v.get('builder') and v.get('cls') != 'aqa']
    if len(live) > cap:
        return '%d live nbci VMs (cap %d)' % (len(live), cap)
    idle = {}
    for v in live:
        if any(r.get('busy') for r in vm_runners(v, runners)):
            continue
        if grace_s and now - v.get('created', now) < grace_s:
            continue            # still booting (startup mode provisions ~10 min); the reaper owns a VM that never takes a job
        idle[v['cls']] = idle.get(v['cls'], 0) + 1
    over = {c: n - queued_by_class.get(c, 0) for c, n in idle.items() if n > queued_by_class.get(c, 0)}
    for c in list(since):
        if c not in over:
            del since[c]
    for c in over:
        since.setdefault(c, now)
    for c, t in since.items():
        if now - t > window_s:
            return 'class %s: %d idle VM(s) beyond its %d queued job(s) for %ds' % (c, over[c], queued_by_class.get(c, 0), now - t)
    return None


def aqa_reap_verdict(vm, now, max_s):
    """Any aqa-* VM (ours or the AQA agent's), except managed-instance-group members (their group owns them):
    TERMINATED -> delete (a stopped VM's disk bills); older than max_s -> delete. vm: {status, created, mig}."""
    if vm.get('mig'):
        return None
    if vm['status'] in DEAD:
        return 'stopped (%s)' % vm['status']
    if now - vm['created'] >= max_s:
        return 'older than %d min' % (max_s // 60)
    return None


def vm_minutes(vms, dt_s):
    """VM-minutes consumed since the last pass by live VMs (any class, AQA included)."""
    return sum(dt_s / 60.0 for v in vms if v['status'] in LIVE)


def minutes_gate(used, cap):
    """True while VMs may still start today (all classes, deploy included); False at/after the cap."""
    return cap <= 0 or used < cap


def alert_key(text):
    """Dedupe key of one incident: the text with numbers and ids blanked, so 'queued 16 min' and 'queued 17 min'
    of the same job are one incident only if the caller also passes a job id."""
    return re.sub(r'[0-9a-f]{7,40}|\d+(\.\d+)?', '#', text.lower())[:120]


# workflow_job: the queued-job poll is the truth (old 'queued' events would look like live demand); push: old pushes
# would raise false BYPASS alerts. Redelivered workflow_run older than 2 h is ignored by the receiver.
REDELIVER_EVENTS = ('workflow_run', 'pull_request', 'check_suite', 'check_run')


def redeliver_ids(deliveries, now, seen, max_age_s=86400):
    """Webhook deliveries to redeliver: failed (status_code not 2xx), not a redelivery itself, younger than
    max_age_s, one attempt per GUID (seen = GUIDs already retried). deliveries: GitHub /app/hook/deliveries."""
    ok_guids = {d['guid'] for d in deliveries if 200 <= int(d.get('status_code') or 0) < 300}
    out = []
    for d in deliveries:
        if d.get('redelivery') or d['guid'] in ok_guids or d['guid'] in seen or d.get('event') not in REDELIVER_EVENTS:
            continue
        if now - ts(d['delivered_at']) > max_age_s:
            continue
        out.append(d['id']); seen.add(d['guid'])
    return out


def dns_plan(resolved, ip):
    """(webhook url host to use, whether to request a DNS update). resolved: IPs the fixed host resolves to."""
    return ('fixed', False) if ip in resolved else ('sslip', True)


def image_build_due(age_d, max_age_d, last_try, now, retry_s=20 * 3600):
    """Weekly rebuild: due when the newest image is older than max_age_d and no build was tried in retry_s
    (a failing build is not retried every hour)."""
    return age_d > max_age_d and now - (last_try or 0) >= retry_s


def aqa_outcome(result, gone_s, grace_s=900):
    """AQA verdict from <evidence>/result.json once the VM is gone. result: parsed JSON, None (404) or {'_err': ...}.
    'pass' (exit 0) | 'not_tested' (exit 4 / state not_tested) | 'failed (exit N)' | 'wait' (no result yet, inside grace) | 'ended without a result'."""
    if isinstance(result, dict) and 'exit' in result:
        try: code = int(result['exit'])
        except (TypeError, ValueError): code = -1
        if result.get('state') == 'not_tested' or code == 4:
            return 'not_tested'                 # 0 cases executed: never a pass (alerted like a failure)
        return 'pass' if code == 0 else 'failed (exit %s)' % result['exit']
    return 'wait' if gone_s < grace_s else 'ended without a result'


# ── packing (v4): N runner slots per VM, VM reuse, quota-driven scale-up, sweeper ─────────────────
IDLE_S = 180            # a packed VM with no busy slot for 3 min is deleted (VM + disk)
SLOT_RE = re.compile(r'^(?P<vm>.+)-s(?P<i>\d+)-[0-9a-z]+$')


def slots_of(c):
    """Runner slots per VM of class c: 1 = today's one-job VM; > 1 = a packed, reused VM."""
    return max(1, int((c or {}).get('slots', 1)))


def packed(c):
    return slots_of(c) > 1


def slot_name(vm, i, now):
    """Runner name of slot i's next job: unique per refill, <= 64 chars, maps back to its VM (vm_of)."""
    n, out = int(now * 10) % (36 ** 6), ''
    while True:
        n, d = divmod(n, 36); out = '0123456789abcdefghijklmnopqrstuvwxyz'[d] + out
        if not n:
            break
    return '%s-s%d-%s' % (vm, i, out)


def vm_of(runner_name, vm_names):
    """The VM a runner lives on: the VM itself (one-job VM) or the VM of a slot runner; None = no live VM."""
    if runner_name in vm_names:
        return runner_name
    m = SLOT_RE.match(runner_name or '')
    return m.group('vm') if m and m.group('vm') in vm_names else None


def vm_runners(vm, runners):
    """Runners registered for one VM: its slot runners (packed) or the runner named after it."""
    names = vm.get('slot_names') or {}
    if names:
        return [runners[n] for n in names.values() if n in runners]
    r = runners.get(vm['name'])
    return [r] if r else []


def capacity(quota, aqa_live=None, reserve=None):
    """Free room for CI VMs from LIVE quota usage, not a constant. quota: {metric: (limit, usage)} for
    CPUS_ALL_REGIONS / CPUS / IN_USE_ADDRESSES / INSTANCES / SSD_TOTAL_GB (+ optional cap MAX_VCPU). The AQA
    reserve (default 1 VM: 4 vCPU + 1 IP) stays free, less what AQA VMs already hold (they are in the usage)."""
    reserve = reserve or {'vcpu': 4, 'ips': 1}
    aqa_live = aqa_live or {}

    def free(*ms):
        xs = [int(quota[m][0] - quota[m][1]) for m in ms if m in quota]
        return max(0, min(xs)) if xs else 0
    # E2_CPUS is not used: live usage stays 0 with e2 VMs running (2026-11-27), e2 vCPUs count against CPUS
    out = {'vcpu': free('CPUS_ALL_REGIONS', 'CPUS'), 'ips': free('IN_USE_ADDRESSES'),
           'instances': free('INSTANCES'), 'ssd_gb': free('SSD_TOTAL_GB')}
    if 'MAX_VCPU' in quota:
        out['vcpu'] = max(0, min(out['vcpu'], int(quota['MAX_VCPU'][0] - quota['CPUS_ALL_REGIONS'][1])))
    out['vcpu'] = max(0, out['vcpu'] - max(0, reserve.get('vcpu', 0) - aqa_live.get('vcpu', 0)))
    out['ips'] = max(0, out['ips'] - max(0, reserve.get('ips', 0) - aqa_live.get('ips', 0)))
    return out


def fits(c, room):
    """One VM of class c fits the room (vCPU, one external IP, one instance, its pd-balanced disk)."""
    return room['vcpu'] >= c['vcpu'] and room['ips'] >= 1 and room['instances'] >= 1 and \
        room.get('ssd_gb', 10 ** 6) >= c.get('disk_gb', 30)


def take(c, room):
    return dict(room, vcpu=room['vcpu'] - c['vcpu'], ips=room['ips'] - 1, instances=room['instances'] - 1,
                ssd_gb=room.get('ssd_gb', 10 ** 6) - c.get('disk_gb', 30))


def refill_open(vm, c, now, max_s, job_max_s):
    """A packed VM takes new jobs only while a job started now still ends before its hard lifetime."""
    return packed(c) and now - vm['created'] < max_s - job_max_s


def free_slots(vm, c, runners, now, max_s, job_max_s):
    """Supply one live VM offers to waiting jobs: packed = slots not busy while refill is open (else its idle
    registered runners); one-job VM = 1 while its runner is not busy."""
    if vm['status'] not in LIVE:
        return 0
    rs = vm_runners(vm, runners)
    busy = sum(1 for r in rs if r.get('busy'))
    if packed(c):
        if refill_open(vm, c, now, max_s, job_max_s):
            return max(0, slots_of(c) - busy)
        return sum(1 for r in rs if not r.get('busy') and r.get('status') == 'online')
    return 0 if busy else 1


ROUTE_SPILL_S = 90


def aqa_max_live(cfg_max, ci_waiting):
    """AQA never starves CI: while CI jobs wait for a VM, AQA keeps only its reserved VM (quota.reserve_aqa)
    (2026-11-27: 3 aqa VMs live, 0 CI VMs, 16 jobs waiting)."""
    return min(cfg_max, 1) if ci_waiting > 0 else cfg_max


def spill_class(job_labels, classes):
    """First VM class (not the controller) whose labels also cover a controller job's labels, or None."""
    want = lower(job_labels)
    return next((c for c in classes if c.get('host') != 'controller' and want <= lower(c['labels'])), None)


def plan_packed(waiting, supply_by_class, classes, now=None, spill_s=ROUTE_SPILL_S):
    """VMs to buy: per class, ceil((waiting jobs - free slots) / slots). Deploy (priority 0, standard) first.
    A controller job (route/notify) waiting >= spill_s counts as demand for the packed class that also carries
    its label, so a backlog behind the 2 controller runners buys slots (2026-11-27: 16 queued, want=0)."""
    import math
    need = {}
    for j in waiting:
        c = class_for(j['labels'], classes)
        if c and c.get('host') == 'controller' and now is not None and now - j.get('created', now) >= spill_s:
            c = spill_class(j['labels'], classes)
        if c and c.get('host') != 'controller':
            need[c['name']] = need.get(c['name'], 0) + 1
    out = []
    for c in sorted(classes, key=lambda c: (c.get('priority', 9), bool(c.get('spot')))):
        n = need.get(c['name'], 0) - supply_by_class.get(c['name'], 0)
        out += [c] * max(0, int(math.ceil(n / float(slots_of(c)))))
    return out


def pool_verdict(vm, c, rs, last_busy, now, idle_s=IDLE_S, grace_s=NO_JOB_STARTUP_S, max_s=4 * 3600):
    """Why to delete one packed VM, or None. rs: its registered slot runners; last_busy: last time any slot was
    seen busy or finished a job (None = never). A busy slot keeps the VM except past the hard lifetime."""
    if vm['status'] in DEAD:
        return 'stopped (%s): disk still bills' % vm['status']
    if now - vm['created'] >= max_s:
        return LIFETIME + ' (%d min)' % ((now - vm['created']) // 60)
    if any(r.get('busy') for r in rs):
        return None
    if last_busy is None:
        return 'no job %ds after boot' % (now - vm['created']) if now - vm['created'] >= grace_s else None
    return 'idle %ds' % (now - last_busy) if now - last_busy >= idle_s else None


def slot_actions(vm, c, runners, done, issued, now, max_s, job_max_s, settle_s=60, grace_s=NO_JOB_STARTUP_S):
    """Per packed VM: (slots to refill with a fresh JIT config, slots whose JIT config can be scrubbed).
    A slot needs a new config once its one-job runner finished (done event) or is gone from GitHub (ephemeral
    runners deregister after their job), or never came online within the boot grace, and only while refill is
    open. issued: {runner name: unix time its config was generated}; a config younger than settle_s is never
    replaced (listing lag)."""
    refill, scrub = [], []
    names = vm.get('slot_names') or {}
    jits = vm.get('slot_jits') or set()
    for i in range(1, slots_of(c) + 1):
        n = names.get(i)
        r = runners.get(n) if n else None
        if r and (r.get('busy') or r.get('status') == 'online') and i in jits:
            scrub.append(i)
        finished = n is None or n in done or (r is None and now - issued.get(n, 0) >= settle_s)
        # a config that never came online (runner failed to start) is replaced once the boot grace is over
        stuck = r is not None and not r.get('busy') and r.get('status') != 'online' and \
            now - vm['created'] >= grace_s and now - issued.get(n, 0) >= grace_s
        finished = finished or stuck
        if finished and refill_open(vm, c, now, max_s, job_max_s):
            refill.append(i)
    return refill, scrub


def sweep_verdict(vm, runners, now, idle_s=IDLE_S, grace_s=NO_JOB_STARTUP_S, aqa_max_s=7200):
    """Sweeper for VMs the pool logic does not own (no nbci label: other code, a lost state, the AQA agent):
    nbci-* / aqa-* only, never the controller or MIG members. Deleted when stopped, or (with a registered
    runner) no runner busy and older than grace + idle; without a runner, aqa-* after aqa_max_s, nbci-* after
    grace. vm: {name, status, created, mig}; runners: the registrations that map to this VM."""
    n = vm['name']
    if n == 'nbci-controller' or vm.get('mig') or not n.startswith(('nbci-', 'aqa-')):
        return None
    if vm['status'] in DEAD:
        return 'stopped (%s)' % vm['status']
    age = now - vm['created']
    if runners:
        return None if any(r.get('busy') for r in runners) or age < grace_s + idle_s else 'no busy runner %ds' % age
    if n.startswith('aqa-'):
        return 'older than %d min' % (aqa_max_s // 60) if age >= aqa_max_s else None
    return 'unowned nbci VM, no runner, %ds old' % age if age >= grace_s + idle_s else None


def stale_runner(r, live_vm_names, now_unused=None):
    """An offline, idle nbci-*/aqa-* registration whose VM is gone. The controller's own nb-ctl runners never."""
    n = r['name']
    if r.get('busy') or r.get('status') != 'offline' or n.startswith('nbci-ctl-') or not n.startswith(('nbci-', 'aqa-')):
        return False
    return vm_of(n, live_vm_names) is None



def vm_name_of(runner_name):
    """VM of a runner name without a live VM list (the VM may be gone): a slot runner's VM, else the name."""
    m = SLOT_RE.match(runner_name or '')
    return m.group('vm') if m else runner_name


def lost_vms(known, current, deleted_by_us, stopped):
    """VMs GCE took back: seen live last pass and now gone without a delete of ours, or found stopped (a Spot
    preemption with instanceTerminationAction DELETE passes through TERMINATED)."""
    return sorted((set(known) - set(current) - set(deleted_by_us)) | (set(stopped) & set(known)))


def preempt_retry(f, preempted, retried):
    """Re-run the failed jobs of a run once when the job died with its VM: the runner's VM was preempted (Spot)
    and the job ended failure/cancelled. Any repo mode; at most once per run (retried: {run id: time})."""
    return f.get('conclusion') in ('failure', 'cancelled') and vm_name_of(f.get('runner', '')) in preempted \
        and str(f.get('run')) not in retried and int(f.get('attempt') or 1) == 1
