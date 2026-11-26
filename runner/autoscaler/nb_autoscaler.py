#!/usr/bin/env python3
"""nb-autoscaler: the ONE controller for Neonbee-ai self-hosted CI capacity (runs on the Internal Tools VM).

Every POLL_S seconds it:
  1. reads the org's queued jobs (ETag-cached, so unchanged repos cost no rate limit) and runners;
  2. starts GCP Spot runner VMs (stock ubuntu-2404 + startup.sh, ephemeral IP, no service account,
     no custom image) sized to the backlog the Mac Minis cannot take, within MAX_VCPU (quota 32);
  3. hands each VM slot a one-job JIT runner config through instance metadata (ephemeral runners);
  4. deletes VMs (boot disk auto-deletes) once no slot has been busy for IDLE_S (180 s), plus
     TERMINATED / never-registered VMs and runner records with no VM;
  5. alerts Teams when a Mac Mini runner is offline or a job waits > QUEUE_ALERT_S;
  6. (opt-in, PROMOTE_REPOS) enables auto-merge on a repo's develop PR once the same branch's main
     merge has a fully green push run (main deploy green -> develop follows, per repo).

Labels come only from ci/runner-labels.json. Routing stays as is: NB_BURST stays off, the router sends
first runs to runner-vm, and both Minis and burst VMs carry runner-vm, so the first free runner wins.

Modes: loop (default) | once | status | drain (delete every VM it owns).  Stdlib only.
Env: ORG, GCP_PROJECT, GCP_ZONES, GH_TOKEN (or GH_TOKEN_FILE), GCLOUD, LABELS_FILE, MAX_VCPU, POLL_S,
     IDLE_S, BOOT_GRACE_S, MIN_AGE_S, QUEUE_ALERT_S, TEAMS_WEBHOOK, PROMOTE_REPOS, DRY_RUN, BUNDLE_REPO.
"""
import calendar, json, os, subprocess, sys, time, urllib.error, urllib.request

E = os.environ.get
HERE = os.path.dirname(os.path.abspath(__file__))
ORG = E('ORG', 'Neonbee-ai')
PROJECT = E('GCP_PROJECT', 'gen-lang-client-0521938031')
ZONES = E('GCP_ZONES', 'europe-west1-b,europe-west1-c,europe-west1-d').split(',')
GCLOUD = E('GCLOUD', 'gcloud')
LABELS_FILE = E('LABELS_FILE', os.path.join(HERE, '..', '..', 'ci', 'runner-labels.json'))
MAX_VCPU = int(E('MAX_VCPU', '32'))          # GCP CPUS_ALL_REGIONS quota; nothing else may use it
VCPU_PER_SLOT = int(E('VCPU_PER_SLOT', '2'))
POLL_S = int(E('POLL_S', '30'))
IDLE_S = int(E('IDLE_S', '180'))             # delete a VM after 3 idle minutes
BOOT_GRACE_S = int(E('BOOT_GRACE_S', '420'))  # a slot handed a JIT config counts as capacity this long
DEAD_BOOT_S = int(E('DEAD_BOOT_S', '720'))    # VM with no runner ever online after this -> delete
MIN_AGE_S = int(E('MIN_AGE_S', '20'))         # let an idle Mini take a job before buying a VM
QUEUE_ALERT_S = int(E('QUEUE_ALERT_S', '900'))
RECENT_H = float(E('RECENT_H', '8'))
STEM = E('VM_STEM', 'nbci')
DRY = E('DRY_RUN', '0') == '1'
BUNDLE_REPO = E('BUNDLE_REPO', 'Neonbee-ai/neonbee-devops')
MINI_PREFIX = E('MINI_PREFIX', 'macmini')
# Shapes by slots (2 vCPU per job slot). Largest first; the planner picks the fewest VMs.
SHAPES = [(8, 't2d-standard-16'), (4, 't2d-standard-8'), (2, 't2d-standard-4'), (1, 't2d-standard-2')]
DEAD = ('TERMINATED', 'STOPPED', 'SUSPENDED')


