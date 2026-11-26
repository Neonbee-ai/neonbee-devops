import os
"""BDD tests for the nb-ci controller logic, config and workflow contracts (no network)."""
import json, os, re, sys, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'runner', 'controller'))
import nbci_logic as L  # noqa: E402

CFG = json.load(open(os.path.join(ROOT, 'ci', 'runner-labels.json')))
MAN = json.load(open(os.path.join(ROOT, 'ci', 'release-manifest.json')))
CLS = CFG['classes']; BY = {c['name']: c for c in CLS}; PRICES = CFG['prices_usd']
NOW = 1_800_000_000


def wf(name):
    with open(os.path.join(ROOT, '.github', 'workflows', name)) as f:
        return f.read()


class Classes(unittest.TestCase):
    def test_given_a_plain_auto_job_then_it_gets_a_spot_gate_vm(self):
        c = L.class_for(['self-hosted', 'nb-auto'], CLS)
        self.assertEqual((c['name'], c['spot']), ('gate', True))

    def test_given_a_deploy_job_then_it_gets_a_standard_non_spot_vm(self):
        c = L.class_for(['self-hosted', 'nb-deploy'], CLS)
        self.assertEqual((c['name'], c['spot']), ('deploy', False))

    def test_given_a_route_job_then_it_runs_on_the_controller_not_a_vm(self):
        self.assertEqual(L.class_for(['self-hosted', 'nb-ctl'], CLS)['host'], 'controller')

    def test_given_size_labels_then_vms_are_sized_2_8_8(self):
        self.assertEqual(L.class_for(['self-hosted', 'nb-auto', 'nb-size-l'], CLS)['vcpu'], 8)
        self.assertEqual(L.class_for(['self-hosted', 'nb-auto', 'nb-size-s'], CLS)['vcpu'], 2)
        self.assertEqual(L.class_for(['self-hosted', 'nb-auto'], CLS)['vcpu'], 8)

    def test_given_a_macos_job_then_no_class_serves_it(self):
        self.assertIsNone(L.class_for(['self-hosted', 'aqa-coord'], CLS))

    def test_no_spot_class_carries_the_deploy_label(self):
        for c in CLS:
            if c.get('spot'):
                self.assertNotIn('nb-deploy', c['labels'])

    def test_every_auto_lane_in_the_workflows_has_a_class_and_no_legacy_lane_does(self):
        auto = {'nb-auto', 'nb-deploy', 'nb-ctl'}
        for f in os.listdir(os.path.join(ROOT, '.github', 'workflows')):
            for lane in re.findall(r"'(\[\"self-hosted\"[^']*\])'", wf(f)):
                with self.subTest(f=f, lane=lane):
                    labels = json.loads(lane)
                    if auto & set(labels):
                        self.assertIsNotNone(L.class_for(labels, CLS))
                    else:
                        self.assertIsNone(L.class_for(labels, CLS))


class Minis(unittest.TestCase):
    def test_no_mac_mini_carries_a_ci_label_in_the_end_state(self):
        ci = set(CFG['ci_labels']) | {'nb-deploy', 'nb-auto', 'nb-ctl'}
        for name, labels in CFG['minis'].items():
            with self.subTest(mini=name):
                self.assertFalse(ci & set(labels))

    def test_exactly_one_fallback_runner_and_it_is_mini_3(self):
        self.assertEqual(CFG['fallback']['runner'], 'macmini-preview-3')
        self.assertEqual(set(CFG['fallback']['labels']), {'nb-auto', 'nb-deploy', 'nb-ctl', 'nb-size-l', 'nb-size-s'})


class Cost(unittest.TestCase):
    def test_a_spot_gate_vm_costs_its_real_hourly_price(self):
        # 4 x 0.01309 + 16 x 0.001754 + IP 0.0025 + 40 GB x 0.10 / 730
        self.assertAlmostEqual(L.hourly_usd(BY['gate'], PRICES), 0.1132 + 0.03032 + 0.0025 + 0.005479, places=4)   # c2d-highcpu-8 spot, 40 GB pd-balanced

    def test_a_deploy_vm_is_dearer_than_spot(self):
        self.assertGreater(L.hourly_usd(BY['deploy'], PRICES), L.hourly_usd(dict(BY['deploy'], spot=True), PRICES))

    def test_spend_levels(self):
        self.assertEqual([L.spend_state(x, 2) for x in (0.5, 1.0, 1.7, 2.0)], ['ok', 'warn50', 'warn80', 'capped'])

    def test_given_the_cap_is_hit_then_only_deploy_vms_start(self):
        self.assertTrue(L.may_start(BY['deploy'], 'capped', 8, 4))
        self.assertFalse(L.may_start(BY['gate'], 'capped', 32, 4))

    def test_given_little_quota_left_then_gate_leaves_the_deploy_reserve(self):
        self.assertFalse(L.may_start(BY['gate'], 'ok', 6, 4))
        self.assertTrue(L.may_start(BY['deploy'], 'ok', 4, 4))

    def test_autoqa_gets_only_what_ci_does_not_need(self):
        self.assertEqual(L.autoqa_allowance(20, 4, 8), 8)
        self.assertEqual(L.autoqa_allowance(6, 4, 8), 0)


class Plan(unittest.TestCase):
    def job(self, labels):
        return {'labels': labels}

    def test_one_vm_per_waiting_job_deploy_first(self):
        out = L.plan([self.job(['self-hosted', 'nb-auto'])] * 2 + [self.job(['self-hosted', 'nb-deploy'])], {}, CLS)
        self.assertEqual([c['name'] for c in out], ['deploy', 'gate', 'gate'])

    def test_vms_already_booting_count_as_capacity(self):
        self.assertEqual(len(L.plan([self.job(['self-hosted', 'nb-auto'])] * 3, {'gate': 2}, CLS)), 1)

    def test_route_jobs_never_buy_a_vm(self):
        self.assertEqual(L.plan([self.job(['self-hosted', 'nb-ctl'])], {}, CLS), [])


