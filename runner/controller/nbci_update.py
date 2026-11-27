#!/usr/bin/env python3
"""Controller auto-update (run by nbci-update.timer every 10 min, as root, under the bootstrap flock).
Follows instance metadata nbci-ref ('main' = follow main; a 40-char sha is pinned and never moves). When the ref has a
new sha: run bootstrap.sh (checkout + restart nbci/nbci-guard), then health-check (nbci active, /healthz 200, loop
heartbeat written after the restart and < 3 min old). On failure: remember the sha in /var/lib/nbci/bad-ref (never
retried), roll back to the previous sha (bootstrap with NBCI_REF_OVERRIDE) and alert. Never touches job VMs."""
import os, subprocess, sys, time, urllib.request
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import nbci_logic as L  # noqa: E402

SRC = '/opt/nbci/src'; BOOT = SRC + '/runner/controller/bootstrap.sh'
HB = os.environ.get('HEARTBEAT_FILE', '/var/lib/nbci/heartbeat'); BAD = '/var/lib/nbci/bad-ref'
WAIT_S = int(os.environ.get('NBCI_UPDATE_WAIT_S', '300'))


def sh(*a, env=None, timeout=900):
    return subprocess.run(list(a), env=dict(os.environ, **(env or {})), capture_output=True, text=True, timeout=timeout)


def head():
    r = sh('git', '-C', SRC, 'rev-parse', 'HEAD'); return r.stdout.strip() if r.returncode == 0 else ''


def md(path):
    try:
        with urllib.request.urlopen(urllib.request.Request('http://metadata.google.internal/computeMetadata/v1/' + path,
                                    headers={'Metadata-Flavor': 'Google'}), timeout=5) as f:
            return f.read().decode()
    except Exception:
        return ''


def probe(started):
    """(healthz status, heartbeat epoch, nbci active) right now."""
    try:
        with urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('HOOK_PORT', '8787'), timeout=5) as f: code = f.status
    except Exception: code = 0
    try: hb = int(open(HB).read().strip())
    except (OSError, ValueError): hb = 0
    return code, hb, sh('systemctl', 'is-active', '--quiet', 'nbci').returncode == 0


def healthy(started):
    end = time.time() + WAIT_S
    while True:
        code, hb, active = probe(started)
        if L.health_ok(hb, started, time.time(), code, active):
            return True
        if time.time() > end:
            return False
        time.sleep(10)


def alert(text):
    sh('python3', HERE + '/nbci_alert.py', text, timeout=120)


def main():
    ref = md('instance/attributes/nbci-ref').strip() or 'main'
    old = head()
    if L.update_action(ref, old, old, '') == 'pinned':
        return 0
    t0 = int(time.time())
    r = sh('bash', BOOT, env={'NBCI_ONLY_IF_CHANGED': '1'}, timeout=1200)
    new = head()
    bad = open(BAD).read().strip() if os.path.exists(BAD) else ''
    if L.update_action(ref, old, new, bad) != 'update':
        return 0
    if r.returncode == 0 and healthy(t0):
        print('updated %s -> %s' % (old[:7], new[:7])); alert('controller updated %s -> %s (health ok)' % (old[:7], new[:7])); return 0
    open(BAD, 'w').write(new)
    sh('bash', BOOT, env={'NBCI_REF_OVERRIDE': old}, timeout=1200)
    ok = healthy(int(time.time()) - 5)
    alert('controller update to %s FAILED health check: rolled back to %s (%s). That sha will not be retried until main moves.' % (
        new[:7], old[:7], 'healthy' if ok else 'STILL UNHEALTHY, check the VM'))
    return 1


if __name__ == '__main__':
    sys.exit(main())
