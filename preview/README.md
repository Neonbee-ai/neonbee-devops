# Feature-branch previews

Every push to `feat/**` or `fix/**` deploys the changed repo to
`https://pr-<slug>.skyoffice360.com`, behind Cloudflare Access. Only the repos
a branch changes are deployed. Everything else in that preview is served from
develop, so a single-MFE branch costs one `dist/` folder.

## How it works

```
browser ─► Cloudflare (Access app per preview) ─► preview host nginx
                                                   ├─ /cdn/<mfe>/  own build, else dev-cdn
                                                   ├─ /api/<svc>/  own pm2 backend, else dev-api
                                                   └─ /            own shell build, else dev-dashboard
```

- Builds are **the dev build unchanged**. nginx `sub_filter` rewrites the
  `dev-cdn` / `dev-api` / `dev-dashboard` hosts in JS/CSS/JSON/HTML to
  `pr-<slug>.skyoffice360.com/{cdn,api,}`, so every call stays same-origin.
- Fallback requests go straight to the Dev VM origin (`DEV_ORIGIN`) with the
  dev hostname as Host/SNI. They skip Cloudflare Access and are cached for 1 h.
- `/api` fallbacks carry `X-SO360-Preview: <slug>`.
- `preview-ctl` on the host owns all state (`/srv/previews/registry.json`)
  and generates `/etc/nginx/conf.d/so360-previews.conf`. It runs under a flock,
  and `nginx -t` must pass or the previous config is restored.

## Lifecycle

| Event | What happens |
|---|---|
| push to `feat/*` / `fix/*` | test + build → Access app → DNS → rsync → `preview-ctl deploy` → PR comment |
| commit message has `[no-preview]` | skipped |
| newer push to same branch | older run cancelled |
| branch deleted | `neonbee-preview-teardown.yml` removes that repo's component; last one removes the preview, DNS and Access app |
| 24 h no push | backends stopped (`sleep`); next push wakes them |
| 72 h no push, or branch gone everywhere | removed by `preview-reconcile.yml` (nightly) |

## Backends (phase 2)

Backend previews need a **non-production database**. They are refused unless:

- `SUPABASE_URL_PREVIEW`, `SUPABASE_ANON_KEY_PREVIEW` and `SUPABASE_SERVICE_KEY_PREVIEW` are set. There is no fallback to the dev/prod DB.
- `preview-ctl` also rejects any `.env` containing `PROD_SUPABASE_HOST`.
- The `.env` gets `SO360_PREVIEW=true`, `EVENT_WORKERS_DISABLED=true`, `SIGNAL_EVENT_CONSUMER_DISABLED=true` and `SIGNAL_RULES_SWEEP=off`. Each backend skips `ScheduleModule` when `SO360_PREVIEW=true` (per-repo PRs).
- No mail/SES keys are written, so email fails closed.

Shared production deps are uploaded once per lockfile to `_deps/<repo>/<hash>`
and symlinked in. Each backend runs as pm2 `pv-<slug>-<name>` with a 300 MB cap
on a port from 7100–7999.

## Setup

1. On the preview host (never the Dev VM; it runs Prod HA):
   `DEV_ORIGIN=84.247.131.148 PROD_SUPABASE_HOST=<ref>.supabase.co bash preview/bootstrap-host.sh`
2. Org secrets:
   - `PREVIEW_HOST`: the preview host's IP.
   - `PREVIEW_SSH_KEY`: its root key.
   - `CF_PREVIEW_API_TOKEN`: a token with Zone DNS:Edit on skyoffice360.com and Account Access: Apps and Policies:Edit.
3. Phase 2 only: the `*_PREVIEW` database secrets above.
4. Add to each repo's `deploy.yml`:

```yaml
on:
  push:
    branches: [develop, qa, 'feat/**', 'fix/**']
  delete:

jobs:
  preview:
    if: github.event_name == 'push' && (startsWith(github.ref, 'refs/heads/feat/') || startsWith(github.ref, 'refs/heads/fix/'))
    permissions: { contents: read, pull-requests: write }
    uses: Neonbee-ai/neonbee-devops/.github/workflows/neonbee-deploy-preview.yml@main
    with:
      kind: fe            # fe | shell | be
      name: crm           # dev-cdn folder / dev-api prefix
      vite_base_url: https://dev-cdn.skyoffice360.com/crm/
      vite_env: |         # same block as vite_env_dev
        ...
    secrets: inherit

  preview-teardown:
    if: github.event_name == 'delete' && github.event.ref_type == 'branch'
    uses: Neonbee-ai/neonbee-devops/.github/workflows/neonbee-preview-teardown.yml@main
    secrets: inherit
```

The existing develop/qa/main deploy jobs need `if:` guards so that they skip
`feat/**`, `fix/**` and `delete` events.

## Known gaps

- SSO login returns to the preview only once so360-shell-fe#113 (`redirect_url`) is merged.
- `runtimeConfig.ts` in the shell and `manufacturingApi.ts` in insight-fe treat `*.skyoffice360.com` as prod.
- client-portal-pwa hard-codes the prod SSO host.
- Next.js apps (storefront, portals) are not previewable yet.