class Delete(unittest.TestCase):
    def vm(self, status='RUNNING', age=60):
        return {'status': status, 'created': NOW - age}

    def test_job_completed_deletes_at_once(self):
        self.assertEqual(L.vm_verdict(self.vm(), None, True, NOW), 'job completed')

    def test_no_job_five_minutes_after_boot_deletes(self):
        self.assertIn('no job', L.vm_verdict(self.vm(age=301), {'busy': False, 'status': 'online'}, False, NOW))

    def test_busy_runner_is_never_deleted_even_after_five_minutes(self):
        self.assertIsNone(L.vm_verdict(self.vm(age=3000), {'busy': True}, False, NOW))

    def test_a_stopped_vm_is_deleted_because_its_disk_still_bills(self):
        self.assertIn('disk still bills', L.vm_verdict(self.vm('TERMINATED'), None, False, NOW))

    def test_a_booting_vm_is_kept(self):
        self.assertIsNone(L.vm_verdict(self.vm(age=60), None, False, NOW))

    def test_gce_timestamp_offset_is_honoured(self):
        # given the same instant as GCE (-08:00) and GitHub (Z) print it, then both give the same epoch
        self.assertEqual(L.ts('2026-11-26T11:41:13.922-08:00'), L.ts('2026-11-26T19:41:13Z'))
        self.assertEqual(L.ts('2026-11-26T21:11:13+01:30'), L.ts('2026-11-26T19:41:13Z'))

    def test_vm_created_seconds_ago_in_gce_local_time_is_not_deleted(self):
        # given a VM GCE reports as created 30 s ago in -08:00, when judged with no runner yet, then it is kept
        now = L.ts('2026-11-26T19:41:43Z')
        vm = {'status': 'RUNNING', 'created': L.ts('2026-11-26T11:41:13.922-08:00')}
        self.assertIsNone(L.vm_verdict(vm, None, False, now, L.NO_JOB_STARTUP_S))

    def test_startup_boot_gets_a_longer_no_job_grace(self):
        # given a stock-ubuntu VM still running provision.sh 6 min after creation (no runner yet)
        # when the verdict uses the startup grace, then it is kept; past the grace it is deleted
        self.assertIsNone(L.vm_verdict(self.vm(age=360), None, False, NOW, L.NO_JOB_STARTUP_S))
        self.assertIn('no job', L.vm_verdict(self.vm(age=901), None, False, NOW, L.NO_JOB_STARTUP_S))


class Fallback(unittest.TestCase):
    def test_billing_and_quota_fail_at_once(self):
        self.assertEqual(L.gcp_failing([('z', L.classify_gce_error('BILLING_DISABLED'))]), 'billing')
        self.assertEqual(L.gcp_failing([('z', L.classify_gce_error('QUOTA_EXCEEDED'))]), 'quota')

    def test_one_zone_stockout_is_not_failing_two_are(self):
        e = L.classify_gce_error('ZONE_RESOURCE_POOL_EXHAUSTED')
        self.assertIsNone(L.gcp_failing([('a', e)]))
        self.assertEqual(L.gcp_failing([('a', e), ('b', e)]), 'stockout')

    def test_fallback_on_after_two_minutes_of_waiting_off_after_a_vm_starts(self):
        self.assertFalse(L.fallback_next(False, 30, 'quota', False))
        self.assertTrue(L.fallback_next(False, 120, 'quota', False))
        self.assertFalse(L.fallback_next(True, 999, None, True))
        self.assertTrue(L.fallback_next(True, 0, 'api', False))


class Train(unittest.TestCase):
    pr = {'title': 'fix(crm): import', '_pulse_ok': True, 'draft': False, 'mergeable': True, 'mergeable_state': 'clean', 'labels': []}

    def runs(self, *spec):
        return [{'id': i, 'name': n, 'status': st, 'conclusion': c} for i, (n, st, c) in enumerate(spec)]

    def test_title_without_pulse_id_is_ejected(self):
        self.assertTrue(L.train_action(dict(self.pr, _pulse_ok=False), 0, 'green').startswith('eject:'))

    def test_needs_migration_holds_until_migration_applied(self):
        held = dict(self.pr, labels=[{'name': 'needs-migration'}])
        self.assertTrue(L.train_action(held, 0, 'green').startswith('hold:needs-migration'))
        ok = dict(self.pr, labels=[{'name': 'needs-migration'}, {'name': 'migration-applied'}])
        self.assertEqual(L.train_action(ok, 0, 'green'), 'merge')

    def test_release_order_holds_later_groups(self):
        self.assertTrue(L.train_action(self.pr, 0, 'green', group_open=False).startswith('hold:release order'))

    def test_behind_base_is_updated_and_retested(self):
        self.assertEqual(L.train_action(self.pr, 2, 'green'), 'update')

    def test_conflict_and_red_are_ejected_green_merges(self):
        self.assertIn('conflict', L.train_action(dict(self.pr, mergeable=False), 0, 'green'))
        self.assertTrue(L.train_action(self.pr, 0, 'red').startswith('eject:'))
        self.assertEqual(L.train_action(self.pr, 0, 'green'), 'merge')
        self.assertEqual(L.train_action(self.pr, 0, 'pending'), 'wait')

    def test_checks_latest_rerun_counts_and_gate_must_exist(self):
        self.assertEqual(L.checks_verdict(self.runs(('gate / Gate', 'completed', 'failure'), ('gate / Gate', 'completed', 'success'))), 'green')
        self.assertEqual(L.checks_verdict(self.runs(('lint', 'completed', 'success'))), 'no-gate')

    def test_manifest_order_core_backend_frontend_shell(self):
        g = lambda r: L.group_of(r, MAN)  # noqa: E731
        self.assertLess(g('so360-core'), g('so360-crm-be'))
        self.assertLess(g('so360-crm-be'), g('so360-accounting-fe'))
        self.assertLess(g('so360-accounting-fe'), g('so360-shell-fe'))
        self.assertEqual(L.open_group({'so360-shell-fe', 'so360-crm-be'}, MAN), g('so360-crm-be'))

    def test_pulse_ids_and_main_green(self):
        self.assertTrue(L.main_green([{'status': 'completed', 'conclusion': 'success'}]))
        self.assertFalse(L.main_green([{'status': 'completed', 'conclusion': 'failure'}]))
        self.assertFalse(L.main_green([]))


class Bypass(unittest.TestCase):
    def push(self, who, ref='refs/heads/main', sha='abc123'):
        return {'ref': ref, 'sender': {'login': who}, 'after': sha}

    def test_a_human_push_to_main_is_reported(self):
        self.assertIn('outside the merge train', L.bypass(self.push('dev1'), set(), {'nb-ci[bot]'}, set()))

    def test_the_train_merge_and_the_bot_are_not_reported(self):
        self.assertIsNone(L.bypass(self.push('dev1', sha='s1'), {'s1'}, {'nb-ci[bot]'}, set()))
        self.assertIsNone(L.bypass(self.push('nb-ci[bot]'), set(), {'nb-ci[bot]'}, set()))

    def test_feature_branches_are_ignored(self):
        self.assertIsNone(L.bypass(self.push('dev1', ref='refs/heads/feat/x'), set(), set(), set()))


