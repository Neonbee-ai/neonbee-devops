"""BDD tests for the production-readiness layer of the nb-ci controller: alerts, reaper, caps, DNS/webhook,
redelivery, CI failure -> Pulse, AQA results, image rebuild, alert relay and the boot/infra contracts (no network)."""
import base64, json, os, sys, time, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'runner', 'controller'))
sys.path.insert(0, os.path.join(ROOT, 'runner', 'alert-relay'))
os.environ.setdefault('DRY_RUN', '1')
import nbci_logic as L  # noqa: E402
import nbci  # noqa: E402
import nbci_aqa as A  # noqa: E402
import relay  # noqa: E402

NOW = 1_800_000_000
CTL = os.path.join(ROOT, 'runner', 'controller')


def read(*p):
    return open(os.path.join(ROOT, *p)).read()


def iso(t):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(t))


def fresh_state():
    return {'alerted': {}, 'last': {}, 'pulse': {'q': [], 'bugs': {}}, 'day': {}, 'release': {}}


class Reaper(unittest.TestCase):
    def test_given_a_runner_vm_past_its_hard_lifetime_then_it_is_deleted_even_if_busy(self):
        why = L.vm_verdict({'status': 'RUNNING', 'created': NOW - 7000}, {'busy': True}, False, NOW, 300, 6300)
        self.assertTrue(why.startswith(L.LIFETIME))

    def test_given_a_busy_runner_vm_inside_its_lifetime_then_it_is_kept(self):
        self.assertIsNone(L.vm_verdict({'status': 'RUNNING', 'created': NOW - 1000}, {'busy': True}, False, NOW, 300, 6300))

    def test_given_a_terminated_vm_then_it_is_deleted_first(self):
        self.assertIn('stopped', L.vm_verdict({'status': 'TERMINATED', 'created': NOW}, None, False, NOW, 300, 6300))

    def test_given_any_aqa_vm_terminated_then_it_is_reaped(self):
        self.assertIn('stopped', L.aqa_reap_verdict({'status': 'TERMINATED', 'created': NOW}, NOW, 7200))

    def test_given_an_aqa_vm_older_than_its_max_lifetime_then_it_is_reaped(self):
        self.assertIn('older', L.aqa_reap_verdict({'status': 'RUNNING', 'created': NOW - 7300}, NOW, 7200))

    def test_given_a_young_running_aqa_vm_then_it_is_kept(self):
        self.assertIsNone(L.aqa_reap_verdict({'status': 'RUNNING', 'created': NOW - 60}, NOW, 7200))

    def test_given_a_managed_instance_group_member_then_the_reaper_leaves_it_to_its_group(self):
        self.assertIsNone(L.aqa_reap_verdict({'status': 'TERMINATED', 'created': 0, 'mig': True}, NOW, 7200))

    def test_given_a_hard_lifetime_verdict_then_delete_vm_deregisters_and_deletes_a_busy_runner(self):
        calls = []
        gh = lambda path, method='GET', body=None: calls.append((method, path))  # noqa: E731
        gcp = type('G', (), {'compute': lambda self, p, m='GET', b=None: calls.append((m, p))})()
        old = nbci.DRY; nbci.DRY = False
        try:
            ok = nbci.delete_vm(gh, gcp, {'name': 'nbci-gate-1', 'zone': 'z', 'status': 'RUNNING'}, {'id': 7, 'busy': True},
                                L.LIFETIME + ' (120 min)')
        finally:
            nbci.DRY = old
        self.assertTrue(ok)
        self.assertEqual([m for m, _ in calls], ['DELETE', 'DELETE'])


class Caps(unittest.TestCase):
    def test_given_live_vms_then_their_minutes_are_counted(self):
        vms = [{'status': 'RUNNING'}, {'status': 'STAGING'}, {'status': 'TERMINATED'}]
        self.assertAlmostEqual(L.vm_minutes(vms, 60), 2.0)

    def test_given_the_daily_vm_minutes_cap_is_reached_then_no_vm_may_start(self):
        self.assertFalse(L.minutes_gate(1500, 1500))
        self.assertTrue(L.minutes_gate(1499, 1500))

    def test_given_a_zero_cap_then_the_minutes_gate_is_off(self):
        self.assertTrue(L.minutes_gate(10 ** 6, 0))

    def test_given_the_runner_classes_then_every_non_deploy_vm_class_is_spot(self):
        cfg = json.loads(read('ci', 'runner-labels.json'))
        for c in cfg['classes']:
            if c.get('host') == 'controller':
                continue
            self.assertEqual(c['spot'], c['name'] not in ('deploy', 'legacy-prod'), c['name'])

    def test_given_an_aqa_vm_body_then_it_is_spot_with_a_delete_backstop(self):
        b = A.vm_body({'machine': 'e2-standard-2', 'startup_script': 'x', 'service_account': 'sa'}, 'aqa-x', 'z', [], 'nbci', 5400)
        self.assertEqual(b['scheduling']['provisioningModel'], 'SPOT')
        self.assertEqual(b['scheduling']['instanceTerminationAction'], 'DELETE')


