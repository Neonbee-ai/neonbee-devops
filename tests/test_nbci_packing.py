"""BDD tests for nb-ci packing (Pulse 652ffcd8): slots per VM, quota-driven scale-up, VM reuse + idle delete,
the sweeper and the packed VM boot script. Pure logic, no network."""
import json, os, re, subprocess, sys, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'runner', 'controller'))
import nbci_logic as L  # noqa: E402

CFG = json.load(open(os.path.join(ROOT, 'ci', 'runner-labels.json')))
CLS = CFG['classes']; BY = {c['name']: c for c in CLS}
LEGACY = BY['legacy']; PROD = BY['legacy-prod']
NOW = 1_800_000_000
LJOB = ['self-hosted', 'runner-vm']
PJOB = ['self-hosted', 'runner-vm', 'lane-prod']
MAX_S, JOB_S = 4 * 3600, 5400
# live quota today: controller (e2-micro) holds 1 IP + 2 vCPU of the 32 / 8
QUOTA = {'CPUS_ALL_REGIONS': (32, 2), 'CPUS': (200, 2), 'E2_CPUS': (24, 0), 'IN_USE_ADDRESSES': (8, 1),
         'INSTANCES': (24, 1), 'SSD_TOTAL_GB': (500, 10), 'MAX_VCPU': (32, 0)}


def jobs(n, labels=LJOB, start=1):
    return [{'id': i, 'labels': labels} for i in range(start, start + n)]


def pvm(name='nbci-legacy-1', age=600, status='RUNNING', slots=4, names=None, jits=()):
    return {'name': name, 'status': status, 'created': NOW - age, 'cls': 'legacy', 'slots': slots,
            'slot_names': names if names is not None else {i: '%s-s%d-a' % (name, i) for i in range(1, slots + 1)},
            'slot_jits': set(jits)}


def online(*names, busy=()):
    return {n: {'name': n, 'status': 'online', 'busy': n in busy} for n in names}


class Config(unittest.TestCase):
    def test_given_the_measured_load_then_legacy_vms_carry_four_slots_and_deploy_lanes_one(self):
        self.assertEqual((LEGACY['machine'], L.slots_of(LEGACY)), ('e2-standard-4', 4))
        self.assertEqual(L.slots_of(PROD), 1)
        self.assertFalse(L.packed(PROD))

    def test_given_no_slots_key_then_a_class_keeps_one_job_per_vm(self):
        self.assertEqual(L.slots_of(BY['gate']), 1)

    def test_the_aqa_reserve_is_one_vm(self):
        self.assertEqual(CFG['quota']['reserve_aqa'], {'vcpu': 4, 'ips': 1})


class SlotMath(unittest.TestCase):
    def test_given_eight_queued_jobs_and_no_vms_then_two_packed_vms_are_bought(self):
        self.assertEqual([c['name'] for c in L.plan_packed(jobs(8), {}, CLS)], ['legacy', 'legacy'])

    def test_given_nine_queued_jobs_then_a_partial_vm_is_still_bought(self):
        self.assertEqual(len(L.plan_packed(jobs(9), {}, CLS)), 3)

    def test_given_free_slots_on_live_vms_then_they_are_used_before_buying(self):
        self.assertEqual(L.plan_packed(jobs(3), {'legacy': 3}, CLS), [])
        self.assertEqual(len(L.plan_packed(jobs(5), {'legacy': 3}, CLS)), 1)

    def test_given_a_deploy_lane_job_then_its_standard_vm_is_bought_first_one_per_job(self):
        out = L.plan_packed(jobs(4) + jobs(2, PJOB, 10), {}, CLS)
        self.assertEqual([c['name'] for c in out], ['legacy-prod', 'legacy-prod', 'legacy'])

    def test_given_a_packed_vm_then_one_create_serves_four_queued_jobs(self):
        want = L.plan_packed(jobs(8), {}, CLS)
        out, ids = L.cap_creates(want, jobs(8), {}, NOW)
        self.assertEqual((len(out), ids), (2, list(range(1, 9))))

    def test_given_jobs_already_served_in_the_window_then_no_second_packed_vm(self):
        served = {str(i): NOW - 60 for i in range(1, 5)}
        out, ids = L.cap_creates(L.plan_packed(jobs(4), {}, CLS), jobs(4), served, NOW)
        self.assertEqual((out, ids), ([], []))

    def test_given_one_job_vms_then_cap_creates_keeps_one_create_per_job(self):
        want = L.plan(jobs(1, ['self-hosted', 'nb-auto']), {}, CLS) * 3
        out, ids = L.cap_creates(want, jobs(1, ['self-hosted', 'nb-auto']), {}, NOW)
        self.assertEqual((len(out), ids), (1, [1]))