# ── pure logic (unit-tested in tests/test_autoscaler.py) ───────────────────────────────────────────
def load_labels(path=LABELS_FILE):
    d = json.load(open(path))
    out = {}
    for name, p in d['pools'].items():
        out[name] = p['base'] + (d['ci_labels'] if p.get('include_ci_labels') else []) + p.get('extra', [])
    return out


def lower(xs):
    return {x.lower() for x in xs}


def servable(job_labels, pool_labels):
    """GitHub gives a job to a runner whose labels are a superset of the job's runs-on labels."""
    return lower(job_labels) <= lower(pool_labels)


def plan_vms(need_slots, free_vcpu):
    """Fewest VMs covering need_slots within free_vcpu. Returns [(slots, machine)]."""
    plan, left, room = [], need_slots, free_vcpu
    for slots, machine in SHAPES:
        while left >= slots and room >= slots * VCPU_PER_SLOT:
            plan.append((slots, machine)); left -= slots; room -= slots * VCPU_PER_SLOT
    for slots, machine in reversed(SHAPES):          # remainder: smallest shape that covers it
        if left <= 0:
            break
        if slots >= left and room >= slots * VCPU_PER_SLOT:
            plan.append((slots, machine)); room -= slots * VCPU_PER_SLOT; left = 0
    return plan


def slot_state(vm, runners_by_name, now):
    """Per slot: 'busy' | 'idle' (runner online, no job) | 'pending' (JIT handed out, not yet online,
    within BOOT_GRACE_S) | 'empty' (needs a JIT config). vm = {'name', 'slots', 'md': {key: value}}."""
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
    """Delete when no slot is busy or pending and nothing ran for IDLE_S; or when it never came up."""
    if vm['status'] in DEAD:
        return 'dead (%s)' % vm['status']
    vals = set(states.values())
    if 'busy' in vals or 'pending' in vals:
        return None
    if now - vm['created'] > DEAD_BOOT_S and vm.get('ever_online') is False:
        return 'no runner came online in %ds' % DEAD_BOOT_S
    if now - max(last_busy, vm['created']) >= IDLE_S:
        return 'idle %ds' % (now - max(last_busy, vm['created']))
    return None


def demand(jobs, runners, pool_labels, now):
    """Jobs the burst pool can serve and no idle runner (Mini or VM) can take right now."""
    q = [j for j in jobs if servable(j['labels'], pool_labels) and now - j['created'] >= MIN_AGE_S]
    idle = [r for r in runners if r['status'] == 'online' and not r['busy']]
    taken, waiting = set(), []
    for j in q:
        m = next((r['id'] for r in idle if r['id'] not in taken and servable(j['labels'], [l['name'] for l in r['labels']])), None)
        if m is None:
            waiting.append(j)
        else:
            taken.add(m)
    return waiting


def promote_ready(main_runs):
    """main push runs of one merge commit: all completed and none failed/cancelled/timed out."""
    if not main_runs:
        return False
    bad = {'failure', 'cancelled', 'timed_out', 'action_required', 'startup_failure', 'stale'}
    return all(r['status'] == 'completed' and r['conclusion'] not in bad for r in main_runs)


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
        raise RuntimeError('%s %s -> %s %s' % (method, url.split('?')[0], e.code, e.read()[:300]))


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


def ghql(query, variables):
    _, d, _ = _req('https://api.github.com/graphql', 'POST', {'query': query, 'variables': variables},
                   {'Authorization': 'Bearer ' + gh_token()})
    if d.get('errors'):
        raise RuntimeError(d['errors'])
    return d['data']


_gtok = [0, '']


