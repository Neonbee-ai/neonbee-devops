#!/usr/bin/env python3
"""nb-autoscaler: the ONE controller for Neonbee-ai CI (systemd service on the Internal Tools VM).

Capacity. CI runs ONLY on GCP Spot runner VMs. The Mac Minis carry no CI labels (they do the pre-push
gate over ssh; macmini-preview-2 keeps aqa-coord for AutoQA). Every POLL_S seconds:
  1. read the org's queued jobs (ETag-cached) and runners;
  2. demand = queued jobs the burst label set can serve (ci/runner-labels.json) minus idle VM slots;
  3. refill empty slots of live VMs with one-job JIT runner configs, then create new VMs (stock
     ubuntu-2404 + startup.sh, no service account, ephemeral IP, auto-delete disk) within the free vCPU
     quota (MAX_VCPU 32), trying every zone in GCP_ZONES on Spot stock-outs;
  4. delete a VM only after IDLE_S (180 s) with no busy or pending slot, and only after GitHub has
     accepted the removal of all its runners (a runner that just took a job refuses -> VM kept).
Fallback. When GCP cannot give capacity (API/auth error, billing/payment problem, quota exhausted, or
stock-out in 2+ zones) while jobs wait, ONE Mac Mini runner (fallback.runner) gets the CI labels and
Teams is alerted; the labels come off again after GCP next starts a VM. Reconciled every pass.
Release (nb_release.py): merge train, develop promotion, Pulse 'released'.
Never-breaking: every pass is a fresh idempotent reconcile from GitHub + GCE state; exceptions are
logged and the loop continues; systemd Restart=always + WatchdogSec; a heartbeat file, org variable
NB_AUTOSCALER_HEARTBEAT and a Teams heartbeat; DRY_RUN=1 logs every action instead of doing it.

Modes: loop (default) | once | status | drain (delete all its idle VMs). Stdlib only.
"""
import calendar, json, os, socket, subprocess, sys, time, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import nb_release  # noqa: E402

E = os.environ.get
ORG = E('ORG', 'Neonbee-ai')
PROJECT = E('GCP_PROJECT', 'gen-lang-client-0521938031')
ZONES = E('GCP_ZONES', 'europe-west1-b,europe-west1-c,europe-west1-d').split(',')
GCLOUD = E('GCLOUD', 'gcloud')
LABELS_FILE = E('LABELS_FILE', os.path.join(HERE, '..', '..', 'ci', 'runner-labels.json'))
STATE_FILE = E('STATE_FILE', '/var/lib/nb-autoscaler/state.json')
HEARTBEAT_FILE = E('HEARTBEAT_FILE', '/var/lib/nb-autoscaler/heartbeat')
MAX_VCPU = int(E('MAX_VCPU', '32'))
VCPU_PER_SLOT = int(E('VCPU_PER_SLOT', '2'))
POLL_S = int(E('POLL_S', '30'))
IDLE_S = int(E('IDLE_S', '180'))
BOOT_GRACE_S = int(E('BOOT_GRACE_S', '420'))
MIN_AGE_S = int(E('MIN_AGE_S', '5'))
QUEUE_ALERT_S = int(E('QUEUE_ALERT_S', '900'))
FALLBACK_AFTER_S = int(E('FALLBACK_AFTER_S', '120'))   # jobs waiting this long with GCP failing -> fallback
HEARTBEAT_H = float(E('HEARTBEAT_H', '6'))
RELEASE_S = int(E('RELEASE_S', '60'))
RECENT_H = float(E('RECENT_H', '8'))
STEM = E('VM_STEM', 'nbci')
DRY = E('DRY_RUN', '1') == '1'                       # safe default: an unconfigured start does nothing
BUNDLE_REPO = E('BUNDLE_REPO', 'Neonbee-ai/neonbee-devops')
SHAPES = [(8, 't2d-standard-16'), (4, 't2d-standard-8'), (2, 't2d-standard-4'), (1, 't2d-standard-2')]
DEAD = ('TERMINATED', 'STOPPED', 'SUSPENDED')


