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
    """Hard cap: at most one create per queued job per window. served: {job_id: unix time a VM was bought
    for it}. Returns (want trimmed to the unserved jobs, the job ids those creates will serve)."""
    open_ids = [j['id'] for j in waiting if now - served.get(str(j['id']), 0) >= window_s]
    n = min(len(want), len(open_ids))
    return want[:n], open_ids[:n]


def vm_verdict(vm, runner, job_done, now, no_job_s=NO_JOB_S):
    """Why to delete one runner VM, or None. vm: {status, created}; runner: GitHub runner or None;
    job_done: the controller saw workflow_job completed for this VM's runner."""
    if vm['status'] in DEAD:
        return 'stopped (%s): disk still bills' % vm['status']
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


def boot_plan(mode, family_link, provision_sh, jit):
    """How a job VM boots. 'image': the prebuilt nb-ci-runner family, metadata = the JIT config only.
    'startup': stock ubuntu-2404 (no custom image) that runs runner/image/provision.sh as its
    startup-script, then starts the one-job runner; any provisioning error powers the VM off (the
    controller deletes it). Returns (sourceImage, metadata items)."""
    items = [{'key': 'jit', 'value': jit}]
    if mode != 'startup':
        return family_link, items
    script = ('#!/bin/bash\nexport NB_STARTUP=1\ntrap poweroff ERR\n' + provision_sh +
              '\nsystemctl start --no-block nb-runner.service\n')
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