class Alerts(unittest.TestCase):
    def test_given_one_incident_twice_then_only_one_pulse_comment_is_queued(self):
        st = fresh_state()
        self.assertTrue(nbci.notify(st, 'job a / b queued 16 min'))
        self.assertFalse(nbci.notify(st, 'job a / b queued 17 min'))
        q = [o for o in st['pulse']['q'] if o['task'] == nbci.ALERT_TASK]
        self.assertEqual(len(q), 1)
        self.assertTrue(q[0]['text'].startswith('[nbci] job a / b queued 16 min'))

    def test_given_two_different_incidents_then_both_are_queued(self):
        st = fresh_state()
        nbci.notify(st, 'VM create failed: class gate (quota)', key='create gate quota')
        nbci.notify(st, 'VM create failed: class deploy (quota)', key='create deploy quota')
        self.assertEqual(len(st['pulse']['q']), 2)

    def test_given_the_window_has_passed_then_the_same_incident_alerts_again(self):
        st = fresh_state()
        nbci.notify(st, 'x', key='k', window_s=10)
        st['alerted']['n:k'] -= 11
        self.assertTrue(nbci.notify(st, 'x', key='k', window_s=10))

    def test_given_numbers_and_shas_then_the_alert_key_ignores_them(self):
        self.assertEqual(L.alert_key('AQA failed abc12345 exit 1'), L.alert_key('AQA failed ffe98765 exit 2'))

    def test_given_the_watchdog_then_it_alerts_through_pulse_and_respects_the_guard_marker(self):
        w = read('runner', 'controller', 'watchdog.sh')
        self.assertIn('nbci_alert.py', w)
        self.assertIn('/run/nbci-stopped-by-guard', w)
        self.assertIn('nbci-stopped-by-guard', read('runner', 'controller', 'nbci_guard.py'))


