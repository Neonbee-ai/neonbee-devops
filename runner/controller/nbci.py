#!/usr/bin/env python3
"""nb-ci controller: the one always-on piece of Neonbee CI. Runs on a free-tier e2-micro (us-central1)
as systemd `nbci` behind Caddy (HTTPS for the GitHub App webhook at https://<ip>.sslip.io/gh).

Per job, one VM: a queued workflow_job (webhook, reconciled by slow polling) whose labels match a
class in ci/runner-labels.json gets one VM from image family nb-ci-runner with a one-job JIT runner:
Spot for gate/test classes, standard for `nb-deploy` (a preemption cannot leave a partial deploy).
Every VM: max-run-duration 90 min with DELETE, no service account, ephemeral IP, auto-delete disk.
Deleted when its job completes, when no job started 5 min after boot, or when stopped (a guest
shutdown only STOPS a VM and its disk keeps billing). Orphan nb-ci disks are swept every pass.
route/notify jobs (nb-route) run on 2 runners on this VM itself: no VM boot for 10-second jobs.

Budget: one 32-vCPU quota for CI + AutoQA, priority deploy > gate > AutoQA (AutoQA's allowance is
published as org variable NB_AUTOQA_VCPU_ALLOWANCE). Daily spend cap (NB_DAILY_CAP_USD, default 2)
computed from real prices; Teams at 50/80/100 %; at 100 % only deploy VMs start.
Fallback: macmini-preview-3 gets CI labels only while GCP cannot start VMs (billing, quota, API,
stock-out in 2+ zones). Off switch: org variable NB_CI_MODE=legacy -> no VMs, no train merges,
fallback runner ON (today's behaviour); auto -> this design.

GitHub API budget (App: 5,000 + 50 x (54-20) = 6,700 calls/h; 304 answers are free):
  webhooks deliver jobs/PR/push events; reconcile every 5 min: repos (ETag) + 1 runs list per
  repo pushed in the last 2 h (ETag) ~= 12 x (1 + ~10) = ~130/h; runners list 120/h; per VM ~3 calls;
  train ~4 calls per event + a 10-min sweep of TRAIN_REPOS pull lists (ETag). Typical < 800/h, a
  54-repo release day < 3,000/h. Without webhooks the reconcile tightens to 60 s (~1,500/h). The
  governor (GitHub.low) pauses train sweeps and reports below 1,000 left.

Modes: loop | once | status | drain | build-image | set-hook. DRY_RUN=1 (default) changes nothing.
"""
import hashlib, hmac, http.server, json, os, sys, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import nbci_logic as L
import nbci_aqa as A  # noqa: E402
import nb_release  # noqa: E402
import nb_pulse  # noqa: E402
from nbci_io import GCP, GitHub, job_log, log, meta, req, teams  # noqa: E402

E = os.environ.get
ORG = E('ORG', 'Neonbee-ai')
PROJECT = E('GCP_PROJECT', 'gen-lang-client-0521938031')
REGION = E('GCP_REGION', 'us-central1')
ZONES = E('GCP_ZONES', 'us-central1-a,us-central1-b,us-central1-c,us-central1-f').split(',')
CFG = os.path.join(HERE, '..', '..', 'ci')
STATE_FILE = E('STATE_FILE', '/var/lib/nbci/state.json')
HEARTBEAT_FILE = E('HEARTBEAT_FILE', '/var/lib/nbci/heartbeat')
POLL_S, RECONCILE_S = int(E('POLL_S', '30')), int(E('RECONCILE_S', '300'))
CAP_USD = float(E('NB_DAILY_CAP_USD', '2'))
MAX_RUN_S = int(E('MAX_RUN_S', '5400'))
# RUNNER_BOOT=image (prebuilt family) | startup (stock ubuntu + provision.sh as startup-script, no image)
RUNNER_BOOT = E('RUNNER_BOOT', 'image')
IMAGE_FAMILY, IMAGE_MAX_AGE_D = E('IMAGE_FAMILY', 'nb-ci-runner'), float(E('IMAGE_MAX_AGE_D', '7'))
REPORT_HOUR = int(E('REPORT_HOUR_UTC', '3'))
STEM = 'nbci'
# HANDS_OFF_LEGACY=1 (default; canary): the controller only ever touches its own nbci-* VMs/runners. No legacy
# runner deregistration, no fallback-Mini label changes, no org-variable writes. Set 0 once nb-ci owns the fleet.
HANDS_OFF = E('HANDS_OFF_LEGACY', '1') == '1'
STALE = ('nbci-',) if HANDS_OFF else tuple(E('STALE_RUNNER_PREFIXES', 'nbci-,gcp-burst-,contabo,nb-runner-').split(','))
BOT_LOGINS = set(E('BOT_LOGINS', 'nb-ci[bot]').split(','))
ALLOWED_PUSHERS = set(filter(None, E('ALLOWED_PUSHERS', '').split(',')))
DRY = E('DRY_RUN', '1') == '1'