class Stats(unittest.TestCase):
    def test_percentiles(self):
        self.assertEqual(L.percentile([10, 20, 30, 40, 100], 50), 30)
        self.assertEqual(L.percentile([10, 20, 30, 40, 100], 95), 100)
        self.assertEqual(L.percentile([], 50), 0)


class Workflows(unittest.TestCase):
    def test_every_notify_job_is_non_gating(self):
        for f in os.listdir(os.path.join(ROOT, '.github', 'workflows')):
            for m in re.finditer(r'\n    name: Notify Teams\n((?:    .*\n)*?)    steps:', wf(f)):
                with self.subTest(f=f):
                    self.assertIn('continue-on-error: true', m.group(1))

    def test_deploy_job_runs_on_the_non_spot_lane_in_auto_mode(self):
        t = wf('neonbee-deploy-prod.yml')
        self.assertIn("""deploy_runs_on: ${{ vars.NB_CI_MODE == 'auto' && '["self-hosted","nb-deploy"]' || steps.r.outputs.runs_on }}""", t)
        bd = t[t.index('  build-deploy:\n'):]
        self.assertIn('runs-on: ${{ fromJSON(needs.route.outputs.deploy_runs_on) }}', bd[:bd.index('\n    steps:')])

    def test_old_ha_vm_canary_is_gone(self):
        self.assertNotIn('NB_DEPLOY_CANARY', wf('neonbee-deploy-prod.yml'))

    def test_s3_release_canaries_one_node_then_the_rest_and_reverts_on_failure(self):
        t = wf('neonbee-deploy-s3-release.yml')
        c = t[t.index('if [ "$CANARY" = "true" ]'):]
        self.assertLess(c.index('ssm_run "$FIRST"'), c.index('ssm_run $REST'))
        self.assertIn("vars.NB_CI_MODE == 'auto'", t)
        self.assertLess(t.index('name: Revert to previous release'), t.index('name: Verify served artifact is this release'))

    def test_given_an_auto_repo_pr_then_the_gate_checks_every_commit_for_a_pulse_trailer(self):
        t = wf('neonbee-pr-gate.yml')
        step = t[t.index('- name: Every commit has a Pulse trailer'):]
        step = step[:step.index('PY\n', step.index("<<'PY'") + 6)]
        self.assertIn("vars.NB_CI_MODE == 'auto'", step.splitlines()[1])
        self.assertIn(nb_pulse.TRAILER_RE.pattern, step)          # parity with nb_pulse.py
        self.assertIn('get_task', step)
        self.assertIn('sys.exit(1 if bad or missing else 0)', step)
        self.assertNotIn('PULSE-[0-9a-f]{8}', t)                   # the PR-title rule is gone


class Image(unittest.TestCase):
    def test_image_runs_one_job_then_powers_off_and_blocks_metadata_for_jobs(self):
        t = open(os.path.join(ROOT, 'runner', 'image', 'provision.sh')).read()
        self.assertIn('--jitconfig', t)
        self.assertIn('--uid-owner runner -d 169.254.169.254 -j REJECT', t)
        self.assertIn('sleep 300', t)
        self.assertNotRegex(t, r'(?i)(token|secret|password)=')


class Controller(unittest.TestCase):
    src = open(os.path.join(ROOT, 'runner', 'controller', 'nbci.py')).read()

    def test_every_vm_has_a_90_minute_delete_limit_and_no_service_account(self):
        self.assertIn("'instanceTerminationAction': 'DELETE', 'maxRunDuration': {'seconds': MAX_RUN_S}", self.src)
        self.assertIn("MAX_RUN_S = int(E('MAX_RUN_S', '5400'))", self.src)
        self.assertIn("'serviceAccounts': L.service_accounts(c, PROJECT)", self.src)

    def test_dry_run_is_the_default(self):
        self.assertIn("DRY = E('DRY_RUN', '1') == '1'", self.src)

    def test_controller_local_runners_are_never_deregistered(self):
        self.assertIn("not r['name'].startswith('nbci-ctl-')", self.src)


import nb_pulse  # noqa: E402
import nb_release  # noqa: E402


# ── fakes ────────────────────────────────────────────────────────────────────────────────────────
class FakeGH:
    """Routes GitHub API paths to canned answers; records writes. A path mapped to an Exception raises."""
    def __init__(self, routes):
        self.routes, self.writes = routes, []

    def __call__(self, path, method='GET', body=None, cache=False):
        if method != 'GET':
            self.writes.append((method, path)); return {'sha': 'merged-' + path.split('/')[-2]} if path.endswith('/merge') else {}
        for k, v in self.routes.items():
            if path.startswith(k):
                if isinstance(v, Exception):
                    raise v
                return v
        raise RuntimeError('GET %s -> 404 ' % path)

    def token(self):
        return 't'


class FakeMCP:
    def __init__(self, fail=False):
        self.calls, self.fail, self.n = [], fail, 0

    def call(self, tool, args):
        if self.fail:
            raise RuntimeError('connection refused')
        self.calls.append((tool, args))
        if tool == 'create_task':
            self.n += 1; return 'Created task %08x "Bug"' % (0xb0000000 + self.n)
        if tool == 'list_test_cases':
            return '1. [aaaa1111] Lead import works [check:gate / Gate] (pending)\n2. [bbbb2222] Unrelated case (pending)'
        return 'ok'


def pr(n, title='fix: x', base='main', head='feat/x', labels=('nb-merge',), pulse=('1a2b3c4d',)):
    return {'number': n, 'title': title, 'body': '\n'.join('Pulse: ' + i for i in pulse), 'draft': False, 'mergeable': True, 'mergeable_state': 'clean',
            'labels': [{'name': l} for l in labels], 'base': {'ref': base}, 'head': {'ref': head, 'sha': 'sha%d' % n},
            'html_url': 'https://github.com/x/pull/%d' % n}


def checks(conclusion, cid=1, name='gate / Gate'):
    return {'check_runs': [{'id': cid, 'name': name, 'status': 'completed', 'conclusion': conclusion,
                            'app': {'slug': 'other'}, 'html_url': 'https://run/%d' % cid}]}


def release(routes, pq=None, repos=('r1',)):
    gh = FakeGH(routes)
    st = {}
    rel = nb_release.Release(gh, 'org', MAN, lambda *a: None, lambda t: None, st, pq or nb_pulse.PulseQueue({}, FakeMCP(), lambda *a: None), False, list(repos))
    rel.st['labelled'] = list(repos)
    return gh, rel


