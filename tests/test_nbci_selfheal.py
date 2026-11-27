"""BDD tests for nb-ci self-heal, metrics and alert helpers (Pulse 652ffcd8). Pure logic, no network."""
import json, os, re, subprocess, sys, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'runner', 'controller'))
import nbci_logic as L  # noqa: E402

CFG = json.load(open(os.path.join(ROOT, 'ci', 'runner-labels.json')))
CLS = CFG['classes']; BY = {c['name']: c for c in CLS}; PRICES = CFG['prices_usd']
NOW = 1_800_000_000
LJOB = ['self-hosted', 'runner-vm']


def job(i, age, labels=LJOB):
    return {'id': i, 'labels': labels, 'created': NOW - age}


class StuckJobs(unittest.TestCase):
    def test_given_old_served_job_and_no_live_vm_when_checked_then_its_mark_is_cleared(self):
        self.assertEqual(L.stuck_jobs([job(1, 1300)], {'1': NOW - 100}, set(), CLS, NOW), [1])

    def test_given_old_served_job_and_a_live_vm_of_its_class_then_nothing_is_cleared(self):
        self.assertEqual(L.stuck_jobs([job(1, 1300)], {'1': NOW - 100}, {'legacy'}, CLS, NOW), [])

    def test_given_a_young_job_then_it_is_not_stuck(self):
        self.assertEqual(L.stuck_jobs([job(1, 600)], {'1': NOW - 100}, set(), CLS, NOW), [])

    def test_given_an_old_job_never_served_then_it_is_left_to_the_normal_buy(self):
        self.assertEqual(L.stuck_jobs([job(1, 1300)], {}, set(), CLS, NOW), [])

    def test_given_a_job_no_class_serves_then_it_is_ignored(self):
        self.assertEqual(L.stuck_jobs([job(1, 1300, ['macos'])], {'1': NOW}, set(), CLS, NOW), [])

    def test_given_a_controller_hosted_job_then_it_is_ignored(self):
        self.assertEqual(L.stuck_jobs([job(1, 1300, ['self-hosted', 'nb-ctl'])], {'1': NOW}, set(), CLS, NOW), [])


class OrphanDisk(unittest.TestCase):
    D = {'name': 'nbci-gate-1', 'creationTimestamp': '2026-11-27T10:00:00+00:00'}
    T = L.ts('2026-11-27T10:20:00+00:00')

    def test_given_an_unattached_old_nbci_disk_then_it_is_an_orphan(self):
        self.assertTrue(L.orphan_disk(self.D, self.T))

    def test_given_an_aqa_named_disk_then_it_is_an_orphan(self):
        self.assertTrue(L.orphan_disk(dict(self.D, name='aqa-x-1'), self.T))

    def test_given_a_labelled_disk_with_other_name_then_it_is_an_orphan(self):
        self.assertTrue(L.orphan_disk(dict(self.D, name='disk-9', labels={'nbci': '1'}), self.T))

    def test_given_an_attached_disk_then_it_is_not_an_orphan(self):
        self.assertFalse(L.orphan_disk(dict(self.D, users=['vm']), self.T))

    def test_given_a_young_disk_then_it_is_not_an_orphan(self):
        self.assertFalse(L.orphan_disk(self.D, self.T - 900))

    def test_given_a_foreign_disk_then_it_is_not_an_orphan(self):
        self.assertFalse(L.orphan_disk(dict(self.D, name='prod-db'), self.T))


class Warn80(unittest.TestCase):
    def test_given_79_percent_then_no_warning(self):
        self.assertFalse(L.minutes_warn80(2369, 3000))

    def test_given_80_percent_then_warn(self):
        self.assertTrue(L.minutes_warn80(2400, 3000))

    def test_given_the_cap_reached_then_the_cap_alert_owns_it(self):
        self.assertFalse(L.minutes_warn80(3000, 3000))

    def test_given_no_cap_then_no_warning(self):
        self.assertFalse(L.minutes_warn80(5, 0))