LOCK = threading.Lock()
EVENTS = {'jobs': {}, 'done_runners': set(), 'dirty_repos': set(), 'last_event': 0, 'waits': [], 'pushes': [], 'failed': [], 'aqa': []}


ts = L.ts   # offset-aware (GCE timestamps are -08:00)


# ── webhook receiver (Caddy terminates TLS and proxies to 127.0.0.1:8787) ─────────────────────────
class Hook(http.server.BaseHTTPRequestHandler):
    secret = b''

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(min(int(self.headers.get('Content-Length', 0)), 5_000_000))
        sig = 'sha256=' + hmac.new(self.secret, body, hashlib.sha256).hexdigest()
        if not self.secret or not hmac.compare_digest(sig, self.headers.get('X-Hub-Signature-256', '')):
            self.send_response(401); self.end_headers(); return
        self.send_response(202); self.end_headers()
        try:
            on_event(self.headers.get('X-GitHub-Event', ''), json.loads(body))
        except Exception as e:
            log('event error', str(e)[:200])


def on_event(kind, ev):
    with LOCK:
        EVENTS['last_event'] = time.time()
        repo = (ev.get('repository') or {}).get('name', '')
        if kind == 'workflow_job':
            j = ev['workflow_job']
            if ev['action'] in ('queued', 'waiting'):
                EVENTS['jobs'][j['id']] = {'id': j['id'], 'repo': repo, 'name': j['name'], 'labels': j['labels'],
                                           'created': ts(j['created_at'])}
            else:
                EVENTS['jobs'].pop(j['id'], None)
                if ev['action'] == 'in_progress' and j.get('started_at'):
                    EVENTS['waits'].append(max(0, ts(j['started_at']) - ts(j['created_at'])))
                rn = j.get('runner_name') or ''
                if ev['action'] == 'completed' and rn.startswith(STEM + '-') and not rn.startswith('nbci-ctl-'):
                    EVENTS['done_runners'].add(rn)
                if ev['action'] == 'completed' and j.get('conclusion') in ('failure', 'cancelled') and rn.startswith(STEM + '-'):
                    EVENTS['failed'].append({'repo': repo, 'job': j['id'], 'run': j['run_id'], 'attempt': j.get('run_attempt', 1),
                                             'name': j['name'], 'conclusion': j['conclusion'], 'runner': rn})
        elif kind in ('pull_request', 'check_suite', 'workflow_run', 'check_run'):
            EVENTS['dirty_repos'].add(repo)
            wr = ev.get('workflow_run') or {}
            if kind == 'workflow_run' and ev.get('action') == 'completed' and wr.get('conclusion') == 'success':
                EVENTS['aqa'] = (EVENTS['aqa'] + [{'repo': repo, 'run': {k: wr.get(k) for k in (
                    'id', 'name', 'event', 'conclusion', 'head_branch', 'head_sha', 'workflow_id', 'html_url')}}])[-50:]
        elif kind == 'push':
            EVENTS['pushes'].append(dict(ev, _repo=repo))


# ── GitHub / GCE state ──────────────────────────────────────────────────────────────────────────────
def queued_jobs(gh):
    """Slow-poll truth for the job table (webhooks can be missed). Repos pushed in the last 2 h only."""
    out, since = {}, time.time() - 2 * 3600
    for r in gh('orgs/%s/repos?sort=pushed&direction=desc&per_page=100' % ORG, cache=True):
        if r['archived'] or not r['pushed_at'] or ts(r['pushed_at']) < since:
            continue
        runs = gh('repos/%s/actions/runs?status=queued&per_page=30' % r['full_name'], cache=True)['workflow_runs'] + \
            gh('repos/%s/actions/runs?status=in_progress&per_page=30' % r['full_name'], cache=True)['workflow_runs']
        for run in runs:
            for j in gh('repos/%s/actions/runs/%s/jobs?filter=latest&per_page=100' % (r['full_name'], run['id']), cache=True)['jobs']:
                if j['status'] == 'queued' and j['labels']:
                    out[j['id']] = {'id': j['id'], 'repo': r['name'], 'name': j['name'], 'labels': j['labels'],
                                    'created': ts(j['created_at'])}
    return out


def runners(gh):
    out, pg = [], 1
    while True:
        d = gh('orgs/%s/actions/runners?per_page=100&page=%d' % (ORG, pg), cache=True)
        out += d['runners']
        if not d['runners'] or len(out) >= d['total_count']:
            return out
        pg += 1


def our_vms(gcp):
    vms = []
    for z in ZONES:
        for v in gcp.compute('/zones/%s/instances?filter=labels.%s%%3D1' % (z, STEM)).get('items', []):
            lab = v.get('labels', {})
            vms.append({'name': v['name'], 'zone': z, 'status': v['status'], 'created': ts(v['creationTimestamp']),
                        'cls': lab.get('nb-class', 'gate'), 'builder': lab.get('nb-role') == 'builder',
                        'vcpu': int(v['machineType'].rsplit('-', 1)[1])})
    return vms