class IsolationModes(unittest.TestCase):
    def test_a_repo_variable_overrides_the_org_variable(self):
        self.assertEqual(L.repo_mode('auto', 'legacy'), 'auto')
        self.assertEqual(L.repo_mode('legacy', 'auto'), 'legacy')
        self.assertEqual(L.repo_mode('', 'auto'), 'auto')
        self.assertEqual(L.repo_mode(None, None), 'legacy')

    def test_workflows_switch_lanes_only_when_the_resolved_variable_is_auto(self):
        # vars.NB_CI_MODE resolves repo > org in GitHub itself; the route step uses that value
        for f in ('neonbee-deploy-prod.yml', 'neonbee-pr-gate.yml', 'neonbee-deploy-vite-mfe.yml', 'neonbee-deploy-backend.yml'):
            t = wf(f)
            with self.subTest(f=f):
                self.assertIn('CI_MODE: ${{ vars.NB_CI_MODE }}', t)
                self.assertIn('if [ "${CI_MODE:-}" = auto ]; then lane=\'["self-hosted","nb-auto"]\'', t)


class IsolationLegacy(unittest.TestCase):
    def test_no_vm_class_can_serve_a_legacy_lane(self):
        for lane in (['self-hosted', 'runner-vm'], ['self-hosted', 'nb-route'], ['self-hosted', 'lane-burst'], ['self-hosted', 'nb-pool']):
            self.assertIsNone(L.class_for(lane, CLS))

    def test_fallback_only_adds_and_removes_its_own_labels(self):
        legacy = ['runner-vm', 'nb-route', 'contabo']
        on = L.fallback_target(legacy, CFG['fallback']['labels'], True)
        self.assertTrue(set(legacy) <= set(on))
        self.assertEqual(L.fallback_target(on, CFG['fallback']['labels'], False), sorted(legacy))

    def test_legacy_route_and_notify_keep_nb_route_when_the_controller_is_gone(self):
        t = wf('neonbee-deploy-prod.yml')
        self.assertIn("|| fromJSON('[\"self-hosted\",\"nb-route\"]') }}", t)
        self.assertIn("vars.NB_CI_MODE == 'auto' && '[\"self-hosted\",\"nb-deploy\"]' || steps.r.outputs.runs_on", t)


class IsolationTrain(unittest.TestCase):
    def test_a_failing_pr_is_ejected_and_the_next_pr_still_merges(self):
        self.assertEqual(L.pick_train([(1, 'eject:a check failed'), (2, 'merge')]), [(1, 'eject:a check failed'), (2, 'merge')])

    def test_a_waiting_or_held_pr_blocks_nobody(self):
        self.assertEqual(L.pick_train([(1, 'wait'), (2, 'hold:needs-migration'), (3, 'merge')]), [(3, 'merge')])

    def test_only_one_behind_pr_is_updated_at_a_time(self):
        self.assertEqual(L.pick_train([(1, 'update'), (2, 'update')]), [(1, 'update')])

    def test_in_one_repo_a_red_pr_is_ejected_with_a_comment_and_the_green_one_merges(self):
        routes = {'repos/org/r1/pulls?state=open': [pr(1), pr(2)], 'repos/org/r1/pulls/1': pr(1), 'repos/org/r1/pulls/2': pr(2),
                  'repos/org/r1/compare/': {'behind_by': 0},
                  'repos/org/r1/commits/sha1/check-runs': checks('failure'), 'repos/org/r1/commits/sha2/check-runs': checks('success'),
                  'repos/org/r1/pulls?state=closed': []}
        gh, rel = release(routes)
        rel.tick(lambda *a: None)
        self.assertIn(('DELETE', 'repos/org/r1/issues/1/labels/nb-merge'), gh.writes)
        self.assertIn(('POST', 'repos/org/r1/issues/1/comments'), gh.writes)
        self.assertIn(('PUT', 'repos/org/r1/pulls/2/merge'), gh.writes)

    def test_an_error_in_one_repo_never_stops_another_repo(self):
        routes = {'repos/org/bad/': RuntimeError('boom 500 '), 'repos/org/r1/pulls?state=open': [pr(2)], 'repos/org/r1/pulls/2': pr(2),
                  'repos/org/r1/compare/': {'behind_by': 0}, 'repos/org/r1/commits/sha2/check-runs': checks('success'),
                  'repos/org/r1/pulls?state=closed': []}
        gh, rel = release(routes, repos=('bad', 'r1'))
        rel.tick(lambda *a: None)
        self.assertIn(('PUT', 'repos/org/r1/pulls/2/merge'), gh.writes)

    def test_release_order_never_blocks_after_two_hours_or_when_held(self):
        it = {'branch': 'release/2026-11-26b', 'state': 'queued', 'since': NOW}
        self.assertTrue(L.release_blocking(it, NOW + 60))
        self.assertFalse(L.release_blocking(it, NOW + 7300))
        self.assertFalse(L.release_blocking(dict(it, state='held'), NOW + 60))
        self.assertFalse(L.release_blocking(dict(it, branch='feat/x'), NOW + 60))


class InfraRetry(unittest.TestCase):
    def test_runner_lost_and_preemption_are_infra_test_failures_are_code(self):
        self.assertEqual(L.classify_log('##[error]The self-hosted runner lost communication with the server.'), 'infra')
        self.assertEqual(L.classify_log('', 'cancelled', runner_gone=True), 'infra')
        self.assertEqual(L.classify_log('Tests: 3 failed, 10 passed\nECONNRESET'), 'code')

    def test_infra_is_retried_exactly_once(self):
        self.assertTrue(L.should_retry('infra', 1))
        self.assertFalse(L.should_retry('infra', 2))
        self.assertFalse(L.should_retry('code', 1))

    def _routes(self, attempt, log):
        c = checks('failure'); c['check_runs'][0]['app'] = {'slug': 'github-actions'}
        return {'repos/org/r1/pulls?state=open': [pr(1)], 'repos/org/r1/pulls/1': pr(1), 'repos/org/r1/compare/': {'behind_by': 0},
                'repos/org/r1/commits/sha1/check-runs': c, 'repos/org/r1/pulls?state=closed': [],
                'repos/org/r1/actions/jobs/1': {'run_id': 77, 'run_attempt': attempt, 'runner_name': 'nbci-gate-1', 'html_url': 'u'}}, log

    def test_a_flaky_infra_failure_is_rerun_not_ejected_and_not_reported(self):
        routes, log = self._routes(1, 'The runner has received a shutdown signal')
        mcp = FakeMCP(); pq = nb_pulse.PulseQueue({}, mcp, lambda *a: None)
        nb_release.job_log = lambda *a, **k: log
        gh, rel = release(routes, pq)
        rel.tick(lambda *a: None)
        self.assertIn(('POST', 'repos/org/r1/actions/runs/77/rerun-failed-jobs'), gh.writes)
        self.assertNotIn(('DELETE', 'repos/org/r1/issues/1/labels/nb-merge'), gh.writes)
        self.assertEqual(pq.st['q'], [])

    def test_infra_failing_twice_is_ejected_but_never_reported_as_a_code_bug(self):
        routes, log = self._routes(2, 'Connection reset by peer')
        pq = nb_pulse.PulseQueue({}, FakeMCP(), lambda *a: None)
        nb_release.job_log = lambda *a, **k: log
        gh, rel = release(routes, pq)
        rel.tick(lambda *a: None)
        self.assertIn(('DELETE', 'repos/org/r1/issues/1/labels/nb-merge'), gh.writes)
        self.assertEqual(pq.st['q'], [])


