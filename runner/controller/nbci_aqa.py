"""Deploy -> AQA trigger of the nb-ci controller: pure decisions (no I/O), unit-tested in tests/test_nbci.py.
A successful deploy workflow_run (Dev = develop, a preview = pr-<branch>, main = prod QA orgs only) buys ONE
AQA Spot VM. The VM create contract (service account, startup source, flags) lives in ci/aqa.json and the
trigger stays held until `contract_ready` is true."""
import re

PULSE_RE = re.compile(r'^\s*Pulse:\s*(.+?)\s*$', re.I | re.M)
ID_RE = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})?', re.I)


def slug(branch):
    return re.sub(r'[^a-z0-9]+', '-', branch.lower()).strip('-')[:40]


def deploy_env(run, cfg):
    """AQA environment for a finished workflow_run, or None when it is not a successful deploy."""
    if run.get('conclusion') != 'success' or run.get('event') not in ('push', 'workflow_dispatch', 'pull_request', 'workflow_run'):
        return None
    name = (run.get('name') or '').lower()
    if not any(w.lower() in name for w in cfg.get('deploy_workflows', ['deploy'])):
        return None
    if any(w.lower() in name for w in cfg.get('ignore_workflows', ['teardown', 'rollback'])):
        return None
    br = run.get('head_branch') or ''
    if 'preview' in name or br.startswith(('feat/', 'fix/')):
        return 'pr-' + slug(br) if br else None      # feat/** and fix/** pushes deploy a preview at pr-<branch-slug>
    if br == 'develop':
        return 'dev'
    if br == 'main':
        return 'prod'
    return None


def pulse_ids(messages):
    """Pulse task ids from `Pulse: <id>[, <id>]` commit trailers (`Pulse: none` and junk are ignored), de-duplicated in order."""
    out = []
    for m in messages:
        for line in PULSE_RE.findall(m or ''):
            for i in ID_RE.findall(line):
                if i.lower() not in out:
                    out.append(i.lower())
    return out


BUNDLE_DIRS = ('aqa/', 'engine/', 'comb-beeline/e2e/lib/', 'comb-beeline/e2e/journeys/')
BUNDLE_FILES = ('comb-beeline/e2e/package.json',)
BUNDLE_SKIP = ('node_modules', 'sessions', '.aqa-state')


def bundle_path(member):
    """Path inside the AQA bundle for one member of a GitHub tarball (`<owner>-<repo>-<sha>/...`), or None.
    Same selection as neonbee-qa aqa/bundle.sh: aqa/, engine/, e2e lib + journeys + package.json, minus
    node_modules, sessions, .aqa-state and *.test.mjs."""
    rel = member.split('/', 1)[1] if '/' in member else ''
    if not rel or rel.endswith('.test.mjs') or any(p in BUNDLE_SKIP for p in rel.split('/')):
        return None
    if rel in BUNDLE_FILES or any(rel.startswith(d) or rel + '/' == d for d in BUNDLE_DIRS):
        return rel.rstrip('/')
    return None


def gs_split(uri):
    """('bucket', 'object/path') from gs://bucket/object/path."""
    b, _, o = uri[5:].partition('/')
    return b, o


def dedupe_key(repo, sha):
    return '%s@%s' % (repo, sha)


def may_trigger(state, key, today, cap):
    """(ok, reason): one AQA VM per repo+sha ever, and at most `cap` per UTC day."""
    if key in state.get('done', {}):
        return False, 'duplicate'
    if state.get('days', {}).get(today, 0) >= cap:
        return False, 'daily cap'
    return True, ''


def record(state, key, today, now):
    state.setdefault('done', {})[key] = now
    days = state.setdefault('days', {})
    days[today] = days.get(today, 0) + 1
    for k in [k for k, t in state['done'].items() if now - t > 14 * 86400]:
        del state['done'][k]
    for d in [d for d in days if d < today][:-7]:
        del days[d]