def free_vcpu(gcp):
    g = {q['metric']: q for q in gcp.compute('')['quotas']}['CPUS_ALL_REGIONS']
    rq = {q['metric']: q for q in gcp.compute('/regions/' + REGION)['quotas']}
    r, fam = rq.get('CPUS', g), rq.get('E2_CPUS', rq.get('CPUS', g))   # E2 family quota (us-central1: 24)
    cap = int(E('MAX_VCPU', '32'))
    return max(0, min(int(g['limit'] - g['usage']), int(r['limit'] - r['usage']), int(fam['limit'] - fam['usage']),
                      cap - int(g['usage'])))


def image_link(gcp):
    if RUNNER_BOOT == 'startup':
        return L.STOCK_IMAGE
    return gcp.compute('/global/images/family/' + IMAGE_FAMILY)['selfLink']


def create_vm(gh, gcp, c, failures, image):
    name = '%s-%s-%s' % (STEM, c['name'], time.strftime('%m%d%H%M%S', time.gmtime()) + str(int(time.time() * 1000) % 1000))
    if DRY:
        log('DRY create', name, c['machine'], 'spot' if c['spot'] else 'standard'); return True
    jit = gh('orgs/%s/actions/runners/generate-jitconfig' % ORG, 'POST',
             {'name': name, 'runner_group_id': 1, 'labels': c['labels'], 'work_folder': '_work'})
    sched = {'instanceTerminationAction': 'DELETE', 'maxRunDuration': {'seconds': MAX_RUN_S},
             'automaticRestart': False, 'onHostMaintenance': 'TERMINATE'}
    if c['spot']:
        sched['provisioningModel'] = 'SPOT'
    prov = open(os.path.join(HERE, '..', 'image', 'provision.sh')).read() if RUNNER_BOOT == 'startup' else ''
    src, items = L.boot_plan(RUNNER_BOOT, image, prov, jit['encoded_jit_config'])
    for z in ZONES:
        body = {'name': name, 'machineType': 'zones/%s/machineTypes/%s' % (z, c['machine']),
                'labels': {STEM: '1', 'nb-class': c['name']}, 'scheduling': sched,
                'disks': [{'boot': True, 'autoDelete': True, 'initializeParams': {
                    'sourceImage': src, 'diskSizeGb': str(c.get('disk_gb', 30)),
                    'diskType': 'zones/%s/diskTypes/pd-balanced' % z, 'labels': {STEM: '1'}}}],
                'networkInterfaces': [{'network': 'global/networks/default',
                                       'accessConfigs': [{'type': 'ONE_TO_ONE_NAT', 'name': 'External NAT'}]}],
                'serviceAccounts': L.service_accounts(c, PROJECT), 'metadata': {'items': items}}
        try:
            gcp.wait_op(gcp.compute('/zones/%s/instances' % z, 'POST', body), z, 120)
            log('created', name, c['machine'], z, 'spot' if c['spot'] else 'standard'); return True
        except Exception as e:
            k = L.classify_gce_error(str(e)); failures.append((z, k)); log('create failed', z, k, str(e)[:160])
            if k in ('billing', 'quota'):
                break
    try: gh('orgs/%s/actions/runners/%s' % (ORG, jit['runner']['id']), 'DELETE')
    except RuntimeError: pass
    return False


def delete_vm(gh, gcp, vm, runner, why):
    """Never kills a running job: a busy runner keeps its VM, and the runner is deregistered first
    (GitHub refuses with 422 if it took a job in the meantime)."""
    if runner and runner.get('busy') and vm['status'] not in L.DEAD and why != 'job completed':
        return False
    log('delete', vm['name'], why)
    if DRY:
        return True
    if runner and vm['status'] not in L.DEAD and why != 'job completed':
        try: gh('orgs/%s/actions/runners/%s' % (ORG, runner['id']), 'DELETE')
        except RuntimeError as e:
            if ' 404 ' not in str(e):
                log('keep', vm['name'], 'runner took a job'); return False
    gcp.compute('/zones/%s/instances/%s' % (vm['zone'], vm['name']), 'DELETE')
    return True


def sweep_disks(gcp):
    for z in ZONES:
        for d in gcp.compute('/zones/%s/disks?filter=labels.%s%%3D1' % (z, STEM)).get('items', []):
            if not d.get('users') and time.time() - ts(d['creationTimestamp']) > 600:
                log('orphan disk', d['name'], d.get('sizeGb'), 'GB')
                if not DRY:
                    gcp.compute('/zones/%s/disks/%s' % (z, d['name']), 'DELETE')


def org_var(gh, name, value=None):
    if value is None:
        try: return gh('orgs/%s/actions/variables/%s' % (ORG, name), cache=True).get('value', '')
        except RuntimeError: return ''
    if DRY or HANDS_OFF:
        return value
    try: gh('orgs/%s/actions/variables/%s' % (ORG, name), 'PATCH', {'value': value})
    except RuntimeError:
        gh('orgs/%s/actions/variables' % ORG, 'POST', {'name': name, 'value': value, 'visibility': 'all'})
    return value


