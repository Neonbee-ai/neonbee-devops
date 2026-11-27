"""BDD tests for the continuous merge train + gated promotion (pure decisions + fake I/O; no network)."""
import importlib.util, os, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CTL = os.path.join(ROOT, 'runner', 'controller')
spec = importlib.util.spec_from_file_location('nbci_train', os.path.join(CTL, 'nbci_train.py'))
T = importlib.util.module_from_spec(spec); spec.loader.exec_module(T)
OK = {'status': 'COMPLETED', 'conclusion': 'SUCCESS'}


def pr(**k):
    d = {'state': 'OPEN', 'baseRefName': 'develop', 'isDraft': False, 'labels': [], 'statusCheckRollup': [OK],
         'mergeable': 'MERGEABLE', 'mergeStateStatus': 'CLEAN'}
    d.update(k); return d


class Eligibility(unittest.TestCase):
    def v(self, repo='so360-crm-fe', files=(), **k): return T.pr_verdict(repo, pr(**k), list(files))

    def test_given_a_green_clean_pr_then_it_is_eligible(self): self.assertTrue(self.v()[0])
    def test_given_no_label_at_all_then_it_is_still_eligible(self): self.assertTrue(self.v(labels=[])[0])
    def test_given_a_draft_then_skipped(self): self.assertEqual(self.v(isDraft=True), (False, 'draft'))
    def test_given_neonbee_devops_then_skipped(self): self.assertFalse(self.v('neonbee-devops')[0])
    def test_given_base_main_then_skipped(self): self.assertFalse(self.v(baseRefName='main')[0])
    def test_given_label_hold_then_skipped(self): self.assertIn('hold', self.v(labels=[{'name': 'hold'}])[1])
    def test_given_label_do_not_merge_then_skipped(self): self.assertFalse(self.v(labels=[{'name': 'Do-Not-Merge'}])[0])
    def test_given_label_needs_migration_then_skipped(self): self.assertFalse(self.v(labels=[{'name': 'needs-migration'}])[0])
    def test_given_a_migrations_file_then_skipped(self): self.assertIn('migration', self.v(files=['x/database/migrations/a.txt'])[1])
    def test_given_a_sql_file_then_skipped(self): self.assertFalse(self.v(files=['db/seed.SQL'])[0])
    def test_given_a_failed_check_then_skipped(self):
        self.assertEqual(self.v(statusCheckRollup=[OK, {'status': 'COMPLETED', 'conclusion': 'FAILURE'}]), (False, 'checks failed'))
    def test_given_a_pending_check_then_skipped(self):
        self.assertEqual(self.v(statusCheckRollup=[{'status': 'IN_PROGRESS'}])[1], 'checks pending')
    def test_given_no_checks_then_skipped(self): self.assertEqual(self.v(statusCheckRollup=[])[1], 'checks none')
    def test_given_a_failed_status_context_then_checks_failed(self):
        self.assertEqual(T.checks_state([{'__typename': 'StatusContext', 'state': 'FAILURE'}]), 'failed')
    def test_given_a_conflict_then_skipped(self): self.assertIn('not mergeable', self.v(mergeable='CONFLICTING', mergeStateStatus='DIRTY')[1])