class ControllerChoke(unittest.TestCase):
    """Route/notify/Gate-summary jobs must not queue behind the 2 controller runners (30-45 min, 2026-11-27)."""

    def test_given_a_bare_nb_route_job_then_it_never_buys_a_vm(self):
        self.assertEqual(L.class_for(['self-hosted', 'nb-route'], CLS).get('host'), 'controller')
        self.assertEqual(L.plan_packed(jobs(5, ['self-hosted', 'nb-route']), {}, CLS), [])

    def test_given_a_live_packed_vm_then_its_slots_can_also_take_nb_route_jobs(self):
        self.assertIn('nb-route', LEGACY['labels'])
        self.assertNotIn('nb-route', PROD['labels'])      # deploy lanes stay one job per fresh VM

    def test_given_the_controller_then_the_loop_outranks_its_runner_jobs(self):
        b = open(os.path.join(ROOT, 'runner', 'controller', 'bootstrap.sh')).read()
        self.assertIn('CPUWeight=1000', b)
        self.assertIn('CPUWeight=20', b)
        self.assertNotIn('try-restart "actions.runner', b)   # never kill a running job to apply limits


class Supply(unittest.TestCase):
    def test_given_a_packed_vm_with_one_busy_slot_then_three_slots_are_supply(self):
        v = pvm(); rs = online(*v['slot_names'].values(), busy=(v['slot_names'][1],))
        self.assertEqual(L.free_slots(v, LEGACY, rs, NOW, MAX_S, JOB_S), 3)

    def test_given_a_booting_packed_vm_then_all_its_slots_are_supply(self):
        v = pvm(age=60, status='STAGING')
        self.assertEqual(L.free_slots(v, LEGACY, {}, NOW, MAX_S, JOB_S), 4)

    def test_given_a_vm_past_its_refill_window_then_only_its_online_idle_runners_count(self):
        v = pvm(age=MAX_S - JOB_S + 1); n = list(v['slot_names'].values())
        rs = online(n[0], n[1], busy=(n[0],))
        self.assertEqual(L.free_slots(v, LEGACY, rs, NOW, MAX_S, JOB_S), 1)

    def test_given_a_stopped_vm_then_it_offers_nothing(self):
        self.assertEqual(L.free_slots(pvm(status='TERMINATED'), LEGACY, {}, NOW, MAX_S, JOB_S), 0)