def gce(path, method='GET', body=None):
    if time.time() - _gtok[0] > 1800:
        _gtok[1] = subprocess.check_output([GCLOUD, 'auth', 'print-access-token'], text=True).strip()
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
            runs = gh('repos/%s/actions/runs?status=%s&per_page=30' % (r['full_name'], st), cache=True)
            for run in runs['workflow_runs']:
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
    region = ZONES[0].rsplit('-', 1)[0]
    r = {x['metric']: x for x in gce('/regions/' + region)['quotas']}.get('CPUS', g)
    used_by_us = sum(v['vcpu'] for v in vms if v['status'] not in DEAD)
    return max(0, min(int(g['limit'] - g['usage']), int(r['limit'] - r['usage']), MAX_VCPU - used_by_us))


def bundle_url():
    """Short-lived signed download URL of the newest burst-tools bundle (no token goes to the VM)."""
    try:
        rel = [x for x in gh('repos/%s/releases?per_page=20' % BUNDLE_REPO) if x['tag_name'].startswith('burst-tools-')]
        a = next(a for a in rel[0]['assets'] if a['name'] == 'burst-tools.tar.gz')
        req = urllib.request.Request(a['url'], headers={'Authorization': 'Bearer ' + gh_token(),
                                                        'Accept': 'application/octet-stream'})

        class NoRedir(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        try:
            urllib.request.build_opener(NoRedir).open(req, timeout=20)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 307):
                return e.headers['Location']
    except Exception as e:
        log('bundle url unavailable (VM installs upstream tools):', e)
    return ''


def jit(name, labels):
    d = gh('orgs/%s/actions/runners/generate-jitconfig' % ORG, 'POST',
           {'name': name, 'runner_group_id': 1, 'labels': labels, 'work_folder': '_work'})
    return d['encoded_jit_config']


def startup_script():
    return open(os.path.join(HERE, 'startup.sh')).read()


def create_vm(slots, machine, labels):
    name = '%s-%s-%d' % (STEM, time.strftime('%m%d-%H%M%S', time.gmtime()), slots)
    md = [{'key': 'startup-script', 'value': startup_script()}, {'key': 'slots', 'value': str(slots)},
          {'key': 'bundle-url', 'value': bundle_url()}]
    now = int(time.time())
    for k in range(slots):                             # every slot starts 'pending' with its own JIT config
        rn = '%s-s%d-%d' % (name, k, now % 100000)
        md += [{'key': 'jit-%d' % k, 'value': '' if DRY else jit(rn, labels)},
               {'key': 'name-%d' % k, 'value': rn}, {'key': 'at-%d' % k, 'value': str(now)}]
    for z in ZONES:                                    # Spot stock-outs: try the next zone
        body = {'name': name, 'machineType': 'zones/%s/machineTypes/%s' % (z, machine), 'labels': {STEM: '1'},
                'scheduling': {'provisioningModel': 'SPOT', 'instanceTerminationAction': 'DELETE',
                               'maxRunDuration': {'seconds': 4 * 3600}, 'automaticRestart': False,
                               'onHostMaintenance': 'TERMINATE'},
                'disks': [{'boot': True, 'autoDelete': True, 'initializeParams': {
                    'sourceImage': 'projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64',
                    'diskSizeGb': str(40 + 10 * slots), 'diskType': 'zones/%s/diskTypes/pd-balanced' % z}}],
                'networkInterfaces': [{'network': 'global/networks/default',
                                       'accessConfigs': [{'type': 'ONE_TO_ONE_NAT', 'name': 'External NAT'}]}],
                'serviceAccounts': [],                 # job code can never reach GCP
                'metadata': {'items': md}}
        if DRY:
            log('DRY create', name, machine, z); return
        try:
            gce('/zones/%s/instances' % z, 'POST', body); log('created', name, machine, z, 'slots=%d' % slots); return
        except RuntimeError as e:
            log('create failed in', z, str(e)[:160])


def set_md(vm, updates):
    items = dict(vm['md']); items.update(updates)
    if DRY:
        log('DRY metadata', vm['name'], sorted(updates)); return
    gce('/zones/%s/instances/%s/setMetadata' % (vm['zone'], vm['name']), 'POST',
        {'fingerprint': vm['fp'], 'items': [{'key': k, 'value': v} for k, v in items.items()]})