class AqaResults(unittest.TestCase):
    def test_given_exit_zero_then_the_aqa_run_passed(self):
        self.assertEqual(L.aqa_outcome({'exit': 0}, 5), 'pass')

    def test_given_a_non_zero_exit_then_the_aqa_run_failed(self):
        self.assertEqual(L.aqa_outcome({'exit': 1}, 5), 'failed (exit 1)')

    def test_given_no_result_inside_the_grace_then_it_waits(self):
        self.assertEqual(L.aqa_outcome(None, 60), 'wait')

    def test_given_no_result_after_the_grace_then_it_alerts(self):
        self.assertEqual(L.aqa_outcome(None, 901), 'ended without a result')

    def test_given_a_gone_aqa_vm_with_a_failed_result_then_one_alert_is_queued_and_the_run_forgotten(self):
        st = fresh_state()
        st['aqa_runs'] = {'aqa-dev-1': {'repo': 'r', 'sha': 'a' * 40, 'env': 'dev', 'created': NOW, 'tasks': ['3d32de89'],
                                        'evidence': 'gs://b/replay/dev/' + 'a' * 40}}

        class G:
            def compute(self, p, m='GET', b=None): return {'items': []}
            def storage_json(self, b, n): return {'exit': 1}
        nbci.aqa_results(G(), st, NOW)
        self.assertEqual(st['aqa_runs'], {})
        self.assertIn('AQA failed (exit 1)', st['pulse']['q'][0]['text'])

    def test_given_a_pending_gate_then_the_deploy_event_is_kept_for_the_next_pass(self):
        st = fresh_state()
        nbci.EVENTS['aqa'] = [{'repo': 'so360-accounting-fe', 'run': {'id': 1, 'name': 'deploy', 'event': 'push', 'conclusion': 'success',
                               'head_branch': 'feat/x', 'head_sha': 'b' * 40, 'workflow_id': 9, 'html_url': ''}}]

        def gh(path, method='GET', body=None, cache=False):
            if 'check-runs' in path:
                return {'check_runs': [{'name': 'Gate', 'status': 'in_progress', 'conclusion': None}]}
            raise RuntimeError('unexpected ' + path)
        nbci.aqa_trigger(gh, None, st, NOW, '2026-11-27', [])
        if A.gate_blocks([{'name': 'Gate', 'status': 'in_progress', 'conclusion': None}]) == 'gate pending':
            self.assertEqual(len(st['aqa_pending']), 1)

    def test_given_the_vm_minutes_cap_then_deploy_events_wait_instead_of_starting_aqa(self):
        st = fresh_state()
        nbci.EVENTS['aqa'] = [{'repo': 'r', 'run': {'head_sha': 'c' * 40}}]
        nbci.aqa_trigger(lambda *a, **k: {}, None, st, NOW, '2026-11-27', [], vm_room=False)
        self.assertEqual(len(st['aqa_pending']), 1)

    def _dev_event(self, sha='d' * 40):
        nbci.EVENTS['aqa'] = [{'repo': 'so360-forms-be', 'run': {'id': 2, 'name': 'Deploy', 'event': 'push', 'conclusion': 'success',
                               'head_branch': 'develop', 'head_sha': sha, 'workflow_id': 9, 'html_url': ''}}]

    def _gh(self, startup_ok=True):
        def gh(path, method='GET', body=None, cache=False):
            if 'check-runs' in path: return {'check_runs': []}
            if '/runs?' in path: return {'workflow_runs': []}
            if '/commits/main' in path: return {'sha': 'f' * 40}
            if '/commits/' in path: return {'sha': 'd' * 40, 'commit': {'message': 'x\n\nPulse: 652ffcd8'}}
            if '/contents/' in path:
                if not startup_ok: raise RuntimeError('GET contents -> 404 Not Found')
                return {'content': 'IyEvYmluL2Jhc2g='}
            raise RuntimeError('unexpected ' + path)
        return gh

    def test_given_the_startup_script_is_missing_then_the_deploy_event_is_held_not_dropped(self):
        st = fresh_state(); self._dev_event()
        nbci.aqa_trigger(self._gh(startup_ok=False), None, st, NOW, '2026-11-27', [])
        self.assertEqual(len(st['aqa_pending']), 1)

    def test_given_the_bundle_cannot_be_built_then_the_deploy_event_is_held_not_dropped(self):
        st = fresh_state(); self._dev_event()

        class G:
            def storage_exists(self, b, n): raise RuntimeError('GET storage -> 403 denied')
        dry, nbci.DRY = nbci.DRY, False
        try: nbci.aqa_trigger(self._gh(), G(), st, NOW, '2026-11-27', [])
        finally: nbci.DRY = dry
        self.assertEqual(len(st['aqa_pending']), 1)
        self.assertNotIn('aqa_bundle', st)

    def test_given_the_vm_create_hits_quota_then_the_deploy_event_is_held_not_dropped(self):
        st = fresh_state(); self._dev_event(); st['aqa_bundle'] = 'f' * 40

        class G:
            def compute(self, p, m='GET', b=None): raise RuntimeError("POST -> 403 QUOTA_EXCEEDED Quota 'IN_USE_ADDRESSES' exceeded")
        dry, nbci.DRY = nbci.DRY, False
        try: nbci.aqa_trigger(self._gh(), G(), st, NOW, '2026-11-27', [])
        finally: nbci.DRY = dry
        self.assertEqual(len(st['aqa_pending']), 1)
        self.assertNotIn(A.dedupe_key('so360-forms-be', 'd' * 40), st.get('aqa', {}).get('done', {}))

    def test_given_the_bundle_already_exists_then_it_is_not_rebuilt_and_remembered(self):
        st = {}

        class G:
            uploads = 0
            def storage_exists(self, b, n): self.where = (b, n); return True
            def storage_upload(self, *a): G.uploads += 1
        g = G()
        nbci.ensure_bundle(None, g, st, {'bundle_template': 'gs://bkt/aqa/{qa_sha}.tgz', 'qa_repo': 'neonbee-qa'}, 'f' * 40)
        self.assertEqual(g.where, ('bkt', 'aqa/' + 'f' * 40 + '.tgz'))
        self.assertEqual((G.uploads, st['aqa_bundle']), (0, 'f' * 40))
        nbci.ensure_bundle(None, None, st, {}, 'f' * 40)      # cached: no GCS call at all

    def test_given_a_github_tarball_then_the_bundle_keeps_only_the_bundle_sh_selection(self):
        top = 'Neonbee-ai-neonbee-qa-4008e0a/'
        keep = {top + 'aqa/vm-startup.sh': 'aqa/vm-startup.sh', top + 'engine/secret-names.txt': 'engine/secret-names.txt',
                top + 'comb-beeline/e2e/lib/a.mjs': 'comb-beeline/e2e/lib/a.mjs',
                top + 'comb-beeline/e2e/journeys/ai/x.spec.mjs': 'comb-beeline/e2e/journeys/ai/x.spec.mjs',
                top + 'comb-beeline/e2e/package.json': 'comb-beeline/e2e/package.json', top + 'aqa/': 'aqa'}
        drop = [top, top + 'README.md', top + 'aqa/replay.test.mjs', top + 'comb-beeline/e2e/lib/node_modules/p/i.js',
                top + 'comb-beeline/e2e/sessions/s.json', top + 'comb-beeline/e2e/specs/y.mjs', top + 'engine/.aqa-state/x']
        for m, want in keep.items():
            self.assertEqual(A.bundle_path(m), want, m)
        for m in drop:
            self.assertIsNone(A.bundle_path(m), m)
        self.assertEqual(A.gs_split('gs://b/aqa/s.tgz'), ('b', 'aqa/s.tgz'))

    def test_given_the_vite_deploy_then_it_has_no_dead_aqa_bundle_step_so_no_variable_gates_aqa(self):
        wf = read('.github', 'workflows', 'neonbee-deploy-vite-mfe.yml')
        self.assertNotIn('NB_AQA_BUNDLE', wf)

    def test_given_zero_executed_cases_then_the_outcome_is_not_tested_never_pass(self):
        self.assertEqual(L.aqa_outcome({'exit': 4, 'state': 'not_tested'}, 5), 'not_tested')
        self.assertEqual(L.aqa_outcome({'exit': 4}, 5), 'not_tested')

    def test_given_a_not_tested_result_then_it_alerts_like_a_failure(self):
        st = fresh_state()
        st['aqa_runs'] = {'aqa-dev-2': {'repo': 'r', 'sha': 'e' * 40, 'env': 'dev', 'created': NOW, 'tasks': [],
                                        'evidence': 'gs://b/replay/dev/' + 'e' * 40}}

        class G:
            def compute(self, p, m='GET', b=None): return {'items': []}
            def storage_json(self, b, n): return {'exit': 4, 'state': 'not_tested'}
        nbci.aqa_results(G(), st, NOW)
        self.assertIn('AQA not_tested', st['pulse']['q'][0]['text'])

    def test_given_recent_successful_runs_then_the_reconcile_turns_them_into_trigger_events(self):
        iso = lambda t: time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(t))  # noqa: E731
        runs = [{'conclusion': 'success', 'updated_at': iso(NOW - 60), 'repository': {'name': 'so360-forms-fe'}, 'name': 'Deploy', 'head_sha': 'a'},
                {'conclusion': 'success', 'updated_at': iso(NOW - 9000), 'repository': {'name': 'old'}, 'head_sha': 'b'},
                {'conclusion': 'failure', 'updated_at': iso(NOW - 60), 'repository': {'name': 'red'}, 'head_sha': 'c'}]
        ev = A.recent_deploy_events(runs, NOW)
        self.assertEqual([(e['repo'], e['run']['head_sha']) for e in ev], [('so360-forms-fe', 'a')])

    def test_given_duplicate_and_already_run_events_then_one_pending_event_per_new_repo_sha(self):
        e = lambda r, s: {'repo': r, 'run': {'head_sha': s}}  # noqa: E731
        out = A.merge_pending([e('a', '1'), e('a', '1')], [e('a', '1'), e('b', '2'), e('c', '3')], {'c@3': 1})
        self.assertEqual([(x['repo'], x['run']['head_sha']) for x in out], [('a', '1'), ('b', '2')])

    def test_given_max_live_aqa_vms_then_new_deploy_events_queue_instead_of_creating(self):
        st = fresh_state(); self._dev_event(); st['aqa_bundle'] = 'f' * 40
        created = []

        class G:
            def compute(self, p, m='GET', b=None):
                if m == 'POST': created.append(p)
                return {'items': [{'name': 'aqa-dev-%d' % i, 'status': 'RUNNING'} for i in range(3)]}
        dry, nbci.DRY = nbci.DRY, False
        try: nbci.aqa_trigger(self._gh(), G(), st, NOW, '2026-11-27', [])
        finally: nbci.DRY = dry
        self.assertEqual(created, [])
        self.assertEqual(len(st['aqa_pending']), 1)

    def test_given_the_contract_then_aqa_disks_are_pd_standard_outside_the_ssd_quota(self):
        with open(os.path.join(os.path.dirname(__file__), '..', 'ci', 'aqa.json')) as f:
            c = json.load(f)
        b = A.vm_body(c, 'n', 'us-central1-a', [], 'nbci', 5400)
        self.assertTrue(b['disks'][0]['initializeParams']['diskType'].endswith('/pd-standard'))
        self.assertLessEqual(c['max_live'], 4)

    def test_given_the_vm_startup_contract_then_the_relay_and_result_file_are_documented(self):
        self.assertIn('result.json', read('runner', 'controller', 'RUNBOOK.md'))