# ── pure logic (tests/test_autoscaler.py) ──────────────────────────────────────────────────────────
def load_config(path=LABELS_FILE):
    d = json.load(open(path))
    pools = {n: p['base'] + (d['ci_labels'] if p.get('include_ci_labels') else []) + p.get('extra', [])
             for n, p in d['pools'].items()}
    return pools, d.get('fallback', {}), d['ci_labels']


def lower(xs):
    return {x.lower() for x in xs}


def servable(job_labels, pool_labels):
    return lower(job_labels) <= lower(pool_labels)


def plan_vms(need_slots, free_vcpu):
    plan, left, room = [], need_slots, free_vcpu
    for slots, machine in SHAPES:
        while left >= slots and room >= slots * VCPU_PER_SLOT:
            plan.append((slots, machine)); left -= slots; room -= slots * VCPU_PER_SLOT
    for slots, machine in reversed(SHAPES):
        if left <= 0:
            break
        if slots >= left and room >= slots * VCPU_PER_SLOT:
            plan.append((slots, machine)); room -= slots * VCPU_PER_SLOT; left = 0
    return plan


def slot_state(vm, runners_by_name, now):
    out = {}
    for k in range(vm['slots']):
        name, at = vm['md'].get('name-%d' % k), int(vm['md'].get('at-%d' % k, '0') or 0)
        r = runners_by_name.get(name) if name else None
        if r and r.get('busy'):
            out[k] = 'busy'
        elif r and r.get('status') == 'online':
            out[k] = 'idle'
        elif name and now - at < BOOT_GRACE_S:
            out[k] = 'pending'
        else:
            out[k] = 'empty'
    return out


def vm_should_delete(vm, states, last_busy, now):
    if vm['status'] in DEAD:
        return 'dead (%s)' % vm['status']
    vals = set(states.values())
    if 'busy' in vals or 'pending' in vals:
        return None
    idle_for = now - max(last_busy, vm['created'])
    return 'idle %ds' % idle_for if idle_for >= IDLE_S else None


def demand(jobs, runners, pool_labels, now, stem=STEM):
    """Queued jobs the burst pool can serve and no idle burst-VM slot can take. Idle Mac Minis are
    never counted as capacity: CI must not depend on them (the fallback runner is the only exception
    and it only exists while GCP is failing)."""
    q = [j for j in jobs if servable(j['labels'], pool_labels) and now - j['created'] >= MIN_AGE_S]
    idle = [r for r in runners if r['name'].startswith(stem + '-') and r['status'] == 'online' and not r['busy']]
    taken, waiting = set(), []
    for j in q:
        m = next((r['id'] for r in idle if r['id'] not in taken
                  and servable(j['labels'], [l['name'] for l in r['labels']])), None)
        if m is None:
            waiting.append(j)
        else:
            taken.add(m)
    return waiting


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
    """attempts = [(zone, kind)] of failed creates this pass. Billing/quota/api errors count at once;
    stock-outs count only when 2+ different zones are out."""
    kinds = {k for _, k in attempts}
    if kinds & {'billing', 'quota', 'api'}:
        return sorted(kinds & {'billing', 'quota', 'api'})[0]
    if len({z for z, k in attempts if k == 'stockout'}) >= 2:
        return 'stockout'
    return None


def fallback_decision(on, waiting_s, failing, vm_started):
    """Next fallback state. On: GCP failing while a job has waited FALLBACK_AFTER_S. Off: GCP started a VM."""
    if vm_started:
        return False
    if failing and waiting_s >= FALLBACK_AFTER_S:
        return True
    return on


def promote_ready(runs):
    return nb_release.main_green(runs)


# ── I/O ─────────────────────────────────────────────────────────────────────────────────────────────
def ts(s):
    return calendar.timegm(time.strptime(s[:19], '%Y-%m-%dT%H:%M:%S'))


def log(*a):
    print(time.strftime('%H:%M:%S'), *a, flush=True)


def _req(url, method='GET', body=None, headers=None):
    h = dict(headers or {}, **{'Content-Type': 'application/json'})
    r = urllib.request.Request(url, method=method, headers=h,
                               data=None if body is None else json.dumps(body).encode())
    try:
        with urllib.request.urlopen(r, timeout=30) as f:
            b = f.read()
            return f.status, (json.loads(b) if b else {}), f.headers
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return 304, None, e.headers
        raise RuntimeError('%s %s -> %s %s' % (method, url.split('?')[0], e.code, e.read()[:400]))