def set_fallback(gh, runner, on, labels):
    """Adds (on) or removes (off) ONLY the fallback labels; the runner's legacy labels are never touched,
    so legacy repos keep their capacity whatever the controller does."""
    have = sorted(l['name'] for l in runner['labels'] if l.get('type') == 'custom')
    want = L.fallback_target(have, labels, on)
    if have == want:
        return False
    log('fallback runner', runner['name'], 'ON' if on else 'OFF', want, '(hands off: skipped)' if HANDS_OFF else '')
    if not DRY and not HANDS_OFF:
        if want: gh('orgs/%s/actions/runners/%s/labels' % (ORG, runner['id']), 'PUT', {'labels': want})
        else: gh('orgs/%s/actions/runners/%s/labels' % (ORG, runner['id']), 'DELETE')
    return True


def repo_mode(gh, st, repo, org_mode, now):
    c = st.setdefault('modes', {}).get(repo)
    if c and now - c[1] < 300:
        return c[0]
    try: rv = gh('repos/%s/%s/actions/variables/NB_CI_MODE' % (ORG, repo), cache=True).get('value', '')
    except RuntimeError: rv = ''
    m = L.repo_mode(rv, org_mode)
    st['modes'][repo] = (m, now)
    return m


# ── image (family nb-ci-runner), rebuilt weekly ────────────────────────────────────────────────────
def build_image(gcp):
    """Builder VM from stock ubuntu-2404 runs runner/image/provision.sh and powers off; its disk becomes
    the next image of the family; builder + disk are deleted; only the 2 newest images are kept."""
    name = 'nbci-builder-%s' % time.strftime('%m%d%H%M', time.gmtime()); z = ZONES[0]
    script = open(os.path.join(HERE, '..', 'image', 'provision.sh')).read() + '\npoweroff\n'
    if DRY:
        log('DRY build-image', name); return
    body = {'name': name, 'machineType': 'zones/%s/machineTypes/e2-standard-4' % z,
            'labels': {STEM: '1', 'nb-role': 'builder'},
            'scheduling': {'provisioningModel': 'SPOT', 'instanceTerminationAction': 'DELETE',
                           'maxRunDuration': {'seconds': 3600}, 'automaticRestart': False, 'onHostMaintenance': 'TERMINATE'},
            'disks': [{'boot': True, 'autoDelete': False, 'initializeParams': {
                'sourceImage': 'projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64',
                'diskSizeGb': '30', 'diskType': 'zones/%s/diskTypes/pd-balanced' % z, 'labels': {STEM: '1'}}}],
            'networkInterfaces': [{'network': 'global/networks/default',
                                   'accessConfigs': [{'type': 'ONE_TO_ONE_NAT', 'name': 'External NAT'}]}],
            'serviceAccounts': [], 'metadata': {'items': [{'key': 'startup-script', 'value': script}]}}
    gcp.wait_op(gcp.compute('/zones/%s/instances' % z, 'POST', body), z)
    end = time.time() + 2400
    while time.time() < end and gcp.compute('/zones/%s/instances/%s' % (z, name))['status'] != 'TERMINATED':
        time.sleep(20)
    try:
        img = 'nb-ci-runner-%s' % time.strftime('%Y%m%d-%H%M', time.gmtime())
        gcp.wait_op(gcp.compute('/global/images', 'POST', {
            'name': img, 'family': IMAGE_FAMILY, 'sourceDisk': 'zones/%s/disks/%s' % (z, name),
            'labels': {STEM: '1'}, 'storageLocations': [REGION]}), None, 900)
        log('image built', img)
        imgs = sorted(gcp.compute('/global/images?filter=family%%3D%s' % IMAGE_FAMILY).get('items', []),
                      key=lambda i: i['creationTimestamp'], reverse=True)
        for old in imgs[1:]:     # keep only the newest image of the family
            gcp.compute('/global/images/%s' % old['name'], 'DELETE'); log('image deleted', old['name'])
    finally:
        try: gcp.wait_op(gcp.compute('/zones/%s/instances/%s' % (z, name), 'DELETE'), z)
        except Exception as e: log('builder delete', e)
        try: gcp.compute('/zones/%s/disks/%s' % (z, name), 'DELETE')
        except Exception as e: log('builder disk delete', e)


# ── state ───────────────────────────────────────────────────────────────────────────────────────────
def load_state():
    try: st = json.load(open(STATE_FILE))
    except Exception: st = {}
    for k, v in (('fallback', False), ('fallback_count', 0), ('spend', {}), ('alerted', {}), ('last', {}),
                 ('release', {}), ('day', {}), ('seen_vm', {})):
        st.setdefault(k, v)
    return st