class CiFailureToPulse(unittest.TestCase):
    def test_given_an_open_pr_then_its_commit_trailers_give_the_task_ids(self):
        def gh(path, method='GET', body=None, cache=False):
            if path.endswith('/pulls'):
                return [{'state': 'open', 'number': 128, 'head': {'ref': 'feat/aqa-flow-3d32de89'}, 'body': ''}]
            if '/pulls/128/commits' in path:
                return [{'commit': {'message': 'fix: x\n\nPulse: 3d32de89'}}]
            raise RuntimeError(path)
        ids, pr = nbci.pulse_refs(gh, fresh_state(), 'so360-accounting-fe', '9' * 40, 'feat/aqa-flow-3d32de89')
        self.assertEqual((ids, pr), (['3d32de89'], 128))

    def test_given_no_pr_then_the_commit_trailer_is_used(self):
        def gh(path, method='GET', body=None, cache=False):
            if path.endswith('/pulls'):
                return []
            return {'commit': {'message': 'feat: y\n\nPulse: 1a2b3c4d'}}
        self.assertEqual(nbci.pulse_refs(gh, fresh_state(), 'r', '8' * 40, 'develop'), (['1a2b3c4d'], 0))

    def test_given_an_empty_pulse_url_in_the_env_then_the_default_endpoint_is_used(self):
        self.assertTrue(nbci.PULSE_URL.startswith('https://'))

    def test_given_the_gate_aggregate_then_it_is_not_reported_twice(self):
        self.assertIn('Gate', nbci.PULSE_SKIP_CHECKS)

    def test_given_a_failed_job_event_then_its_sha_branch_and_url_are_kept(self):
        nbci.EVENTS['failed'] = []
        nbci.on_event('workflow_job', {'action': 'completed', 'repository': {'name': 'r'}, 'workflow_job': {
            'id': 1, 'run_id': 2, 'name': 'UT', 'labels': [], 'conclusion': 'failure', 'runner_name': 'nbci-gate-1',
            'head_sha': 'd' * 40, 'head_branch': 'feat/x', 'html_url': 'u', 'created_at': iso(NOW)}})
        f = nbci.EVENTS['failed'][-1]
        self.assertEqual((f['sha'], f['branch'], f['url']), ('d' * 40, 'feat/x', 'u'))


