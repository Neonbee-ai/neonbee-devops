#!/usr/bin/env python3
"""Controller-side churn guard (systemd nbci-guard.service, no laptop needed): every 60 s compares live nbci VMs
with queued jobs per class and runs `systemctl stop nbci` + a Teams alert when VMs exceed queued jobs for more
than one reconcile window, or when more than 8 nbci VMs are live. Pure decision: nbci_logic.churn_verdict.
Backstop that runs even while nbci is stopped: a VM with no runner online or busy for GUARD_UNREG_S is deleted
(VM + disk) with an alert, so a VM that can never take a job never keeps billing (nbci_logic.unregistered_vms)."""
import os, subprocess, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import nbci_logic as L  # noqa: E402
import nbci  # noqa: E402
from nbci_io import GCP, GitHub, log  # noqa: E402
import nbci_alert  # noqa: E402

WINDOW_S = int(os.environ.get('GUARD_WINDOW_S', os.environ.get('RECONCILE_S', '300')))
CAP = int(os.environ.get('GUARD_MAX_VMS', '8'))
UNREG_S = max(int(os.environ.get('GUARD_UNREG_S', str(L.UNREGISTERED_S))), L.NO_JOB_STARTUP_S + 60)


def class_of(job, classes):
    c = L.class_for(job['labels'], classes)
    return c['name'] if c and c.get('host') != 'controller' else None


def once(gh, gcp, classes, since, now, seen=None):
    vms = nbci.our_vms(gcp)
    runners = {r['name']: r for r in nbci.runners(gh)}
    reap_unregistered(gh, gcp, vms, runners, {} if seen is None else seen, now)
    queued = {}
    for j in nbci.queued_jobs(gh).values():
        c = class_of(j, classes)
        if c:
            queued[c] = queued.get(c, 0) + 1
    return L.churn_verdict(vms, runners, queued, since, now, WINDOW_S, CAP, L.NO_JOB_STARTUP_S)  # booting VMs are not churn


def reap_unregistered(gh, gcp, vms, runners, seen, now):
    """Delete (and alert on) VMs with no live runner past UNREG_S. Returns the names deleted."""
    gone = []
    for v, why in L.unregistered_vms(vms, runners, seen, now, UNREG_S):
        try:
            if nbci.delete_vm(gh, gcp, v, L.vm_runners(v, runners), why):
                gone.append(v['name']); seen.pop(v['name'], None)
                nbci_alert.send('guard deleted %s (%s): %s' % (v['name'], v.get('cls'), why), gcp, nbci.DRY)
        except Exception as e:
            log('guard delete failed', v['name'], str(e)[:200])
    return gone


def main():
    gcp = GCP(nbci.PROJECT); gh = GitHub(gcp)
    classes = L.load(os.path.join(nbci.CFG, 'runner-labels.json'))['classes']
    since, seen = {}, {}
    log('guard started window=%ds cap=%d unreg=%ds' % (WINDOW_S, CAP, UNREG_S))
    while True:
        try:
            why = once(gh, gcp, classes, since, time.time(), seen)
            if why:
                log('GUARD TRIPPED', why)
                if not nbci.DRY:
                    open('/run/nbci-stopped-by-guard', 'w').write(why[:200])   # watchdog.sh must not restart it
                    subprocess.run(['systemctl', 'stop', 'nbci'], check=False)
                nbci_alert.send('nbci STOPPED by the churn guard: %s. Restart: sudo systemctl start nbci (after checking VMs)' % why, gcp, nbci.DRY)
                since.clear(); time.sleep(600)
        except Exception as e:
            log('guard error', str(e)[:200])
        time.sleep(60)


if __name__ == '__main__':
    main()
