"""Zero-agent release flow for NB_CI_MODE=auto repos (Free plan: this is the merge queue).

Isolation: one independent train per repo+base. Every labelled PR is evaluated on its own each pass;
any PR that fails is ejected (comment + Teams + Pulse) and the others carry on; the oldest merge-ready
PR merges; if none is ready only the oldest PR behind its base is updated. Errors are caught per PR and
per repo. Manifest order (core -> backends -> frontends -> shell-fe -> devops) applies only to PRs of
the same release/* branch and stops blocking after 2 h, so one bad repo never freezes the rest.

Failures: a red check is classified from its job log. Flaky infra (runner lost, Spot preemption,
network) -> the failed jobs are re-run once automatically and nothing is reported. Anything else ->
eject + Pulse (nb_pulse: comment, 'fixing', one bug per task+check, test cases failed).
Fix: the same check green later -> bug closed, task -> 'reverify'; green main -> develop PR queued,
task -> 'released'.
"""
import os, time
import nbci_logic as L
import nb_pulse as P
from nbci_io import job_log

E = os.environ.get
LABEL = E('TRAIN_LABEL', 'nb-merge')


def _ts(s):
    import calendar
    return calendar.timegm(time.strptime(s[:19], '%Y-%m-%dT%H:%M:%S'))