def gh_token():
    f = E('GH_TOKEN_FILE')
    return open(f).read().strip() if f else os.environ['GH_TOKEN']


_etag = {}


def gh(path, method='GET', body=None, cache=False):
    h = {'Authorization': 'Bearer ' + gh_token(), 'Accept': 'application/vnd.github+json',
         'X-GitHub-Api-Version': '2022-11-28'}
    if cache and path in _etag:
        h['If-None-Match'] = _etag[path][0]
    code, data, hd = _req('https://api.github.com/' + path, method, body, h)
    if cache:
        if code == 304:
            return _etag[path][1]
        if hd.get('ETag'):
            _etag[path] = (hd['ETag'], data)
    return data


_gtok = [0, '']


def gce(path, method='GET', body=None):
    if time.time() - _gtok[0] > 1800:
        _gtok[1] = subprocess.check_output([GCLOUD, 'auth', 'print-access-token'], text=True, timeout=60).strip()
        _gtok[0] = time.time()
    _, d, _ = _req('https://compute.googleapis.com/compute/v1/projects/%s%s' % (PROJECT, path), method, body,
                   {'Authorization': 'Bearer ' + _gtok[1]})
    return d


def queued_jobs():
    out, since = [], time.time() - RECENT_H * 3600
    for r in gh('orgs/%s/repos?sort=pushed&direction=desc&per_page=100' % ORG, cache=True):
        if r['archived'] or not r['pushed_at'] or ts(r['pushed_at']) < since:
            continue
        for st in ('queued', 'in_progress'):
            for run in gh('repos/%s/actions/runs?status=%s&per_page=30' % (r['full_name'], st), cache=True)['workflow_runs']:
                for j in gh('repos/%s/actions/runs/%s/jobs?filter=latest&per_page=100' % (r['full_name'], run['id']))['jobs']:
                    if j['status'] == 'queued' and j['labels']:
                        out.append({'repo': r['name'], 'id': j['id'], 'name': j['name'], 'labels': j['labels'],
                                    'created': ts(j['created_at'])})
    return out


def runners():
    out, pg = [], 1
    while True:
        d = gh('orgs/%s/actions/runners?per_page=100&page=%d' % (ORG, pg))
        out += d['runners']
        if not d['runners'] or len(out) >= d['total_count']:
            return out
        pg += 1


def our_vms():
    vms = []
    for z in ZONES:
        for v in gce('/zones/%s/instances?filter=labels.%s%%3D1' % (z, STEM)).get('items', []):
            md = {i['key']: i.get('value', '') for i in v.get('metadata', {}).get('items', [])}
            vms.append({'name': v['name'], 'zone': z, 'status': v['status'], 'created': ts(v['creationTimestamp']),
                        'slots': int(md.get('slots', '1')), 'md': md, 'fp': v['metadata']['fingerprint'],
                        'vcpu': int(v['machineType'].rsplit('-', 1)[1])})
    return vms


def free_vcpu(vms):
    g = {x['metric']: x for x in gce('')['quotas']}['CPUS_ALL_REGIONS']
    r = {x['metric']: x for x in gce('/regions/' + ZONES[0].rsplit('-', 1)[0])['quotas']}.get('CPUS', g)
    used = sum(v['vcpu'] for v in vms if v['status'] not in DEAD)
    return max(0, min(int(g['limit'] - g['usage']), int(r['limit'] - r['usage']), MAX_VCPU - used))


def bundle_url():
    try:
        rel = [x for x in gh('repos/%s/releases?per_page=20' % BUNDLE_REPO) if x['tag_name'].startswith('burst-tools-')]
        a = next(a for a in rel[0]['assets'] if a['name'] == 'burst-tools.tar.gz')

        class NoRedir(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        try:
            urllib.request.build_opener(NoRedir).open(urllib.request.Request(
                a['url'], headers={'Authorization': 'Bearer ' + gh_token(), 'Accept': 'application/octet-stream'}), timeout=20)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 307):
                return e.headers['Location']
    except Exception as e:
        log('bundle url unavailable (VM installs upstream tools):', e)
    return ''


