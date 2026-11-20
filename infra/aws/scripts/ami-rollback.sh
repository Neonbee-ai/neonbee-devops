#!/usr/bin/env bash
# Manual AMI rollback (Stage 2.3). The ami-promote Lambda does this automatically when a
# promote refresh fails; use this when a problem shows up later.
#   ./ami-rollback.sh [--apply]        (dry run by default)
# Swaps /neonbee/prod/ami/current with -previous, then starts an instance refresh
# (launch-before-terminate, min healthy 100%) if the ASG has nodes.
set -euo pipefail
PARAM=${AMI_PARAM:-/neonbee/prod/ami/current}
ASG=${ASG_NAME:-nb-prod-asg}
REGION=${AWS_REGION:-ap-south-1}
CUR=$(aws ssm get-parameter --region "$REGION" --name "$PARAM" --query Parameter.Value --output text)
PREV=$(aws ssm get-parameter --region "$REGION" --name "$PARAM-previous" --query Parameter.Value --output text)
echo "current=$CUR previous=$PREV asg=$ASG"
[ "$CUR" != "$PREV" ] || { echo "current == previous, nothing to roll back"; exit 1; }
if [ "${1:-}" != "--apply" ]; then echo "(dry run - pass --apply)"; exit 0; fi
aws ssm put-parameter --region "$REGION" --name "$PARAM-previous" --value "$CUR" --type String --data-type aws:ec2:image --overwrite >/dev/null
aws ssm put-parameter --region "$REGION" --name "$PARAM" --value "$PREV" --type String --data-type aws:ec2:image --overwrite >/dev/null
DESIRED=$(aws autoscaling describe-auto-scaling-groups --region "$REGION" --auto-scaling-group-names "$ASG" --query 'AutoScalingGroups[0].DesiredCapacity' --output text)
if [ "$DESIRED" != "0" ]; then
  aws autoscaling start-instance-refresh --region "$REGION" --auto-scaling-group-name "$ASG" --strategy Rolling \
    --preferences '{"MinHealthyPercentage":100,"MaxHealthyPercentage":200,"InstanceWarmup":300,"SkipMatching":false}'
fi
echo "AMI now $PREV"