def delete_vm(vm, rs, why):
    """Deregister the VM's runners first: GitHub refuses (422) to delete a runner that just took a job,
    and then the VM is kept, so a job assigned between the tick's snapshot and now is never killed."""
    log('delete', vm['name'], why)
    if DRY:
        return True
    if vm['status'] not in DEAD:
        for r in rs:
            if r['name'].startswith(vm['name'] + '-s'):
                try: gh('orgs/%s/actions/runners/%s' % (ORG, r['id']), 'DELETE')
                except RuntimeError as e:
                    if ' 404 ' not in str(e):
                        log('keep', vm['name'], '- runner', r['name'], 'is busy'); return False
    gce('/zones/%s/instances/%s' % (vm['zone'], vm['name']), 'DELETE')
    return True


def alert(text):
    hook = E('TEAMS_WEBHOOK')
    log('ALERT', text)
    if hook and not DRY:
        try: _req(hook, 'POST', {'text': text})
        except Exception as e: log('alert failed', e)


# ── one pass ──────────────────────────────────────────────────────────────────────────────────────
STATE = {'last_busy': {}, 'ever_online': {}, 'alerted': {}, 'promoted': set(), 'last_promote': 0}


def tick(mode='scale'):
    now = time.time()
    pools = load_labels(); burst = pools['burst']
    rs = runners(); by_name = {r['name']: r for r in rs}
    jobs = queued_jobs(); vms = our_vms()
    waiting = demand(jobs, rs, burst, now)
    if mode == 'status':
        for j in waiting: print('waiting %-24s %-40s %4ds' % (j['repo'], j['name'][:40], now - j['created']))
        for v in vms: print('vm      %-28s %-10s %s' % (v['name'], v['status'], slot_state(v, by_name, now)))
        print('queued=%d waiting=%d vms=%d free_vcpu=%d' % (len(jobs), len(waiting), len(vms), free_vcpu(vms)))
        return
    if mode == 'drain':
        for v in vms: delete_vm(v, rs, 'drain')
        return
    need = len(waiting)
    # 1. refill empty slots of live VMs first (cheapest capacity), count pending as capacity
    live = []
    for v in vms:
        st = slot_state(v, by_name, now)
        if any(s in ('busy', 'idle') for s in st.values()):
            STATE['ever_online'][v['name']] = True
        if 'busy' in st.values():
            STATE['last_busy'][v['name']] = now
        v['ever_online'] = STATE['ever_online'].get(v['name'], False)
        why = vm_should_delete(v, st, STATE['last_busy'].get(v['name'], now if v['ever_online'] else 0), now)
        if why and not (need > 0 and v['status'] not in DEAD and 'empty' in st.values() and v['ever_online']):
            if delete_vm(v, rs, why):
                continue
        live.append(v)
        need -= sum(1 for s in st.values() if s == 'pending')   # idle runners were already netted in demand()
        upd = {}
        for k, s in st.items():
            if s == 'empty' and need > 0 and v['status'] == 'RUNNING':
                rn = '%s-s%d-%d' % (v['name'], k, int(now) % 100000)
                upd.update({'jit-%d' % k: '' if DRY else jit(rn, burst), 'name-%d' % k: rn, 'at-%d' % k: str(int(now))})
                need -= 1
        if upd:
            set_md(v, upd)
    # 2. buy new VMs for the rest
    if need > 0:
        for slots, machine in plan_vms(need, free_vcpu(vms)):
            create_vm(slots, machine, burst)
    # 3. stale runner records of deleted VMs
    names = {v['name'] for v in live}
    for r in rs:
        if r['name'].startswith(STEM + '-') and r['status'] == 'offline' and not r['busy'] \
                and r['name'].rsplit('-s', 1)[0] not in names and not DRY:
            try: gh('orgs/%s/actions/runners/%s' % (ORG, r['id']), 'DELETE')
            except RuntimeError: pass
    # 4. health alerts (once per incident)
    for r in rs:
        key = 'off:' + r['name']
        if r['name'].startswith(MINI_PREFIX) and r['status'] == 'offline':
            STATE['alerted'].setdefault(key, now)
            if now - STATE['alerted'][key] >= 300 and STATE['alerted'].get(key + ':sent') is None:
                alert('CI runner %s offline for 5+ min (disk full or Mini down?)' % r['name']); STATE['alerted'][key + ':sent'] = now
        elif key in STATE['alerted']:
            STATE['alerted'].pop(key, None); STATE['alerted'].pop(key + ':sent', None)
    for j in jobs:
        key = 'q:%s' % j['id']
        if now - j['created'] > QUEUE_ALERT_S and key not in STATE['alerted']:
            STATE['alerted'][key] = now
            alert('CI job %s / %s queued %d min, labels %s' % (j['repo'], j['name'], (now - j['created']) // 60, j['labels']))
    log('queued=%d waiting=%d vms=%d need_after=%d' % (len(jobs), len(waiting), len(live), max(need, 0)))
    # 5. per-repo develop promotion (opt-in)
    if E('PROMOTE_REPOS') and now - STATE['last_promote'] >= 120:
        STATE['last_promote'] = now
        try: promote()
        except Exception as e: log('promote error', e)


AUTO_MERGE = 'mutation($id:ID!){enablePullRequestAutoMerge(input:{pullRequestId:$id,mergeMethod:MERGE}){clientMutationId}}'


def develop_protected(repo):
    try:
        d = gh('repos/%s/%s/branches/develop/protection/required_status_checks' % (ORG, repo))
        return bool(d.get('contexts') or d.get('checks'))
    except RuntimeError:
        return False


def promote():
    """For each opted-in repo: a release branch merged to main whose main push runs are all green ->
    enable auto-merge on the open develop PR from the same branch. GitHub merges it once develop's
    required checks pass, so develop never merges ahead of a green main deploy."""
    allow = E('PROMOTE_REPOS', '')
    repos = [r['name'] for r in gh('orgs/%s/repos?sort=pushed&direction=desc&per_page=100' % ORG, cache=True)] \
        if allow == '*' else [x.strip() for x in allow.split(',') if x.strip()]
    since = time.time() - RECENT_H * 3600
    for repo in repos:
        for pr in gh('repos/%s/%s/pulls?state=closed&base=main&sort=updated&direction=desc&per_page=10' % (ORG, repo)):
            if not pr.get('merged_at') or ts(pr['merged_at']) < since:
                continue
            key = '%s#%s' % (repo, pr['number'])
            if key in STATE['promoted']:
                continue
            runs = gh('repos/%s/%s/actions/runs?head_sha=%s&branch=main&event=push&per_page=20'
                      % (ORG, repo, pr['merge_commit_sha']))['workflow_runs']
            if not promote_ready(runs):
                continue
            head = pr['head']['ref']
            dev = gh('repos/%s/%s/pulls?state=open&base=develop&head=%s:%s' % (ORG, repo, ORG, head))
            if not dev:
                STATE['promoted'].add(key); continue
            if not develop_protected(repo):
                log('promote skip %s: develop has no required checks (auto-merge would merge untested)' % repo)
                STATE['promoted'].add(key); continue
            if DRY:
                log('DRY auto-merge', repo, dev[0]['number'])
            else:
                ghql(AUTO_MERGE, {'id': dev[0]['node_id']})
                log('auto-merge enabled', repo, '#%s' % dev[0]['number'], 'after main', key)
            STATE['promoted'].add(key)


def main():
    mode = (sys.argv[1:] or ['loop'])[0]
    if mode in ('status', 'drain', 'once'):
        return tick({'once': 'scale'}.get(mode, mode))
    while True:
        t = time.time()
        try:
            tick()
        except Exception as e:      # never die: a dead controller leaves VMs running
            log('tick error', str(e)[:300])
        time.sleep(max(1, POLL_S - (time.time() - t)))


if __name__ == '__main__':
    main()
