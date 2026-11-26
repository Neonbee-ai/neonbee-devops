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
No fallback runner: while GCP cannot start VMs (billing, quota, API, stock-out in 2+ zones) jobs wait and
Pulse is alerted; no Mac Mini ever gets CI labels. Off switch: org variable NB_CI_MODE=legacy -> no VMs,
no train merges; auto -> this design.

GitHub API budget (App: 5,000 + 50 x (54-20) = 6,700 calls/h; 304 answers are free):
  webhooks deliver jobs/PR/push events; reconcile every 5 min: repos (ETag) + 1 runs list per
  repo pushed in the last 2 h (ETag) ~= 12 x (1 + ~10) = ~130/h; runners list 120/h; per VM ~3 calls;
  train ~4 calls per event + a 10-min sweep of TRAIN_REPOS pull lists (ETag). Typical < 800/h, a
  54-repo release day < 3,000/h. Without webhooks the reconcile tightens to 60 s (~1,500/h). The
  governor (GitHub.low) pauses train sweeps and reports below 1,000 left.

Modes: loop | once | status | drain | build-image | set-hook. DRY_RUN=1 (default) changes nothing.
"""
import base64, hashlib, hmac, http.server, json, os, sys, threading, time

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
HEALTH_MAX_S = int(E('HEALTH_MAX_S', '600'))
STALE_EVENT_S = int(E('STALE_EVENT_S', '600'))
PULSE_URL = E('PULSE_MCP_URL') or 'https://pulse.moonhive.in/mcp'   # an empty value in /etc/nbci.env used to switch Pulse off silently
# Stable webhook host (DNS-only A record, updated on every boot by the ops/nbci-dns workflow); sslip.io is the fallback
NBCI_HOST = E('NBCI_HOST', 'nbci.moonhive.in')
DNS_BRANCH, DNS_REPO = E('NBCI_DNS_BRANCH', 'ops/nbci-dns'), E('NBCI_DNS_REPO', 'neonbee-devops')
ALERT_TASK = E('NBCI_ALERT_TASK', '71b8be5b')       # Pulse "[nbci] alerts" (Neonbee project): one comment per incident
DAILY_VM_MIN = float(E('NB_DAILY_VM_MIN', '3000'))   # all nbci + AQA VM-minutes per UTC day; at the cap no VM starts
AQA_MAX_S = int(E('AQA_MAX_S', '7200'))
PULSE_SKIP_CHECKS = set(filter(None, E('PULSE_SKIP_CHECKS', 'Gate').split(',')))   # the aggregate Gate job repeats its parts' failure             # any aqa-* VM (ours or the AQA agent's) older than this is reaped

LOCK = threading.Lock()
EVENTS = {'jobs': {}, 'done_runners': set(), 'dirty_repos': set(), 'last_event': 0, 'waits': [], 'pushes': [], 'failed': [], 'aqa': []}


ts = L.ts   # offset-aware (GCE timestamps are -08:00)


# ── webhook receiver (Caddy terminates TLS and proxies to 127.0.0.1:8787) ─────────────────────────
class Hook(http.server.BaseHTTPRequestHandler):
    secret = b''

    def log_message(self, *a):
        pass

    def do_GET(self):
        """/healthz for the GCP uptime check: 200 while the loop heartbeat is fresh, else 503. No data leaves."""
        try: age = time.time() - int(open(HEARTBEAT_FILE).read().strip())
        except (OSError, ValueError): age = 1e9
        ok = self.path.split('?')[0] == '/healthz' and age < HEALTH_MAX_S
        self.send_response(200 if ok else 503); self.send_header('Content-Type', 'text/plain'); self.end_headers()
        self.wfile.write(b'ok\n' if ok else b'stale\n')

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
                if time.time() - ts(j['created_at']) > STALE_EVENT_S:
                    return              # a redelivered/late event: the queued-job poll is the truth for old jobs
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
                                             'name': j['name'], 'conclusion': j['conclusion'], 'runner': rn,
                                             'sha': j.get('head_sha', ''), 'branch': j.get('head_branch') or '',
                                             'url': j.get('html_url', '')})
                if ev['action'] == 'completed' and j.get('conclusion') == 'success':
                    EVENTS['passed'] = (EVENTS.get('passed', []) + [{'repo': repo, 'name': j['name'], 'sha': j.get('head_sha', ''),
                                                                     'url': j.get('html_url', '')}])[-200:]
        elif kind in ('pull_request', 'check_suite', 'workflow_run', 'check_run'):
            EVENTS['dirty_repos'].add(repo)
            wr = ev.get('workflow_run') or {}
            if kind == 'workflow_run' and ev.get('action') == 'completed' and wr.get('conclusion') == 'success' \
                    and time.time() - ts(wr.get('updated_at') or wr.get('created_at') or '1970-01-01T00:00:00Z') < 7200:
                EVENTS['aqa'] = (EVENTS['aqa'] + [{'repo': repo, 'run': {k: wr.get(k) for k in (
                    'id', 'name', 'event', 'conclusion', 'head_branch', 'head_sha', 'workflow_id', 'html_url')}}])[-50:]
        elif kind == 'push':
            EVENTS['pushes'] = (EVENTS['pushes'] + [dict(ev, _repo=repo)])[-200:]


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
                        'vcpu': int(v['machineType'].rsplit('-', 1)[1]),
                        'jit': any(i['key'] == 'jit' for i in v.get('metadata', {}).get('items', []))})
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
             'automaticRestart': False, 'onHostMaintenance': 'TERMINATE' if c['spot'] else 'MIGRATE'}   # e2 standard (deploy) VMs cannot use TERMINATE
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
            k = L.classify_gce_error(str(e)); failures.append((z, k)); log('create failed', z, k, ' '.join(str(e).split())[:700])
            if k in ('billing', 'quota'):
                break
    try: gh('orgs/%s/actions/runners/%s' % (ORG, jit['runner']['id']), 'DELETE')
    except RuntimeError: pass
    return False


def delete_vm(gh, gcp, vm, runner, why):
    """Never kills a running job: a busy runner keeps its VM, and the runner is deregistered first
    (GitHub refuses with 422 if it took a job in the meantime)."""
    hard = why.startswith(L.LIFETIME)
    if runner and runner.get('busy') and vm['status'] not in L.DEAD and why != 'job completed' and not hard:
        return False
    log('delete', vm['name'], why)
    if DRY:
        return True
    if runner and vm['status'] not in L.DEAD and why != 'job completed':
        try: gh('orgs/%s/actions/runners/%s' % (ORG, runner['id']), 'DELETE')
        except RuntimeError as e:
            if ' 404 ' not in str(e) and not hard:
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
def build_image(gcp, on_fail=None):
    try:
        _build_image(gcp)
    except Exception as e:
        log('image build failed', str(e)[:300])
        if on_fail: on_fail('weekly image build failed: %s (runner VMs keep the previous image)' % str(e)[:200])


def _build_image(gcp):
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


AQA_BACKFILL = os.path.join(os.path.dirname(STATE_FILE), 'aqa-backfill.json')
RUN_KEYS = ('id', 'name', 'event', 'conclusion', 'head_branch', 'head_sha', 'workflow_id', 'html_url')


def take_backfill(now):
    """Deploy runs queued by `nbci.py aqa <repo>:<run_id> ...` (spool file, so the loop owns state + results)."""
    try:
        evs = json.load(open(AQA_BACKFILL)); os.unlink(AQA_BACKFILL)
    except (OSError, ValueError):
        return []
    return [dict(e, seen=now) for e in evs if isinstance(e, dict) and e.get('repo') and e.get('run')]


def queue_backfill(gh, args):
    """CLI: spool successful deploy runs (older than the 2 h webhook window) for one AQA replay each."""
    evs = []
    for a in args:
        repo, rid = a.split(':', 1)
        wr = gh('repos/%s/%s/actions/runs/%s' % (ORG, repo, rid))
        evs.append({'repo': repo, 'run': {k: wr.get(k) for k in RUN_KEYS}})
        log('aqa backfill', repo, rid, wr.get('name'), wr.get('head_branch'), (wr.get('head_sha') or '')[:9], wr.get('conclusion'))
    try: evs = json.load(open(AQA_BACKFILL)) + evs
    except (OSError, ValueError): pass
    json.dump(evs, open(AQA_BACKFILL + '.tmp', 'w')); os.replace(AQA_BACKFILL + '.tmp', AQA_BACKFILL)
    return evs


def aqa_trigger(gh, gcp, st, now, today, failures, vm_room=True):
    """Deploy -> AQA: ONE Spot VM per successful deploy run (dedupe repo+sha, daily cap), deleted by the
    controller once it powers off. Held (events kept) until ci/aqa.json says contract_ready."""
    cfg = L.load(os.path.join(CFG, 'aqa.json'))
    with LOCK:
        pending = st.pop('aqa_pending', []) + [dict(e, seen=e.get('seen', now)) for e in EVENTS['aqa']]   # persisted: survives a restart
        EVENTS['aqa'] = []
    pending += take_backfill(now)
    later = []
    try:
        if not pending or not cfg.get('enabled'):
            return
        if not cfg.get('contract_ready') or not vm_room:
            later = pending
            log('aqa trigger held: %s (%d deploy event(s) waiting)' % ('VM-minutes cap' if not vm_room else 'contract_ready=false', len(pending)))
            return
        _aqa_events(gh, gcp, st, now, today, failures, cfg, pending, later)
    finally:
        st['aqa_pending'] = [e for e in later if now - e.get('seen', now) < 7200][-50:]


def _aqa_events(gh, gcp, st, now, today, failures, cfg, pending, later):
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
        try:
            why_gate = A.gate_blocks(gh('repos/%s/%s/commits/%s/check-runs?per_page=100' % (ORG, repo, run['head_sha']))['check_runs'])
        except (RuntimeError, KeyError) as e:
            why_gate = ''; log('aqa gate lookup failed', key, str(e)[:100])
        if why_gate:
            log('aqa skip', key, why_gate)
            if why_gate != 'gate pending': A.record(ast, key, today, now)      # a failed gate never gets AQA for this sha
            else: later.append(ev)                                               # gate still running: retry next pass (2 h max)
            continue
        shas, msgs = [run['head_sha']], []
        try:
            prev = [r for r in gh('repos/%s/%s/actions/workflows/%s/runs?branch=%s&status=success&per_page=5' % (
                ORG, repo, run['workflow_id'], run['head_branch']))['workflow_runs'] if r['head_sha'] != run['head_sha']]
            base = prev[0]['head_sha'] if prev else None
            if run['head_branch'].startswith(('feat/', 'fix/')):
                base = 'develop'        # a preview's deployed range = the branch's commits not yet on develop
            cm = gh('repos/%s/%s/compare/%s...%s' % (ORG, repo, base, run['head_sha']))['commits'] if base else \
                [gh('repos/%s/%s/commits/%s' % (ORG, repo, run['head_sha']))]
            cm = cm[-100:]
            shas, msgs = [c['sha'] for c in cm], [c['commit']['message'] for c in cm]
        except RuntimeError as e:
            log('aqa range lookup failed', key, str(e)[:120])
        ids = A.pulse_ids(msgs)
        try:
            qa_sha = gh('repos/%s/%s/commits/%s' % (ORG, cfg['qa_repo'], cfg['qa_ref']))['sha']
            script = base64.b64decode(gh('repos/%s/%s/contents/%s?ref=%s' % (ORG, cfg['qa_repo'], cfg['startup_path'], qa_sha))['content']).decode()
        except (RuntimeError, KeyError) as e:
            log('aqa held: cannot read %s@%s' % (cfg.get('qa_repo'), cfg.get('qa_ref')), str(e)[:120]); continue
        machine, workers = A.sizing(len(ids))
        name = A.vm_name(env, run['head_sha'])
        commits = json.dumps([{'message': m[:500], 'branch': run['head_branch'], 'prBody': ''} for m in msgs])
        items = A.metadata(env, run['head_sha'], cfg['bundle_template'].format(qa_sha=qa_sha), ids, commits,
                           cfg['evidence_template'].format(env=A.aqa_env(env), sha=run['head_sha'], repo=repo),
                           A.pass_stage(env, cfg), repo, workers)
        vm_cfg = dict(cfg, machine=machine, startup_script=script)
        if DRY:
            log('DRY aqa create', name, env, key); A.record(ast, key, today, now); continue
        made = False
        for z in ZONES:
            try:
                gcp.wait_op(gcp.compute('/zones/%s/instances' % z, 'POST', A.vm_body(vm_cfg, name, z, items, STEM, MAX_RUN_S)), z, 120)
                log('aqa created', name, env, key, 'tasks=%s' % ','.join(ids)); made = True; break
            except Exception as e:
                k = L.classify_gce_error(str(e)); failures.append((z, k)); log('aqa create failed', z, k, str(e)[:160])
                if k in ('billing', 'quota'): break
        if made:
            A.record(ast, key, today, now)
            st.setdefault('aqa_runs', {})[name] = {'repo': repo, 'sha': run['head_sha'], 'env': env, 'created': now,
                                                   'evidence': cfg['evidence_template'].format(env=A.aqa_env(env), sha=run['head_sha'], repo=repo),
                                                   'tasks': ids}
        else:
            notify(st, 'AQA VM create failed for %s@%s (%s)' % (repo, run['head_sha'][:8], ','.join(sorted({k for _, k in failures})) or 'api'),
                   key='aqacreate' + key)


def reap_aqa(gcp):
    """Every aqa-* VM, ours or the AQA agent's: deleted when TERMINATED (a stopped VM's disk bills) or older than
    AQA_MAX_S (a hung replay). Managed-instance-group members are left to their group. Returns the names reaped."""
    out = []
    for z in ZONES:
        for v in gcp.compute('/zones/%s/instances?filter=name%%3Daqa-*' % z).get('items', []):
            mig = any(i['key'] == 'created-by' for i in v.get('metadata', {}).get('items', []))
            why = L.aqa_reap_verdict({'status': v['status'], 'created': ts(v['creationTimestamp']), 'mig': mig}, time.time(), AQA_MAX_S)
            if not why:
                continue
            log('reap aqa VM', v['name'], why); out.append((v['name'], why))
            if not DRY:
                gcp.compute('/zones/%s/instances/%s' % (z, v['name']), 'DELETE')
    return out


def aqa_results(gcp, st, now):
    """After each AQA VM we started is gone: read <evidence>/result.json ({"exit": N}) written by aqa/vm-startup.sh;
    exit != 0, or no result 15 min after the VM is gone -> one alert. Checked every 2 min."""
    runs = st.setdefault('aqa_runs', {})
    if not runs:
        return
    alive = set()
    for z in ZONES:
        alive |= {v['name'] for v in gcp.compute('/zones/%s/instances?filter=name%%3Daqa-*' % z).get('items', [])}
    for name, r in list(runs.items()):
        if name in alive:
            continue
        r.setdefault('gone', now)
        b, _, path = r['evidence'][5:].partition('/')
        try:
            res = gcp.storage_json(b, path + '/result.json')
        except Exception as e:
            res = None if ' 404 ' in str(e) else {'_err': str(e)[:80]}
        what = L.aqa_outcome(res, now - r['gone'])
        if what == 'wait':
            continue
        if what != 'pass':
            notify(st, 'AQA %s: %s@%s on %s (tasks %s). Evidence: %s/evidence.tgz' % (
                what, r['repo'], r['sha'][:8], r['env'], ','.join(r.get('tasks') or []) or '-', r['evidence']), key='aqares' + name)
        log('aqa result', name, what)
        del runs[name]
    for name in [n for n, r in runs.items() if now - r['created'] > 6 * 3600]:
        del runs[name]


def pulse_refs(gh, st, repo, sha, branch):
    """(Pulse task ids, PR number) of a failed job's commit: its open PR's commit trailers + branch + body, else the
    commit's own trailer. Cached per repo@sha for 6 h."""
    cache = st.setdefault('pulse_refs', {}); k = '%s@%s' % (repo, sha)
    hit = cache.get(k)
    if hit and time.time() - hit[2] < 6 * 3600:
        return hit[0], hit[1]
    ids, num = [], 0
    try:
        prs = [p for p in gh('repos/%s/%s/commits/%s/pulls' % (ORG, repo, sha)) if p.get('state') == 'open']
        if prs:
            num = prs[0]['number']
            msgs = [c['commit']['message'] for c in gh('repos/%s/%s/pulls/%s/commits?per_page=100' % (ORG, repo, num))]
            ids = nb_pulse.task_ids(msgs, prs[0]['head']['ref'], prs[0].get('body') or '')
        else:
            ids = nb_pulse.task_ids([gh('repos/%s/%s/commits/%s' % (ORG, repo, sha))['commit']['message']], branch)
    except (RuntimeError, KeyError, TypeError) as e:
        log('pulse refs', repo, sha[:8], str(e)[:120])
    cache[k] = (ids, num, time.time())
    for old in [x for x, v in cache.items() if time.time() - v[2] > 6 * 3600]:
        del cache[old]
    return ids, num