class IdleOverdue(unittest.TestCase):
    def vm(self, **k): return dict({'status': 'RUNNING', 'slots': 4, 'created': NOW - 5000}, **k)

    def test_given_idle_for_idle_plus_two_min_and_running_then_overdue(self):
        self.assertTrue(L.idle_overdue(self.vm(), NOW - 300, NOW, 180))

    def test_given_idle_just_over_the_idle_window_then_not_yet(self):
        self.assertFalse(L.idle_overdue(self.vm(), NOW - 200, NOW, 180))

    def test_given_never_busy_then_age_counts_from_creation(self):
        self.assertTrue(L.idle_overdue(self.vm(), None, NOW, 180))

    def test_given_a_stopped_vm_then_not_overdue(self):
        self.assertFalse(L.idle_overdue(self.vm(status='TERMINATED'), NOW - 900, NOW, 180))

    def test_given_a_single_slot_vm_then_not_this_check(self):
        self.assertFalse(L.idle_overdue(self.vm(slots=1), NOW - 900, NOW, 180))


LINE = 'nbci-mem 10:01:02 mem_total=15000 used=9000 avail=5000 swap_used=100 load=3.5 busy=2'


class Memory(unittest.TestCase):
    def test_given_serial_text_when_parsed_then_samples_come_out(self):
        s = L.parse_mem_lines('noise\n' + LINE + '\nmore\n' + LINE.replace('3.5', '7.25'))
        self.assertEqual((len(s), s[0]['used'], s[1]['load'], s[0]['busy']), (2, 9000, 7.25, 2))

    def test_given_a_line_without_swap_then_swap_is_zero(self):
        self.assertEqual(L.parse_mem_lines(LINE.replace(' swap_used=100', ''))[0]['swap'], 0)

    def test_given_no_text_then_no_samples(self):
        self.assertEqual(L.parse_mem_lines(None), [])

    def test_given_samples_when_folded_then_peaks_are_kept_across_calls(self):
        p = L.fold_peaks(None, L.parse_mem_lines(LINE))
        p = L.fold_peaks(p, L.parse_mem_lines(LINE.replace('used=9000', 'used=4000').replace('load=3.5', 'load=1.0')))
        self.assertEqual((p['mem'], p['load']), (9000, 3.5))

    def test_given_low_available_memory_then_pressure(self):
        s = L.parse_mem_lines(LINE.replace('avail=5000', 'avail=1400'))
        self.assertIn('available memory', L.mem_pressure(s))

    def test_given_big_swap_then_pressure(self):
        s = L.parse_mem_lines(LINE.replace('swap_used=100', 'swap_used=2049'))
        self.assertIn('swap', L.mem_pressure(s))

    def test_given_healthy_memory_then_no_pressure(self):
        self.assertIsNone(L.mem_pressure(L.parse_mem_lines(LINE)))

    def test_given_no_samples_then_no_pressure(self):
        self.assertIsNone(L.mem_pressure([]))


