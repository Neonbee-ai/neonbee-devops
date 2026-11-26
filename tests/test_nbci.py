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
    def test_given_a_plain_runner_vm_job_then_it_gets_a_spot_gate_vm(self):
        c = L.class_for(['self-hosted', 'runner-vm'], CLS)
        self.assertEqual((c['name'], c['spot']), ('gate', True))

    def test_given_a_deploy_job_then_it_gets_a_standard_non_spot_vm(self):
        c = L.class_for(['self-hosted', 'nb-deploy'], CLS)
        self.assertEqual((c['name'], c['spot']), ('deploy', False))

    def test_given_a_route_job_then_it_runs_on_the_controller_not_a_vm(self):
        self.assertEqual(L.class_for(['self-hosted', 'nb-route'], CLS)['host'], 'controller')

    def test_given_size_labels_then_vms_are_sized_2_4_8(self):
        self.assertEqual(L.class_for(['self-hosted', 'nb-size-l'], CLS)['vcpu'], 8)
        self.assertEqual(L.class_for(['self-hosted', 'nb-size-s'], CLS)['vcpu'], 2)
        self.assertEqual(L.class_for(['self-hosted', 'runner-vm'], CLS)['vcpu'], 4)

    def test_given_a_macos_job_then_no_class_serves_it(self):
        self.assertIsNone(L.class_for(['self-hosted', 'aqa-coord'], CLS))

    def test_no_spot_class_carries_the_deploy_label(self):
        for c in CLS:
            if c.get('spot'):
                self.assertNotIn('nb-deploy', c['labels'])

    def test_every_routed_lane_in_the_workflows_has_a_class(self):
        for f in os.listdir(os.path.join(ROOT, '.github', 'workflows')):
            for lane in re.findall(r"'(\[\"self-hosted\"[^']*\])'", wf(f)):
                with self.subTest(f=f, lane=lane):
                    self.assertIsNotNone(L.class_for(json.loads(lane), CLS))


class Minis(unittest.TestCase):
    def test_no_mac_mini_carries_a_ci_label(self):
        ci = set(CFG['ci_labels']) | {'nb-deploy'}
        for name, labels in CFG['minis'].items():
            with self.subTest(mini=name):
                self.assertFalse(ci & set(labels))

    def test_exactly_one_fallback_runner_and_it_is_mini_3(self):
        self.assertEqual(CFG['fallback']['runner'], 'macmini-preview-3')
        self.assertIn('nb-deploy', CFG['fallback']['labels'])


class Cost(unittest.TestCase):
    def test_a_spot_gate_vm_costs_its_real_hourly_price(self):
        # 4 x 0.01309 + 16 x 0.001754 + IP 0.0025 + 40 GB x 0.10 / 730
        self.assertAlmostEqual(L.hourly_usd(BY['gate'], PRICES), 0.0524 + 0.028064 + 0.0025 + 0.005479, places=4)

    def test_a_deploy_vm_is_dearer_than_spot(self):
        self.assertGreater(L.hourly_usd(BY['deploy'], PRICES), L.hourly_usd(BY['gate'], PRICES))

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
        out = L.plan([self.job(['self-hosted', 'runner-vm'])] * 2 + [self.job(['self-hosted', 'nb-deploy'])], {}, CLS)
        self.assertEqual([c['name'] for c in out], ['deploy', 'gate', 'gate'])

    def test_vms_already_booting_count_as_capacity(self):
        self.assertEqual(len(L.plan([self.job(['self-hosted', 'runner-vm'])] * 3, {'gate': 2}, CLS)), 1)

    def test_route_jobs_never_buy_a_vm(self):
        self.assertEqual(L.plan([self.job(['self-hosted', 'nb-route'])], {}, CLS), [])


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
    pr = {'title': 'fix(crm): import PULSE-1a2b3c4d', 'draft': False, 'mergeable': True, 'mergeable_state': 'clean', 'labels': []}

    def runs(self, *spec):
        return [{'id': i, 'name': n, 'status': st, 'conclusion': c} for i, (n, st, c) in enumerate(spec)]

    def test_title_without_pulse_id_is_ejected(self):
        self.assertTrue(L.train_action(dict(self.pr, title='fix: x'), 0, 'green').startswith('eject:'))

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
        self.assertEqual(L.pulse_ids('x PULSE-1a2b3c4d', 'PULSE-1A2B3C4D PULSE-deadbeef'), ['1a2b3c4d', 'deadbeef'])
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

    def test_pr_title_needs_a_pulse_id_unless_legacy(self):
        t = wf('neonbee-pr-gate.yml')
        self.assertIn('PULSE-[0-9a-f]{8}', t)
        self.assertIn("vars.NB_CI_MODE == 'auto'", t)


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
        self.assertIn("'serviceAccounts': []", self.src)

    def test_dry_run_is_the_default(self):
        self.assertIn("DRY = E('DRY_RUN', '1') == '1'", self.src)

    def test_controller_local_runners_are_never_deregistered(self):
        self.assertIn("not r['name'].startswith('nbci-ctl-')", self.src)


if __name__ == '__main__':
    unittest.main()
