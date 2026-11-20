# Neonbee prod on AWS (ap-south-1) - Stage 2 foundation

Infrastructure as code for plan items 2.1, 2.2, 2.3, 2.5 and 2.6
(`documents/infrastructure/neonbee-production-final-plan.md`). CloudFormation YAML,
one stack per layer. **Nothing here has been deployed.** Every command below that
changes AWS or Cloudflare is for the operator to run after approval.

Front door: **Cloudflare Load Balancer** in front of the nodes' public IPs. There is
no AWS ALB (the design doc's ALB option was replaced). Shared cache: Postgres (no
ElastiCache). Alerts: Microsoft Teams.

**Out of scope (owner decision 2026-11-20):** Command (`command.neonbee.app`,
`so360-command`, `so360-command-be`), the marketing website (`neonbee.ai`) and the
Internal Tools apps stay on the Internal Tools VM 167.114.98.177. They are not in the
service catalog, not in the AWS Cloudflare pool and not part of the cutover.

## Layout

| Path | What |
|---|---|
| `templates/01-network.yaml` | VPC 10.40.0.0/16, 2 public subnets (2 AZs), IGW, free S3 gateway endpoint, **no NAT**. Node SG: 80/443 only from a Cloudflare IPv4 prefix list; no SSH (SSM Session Manager) |
| `templates/02-data.yaml` | Release bucket (versioned, encrypted, private, lifecycle), secrets `CF_LB_API_TOKEN`, `neonbee/prod/teams-webhook-url`, `neonbee/prod/env/_shared` |
| `templates/03-alerts.yaml` | SNS topic `nb-prod-alerts` -> `teams-notify` Lambda -> Teams webhook; AWS Budget $350/month (80% + 100% actual, 100% forecast); optional email backstop |
| `templates/04-compute.yaml` | Instance role + profile, AMI pointer params, CloudWatch agent config, launch template (IMDSv2, gp3 encrypted, AMI via `resolve:ssm`), ASG **0/0/4** (on-demand base 2, Spot above), lifecycle hooks `nb-ready` / `nb-drain`, alarms (CPU, memory, disk, missing nodes), optional scaling policies, SSM document **`nb-deploy`** |
| `templates/05-scale-events.yaml` | Lifecycle events -> `cf-lb-sync` Lambda (add healthy node to the CF pool, drain + remove on terminate, 15-min reconcile, alerts) |
| `templates/06-image.yaml` | Image Builder monthly pipeline (Ubuntu 24.04 + patches + Node 22 + PM2 + nginx + CW agent + nb-agent) and `ami-promote` Lambda (promote, instance refresh, auto-rollback) |
| `lambdas/` | `teams_notify`, `cf_lb_sync`, `ami_promote` (Python 3.12, stdlib + boto3) |
| `vm-agent/` | `nb-boot.sh`, `nb-deploy.sh`, `nb-health.sh`, `nb-pm2-apply.js`, systemd units, nginx `/__lb_health` snippet, `services.example.json` |
| `scripts/` | `package-agent.sh`, `ami-rollback.sh` |
| `cloudflare/set-health-monitor.sh` | 2.6 - tighten the CF monitor (dry run by default) |
| `tests/` | BDD unit tests (`python3 -m unittest discover -p 'test_*.py'` from `tests/`, Mac Mini only) |

## How a node works (2.2)

1. Launch template user data writes `/etc/nb-agent/agent.conf` (bucket, region,
   secret prefix, `NB_SCHEDULER_DB_LOCK=true`; **no secrets**) and starts
   `nb-boot.service` + `nb-health.timer`, which are baked into the AMI.
2. `nb-boot.sh` starts the CloudWatch agent, downloads `s3://<bucket>/agent/services.json`
   (the service catalog), then for every service reads `s3://<bucket>/<svc>/current.json`
   (written by the deploy workflow, plan 2.4) and runs `nb-deploy.sh` for that sha.
3. `nb-deploy.sh <service> <sha> <bucket> <key>` (also what SSM `nb-deploy` runs):
   download `<key>` + verify `<key>.sha256` -> unpack to
   `/opt/neonbee/services/<svc>/releases/<sha>` -> render `.env` from Secrets Manager
   (`neonbee/prod/env/_shared`, then `neonbee/prod/env/<svc>`) -> flip the `current`
   symlink -> write `<service_dir>/.nb-ecosystem.<svc>.json` and run
   `pm2 reload <file> --only <svc>` (delete+start of that one app only when its shape
   changed; never `pm2 ... all`) -> health check `127.0.0.1:<port><health_path>` ->
   on failure flip back to the previous release and reload again, exit 1.
   `type: nginx` catalog entries ship nginx site files instead (`nginx -t` + reload).