class Ordering(unittest.TestCase):
    def test_given_shell_in_the_batch_then_it_goes_first_even_if_newer(self):
        items = [{'repo': 'so360-crm-fe', 'createdAt': '1'}, {'repo': 'so360-shell-fe', 'createdAt': '9'}]
        self.assertEqual(T.order_prs(items)[0]['repo'], 'so360-shell-fe')

    def test_given_no_shell_then_oldest_first(self):
        items = [{'repo': 'b', 'createdAt': '2'}, {'repo': 'a', 'createdAt': '1'}]
        self.assertEqual([i['repo'] for i in T.order_prs(items)], ['a', 'b'])

    def test_given_two_prs_in_one_repo_then_only_one_merges_per_tick(self):
        items = [{'repo': 'a', 'createdAt': '1'}, {'repo': 'a', 'createdAt': '2'}, {'repo': 'b', 'createdAt': '3'}]
        self.assertEqual([(i['repo'], i['createdAt']) for i in T.pick(items, 10)], [('a', '1'), ('b', '3')])

    def test_given_fifteen_repos_then_ten_merge_in_parallel_and_shell_is_among_them(self):
        items = [{'repo': 'r%02d' % i, 'createdAt': '%02d' % i} for i in range(14)] + [{'repo': 'so360-shell-fe', 'createdAt': '99'}]
        got = T.pick(items, 10)
        self.assertEqual(len(got), 10); self.assertEqual(got[0]['repo'], 'so360-shell-fe')


class FakeGH:
    def __init__(self, prs): self.prs, self.merged = prs, []
    def open_prs(self): return [(r, n) for (r, n) in self.prs]
    def view(self, repo, n): return dict(self.prs[(repo, n)], headRefOid='sha%s' % n, createdAt=str(n))
    def files(self, repo, n): return []
    def merge(self, repo, n, sha, method): self.merged.append((repo, n, sha, method)); return True, ''


class TrainTick(unittest.TestCase):
    def test_given_green_prs_across_repos_then_all_merge_with_squash_and_match_head(self):
        gh = FakeGH({('Neonbee-ai/a', 1): pr(), ('Neonbee-ai/b', 2): pr()})
        got = T.train_tick(gh, 10)
        self.assertEqual(len(got), 2); self.assertTrue(all(m[3] == 'squash' and m[2].startswith('sha') for m in gh.merged))

    def test_given_twelve_green_prs_in_twelve_repos_and_cap_ten_then_ten_merge(self):
        gh = FakeGH({('Neonbee-ai/r%d' % i, i): pr() for i in range(12)})
        self.assertEqual(len(T.train_tick(gh, 10)), 10)

    def test_given_a_red_pr_then_it_is_not_merged(self):
        gh = FakeGH({('Neonbee-ai/a', 1): pr(statusCheckRollup=[{'status': 'COMPLETED', 'conclusion': 'FAILURE'}])})
        self.assertEqual(T.train_tick(gh, 10), []); self.assertEqual(gh.merged, [])

    def test_given_dry_run_then_nothing_is_merged(self):
        gh = FakeGH({('Neonbee-ai/a', 1): pr()})
        self.assertEqual(len(T.train_tick(gh, 10, dry=True)), 1); self.assertEqual(gh.merged, [])


class Switches(unittest.TestCase):
    def test_given_no_env_then_train_is_on_and_promotion_off(self): self.assertEqual(T.plan({})[:2], (True, False))
    def test_given_train_off_then_nothing_runs_even_if_promote_on(self): self.assertEqual(T.plan({'MT_TRAIN': 'off', 'MT_PROMOTE': 'on'})[:2], (False, False))
    def test_given_promote_on_then_promotion_runs(self): self.assertEqual(T.plan({'MT_PROMOTE': ' ON '})[:2], (True, True))
    def test_given_an_unknown_promote_value_then_promotion_stays_off(self): self.assertFalse(T.plan({'MT_PROMOTE': 'yes'})[1])