def save_state(st):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        json.dump(st, open(STATE_FILE + '.tmp', 'w')); os.replace(STATE_FILE + '.tmp', STATE_FILE)
    except OSError as e:
        log('state save failed', e)


def every(st, key, seconds, now):
    if now - st['last'].get(key, 0) >= seconds:
        st['last'][key] = now; return True
    return False


def aqa_trigger(gh, gcp, st, now, today, failures):
    """Deploy -> AQA: ONE Spot VM per successful deploy run (dedupe repo+sha, daily cap), deleted by the
    controller once it powers off. Held (events kept) until ci/aqa.json says contract_ready."""
    cfg = L.load(os.path.join(CFG, 'aqa.json'))
    with LOCK:
        pending = list(EVENTS['aqa'])
        if cfg.get('enabled') and cfg.get('contract_ready'):
            EVENTS['aqa'] = []
    if not pending or not cfg.get('enabled'):
        return
    if not cfg.get('contract_ready'):
        log('aqa trigger held: contract_ready=false (%d deploy event(s) waiting)' % len(pending)); return
    ast = st.setdefault('aqa', {})
    for ev in pending:
        repo, run = ev['repo'], ev['run']
        env = A.deploy_env(run, cfg)
        key = A.dedupe_key(repo, run['head_sha'])
        if not env:
            continue
        ok, why = A.may_trigger(ast, key, today, cfg.get('daily_cap', 8))
        if not ok:
            log('aqa skip', key, why)
            if why == 'daily cap': alert_once(st, now, 'AQA daily cap reached (%s)' % cfg.get('daily_cap', 8))
            continue
        shas, msgs = [run['head_sha']], []
        try:
            prev = [r for r in gh('repos/%s/%s/actions/workflows/%s/runs?branch=%s&status=success&per_page=5' % (
                ORG, repo, run['workflow_id'], run['head_branch']))['workflow_runs'] if r['head_sha'] != run['head_sha']]
            base = prev[0]['head_sha'] if prev else None
            cm = gh('repos/%s/%s/compare/%s...%s' % (ORG, repo, base, run['head_sha']))['commits'] if base else \
                [gh('repos/%s/%s/commits/%s' % (ORG, repo, run['head_sha']))]
            shas, msgs = [c['sha'] for c in cm], [c['commit']['message'] for c in cm]
        except RuntimeError as e:
            log('aqa range lookup failed', key, str(e)[:120])
        name = 'nbci-aqa-%s' % time.strftime('%m%d%H%M%S', time.gmtime(now))
        items = A.metadata(env, run['head_sha'], cfg.get('bundle_template', 'aqa-bundle-{sha}').format(sha=run['head_sha']),
                           A.pulse_ids(msgs), shas, (cfg.get('evidence_template') or run['html_url']).format(repo=repo, sha=run['head_sha']),
                           cfg.get('pass_stage', 'done'), repo)
        if DRY:
            log('DRY aqa create', name, env, key); A.record(ast, key, today, now); continue
        made = False
        for z in ZONES:
            try:
                gcp.wait_op(gcp.compute('/zones/%s/instances' % z, 'POST', A.vm_body(cfg, name, z, items, STEM, MAX_RUN_S)), z, 120)
                log('aqa created', name, env, key, 'tasks=%s' % ','.join(A.pulse_ids(msgs))); made = True; break
            except Exception as e:
                k = L.classify_gce_error(str(e)); failures.append((z, k)); log('aqa create failed', z, k, str(e)[:160])
                if k in ('billing', 'quota'): break
        if made:
            A.record(ast, key, today, now)


def alert_once(st, now, text):
    k = 'aqa-' + time.strftime('%Y-%m-%d', time.gmtime(now)) + text[:20]
    if k not in st['alerted']:
        st['alerted'][k] = now; teams(text, DRY)