class Webhook(unittest.TestCase):
    def test_given_the_fixed_host_resolves_to_our_ip_then_it_is_used(self):
        self.assertEqual(L.dns_plan({'1.2.3.4'}, '1.2.3.4'), ('fixed', False))

    def test_given_a_stale_dns_record_then_sslip_is_used_and_an_update_requested(self):
        self.assertEqual(L.dns_plan({'9.9.9.9'}, '1.2.3.4'), ('sslip', True))

    def test_given_failed_deliveries_then_each_guid_is_redelivered_once(self):
        d = [{'id': 1, 'guid': 'a', 'status_code': 502, 'redelivery': False, 'delivered_at': iso(NOW - 60)},
             {'id': 6, 'guid': 'e', 'status_code': 502, 'redelivery': False, 'delivered_at': iso(NOW - 60), 'event': 'workflow_job'},
             {'id': 7, 'guid': 'f', 'status_code': 502, 'redelivery': False, 'delivered_at': iso(NOW - 60), 'event': 'push'},
             {'id': 2, 'guid': 'b', 'status_code': 200, 'redelivery': False, 'delivered_at': iso(NOW - 60)},
             {'id': 3, 'guid': 'c', 'status_code': 0, 'redelivery': False, 'delivered_at': iso(NOW - 90000)},
             {'id': 4, 'guid': 'd', 'status_code': 500, 'redelivery': False, 'delivered_at': iso(NOW - 60)},
             {'id': 5, 'guid': 'd', 'status_code': 200, 'redelivery': True, 'delivered_at': iso(NOW - 30)}]
        for x in d:
            x.setdefault('event', 'workflow_run')
        seen = set()
        self.assertEqual(L.redeliver_ids(d, NOW, seen), [1])      # workflow_job and push are never redelivered
        self.assertEqual(L.redeliver_ids(d, NOW, seen), [])

    def test_given_a_late_queued_job_event_then_it_is_not_taken_as_live_demand(self):
        nbci.EVENTS['jobs'] = {}
        nbci.on_event('workflow_job', {'action': 'queued', 'repository': {'name': 'r'}, 'workflow_job': {
            'id': 11, 'name': 'UT', 'labels': ['self-hosted', 'nb-auto'], 'created_at': iso(time.time() - 3600)}})
        self.assertNotIn(11, nbci.EVENTS['jobs'])

    def test_given_a_fresh_queued_job_event_then_it_is_live_demand(self):
        nbci.EVENTS['jobs'] = {}
        nbci.on_event('workflow_job', {'action': 'queued', 'repository': {'name': 'r'}, 'workflow_job': {
            'id': 12, 'name': 'UT', 'labels': ['self-hosted', 'nb-auto'], 'created_at': iso(time.time() - 5)}})
        self.assertIn(12, nbci.EVENTS['jobs'])

    def test_given_a_redelivered_old_deploy_success_then_no_aqa_event(self):
        nbci.EVENTS['aqa'] = []
        nbci.on_event('workflow_run', {'action': 'completed', 'repository': {'name': 'r'}, 'workflow_run': {
            'id': 1, 'conclusion': 'success', 'updated_at': iso(time.time() - 3 * 3600), 'head_sha': 'a'}})
        self.assertEqual(nbci.EVENTS['aqa'], [])

    def test_given_pulse_calls_then_a_non_default_user_agent_is_sent(self):
        self.assertIn("'User-Agent': 'nb-ci/3.1'", read('runner', 'controller', 'nb_pulse.py'))
        self.assertIn("'User-Agent': 'nbci-alert-relay/1'", read('runner', 'alert-relay', 'relay.py'))

    def test_given_a_get_request_then_healthz_reflects_the_heartbeat(self):
        self.assertTrue(hasattr(nbci.Hook, 'do_GET'))

    def test_given_the_dns_workflow_then_it_runs_on_the_controller_runner_and_never_touches_lb_pools(self):
        # the workflow lives on branch ops/nbci-dns; its contract is mirrored here
        w = read('runner', 'controller', 'ops-nbci-dns.yml')
        self.assertIn('runs-on: [self-hosted, nb-ctl]', w)
        self.assertNotIn('ubuntu-latest', w)
        self.assertNotIn('load_balancers', w)
        self.assertIn('proxied:false', w)


