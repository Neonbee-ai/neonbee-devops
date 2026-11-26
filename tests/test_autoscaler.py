"""BDD tests for runner/autoscaler/nb_autoscaler.py decision logic (no network)."""
import json, os, sys, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'runner', 'autoscaler'))
import nb_autoscaler as A  # noqa: E402
import nb_release as R  # noqa: E402
LABELS = os.path.join(ROOT, 'ci', 'runner-labels.json')

NOW = 1_800_000_000


def runner(rid, name, labels, status='online', busy=False):
    return {'id': rid, 'name': name, 'status': status, 'busy': busy, 'labels': [{'name': l} for l in labels]}


def job(jid, labels, age=60):
    return {'id': jid, 'repo': 'r', 'name': 'j', 'labels': labels, 'created': NOW - age}


def vm(name='nbci-1', slots=2, md=None, status='RUNNING', age=600):
    return {'name': name, 'slots': slots, 'md': md or {}, 'status': status, 'created': NOW - age}


class Labels(unittest.TestCase):
    def test_given_the_canonical_file_then_burst_carries_every_ci_label_including_nb_route(self):
        burst = A.load_config(LABELS)[0]['burst']
        for l in ('self-hosted', 'Linux', 'X64', 'runner-vm', 'nb-route', 'nb-pool', 'lane-burst'):
            self.assertIn(l, burst)

    def test_given_the_canonical_file_then_aqa_coord_stays_off_the_burst_pool(self):
        pools = A.load_config(LABELS)[0]
        self.assertNotIn('aqa-coord', pools['burst'])
        self.assertIn('aqa-coord', pools['aqa-coord'])

    def test_given_the_canonical_file_then_no_mac_mini_carries_a_ci_label(self):
        d = json.load(open(LABELS))
        ci = set(d['ci_labels'])
        self.assertFalse(ci & set(A.load_config(LABELS)[0]['macmini']))
        for name, labels in d['minis'].items():
            with self.subTest(mini=name):
                self.assertFalse(ci & set(labels))

    def test_given_the_canonical_file_then_exactly_one_fallback_runner_is_named(self):
        fb = A.load_config(LABELS)[1]
        self.assertTrue(fb['runner'].startswith('macmini'))
        self.assertNotIn('aqa-coord', json.load(open(LABELS))['minis'][fb['runner']])

    def test_when_a_workflow_routes_a_job_then_its_label_is_in_the_burst_pool(self):
        burst = A.lower(A.load_config(LABELS)[0]['burst'])
        wf = os.path.join(ROOT, '.github', 'workflows')
        import re
        for f in os.listdir(wf):
            text = open(os.path.join(wf, f)).read()
            for lane in re.findall(r"'(\[\"self-hosted\"[^']*\])'", text):
                with self.subTest(f=f, lane=lane):
                    self.assertTrue(A.lower(json.loads(lane)) <= burst)


class Demand(unittest.TestCase):
    burst = ['self-hosted', 'Linux', 'X64', 'runner-vm', 'nb-route']

    def test_given_an_idle_mini_with_ci_labels_then_it_is_not_counted_and_a_vm_is_bought(self):
        rs = [runner(1, 'macmini-preview-3', ['self-hosted', 'macOS', 'runner-vm'])]
        self.assertEqual(len(A.demand([job(1, ['self-hosted', 'runner-vm'])], rs, self.burst, NOW)), 1)

    def test_given_an_idle_burst_slot_then_it_takes_the_job(self):
        rs = [runner(1, 'nbci-0101-1-s0-1', ['self-hosted', 'runner-vm'])]
        self.assertEqual(A.demand([job(1, ['self-hosted', 'runner-vm'])], rs, self.burst, NOW), [])

    def test_given_a_macos_only_job_then_burst_never_counts_it(self):
        self.assertEqual(A.demand([job(1, ['self-hosted', 'aqa-coord'])], [], self.burst, NOW), [])

    def test_given_a_job_younger_than_min_age_then_it_is_not_bought_for_yet(self):
        self.assertEqual(A.demand([job(1, ['self-hosted', 'runner-vm'], age=1)], [], self.burst, NOW), [])

    def test_given_two_jobs_and_one_idle_runner_then_one_waits(self):
        rs = [runner(1, 'nbci-x-s0-1', ['self-hosted', 'runner-vm', 'nb-route'])]
        jobs = [job(1, ['self-hosted', 'runner-vm']), job(2, ['self-hosted', 'nb-route'])]
        self.assertEqual(len(A.demand(jobs, rs, self.burst, NOW)), 1)