# ── one reconcile pass ──────────────────────────────────────────────────────────────────────────────
def tick(gh, gcp, st, mode='scale'):
    now = time.time(); today = time.strftime('%Y-%m-%d', time.gmtime(now))
    cfg = L.load(os.path.join(CFG, 'runner-labels.json')); manifest = L.load(os.path.join(CFG, 'release-manifest.json'))
    classes = cfg['classes']; byname = {c['name']: c for c in classes}; prices = cfg['prices_usd']
    alert = lambda t: teams(t, DRY)  # noqa: E731
    ci_mode = org_var(gh, 'NB_CI_MODE') or 'legacy'        # org default; repo variables override it
    rs = runners(gh); rby = {r['name']: r for r in rs}
    webhook_live = now - EVENTS['last_event'] < 900
    if every(st, 'reconcile', RECONCILE_S if webhook_live else 60, now) or mode != 'scale':
        polled = queued_jobs(gh)
        with LOCK:
            EVENTS['jobs'] = polled
    with LOCK:
        jobs = list(EVENTS['jobs'].values()); done = set(EVENTS['done_runners'])
        waits, EVENTS['waits'] = EVENTS['waits'], []
        pushes, EVENTS['pushes'] = EVENTS['pushes'], []
        dirty, EVENTS['dirty_repos'] = set(EVENTS['dirty_repos']), set()
        failed, EVENTS['failed'] = EVENTS['failed'], []
    day = st['day'].setdefault(today, {'usd': 0.0, 'vcpu_h': {}, 'jobs': 0, 'waits': [], 'create_fail': 0, 'fallback': 0, 'bypass': 0})
    day['waits'] = (day['waits'] + waits)[-5000:]
    failures, started = [], False
    try:
        vms = our_vms(gcp); gce_ok = True
    except Exception as e:
        vms, gce_ok = [], False; failures.append(('-', L.classify_gce_error(str(e)))); log('gce list failed', str(e)[:200])
    # spend ledger: real price x time each VM ran since the last pass
    dt_h = min(now - st['last'].get('ledger', now), 600) / 3600.0; st['last']['ledger'] = now
    for v in vms:
        if v['status'] in ('RUNNING', 'STAGING', 'PROVISIONING'):
            c = byname.get(v['cls'], byname['gate'])
            day['usd'] += L.hourly_usd(dict(c, vcpu=v['vcpu']), prices) * dt_h
            day['vcpu_h'][v['cls']] = day['vcpu_h'].get(v['cls'], 0) + v['vcpu'] * dt_h
    spend = L.spend_state(day['usd'], CAP_USD)
    levels = ('ok', 'warn50', 'warn80', 'capped')
    for lvl in levels[1:]:
        key = today + lvl
        if levels.index(spend) >= levels.index(lvl) and key not in st['alerted']:
            st['alerted'][key] = now
            alert('CI spend today $%.2f of $%.2f cap (%s)%s' % (day['usd'], CAP_USD, lvl,
                  ': only deploy VMs start until 00:00 UTC' if lvl == 'capped' else ''))
    # deletions (job done / no job in 5 min / stopped) -- idempotent
    live = []
    for v in vms:
        if v['builder']:
            continue
        r = rby.get(v['name'])
        why = L.vm_verdict(v, r, v['name'] in done, now, L.NO_JOB_STARTUP_S if RUNNER_BOOT == 'startup' else L.NO_JOB_S) \
            if mode != 'drain' else 'drain'
        if mode == 'status':
            print('vm   %-34s %-10s %-6s runner=%s delete=%s' % (v['name'], v['status'], v['cls'],
                  r and ('busy' if r['busy'] else r['status']), why))
            live.append(v); continue
        if why and gce_ok and delete_vm(gh, gcp, v, r, why):
            with LOCK: EVENTS['done_runners'].discard(v['name'])
            if why == 'job completed': day['jobs'] += 1
            continue
        live.append(v)
    if mode == 'drain':
        return
    # demand -> VMs (one per job), priority + quota + spend cap
    idle_names = {r['name'] for r in rs if r['status'] == 'online' and not r['busy']}
    pending = L.pending_by_class(live, rby, now, L.NO_JOB_STARTUP_S if RUNNER_BOOT == 'startup' else L.NO_JOB_S)
    waiting = [j for j in jobs if now - j['created'] >= 3]
    want = L.plan(waiting, pending, classes)
    served = st.setdefault('served', {})
    for k in [k for k, t in served.items() if now - t > 2 * L.SERVE_WINDOW_S]:
        del served[k]
    want, serve_ids = L.cap_creates(want, waiting, served, now)
    room = 0
    if mode == 'status':
        for j in waiting:
            c = L.class_for(j['labels'], classes)
            print('job  %-24s %-36s %4ds class=%s' % (j['repo'], j['name'][:36], now - j['created'], c and c['name']))
        print('mode=%s want=%s spend=$%.3f (%s) fallback=%s rate_left=%s' % (
            ci_mode, [c['name'] for c in want], day['usd'], spend, st['fallback'], gh.remaining))
        return
    # VMs only ever serve auto-lane labels (nb-auto / nb-deploy): legacy repos' jobs match no class.
    if want and gce_ok:
        try:
            room = free_vcpu(gcp); image = image_link(gcp)
        except Exception as e:
            room, image = 0, None; failures.append(('-', L.classify_gce_error(str(e))))
        reserve = cfg['quota']['reserve_deploy_vcpu']
        for c in want:
            if image and L.may_start(c, spend, room, reserve):
                if create_vm(gh, gcp, c, failures, image):
                    started = True; room -= c['vcpu']
                    if serve_ids: served[str(serve_ids.pop(0))] = now
                else:
                    day['create_fail'] += 1
            elif spend != 'capped' and room < c['vcpu'] and not any(v['status'] == 'RUNNING' for v in live):
                failures.append(('-', 'quota'))          # nothing of ours running, yet no room: someone else holds it
    try: aqa_trigger(gh, gcp, st, now, today, failures)
    except Exception as e: log('aqa trigger error', str(e)[:200])
    # AutoQA share of the one quota
    if every(st, 'autoqa', 120, now) and gce_ok:
        try:
            ci_need = sum(c['vcpu'] for c in want)
            org_var(gh, 'NB_AUTOQA_VCPU_ALLOWANCE', str(L.autoqa_allowance(free_vcpu(gcp), cfg['quota']['reserve_deploy_vcpu'], ci_need)))
        except Exception as e: log('autoqa allowance', e)
    # orphan disks, stale runners
    if gce_ok and every(st, 'disks', 120, now):
        try: sweep_disks(gcp)
        except Exception as e: log('disk sweep', e)
    names = {v['name'] for v in live}
    for r in rs:
        if r['status'] == 'offline' and not r['busy'] and r['name'].startswith(STALE) and r['name'] not in names \
                and not r['name'].startswith('nbci-ctl-'):
            log('deregister offline runner', r['name'])
            if not DRY:
                try: gh('orgs/%s/actions/runners/%s' % (ORG, r['id']), 'DELETE')
                except RuntimeError: pass
    # fallback Mini runner
    failing = L.gcp_failing(failures)
    oldest = max([now - j['created'] for j in waiting] or [0])
    new = L.fallback_next(st['fallback'], oldest if failing else 0, failing, started)
    fb = cfg['fallback']
    if new != st['fallback']:
        st['fallback'] = new
        if new: day['fallback'] += 1
        alert(('fallback runner %s ON: GCP cannot start VMs (%s)' % (fb['runner'], failing))
              if new else 'GCP started a VM again: fallback runner %s OFF' % fb['runner'])
    if fb['runner'] in rby:
        set_fallback(gh, rby[fb['runner']], st['fallback'], fb['labels'])
    # alerts: Mini fallback offline while needed, long queue, bypass
    for j in jobs:
        k = 'q%s' % j['id']
        if now - j['created'] > 900 and k not in st['alerted']:
            st['alerted'][k] = now; alert('job %s / %s queued %d min (labels %s)' % (j['repo'], j['name'], (now - j['created']) // 60, j['labels']))
    for p in pushes:
        why = L.bypass(p, set(st['release'].get('train_shas', [])), BOT_LOGINS, ALLOWED_PUSHERS)
        if why:
            day['bypass'] += 1; alert('BYPASS %s: %s (no prevention on the Free plan)' % (p['_repo'], why))
    st['alerted'] = {k: v for k, v in st['alerted'].items() if now - v < 3 * 86400}
    log('mode=%s jobs=%d vms=%d want=%d room=%d spend=$%.3f %s fallback=%s%s' % (
        ci_mode, len(jobs), len(live), len(want), room, day['usd'], spend, st['fallback'], ' gcp=' + failing if failing else ''))
    # Pulse queue (failures, fixes, released): drained a few per pass, retried, never blocks CI
    pq = nb_pulse.PulseQueue(st.setdefault('pulse', {}), nb_pulse.MCP(E('PULSE_MCP_URL', ''), st.get('_pulse_token', '')), log,
                             DRY or not (E('PULSE_MCP_URL') and st.get('_pulse_token')))
    pq.ingest_spool()

    def released(tid, note):
        pq._put('status', task=tid, status=E('PULSE_RELEASED_STATUS', 'released')); pq._put('comment', task=tid, text=note)
    rel_state = st['release']
    # flaky-infra retry for any failed job on an nb-ci runner (push runs too), once per run
    for f in failed:
        try:
            if repo_mode(gh, st, f['repo'], ci_mode, now) != 'auto' or str(f['run']) in rel_state.setdefault('retried', {}):
                continue
            kind = L.classify_log(job_log(gh, ORG, f['repo'], f['job']), f['conclusion'],
                                  runner_gone=f['conclusion'] == 'cancelled' and f['runner'] not in rby)
            if L.should_retry(kind, f['attempt']):
                log('infra retry', f['repo'], f['name'], f['run'])
                rel_state['retried'][str(f['run'])] = now
                if not DRY:
                    gh('repos/%s/%s/actions/runs/%s/rerun-failed-jobs' % (ORG, f['repo'], f['run']), 'POST', {})
        except Exception as e:
            log('retry check', f['repo'], str(e)[:120])
    # merge train: auto repos of TRAIN_REPOS only, on events + a sweep every 10 min within the rate budget
    if E('TRAIN_REPOS'):
        names = [r['name'] for r in gh('orgs/%s/repos?per_page=100' % ORG, cache=True) if not r['archived']] \
            if E('TRAIN_REPOS') == '*' else [x.strip() for x in E('TRAIN_REPOS').split(',') if x.strip()]
        auto = [r for r in names if repo_mode(gh, st, r, ci_mode, now) == 'auto']
        rel = nb_release.Release(gh, ORG, manifest, log, alert, rel_state, pq, DRY, auto)
        if every(st, 'train', 600, now) and not gh.low():
            rel.tick(released)
        else:
            for repo in dirty & set(auto):
                rel.tick(released, only=repo)
    try: pq.drain()
    except Exception as e: log('pulse drain', e)
    # weekly image rebuild (background thread; the loop keeps serving)
    if RUNNER_BOOT != 'startup' and every(st, 'image_check', 3600, now):
        try:
            age_d = (now - ts(gcp.compute('/global/images/family/' + IMAGE_FAMILY)['creationTimestamp'])) / 86400
        except Exception:
            age_d = 999
        if age_d > IMAGE_MAX_AGE_D:
            threading.Thread(target=lambda: _safe(build_image, gcp), daemon=True).start()
    # heartbeat + daily report
    try:
        os.makedirs(os.path.dirname(HEARTBEAT_FILE), exist_ok=True); open(HEARTBEAT_FILE, 'w').write(str(int(now)))
    except OSError: pass
    if every(st, 'hb_var', 300, now):
        try: org_var(gh, 'NB_AUTOSCALER_HEARTBEAT', str(int(now)))
        except RuntimeError: pass
    if every(st, 'hb_teams', 6 * 3600, now):
        alert('heartbeat: mode=%s, %d VMs, %d jobs queued, today $%.2f' % (ci_mode, len(live), len(jobs), day['usd']))
    if time.gmtime(now).tm_hour == REPORT_HOUR and every(st, 'report', 20 * 3600, now) and not gh.low():
        report(st, alert, today)
    st['day'] = {k: v for k, v in st['day'].items() if k >= time.strftime('%Y-%m-%d', time.gmtime(now - 35 * 86400))}


def report(st, alert, today):
    days = sorted(k for k in st['day'] if k < today)
    if not days:
        return
    d = st['day'][days[-1]]
    vh = ', '.join('%s %.1f' % kv for kv in sorted(d['vcpu_h'].items())) or '0'
    month = sum(st['day'][k]['usd'] for k in days if k[:7] == days[-1][:7])
    alert('daily %s: cost $%.2f (month $%.2f), vCPU-h %s, jobs %d, queue p50 %ds / p95 %ds, VM create failures %d, '
          'fallback activations %d, bypass pushes %d' % (days[-1], d['usd'], month, vh, d['jobs'],
                                                         L.percentile(d['waits'], 50), L.percentile(d['waits'], 95),
                                                         d['create_fail'], d['fallback'], d['bypass']))


def _safe(fn, *a):
    try: fn(*a)
    except Exception as e: log('%s failed: %s' % (fn.__name__, str(e)[:300]))


def set_hook(gh):
    """Point the GitHub App webhook at this VM (its ephemeral IP can change on a restart)."""
    ip = meta('instance/network-interfaces/0/access-configs/0/external-ip')
    url = 'https://%s.sslip.io/gh' % ip.replace('.', '-')
    if DRY:
        log('DRY webhook ->', url); return
    req('https://api.github.com/app/hook/config', 'PATCH', {'url': url, 'content_type': 'json'},
        {'Authorization': 'Bearer ' + gh.jwt(), 'Accept': 'application/vnd.github+json'})
    log('webhook ->', url)


def notify_systemd(msg):
    addr = E('NOTIFY_SOCKET')
    if addr:
        import socket
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            s.connect('\0' + addr[1:] if addr.startswith('@') else addr); s.sendall(msg.encode()); s.close()
        except OSError: pass


def main():
    mode = (sys.argv[1:] or ['loop'])[0]
    gcp = GCP(PROJECT); gh = GitHub(gcp); st = load_state()
    if not E('TEAMS_WEBHOOK'):
        try: os.environ['TEAMS_WEBHOOK'] = gcp.secret('nbci-teams-webhook')
        except Exception: pass
    try: st['_pulse_token'] = gcp.secret('nbci-pulse-token')
    except Exception: st['_pulse_token'] = ''
    if mode == 'build-image':
        if RUNNER_BOOT == 'startup':
            log('build-image refused: RUNNER_BOOT=startup (no custom images)'); return
        return build_image(gcp)
    if mode == 'set-hook':
        return set_hook(gh)
    if mode in ('status', 'drain', 'once'):
        tick(gh, gcp, st, mode if mode != 'once' else 'reconcile'); st.pop('_pulse_token', None); save_state(st); return
    try:
        Hook.secret = gcp.secret('nbci-webhook-secret').encode()
        srv = http.server.ThreadingHTTPServer(('127.0.0.1', int(E('HOOK_PORT', '8787'))), Hook)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        _safe(set_hook, gh)
    except Exception as e:
        log('webhook receiver off (polling only):', e)
    notify_systemd('READY=1'); teams('controller started (DRY_RUN=%d)' % DRY, DRY)
    while True:
        t = time.time()
        try: tick(gh, gcp, st)
        except Exception as e: log('tick error', str(e)[:300])
        tok = st.pop('_pulse_token', None); save_state(st); st['_pulse_token'] = tok
        notify_systemd('WATCHDOG=1')
        time.sleep(max(1, POLL_S - (time.time() - t)))


if __name__ == '__main__':
    main()
