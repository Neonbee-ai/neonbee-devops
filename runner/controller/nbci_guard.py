#!/usr/bin/env python3
"""Controller-side churn guard (systemd nbci-guard.service, no laptop needed): every 60 s compares live nbci VMs
with queued jobs per class. When VMs exceed queued jobs for more than one reconcile window, or more than 8 nbci
VMs are live, it DELETES the excess idle VMs (with their disks) + a Teams alert and keeps nbci running. Only
GUARD_MAX_TRIPS trips within GUARD_TRIP_WINDOW_S (default 3 in 30 min) stop nbci (`systemctl stop nbci`) with a
loud alert. Pure decisions: nbci_logic.churn_verdict, excess_idle_vms, trips_exceeded.
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
MAX_TRIPS = int(os.environ.get('GUARD_MAX_TRIPS', '3'))          # stop nbci only after this many trips ...
TRIP_WINDOW_S = int(os.environ.get('GUARD_TRIP_WINDOW_S', '1800'))  # ... within this window
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
    why = L.churn_verdict(vms, runners, queued, since, now, WINDOW_S, CAP, L.NO_JOB_STARTUP_S)  # booting VMs are not churn
    return (why, vms, runners, queued) if why else None


def handle_trip(gh, gcp, why, vms, runners, queued, trips, now):
    """Delete the excess idle VMs (VM + disk, runners deregistered first) and alert; keep nbci running. Returns
    True when the trip repeats too often (trips_exceeded) and nbci must be stopped."""
    gone = []
    for v in L.excess_idle_vms(vms, runners, queued, now, CAP, L.NO_JOB_STARTUP_S):
        try:
            if nbci.delete_vm(gh, gcp, v, L.vm_runners(v, runners), 'churn guard: ' + why):
                gone.append(v['name'])
        except Exception as e:
            log('guard delete failed', v['name'], str(e)[:200])
    trips.append(now)
    stop = L.trips_exceeded(trips, now, MAX_TRIPS, TRIP_WINDOW_S)
    if not stop:
        nbci_alert.send('churn guard tripped (%d/%d in %ds): %s. Deleted %d idle VM(s) %s; nbci keeps running'
                        % (len(trips), MAX_TRIPS, TRIP_WINDOW_S, why, len(gone), gone), gcp, nbci.DRY)
    return stop


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
    since, seen, trips = {}, {}, []
    log('guard started window=%ds cap=%d unreg=%ds trips=%d/%ds' % (WINDOW_S, CAP, UNREG_S, MAX_TRIPS, TRIP_WINDOW_S))
    while True:
        try:
            res = once(gh, gcp, classes, since, time.time(), seen)
            if res:
                why = res[0]
                log('GUARD TRIPPED', why)
                since.clear()
                if handle_trip(gh, gcp, why, res[1], res[2], res[3], trips, time.time()):
                    log('GUARD STOPPING nbci: repeated trips', why)
                    if not nbci.DRY:
                        open('/run/nbci-stopped-by-guard', 'w').write(why[:200])   # watchdog.sh must not restart it
                        subprocess.run(['systemctl', 'stop', 'nbci'], check=False)
                    nbci_alert.send('nbci STOPPED by the churn guard: %d trips in %ds (deleting excess VMs did not cure it): %s. '
                                    'Restart: sudo rm -f /run/nbci-stopped-by-guard && sudo systemctl start nbci (after checking VMs)'
                                    % (MAX_TRIPS, TRIP_WINDOW_S, why), gcp, nbci.DRY)
                    trips.clear(); time.sleep(600)
        except Exception as e:
            log('guard error', str(e)[:200])
        time.sleep(60)


if __name__ == '__main__':
    main()
