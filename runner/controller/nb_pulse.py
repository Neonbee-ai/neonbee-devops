"""Pulse integration for nb-ci: failures and fixes are written to Pulse through a persistent, retrying
queue. CI never waits for Pulse: every call is queued in the controller state, drained a few per pass,
retried with exponential backoff (1 min .. 1 h) and dropped with a log line after 48 h.

On a failure (PR check red after the one infra retry, or main RED), for every Pulse id of the PR:
  1. add_comment: repo, PR, failing check, run URL, <= 20 log lines
  2. update_task_status -> 'fixing'   (stage key, project f0df5df0 default workflow)
  3. one open bug per task+check: create_task(parent_task_id=task) with repro, logs, severity; a
     repeated failure only comments on that bug
  4. test cases of the task that map to the check (title contains the check name or "[check:<name>]")
     -> update_test_case status 'fail' + an evidence comment
On the fix (same check green later): comment + bug -> 'done', task -> 'reverify'
(released follows the green prod deploy, see nb_release.after_main).

AutoQA hook: drop a JSON file into SPOOL_DIR (default /var/lib/nbci/pulse-spool) or call
report_failure() directly:
  {"kind": "failure"|"fixed", "task_id": "1a2b3c4d", "check": "AutoQA L13 drip", "repo": "...",
   "pr": 0, "url": "https://...", "excerpt": "...", "severity": "High", "test_case_ids": ["..."]}
"""
import glob, json, os, re, time, urllib.request

E = os.environ.get
SPOOL_DIR = E('PULSE_SPOOL_DIR', '/var/lib/nbci/pulse-spool')
FIXING, FIXED_NEXT, DONE = E('PULSE_FIXING_STATUS', 'fixing'), E('PULSE_REVERIFY_STATUS', 'reverify'), E('PULSE_BUG_DONE', 'done')
# Pulse task id of a commit/PR: ONLY the `Pulse:` trailer (last paragraph of the commit message),
# else the branch name <type>/<8hex>-..., else `Pulse:` lines in the PR body. Never loose hex.
TRAILER_RE = re.compile(r'^Pulse:[ \t]*(none|[0-9a-f]{8}(?:-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})?)[ \t]*$', re.I)
BRANCH_RE = re.compile(r'^[a-z]+/([0-9a-f]{8})(?:-|$)', re.I)
ID_RE = re.compile(r'\b([0-9a-f]{8}(?:-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})?)\b')
PERMANENT = ('not found', 'ACCESS_DENIED', 'access denied')


class MCP:
    def __init__(self, url, token):
        self.url, self.token, self.sid, self.n = url, token, None, 0

    def rpc(self, method, params):
        self.n += 1
        h = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream',
             'Authorization': 'Bearer ' + self.token}
        if self.sid:
            h['Mcp-Session-Id'] = self.sid
        body = json.dumps({'jsonrpc': '2.0', 'id': self.n, 'method': method, 'params': params}).encode()
        with urllib.request.urlopen(urllib.request.Request(self.url, data=body, headers=h, method='POST'), timeout=20) as f:
            self.sid = f.headers.get('Mcp-Session-Id') or self.sid
            raw = f.read().decode()
        for line in raw.splitlines():
            line = line[5:].strip() if line.startswith('data:') else line.strip()
            if line.startswith('{'):
                return json.loads(line)
        return {}

    def call(self, tool, args):
        """Returns the tool's text. Raises on transport/server errors (-> retried)."""
        if not self.sid:
            self.rpc('initialize', {'protocolVersion': '2025-03-26', 'capabilities': {},
                                    'clientInfo': {'name': 'nb-ci', 'version': '3.1'}})
        r = self.rpc('tools/call', {'name': tool, 'arguments': args})
        if 'error' in r:
            self.sid = None
            raise RuntimeError(json.dumps(r['error'])[:200])
        res = r.get('result', {})
        text = ' '.join(c.get('text', '') for c in res.get('content', []) if isinstance(c, dict))
        if res.get('isError'):
            raise RuntimeError(text[:200] or 'tool error')
        return text