class Plan(unittest.TestCase):
    def test_given_ten_waiting_jobs_and_32_vcpu_then_it_buys_ten_slots_in_few_vms(self):
        plan = A.plan_vms(10, 32)
        self.assertEqual(sum(s for s, _ in plan), 10)
        self.assertLessEqual(sum(s for s, _ in plan) * A.VCPU_PER_SLOT, 32)
        self.assertEqual(plan[0], (8, 't2d-standard-16'))

    def test_given_more_demand_than_quota_then_it_stops_at_the_quota(self):
        plan = A.plan_vms(40, 32)
        self.assertEqual(sum(s for s, _ in plan) * A.VCPU_PER_SLOT, 32)

    def test_given_no_quota_then_it_buys_nothing(self):
        self.assertEqual(A.plan_vms(5, 0), [])

    def test_given_three_jobs_then_the_remainder_is_covered_by_the_smallest_fitting_vm(self):
        self.assertEqual(sum(s for s, _ in A.plan_vms(3, 32)), 3)


class Slots(unittest.TestCase):
    def test_given_a_fresh_jit_without_runner_then_the_slot_is_pending(self):
        v = vm(md={'name-0': 'a', 'at-0': str(NOW - 30)}, slots=1)
        self.assertEqual(A.slot_state(v, {}, NOW), {0: 'pending'})

    def test_given_an_old_jit_whose_runner_finished_then_the_slot_is_empty(self):
        v = vm(md={'name-0': 'a', 'at-0': str(NOW - 3600)}, slots=1)
        self.assertEqual(A.slot_state(v, {}, NOW), {0: 'empty'})

    def test_given_runners_online_and_busy_then_states_follow_them(self):
        v = vm(md={'name-0': 'a', 'at-0': '0', 'name-1': 'b', 'at-1': '0'})
        rs = {'a': {'status': 'online', 'busy': True}, 'b': {'status': 'online', 'busy': False}}
        self.assertEqual(A.slot_state(v, rs, NOW), {0: 'busy', 1: 'idle'})


class Reap(unittest.TestCase):
    def test_given_a_busy_slot_then_the_vm_is_kept(self):
        self.assertIsNone(A.vm_should_delete(vm(), {0: 'busy', 1: 'empty'}, NOW - 9999, NOW))

    def test_given_no_job_for_three_minutes_then_the_vm_is_deleted(self):
        self.assertIn('idle', A.vm_should_delete(vm(), {0: 'idle', 1: 'empty'}, NOW - A.IDLE_S, NOW))

    def test_given_a_job_two_minutes_ago_then_the_vm_is_kept(self):
        self.assertIsNone(A.vm_should_delete(vm(), {0: 'idle'}, NOW - 120, NOW))

    def test_given_a_terminated_vm_then_it_is_deleted_at_once(self):
        self.assertIn('dead', A.vm_should_delete(vm(status='TERMINATED'), {0: 'pending'}, NOW, NOW))

    def test_given_a_booting_vm_with_pending_slots_then_it_is_kept(self):
        self.assertIsNone(A.vm_should_delete(vm(age=60), {0: 'pending'}, 0, NOW))

    def test_given_a_busy_runner_then_delete_vm_refuses_without_any_api_call(self):
        rs = [runner(9, 'nbci-1-s0-5', ['self-hosted'], busy=True)]
        self.assertFalse(A.delete_vm(vm(), rs, 'idle 999s'))


class Fallback(unittest.TestCase):
    def test_given_a_billing_error_then_gcp_is_failing_at_once(self):
        self.assertEqual(A.gcp_failing([('europe-west1-b', A.classify_gce_error('403 BILLING_DISABLED for project'))]), 'billing')

    def test_given_quota_exceeded_then_gcp_is_failing(self):
        self.assertEqual(A.gcp_failing([('z', A.classify_gce_error("QUOTA_EXCEEDED: Quota 'CPUS_ALL_REGIONS' exceeded"))]), 'quota')

    def test_given_a_stockout_in_one_zone_only_then_it_is_not_failing(self):
        self.assertIsNone(A.gcp_failing([('europe-west1-b', 'stockout')]))

    def test_given_stockouts_in_two_zones_then_it_is_failing(self):
        e = A.classify_gce_error('ZONE_RESOURCE_POOL_EXHAUSTED')
        self.assertEqual(A.gcp_failing([('europe-west1-b', e), ('europe-west1-c', e)]), 'stockout')

    def test_given_gcp_failing_and_a_job_waiting_two_minutes_then_fallback_turns_on(self):
        self.assertTrue(A.fallback_decision(False, A.FALLBACK_AFTER_S, 'quota', False))

    def test_given_gcp_failing_but_no_job_waiting_long_then_fallback_stays_off(self):
        self.assertFalse(A.fallback_decision(False, 10, 'quota', False))

    def test_given_fallback_on_and_gcp_started_a_vm_then_fallback_turns_off(self):
        self.assertFalse(A.fallback_decision(True, 999, None, True))

    def test_given_fallback_on_and_gcp_still_failing_then_it_stays_on(self):
        self.assertTrue(A.fallback_decision(True, 0, 'billing', False))

    def test_given_labels_already_match_then_set_fallback_makes_no_change(self):
        r = {'id': 1, 'name': 'macmini-preview-3', 'labels': [{'name': 'self-hosted', 'type': 'read-only'}]}
        self.assertFalse(A.set_fallback(r, False, ['runner-vm']))