class Quota(unittest.TestCase):
    def test_given_todays_quota_then_six_legacy_vms_fit_and_one_aqa_vm_stays_free(self):
        room = L.capacity(QUOTA, {}, CFG['quota']['reserve_aqa'])
        n = 0
        while L.fits(LEGACY, room):
            room = L.take(LEGACY, room); n += 1
        self.assertEqual(n, 6)                       # 6 x 4 slots = 24 parallel jobs
        self.assertEqual((room['vcpu'], room['ips']), (2, 0))

    def test_given_ips_are_the_limit_then_no_vm_starts_without_a_free_ip(self):
        q = dict(QUOTA, IN_USE_ADDRESSES=(8, 7))
        self.assertFalse(L.fits(LEGACY, L.capacity(q, {}, {'vcpu': 4, 'ips': 1})))

    def test_given_a_live_aqa_vm_then_it_uses_the_reserve_rather_than_doubling_it(self):
        q = dict(QUOTA, CPUS_ALL_REGIONS=(32, 6), CPUS=(200, 6), IN_USE_ADDRESSES=(8, 2))
        self.assertEqual(L.capacity(q, {'vcpu': 4, 'ips': 1}, {'vcpu': 4, 'ips': 1})['ips'], 6)
        self.assertEqual(L.capacity(q, {}, {'vcpu': 4, 'ips': 1})['ips'], 5)

    def test_given_other_vms_take_quota_then_room_shrinks_from_live_usage(self):
        q = dict(QUOTA, CPUS_ALL_REGIONS=(32, 22), CPUS=(200, 22))
        self.assertEqual(L.capacity(q, {}, {'vcpu': 4, 'ips': 1})['vcpu'], 6)

    def test_given_the_controller_vcpu_ceiling_then_it_caps_the_quota(self):
        q = dict(QUOTA, MAX_VCPU=(16, 0))
        self.assertEqual(L.capacity(q, {}, {'vcpu': 0, 'ips': 0})['vcpu'], 14)

    def test_given_ssd_or_instances_exhausted_then_nothing_fits(self):
        self.assertFalse(L.fits(LEGACY, L.capacity(dict(QUOTA, SSD_TOTAL_GB=(500, 480)))))
        self.assertFalse(L.fits(LEGACY, L.capacity(dict(QUOTA, INSTANCES=(24, 24)))))


class IdleDelete(unittest.TestCase):
    def test_given_a_busy_slot_then_the_vm_is_kept_however_long_it_was_idle_before(self):
        self.assertIsNone(L.pool_verdict(pvm(), LEGACY, [{'busy': True}], NOW - 3600, NOW))

    def test_given_no_busy_slot_for_three_minutes_then_the_vm_is_deleted(self):
        self.assertIn('idle 180s', L.pool_verdict(pvm(), LEGACY, [{'busy': False}], NOW - 180, NOW))

    def test_given_two_minutes_idle_then_the_vm_keeps_taking_jobs(self):
        self.assertIsNone(L.pool_verdict(pvm(), LEGACY, [{'busy': False}], NOW - 120, NOW))

    def test_given_a_vm_still_provisioning_then_it_gets_the_boot_grace(self):
        self.assertIsNone(L.pool_verdict(pvm(age=600), LEGACY, [], None, NOW))
        self.assertIn('no job', L.pool_verdict(pvm(age=L.NO_JOB_STARTUP_S), LEGACY, [], None, NOW))

    def test_given_the_hard_lifetime_then_the_vm_goes_busy_or_not(self):
        self.assertIn(L.LIFETIME, L.pool_verdict(pvm(age=MAX_S), LEGACY, [{'busy': True}], NOW, NOW, max_s=MAX_S))

    def test_given_a_stopped_vm_then_it_is_deleted(self):
        self.assertIn('disk still bills', L.pool_verdict(pvm(status='TERMINATED'), LEGACY, [], NOW, NOW))


