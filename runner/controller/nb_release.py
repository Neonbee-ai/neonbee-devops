"""Zero-agent release flow (Free plan: no protection / merge queue / auto-merge, so this is the queue).

merge train   open PRs labelled nb-merge, per repo+base the oldest first. Needs a Pulse id in the title,
              `needs-migration` holds it until `migration-applied` is added, main PRs follow the release
              manifest groups (core -> backends -> frontends -> shell-fe -> devops). Behind its base ->
              update-branch (CI re-tests on the latest base); every check on the head green + PR Gate
              present -> merge commit pinned to the head sha. Ejects with a comment + Teams on failure.
after main    a merged main PR whose main push runs are all green -> the develop PR from the same branch
              gets nb-merge; every Pulse id in its title/body -> PULSE_RELEASED_STATUS + comment.
Idempotent: decisions are re-derived from GitHub every pass; state only remembers announcements.
"""
import json, os, time, urllib.request
import nbci_logic as L

E = os.environ.get
LABEL = E('TRAIN_LABEL', 'nb-merge')


def _ts(s):
    import calendar
    return calendar.timegm(time.strptime(s[:19], '%Y-%m-%dT%H:%M:%S'))


class Release:
    def __init__(self, gh, org, manifest, log, alert, state, dry):
        self.gh, self.org, self.m, self.log, self.alert, self.st, self.dry = gh, org, manifest, log, alert, state, dry
        for k in ('announced', 'red', 'labelled', 'train_shas', 'ejected'):
            self.st.setdefault(k, [])

    def repos(self):
        allow = E('TRAIN_REPOS', '')
        if allow == '*':
            return [r['name'] for r in self.gh('orgs/%s/repos?per_page=100' % self.org, cache=True) if not r['archived']]
        return [x.strip() for x in allow.split(',') if x.strip()]

    def _do(self, what, path, method, body=None):
        if self.dry:
            self.log('DRY', what, path); return {}
        return self.gh(path, method, body)

    def ensure_label(self, repo):
        if repo in self.st['labelled'] or self.dry:
            return
        for name, color in ((LABEL, '0e8a16'), ('needs-migration', 'd93f0b'), ('migration-applied', '0075ca')):
            try:
                self.gh('repos/%s/%s/labels' % (self.org, repo), 'POST', {'name': name, 'color': color})
            except RuntimeError as e:
                if ' 422 ' not in str(e):
                    raise
        self.st['labelled'].append(repo)

    def queued(self, repo):
        prs = self.gh('repos/%s/%s/pulls?state=open&sort=created&direction=asc&per_page=50' % (self.org, repo), cache=True)
        return [p for p in prs if p['base']['ref'] in ('main', 'develop') and any(l['name'] == LABEL for l in p['labels'])]

    def pending_main(self, queued_by_repo):
        """Repos with release work in flight on main: a queued main PR, or a merge whose main runs are not green yet."""
        out = {r for r, prs in queued_by_repo.items() if any(p['base']['ref'] == 'main' for p in prs)}
        for key in self.st.get('inflight', []):
            out.add(key.split('#')[0])
        return out

    def train(self, repo, prs, open_grp):
        heads = {}
        for p in prs:
            heads.setdefault(p['base']['ref'], p)
        for base, p in heads.items():
            pr = self.gh('repos/%s/%s/pulls/%s' % (self.org, repo, p['number']))
            sha = pr['head']['sha']
            behind = self.gh('repos/%s/%s/compare/%s...%s' % (self.org, repo, base, sha)).get('behind_by', 0)
            runs = self.gh('repos/%s/%s/commits/%s/check-runs?per_page=100' % (self.org, repo, sha))['check_runs']
            grp_ok = base != 'main' or open_grp is None or L.group_of(repo, self.m) <= open_grp
            act = L.train_action(pr, behind, L.checks_verdict(runs), grp_ok)
            tag = '%s#%s->%s' % (repo, pr['number'], base)
            if act == 'merge':
                r = self._do('merge', 'repos/%s/%s/pulls/%s/merge' % (self.org, repo, pr['number']), 'PUT',
                             {'merge_method': 'merge', 'sha': sha})
                if r.get('sha'):
                    self.st['train_shas'] = (self.st['train_shas'] + [r['sha']])[-300:]
                    if base == 'main':
                        self.st.setdefault('inflight', []).append('%s#%s' % (repo, pr['number']))
                self.log('merged', tag, sha[:9])
            elif act == 'update':
                self._do('update-branch', 'repos/%s/%s/pulls/%s/update-branch' % (self.org, repo, pr['number']), 'PUT',
                         {'expected_head_sha': sha})
            elif act.startswith('eject:'):
                why = act[6:]
                self._do('unlabel', 'repos/%s/%s/issues/%s/labels/%s' % (self.org, repo, pr['number'], LABEL), 'DELETE')
                self._do('comment', 'repos/%s/%s/issues/%s/comments' % (self.org, repo, pr['number']), 'POST',
                         {'body': 'nb-merge train: removed from the queue: %s. Fix it and add `%s` again.' % (why, LABEL)})
                self.alert('merge train ejected %s: %s' % (tag, why))
            elif act.startswith('hold:') and tag not in self.st['ejected']:
                self.st['ejected'].append(tag); self.log('hold', tag, act[5:])

    def after_main(self, repo, pulse):
        since = time.time() - 12 * 3600
        for pr in self.gh('repos/%s/%s/pulls?state=closed&base=main&sort=updated&direction=desc&per_page=15' % (self.org, repo), cache=True):
            if not pr.get('merged_at') or _ts(pr['merged_at']) < since:
                continue
            key = '%s#%s' % (repo, pr['number'])
            if key in self.st['announced']:
                continue
            runs = self.gh('repos/%s/%s/actions/runs?head_sha=%s&branch=main&event=push&per_page=30'
                           % (self.org, repo, pr['merge_commit_sha']))['workflow_runs']
            if not L.main_green(runs):
                if runs and all(r['status'] == 'completed' for r in runs) and key not in self.st['red']:
                    self.st['red'].append(key)
                    self.alert('main RED for %s: develop not promoted, Pulse not released' % key)
                continue
            for d in self.gh('repos/%s/%s/pulls?state=open&base=develop&head=%s:%s' % (self.org, repo, self.org, pr['head']['ref'])):
                self._do('label develop', 'repos/%s/%s/issues/%s/labels' % (self.org, repo, d['number']), 'POST', {'labels': [LABEL]})
            for tid in L.pulse_ids(pr.get('title'), pr.get('body')):
                pulse(tid, 'Released to production: %s/%s PR #%s (%s), main runs green.'
                      % (self.org, repo, pr['number'], pr['merge_commit_sha'][:9]))
            self.st['announced'] = (self.st['announced'] + [key])[-500:]
            self.st['inflight'] = [k for k in self.st.get('inflight', []) if k != key]

    def tick(self, pulse, only=None):
        repos = [only] if only else self.repos()
        queued = {}
        for repo in repos:
            try:
                self.ensure_label(repo)
                queued[repo] = self.queued(repo)
            except Exception as e:
                self.log('train read error', repo, str(e)[:160])
        grp = L.open_group(self.pending_main(queued), self.m)
        for repo in repos:
            try:
                if queued.get(repo):
                    self.train(repo, queued[repo], grp)
                self.after_main(repo, pulse)
            except Exception as e:
                self.log('release error', repo, str(e)[:200])


class Pulse:
    """Pulse MCP (JSON-RPC over HTTP). Robust choice over a webhook endpoint: the controller already
    sees the green main run, retries every pass, and needs no extra public endpoint."""

    def __init__(self, token, log, dry):
        self.url, self.token = E('PULSE_MCP_URL', ''), token
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
        for line in raw.splitlines():
            line = line[5:].strip() if line.startswith('data:') else line.strip()
            if line.startswith('{'):
                return json.loads(line)
        return {}

    def __call__(self, task_id, note):
        if not (self.url and self.token) or self.dry:
            self.log('pulse %s %s -> %s' % ('DRY' if self.dry else 'unconfigured', task_id, self.status)); return
        if not self.sid:
            self._rpc('initialize', {'protocolVersion': '2025-03-26', 'capabilities': {},
                                     'clientInfo': {'name': 'nb-ci', 'version': '3'}})
        for name, args in (('update_task_status', {'task_id': task_id, 'status': self.status}),
                           ('add_comment', {'task_id': task_id, 'message': note})):
            r = self._rpc('tools/call', {'name': name, 'arguments': args})
            self.log('pulse', name, task_id, json.dumps(r.get('result', r.get('error', {})))[:120])