class Bypass(unittest.TestCase):
    def test_given_no_merge_train_then_direct_pushes_raise_no_bypass_alert(self):
        src = read('runner', 'controller', 'nbci.py')
        self.assertIn("if not train or (train != {'*'} and p['_repo'] not in train):", src)


class Image(unittest.TestCase):
    def test_given_an_old_image_and_no_recent_attempt_then_a_build_is_due(self):
        self.assertTrue(L.image_build_due(8, 7, None, NOW))

    def test_given_a_failed_build_an_hour_ago_then_it_is_not_retried_yet(self):
        self.assertFalse(L.image_build_due(8, 7, NOW - 3600, NOW))

    def test_given_a_fresh_image_then_no_build(self):
        self.assertFalse(L.image_build_due(2, 7, None, NOW))

    def test_given_a_build_then_only_the_newest_image_of_the_family_is_kept(self):
        import nbci_logic as L
        imgs = [{'name': 'old', 'creationTimestamp': '2026-11-01'}, {'name': 'new', 'creationTimestamp': '2026-11-02'}]
        self.assertEqual(L.images_to_delete(imgs), ['old'])


class Boot(unittest.TestCase):
    b = read('runner', 'controller', 'bootstrap.sh')

    def test_given_boot_then_the_guard_unit_is_installed_and_enabled(self):
        self.assertIn('nbci-guard.service /etc/systemd/system/', self.b)
        self.assertIn('enable --now caddy nbci nbci-guard', self.b)

    def test_given_boot_then_both_units_restart_always(self):
        self.assertIn('Restart=always', self.b)
        self.assertIn('Restart=always', read('runner', 'controller', 'nbci-guard.service'))

    def test_given_boot_then_caddy_serves_the_fixed_host_on_443_only(self):
        self.assertIn('$HOST, ${IP//./-}.sslip.io', self.b)
        self.assertIn('disable_http_challenge', self.b)
        self.assertIn('handle /healthz', self.b)

    def test_given_boot_then_security_patches_install_automatically(self):
        self.assertIn('Unattended-Upgrade "1"', self.b)
        self.assertIn('Automatic-Reboot "true"', self.b)

    def test_given_boot_then_pulse_alerts_are_on_by_default(self):
        self.assertIn('echo https://pulse.moonhive.in/mcp', self.b)
        self.assertIn('NBCI_ALERT_TASK', self.b)

    def test_given_job_vms_then_the_jit_config_is_scrubbed_from_metadata(self):
        self.assertIn("x['key'] != 'jit'", read('runner', 'controller', 'nbci.py'))