class Refill(unittest.TestCase):
    def test_given_a_slot_whose_job_finished_then_it_gets_a_fresh_config(self):
        v = pvm(); n = v['slot_names']
        rs = online(n[2], n[3], n[4]); done = {n[1]}
        self.assertEqual(L.slot_actions(v, LEGACY, rs, done, {}, NOW, MAX_S, JOB_S)[0], [1])

    def test_given_an_ephemeral_runner_gone_from_github_then_its_slot_is_refilled(self):
        v = pvm(); n = v['slot_names']
        self.assertEqual(L.slot_actions(v, LEGACY, online(n[1], n[2], n[3]), set(), {}, NOW, MAX_S, JOB_S)[0], [4])

    def test_given_a_config_issued_seconds_ago_then_listing_lag_never_replaces_it(self):
        v = pvm(); n = v['slot_names']
        refill, _ = L.slot_actions(v, LEGACY, online(n[1], n[2], n[3]), set(), {n[4]: NOW - 10}, NOW, MAX_S, JOB_S)
        self.assertEqual(refill, [])

    def test_given_the_refill_window_closed_then_no_slot_takes_a_new_job(self):
        v = pvm(age=MAX_S - JOB_S); n = v['slot_names']
        self.assertEqual(L.slot_actions(v, LEGACY, {}, set(n.values()), {}, NOW, MAX_S, JOB_S)[0], [])

    def test_given_a_runner_online_then_its_config_is_scrubbed_from_metadata(self):
        v = pvm(jits=(1, 2)); n = v['slot_names']
        _, scrub = L.slot_actions(v, LEGACY, online(*n.values(), busy=(n[2],)), set(), {}, NOW, MAX_S, JOB_S)
        self.assertEqual(scrub, [1, 2])

    def test_given_a_runner_that_never_came_online_then_it_is_replaced_after_the_boot_grace(self):
        v = pvm(age=L.NO_JOB_STARTUP_S + 60); n = v['slot_names']
        rs = dict(online(n[2], n[3], n[4]), **{n[1]: {'name': n[1], 'status': 'offline', 'busy': False}})
        self.assertEqual(L.slot_actions(v, LEGACY, rs, set(), {n[1]: NOW - L.NO_JOB_STARTUP_S}, NOW, MAX_S, JOB_S)[0], [1])
        young = pvm(age=300)
        self.assertEqual(L.slot_actions(young, LEGACY, rs, set(), {}, NOW, MAX_S, JOB_S)[0], [])

    def test_slot_runner_names_are_unique_short_and_map_back_to_their_vm(self):
        a, b = L.slot_name('nbci-legacy-1127063231199', 3, NOW), L.slot_name('nbci-legacy-1127063231199', 3, NOW + 1)
        self.assertNotEqual(a, b)
        self.assertLessEqual(len(a), 64)
        self.assertEqual(L.vm_of(a, {'nbci-legacy-1127063231199'}), 'nbci-legacy-1127063231199')
        self.assertIsNone(L.vm_of(a, {'nbci-legacy-other'}))
        self.assertEqual(L.vm_of('nbci-gate-1', {'nbci-gate-1'}), 'nbci-gate-1')


class Sweeper(unittest.TestCase):
    def vm(self, name, age=3600, status='RUNNING', mig=False):
        return {'name': name, 'status': status, 'created': NOW - age, 'mig': mig}

    def test_the_controller_and_mig_members_are_never_swept(self):
        self.assertIsNone(L.sweep_verdict(self.vm('nbci-controller', status='TERMINATED'), [], NOW))
        self.assertIsNone(L.sweep_verdict(self.vm('aqa-dev-1', status='TERMINATED', mig=True), [], NOW))

    def test_vms_that_are_not_nbci_or_aqa_are_never_swept(self):
        self.assertIsNone(L.sweep_verdict(self.vm('prod-web-1', status='TERMINATED'), [], NOW))

    def test_given_a_stopped_vm_then_it_is_swept(self):
        self.assertIn('stopped', L.sweep_verdict(self.vm('aqa-dev-1', status='TERMINATED'), [], NOW))

    def test_given_a_registered_runner_that_is_busy_then_the_vm_is_kept(self):
        self.assertIsNone(L.sweep_verdict(self.vm('aqa-dev-1'), [{'busy': True}], NOW))

    def test_given_no_busy_runner_past_boot_grace_plus_idle_window_then_it_is_swept(self):
        grace = L.NO_JOB_STARTUP_S + L.IDLE_S
        self.assertIsNone(L.sweep_verdict(self.vm('aqa-dev-1', age=grace - 1), [{'busy': False}], NOW))
        self.assertIn('no busy runner', L.sweep_verdict(self.vm('aqa-dev-1', age=grace), [{'busy': False}], NOW))

    def test_given_an_aqa_replay_vm_without_runner_then_only_its_max_age_applies(self):
        self.assertIsNone(L.sweep_verdict(self.vm('aqa-run-1', age=3600), [], NOW))
        self.assertIn('older', L.sweep_verdict(self.vm('aqa-run-1', age=7200), [], NOW))

    def test_given_an_unowned_nbci_vm_without_runner_then_it_is_swept_after_the_grace(self):
        self.assertIn('unowned', L.sweep_verdict(self.vm('nbci-legacy-x', age=L.NO_JOB_STARTUP_S + L.IDLE_S), [], NOW))

    def test_offline_registrations_without_a_vm_are_removed_and_the_controllers_never(self):
        off = lambda n: {'name': n, 'status': 'offline', 'busy': False}  # noqa: E731
        live = {'nbci-legacy-1'}
        self.assertTrue(L.stale_runner(off('nbci-legacy-gone-s1-abc'), live))
        self.assertTrue(L.stale_runner(off('aqa-dev-7'), live))
        self.assertFalse(L.stale_runner(off('nbci-legacy-1-s2-abc'), live))      # slot of a live (booting) VM
        self.assertFalse(L.stale_runner(off('nbci-ctl-1'), live))
        self.assertFalse(L.stale_runner(off('macmini-preview'), live))
        self.assertFalse(L.stale_runner({'name': 'nbci-x', 'status': 'offline', 'busy': True}, live))