class PulseOnFailure(unittest.TestCase):
    def test_a_failure_comments_moves_to_fixing_files_one_bug_and_fails_mapped_test_cases(self):
        mcp = FakeMCP(); pq = nb_pulse.PulseQueue({}, mcp, lambda *a: None)
        pq.report_failure('1a2b3c4d', 'gate / Gate', 'r1', 5, 'https://run/1', 'FAIL src/a.spec.ts', 'Medium')
        pq.drain()
        tools = [t for t, _ in mcp.calls]
        self.assertEqual(tools[:2], ['add_comment', 'update_task_status'])
        self.assertEqual(mcp.calls[1][1]['status'], 'fixing')
        self.assertEqual(tools.count('create_task'), 1)
        self.assertEqual(mcp.calls[tools.index('create_task')][1]['parent_task_id'], '1a2b3c4d')
        self.assertIn(('update_test_case', {'task_id': '1a2b3c4d', 'test_case_id': 'aaaa1111', 'status': 'fail'}), mcp.calls)
        self.assertNotIn('bbbb2222', json.dumps(mcp.calls))

    def test_a_repeated_failure_comments_on_the_open_bug_instead_of_a_new_one(self):
        mcp = FakeMCP(); pq = nb_pulse.PulseQueue({}, mcp, lambda *a: None)
        for _ in range(2):
            pq.report_failure('1a2b3c4d', 'gate / Gate', 'r1', 5, 'u', 'x')
        pq.drain(budget=50)
        self.assertEqual([t for t, _ in mcp.calls].count('create_task'), 1)
        self.assertTrue(any(t == 'add_comment' and 'Failed again' in a['message'] for t, a in mcp.calls))

    def test_the_fix_closes_the_bug_and_moves_the_task_to_reverify(self):
        mcp = FakeMCP(); pq = nb_pulse.PulseQueue({}, mcp, lambda *a: None)
        pq.report_failure('1a2b3c4d', 'gate / Gate', 'r1', 5, 'u', 'x'); pq.drain()
        bug = pq.st['bugs']['1a2b3c4d|gate / Gate']['id']
        pq.report_fixed('1a2b3c4d', 'gate / Gate', 'r1', 5, 'u'); pq.drain()
        self.assertIn(('update_task_status', {'task_id': bug, 'status': 'done'}), mcp.calls)
        self.assertIn(('update_task_status', {'task_id': '1a2b3c4d', 'status': 'reverify'}), mcp.calls)
        self.assertFalse(nb_pulse.open_bug(pq.st, '1a2b3c4d', 'gate / Gate'))

    def test_pulse_down_queues_and_retries_without_raising(self):
        pq = nb_pulse.PulseQueue({}, FakeMCP(fail=True), lambda *a: None)
        pq.report_failure('1a2b3c4d', 'gate / Gate', 'r1', 5, 'u', 'x')
        pq.drain()                                    # must not raise
        self.assertEqual(len(pq.st['q']), 4)
        self.assertTrue(all(op['next_at'] > 0 and op['tries'] == 1 for op in pq.st['q'][:1]))

    def test_the_train_reports_a_code_failure_to_every_pulse_id(self):
        routes = {'repos/org/r1/pulls?state=open': [pr(1, pulse=('1a2b3c4d', 'deadbeef'))],
                  'repos/org/r1/pulls/1': pr(1, pulse=('1a2b3c4d', 'deadbeef')), 'repos/org/r1/compare/': {'behind_by': 0},
                  'repos/org/r1/commits/sha1/check-runs': checks('failure'), 'repos/org/r1/pulls?state=closed': []}
        pq = nb_pulse.PulseQueue({}, FakeMCP(), lambda *a: None)
        nb_release.job_log = lambda *a, **k: 'FAIL src/x.spec.ts'
        gh, rel = release(routes, pq)
        rel.tick(lambda *a: None)
        self.assertEqual(sorted({op['task'] for op in pq.st['q'] if op['op'] == 'bug'}), ['1a2b3c4d', 'deadbeef'])

    def test_autoqa_hook_contract_maps_explicit_test_case_ids(self):
        mcp = FakeMCP(); pq = nb_pulse.PulseQueue({}, mcp, lambda *a: None)
        pq.report_failure('1a2b3c4d', 'AutoQA L13', test_case_ids=['cccc3333'])
        pq.drain()
        self.assertIn(('update_test_case', {'task_id': '1a2b3c4d', 'test_case_id': 'cccc3333', 'status': 'fail'}), mcp.calls)

    def test_log_excerpt_is_at_most_20_lines_around_the_error(self):
        text = '\n'.join('line %d' % i for i in range(100)) + '\nFAIL src/a.spec.ts\n' + '\n'.join('tail %d' % i for i in range(50))
        ex = L.log_excerpt(text)
        self.assertLessEqual(len(ex.splitlines()), 20)
        self.assertIn('FAIL src/a.spec.ts', ex)



