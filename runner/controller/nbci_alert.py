#!/usr/bin/env python3
"""Direct alert for processes outside the nbci loop (churn guard, watchdog): one comment on the Pulse task
"[nbci] alerts" (NBCI_ALERT_TASK) with the Pulse token from Secret Manager (nbci-pulse-token), plus Teams when a
webhook secret is filled. Never raises; prints what it did. Usage: nbci_alert.py "text" """
import os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import nb_pulse  # noqa: E402
from nbci_io import GCP, log, teams  # noqa: E402

E = os.environ.get


def send(text, gcp=None, dry=None):
    dry = (E('DRY_RUN', '1') == '1') if dry is None else dry
    task = E('NBCI_ALERT_TASK', '71b8be5b')
    msg = '[nbci] %s (%s)' % (text, time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime()))
    try:
        gcp = gcp or GCP(E('GCP_PROJECT', 'gen-lang-client-0521938031'))
        if not E('TEAMS_WEBHOOK'):
            try: os.environ['TEAMS_WEBHOOK'] = gcp.secret('nbci-teams-webhook')
            except Exception: pass
        teams(text, dry)
        if dry or not task:
            log('DRY alert', msg); return False
        nb_pulse.MCP(E('PULSE_MCP_URL') or 'https://pulse.moonhive.in/mcp', gcp.secret('nbci-pulse-token')).call(
            'add_comment', {'task_id': task, 'message': msg})
        log('alert sent', task); return True
    except Exception as e:
        log('alert failed', str(e)[:200]); return False


if __name__ == '__main__':
    send(' '.join(sys.argv[1:]) or 'test alert')