class PromotionGate(unittest.TestCase):
    def test_given_exit_0_for_the_sha_then_passed(self): self.assertEqual(T.aqa_verdict({'exit': 0, 'sha': 'd'}, 'd'), 'passed')
    def test_given_state_passed_without_exit_then_passed(self): self.assertEqual(T.aqa_verdict({'state': 'passed'}, 'd'), 'passed')
    def test_given_exit_4_then_not_tested(self): self.assertEqual(T.aqa_verdict({'exit': 4}, 'd'), 'not_tested')
    def test_given_state_not_tested_with_exit_0_then_not_tested(self): self.assertEqual(T.aqa_verdict({'exit': 0, 'state': 'not_tested'}, 'd'), 'not_tested')
    def test_given_exit_1_then_failed(self): self.assertEqual(T.aqa_verdict({'exit': 1}, 'd'), 'failed')
    def test_given_no_result_then_missing(self): self.assertEqual(T.aqa_verdict(None, 'd'), 'missing')
    def test_given_a_result_for_another_sha_then_stale(self): self.assertEqual(T.aqa_verdict({'exit': 0, 'sha': 'old'}, 'd'), 'stale')
    def test_given_only_passed_then_the_gate_opens(self):
        self.assertTrue(T.promote_gate('passed')[0])
        for v in ('not_tested', 'failed', 'missing', 'stale'):
            self.assertFalse(T.promote_gate(v)[0])


class FakeStore:
    def __init__(self, res): self.res, self.alerts, self.st = res, [], {'promoted': {}, 'alerted': {}}
    def aqa_result(self, env, sha): return self.res.get((env, sha))
    def alert(self, t): self.alerts.append(t)
    def load(self): return self.st
    def save(self, st): self.st = st


class FakePromoGH:
    def __init__(self, ahead=1, files=(), head='D1', pr_open=True, green=True):
        self.ahead, self.files, self.head, self.pr_open, self.green = ahead, list(files), head, pr_open, green
        self.merged, self.opened = [], []
    def repos(self): return ['Neonbee-ai/so360-x-fe']
    def develop_state(self, repo): return self.head, self.ahead, self.files
    def promo_pr(self, repo): return 7 if self.pr_open else None
    def open_promo_pr(self, repo, body): self.opened.append(repo)
    def view(self, repo, n):
        return pr(headRefOid=self.head, statusCheckRollup=[OK] if self.green else [{'status': 'IN_PROGRESS'}], mergeCommit={'oid': 'M1'})
    def merge(self, repo, n, sha, method): self.merged.append((n, sha, method)); return True, ''


class PromotionTick(unittest.TestCase):
    def test_given_dev_passed_for_the_exact_sha_then_the_main_pr_merges_with_match_head(self):
        gh, s = FakePromoGH(), FakeStore({('dev', 'D1'): {'exit': 0}})
        T.promote_tick(gh, s, now=100)
        self.assertEqual(gh.merged, [(7, 'D1', 'merge')]); self.assertEqual(s.st['promoted']['Neonbee-ai/so360-x-fe']['main_sha'], 'M1')

    def test_given_dev_not_tested_then_no_action(self):
        gh = FakePromoGH(); T.promote_tick(gh, FakeStore({('dev', 'D1'): {'exit': 4}}), now=1)
        self.assertEqual((gh.merged, gh.opened), ([], []))

    def test_given_dev_failed_then_no_action(self):
        gh = FakePromoGH(); T.promote_tick(gh, FakeStore({('dev', 'D1'): {'exit': 1}}), now=1); self.assertEqual(gh.merged, [])

    def test_given_no_dev_result_then_no_action(self):
        gh = FakePromoGH(); T.promote_tick(gh, FakeStore({}), now=1); self.assertEqual((gh.merged, gh.opened), ([], []))

    def test_given_a_dev_pass_of_an_older_sha_then_no_action(self):
        gh = FakePromoGH(head='D2'); T.promote_tick(gh, FakeStore({('dev', 'D1'): {'exit': 0}}), now=1); self.assertEqual(gh.merged, [])

    def test_given_a_dev_pass_but_no_open_main_pr_then_the_pr_is_opened_not_merged(self):
        gh = FakePromoGH(pr_open=False); T.promote_tick(gh, FakeStore({('dev', 'D1'): {'exit': 0}}), now=1)
        self.assertEqual((gh.opened, gh.merged), (['Neonbee-ai/so360-x-fe'], []))

    def test_given_main_pr_checks_pending_then_it_waits(self):
        gh = FakePromoGH(green=False); T.promote_tick(gh, FakeStore({('dev', 'D1'): {'exit': 0}}), now=1); self.assertEqual(gh.merged, [])

    def test_given_develop_carries_a_migration_then_it_is_not_promoted(self):
        gh = FakePromoGH(files=['database/migrations/x.sql']); T.promote_tick(gh, FakeStore({('dev', 'D1'): {'exit': 0}}), now=1); self.assertEqual(gh.merged, [])

    def test_given_develop_equals_main_then_no_action(self):
        gh = FakePromoGH(ahead=0); T.promote_tick(gh, FakeStore({('dev', 'D1'): {'exit': 0}}), now=1); self.assertEqual(gh.merged, [])

    def test_given_dry_run_then_nothing_merges_or_saves(self):
        gh, s = FakePromoGH(), FakeStore({('dev', 'D1'): {'exit': 0}}); T.promote_tick(gh, s, now=1, dry=True)
        self.assertEqual((gh.merged, s.st['promoted']), ([], {}))


