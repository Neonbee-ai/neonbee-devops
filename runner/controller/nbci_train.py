"""Continuous merge train (develop) + gated promotion develop -> main, for the nb-ci controller (API-only: gh + GCS).

Train: every open non-draft PR to develop in the train orgs whose checks are all green and that is mergeable merges
by itself (`gh pr merge --squash --match-head-commit <sha>`, never --admin). No label, no deadline. Skips: repo
neonbee-devops, labels hold / do-not-merge / needs-migration, any database/migrations or *.sql file, drafts,
conflicts, red or pending checks. Up to `cap` PRs merge per tick in parallel, at most one per repo (a second merge
in the same repo would race the first one's new develop head), Shell (so360-shell-fe) first.

Promote (switch nbci-promote-main, default off): develop head D of a repo goes to main only when AutoQA on Dev
recorded result.json "passed" for exactly D (gs://.../replay/dev/<D>/result.json). not_tested, failed, missing
or a result of another sha never promote. After the main merge the Live AutoQA result for the main sha must pass,
else one alert (nbci_alert -> Pulse alert task + Teams). Pure decisions are module functions; I/O goes through the
`GH` / `Store` objects so tests inject fakes."""
import json, os, re, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor

E = os.environ.get
FIRST_REPO = 'so360-shell-fe'
SKIP_REPOS = {'neonbee-devops'}
HOLD_LABELS = {'hold', 'do-not-merge', 'needs-migration'}
# never merged by the train (canaries / hand-held PRs), plus MT_SKIP_PRS="repo#n,repo#n"
SKIP_PRS = {'so360-accounting-fe#127', 'so360-accounting-fe#128', 'neonbee-website#12'} | \
    {x.strip() for x in (os.environ.get('MT_SKIP_PRS') or '').split(',') if x.strip()}
CLEAN = ('CLEAN', 'HAS_HOOKS')
LIVE_WAIT_S = 90 * 60            # no Live AQA result this long after the main merge -> alert


def log(*a):
    print(time.strftime('[%H:%M:%S]'), *a, flush=True)


# ── pure decisions ────────────────────────────────────────────────────────────────────────────────
def plan(env):
    """(train?, promote?, why) from /etc/merge-train.env values."""
    train = (env.get('MT_TRAIN') or 'develop').strip().lower() == 'develop'
    promote = (env.get('MT_PROMOTE') or 'off').strip().lower() == 'on'
    return train, promote and train, 'merge train %s, promote main %s' % ('develop' if train else 'off', 'on' if promote and train else 'off')


def is_migration(path):
    return bool(re.search(r'(^|/)database/migrations/', path) or path.lower().endswith('.sql'))


def checks_state(rollup):
    """'green' | 'pending' | 'failed' | 'none' from a gh statusCheckRollup."""
    items = rollup or []
    if not items:
        return 'none'
    done_ok = pending = failed = 0
    for c in items:
        if c.get('__typename') == 'StatusContext':
            st = c.get('state')
            if st in ('PENDING', 'EXPECTED'): pending += 1
            elif st == 'SUCCESS': done_ok += 1
            else: failed += 1
        elif c.get('status') != 'COMPLETED':
            pending += 1
        elif c.get('conclusion') in ('SUCCESS', 'SKIPPED', 'NEUTRAL'):
            done_ok += 1
        else:
            failed += 1
    return 'failed' if failed else 'pending' if pending else 'green'


def pr_verdict(repo, pr, files):
    """(eligible, reason) for one open PR."""
    if repo in SKIP_REPOS:
        return False, 'repo %s is merged by hand' % repo
    if '%s#%s' % (repo, pr.get('number')) in SKIP_PRS:
        return False, 'PR held by hand (SKIP_PRS)'
    if pr.get('state') != 'OPEN' or pr.get('baseRefName') != 'develop':
        return False, 'not an open PR to develop'
    if pr.get('isDraft'):
        return False, 'draft'
    held = HOLD_LABELS & {l.get('name', '').lower() for l in pr.get('labels') or []}
    if held:
        return False, 'label ' + ','.join(sorted(held))
    mig = [f for f in files if is_migration(f)]
    if mig:
        return False, 'migration/sql: ' + ', '.join(mig[:3])
    cs = checks_state(pr.get('statusCheckRollup'))
    if cs != 'green':
        return False, 'checks ' + cs
    if pr.get('mergeable') != 'MERGEABLE' or pr.get('mergeStateStatus') not in CLEAN:
        return False, 'not mergeable: %s/%s' % (pr.get('mergeable'), pr.get('mergeStateStatus'))
    return True, 'green'


def order_prs(items):
    """items: dicts with repo + createdAt. Shell first, then oldest first."""
    return sorted(items, key=lambda i: (i['repo'] != FIRST_REPO, i.get('createdAt') or ''))


def pick(items, cap):
    """Ordered eligible items -> the ones that merge this tick: one per repo, at most cap."""
    out, seen = [], set()
    for i in order_prs(items):
        if i['repo'] in seen:
            continue
        seen.add(i['repo']); out.append(i)
        if len(out) >= cap:
            break
    return out


