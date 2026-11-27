#!/usr/bin/env python3
"""One night-train tick on the nb-ci controller (systemd night-train.service, fired by night-train.timer).

Mode from /etc/night-train.env (instance metadata nbci-night-train): off | dry | live. Off, or past NT_DEADLINE
once the final tick ran, exits 0 without touching GitHub or Pulse. Otherwise it mints a GitHub App installation
token (same App + key as nbci) and reads the Pulse token from Secret Manager `nbci-pulse-token`, hands both to
night-train.mjs only through the child's environment (never logged, never written to disk) and execs node.
API-only work (gh + Pulse), so it runs on the controller itself; no job VM is needed."""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
E = os.environ.get


def plan(env):
    """Pure: (run?, reason, extra env) for one tick."""
    mode = (env.get('NT_MODE') or 'off').strip().lower()
    if mode not in ('dry', 'live'):
        return False, 'night train off (metadata nbci-night-train=%s)' % mode, {}
    if not env.get('NT_DEADLINE'):
        return False, 'NT_DEADLINE not set (metadata nbci-nt-deadline)', {}
    return True, 'night train %s until %s' % (mode, env['NT_DEADLINE']), {'DRY': '1' if mode == 'dry' else '0'}


def main():
    ok, why, extra = plan(os.environ)
    print(why, flush=True)
    if not ok:
        return 0
    from nbci_io import GCP, GitHub
    gcp = GCP(E('GCP_PROJECT'))
    env = dict(os.environ, **extra)
    env.pop('GH_TOKEN', None)
    env['GH_TOKEN'] = GitHub(gcp).token()
    env['PULSE_TOKEN'] = gcp.secret('nbci-pulse-token').strip()
    env.setdefault('PULSE_MCP_URL', 'https://pulse.moonhive.in/mcp')
    env['GH_PROMPT_DISABLED'] = '1'
    os.execvpe('node', ['node', os.path.join(HERE, 'night-train.mjs')], env)


if __name__ == '__main__':
    sys.exit(main())
