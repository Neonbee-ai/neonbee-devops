#!/usr/bin/env python3
"""One merge-train tick on the nb-ci controller (systemd merge-train.service, fired by merge-train.timer).
Switches from /etc/merge-train.env (instance metadata): nbci-merge-train develop|off (default develop),
nbci-promote-main on|off (default off). Mints the GitHub App installation token (same App + key as nbci), hands it to
nbci_train.py only through the child's environment (never logged or written) and runs one tick. API-only."""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def main():
    import nbci_train
    train, _, why = nbci_train.plan(os.environ)
    print(why, flush=True)
    if not train:
        return 0
    from nbci_io import GCP, GitHub
    os.environ.pop('GH_TOKEN', None)
    os.environ['GH_TOKEN'] = GitHub(GCP(os.environ.get('GCP_PROJECT'))).token()
    os.environ['GH_PROMPT_DISABLED'] = '1'
    return nbci_train.main()


if __name__ == '__main__':
    sys.exit(main())
