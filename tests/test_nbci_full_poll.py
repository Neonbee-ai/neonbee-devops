import os, sys, threading, time, unittest
from unittest import mock

ROOT = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, os.path.join(ROOT, 'runner', 'controller'))
sys.path.insert(0, os.path.join(ROOT, 'runner', 'alert-relay'))
os.environ.setdefault('DRY_RUN', '1')
import nbci  # noqa: E402


def job(i, created):
    return {'id': i, 'repo': 'r', 'name': 'j', 'labels': ['runner-vm'], 'created': created}


class FullPoll(unittest.TestCase):
    def setUp(self):
        self.saved = dict(nbci.EVENTS); nbci.POLL['thread'] = None

    def tearDown(self):
        nbci.EVENTS.clear(); nbci.EVENTS.update(self.saved); nbci.POLL['thread'] = None

    def test_given_polled_jobs_with_integer_ids_then_the_table_is_replaced_without_error(self):
        nbci.EVENTS['jobs'] = {1: job(1, 0)}                   # old webhook job, not seen by the poll: dropped
        with mock.patch.object(nbci, 'queued_jobs', return_value={2: job(2, 0), 3: job(3, 0)}):
            nbci.full_poll(None, {})
        self.assertEqual(sorted(nbci.EVENTS['jobs']), [2, 3])

    def test_given_a_webhook_job_queued_during_the_poll_then_it_survives_the_merge(self):
        def slow(gh, recent):
            nbci.EVENTS['jobs'][9] = job(9, time.time())       # arrives while the poll runs
            return {2: job(2, 0)}
        nbci.EVENTS['jobs'] = {}
        with mock.patch.object(nbci, 'queued_jobs', side_effect=slow):
            nbci.full_poll(None, {})
        self.assertEqual(sorted(nbci.EVENTS['jobs']), [2, 9])

    def test_given_the_poll_fails_then_the_table_is_kept_and_the_next_pass_polls_again(self):
        nbci.EVENTS['jobs'] = {1: job(1, 0)}; nbci.EVENTS['need_full_poll'] = False
        with mock.patch.object(nbci, 'queued_jobs', side_effect=RuntimeError('502')):
            nbci.full_poll(None, {})
        self.assertEqual(list(nbci.EVENTS['jobs']), [1])
        self.assertTrue(nbci.EVENTS['need_full_poll'])

    def test_given_a_poll_already_running_then_no_second_one_starts(self):
        gate = threading.Event()
        with mock.patch.object(nbci, 'queued_jobs', side_effect=lambda g, r: (gate.wait(5), {})[1]):
            self.assertTrue(nbci.start_full_poll(None, {}))
            self.assertFalse(nbci.start_full_poll(None, {}))   # the 30 s loop does not stack polls
            gate.set(); nbci.POLL['thread'].join(5)
            self.assertTrue(nbci.start_full_poll(None, {}))    # finished: the next pass may start one
            nbci.POLL['thread'].join(5)

    def test_given_a_slow_poll_then_starting_it_returns_at_once(self):
        gate = threading.Event()
        with mock.patch.object(nbci, 'queued_jobs', side_effect=lambda g, r: (gate.wait(5), {})[1]):
            t = time.time(); nbci.start_full_poll(None, {})
            self.assertLess(time.time() - t, 1)                # the tick never blocks on GitHub listing
            gate.set(); nbci.POLL['thread'].join(5)


if __name__ == '__main__':
    unittest.main()