class Relay(unittest.TestCase):
    def test_given_an_open_incident_then_one_comment_text_per_incident_state(self):
        k, t = relay.render({'incident': {'incident_id': 'i1', 'state': 'open', 'policy_name': 'nbci controller down',
                                          'summary': 's', 'url': 'u'}})
        self.assertEqual(k, 'inc-i1-open'); self.assertIn('ALERT OPEN: nbci controller down', t)

    def test_given_a_budget_update_without_a_threshold_then_it_is_ignored(self):
        self.assertEqual(relay.render({'budgetDisplayName': 'b', 'costAmount': 1}), (None, None))

    def test_given_a_budget_threshold_then_it_posts_once_per_month_and_threshold(self):
        k, t = relay.render({'budgetDisplayName': 'b', 'alertThresholdExceeded': 0.9, 'costAmount': 5, 'budgetAmount': 6,
                             'currencyCode': 'INR', 'costIntervalStart': '2026-11-01T00:00:00Z'})
        self.assertEqual(k, 'budget-2026-11-0.9'); self.assertIn('90%', t)

    def test_given_the_same_text_then_the_dedupe_key_is_stable_across_processes(self):
        self.assertEqual(relay.render({'text': 'x'})[0], 'msg-' + __import__('hashlib').sha1(b'x').hexdigest()[:16])

    def test_given_the_deploy_script_then_cloud_run_is_private_and_the_token_is_a_secret_ref(self):
        d = read('runner', 'alert-relay', 'deploy.sh')
        self.assertIn('--no-allow-unauthenticated', d)
        self.assertIn('PULSE_TOKEN=nbci-pulse-token:latest', d)
        self.assertNotIn('--allow-unauthenticated ', d)


if __name__ == '__main__':
    unittest.main()


class AqaBackfill(unittest.TestCase):
    def test_given_a_spooled_deploy_run_then_the_loop_takes_it_once(self):
        import tempfile
        d = tempfile.mkdtemp(); old = nbci.AQA_BACKFILL; nbci.AQA_BACKFILL = os.path.join(d, 'b.json')
        try:
            json.dump([{'repo': 'so360-crm-fe', 'run': {'id': 1}}, {'bad': 1}], open(nbci.AQA_BACKFILL, 'w'))
            got = nbci.take_backfill(NOW)
            self.assertEqual([(e['repo'], e['seen']) for e in got], [('so360-crm-fe', NOW)])
            self.assertEqual(nbci.take_backfill(NOW), [])
        finally:
            nbci.AQA_BACKFILL = old

    def test_given_no_spool_then_nothing_is_taken(self):
        old = nbci.AQA_BACKFILL; nbci.AQA_BACKFILL = '/nonexistent/x.json'
        try: self.assertEqual(nbci.take_backfill(NOW), [])
        finally: nbci.AQA_BACKFILL = old


class JitScrub(unittest.TestCase):
    def test_given_a_listed_but_offline_runner_then_the_jit_config_stays_in_metadata(self):
        src = read('runner', 'controller', 'nbci.py')
        self.assertIn("if v.get('jit') and r and (r.get('status') == 'online' or r.get('busy'))", src)


class AqaIdempotentCreate(unittest.TestCase):
    """Pulse 652ffcd8: AQA creates hit 409 duplicate-name errors; a restart lost the record and the same repo+sha
    was created again, once landing a duplicate in a second zone."""

    def _gh(self):
        def gh(path, method='GET', body=None, cache=False):
            if 'check-runs' in path: return {'check_runs': []}
            if '/runs?' in path: return {'workflow_runs': []}
            if '/commits/main' in path: return {'sha': 'f' * 40}
            if '/commits/' in path: return {'sha': 'd' * 40, 'commit': {'message': 'x\n\nPulse: 652ffcd8'}}
            if '/contents/' in path: return {'content': 'IyEvYmluL2Jhc2g='}
            raise RuntimeError('unexpected ' + path)
        return gh

    def _run(self, G, st):
        nbci.EVENTS['aqa'] = [{'repo': 'so360-forms-be', 'run': {'id': 2, 'name': 'Deploy', 'event': 'push', 'conclusion': 'success',
                               'head_branch': 'develop', 'head_sha': 'd' * 40, 'workflow_id': 9, 'html_url': ''}}]
        st['aqa_bundle'] = 'f' * 40
        dry, nbci.DRY = nbci.DRY, False
        saved, nbci.save_state = nbci.save_state, (lambda s: None)
        load = nbci.L.load        # create path under test: independent of a release-time contract_ready=false hold
        nbci.L.load = lambda p: dict(load(p), contract_ready=True) if p.endswith('aqa.json') else load(p)
        try: nbci.aqa_trigger(self._gh(), G, st, NOW, '2026-11-27', [])
        finally: nbci.DRY = dry; nbci.save_state = saved; nbci.L.load = load

    def test_given_a_vm_with_the_runs_name_already_exists_then_no_create_is_sent_and_the_run_is_recorded(self):
        posts = []

        class G:
            def compute(self, p, m='GET', b=None):
                if m == 'POST': posts.append(p)
                return {'items': [{'name': 'aqa-dev-dddddddd-1', 'status': 'RUNNING'}] if p.startswith('/zones/us-central1-a/') else []}
        st = fresh_state(); self._run(G(), st)
        self.assertEqual(posts, [])
        self.assertIn('so360-forms-be@' + 'd' * 40, st['aqa']['done'])
        self.assertEqual(st['aqa_pending'], [])

    def test_given_the_create_returns_409_then_no_other_zone_is_tried_and_the_run_is_recorded(self):
        posts = []

        class G:
            def compute(self, p, m='GET', b=None):
                if m == 'POST':
                    posts.append(p); raise RuntimeError('POST %s -> 409 {"error": {"code": 409, "errors": [{"reason": "alreadyExists"}]}}' % p)
                return {'items': []}
            def wait_op(self, op, z, t): return op
        st = fresh_state(); self._run(G(), st)
        self.assertEqual(len(posts), 1)
        self.assertIn('so360-forms-be@' + 'd' * 40, st['aqa']['done'])

    def test_given_ci_jobs_waiting_then_a_big_aqa_run_still_gets_the_small_vm(self):
        self.assertEqual(A.sizing(30, ci_waiting=5), ('e2-standard-2', None))
        self.assertEqual(A.sizing(30), ('e2-standard-8', 8))

    def test_given_a_409_text_then_it_classifies_as_exists_not_api(self):
        self.assertEqual(L.classify_gce_error('POST x -> 409 {"reason": "alreadyExists"}'), 'exists')