def scrub_jit(gcp, vm):
    """Remove the one-time JIT runner config from a VM's metadata once its runner is registered (no credential
    stays readable in instance metadata)."""
    if DRY:
        return
    i = gcp.compute('/zones/%s/instances/%s' % (vm['zone'], vm['name']))
    md = i.get('metadata', {})
    items = [x for x in md.get('items', []) if x['key'] != 'jit']
    gcp.compute('/zones/%s/instances/%s/setMetadata' % (vm['zone'], vm['name']), 'POST',
                {'fingerprint': md.get('fingerprint'), 'items': items})


def notify(st, text, key=None, window_s=6 * 3600):
    """One alert per incident: logged, sent to Teams when a webhook is configured, and queued (retried, never
    blocking) as ONE comment on the Pulse task ALERT_TASK. key (default: the text with numbers/ids blanked)
    dedupes repeats inside window_s."""
    now = time.time(); k = 'n:' + (key or L.alert_key(text))
    if now - st['alerted'].get(k, 0) < window_s:
        return False
    st['alerted'][k] = now
    teams(text, DRY)
    if ALERT_TASK:
        st.setdefault('pulse', {}).setdefault('q', []).append(
            {'op': 'comment', 'task': ALERT_TASK, 'text': '[nbci] %s (%s)' % (text, time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(now))),
             'tries': 0, 'next_at': 0, 'born': now})
    return True