class Train(unittest.TestCase):
    pr = {'title': 'fix(crm): import PULSE-1a2b3c4d', 'draft': False, 'mergeable': True, 'mergeable_state': 'clean'}

    def runs(self, *spec):
        return [{'id': i, 'name': n, 'status': st, 'conclusion': c} for i, (n, st, c) in enumerate(spec)]

    def test_given_a_title_without_pulse_id_then_the_pr_is_ejected(self):
        self.assertTrue(R.train_action(dict(self.pr, title='fix: x'), 0, 'green').startswith('eject:'))

    def test_given_a_pr_behind_its_base_then_it_is_updated_and_retested(self):
        self.assertEqual(R.train_action(self.pr, 3, 'green'), 'update')

    def test_given_a_conflict_then_the_pr_is_ejected(self):
        self.assertIn('conflict', R.train_action(dict(self.pr, mergeable=False), 0, 'green'))

    def test_given_green_checks_on_an_up_to_date_head_then_it_merges(self):
        self.assertEqual(R.train_action(self.pr, 0, 'green'), 'merge')

    def test_given_checks_running_then_it_waits(self):
        self.assertEqual(R.train_action(self.pr, 0, 'pending'), 'wait')

    def test_given_a_red_check_then_it_is_ejected(self):
        self.assertTrue(R.train_action(self.pr, 0, 'red').startswith('eject:'))

    def test_given_no_pr_gate_check_then_verdict_is_no_gate(self):
        self.assertEqual(R.checks_verdict(self.runs(('lint', 'completed', 'success'))), 'no-gate')

    def test_given_gate_green_and_others_skipped_then_verdict_is_green(self):
        self.assertEqual(R.checks_verdict(self.runs(('gate / Gate', 'completed', 'success'),
                                                    ('gate / Integration Tests', 'completed', 'skipped'))), 'green')

    def test_given_an_old_failure_superseded_by_a_rerun_then_the_latest_counts(self):
        self.assertEqual(R.checks_verdict(self.runs(('gate / Gate', 'completed', 'failure'),
                                                    ('gate / Gate', 'completed', 'success'))), 'green')

    def test_given_pulse_ids_in_title_and_body_then_each_is_found_once(self):
        self.assertEqual(R.pulse_ids('x PULSE-1a2b3c4d', 'also PULSE-1A2B3C4D and PULSE-deadbeef'), ['1a2b3c4d', 'deadbeef'])


class Promote(unittest.TestCase):
    def test_given_all_main_runs_green_then_develop_may_auto_merge(self):
        self.assertTrue(A.promote_ready([{'status': 'completed', 'conclusion': 'success'},
                                         {'status': 'completed', 'conclusion': 'skipped'}]))

    def test_given_a_run_still_going_then_it_waits(self):
        self.assertFalse(A.promote_ready([{'status': 'in_progress', 'conclusion': None}]))

    def test_given_a_failed_main_run_then_develop_is_not_promoted(self):
        self.assertFalse(A.promote_ready([{'status': 'completed', 'conclusion': 'failure'}]))

    def test_given_no_runs_then_it_waits(self):
        self.assertFalse(A.promote_ready([]))


class WorkflowContracts(unittest.TestCase):
    def test_every_notify_job_is_non_gating(self):
        import re
        wf = os.path.join(ROOT, '.github', 'workflows')
        for f in os.listdir(wf):
            text = open(os.path.join(wf, f)).read()
            for m in re.finditer(r'\n    name: Notify Teams\n((?:    .*\n)*?)    steps:', text):
                with self.subTest(f=f):
                    self.assertIn('continue-on-error: true', m.group(1))

    def test_pr_gate_requires_a_pulse_id_in_the_title(self):
        text = open(os.path.join(ROOT, '.github', 'workflows', 'neonbee-pr-gate.yml')).read()
        self.assertIn('name: PR title has a Pulse task id', text)
        self.assertIn("PULSE-[0-9a-f]{8}", text)

    def test_prod_canary_deploys_ha_first_and_never_touches_primary_when_ha_fails(self):
        text = open(os.path.join(ROOT, '.github', 'workflows', 'neonbee-deploy-prod.yml')).read()
        c = text[text.index('Canary: HA VM first'):text.index('Canary healthy on HA')]
        self.assertIn('deploy_vm "$HA_IP"', c)
        self.assertNotIn('deploy_vm "$SERVER_IP"', c)
        self.assertIn('exit 1', c)
        self.assertIn("dist-prev", c)

    def test_s3_release_verifies_the_served_file_after_the_revert_step(self):
        text = open(os.path.join(ROOT, '.github', 'workflows', 'neonbee-deploy-s3-release.yml')).read()
        self.assertLess(text.index('name: Revert to previous release'), text.index('name: Verify served artifact is this release'))


if __name__ == '__main__':
    unittest.main()
