"""nb-release: zero-agent release flow, driven by nb_autoscaler.py every RELEASE_S seconds.

The org is on the GitHub Free plan: private repos get no branch protection, rulesets, merge queue or
auto-merge. This module is the merge queue instead, and it is the ONLY thing that merges:

  merge train   PRs labelled `nb-merge` (gh-merge-gated.sh adds it), per repo+base one at a time, oldest
                first: needs a Pulse id in the title, no conflicts, head up to date with the base (else
                it calls update-branch, so CI re-tests against the latest base), every check run on the
                head green and a PR Gate check present -> merge (merge commit, pinned to the head sha).
  promote       a release branch merged to main whose main push runs are all green (deploy, deploy-aws,
                golden gates) -> label the open develop PR from the same branch `nb-merge`.
  released      same trigger -> every Pulse id in the main PR's title/body moves to PULSE_RELEASED_STATUS
                with a comment (Pulse MCP over HTTP, token PULSE_TOKEN_FILE).

Everything is idempotent and restart-safe: decisions are re-derived from GitHub each pass; the state
file only remembers what was already announced. Opt-in per repo: TRAIN_REPOS (comma list or '*').
"""
import json, os, re, time, urllib.request

E = os.environ.get
LABEL = E('TRAIN_LABEL', 'nb-merge')
PULSE_RE = re.compile(r'PULSE-([0-9a-f]{8})\b', re.I)
OK = {'success', 'skipped', 'neutral'}
BAD_RUN = {'failure', 'cancelled', 'timed_out', 'action_required', 'startup_failure', 'stale'}
GATE_CHECK = re.compile(E('GATE_CHECK_RE', r'(^|/ )Gate$'))


# ── pure logic ──────────────────────────────────────────────────────────────────────────────────────
def pulse_ids(*texts):
    seen = []
    for t in texts:
        for m in PULSE_RE.findall(t or ''):
            if m.lower() not in seen:
                seen.append(m.lower())
    return seen


def checks_verdict(check_runs):
    """'green' | 'pending' | 'red' | 'no-gate' for the head commit's check runs (latest per name)."""
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


def train_action(pr, behind_by, verdict):
    """Next step for the head-of-line PR: merge | update | wait | eject:<why>."""
    if pr.get('draft'):
        return 'eject:draft'
    if not pulse_ids(pr.get('title')):
        return 'eject:title needs a Pulse id (PULSE-xxxxxxxx)'
    if pr.get('mergeable') is False or pr.get('mergeable_state') == 'dirty':
        return 'eject:merge conflict with base'
    if behind_by > 0:
        return 'update'
    if verdict == 'red':
        return 'eject:a check failed on the head commit'
    if verdict == 'no-gate':
        return 'wait'          # PR Gate not reported yet (it may start after update-branch)
    if verdict == 'pending' or pr.get('mergeable') is None:
        return 'wait'
    return 'merge'


def main_green(runs):
    return bool(runs) and all(r['status'] == 'completed' and r['conclusion'] not in BAD_RUN for r in runs)