def aqa_verdict(res, sha):
    """'passed' | 'not_tested' | 'failed' | 'missing' | 'stale' for a result.json read for `sha`."""
    if not isinstance(res, dict) or not res:
        return 'missing'
    if res.get('sha') and res['sha'] != sha:
        return 'stale'
    try: code = int(res['exit']) if 'exit' in res else None
    except (TypeError, ValueError): code = -1
    if res.get('state') == 'not_tested' or code == 4:
        return 'not_tested'
    if code == 0 or (code is None and res.get('state') in ('passed', 'pass')):
        return 'passed'
    return 'failed'


def promote_gate(dev_verdict):
    """(ok, reason): only a real Dev pass promotes."""
    return (True, 'Dev AutoQA passed') if dev_verdict == 'passed' else (False, 'Dev AutoQA %s' % dev_verdict)


def live_check(rec, verdict, now):
    """'verified' | 'alert' | 'wait' for a promoted record {merged: epoch} given the Live AQA verdict."""
    if verdict == 'passed':
        return 'verified'
    if verdict in ('failed', 'not_tested', 'stale'):
        return 'alert'
    return 'alert' if now - rec['merged'] > LIVE_WAIT_S else 'wait'


# ── I/O ───────────────────────────────────────────────────────────────────────────────────────────
class GH:
    def __init__(self, orgs, extra_repos=()):
        self.orgs, self.extra = orgs, list(extra_repos)

    def run(self, *a):
        r = subprocess.run(('gh',) + a, capture_output=True, text=True, timeout=120)
        if r.returncode:
            raise RuntimeError((r.stderr or r.stdout).strip()[:400])
        return r.stdout

    def j(self, *a):
        return json.loads(self.run(*a) or 'null')

    def open_prs(self):
        out = []
        for o in self.orgs:
            for p in self.j('search', 'prs', '--owner', o, '--state', 'open', '--limit', '200', '--json', 'repository,number') or []:
                out.append((p['repository']['nameWithOwner'], p['number']))
        return out

    def view(self, repo, n):
        return self.j('pr', 'view', str(n), '-R', repo, '--json',
                      'number,state,isDraft,baseRefName,headRefOid,mergeable,mergeStateStatus,labels,statusCheckRollup,createdAt,url,mergeCommit')

    def files(self, repo, n):
        return [x for x in self.run('api', '--paginate', 'repos/%s/pulls/%s/files?per_page=100' % (repo, n), '-q', '.[].filename').split('\n') if x]

    def merge(self, repo, n, sha, method):
        try:
            self.run('pr', 'merge', str(n), '-R', repo, '--' + method, '--match-head-commit', sha)   # never --admin
            return True, ''
        except RuntimeError as e:
            return False, str(e).split('\n')[0]

    def repos(self):
        if self.extra:
            return self.extra
        out = []
        for o in self.orgs:
            out += ['%s/%s' % (o, r['name']) for r in self.j('repo', 'list', o, '--no-archived', '--limit', '300', '--json', 'name') or []
                    if r['name'].startswith('so360-') and r['name'] not in SKIP_REPOS]
        return out

    def develop_state(self, repo):
        """(develop head sha, commits develop is ahead of main, files changed) or None when a branch is missing."""
        try:
            c = self.j('api', 'repos/%s/compare/main...develop' % repo)
        except RuntimeError:
            return None
        return c['commits'][-1]['sha'] if c.get('commits') else None, c.get('ahead_by', 0), [f['filename'] for f in c.get('files', [])]

    def develop_head(self, repo):
        return self.run('api', 'repos/%s/commits/develop' % repo, '-q', '.sha').strip()

    def promo_pr(self, repo):
        l = self.j('pr', 'list', '-R', repo, '--base', 'main', '--head', 'develop', '--state', 'open', '--json', 'number')
        return l[0]['number'] if l else None

    def open_promo_pr(self, repo, body):
        self.run('pr', 'create', '-R', repo, '--base', 'main', '--head', 'develop', '--title',
                 'release: develop -> main (AutoQA Dev passed)', '--body', body)


class Store:
    """AQA results (GCS), alerts and the small state file."""
    def __init__(self, gcp, home, bucket_tpl, alert):
        self.gcp, self.home, self.tpl, self._alert = gcp, home, bucket_tpl, alert

    def aqa_result(self, env, sha):
        b, _, path = (self.tpl.format(env=env, sha=sha, repo='')[5:]).partition('/')
        try:
            return self.gcp.storage_json(b, path.rstrip('/') + '/result.json')
        except RuntimeError as e:
            if ' 404 ' in str(e):
                return None
            raise

    def alert(self, text):
        self._alert(text)

    def load(self):
        try:
            return json.load(open(os.path.join(self.home, 'state.json')))
        except (OSError, ValueError):
            return {'promoted': {}, 'alerted': {}}

    def save(self, st):
        os.makedirs(self.home, exist_ok=True)
        p = os.path.join(self.home, 'state.json')
        json.dump(st, open(p + '.tmp', 'w')); os.replace(p + '.tmp', p)