def aqa_env(env):
    """The AQA VM knows dev|prod only: a preview (pr-<slug>) counts as dev with its own base host."""
    return 'prod' if env == 'prod' else 'dev'


def base_url(env, domain='skyoffice360.com'):
    return 'https://%s.%s' % (env, domain) if env.startswith('pr-') else ''


def sizing(n_tasks):
    """(machine, workers): e2-standard-2 up to 5 tasks, then 4 and 8 vCPU with aqa-workers = nproc."""
    if n_tasks <= 5:
        return 'e2-standard-2', None
    return ('e2-standard-4', 4) if n_tasks <= 15 else ('e2-standard-8', 8)


def vm_name(env, sha, n=1):
    return 'aqa-%s-%s-%d' % (aqa_env(env), sha[:8], n)


def pass_stage(env, cfg):
    return (cfg.get('pass_stage') or {}).get('pr' if env.startswith('pr-') else aqa_env(env), '')


def metadata(env, sha, bundle, ids, commits, evidence, stage, repo='', workers=None, shard='', dry=False):
    items = {'aqa-env': aqa_env(env), 'aqa-sha': sha, 'aqa-bundle': bundle, 'aqa-tasks': ','.join(ids), 'aqa-commits': commits if isinstance(commits, str) else ','.join(commits),
             'aqa-evidence': evidence, 'aqa-pass-stage': stage, 'aqa-repo': repo, 'aqa-base': base_url(env),
             'aqa-workers': str(workers) if workers else '', 'aqa-shard': shard, 'aqa-dry': '1' if dry else ''}
    return [{'key': k, 'value': v} for k, v in items.items() if v != '']


def vm_body(cfg, name, zone, items, stem, max_run_s):
    """Instance body from the AQA contract in ci/aqa.json: Spot, no static IP, boot disk deleted with the VM."""
    sa = cfg.get('service_account')
    meta = list(items)
    machine = cfg.get('machine', 'e2-standard-2')
    if cfg.get('startup_script'):
        meta.append({'key': 'startup-script', 'value': cfg['startup_script']})
    return {'name': name, 'machineType': 'zones/%s/machineTypes/%s' % (zone, machine),
            'labels': {stem: '1', 'nb-class': 'aqa'},
            'scheduling': {'provisioningModel': 'SPOT', 'instanceTerminationAction': 'DELETE', 'automaticRestart': False,
                           'onHostMaintenance': 'TERMINATE', 'maxRunDuration': {'seconds': cfg.get('max_run_s', 5400)}},
            'disks': [{'boot': True, 'autoDelete': True, 'initializeParams': {
                'sourceImage': cfg.get('image', 'projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64'),   # stock Ubuntu: vm-startup.sh is written for it; the nb-ci-runner image powers off when it finds no JIT config
                'diskSizeGb': str(cfg.get('disk_gb', 30)), 'diskType': 'zones/%s/diskTypes/pd-balanced' % zone, 'labels': {stem: '1'}}}],
            'networkInterfaces': [{'network': 'global/networks/default', 'accessConfigs': [{'type': 'ONE_TO_ONE_NAT', 'name': 'External NAT'}]}],
            'serviceAccounts': [{'email': sa, 'scopes': cfg.get('scopes', ['https://www.googleapis.com/auth/cloud-platform'])}] if sa else [],
            'metadata': {'items': meta}}


def gate_blocks(check_runs):
    """Reason AQA must not run for a sha, from its check runs: the newest 'Gate' (reusable 'gate / Gate' or 'Gate')
    failed, was cancelled, or has not finished. '' = no Gate exists (nothing to wait for) or it passed."""
    gates = sorted([c for c in check_runs if c.get('name') in ('gate / Gate', 'Gate')], key=lambda c: c.get('id', 0))
    if not gates:
        return ''
    g = gates[-1]
    if g.get('status') != 'completed':
        return 'gate pending'
    return '' if g.get('conclusion') == 'success' else 'gate %s' % g.get('conclusion')
