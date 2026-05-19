# neonbee-devops

Shared GitHub Actions reusable workflows for the Neonbee platform.

## Reusable Workflows

| Workflow | Use for |
|---|---|
| `neonbee-deploy-backend.yml` | NestJS backends (PM2) |
| `neonbee-deploy-vite-mfe.yml` | Vite MFE frontends (NGINX static) |
| `neonbee-deploy-nextjs.yml` | Next.js apps (PM2) |
| `neonbee-rollback.yml` | Universal rollback UI (all 60 services) |

## Architecture

- Build runs on Contabo self-hosted runner — never on the target VM
- Gate job: install → tests → audit → licenses → gitleaks (single merged job)
- Blue/Green: `dist-prev` backup + `.slot` tracking + Grafana Pushgateway metrics
- Health checks run from VM (127.0.0.1:PORT), not from runner

## Usage

Each service repo has a 15-line caller stub in `.github/workflows/deploy.yml`:

```yaml
jobs:
  deploy:
    uses: Neonbee-ai/neonbee-devops/.github/workflows/neonbee-deploy-backend.yml@main
    with:
      service_name: my-service
      service_dir: /github/neonbee/my-service
      port_dev: 6001
      port_staging: 7001
    secrets: inherit
```
