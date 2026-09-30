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

## Kinds

| `kind` | Served from | Used by |
|---|---|---|
| `fe` | `/cdn/<name>/` on the main host (`is_mfe: false` → SPA fallback to its `index.html`) | MFEs; help-fe, platform-help-fe, sign-public-fe |
| `shell` | `/` on the main host | so360-shell-fe |
| `be` | `/api/<name>/` → pm2 | NestJS services |
| `site` | own host `pr-<slug>--<name>`, static | sso-fe, client-portal-pwa |
| `next` | own host `pr-<slug>--<name>` → pm2 (`next start` or standalone `server.js`) | so360-command, mobility-pwa, partner-portal-fe, storefront-web-fe, tools-fe |

- `site`/`next` hosts get the same `/cdn` + `/api` overlay and their own
  Access app and DNS record. Pass `dev_host` (e.g. `dev-sso.skyoffice360.com`)
  and every preview host rewrites links to it to the app host.
- Next browser calls go through `/__so360api/` on the app host. Server-side
  calls to develop's service ports hit loopback listeners (`127.0.0.1:60xx`)
  that proxy to dev-api.
- Next `.env.local` comes from `extra_env` plus develop's public Supabase URL
  and anon key. `preview-ctl` strips any `*SERVICE_KEY*`/`*SERVICE_ROLE*`, so
  SSR can do no more than the signed-in user.

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
     Only public repos (this one) receive it on the org's plan: the host config
     sync writes it to `/etc/so360-preview/cf.env` (600) and `preview-ctl
     cf-ensure` / `cf-remove` make the DNS + Access calls on the host. Run
     preview-reconcile once after creating or rotating it.
   - Private repos get no org secrets on the free plan, so each preview repo
     needs `PREVIEW_HOST`, `PREVIEW_SSH_KEY` and `PREVIEW_DEV_ORIGIN` as repo
     secrets (the Cloudflare token is not needed).
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
      kind: fe            # fe | shell | be | site | next
      # is_mfe: false     # fe that is a standalone SPA, not a remoteEntry MFE
      # dev_host: dev-sso.skyoffice360.com   # site/next: the app's dev host
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
- storefront-web-fe tenant subdomains are not previewed; only the app host.
- mobility-pwa's dev API is a plain-http IP URL; it is not rewritten, so point `extra_env` at dev-api instead.
- tools-fe links to prod `neonbee.app` URLs; those stay prod.
- Cross-host calls from a site/next host to another preview host need CORS on the target.