# ── I/O (gh = nb_autoscaler.gh, injected) ───────────────────────────────────────────────────────────
class Release:
    def __init__(self, gh, org, log, alert, state, dry=False):
        self.gh, self.org, self.log, self.alert, self.st, self.dry = gh, org, log, alert, state, dry
        self.st.setdefault('announced', [])
        self.st.setdefault('ejected', [])

    def repos(self):
        allow = E('TRAIN_REPOS', '')
        if allow == '*':
            return [r['name'] for r in self.gh('orgs/%s/repos?sort=pushed&direction=desc&per_page=100' % self.org, cache=True)
                    if not r['archived']]
        return [x.strip() for x in allow.split(',') if x.strip()]

    def comment(self, repo, n, body):
        if self.dry:
            self.log('DRY comment', repo, n, body[:80]); return
        self.gh('repos/%s/%s/issues/%s/comments' % (self.org, repo, n), 'POST', {'body': body})

    def label(self, repo, n, add=True):
        if self.dry:
            self.log('DRY label', repo, n, add); return
        if add:
            self.gh('repos/%s/%s/issues/%s/labels' % (self.org, repo, n), 'POST', {'labels': [LABEL]})
        else:
            try: self.gh('repos/%s/%s/issues/%s/labels/%s' % (self.org, repo, n, LABEL), 'DELETE')
            except RuntimeError: pass

    def train(self, repo):
        prs = self.gh('repos/%s/%s/pulls?state=open&sort=created&direction=asc&per_page=50' % (self.org, repo))
        heads = {}
        for p in prs:
            if p['base']['ref'] in ('main', 'develop') and any(l['name'] == LABEL for l in p['labels']):
                heads.setdefault(p['base']['ref'], p)          # oldest labelled PR per base = head of line
        for base, p in heads.items():
            pr = self.gh('repos/%s/%s/pulls/%s' % (self.org, repo, p['number']))
            sha = pr['head']['sha']
            cmp = self.gh('repos/%s/%s/compare/%s...%s' % (self.org, repo, base, sha))
            runs = self.gh('repos/%s/%s/commits/%s/check-runs?per_page=100' % (self.org, repo, sha))['check_runs']
            act = train_action(pr, cmp.get('behind_by', 0), checks_verdict(runs))
            tag = '%s#%s->%s' % (repo, pr['number'], base)
            if act == 'merge':
                if self.dry:
                    self.log('DRY merge', tag); continue
                self.gh('repos/%s/%s/pulls/%s/merge' % (self.org, repo, pr['number']), 'PUT',
                        {'merge_method': 'merge', 'sha': sha})
                self.log('merged', tag, sha[:9])
            elif act == 'update':
                if not self.dry:
                    self.gh('repos/%s/%s/pulls/%s/update-branch' % (self.org, repo, pr['number']), 'PUT',
                            {'expected_head_sha': sha})
                self.log('update-branch', tag, '(re-test against latest %s)' % base)
            elif act.startswith('eject:'):
                why = act[6:]
                self.label(repo, pr['number'], add=False)
                self.comment(repo, pr['number'], 'nb-merge train: removed from the queue: %s. Fix it and add the `%s` label again.' % (why, LABEL))
                self.alert('merge train ejected %s: %s' % (tag, why))

    def after_main(self, repo, pulse):
        since = time.time() - 12 * 3600
        for pr in self.gh('repos/%s/%s/pulls?state=closed&base=main&sort=updated&direction=desc&per_page=15' % (self.org, repo)):
            if not pr.get('merged_at') or _ts(pr['merged_at']) < since:
                continue
            key = '%s#%s' % (repo, pr['number'])
            if key in self.st['announced']:
                continue
            runs = self.gh('repos/%s/%s/actions/runs?head_sha=%s&branch=main&event=push&per_page=30'
                           % (self.org, repo, pr['merge_commit_sha']))['workflow_runs']
            if not main_green(runs):
                if runs and all(r['status'] == 'completed' for r in runs) and key not in self.st['ejected']:
                    self.st['ejected'].append(key)
                    self.alert('main deploy RED for %s (%s): develop not promoted, Pulse not released' % (key, pr['title'][:60]))
                continue
            head = pr['head']['ref']
            for d in self.gh('repos/%s/%s/pulls?state=open&base=develop&head=%s:%s' % (self.org, repo, self.org, head)):
                self.label(repo, d['number'])
                self.log('promote', repo, 'develop #%s queued after green main %s' % (d['number'], key))
            for tid in pulse_ids(pr.get('title'), pr.get('body')):
                pulse(tid, 'Released to production: %s/%s PR #%s (%s), main deploy green.'
                      % (self.org, repo, pr['number'], pr['merge_commit_sha'][:9]))
            self.st['announced'].append(key)
            self.st['announced'] = self.st['announced'][-500:]

    def ensure_label(self, repo):
        done = self.st.setdefault('labelled', [])
        if repo in done or self.dry:
            return
        try:
            self.gh('repos/%s/%s/labels' % (self.org, repo), 'POST',
                    {'name': LABEL, 'color': '0e8a16', 'description': 'queued on the nb-autoscaler merge train'})
        except RuntimeError as e:
            if ' 422 ' not in str(e):          # 422 = already exists
                raise
        done.append(repo)

    def tick(self, pulse):
        for repo in self.repos():
            try:
                self.ensure_label(repo)
                self.train(repo)
                self.after_main(repo, pulse)
            except Exception as e:
                self.log('release error', repo, str(e)[:200])


def _ts(s):
    import calendar
    return calendar.timegm(time.strptime(s[:19], '%Y-%m-%dT%H:%M:%S'))


# ── Pulse (MCP JSON-RPC over HTTP) ──────────────────────────────────────────────────────────────────
class Pulse:
    def __init__(self, log, dry=False):
        self.url = E('PULSE_MCP_URL', '')
        tf = E('PULSE_TOKEN_FILE')
        self.token = open(tf).read().strip() if tf and os.path.exists(tf) else ''
        self.status = E('PULSE_RELEASED_STATUS', 'released')
        self.log, self.dry, self.sid, self.n = log, dry, None, 0

    def _rpc(self, method, params):
        self.n += 1
        h = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream',
             'Authorization': 'Bearer ' + self.token}
        if self.sid:
            h['Mcp-Session-Id'] = self.sid
        body = json.dumps({'jsonrpc': '2.0', 'id': self.n, 'method': method, 'params': params}).encode()
        with urllib.request.urlopen(urllib.request.Request(self.url, data=body, headers=h, method='POST'), timeout=30) as f:
            self.sid = f.headers.get('Mcp-Session-Id') or self.sid
            raw = f.read().decode()
        for line in raw.splitlines():                      # plain JSON or SSE "data: {...}"
            line = line[5:].strip() if line.startswith('data:') else line.strip()
            if line.startswith('{'):
                return json.loads(line)
        return {}

    def __call__(self, task_id, note):
        if not (self.url and self.token):
            self.log('pulse not configured; would release', task_id); return
        if self.dry:
            self.log('DRY pulse', task_id, self.status); return
        if not self.sid:
            self._rpc('initialize', {'protocolVersion': '2025-03-26', 'capabilities': {},
                                     'clientInfo': {'name': 'nb-autoscaler', 'version': '1'}})
        for name, args in (('update_task_status', {'task_id': task_id, 'status': self.status}),
                           ('add_comment', {'task_id': task_id, 'message': note})):
            r = self._rpc('tools/call', {'name': name, 'arguments': args})
            self.log('pulse', name, task_id, json.dumps(r.get('result', r.get('error', {})))[:120])