4. `nb-health.sh` (every 10 s) writes `/var/lib/nb-agent/health.json` and creates
   `/var/lib/nb-agent/www/__lb_health` only while boot is done, nginx is up and every
   `critical: true` service answers. nginx serves it at `/__lb_health` (include
   `vm-agent/nginx/nb-lb-health.conf` in each server block). Non-critical failures do
   not eject the node.

### Scheduled jobs on N nodes

Apps already pick the cron leader per PM2 cluster with `NODE_APP_INSTANCE == 0`
(`isSchedulerLeader()`), so each VM has its own "instance 0". Across VMs the apps'
**DB run-once lock** guarded by `NB_SCHEDULER_DB_LOCK` makes each run happen once:
the agent sets `NB_SCHEDULER_DB_LOCK=true` in `agent.conf`, every `.env` and every
ecosystem file. No node-level leader election or "only one VM is instance 0" role is
needed. **Before MinSize > 1, confirm every backend with `@Cron` honours the flag**
(it was not found in the monorepo checkout on 2026-11-20 - see plan item 1.2).

## Deploy order (operator, after approval)

```bash
export AWS_REGION=ap-south-1 ENV=nb-prod
cd infra/aws
# 1. network
aws cloudformation deploy --stack-name $ENV-network --template-file templates/01-network.yaml
# 2. data, then set secret values (never commit them)
aws cloudformation deploy --stack-name $ENV-data --template-file templates/02-data.yaml
BUCKET=$(aws cloudformation describe-stacks --stack-name $ENV-data --query "Stacks[0].Outputs[?OutputKey=='ReleaseBucketName'].OutputValue" --output text)
aws secretsmanager put-secret-value --secret-id CF_LB_API_TOKEN --secret-string '<token: Load Balancing pools edit>'
aws secretsmanager put-secret-value --secret-id neonbee/prod/teams-webhook-url --secret-string '<https://...webhook...>'
# per app: aws secretsmanager create-secret --name neonbee/prod/env/<svc> --secret-string file://<svc>.json
# 3. alerts (Lambda code is packaged to s3://$BUCKET/artifacts/)
aws cloudformation package --template-file templates/03-alerts.yaml --s3-bucket $BUCKET --s3-prefix artifacts --output-template-file /tmp/03.yaml
aws cloudformation deploy --stack-name $ENV-alerts --template-file /tmp/03.yaml --capabilities CAPABILITY_IAM \
  --parameter-overrides BudgetEmail=<optional email>
# 4. agent bundle + service catalog (copy services.example.json -> vm-agent/services.json, fill all apps)
scripts/package-agent.sh /tmp/nb-agent s3://$BUCKET
# 5. compute. InitialAmiId = any Ubuntu 24.04 x86 AMI as a placeholder; desired is 0 so
#    nothing launches until step 7 replaces it with a real image.
UBUNTU=$(aws ssm get-parameter --name /aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id --query Parameter.Value --output text)
aws cloudformation deploy --stack-name $ENV-compute --template-file templates/04-compute.yaml --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides InitialAmiId=$UBUNTU
# 6. Cloudflare pool for the AWS nodes (once, in the CF dashboard or API). A pool needs
#    >= 1 origin: seed it with the current prod origin as fallback; cf-lb-sync only
#    ever touches origins named aws-<instance-id>.
aws cloudformation package --template-file templates/05-scale-events.yaml --s3-bucket $BUCKET --s3-prefix artifacts --output-template-file /tmp/05.yaml
aws cloudformation deploy --stack-name $ENV-scale-events --template-file /tmp/05.yaml --capabilities CAPABILITY_IAM \
  --parameter-overrides CloudflareAccountId=<32 hex> CloudflarePoolId=<32 hex>
# 7. image pipeline, then build the first image now (promote Lambda points the AMI param at it)
aws cloudformation package --template-file templates/06-image.yaml --s3-bucket $BUCKET --s3-prefix artifacts --output-template-file /tmp/06.yaml
aws cloudformation deploy --stack-name $ENV-image --template-file /tmp/06.yaml --capabilities CAPABILITY_IAM
aws imagebuilder start-image-pipeline-execution --image-pipeline-arn <PipelineArn output>
#    After the first promote, set -previous = current so a rollback never lands on bare Ubuntu:
aws ssm put-parameter --name /neonbee/prod/ami/current-previous --type String --data-type aws:ec2:image --overwrite \
  --value "$(aws ssm get-parameter --name /neonbee/prod/ami/current --query Parameter.Value --output text)"
# 8. 2.6 - monitor (dry run first)
CF_ACCOUNT_ID=... CF_MONITOR_ID=... INTERVAL=15 cloudflare/set-health-monitor.sh
# 9. Exit test for Stage 2: lower the budget to $1 temporarily, confirm the Teams message, restore.
```