class Guard(unittest.TestCase):
    def test_given_packed_vms_with_a_busy_slot_then_they_are_not_idle(self):
        v = dict(pvm(), cls='legacy'); n = v['slot_names']
        self.assertIsNone(L.churn_verdict([v], online(*n.values(), busy=(n[1],)), {}, {}, NOW + 9999))

    def test_given_booting_vms_then_the_guard_waits_for_the_boot_grace(self):
        v = pvm(age=60); since = {}
        for t in (0, 400, 800):
            self.assertIsNone(L.churn_verdict([v], {}, {}, since, NOW + t - 600 + 60, 300, 8, L.NO_JOB_STARTUP_S))


class BootScript(unittest.TestCase):
    PROV = open(os.path.join(ROOT, 'runner', 'image', 'provision.sh')).read()
    SLOTS = open(os.path.join(ROOT, 'runner', 'image', 'slots.sh')).read()

    def plan(self):
        return L.boot_plan('image', 'fam-link', self.PROV, None, [(1, 'n1', 'J1'), (2, 'n2', 'J2')], self.SLOTS)

    def test_given_a_packed_vm_then_it_boots_stock_ubuntu_never_a_custom_image(self):
        self.assertEqual(self.plan()[0], L.STOCK_IMAGE)

    def test_given_a_packed_vm_then_each_slot_has_its_config_and_name_in_metadata(self):
        md = {i['key']: i['value'] for i in self.plan()[1]}
        self.assertEqual((md['jit-1'], md['jit-2'], md['slot-1'], md['slot-2']), ('J1', 'J2', 'n1', 'n2'))
        self.assertNotIn('jit', md)
        self.assertIn('export NB_SLOTS=2', md['startup-script'])
        self.assertIn('trap poweroff ERR', md['startup-script'])
        self.assertLess(sum(len(i['value']) for i in self.plan()[1]), 256 * 1024)

    def test_each_slot_is_isolated_per_user_with_no_metadata_access_and_no_sudo(self):
        s = self.SLOTS
        self.assertIn('U=nbs$i; D=/opt/slots/$i', s)
        self.assertIn('--uid-owner "$U" -d 169.254.169.254 -j REJECT', s)
        self.assertIn('PrivateTmp=yes', s)
        self.assertIn('pkill -9 -u "$U"', s)
        self.assertIn('rm -rf "$D/_work"', s)
        self.assertNotIn('sudoers', s)
        self.assertNotRegex(s, r'(?i)(token|secret|password)=')

    def test_the_slot_script_parses(self):
        r = subprocess.run(['bash', '-n', '-c', self.SLOTS], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


class Workflows(unittest.TestCase):
    def wf(self, n):
        return open(os.path.join(ROOT, '.github', 'workflows', n)).read()

    def test_pr_gate_jobs_cancel_superseded_pr_runs_only(self):
        t = self.wf('neonbee-pr-gate.yml')
        for job in ('unit', 'security', 'contract-tests', 'integration-tests'):
            g = "group: nb-prgate-${{ github.repository }}-${{ github.workflow }}-" \
                "${{ github.event_name == 'pull_request' && github.ref || github.run_id }}-%s\n" % job
            self.assertIn(g + "      cancel-in-progress: ${{ github.event_name == 'pull_request' }}", t)
        p = self.wf('neonbee-deploy-prod.yml')
        for job in ('gate', 'contract-tests', 'integration-tests'):
            self.assertIn("|| github.run_id }}-%s\n      cancel-in-progress: ${{ github.event_name == 'pull_request' }}" % job, p)

    def test_deploy_jobs_are_never_cancelled_mid_deploy(self):
        for n in ('neonbee-deploy-backend.yml', 'neonbee-deploy-nextjs.yml', 'neonbee-deploy-vite-mfe.yml'):
            t = self.wf(n)
            self.assertRegex(t, r"group: neonbee-deploy-\$\{\{ inputs.service_name \}\}-\$\{\{ github.ref_name \}\}\n\s+cancel-in-progress: false")
            self.assertNotIn("cancel-in-progress: ${{ github.ref != 'refs/heads/main' }}", t)
        self.assertIn('cancel-in-progress: false', self.wf('neonbee-deploy-prod.yml'))


class Preemption(unittest.TestCase):
    def f(self, **k):
        return dict({'repo': 'r', 'run': 7, 'attempt': 1, 'conclusion': 'failure', 'runner': 'nbci-legacy-9-s2-abc'}, **k)

    def test_given_a_vm_gone_without_our_delete_then_it_is_lost(self):
        self.assertEqual(L.lost_vms({'a': 1, 'b': 1}, {'a'}, {}, set()), ['b'])

    def test_given_a_vm_we_deleted_then_it_is_not_lost(self):
        self.assertEqual(L.lost_vms({'a': 1, 'b': 1}, {'a'}, {'b': 1}, set()), [])

    def test_given_a_known_vm_found_stopped_then_it_is_lost(self):
        self.assertEqual(L.lost_vms({'a': 1}, {'a'}, {}, {'a'}), ['a'])

    def test_given_a_job_that_died_with_a_preempted_packed_vm_then_its_run_is_retried_once(self):
        self.assertTrue(L.preempt_retry(self.f(), {'nbci-legacy-9'}, {}))
        self.assertTrue(L.preempt_retry(self.f(conclusion='cancelled', runner='nbci-legacy-9'), {'nbci-legacy-9'}, {}))
        self.assertFalse(L.preempt_retry(self.f(), {'nbci-legacy-9'}, {'7': 1}))     # once per run
        self.assertFalse(L.preempt_retry(self.f(attempt=2), {'nbci-legacy-9'}, {}))  # a re-run that fails again counts

    def test_given_a_job_failed_on_a_healthy_vm_then_no_retry(self):
        self.assertFalse(L.preempt_retry(self.f(), {'nbci-legacy-other'}, {}))
        self.assertFalse(L.preempt_retry(self.f(conclusion='success'), {'nbci-legacy-9'}, {}))


class FastBoot(unittest.TestCase):
    PROV = open(os.path.join(ROOT, 'runner', 'image', 'provision.sh')).read()

    def test_provisioning_runs_independent_installs_in_parallel_and_fails_on_any_group(self):
        self.assertEqual(self.PROV.count(') & P'), 4)
        self.assertIn('wait $PA; wait $PB; wait $PC; wait $PD', self.PROV)
        a = self.PROV[self.PROV.index('( # A (apt)'):self.PROV.index(') & PA=$!')]
        self.assertIn('installdependencies.sh', a)       # every apt user stays in one group (apt lock)
        self.assertIn('playwright@1 install-deps', a)
        self.assertNotIn('apt-get', self.PROV[self.PROV.index(') & PA=$!'):self.PROV.index('wait $PA')])

    def test_provision_still_parses(self):
        r = subprocess.run(['bash', '-n', '-c', self.PROV], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_packed_vms_log_memory_to_the_serial_console(self):
        t = open(os.path.join(ROOT, 'runner', 'image', 'slots.sh')).read()
        self.assertIn('nbci-mem', t)
        self.assertIn('> /dev/ttyS0', t)


if __name__ == '__main__':
    unittest.main()