class Release:
    def __init__(self, gh, org, manifest, log, alert, state, pulse, dry, repos):
        self.gh, self.org, self.m, self.log, self.alert, self.st, self.pq, self.dry = gh, org, manifest, log, alert, state, pulse, dry
        self._refs = {}
        self.repo_list = repos
        for k in ('announced', 'red', 'labelled', 'train_shas'):
            self.st.setdefault(k, [])
        for k in ('retried', 'rel'):
            self.st.setdefault(k, {})

    def _do(self, what, path, method, body=None):
        if self.dry:
            self.log('DRY', what, path); return {}
        return self.gh(path, method, body)

    def ensure_labels(self, repo):
        if repo in self.st['labelled'] or self.dry:
            return
        for name, color in ((LABEL, '0e8a16'), ('needs-migration', 'd93f0b'), ('migration-applied', '0075ca')):
            try: self.gh('repos/%s/%s/labels' % (self.org, repo), 'POST', {'name': name, 'color': color})
            except RuntimeError as e:
                if ' 422 ' not in str(e): raise
        self.st['labelled'].append(repo)

    # ── failure analysis ──────────────────────────────────────────────────────────────────────────
    def failing(self, repo, runs):
        """[(check_run, job, kind, excerpt)] for the red checks of a head commit (latest per name)."""
        latest = {}
        for c in runs:
            if c['name'] not in latest or c['id'] > latest[c['name']]['id']:
                latest[c['name']] = c
        out = []
        for c in latest.values():
            if c['status'] != 'completed' or c['conclusion'] in L.OK:
                continue
            job, text = None, ''
            if (c.get('app') or {}).get('slug') == 'github-actions':
                try:
                    job = self.gh('repos/%s/%s/actions/jobs/%s' % (self.org, repo, c['id']))
                    text = job_log(self.gh, self.org, repo, c['id'])
                except Exception as e:
                    self.log('log fetch', repo, c['name'], str(e)[:80])
            gone = c['conclusion'] in ('cancelled',) and job and (job.get('runner_name') or '').startswith('nbci-')
            kind = L.classify_log(text, c['conclusion'], runner_gone=bool(gone))
            out.append((c, job, kind, L.log_excerpt(text)))
        return out

    def retry_infra(self, repo, fails):
        """True if every failure is flaky infra and was (or is being) retried once."""
        if not fails or any(k != 'infra' for _, _, k, _ in fails):
            return False
        for c, job, kind, _ in fails:
            if not job or not L.should_retry(kind, job.get('run_attempt')):
                return False
        for c, job, _, _ in fails:
            rid = str(job['run_id'])
            if rid not in self.st['retried']:
                self._do('rerun-failed-jobs', 'repos/%s/%s/actions/runs/%s/rerun-failed-jobs' % (self.org, repo, rid), 'POST', {})
                self.st['retried'][rid] = time.time()
                self.log('infra retry', repo, c['name'], rid)
        return True

    def report(self, repo, pr, fails, severity):
        for tid in self.ids(repo, pr):
            for c, job, kind, exc in fails:
                url = (job or {}).get('html_url') or c.get('html_url', '')
                self.pq.report_failure(tid, c['name'], repo, pr.get('number', 0), url, exc, severity)

    def fixed(self, repo, pr, runs):
        green = {c['name'] for c in runs if c['status'] == 'completed' and c['conclusion'] in L.OK}
        for tid in self.ids(repo, pr):
            for key, bug in list(self.pq.st['bugs'].items()):
                t, chk = key.split('|', 1)
                if t == tid and bug.get('open') and chk in green:
                    self.pq.report_fixed(tid, chk, repo, pr.get('number', 0), pr.get('html_url', ''))

    def refs(self, repo, pr):
        """(Pulse task ids, every commit has a trailer) for a PR, cached per head sha."""
        key = '%s#%s@%s' % (repo, pr.get('number'), (pr.get('head') or {}).get('sha', ''))
        if key not in self._refs:
            try:
                r = self.gh('repos/%s/%s/pulls/%s/commits?per_page=100' % (self.org, repo, pr.get('number')))
                msgs = [c['commit']['message'] for c in r] if isinstance(r, list) else []
            except Exception:
                msgs = []
            ids = P.task_ids(msgs, (pr.get('head') or {}).get('ref', ''), pr.get('body'))
            self._refs[key] = (ids, bool(ids) or bool(msgs and not P.missing_trailers(msgs)))
        return self._refs[key]

    def ids(self, repo, pr):
        return self.refs(repo, pr)[0]

    # ── one repo+base train ───────────────────────────────────────────────────────────────────────
    def evaluate(self, repo, pr, open_grp, now):
        base, sha = pr['base']['ref'], pr['head']['sha']
        behind = self.gh('repos/%s/%s/compare/%s...%s' % (self.org, repo, base, sha)).get('behind_by', 0)
        runs = self.gh('repos/%s/%s/commits/%s/check-runs?per_page=100' % (self.org, repo, sha))['check_runs']
        verdict = L.checks_verdict(runs)
        branch = pr['head']['ref']
        grp_ok = True
        if base == 'main' and branch.startswith('release/') and open_grp is not None:
            grp_ok = L.group_of(repo, self.m) <= open_grp
        pr['_pulse_ok'] = self.refs(repo, pr)[1]
        act = L.train_action(pr, behind, verdict, grp_ok)
        fails = []
        if act.startswith('eject:a check failed'):
            fails = self.failing(repo, runs)
            if self.retry_infra(repo, fails):
                act = 'wait'                          # flaky infra: retried once, not a code failure
            elif fails and all(k == 'infra' for _, _, k, _ in fails):
                act = 'eject:infrastructure failed twice (not a code failure) - re-run when GCP is healthy'
                fails = []
        if verdict == 'green':
            self.fixed(repo, pr, runs)
        return act, fails

    def train(self, repo, prs, open_grp, now):
        for base in ('main', 'develop'):
            cands, info = [], {}
            for p in [p for p in prs if p['base']['ref'] == base]:
                try:
                    pr = self.gh('repos/%s/%s/pulls/%s' % (self.org, repo, p['number']))
                    act, fails = self.evaluate(repo, pr, open_grp, now)
                except Exception as e:                # one broken PR never stops the others
                    self.log('train eval error', repo, p['number'], str(e)[:160]); continue
                cands.append((pr['number'], act)); info[pr['number']] = (pr, act, fails)
                if base == 'main' and pr['head']['ref'].startswith('release/'):
                    it = self.st['rel'].setdefault(repo, {'branch': pr['head']['ref'], 'state': 'queued', 'since': now})
                    it['state'] = 'held' if act.startswith(('hold:needs-migration', 'eject:')) else it['state']
            for n, act in L.pick_train(cands):
                pr, _, fails = info[n]
                tag = '%s#%s->%s' % (repo, n, base)
                try:
                    if act == 'merge':
                        r = self._do('merge', 'repos/%s/%s/pulls/%s/merge' % (self.org, repo, n), 'PUT',
                                     {'merge_method': 'merge', 'sha': pr['head']['sha']})
                        if r.get('sha'):
                            self.st['train_shas'] = (self.st['train_shas'] + [r['sha']])[-300:]
                        if base == 'main' and repo in self.st['rel']:
                            self.st['rel'][repo].update(state='inflight', since=now)
                        self.log('merged', tag)
                    elif act == 'update':
                        self._do('update-branch', 'repos/%s/%s/pulls/%s/update-branch' % (self.org, repo, n), 'PUT',
                                 {'expected_head_sha': pr['head']['sha']})
                    elif act.startswith('eject:'):
                        why = act[6:]
                        self._do('unlabel', 'repos/%s/%s/issues/%s/labels/%s' % (self.org, repo, n, LABEL), 'DELETE')
                        self._do('comment', 'repos/%s/%s/issues/%s/comments' % (self.org, repo, n), 'POST',
                                 {'body': 'nb-merge train: removed from the queue (other PRs carry on): %s. Fix it and add `%s` again.' % (why, LABEL)})
                        self.alert('merge train ejected %s: %s' % (tag, why))
                        if fails:
                            self.report(repo, pr, fails, 'Medium')
                except Exception as e:
                    self.log('train action error', tag, act, str(e)[:160])

    def after_main(self, repo, pulse_released):
        since = time.time() - 12 * 3600
        for pr in self.gh('repos/%s/%s/pulls?state=closed&base=main&sort=updated&direction=desc&per_page=15' % (self.org, repo), cache=True):
            if not pr.get('merged_at') or _ts(pr['merged_at']) < since:
                continue
            key = '%s#%s' % (repo, pr['number'])
            if key in self.st['announced'] or key in self.st['red']:
                continue
            runs = self.gh('repos/%s/%s/actions/runs?head_sha=%s&branch=main&event=push&per_page=30'
                           % (self.org, repo, pr['merge_commit_sha']))['workflow_runs']
            if not runs or any(r['status'] != 'completed' for r in runs):
                continue
            if not L.main_green(runs):
                checks = self.gh('repos/%s/%s/commits/%s/check-runs?per_page=100' % (self.org, repo, pr['merge_commit_sha']))['check_runs']
                fails = self.failing(repo, checks)
                if self.retry_infra(repo, fails):
                    continue
                self.st['red'].append(key)
                self.st['rel'].pop(repo, None)             # never blocks the release order
                self.alert('main RED for %s: develop not promoted, Pulse not released' % key)
                self.report(repo, pr, fails, 'High')
                continue
            for d in self.gh('repos/%s/%s/pulls?state=open&base=develop&head=%s:%s' % (self.org, repo, self.org, pr['head']['ref'])):
                self._do('label develop', 'repos/%s/%s/issues/%s/labels' % (self.org, repo, d['number']), 'POST', {'labels': [LABEL]})
            for tid in self.ids(repo, pr):
                pulse_released(tid, 'Released to production: %s/%s PR #%s (%s), main runs green.'
                               % (self.org, repo, pr['number'], pr['merge_commit_sha'][:9]))
            self.st['announced'] = (self.st['announced'] + [key])[-500:]
            self.st['rel'].pop(repo, None)

    def tick(self, pulse_released, only=None):
        now = time.time()
        repos = [only] if only else self.repo_list
        queued = {}
        for repo in repos:
            try:
                self.ensure_labels(repo)
                prs = self.gh('repos/%s/%s/pulls?state=open&sort=created&direction=asc&per_page=50' % (self.org, repo), cache=True)
                queued[repo] = [p for p in prs if p['base']['ref'] in ('main', 'develop') and any(l['name'] == LABEL for l in p['labels'])]
            except Exception as e:
                self.log('train read error', repo, str(e)[:160])
        blocking = {r for r, it in self.st['rel'].items() if L.release_blocking(it, now)}
        grp = L.open_group(blocking, self.m)
        for repo in repos:
            try:
                if queued.get(repo):
                    self.train(repo, queued[repo], grp, now)
                self.after_main(repo, pulse_released)
            except Exception as e:
                self.log('release error', repo, str(e)[:200])
        self.st['retried'] = {k: v for k, v in self.st['retried'].items() if now - v < 7 * 86400}