class PulseTrailer(unittest.TestCase):
    def test_given_a_trailer_in_the_last_paragraph_then_its_id_is_read(self):
        self.assertEqual(nb_pulse.trailer('fix(crm): x\n\nbody 1a2b3c4d\n\nPulse: 1A2B3C4D'), '1a2b3c4d')

    def test_given_a_uuid_or_none_trailer_then_it_is_read(self):
        u = '1a2b3c4d-1111-2222-3333-444455556666'
        self.assertEqual(nb_pulse.trailer('feat: y\n\nPulse: ' + u), u)
        self.assertEqual(nb_pulse.trailer('chore: z\n\nPulse: none'), 'none')

    def test_given_loose_hex_or_a_subject_only_trailer_then_no_id_is_read(self):
        self.assertIsNone(nb_pulse.trailer('fix: deadbeef in 1a2b3c4d\n\nsha cafebabe'))
        self.assertIsNone(nb_pulse.trailer('Pulse: 1a2b3c4d'))
        self.assertIsNone(nb_pulse.trailer('fix: x\n\nPulse: 1a2b3c4\n'))          # 7 hex
        self.assertIsNone(nb_pulse.trailer('fix: x\n\nPulse: 1a2b3c4d\n\nmore text'))  # not the last paragraph

    def test_given_git_comment_lines_then_they_are_ignored(self):
        self.assertEqual(nb_pulse.trailer('fix: x\n\nPulse: 1a2b3c4d\n# Please enter the commit message'), '1a2b3c4d')

    def test_given_merge_or_fixup_commits_then_they_need_no_trailer(self):
        msgs = ["Merge branch 'develop'", 'fixup! fix: x', 'fix: y', 'fix: z\n\nPulse: none']
        self.assertEqual(nb_pulse.missing_trailers(msgs), [2])

    def test_given_commit_trailers_then_they_win_over_branch_and_body(self):
        ids = nb_pulse.task_ids(['a\n\nPulse: 1a2b3c4d', 'b\n\nPulse: deadbeef', 'c\n\nPulse: 1A2B3C4D'],
                                'fix/cafebabe-x', 'Pulse: 0badf00d')
        self.assertEqual(ids, ['1a2b3c4d', 'deadbeef'])

    def test_given_no_trailer_then_the_branch_then_the_pr_body_is_used(self):
        self.assertEqual(nb_pulse.task_ids(['a'], 'fix/cafebabe-login', 'Pulse: 0badf00d'), ['cafebabe'])
        self.assertEqual(nb_pulse.task_ids(['a'], 'feat/login', 'Pulse: 0badf00d\nsee deadbeef'), ['0badf00d'])
        self.assertEqual(nb_pulse.task_ids(['a\n\nPulse: none'], 'feat/login', 'fixes deadbeef'), [])

    def test_given_only_pulse_none_commits_then_the_train_accepts_the_pr(self):
        gh = FakeGH({'repos/org/r1/pulls/7/commits': [{'commit': {'message': 'ci: x\n\nPulse: none'}}]})
        rel = nb_release.Release(gh, 'org', MAN, lambda *a: None, lambda t: None, {}, None, True, ['r1'])
        p = pr(7, pulse=())
        self.assertEqual(rel.refs('r1', p), ([], True))
        p2 = pr(8, pulse=())
        self.assertEqual(rel.refs('r1', p2), ([], False))      # commits unreadable, no branch/body id


class RunnerBoot(unittest.TestCase):
    PROV = open(os.path.join(ROOT, 'runner', 'image', 'provision.sh')).read()

    def test_given_image_mode_then_the_family_image_and_only_the_jit_config_are_used(self):
        src, items = L.boot_plan('image', 'fam-link', self.PROV, 'J')
        self.assertEqual((src, items), ('fam-link', [{'key': 'jit', 'value': 'J'}]))

    def test_given_startup_mode_then_stock_ubuntu_boots_with_provision_as_startup_script(self):
        src, items = L.boot_plan('startup', 'fam-link', self.PROV, 'J')
        self.assertEqual(src, 'projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64')
        md = {i['key']: i['value'] for i in items}
        self.assertEqual(md['jit'], 'J')
        self.assertIn('export NB_STARTUP=1', md['startup-script'])
        self.assertIn('trap poweroff ERR', md['startup-script'])
        self.assertTrue(md['startup-script'].rstrip().endswith('systemctl start --no-block nb-runner.service'))

    def test_given_startup_mode_then_the_script_fits_gce_metadata_limits(self):
        _, items = L.boot_plan('startup', 'f', self.PROV, 'J' * 4000)
        self.assertLess(sum(len(i['value']) for i in items), 256 * 1024)

    def test_given_startup_mode_then_provision_skips_image_only_steps(self):
        self.assertIn('[ -n "${NB_STARTUP:-}" ] || sudo -u runner -H npm cache add', self.PROV)
        self.assertIn('if [ -z "${NB_STARTUP:-}" ]; then', self.PROV)
        self.assertNotIn('exit 0', self.PROV)   # an exit would skip the appended runner start

    def test_given_startup_mode_then_the_controller_never_builds_an_image(self):
        src = open(os.path.join(ROOT, 'runner', 'controller', 'nbci.py')).read()
        self.assertIn("RUNNER_BOOT != 'startup' and every(st, 'image_check'", src)
        self.assertIn("build-image refused: RUNNER_BOOT=startup", src)


class Supply(unittest.TestCase):
    JOB = {'id': 1, 'labels': ['self-hosted', 'nb-auto']}

    def want(self, jobs, vms, runners):
        pend = L.pending_by_class(vms, runners, 1000, 300)
        return L.plan(jobs, pend, CLS)

    def vm(self, status='RUNNING', age=30, name='nbci-gate-1'):
        return {'name': name, 'status': status, 'created': 1000 - age, 'cls': 'gate'}

    def test_given_one_job_and_one_booting_vm_with_an_offline_registered_runner_then_no_create(self):
        self.assertEqual(self.want([self.JOB], [self.vm('STAGING')], {'nbci-gate-1': {'status': 'offline', 'busy': False}}), [])

    def test_given_one_job_and_no_vms_then_one_create(self):
        self.assertEqual(len(self.want([self.JOB], [], {})), 1)

    def test_given_two_jobs_and_one_booting_vm_then_one_create(self):
        self.assertEqual(len(self.want([self.JOB, dict(self.JOB, id=2)], [self.vm()], {'nbci-gate-1': {'status': 'offline', 'busy': False}})), 1)

    def test_given_a_busy_vm_then_it_is_not_supply(self):
        self.assertEqual(len(self.want([self.JOB], [self.vm(age=600)], {'nbci-gate-1': {'status': 'online', 'busy': True}})), 1)

    def test_given_a_stopped_vm_then_it_is_not_supply(self):
        self.assertEqual(len(self.want([self.JOB], [self.vm('TERMINATED')], {})), 1)

    def test_given_a_vm_was_bought_for_the_job_then_no_second_create_in_the_window(self):
        want = L.plan([self.JOB], {}, CLS)
        out, ids = L.cap_creates(want, [self.JOB], {'1': 1000}, 1030)
        self.assertEqual((out, ids), ([], []))

    def test_given_an_unserved_job_then_one_create_and_it_is_marked(self):
        want = L.plan([self.JOB], {}, CLS) * 3
        out, ids = L.cap_creates(want, [self.JOB], {}, 1000)
        self.assertEqual((len(out), ids), (1, [1]))

    def test_given_the_window_has_passed_then_the_job_may_buy_again(self):
        out, _ = L.cap_creates(L.plan([self.JOB], {}, CLS), [self.JOB], {'1': 0}, 1000)
        self.assertEqual(len(out), 1)