class PulseQueue:
    def __init__(self, state, client, log, dry=False):
        self.st, self.c, self.log, self.dry = state, client, log, dry
        state.setdefault('q', []); state.setdefault('bugs', {})

    # ── enqueue (never raises, never blocks) ──────────────────────────────────────────────────────
    def _put(self, op, **kw):
        self.st['q'].append(dict(kw, op=op, tries=0, next_at=0, born=time.time()))

    def report_failure(self, task_id, check, repo='', pr=0, url='', excerpt='', severity='Medium', test_case_ids=None):
        where = '%s PR #%s' % (repo, pr) if pr else repo
        note = ('CI failure: %s — check "%s" failed.\nRun: %s\nLog excerpt:\n%s'
                % (where, check, url, excerpt[:3000]))
        self._put('comment', task=task_id, text=note)
        self._put('status', task=task_id, status=FIXING)
        self._put('bug', task=task_id, check=check, repo=repo, pr=pr, url=url, excerpt=excerpt, severity=severity)
        self._put('testcases', task=task_id, check=check, url=url, ids=test_case_ids or [])

    def report_fixed(self, task_id, check, repo='', pr=0, url=''):
        if self.st['bugs'].get('%s|%s' % (task_id, check), {}).get('open'):
            self._put('fixed', task=task_id, check=check, text='Fixed: check "%s" is green again on %s PR #%s. %s' % (check, repo, pr, url))

    def ingest_spool(self):
        for f in sorted(glob.glob(os.path.join(SPOOL_DIR, '*.json')))[:50]:
            try:
                d = json.load(open(f))
                if d.get('kind') == 'fixed':
                    self.report_fixed(d['task_id'], d['check'], d.get('repo', ''), d.get('pr', 0), d.get('url', ''))
                else:
                    self.report_failure(d['task_id'], d['check'], d.get('repo', ''), d.get('pr', 0), d.get('url', ''),
                                        d.get('excerpt', ''), d.get('severity', 'Medium'), d.get('test_case_ids'))
            except Exception as e:
                self.log('pulse spool bad file', f, e)
            try: os.unlink(f)
            except OSError: pass

    # ── drain (bounded work per pass) ─────────────────────────────────────────────────────────────
    def drain(self, budget=15):
        now, keep, done = time.time(), [], 0
        for op in self.st['q']:
            if done >= budget or op['next_at'] > now:
                keep.append(op); continue
            done += 1
            try:
                if not self.dry:
                    self._run(op)
                else:
                    self.log('DRY pulse', op['op'], op.get('task'), op.get('check', op.get('status', '')))
            except Exception as e:
                msg = str(e)
                if any(p in msg for p in PERMANENT) or now - op['born'] > 48 * 3600:
                    self.log('pulse drop', op['op'], op.get('task'), msg[:120]); continue
                op['tries'] += 1; op['next_at'] = now + min(3600, 60 * 2 ** op['tries'])
                self.log('pulse retry', op['op'], op.get('task'), 'in %ds' % (op['next_at'] - now), msg[:100])
                keep.append(op)
        self.st['q'] = keep[-2000:]

    def _run(self, op):
        k = op['op']
        if k == 'comment':
            self.c.call('add_comment', {'task_id': op['task'], 'message': op['text']})
        elif k == 'status':
            self.c.call('update_task_status', {'task_id': op['task'], 'status': op['status']})
        elif k == 'bug':
            key = '%s|%s' % (op['task'], op['check'])
            bug = self.st['bugs'].get(key)
            if bug and bug.get('open'):
                bug['count'] = bug.get('count', 1) + 1
                self.c.call('add_comment', {'task_id': bug['id'], 'message': 'Failed again (%d): %s\n%s' % (bug['count'], op['url'], op['excerpt'][:1500])})
                return
            text = self.c.call('create_task', {
                'title': 'Bug: "%s" fails on %s%s' % (op['check'], op['repo'], ' PR #%s' % op['pr'] if op['pr'] else ''),
                'parent_task_id': op['task'], 'priority': op['severity'], 'tags': ['ci-failure', 'bug'],
                'description': ('Found by nb-ci.\nRepro: re-run check "%s" on %s%s.\nRun: %s\nSeverity: %s\n'
                                'Expected: the check passes. Actual: it fails with:\n%s')
                               % (op['check'], op['repo'], ' PR #%s' % op['pr'] if op['pr'] else '', op['url'], op['severity'], op['excerpt'][:3000]),
                'acceptance_criteria': ['check "%s" is green on the PR head' % op['check'], 'no regression in the other checks']})
            ids = [i for i in ID_RE.findall(text) if not op['task'].startswith(i[:8])]
            if not ids:
                raise RuntimeError('create_task returned no id: %s' % text[:120])
            self.st['bugs'][key] = {'id': ids[0], 'open': True, 'count': 1, 'since': time.time()}
        elif k == 'testcases':
            ids = op['ids']
            if not ids:
                text = self.c.call('list_test_cases', {'task_id': op['task'], 'limit': 200})
                ids = match_test_cases(text, op['check'])
            for tc in ids:
                self.c.call('update_test_case', {'task_id': op['task'], 'test_case_id': tc, 'status': 'fail'})
            if ids:
                self.c.call('add_comment', {'task_id': op['task'], 'message': 'Test case(s) %s marked failed by check "%s": %s'
                                                                               % (', '.join(i[:8] for i in ids), op['check'], op['url'])})
        elif k == 'fixed':
            key = '%s|%s' % (op['task'], op['check'])
            bug = self.st['bugs'].get(key)
            if not bug or not bug.get('open'):
                return
            self.c.call('add_comment', {'task_id': bug['id'], 'message': op['text']})
            self.c.call('update_task_status', {'task_id': bug['id'], 'status': DONE})
            bug['open'] = False
            if not any(b.get('open') for kk, b in self.st['bugs'].items() if kk.startswith(op['task'] + '|')):
                self.c.call('add_comment', {'task_id': op['task'], 'message': op['text']})
                self.c.call('update_task_status', {'task_id': op['task'], 'status': FIXED_NEXT})