def jit(name, labels):
    return gh('orgs/%s/actions/runners/generate-jitconfig' % ORG, 'POST',
              {'name': name, 'runner_group_id': 1, 'labels': labels, 'work_folder': '_work'})['encoded_jit_config']


def create_vm(slots, machine, labels, failures):
    """Returns True when GCE accepted the VM. Appends (zone, kind) to failures for every refusal."""
    name = '%s-%s-%d' % (STEM, time.strftime('%m%d-%H%M%S', time.gmtime()), slots)
    now = int(time.time())
    md = [{'key': 'startup-script', 'value': open(os.path.join(HERE, 'startup.sh')).read()},
          {'key': 'slots', 'value': str(slots)}, {'key': 'bundle-url', 'value': bundle_url()}]
    jits = []
    for k in range(slots):
        rn = '%s-s%d-%d' % (name, k, now % 100000)
        md += [{'key': 'jit-%d' % k, 'value': '' if DRY else jit(rn, labels)},
               {'key': 'name-%d' % k, 'value': rn}, {'key': 'at-%d' % k, 'value': str(now)}]
        jits.append(rn)
    for z in ZONES:
        body = {'name': name, 'machineType': 'zones/%s/machineTypes/%s' % (z, machine), 'labels': {STEM: '1'},
                'scheduling': {'provisioningModel': 'SPOT', 'instanceTerminationAction': 'DELETE',
                               'maxRunDuration': {'seconds': 4 * 3600}, 'automaticRestart': False,
                               'onHostMaintenance': 'TERMINATE'},
                'disks': [{'boot': True, 'autoDelete': True, 'initializeParams': {
                    'sourceImage': 'projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64',
                    'diskSizeGb': str(40 + 10 * slots), 'diskType': 'zones/%s/diskTypes/pd-balanced' % z}}],
                'networkInterfaces': [{'network': 'global/networks/default',
                                       'accessConfigs': [{'type': 'ONE_TO_ONE_NAT', 'name': 'External NAT'}]}],
                'serviceAccounts': [], 'metadata': {'items': md}}
        if DRY:
            log('DRY create', name, machine, z); return True
        try:
            op = gce('/zones/%s/instances' % z, 'POST', body)
            err = (op or {}).get('error')
            if err:
                raise RuntimeError(json.dumps(err))
            log('created', name, machine, z, 'slots=%d' % slots); return True
        except Exception as e:
            kind = classify_gce_error(str(e)); failures.append((z, kind))
            log('create failed', z, kind, str(e)[:160])
            if kind in ('billing', 'quota'):
                break                                   # same answer in every zone
    for rn in jits:                                     # unused JIT registrations
        for r in runners():
            if r['name'] == rn:
                try: gh('orgs/%s/actions/runners/%s' % (ORG, r['id']), 'DELETE')
                except RuntimeError: pass
    return False


def set_md(vm, updates):
    items = dict(vm['md']); items.update(updates)
    if DRY:
        log('DRY metadata', vm['name'], sorted(updates)); return
    gce('/zones/%s/instances/%s/setMetadata' % (vm['zone'], vm['name']), 'POST',
        {'fingerprint': vm['fp'], 'items': [{'key': k, 'value': v} for k, v in items.items()]})


def delete_vm(vm, rs, why):
    """Never deletes a VM running a job: its runners are deregistered first and GitHub refuses (422)
    for a busy runner, which keeps the VM. A TERMINATED VM runs nothing and is deleted directly."""
    if any(r['busy'] for r in rs if r['name'].startswith(vm['name'] + '-s')):
        return False
    log('delete', vm['name'], why)
    if DRY:
        return True
    if vm['status'] not in DEAD:
        for r in rs:
            if r['name'].startswith(vm['name'] + '-s'):
                try: gh('orgs/%s/actions/runners/%s' % (ORG, r['id']), 'DELETE')
                except RuntimeError as e:
                    if ' 404 ' not in str(e):
                        log('keep', vm['name'], '- runner', r['name'], 'took a job'); return False
    gce('/zones/%s/instances/%s' % (vm['zone'], vm['name']), 'DELETE')
    return True