class ChurnGuard(unittest.TestCase):
    def run_guard(self, tmp, names):
        import subprocess, os
        env = dict(os.environ, SEEN_FILE=os.path.join(tmp, 'seen'), STOP_CMD='touch ' + os.path.join(tmp, 'stopped'))
        r = subprocess.run(['bash', os.path.join(os.path.dirname(__file__), '..', 'runner', 'controller', 'churn-guard.sh')],
                           input=names, text=True, capture_output=True, env=env)
        return r.returncode, os.path.exists(os.path.join(tmp, 'stopped'))

    def test_given_one_vm_then_the_guard_stays_quiet(self):
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(self.run_guard(t, 'nbci-gate-1:RUNNING '), (0, False))

    def test_given_a_second_vm_across_polls_then_nbci_is_stopped(self):
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            self.run_guard(t, 'nbci-gate-1:RUNNING ')
            self.assertEqual(self.run_guard(t, 'nbci-gate-2:RUNNING '), (1, True))

    def test_given_two_vms_in_one_space_separated_poll_then_nbci_is_stopped(self):
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(self.run_guard(t, 'nbci-gate-1:RUNNING nbci-gate-2:STAGING '), (1, True))


class ImageProvision(unittest.TestCase):
    PROV = os.path.join(os.path.dirname(__file__), '..', 'runner', 'image', 'provision.sh')

    def body(self):
        with open(self.PROV) as f:
            return f.read()

    def test_given_provisioning_then_no_unauthenticated_github_api_call_is_made(self):
        self.assertNotIn('api.github.com', self.body())

    def test_given_provisioning_then_runner_and_gitleaks_versions_are_pinned(self):
        b = self.body()
        self.assertRegex(b, r'V=\$\{RUNNER_VERSION:-\d+\.\d+\.\d+\}')
        self.assertRegex(b, r'GL=\$\{GITLEAKS_VERSION:-\d+\.\d+\.\d+\}')

    def test_given_provisioning_then_semgrep_installs_in_a_venv_not_system_pip(self):
        b = self.body()
        self.assertIn('/opt/semgrep/bin/pip install', b)
        self.assertNotIn('pip3 install -q --break-system-packages', b)

    def test_given_the_per_job_boot_script_then_it_only_reads_metadata_and_runs_the_runner(self):
        run = self.body().split("cat > /opt/nb-run.sh <<'RUN'")[1].split('\nRUN\n')[0]
        for bad in ('apt', 'pip', 'npm', 'curl -fsSL', 'git clone', 'wget'):
            self.assertNotIn(bad, run)

    def test_given_an_image_build_then_only_the_newest_image_is_kept(self):
        with open(os.path.join(os.path.dirname(__file__), '..', 'runner', 'controller', 'nbci.py')) as f:
            self.assertIn('imgs[1:]', f.read())