class LiveCheck(unittest.TestCase):
    rec = {'merged': 0}
    def test_given_live_passed_then_verified(self): self.assertEqual(T.live_check(self.rec, 'passed', 10), 'verified')
    def test_given_live_failed_then_alert(self): self.assertEqual(T.live_check(self.rec, 'failed', 10), 'alert')
    def test_given_live_not_tested_then_alert(self): self.assertEqual(T.live_check(self.rec, 'not_tested', 10), 'alert')
    def test_given_no_live_result_yet_then_wait(self): self.assertEqual(T.live_check(self.rec, 'missing', 600), 'wait')
    def test_given_no_live_result_after_90_min_then_alert(self): self.assertEqual(T.live_check(self.rec, 'missing', 6000), 'alert')

    def test_given_a_promoted_repo_whose_live_aqa_failed_then_one_alert_is_raised_once(self):
        s = FakeStore({('prod', 'M1'): {'exit': 1}}); s.st['promoted']['Neonbee-ai/r'] = {'dev_sha': 'D', 'main_sha': 'M1', 'merged': 0}
        gh = FakePromoGH(ahead=0)
        T.promote_tick(gh, s, now=10); T.promote_tick(gh, s, now=20)
        self.assertEqual(len(s.alerts), 1)

    def test_given_a_promoted_repo_whose_live_aqa_passed_then_no_alert(self):
        s = FakeStore({('prod', 'M1'): {'exit': 0}}); s.st['promoted']['Neonbee-ai/r'] = {'dev_sha': 'D', 'main_sha': 'M1', 'merged': 0}
        T.promote_tick(FakePromoGH(ahead=0), s, now=10); self.assertEqual(s.alerts, [])


class Contract(unittest.TestCase):
    b = open(os.path.join(CTL, 'bootstrap.sh')).read()
    src = open(os.path.join(CTL, 'nbci_train.py')).read()

    def test_given_boot_then_merge_train_defaults_develop_on_and_promote_off(self):
        self.assertIn('MT_TRAIN=$(attr nbci-merge-train || echo develop)', self.b)
        self.assertIn('MT_PROMOTE=$(attr nbci-promote-main || echo off)', self.b)

    def test_given_boot_then_the_timer_is_enabled_when_the_train_is_on(self):
        self.assertIn('systemctl enable --now merge-train.timer', self.b)

    def test_given_the_train_then_never_admin_and_always_match_head(self):
        self.assertNotIn("'--admin'", self.src); self.assertIn('--match-head-commit', self.src)

    def test_given_the_runbook_then_it_has_the_turn_on_command(self):
        rb = open(os.path.join(CTL, 'RUNBOOK.md')).read()
        self.assertIn('--metadata nbci-promote-main=on', rb)


if __name__ == '__main__':
    unittest.main()