class PollSeesReruns(unittest.TestCase):
    """Pulse 652ffcd8: a re-run on a repo not pushed for 2 h was dropped from the job table by the slow poll."""

    def _gh(self, pushed):
        iso = lambda t: time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(t))  # noqa: E731

        def gh(path, method='GET', body=None, cache=False):
            if path.startswith('orgs/'):
                return [{'name': 'so360-help-fe', 'full_name': 'Neonbee-ai/so360-help-fe', 'archived': False, 'pushed_at': iso(pushed)}]
            if 'status=queued' in path: return {'workflow_runs': [{'id': 7}]}
            if 'status=in_progress' in path: return {'workflow_runs': []}
            if '/jobs?' in path:
                return {'jobs': [{'id': 1, 'status': 'queued', 'labels': ['self-hosted', 'nb-deploy'], 'name': 'build-deploy',
                                  'created_at': iso(time.time() - 30)}]}
            raise RuntimeError(path)
        return gh

    def test_given_a_rerun_on_a_repo_pushed_long_ago_with_a_recent_webhook_then_the_poll_keeps_its_job(self):
        out = nbci.queued_jobs(self._gh(time.time() - 3 * 86400), {'so360-help-fe': time.time() - 60})
        self.assertEqual(list(out), [1])

    def test_given_no_recent_event_and_no_recent_push_then_the_repo_is_skipped(self):
        self.assertEqual(nbci.queued_jobs(self._gh(time.time() - 3 * 86400), {}), {})


class VmMetricOnce(unittest.TestCase):
    """2026-11-27: a deleted VM stays listed (STOPPING) ~1 min and the idempotent delete re-fires, so one VM was
    recorded 2-3 times in the vm metrics (nbci-legacy-112713115792920ae at 13:47:55, 13:48:25, 13:48:55)."""

    def test_given_a_vm_deleted_on_two_passes_then_one_vm_record_is_written(self):
        st = fresh_state(); by = {c['name']: c for c in nbci.L.load(os.path.join(nbci.CFG, 'runner-labels.json'))['classes']}
        prices = nbci.L.load(os.path.join(nbci.CFG, 'runner-labels.json'))['prices_usd']
        n0 = len(nbci.METRICS.recs)
        for t in (NOW, NOW + 30):
            st.setdefault('vmstat', {})['nbci-legacy-x'] = {'created': NOW - 600, 'slots': 8, 'vcpu': 8, 'cls': 'legacy', 'busy_s': 0, 'idle_s': 0, 'jobs': 0}
            nbci.emit_vm(st, 'nbci-legacy-x', by, prices, t)
        recs = [r for r in nbci.METRICS.recs[n0:] if r.get('vm') == 'nbci-legacy-x']
        self.assertEqual(len(recs), 1)
        self.assertNotIn('nbci-legacy-x', st['vmstat'])      # the re-created bookkeeping entry is still consumed

    def test_given_an_hour_later_then_the_emitted_mark_is_pruned(self):
        st = fresh_state(); st['vm_emitted'] = {'old': NOW - 7200}
        nbci.emit_vm(dict(st, vmstat={}), 'none', {}, {}, NOW)    # no bookkeeping entry: nothing written
        st['vmstat'] = {'nbci-y': {'created': NOW - 60, 'slots': 1, 'vcpu': 2, 'cls': 'gate', 'busy_s': 0, 'idle_s': 0, 'jobs': 0}}
        by = {c['name']: c for c in nbci.L.load(os.path.join(nbci.CFG, 'runner-labels.json'))['classes']}
        nbci.emit_vm(st, 'nbci-y', by, nbci.L.load(os.path.join(nbci.CFG, 'runner-labels.json'))['prices_usd'], NOW)
        self.assertEqual(set(st['vm_emitted']), {'nbci-y'})