class Records(unittest.TestCase):
    def test_given_busy_and_idle_time_when_accumulated_then_both_accounts_grow(self):
        st = L.acc_busy({}, True, 60); L.acc_busy(st, False, 30); L.acc_busy(st, False, -5)
        self.assertEqual(st, {'busy_s': 60, 'idle_s': 30})

    def test_given_a_deleted_vm_then_the_vm_record_has_cost_minutes_and_peaks(self):
        c = BY['legacy']
        stat = {'created': NOW - 3600, 'slots': 4, 'vcpu': c['vcpu'], 'busy_s': 1800, 'idle_s': 600, 'jobs': 5,
                'peaks': {'mem': 9000, 'load': 3.5}}
        r = L.vm_metric(stat, 'nbci-legacy-1', c, PRICES, NOW)
        exp = round(L.hourly_usd(c, PRICES), 4)
        self.assertEqual((r['kind'], r['class'], r['busy_min'], r['idle_min'], r['jobs'], r['peak_mem_mb'], r['peak_load'], r['spot_lost']),
                         ('vm', 'legacy', 30.0, 10.0, 5, 9000, 3.5, False))
        self.assertAlmostEqual(r['cost_usd'], exp, places=3)

    def test_given_a_spot_loss_then_the_flag_is_set_and_missing_peaks_are_null(self):
        r = L.vm_metric({'created': NOW - 60}, 'x', BY['legacy'], PRICES, NOW, spot_lost=True)
        self.assertEqual((r['spot_lost'], r['peak_mem_mb']), (True, None))

    def test_given_a_completed_job_then_queue_and_run_seconds_are_derived(self):
        r = L.job_metric({'repo': 'r', 'name': 'build', 'labels': LJOB, 'runner': 'x', 'conclusion': 'success',
                          'created': 1000, 'started': 1030, 'completed': 1100}, 'legacy', 2000)
        self.assertEqual((r['queued_s'], r['run_s'], r['class'], r['conclusion']), (30, 70, 'legacy', 'success'))

    def test_given_a_job_that_never_started_then_run_is_zero_not_negative(self):
        r = L.job_metric({'repo': 'r', 'name': 'b', 'labels': LJOB, 'created': 1000, 'started': None, 'completed': 900}, None, 2000)
        self.assertEqual((r['queued_s'], r['run_s']), (0, 0))

    def test_given_waiting_jobs_then_the_queue_record_counts_by_class(self):
        r = L.queue_metric([job(1, 60), job(2, 120), job(3, 30, ['macos'])], CLS, NOW, 2, 3)
        self.assertEqual((r['queued_by_class'], r['oldest_s'], r['live_vms'], r['free_slots']), ({'legacy': 2, 'other': 1}, 120, 2, 3))

    def test_given_an_empty_queue_then_oldest_is_zero(self):
        self.assertEqual(L.queue_metric([], CLS, NOW, 0, 0)['oldest_s'], 0)

    def test_given_a_time_then_the_object_path_is_date_kind_hhmm(self):
        self.assertEqual(L.metrics_path('vm', 1_795_000_000 - 1_795_000_000 % 86400 + 3600 * 14 + 60 * 5),
                         'metrics/nbci/%s/vm-1405.jsonl' % __import__('time').strftime('%Y-%m-%d', __import__('time').gmtime(1_795_000_000)))


class Buffer(unittest.TestCase):
    def test_given_records_when_flushed_then_one_object_per_kind_of_jsonl(self):
        up = []
        b = L.MetricsBuffer(lambda p, d: up.append((p, d)), 300)
        b.add({'kind': 'vm', 'a': 1}, NOW); b.add({'kind': 'vm', 'a': 2}, NOW); b.add({'kind': 'pass', 'dur_s': 1}, NOW)
        self.assertEqual(b.flush(NOW), 3)
        self.assertEqual(sorted(p.split('/')[-1].split('-')[0] for p, _ in up), ['pass', 'vm'])
        vm = [d for p, d in up if '/vm-' in p][0].decode().strip().split('\n')
        self.assertEqual([json.loads(x)['a'] for x in vm], [1, 2])

    def test_given_a_flush_within_the_interval_then_nothing_is_uploaded(self):
        up = []; b = L.MetricsBuffer(lambda p, d: up.append(p), 300)
        b.add({'kind': 'pass'}, NOW); b.flush(NOW); b.add({'kind': 'pass'}, NOW + 10)
        self.assertEqual((b.flush(NOW + 100), len(up)), (0, 1))
        self.assertEqual(b.flush(NOW + 301), 1)

    def test_given_an_upload_failure_then_records_return_and_nothing_raises(self):
        def boom(p, d): raise RuntimeError('503')
        b = L.MetricsBuffer(boom, 300); b.add({'kind': 'vm'}, NOW)
        self.assertEqual(b.flush(NOW), 0)
        self.assertEqual(len(b.recs), 1)

    def test_given_more_than_the_cap_then_the_oldest_are_dropped(self):
        b = L.MetricsBuffer(lambda p, d: None, 300, cap=3)
        for i in range(5): b.add({'kind': 'q', 'i': i}, NOW)
        self.assertEqual([r['i'] for r in b.recs], [2, 3, 4])

    def test_given_an_empty_buffer_then_it_is_not_due(self):
        self.assertFalse(L.MetricsBuffer(lambda p, d: None).due(NOW))


