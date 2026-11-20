"""Monthly AMI promote + automatic rollback (Stage 2.3).

SSM parameters (created by the compute stack):
  AMI_PARAM              e.g. /neonbee/prod/ami/current   (launch template resolves it)
  AMI_PARAM-previous     last known-good AMI (rollback target)
  AMI_PARAM-refresh      "<refresh-id>|<kind>|<ami>" of the refresh this Lambda started
                          kind = promote | rollback

Events:
  * Image Builder image AVAILABLE (our pipeline only) -> previous := current,
    current := new AMI, start an instance refresh (if the ASG has nodes and AUTO_REFRESH).
  * Instance refresh Failed/Cancelled for a refresh WE started with kind=promote ->
    current := previous, start a rollback refresh, alert. A failed rollback only alerts
    (no loop).
  * Instance refresh Succeeded -> informational alert.
"""
import json
import os

IMAGE_EVENT = "EC2 Image Builder Image State Change"
REFRESH_FAILED = "EC2 Auto Scaling Instance Refresh Failed"
REFRESH_CANCELLED = "EC2 Auto Scaling Instance Refresh Cancelled"
REFRESH_SUCCEEDED = "EC2 Auto Scaling Instance Refresh Succeeded"

REFRESH_PREFS = {
    "MinHealthyPercentage": 100,
    "MaxHealthyPercentage": 200,
    "InstanceWarmup": 300,
    "SkipMatching": False,
}


class AwsDeps(object):
    def __init__(self):
        import boto3

        self.ssm = boto3.client("ssm")
        self.ib = boto3.client("imagebuilder")
        self.asg = boto3.client("autoscaling")
        self.sns = boto3.client("sns")

    def get_param(self, name):
        try:
            return self.ssm.get_parameter(Name=name)["Parameter"]["Value"]
        except self.ssm.exceptions.ParameterNotFound:
            return None

    def put_param(self, name, value, ami=False):
        kwargs = {"Name": name, "Value": value, "Type": "String", "Overwrite": True}
        if ami:
            kwargs["DataType"] = "aws:ec2:image"
        self.ssm.put_parameter(**kwargs)

    def image_ami(self, image_arn):
        img = self.ib.get_image(imageBuildVersionArn=image_arn)["image"]
        region = os.environ.get("AWS_REGION")
        for ami in img.get("outputResources", {}).get("amis", []):
            if not region or ami.get("region") == region:
                return ami["image"], img.get("sourcePipelineArn", "")
        return None, img.get("sourcePipelineArn", "")

    def desired(self, asg_name):
        g = self.asg.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])["AutoScalingGroups"]
        return g[0]["DesiredCapacity"] if g else 0

    def start_refresh(self, asg_name):
        return self.asg.start_instance_refresh(
            AutoScalingGroupName=asg_name, Strategy="Rolling", Preferences=REFRESH_PREFS
        )["InstanceRefreshId"]

    def alert(self, title, text, severity="attention"):
        self.sns.publish(
            TopicArn=os.environ["ALERT_TOPIC_ARN"],
            Subject=title[:99],
            Message=json.dumps({"source": "ami-promote", "title": title, "text": text,
                                "severity": severity}),
        )


def _p(suffix=""):
    return os.environ["AMI_PARAM"] + suffix


def maybe_refresh(deps, kind, ami):
    asg = os.environ["ASG_NAME"]
    if os.environ.get("AUTO_REFRESH", "true") != "true" or deps.desired(asg) == 0:
        return None
    rid = deps.start_refresh(asg)
    deps.put_param(_p("-refresh"), "{}|{}|{}".format(rid, kind, ami))
    return rid


def on_image(event, deps):
    image_arn = (event.get("resources") or [""])[0]
    ami, pipeline_arn = deps.image_ami(image_arn)
    want = os.environ.get("PIPELINE_NAME", "")
    if want and not pipeline_arn.endswith("/" + want):
        return "ignored:other-pipeline"
    if not ami:
        deps.alert("New image has no AMI in this region", image_arn)
        return "no-ami"
    current = deps.get_param(_p())
    if current == ami:
        return "already-current"
    if current:
        deps.put_param(_p("-previous"), current, ami=True)
    deps.put_param(_p(), ami, ami=True)
    rid = maybe_refresh(deps, "promote", ami)
    deps.alert(
        "New node AMI promoted: {}".format(ami),
        "previous={} refresh={}".format(current, rid or "not started (0 nodes or AUTO_REFRESH=false)"),
        severity="good",
    )
    return "promoted"


def on_refresh_end(kind, detail, deps):
    rid = detail.get("InstanceRefreshId", "")
    tracked = (deps.get_param(_p("-refresh")) or "").split("|")
    if len(tracked) < 3 or tracked[0] != rid:
        return "ignored:not-ours"
    tracked_kind, tracked_ami = tracked[1], tracked[2]

    if kind == REFRESH_SUCCEEDED:
        deps.alert("Instance refresh succeeded ({})".format(tracked_kind),
                   "Nodes now run {}".format(tracked_ami), severity="good")
        return "succeeded"

    if tracked_kind == "rollback":
        deps.alert("AMI ROLLBACK refresh also failed", "Refresh {} - manual action needed "
                   "(infra/aws/scripts/ami-rollback.sh).".format(rid))
        return "rollback-failed"

    previous = deps.get_param(_p("-previous"))
    if not previous or previous == tracked_ami:
        deps.alert("Instance refresh failed, no previous AMI to roll back to", rid)
        return "no-previous"
    deps.put_param(_p(), previous, ami=True)
    new_rid = maybe_refresh(deps, "rollback", previous)
    deps.alert("AMI {} rolled back to {}".format(tracked_ami, previous),
               "Refresh {} ended: {}. Rollback refresh: {}".format(rid, kind, new_rid))
    return "rolled-back"


def handler(event, context, deps=None):
    deps = deps or AwsDeps()
    kind = event.get("detail-type")
    if kind == IMAGE_EVENT:
        if ((event.get("detail") or {}).get("state") or {}).get("status") != "AVAILABLE":
            return "ignored:state"
        return on_image(event, deps)
    if kind in (REFRESH_FAILED, REFRESH_CANCELLED, REFRESH_SUCCEEDED):
        return on_refresh_end(kind, event.get("detail") or {}, deps)
    return "ignored"
