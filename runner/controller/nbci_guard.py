#!/usr/bin/env python3
"""Controller-side churn guard (systemd nbci-guard.service, no laptop needed): every 60 s compares live nbci VMs
with queued jobs per class and runs `systemctl stop nbci` + a Teams alert when VMs exceed queued jobs for more
than one reconcile window, or when more than 8 nbci VMs are live. Pure decision: nbci_logic.churn_verdict."""
import os, subprocess, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import nbci_logic as L  # noqa: E402
import nbci  # noqa: E402
from nbci_io import GCP, GitHub, log, teams  # noqa: E402

WINDOW_S = int(os.environ.get('GUARD_WINDOW_S', os.environ.get('RECONCILE_S', '300')))
CAP = int(os.environ.get('GUARD_MAX_VMS', '8'))


def class_of(job, classes):
    c = L.class_for(job['labels'], classes)
    return c['name'] if c and c.get('host') != 'controller' else None


def once(gh, gcp, classes, since, now):
    vms = nbci.our_vms(gcp)
    runners = {r['name']: r for r in nbci.runners(gh)}
    queued = {}
    for j in nbci.queued_jobs(gh).values():
        c = class_of(j, classes)
        if c:
            queued[c] = queued.get(c, 0) + 1
    return L.churn_verdict(vms, runners, queued, since, now, WINDOW_S, CAP)


def main():
    gcp = GCP(nbci.PROJECT); gh = GitHub(gcp)
    classes = L.load(os.path.join(nbci.CFG, 'runner-labels.json'))['classes']
    since = {}
    log('guard started window=%ds cap=%d' % (WINDOW_S, CAP))
    while True:
        try:
            why = once(gh, gcp, classes, since, time.time())
            if why:
                log('GUARD TRIPPED', why)
                if not nbci.DRY:
                    subprocess.run(['systemctl', 'stop', 'nbci'], check=False)
                teams('nbci STOPPED by the churn guard: %s' % why, nbci.DRY)
                since.clear(); time.sleep(600)
        except Exception as e:
            log('guard error', str(e)[:200])
        time.sleep(60)


if __name__ == '__main__':
    main()