def alert(text):
    hook = E('TEAMS_WEBHOOK')
    log('ALERT', text)
    if hook and not DRY:
        try: _req(hook, 'POST', {'text': '[nb-autoscaler] ' + text})
        except Exception as e: log('alert failed', e)


def set_fallback(runner, on, ci_labels):
    """Idempotent: make the fallback Mini runner's custom labels = ci_labels (on) or none (off)."""
    have = {l['name'] for l in runner['labels'] if l.get('type') == 'custom'}
    want = set(ci_labels) if on else set()
    if have == want:
        return False
    log('fallback runner', runner['name'], 'ON' if on else 'OFF')
    if not DRY:
        if want:
            gh('orgs/%s/actions/runners/%s/labels' % (ORG, runner['id']), 'PUT', {'labels': sorted(want)})
        else:
            gh('orgs/%s/actions/runners/%s/labels' % (ORG, runner['id']), 'DELETE')
    return True


def notify_systemd(msg):
    addr = E('NOTIFY_SOCKET')
    if not addr:
        return
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        s.connect('\0' + addr[1:] if addr.startswith('@') else addr); s.sendall(msg.encode()); s.close()
    except OSError:
        pass


# ── state (survives restarts; everything else is re-derived each pass) ─────────────────────────────
def load_state():
    try:
        st = json.load(open(STATE_FILE))
    except Exception:
        st = {}
    for k, v in (('last_busy', {}), ('alerted', {}), ('fallback', False),
                 ('last_heartbeat', 0), ('last_release', 0), ('release', {})):
        st.setdefault(k, v)
    return st


def save_state(st):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + '.tmp'
        json.dump(st, open(tmp, 'w')); os.replace(tmp, STATE_FILE)
    except OSError as e:
        log('state save failed', e)