class Speed(unittest.TestCase):
    def gate(self):
        return L.class_for(['self-hosted', 'nb-auto'], CLS)

    def test_given_the_gate_class_then_it_is_an_8_vcpu_spot_c2d_and_four_fit_in_32_vcpu(self):
        g = self.gate()
        self.assertEqual((g['machine'], g['vcpu'], g['spot']), ('c2d-highcpu-8', 8, True))
        self.assertEqual(32 // g['vcpu'], 4)

    def test_given_a_class_price_then_the_hourly_cost_uses_it(self):
        g = self.gate(); prices = L.load(os.path.join(os.path.dirname(__file__), '..', 'ci', 'runner-labels.json'))['prices_usd']
        self.assertAlmostEqual(L.hourly_usd(g, prices), 8 * 0.01415 + 16 * 0.001895 + prices['ip_spot'] + 40 * prices['pd_balanced_gb_month'] / 730, places=6)

    def test_given_a_gate_vm_then_its_cache_token_is_read_only_and_a_deploy_vm_read_write(self):
        self.assertTrue(L.service_accounts(self.gate(), 'p')[0]['email'].startswith('nbci-cache-ro@p.'))
        self.assertIn('read_only', L.service_accounts(self.gate(), 'p')[0]['scopes'][0])
        d = L.class_for(['self-hosted', 'nb-deploy'], CLS)
        self.assertIn('read_write', L.service_accounts(d, 'p')[0]['scopes'][0])

    def test_given_a_class_without_sa_then_no_service_account_is_attached(self):
        self.assertEqual(L.service_accounts({'name': 'x'}, 'p'), [])

    def test_given_the_job_vm_then_the_boot_disk_is_pd_balanced(self):
        with open(os.path.join(os.path.dirname(__file__), '..', 'runner', 'controller', 'nbci.py')) as f:
            self.assertIn("'diskType': 'zones/%s/diskTypes/pd-balanced' % z", f.read())

    def test_given_the_image_then_node_is_in_the_tool_cache_and_the_cache_token_is_fetched_as_root(self):
        with open(os.path.join(os.path.dirname(__file__), '..', 'runner', 'image', 'provision.sh')) as f:
            b = f.read()
        self.assertIn('/opt/hostedtoolcache/node/$NODEV/x64.complete', b)
        self.assertIn('RUNNER_TOOLSDIRECTORY=/opt/hostedtoolcache', b)
        self.assertIn('/run/nb-gcs-token', b)


class GateSpeed(unittest.TestCase):
    WF = os.path.join(os.path.dirname(__file__), '..', '.github', 'workflows')

    def load(self, name):
        import yaml
        with open(os.path.join(self.WF, name)) as f:
            return yaml.safe_load(f)

    def test_given_the_pr_gate_then_unit_and_security_are_parallel_jobs_and_gate_needs_both(self):
        j = self.load('neonbee-pr-gate.yml')['jobs']
        self.assertEqual(j['unit']['needs'], ['route'])
        self.assertEqual(j['security']['needs'], ['route'])
        self.assertEqual(sorted(j['gate']['needs']), ['route', 'security', 'unit'])
        self.assertEqual(j['gate']['name'], 'Gate')          # the required check name is unchanged
        self.assertEqual(j['gate']['if'], 'always()')        # a failed or skipped job must fail the gate, not skip it

    def test_given_the_gate_aggregator_then_it_fails_unless_both_results_are_success(self):
        run = self.load('neonbee-pr-gate.yml')['jobs']['gate']['steps'][0]['run']
        self.assertIn('"$UNIT" = success', run)
        self.assertIn('"$SECURITY" = success', run)

    def test_given_a_pr_then_gitleaks_scans_only_the_pr_commits_and_pushes_scan_everything(self):
        with open(os.path.join(self.WF, 'neonbee-pr-gate.yml')) as f:
            b = f.read()
        self.assertIn('GL_SCOPE=(--log-opts "$NB_PR_BASE..HEAD")', b)
        self.assertIn('"${GL_SCOPE[@]}"', b)

    def test_given_the_npm_cache_then_pr_jobs_only_restore_and_only_deploy_saves_on_develop_and_main(self):
        with open(os.path.join(self.WF, 'neonbee-pr-gate.yml')) as f:
            self.assertNotIn('Save npm cache', f.read())
        with open(os.path.join(self.WF, 'neonbee-deploy-vite-mfe.yml')) as f:
            b = f.read()
        self.assertIn("github.ref == 'refs/heads/develop' || github.ref == 'refs/heads/main'", b)
        self.assertIn('NB_NPM_CACHE_HIT != \'1\'', b)


class Aqa(unittest.TestCase):
    CFG = {'deploy_workflows': ['deploy'], 'ignore_workflows': ['teardown', 'rollback']}

    def run_(self, name='Deploy', branch='develop', conclusion='success'):
        return {'name': name, 'head_branch': branch, 'conclusion': conclusion, 'event': 'push'}

    def test_given_a_successful_develop_deploy_then_the_env_is_dev(self):
        import nbci_aqa as A
        self.assertEqual(A.deploy_env(self.run_(), self.CFG), 'dev')

    def test_given_a_successful_preview_deploy_then_the_env_is_pr_branch(self):
        import nbci_aqa as A
        self.assertEqual(A.deploy_env(self.run_('Deploy Preview', 'feat/Full Flow_1'), self.CFG), 'pr-feat-full-flow-1')

    def test_given_a_main_deploy_then_the_env_is_prod(self):
        import nbci_aqa as A
        self.assertEqual(A.deploy_env(self.run_(branch='main'), self.CFG), 'prod')

    def test_given_a_failed_or_teardown_or_other_branch_run_then_no_trigger(self):
        import nbci_aqa as A
        self.assertIsNone(A.deploy_env(self.run_(conclusion='failure'), self.CFG))
        self.assertIsNone(A.deploy_env(self.run_('Preview teardown on merge (deploy)'), self.CFG))
        self.assertIsNone(A.deploy_env(self.run_(branch='qa'), self.CFG))
        self.assertIsNone(A.deploy_env(self.run_('PR Gate'), self.CFG))

    def test_given_commit_trailers_then_pulse_ids_are_extracted_once_and_none_is_ignored(self):
        import nbci_aqa as A
        msgs = ['feat: a\n\nPulse: 3d32de89', 'fix: b\n\nPulse: none', 'c\n\nPulse: 3D32DE89, 0a1b2c3d', 'no trailer']
        self.assertEqual(A.pulse_ids(msgs), ['3d32de89', '0a1b2c3d'])

    def test_given_the_same_repo_and_sha_then_the_second_trigger_is_a_duplicate(self):
        import nbci_aqa as A
        st = {}; k = A.dedupe_key('r', 'abc')
        self.assertEqual(A.may_trigger(st, k, '2026-11-27', 8), (True, ''))
        A.record(st, k, '2026-11-27', 1000)
        self.assertEqual(A.may_trigger(st, k, '2026-11-27', 8), (False, 'duplicate'))

    def test_given_the_daily_cap_is_reached_then_no_more_vms_that_day_but_next_day_is_fine(self):
        import nbci_aqa as A
        st = {}
        for i in range(2):
            A.record(st, 'r@%d' % i, '2026-11-27', 1000 + i)
        self.assertEqual(A.may_trigger(st, 'r@new', '2026-11-27', 2), (False, 'daily cap'))
        self.assertTrue(A.may_trigger(st, 'r@new', '2026-11-28', 2)[0])

    def test_given_a_deploy_then_the_vm_metadata_carries_the_seven_aqa_keys(self):
        import nbci_aqa as A
        m = {i['key']: i['value'] for i in A.metadata('dev', 'sha1', 'b', ['3d32de89'], ['c1', 'c2'], 'ev', 'done', 'repo')}
        self.assertEqual({k: m[k] for k in ('aqa-env', 'aqa-sha', 'aqa-bundle', 'aqa-tasks', 'aqa-commits', 'aqa-evidence', 'aqa-pass-stage')},
                         {'aqa-env': 'dev', 'aqa-sha': 'sha1', 'aqa-bundle': 'b', 'aqa-tasks': '3d32de89', 'aqa-commits': 'c1,c2',
                          'aqa-evidence': 'ev', 'aqa-pass-stage': 'done'})

    def test_given_the_contract_then_the_vm_is_spot_with_no_static_ip_and_disks_die_with_it(self):
        import nbci_aqa as A
        b = A.vm_body({'service_account': 'a@p.iam', 'startup_script': '#!/bin/bash'}, 'n', 'us-central1-a', [], 'nbci', 5400)
        self.assertEqual(b['scheduling']['provisioningModel'], 'SPOT')
        self.assertEqual(b['scheduling']['instanceTerminationAction'], 'DELETE')
        self.assertTrue(b['disks'][0]['autoDelete'])
        self.assertNotIn('natIP', b['networkInterfaces'][0]['accessConfigs'][0])
        self.assertEqual(b['serviceAccounts'][0]['email'], 'a@p.iam')
        self.assertEqual(b['labels']['nb-class'], 'aqa')

    def test_given_an_aqa_vm_without_a_runner_then_it_is_not_deleted_as_no_job_but_is_when_stopped(self):
        vm = {'status': 'RUNNING', 'created': 0, 'cls': 'aqa'}
        self.assertIsNone(L.vm_verdict(vm, None, False, 100000))
        self.assertIn('stopped', L.vm_verdict(dict(vm, status='TERMINATED'), None, False, 100000))

    def test_given_the_controller_then_the_trigger_is_held_until_the_contract_is_ready(self):
        with open(os.path.join(os.path.dirname(__file__), '..', 'ci', 'aqa.json')) as f:
            self.assertFalse(json.load(f)['contract_ready'])


if __name__ == '__main__':
    unittest.main()
