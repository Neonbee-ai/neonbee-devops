"""Pure decision logic of the nb-ci controller (no I/O). Unit-tested in tests/test_nbci.py."""
import json, re

DEAD = ('TERMINATED', 'STOPPED', 'SUSPENDED')
NO_JOB_S = 300          # a VM whose runner has not started a job 5 min after boot is deleted
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


def vm_verdict(vm, runner, job_done, now):
    """Why to delete one runner VM, or None. vm: {status, created}; runner: GitHub runner or None;
    job_done: the controller saw workflow_job completed for this VM's runner."""
    if vm['status'] in DEAD:
        return 'stopped (%s): disk still bills' % vm['status']
    if job_done:
        return 'job completed'
    if runner and runner.get('busy'):
        return None
    if now - vm['created'] >= NO_JOB_S:
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
PULSE_RE = re.compile(r'PULSE-([0-9a-f]{8})\b', re.I)
OK = {'success', 'skipped', 'neutral'}
GATE_CHECK = re.compile(r'(^|/ )Gate$')


def pulse_ids(*texts):
    seen = []
    for t in texts:
        for m in PULSE_RE.findall(t or ''):
            if m.lower() not in seen:
                seen.append(m.lower())
    return seen


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
    if not pulse_ids(pr.get('title')):
        return 'eject:title needs a Pulse id (PULSE-xxxxxxxx)'
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
    """Lowest group index that still has work (queued main PR, or merged but main not green yet)."""
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