def alert_once(st, now, text):
    notify(st, text, key='aqa-' + time.strftime('%Y-%m-%d', time.gmtime(now)) + text[:20], window_s=86400)


# ── one reconcile pass ──────────────────────────────────────────────────────────────────────────────
def tick(gh, gcp, st, mode='scale'):
    now = time.time(); today = time.strftime('%Y-%m-%d', time.gmtime(now))
    cfg = L.load(os.path.join(CFG, 'runner-labels.json')); manifest = L.load(os.path.join(CFG, 'release-manifest.json'))
    classes = cfg['classes']; byname = {c['name']: c for c in classes}; prices = cfg['prices_usd']
    alert = lambda t: notify(st, t)  # noqa: E731
    ci_mode = org_var(gh, 'NB_CI_MODE') or 'legacy'        # org default; repo variables override it
    rs = runners(gh); rby = {r['name']: r for r in rs}
    webhook_live = now - EVENTS['last_event'] < 900 and not EVENTS.get('need_full_poll', True)
    if every(st, 'reconcile', RECONCILE_S if webhook_live else 60, now) or mode != 'scale' or EVENTS.get('need_full_poll', True):
        EVENTS['need_full_poll'] = False   # first pass after a start: poll every queued job (webhook events sent while down are lost)
        polled = queued_jobs(gh)
        with LOCK:
            EVENTS['jobs'] = polled
    with LOCK:
        jobs = list(EVENTS['jobs'].values()); done = set(EVENTS['done_runners'])
        waits, EVENTS['waits'] = EVENTS['waits'], []
        pushes, EVENTS['pushes'] = EVENTS['pushes'], []
        dirty, EVENTS['dirty_repos'] = set(EVENTS['dirty_repos']), set()
        failed, EVENTS['failed'] = EVENTS['failed'], []
        passed, EVENTS['passed'] = EVENTS.get('passed', []), []
    day = st['day'].setdefault(today, {'usd': 0.0, 'vcpu_h': {}, 'jobs': 0, 'waits': [], 'create_fail': 0, 'fallback': 0, 'bypass': 0})
    day.setdefault('vm_min', 0.0)
    day['waits'] = (day['waits'] + waits)[-5000:]
    failures, started = [], False
    try:
        vms = our_vms(gcp); gce_ok = True
    except Exception as e:
        vms, gce_ok = [], False; failures.append(('-', L.classify_gce_error(str(e)))); log('gce list failed', str(e)[:200])
    # spend ledger: real price x time each VM ran since the last pass
    dt_h = min(now - st['last'].get('ledger', now), 600) / 3600.0; st['last']['ledger'] = now
    day['vm_min'] += L.vm_minutes(vms, dt_h * 3600)
    vm_room = L.minutes_gate(day['vm_min'], DAILY_VM_MIN)
    if not vm_room:
        notify(st, 'daily VM-minutes cap reached: %.0f of %.0f min today; no VM (deploy, gate or AQA) starts until 00:00 UTC. '
               'Raise NB_DAILY_VM_MIN in /etc/nbci.env to lift it.' % (day['vm_min'], DAILY_VM_MIN), key='vmcap' + today, window_s=86400)
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
            if mode == 'scale' and gce_ok and now - v['created'] > 7200 and not DRY:      # builder backstop (normal build < 40 min)
                log('delete', v['name'], 'builder ' + L.LIFETIME); _safe(gcp.compute, '/zones/%s/instances/%s' % (v['zone'], v['name']), 'DELETE')
            continue
        r = rby.get(v['name'])
        why = L.vm_verdict(v, r, v['name'] in done, now, L.NO_JOB_STARTUP_S if RUNNER_BOOT == 'startup' else L.NO_JOB_S,
                           MAX_RUN_S + 900) if mode != 'drain' else 'drain'
        if mode == 'status':
            print('vm   %-34s %-10s %-6s runner=%s delete=%s' % (v['name'], v['status'], v['cls'],
                  r and ('busy' if r['busy'] else r['status']), why))
            live.append(v); continue
        if why and gce_ok and delete_vm(gh, gcp, v, r, why):
            with LOCK: EVENTS['done_runners'].discard(v['name'])
            if why == 'job completed': day['jobs'] += 1
            continue
        if v.get('jit') and r and gce_ok and mode == 'scale':     # runner registered: the one-time JIT config leaves metadata
            try: scrub_jit(gcp, v); log('jit scrubbed', v['name'])
            except Exception as e: log('jit scrub', v['name'], str(e)[:120])
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
            if image and vm_room and L.may_start(c, spend, room, reserve):
                if create_vm(gh, gcp, c, failures, image):
                    started = True; room -= c['vcpu']
                    if serve_ids: served[str(serve_ids.pop(0))] = now
                else:
                    day['create_fail'] += 1
                    kinds = ','.join(sorted({k for _, k in failures})) or 'api'
                    notify(st, 'VM create failed: class %s (%s) in every zone tried; its jobs wait for the next pass or the fallback runner'
                           % (c['name'], kinds), key='create' + c['name'] + kinds)
            elif spend != 'capped' and room < c['vcpu'] and not any(v['status'] == 'RUNNING' for v in live):
                failures.append(('-', 'quota'))          # nothing of ours running, yet no room: someone else holds it
    if gce_ok and every(st, 'aqa-reap', 120, now):
        try:
            for name, why in reap_aqa(gcp):
                if 'older' in why: notify(st, 'AQA VM %s reaped: %s (replay hung or never self-deleted)' % (name, why), key='reap' + name)
        except Exception as e: log('aqa reap', str(e)[:160])
        try: aqa_results(gcp, st, now)
        except Exception as e: log('aqa results', str(e)[:160])
    try: aqa_trigger(gh, gcp, st, now, today, failures, vm_room)
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
    # fallback runner: none configured (fallback.runner null) -> state + alert only, no labels moved
    failing = L.gcp_failing(failures)
    oldest = max([now - j['created'] for j in waiting] or [0])
    new = L.fallback_next(st['fallback'], oldest if failing else 0, failing, started)
    fb = cfg['fallback']
    if new != st['fallback']:
        st['fallback'] = new
        if new: day['fallback'] += 1
        alert(('GCP cannot start VMs (%s): jobs wait (fallback runner: %s)' % (failing, fb.get('runner') or 'none'))
              if new else 'GCP started a VM again (fallback runner: %s)' % (fb.get('runner') or 'none'))
    if fb.get('runner') and fb['runner'] in rby:
        set_fallback(gh, rby[fb['runner']], st['fallback'], fb['labels'])
    # alerts: long queue, bypass
    for j in jobs:
        k = 'q%s' % j['id']
        c = L.class_for(j['labels'], classes)
        if not c or c.get('host') == 'controller' and repo_mode(gh, st, j['repo'], ci_mode, now) != 'auto':
            continue            # legacy-lane jobs are not ours to alert on
        if now - j['created'] > 900 and k not in st['alerted']:
            st['alerted'][k] = now; alert('job %s / %s queued %d min (labels %s)' % (j['repo'], j['name'], (now - j['created']) // 60, j['labels']))
    train = set(x.strip() for x in E('TRAIN_REPOS', '').split(',') if x.strip())
    for p in pushes:
        if not train or (train != {'*'} and p['_repo'] not in train):
            continue        # no merge train for this repo: a direct push is the normal path, not a bypass
        why = L.bypass(p, set(st['release'].get('train_shas', [])), BOT_LOGINS, ALLOWED_PUSHERS)
        if why:
            day['bypass'] += 1; alert('BYPASS %s: %s (no prevention on the Free plan)' % (p['_repo'], why))
    st['alerted'] = {k: v for k, v in st['alerted'].items() if now - v < 3 * 86400}
    log('mode=%s jobs=%d vms=%d want=%d room=%d spend=$%.3f %s fallback=%s%s' % (
        ci_mode, len(jobs), len(live), len(want), room, day['usd'], spend, st['fallback'], ' gcp=' + failing if failing else ''))
    # Pulse queue (failures, fixes, released): drained a few per pass, retried, never blocks CI
    pq = nb_pulse.PulseQueue(st.setdefault('pulse', {}), nb_pulse.MCP(PULSE_URL, st.get('_pulse_token', '')), log,
                             DRY or not (PULSE_URL and st.get('_pulse_token')))
    pq.ingest_spool()

    def released(tid, note):
        pq._put('status', task=tid, status=E('PULSE_RELEASED_STATUS', 'released')); pq._put('comment', task=tid, text=note)
    rel_state = st['release']
    # flaky-infra retry for any failed job on an nb-ci runner (push runs too), once per run
    for f in failed:
        try:
            if repo_mode(gh, st, f['repo'], ci_mode, now) != 'auto':
                continue
            text = job_log(gh, ORG, f['repo'], f['job'])
            kind = L.classify_log(text, f['conclusion'], runner_gone=f['conclusion'] == 'cancelled' and f['runner'] not in rby)
            if str(f['run']) not in rel_state.setdefault('retried', {}) and L.should_retry(kind, f['attempt']):
                log('infra retry', f['repo'], f['name'], f['run'])
                rel_state['retried'][str(f['run'])] = now
                if not DRY:
                    gh('repos/%s/%s/actions/runs/%s/rerun-failed-jobs' % (ORG, f['repo'], f['run']), 'POST', {})
                continue
            # a real (or repeated) failure -> Pulse: comment + 'fixing' + one bug per task+check, on every task of the PR/commit
            if f['name'] in PULSE_SKIP_CHECKS or f['conclusion'] != 'failure':
                continue
            ids, pr = pulse_refs(gh, st, f['repo'], f.get('sha', ''), f.get('branch', ''))
            log('pulse failure', f['repo'], f['name'], 'tasks=%s' % ','.join(ids) or '-')
            for tid in ids:
                pq.report_failure(tid, f['name'], f['repo'], pr, f.get('url', ''), L.log_excerpt(text), 'Medium')
        except Exception as e:
            log('retry/pulse check', f['repo'], str(e)[:120])
    for p in passed:          # the same check green again -> bug closed + task 'reverify'
        for key, bug in list(pq.st['bugs'].items()):
            tid, chk = key.split('|', 1)
            if chk == p['name'] and bug.get('open') and bug.get('repo', p['repo']) == p['repo']:
                pq.report_fixed(tid, chk, p['repo'], 0, p.get('url', ''))
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
        if L.image_build_due(age_d, IMAGE_MAX_AGE_D, st['last'].get('image_build'), now):
            st['last']['image_build'] = now      # one attempt per 20 h: a failing build alerts, it is not retried hourly
            threading.Thread(target=lambda: _safe(build_image, gcp, lambda t: notify(st, t, key='image')), daemon=True).start()
    # heartbeat + daily report
    try:
        os.makedirs(os.path.dirname(HEARTBEAT_FILE), exist_ok=True); open(HEARTBEAT_FILE, 'w').write(str(int(now)))
    except OSError: pass
    if every(st, 'hb_var', 300, now):
        try: org_var(gh, 'NB_AUTOSCALER_HEARTBEAT', str(int(now)))
        except RuntimeError: pass
    if every(st, 'hb_teams', 6 * 3600, now):
        log('heartbeat: mode=%s, %d VMs, %d jobs queued, today $%.2f, %.0f VM-min' % (ci_mode, len(live), len(jobs), day['usd'], day['vm_min']))
    if time.gmtime(now).tm_hour == REPORT_HOUR and every(st, 'report', 20 * 3600, now) and not gh.low():
        report(st, alert, today)
    if (time.gmtime(now).tm_hour == REPORT_HOUR and every(st, 'redeliver', 20 * 3600, now)) or EVENTS.pop('redeliver_now', False):
        _safe(redeliver, gh, st)
    if every(st, 'dns', 60 if st.get('hook_host') != 'fixed' else 3600, now):
        _safe(set_hook, gh, st)
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


def set_hook(gh, st=None):
    """Point the GitHub App webhook at this VM. Fixed host NBCI_HOST (DNS-only A record) when it resolves to our
    ephemeral IP; otherwise the sslip.io name of the IP, plus a commit to DNS_BRANCH whose push workflow (on this
    VM's nb-ctl runner, Cloudflare token stays in GitHub) upserts the A record. Re-checked every minute until fixed."""
    import socket
    st = st if st is not None else {}
    ip = meta('instance/network-interfaces/0/access-configs/0/external-ip')
    try: resolved = {a[4][0] for a in socket.getaddrinfo(NBCI_HOST, 443, socket.AF_INET)}
    except OSError: resolved = set()
    kind, update = L.dns_plan(resolved, ip)
    url = 'https://%s/gh' % (NBCI_HOST if kind == 'fixed' else ip.replace('.', '-') + '.sslip.io')
    if update and st.get('dns_asked') != ip:
        _safe(request_dns, gh, ip); st['dns_asked'] = ip
    if st.get('hook_url') == url:
        st['hook_host'] = kind; return
    if DRY:
        log('DRY webhook ->', url); return
    req('https://api.github.com/app/hook/config', 'PATCH', {'url': url, 'content_type': 'json', 'insecure_ssl': '0'},
        {'Authorization': 'Bearer ' + gh.jwt(), 'Accept': 'application/vnd.github+json'})
    st['hook_url'], st['hook_host'] = url, kind
    log('webhook ->', url)


def request_dns(gh, ip):
    """Commit the new IP to DNS_BRANCH (contents API, App token): its push workflow upserts NBCI_HOST -> ip."""
    path = 'repos/%s/%s/contents/nbci-ip.txt' % (ORG, DNS_REPO)
    try: cur = gh(path + '?ref=' + DNS_BRANCH)
    except RuntimeError: cur = {}
    if base64.b64decode(cur.get('content', '') or b'').decode().strip() == ip:
        log('dns already requested', ip); return
    body = {'message': 'nbci ip %s\n\nPulse: none' % ip, 'branch': DNS_BRANCH, 'content': base64.b64encode((ip + '\n').encode()).decode()}
    if cur.get('sha'): body['sha'] = cur['sha']
    if DRY:
        log('DRY dns ->', ip); return
    gh(path, 'PUT', body); log('dns update requested', NBCI_HOST, '->', ip)


def redeliver(gh, st):
    """Redeliver the GitHub App's failed webhook deliveries of the last 24 h (once per delivery GUID)."""
    h = {'Authorization': 'Bearer ' + gh.jwt(), 'Accept': 'application/vnd.github+json'}
    seen = set(st.setdefault('redelivered', [])); out = []
    url = 'https://api.github.com/app/hook/deliveries?per_page=100'
    for _ in range(5):
        code, page, hdr = req(url, headers=h)
        out += page or []
        nxt = [x.split(';')[0].strip(' <>') for x in (hdr.get('Link') or '').split(',') if 'rel="next"' in x]
        if not nxt or (page and time.time() - ts(page[-1]['delivered_at']) > 86400):
            break
        url = nxt[0]
    ids = L.redeliver_ids(out, time.time(), seen)
    for i in ids[:100]:
        if DRY: log('DRY redeliver', i); continue
        try: req('https://api.github.com/app/hook/deliveries/%s/attempts' % i, 'POST', {}, h)
        except RuntimeError as e: log('redeliver', i, str(e)[:100])
    st['redelivered'] = list(seen)[-2000:]
    log('redeliver: %d failed deliveries re-sent (of %d seen)' % (len(ids[:100]), len(out)))


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
        return _build_image(gcp)
    if mode == 'set-hook':
        return set_hook(gh, st)
    if mode == 'redeliver':
        return redeliver(gh, st)
    if mode == 'aqa':            # nbci.py aqa <repo>:<run_id> ... (manual AQA backfill; the loop creates the VMs)
        queue_backfill(gh, sys.argv[2:]); return
    if mode in ('status', 'drain', 'once'):
        tick(gh, gcp, st, mode if mode != 'once' else 'reconcile'); st.pop('_pulse_token', None); save_state(st); return
    try:
        Hook.secret = gcp.secret('nbci-webhook-secret').encode()
        srv = http.server.ThreadingHTTPServer(('127.0.0.1', int(E('HOOK_PORT', '8787'))), Hook)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        st.pop('hook_host', None); st.pop('hook_url', None); _safe(set_hook, gh, st)
    except Exception as e:
        log('webhook receiver off (polling only):', e)
    EVENTS['redeliver_now'] = True       # deliveries that failed while we were down (the full poll covers jobs; this covers runs/PRs)
    notify_systemd('READY=1'); notify(st, 'controller started (DRY_RUN=%d)' % DRY, key='started', window_s=3600)
    while True:
        t = time.time()
        try: tick(gh, gcp, st)
        except Exception as e: log('tick error', str(e)[:300])
        tok = st.pop('_pulse_token', None); save_state(st); st['_pulse_token'] = tok
        notify_systemd('WATCHDOG=1')
        time.sleep(max(1, POLL_S - (time.time() - t)))


if __name__ == '__main__':
    main()