# ── one reconcile pass ──────────────────────────────────────────────────────────────────────────────
def tick(st, mode='scale'):
    now = time.time()
    pools, fb, ci_labels = load_config(); burst = pools['burst']
    rs = runners(); by_name = {r['name']: r for r in rs}
    jobs = queued_jobs()
    waiting = demand(jobs, rs, burst, now)
    failures, started, gce_ok = [], False, True
    try:
        vms = our_vms()
    except Exception as e:
        vms, gce_ok = [], False
        failures.append(('-', classify_gce_error(str(e)))); log('gce list failed', str(e)[:200])
    if mode == 'status':
        for j in waiting: print('waiting %-24s %-40s %4ds' % (j['repo'], j['name'][:40], now - j['created']))
        for v in vms: print('vm      %-28s %-10s %s' % (v['name'], v['status'], slot_state(v, by_name, now)))
        print('queued=%d waiting=%d vms=%d fallback=%s' % (len(jobs), len(waiting), len(vms), st['fallback']))
        return
    need = len(waiting)
    live = []
    for v in vms:
        s = slot_state(v, by_name, now)
        if 'busy' in s.values():
            st['last_busy'][v['name']] = now
        why = vm_should_delete(v, s, st['last_busy'].get(v['name'], now), now)
        reusable = need > 0 and v['status'] == 'RUNNING' and 'empty' in s.values()
        if (mode == 'drain' or (why and not reusable)) and delete_vm(v, rs, why or 'drain'):
            st['last_busy'].pop(v['name'], None); continue
        live.append(v)
        need -= sum(1 for x in s.values() if x == 'pending')
        upd = {}
        for k, x in s.items():
            if x == 'empty' and need > 0 and v['status'] == 'RUNNING':
                rn = '%s-s%d-%d' % (v['name'], k, int(now) % 100000)
                upd.update({'jit-%d' % k: '' if DRY else jit(rn, burst), 'name-%d' % k: rn, 'at-%d' % k: str(int(now))})
                need -= 1
        if upd:
            set_md(v, upd)
    if mode == 'drain':
        return
    if need > 0 and gce_ok:
        try:
            room = free_vcpu(vms)
        except Exception as e:
            room = 0; failures.append(('-', classify_gce_error(str(e))))
        plan = plan_vms(need, room)
        ours = sum(v['vcpu'] for v in vms if v['status'] not in DEAD)
        if not plan and room < VCPU_PER_SLOT and ours + VCPU_PER_SLOT <= MAX_VCPU:
            failures.append(('-', 'quota'))          # GCP quota used up by something else (at our own cap = fine)
        for slots, machine in plan:
            started = create_vm(slots, machine, burst, failures) or started
    # stale runner records of VMs that are gone
    names = {v['name'] for v in live}
    for r in rs:
        if r['name'].startswith(STEM + '-') and r['status'] == 'offline' and not r['busy'] \
                and r['name'].rsplit('-s', 1)[0] not in names and not DRY:
            try: gh('orgs/%s/actions/runners/%s' % (ORG, r['id']), 'DELETE')
            except RuntimeError: pass
    # fallback Mini runner
    failing = gcp_failing(failures)
    oldest = max([now - j['created'] for j in waiting] or [0])
    new = fallback_decision(st['fallback'], oldest if failing else 0, failing, started)
    if new != st['fallback']:
        alert('GCP cannot start runners (%s): fallback Mini runner %s ON for CI' % (failing, fb.get('runner')) if new
              else 'GCP started a VM again: fallback Mini runner %s OFF' % fb.get('runner'))
        st['fallback'] = new
    fr = by_name.get(fb.get('runner', ''))
    if fr:
        set_fallback(fr, st['fallback'], fb.get('labels') or ci_labels)
    elif st['fallback']:
        alert('fallback runner %s is not registered: no CI capacity at all' % fb.get('runner'))
    # alerts, once per incident
    for j in jobs:
        key = 'q:%s' % j['id']
        if now - j['created'] > QUEUE_ALERT_S and key not in st['alerted']:
            st['alerted'][key] = now
            alert('job %s / %s queued %d min, labels %s' % (j['repo'], j['name'], (now - j['created']) // 60, j['labels']))
    st['alerted'] = {k: v for k, v in st['alerted'].items() if now - v < 86400}
    log('queued=%d waiting=%d vms=%d need_after=%d fallback=%s%s' % (
        len(jobs), len(waiting), len(live), max(need, 0), st['fallback'], ' gcp=' + failing if failing else ''))
    # release flow
    if now - st['last_release'] >= RELEASE_S and E('TRAIN_REPOS'):
        st['last_release'] = now
        nb_release.Release(gh, ORG, log, alert, st['release'], DRY).tick(nb_release.Pulse(log, DRY))
    # heartbeat
    try:
        os.makedirs(os.path.dirname(HEARTBEAT_FILE), exist_ok=True); open(HEARTBEAT_FILE, 'w').write(str(int(now)))
    except OSError:
        pass
    if now - st.get('last_hb_var', 0) >= 300 and not DRY:   # sessions / ci-wait can see it is alive
        st['last_hb_var'] = now
        try: gh('orgs/%s/actions/variables/NB_AUTOSCALER_HEARTBEAT' % ORG, 'PATCH', {'value': str(int(now))})
        except RuntimeError:
            try: gh('orgs/%s/actions/variables' % ORG, 'POST', {'name': 'NB_AUTOSCALER_HEARTBEAT', 'value': str(int(now)), 'visibility': 'all'})
            except RuntimeError: pass
    if now - st['last_heartbeat'] >= HEARTBEAT_H * 3600:
        st['last_heartbeat'] = now
        alert('heartbeat: alive, %d VMs, %d jobs queued, fallback=%s' % (len(live), len(jobs), st['fallback']))


def main():
    mode = (sys.argv[1:] or ['loop'])[0]
    st = load_state()
    if mode in ('status', 'drain', 'once'):
        tick(st, {'once': 'scale'}.get(mode, mode)); save_state(st); return
    notify_systemd('READY=1')
    alert('started (DRY_RUN=%s)' % int(DRY))
    while True:
        t = time.time()
        try:
            tick(st)
        except Exception as e:
            log('tick error', str(e)[:300])
        save_state(st)
        notify_systemd('WATCHDOG=1')
        time.sleep(max(1, POLL_S - (time.time() - t)))


if __name__ == '__main__':
    main()