# ── ticks ─────────────────────────────────────────────────────────────────────────────────────────
def train_tick(gh, cap=10, dry=False):
    """Merge the eligible develop PRs; returns [(repo, n, sha)] merged (or, dry, that would merge)."""
    def look(rn):
        repo, n = rn
        try:
            pr = gh.view(repo, n)
            if pr.get('baseRefName') != 'develop' or repo.split('/')[-1] in SKIP_REPOS:
                return None
            ok, why = pr_verdict(repo.split('/')[-1], pr, gh.files(repo, n) if checks_state(pr.get('statusCheckRollup')) == 'green' else [])
            log('pr', repo, n, 'ok' if ok else 'skip: ' + why)
            return {'repo': repo, 'n': n, 'sha': pr['headRefOid'], 'createdAt': pr.get('createdAt')} if ok else None
        except Exception as e:
            log('pr error', repo, n, str(e)[:120]); return None
    with ThreadPoolExecutor(max_workers=cap) as ex:
        cands = [c for c in ex.map(look, gh.open_prs()) if c]
    for c in cands:                      # pick() orders on the bare repo name
        c['full'] = c['repo']; c['repo'] = c['repo'].split('/')[-1]
    chosen = pick(cands, cap)

    def go(c):
        if dry:
            log('DRY merge', c['full'], c['n']); return c
        ok, why = gh.merge(c['full'], c['n'], c['sha'], 'squash')
        log('merged' if ok else 'merge refused', c['full'], c['n'], why)
        return c if ok else None
    with ThreadPoolExecutor(max_workers=cap) as ex:
        return [(c['full'], c['n'], c['sha']) for c in ex.map(go, chosen) if c]


def promote_tick(gh, store, now=None, dry=False):
    """Promote every repo whose develop head has a real Dev AQA pass; verify Live for earlier promotions."""
    now = now or time.time()
    st = store.load(); acts = []
    for repo, rec in list(st['promoted'].items()):                     # Live check of earlier promotions
        if rec.get('done'):
            continue
        v = aqa_verdict(store.aqa_result('prod', rec['main_sha']), rec['main_sha'])
        what = live_check(rec, v, now)
        if what == 'verified':
            rec['done'] = 'verified'; acts.append((repo, 'live verified'))
        elif what == 'alert':
            rec['done'] = 'alerted'; acts.append((repo, 'live ' + v))
            if not dry:
                store.alert('Live AutoQA %s for %s@%s after promotion develop->main (Live may be broken; evidence replay/prod/%s)' % (v, repo, rec['main_sha'][:8], rec['main_sha'][:12]))
    for repo in gh.repos():
        try:
            ds = gh.develop_state(repo)
            if not ds or not ds[1]:
                continue
            head, _, files = ds
            if any(is_migration(f) for f in files):
                log('promote skip', repo, 'migration/sql in develop..main'); continue
            if st['promoted'].get(repo, {}).get('dev_sha') == head:
                continue
            ok, why = promote_gate(aqa_verdict(store.aqa_result('dev', head), head))
            if not ok:
                log('promote hold', repo, why); continue
            n = gh.promo_pr(repo)
            if not n:
                acts.append((repo, 'open PR'))
                if not dry: gh.open_promo_pr(repo, 'Promotes develop %s: Dev AutoQA result "passed" for this exact sha.' % head)
                continue
            pr = gh.view(repo, n)
            if pr['headRefOid'] != head or checks_state(pr.get('statusCheckRollup')) != 'green' or pr.get('mergeStateStatus') not in CLEAN:
                log('promote wait', repo, 'PR checks/head'); continue
            if dry:
                acts.append((repo, 'would merge #%s' % n)); continue
            ok, why = gh.merge(repo, n, head, 'merge')
            if not ok:
                log('promote merge refused', repo, why); continue
            after = gh.view(repo, n)
            st['promoted'][repo] = {'dev_sha': head, 'main_sha': (after.get('mergeCommit') or {}).get('oid', ''), 'merged': now, 'n': n}
            acts.append((repo, 'promoted #%s' % n))
        except Exception as e:
            log('promote error', repo, str(e)[:160])
    if not dry:
        store.save(st)
    return acts


def main():
    train, promote, why = plan(os.environ)
    log(why)
    if not train:
        return 0
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from nbci_io import GCP
    import nbci_alert
    orgs = [o for o in (E('MT_ORGS') or 'Neonbee-ai').split(',') if o]
    gh = GH(orgs, [r for r in (E('MT_REPOS') or '').split(',') if r])
    dry = E('DRY') == '1'
    done = train_tick(gh, int(E('MT_CAP') or 10), dry)
    log('train merged %d' % len(done))
    if promote:
        gcp = GCP(E('GCP_PROJECT', 'gen-lang-client-0521938031'))
        store = Store(gcp, E('MT_HOME', '/var/lib/merge-train'), E('MT_EVIDENCE') or 'gs://neonbee-aqa-0521938031/replay/{env}/{sha}',
                      lambda t: nbci_alert.send(t, gcp, dry))
        for a in promote_tick(gh, store, dry=dry):
            log('promote', *a)
    return 0


if __name__ == '__main__':
    sys.exit(main())
