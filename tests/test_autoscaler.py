"""BDD tests for runner/autoscaler/nb_autoscaler.py decision logic (no network)."""
import json, os, sys, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'runner', 'autoscaler'))
import nb_autoscaler as A  # noqa: E402

NOW = 1_800_000_000


def runner(rid, name, labels, status='online', busy=False):
    return {'id': rid, 'name': name, 'status': status, 'busy': busy, 'labels': [{'name': l} for l in labels]}


def job(jid, labels, age=60):
    return {'id': jid, 'repo': 'r', 'name': 'j', 'labels': labels, 'created': NOW - age}


def vm(name='nbci-1', slots=2, md=None, status='RUNNING', age=600):
    return {'name': name, 'slots': slots, 'md': md or {}, 'status': status, 'created': NOW - age}


class Labels(unittest.TestCase):
    def test_given_the_canonical_file_then_burst_carries_every_ci_label_including_nb_route(self):
        burst = A.load_labels(os.path.join(ROOT, 'ci', 'runner-labels.json'))['burst']
        for l in ('self-hosted', 'Linux', 'X64', 'runner-vm', 'nb-route', 'nb-pool', 'lane-burst'):
            self.assertIn(l, burst)

    def test_given_the_canonical_file_then_aqa_coord_stays_off_the_burst_pool(self):
        pools = A.load_labels(os.path.join(ROOT, 'ci', 'runner-labels.json'))
        self.assertNotIn('aqa-coord', pools['burst'])
        self.assertIn('aqa-coord', pools['aqa-coord'])

    def test_when_a_workflow_routes_a_job_then_its_label_is_in_the_burst_pool(self):
        burst = A.lower(A.load_labels(os.path.join(ROOT, 'ci', 'runner-labels.json'))['burst'])
        wf = os.path.join(ROOT, '.github', 'workflows')
        import re
        for f in os.listdir(wf):
            text = open(os.path.join(wf, f)).read()
            for lane in re.findall(r"'(\[\"self-hosted\"[^']*\])'", text):
                with self.subTest(f=f, lane=lane):
                    self.assertTrue(A.lower(json.loads(lane)) <= burst)


class Demand(unittest.TestCase):
    burst = ['self-hosted', 'Linux', 'X64', 'runner-vm', 'nb-route']

    def test_given_an_idle_mini_then_it_takes_the_job_and_no_vm_is_needed(self):
        rs = [runner(1, 'macmini-preview', ['self-hosted', 'macOS', 'runner-vm'])]
        self.assertEqual(A.demand([job(1, ['self-hosted', 'runner-vm'])], rs, self.burst, NOW), [])

    def test_given_a_busy_mini_then_the_job_is_waiting(self):
        rs = [runner(1, 'macmini-preview', ['self-hosted', 'runner-vm'], busy=True)]
        self.assertEqual(len(A.demand([job(1, ['self-hosted', 'runner-vm'])], rs, self.burst, NOW)), 1)

    def test_given_a_macos_only_job_then_burst_never_counts_it(self):
        self.assertEqual(A.demand([job(1, ['self-hosted', 'aqa-coord'])], [], self.burst, NOW), [])

    def test_given_a_job_younger_than_min_age_then_it_is_not_bought_for_yet(self):
        self.assertEqual(A.demand([job(1, ['self-hosted', 'runner-vm'], age=1)], [], self.burst, NOW), [])

    def test_given_two_jobs_and_one_idle_runner_then_one_waits(self):
        rs = [runner(1, 'macmini-preview', ['self-hosted', 'runner-vm'])]
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

    def test_s3_release_verifies_the_served_file_after_the_revert_step(self):
        text = open(os.path.join(ROOT, '.github', 'workflows', 'neonbee-deploy-s3-release.yml')).read()
        self.assertLess(text.index('name: Revert to previous release'), text.index('name: Verify served artifact is this release'))


if __name__ == '__main__':
    unittest.main()