def trailer(msg):
    """'none', an 8-hex id or a uuid (lowercased) from the message's trailer block (its last paragraph,
    not the subject), or None. Comment lines (#) are ignored, as git does."""
    text = '\n'.join(l for l in (msg or '').splitlines() if not l.startswith('#')).strip()
    paras = [p for p in re.split(r'\n[ \t]*\n', text) if p.strip()]
    if len(paras) < 2:
        return None
    for line in paras[-1].splitlines():
        m = TRAILER_RE.match(line.strip())
        if m:
            return m.group(1).lower()
    return None


def exempt(msg):
    """Git-generated messages (merge, revert, fixup/squash) need no trailer."""
    s = (msg or '').lstrip()
    return s.startswith(('Merge ', 'Revert "', 'fixup! ', 'squash! ', 'amend! '))


def missing_trailers(msgs):
    """Indexes of the commit messages that carry no `Pulse:` trailer and are not exempt."""
    return [i for i, m in enumerate(msgs or []) if not exempt(m) and trailer(m) is None]


def task_ids(msgs, branch='', body=''):
    """Pulse task ids of a PR: commit trailers, else the branch fix/<8hex>-..., else `Pulse:` lines in
    the PR body. `Pulse: none` is a valid trailer that names no task."""
    ids = []

    def add(i):
        if i and i != 'none' and all(x[:8] != i[:8] for x in ids):
            ids.append(i)
    for m in msgs or []:
        add(trailer(m))
    if not ids:
        b = BRANCH_RE.match(branch or '')
        add(b.group(1).lower() if b else None)
    if not ids:
        for line in (body or '').splitlines():
            m = TRAILER_RE.match(line.strip())
            add(m.group(1).lower() if m else None)
    return ids


def match_test_cases(text, check):
    """Pulse returns test cases as text. A case maps to a check when its line contains the check name
    or a "[check:<name>]" tag; returns the test case ids on those lines."""
    out, c = [], check.lower()
    for line in (text or '').splitlines():
        low = line.lower()
        if c and (c in low or ('[check:%s]' % c) in low):
            out += [i for i in ID_RE.findall(line)]
    return out


def open_bug(state, task_id, check):
    return bool(state.get('bugs', {}).get('%s|%s' % (task_id, check), {}).get('open'))
