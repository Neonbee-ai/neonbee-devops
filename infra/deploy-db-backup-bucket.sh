#!/usr/bin/env bash
# Deploy infra/db-backup-bucket.yml (prod AWS account, ap-south-1) and print the
# values to store as GitHub repo variables. Never prints or creates secrets.
#
# Usage: infra/deploy-db-backup-bucket.sh [--with-user] [--dry-run]
#   --with-user  also create IAM user nb-db-backup (access-key fallback; create
#                the key by hand: aws iam create-access-key --user-name nb-db-backup)
#   --dry-run    validate the template and show the change set only
# Env: AWS_PROFILE (prod), STACK (nb-prod-db-backup), REGION (ap-south-1),
#      BUCKET_NAME (neonbee-prod-db-backups), SCHEDULES (ENABLED|DISABLED, default ENABLED)
set -euo pipefail
cd "$(dirname "$0")"

STACK="${STACK:-nb-prod-db-backup}"
REGION="${REGION:-ap-south-1}"
BUCKET_NAME="${BUCKET_NAME:-neonbee-prod-db-backups}"
WITH_USER=false; DRY=false
for a in "$@"; do
  case "$a" in
    --with-user) WITH_USER=true ;;
    --dry-run) DRY=true ;;
    *) echo "unknown arg: $a" >&2; exit 2 ;;
  esac
done

aws sts get-caller-identity --query Account --output text >/dev/null
aws cloudformation validate-template --region "$REGION" --template-body file://db-backup-bucket.yml >/dev/null

args=(--region "$REGION" --stack-name "$STACK" --template-file db-backup-bucket.yml
      --capabilities CAPABILITY_NAMED_IAM
      --parameter-overrides "BucketName=$BUCKET_NAME" "CreateAccessKeyUser=$WITH_USER" "SchedulesEnabled=${SCHEDULES:-ENABLED}"
      --tags Project=neonbee Purpose=prod-db-backup)
$DRY && args+=(--no-execute-changeset)

# Job scripts the Fargate tasks fetch at start (ops/ prefix; versioned + Object Locked like the rest).
if ! $DRY; then
  aws s3 cp --recursive --region "$REGION" --only-show-errors --sse AES256 \
    db-backup-task/ "s3://$BUCKET_NAME/ops/db-backup-task/"
fi

aws cloudformation deploy "${args[@]}"
$DRY && exit 0

out() { aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
echo "Fargate jobs: cluster $(out JobCluster), secret $(out SupabaseSecretArn)"
echo "  first deploy: run workflow nightly-db-backup.yml with mode=sync-secret to fill the secret"
echo "Set these GitHub variables on Neonbee-ai/neonbee-devops:"
echo "  gh variable set NB_DB_BACKUP_BUCKET   -R Neonbee-ai/neonbee-devops -b $(out BucketName)"
echo "  gh variable set NB_DB_BACKUP_ROLE_ARN -R Neonbee-ai/neonbee-devops -b $(out BackupRoleArn)"