Go-live (Stage 3, not now): `MinSize=2 DesiredCapacity=2 EnableScalingPolicies=true`
on the compute stack, after scheduler-lock verification and a load test.

## Parameters

| Stack | Parameter | Default | Needs a value from the owner |
|---|---|---|---|
| all | `EnvName` | `nb-prod` | |
| data | secret `CF_LB_API_TOKEN` | `REPLACE_ME` | **yes** - CF token with Load Balancing: Monitors and Pools edit |
| data | secret `neonbee/prod/teams-webhook-url` | `REPLACE_ME` | **yes** - Teams incoming webhook URL |
| data | secrets `neonbee/prod/env/<svc>` | (none) | **yes** - per-app env JSON (runner can write them in 2.4) |
| alerts | `MonthlyBudgetUsd` / `BudgetEmail` | 350 / blank | optional email backstop |
| compute | `InstanceType` / `SpotInstanceTypes` | r6a.large / r5a,r6i,r5 .large | |
| compute | `MinSize` / `DesiredCapacity` / `MaxSize` | 0 / 0 / 4 | |
| compute | `OnDemandBaseCapacity` | 2 | |
| compute | `InitialAmiId` | (required) | placeholder Ubuntu AMI, first create only |
| compute | `EnableScalingPolicies`, `CpuTargetPercent` | false, 55 | |
| scale-events | `CloudflareAccountId`, `CloudflarePoolId` | (required) | **yes** |
| scale-events | `HealthWaitSeconds` / `DrainSeconds` / `HealthHostHeader` | 480 / 45 / api.neonbee.app | |
| image | `RecipeVersion`, `ScheduleExpression`, `AutoRefresh` | 1.0.0, 1st of month 20:30 UTC, true | bump RecipeVersion on any recipe change |
| S3 | `agent/services.json` | example only | **yes** - full app list, ports, health paths, critical flags |

## Monthly cost (on-demand, ap-south-1, rates from the design doc §8)

Per node: r6a.large $52.20 + public IPv4 $3.65 + 30 GB gp3 $2.74 + 2 agent metrics
$0.60 = **about $59.2**. Fixed: Secrets Manager $0.40/secret (3 base = $1.20; about
36 per-app secrets = $14.40), 6-8 alarms about $0.80, S3 + AMI snapshots about $1.50,
Image Builder build about $0.10, Lambda/EventBridge/SNS/Budgets free tier. Cloudflare
LB add-on is an existing cost and not counted. Egress: first 100 GB free, then $0.1093/GB.

| Nodes | Nodes | Fixed | Egress (est.) | Total |
|---|---|---|---|---|
| 0 (Stage 2) | $0 | about $18 | $0 | **about $18** |
| 2 (base) | $118 | about $18 | $5-16 | **about $141-152** |
| 4 (cap, all on-demand) | $237 | about $18 | up to $44 | **about $299** |

Spot nodes above the base of 2 cut the 3rd/4th node by roughly 60-70%. The $350 budget
alarm sits above the 4-node worst case. Old AMIs: deregister all but the last 3
(about $0.40-1.50 each per month in snapshots).

## Rollback

| What | Automatic | Manual |
|---|---|---|
| App release on a node | `nb-deploy.sh` flips back to the previous release when health fails | SSM `nb-deploy` with the previous sha/key from `<svc>/current.json` (`prev`, `prev_key`) |
| App release fleet-wide | Deploy workflow (2.4) restores `current.json` and redeploys previous | same |
| OS image | `ami-promote` restores `-previous` and refreshes again when a promote refresh fails/cancels (once, no loop) | `scripts/ami-rollback.sh --apply` |
| Cloudflare pool | 15-min reconcile drops `aws-*` origins with no InService node | remove the origin in the CF dashboard |
| Infrastructure | CloudFormation rolls back a failed stack update | redeploy the previous template from git. Bucket is `Retain` |
| Turn AWS off | | `MinSize=0 DesiredCapacity=0` on compute; nodes drain out of the pool via `nb-drain` |

## Cloudflare IP ranges

`01-network.yaml` hard-codes https://www.cloudflare.com/ips-v4 (15 ranges, prefix list
max 20). If Cloudflare publishes changes, edit the list and redeploy the network stack.

## Known limits

- `cf-lb-sync` checks node health through SSM (the node SG admits only Cloudflare), so
  the SSM agent must be online within the 480 s wait.
- A Cloudflare failure on launch abandons the node; the ASG retries with a fresh one and
  every attempt alerts Teams.
- The pool is read-modify-written; Lambda reserved concurrency 1 serialises updates.
- The launch template resolves the AMI from SSM at launch time, so instance refresh
  auto-rollback (launch-template based) is not used; `ami-promote` does the rollback.