class Update(unittest.TestCase):
    def test_given_a_sha_ref_then_the_controller_is_pinned(self):
        self.assertEqual(L.update_action('a' * 40, 'x', 'y', ''), 'pinned')

    def test_given_main_and_a_new_sha_then_update(self):
        self.assertEqual(L.update_action('main', 'a1', 'b2', ''), 'update')

    def test_given_the_same_sha_then_skip(self):
        self.assertEqual(L.update_action('main', 'a1', 'a1', ''), 'skip')

    def test_given_a_sha_that_failed_before_then_skip(self):
        self.assertEqual(L.update_action('main', 'a1', 'b2', 'b2'), 'skip')

    def test_given_no_fetched_sha_then_skip(self):
        self.assertEqual(L.update_action('main', 'a1', '', ''), 'skip')

    def test_given_fresh_heartbeat_after_restart_and_200_then_healthy(self):
        self.assertTrue(L.health_ok(NOW - 60, NOW - 120, NOW, 200, True))

    def test_given_a_heartbeat_older_than_the_restart_then_unhealthy(self):
        self.assertFalse(L.health_ok(NOW - 300, NOW - 120, NOW, 200, True))

    def test_given_a_stale_heartbeat_then_unhealthy(self):
        self.assertFalse(L.health_ok(NOW - 200, NOW - 400, NOW, 200, True))

    def test_given_healthz_503_or_inactive_service_then_unhealthy(self):
        self.assertFalse(L.health_ok(NOW - 10, NOW - 120, NOW, 503, True))
        self.assertFalse(L.health_ok(NOW - 10, NOW - 120, NOW, 200, False))


class Contracts(unittest.TestCase):
    def read(self, *p): return open(os.path.join(ROOT, 'runner', 'controller', *p)).read()

    def test_given_bootstrap_then_it_installs_the_10_min_update_timer_and_override_hooks(self):
        b = self.read('bootstrap.sh')
        for x in ('nbci-update.timer', 'OnUnitInactiveSec=10min', 'NBCI_REF_OVERRIDE', 'NBCI_ONLY_IF_CHANGED', 'bad-ref', 'nbci_update.py'):
            self.assertIn(x, b)
        self.assertEqual(subprocess.run(['bash', '-n', os.path.join(ROOT, 'runner', 'controller', 'bootstrap.sh')]).returncode, 0)

    def test_given_set_ref_then_it_sets_restart_and_migrate_scheduling(self):
        s = self.read('nbci-set-ref.sh')
        self.assertIn('set-scheduling nbci-controller', s); self.assertIn('--restart-on-failure --maintenance-policy=MIGRATE', s)

    def test_given_runbook_then_it_documents_scheduling_and_metrics_path(self):
        r = self.read('RUNBOOK.md')
        self.assertIn('--maintenance-policy=MIGRATE', r); self.assertIn('metrics/nbci/<YYYY-MM-DD>/<kind>-<HHMM>.jsonl', r)

    def test_given_the_unit_then_nbci_service_restarts_always(self):
        self.assertIn('Restart=always', self.read('bootstrap.sh'))

    def test_given_the_loop_then_redeliver_runs_every_30_min_and_restart_is_alerted(self):
        n = self.read('nbci.py')
        self.assertIn("every(st, 'redeliver', 1800, now)", n); self.assertIn("'controller restarted", n)

    def test_given_the_updater_then_it_compiles(self):
        compile(self.read('nbci_update.py'), 'u', 'exec')


if __name__ == '__main__':
    unittest.main()
