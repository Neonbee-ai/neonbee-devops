"""ASG lifecycle hooks -> Cloudflare Load Balancer pool (Stage 2.5).

Events (EventBridge):
  * "EC2 Instance-launch Lifecycle Action"    -> wait for the node's own health gate
    (checked on the node via SSM, because the node SG only admits Cloudflare), then add
    its public IP to the pool and CONTINUE. Unhealthy or Cloudflare failure -> ABANDON
    (the ASG replaces the node) and alert Teams.
  * "EC2 Instance-terminate Lifecycle Action" -> disable the origin (drain), wait
    DRAIN_SECONDS, remove it, CONTINUE. Cloudflare failure -> alert, still CONTINUE.
  * "Scheduled Event"                         -> reconcile: drop aws-* origins whose
    instance is no longer InService; alert on InService nodes missing from the pool.

Only origins named ORIGIN_PREFIX + instance-id are touched; other origins in the pool
(e.g. the Dev VM fallback) are never modified. A pool is never left with zero origins:
the last origin is disabled instead of removed. Reserved concurrency = 1 in the
template serialises read-modify-write on the pool.
"""
import json
import os
import time
import urllib.error
import urllib.request

LAUNCH = "EC2 Instance-launch Lifecycle Action"
TERMINATE = "EC2 Instance-terminate Lifecycle Action"
SCHEDULED = "Scheduled Event"


class CloudflareError(Exception):
    pass


def cfg(name, default=None):
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError("missing env " + name)
    return value


def origin_name(instance_id):
    return cfg("ORIGIN_PREFIX", "aws-") + instance_id


class AwsDeps(object):
    """Real implementations. Unit tests replace this object entirely."""

    def __init__(self):
        import boto3  # lazy so tests run without boto3

        self.ec2 = boto3.client("ec2")
        self.ssm = boto3.client("ssm")
        self.asg = boto3.client("autoscaling")
        self.sns = boto3.client("sns")
        self.secrets = boto3.client("secretsmanager")
        self._token = None

    # --- AWS
    def public_ip(self, instance_id):
        res = self.ec2.describe_instances(InstanceIds=[instance_id])
        for r in res.get("Reservations", []):
            for i in r.get("Instances", []):
                return i.get("PublicIpAddress")
        return None

    def node_healthy(self, instance_id):
        """Run the health command on the node through SSM. False while SSM is not ready."""
        try:
            cmd = self.ssm.send_command(
                InstanceIds=[instance_id],
                DocumentName="AWS-RunShellScript",
                Parameters={"commands": [cfg("HEALTH_CHECK_CMD",
                    "curl -fsS -m 5 -o /dev/null http://127.0.0.1/__lb_health")]},
                TimeoutSeconds=30,
            )
            command_id = cmd["Command"]["CommandId"]
            for _ in range(10):
                time.sleep(2)
                try:
                    inv = self.ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
                except self.ssm.exceptions.InvocationDoesNotExist:
                    continue
                if inv["Status"] in ("Pending", "InProgress", "Delayed"):
                    continue
                return inv["Status"] == "Success"
            return False
        except Exception as exc:  # InvalidInstanceId while the SSM agent registers
            print("health check not ready for {}: {}".format(instance_id, exc))
            return False

    def complete(self, detail, result):
        self.asg.complete_lifecycle_action(
            LifecycleHookName=detail["LifecycleHookName"],
            AutoScalingGroupName=detail["AutoScalingGroupName"],
            LifecycleActionToken=detail["LifecycleActionToken"],
            InstanceId=detail["EC2InstanceId"],
            LifecycleActionResult=result,
        )

    def in_service(self, asg_name):
        res = self.asg.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])
        ids = [i["InstanceId"] for g in res["AutoScalingGroups"] for i in g["Instances"]
               if i["LifecycleState"] == "InService"]
        return {i: self.public_ip(i) for i in ids}

    def alert(self, title, text):
        self.sns.publish(
            TopicArn=cfg("ALERT_TOPIC_ARN"),
            Subject=title[:99],
            Message=json.dumps({"source": "cf-lb-sync", "title": title, "text": text,
                                "severity": "attention"}),
        )

    def sleep(self, seconds):
        time.sleep(seconds)

    def now(self):
        return time.time()

    # --- Cloudflare
    def _cf(self, method, body=None):
        if self._token is None:
            self._token = self.secrets.get_secret_value(
                SecretId=cfg("CF_TOKEN_SECRET_ARN"))["SecretString"].strip()
        url = "https://api.cloudflare.com/client/v4/accounts/{}/load_balancers/pools/{}".format(
            cfg("CF_ACCOUNT_ID"), cfg("CF_POOL_ID"))
        req = urllib.request.Request(
            url, method=method,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={"Authorization": "Bearer " + self._token,
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise CloudflareError("HTTP {} {}".format(exc.code, exc.read()[:300]))
        except urllib.error.URLError as exc:
            raise CloudflareError(str(exc))
        if not payload.get("success"):
            raise CloudflareError(json.dumps(payload.get("errors"))[:300])
        return payload["result"]

    def get_origins(self):
        return list(self._cf("GET").get("origins", []))

    def put_origins(self, origins):
        self._cf("PATCH", {"origins": origins})


# ------------------------------------------------------------------ pure logic

def with_origin(origins, name, address, weight):
    """Replace any origin with the same name or address, then append the new one."""
    kept = [o for o in origins if o.get("name") != name and o.get("address") != address]
    kept.append({"name": name, "address": address, "enabled": True, "weight": weight})
    return kept


def set_enabled(origins, name, enabled):
    out = []
    for o in origins:
        o = dict(o)
        if o.get("name") == name:
            o["enabled"] = enabled
        out.append(o)
    return out


def without_origin(origins, name):
    """Remove `name`; if that would empty the pool, keep it disabled instead."""
    kept = [o for o in origins if o.get("name") != name]
    if kept:
        return kept, True
    return set_enabled(origins, name, False), False


# ------------------------------------------------------------------ flows

def on_launch(detail, deps):
    iid = detail["EC2InstanceId"]
    name = origin_name(iid)
    ip = deps.public_ip(iid)
    if not ip:
        deps.alert("Node {} has no public IP".format(iid), "Launch abandoned; ASG will replace it.")
        deps.complete(detail, "ABANDON")
        return "abandoned:no-ip"

    deadline = deps.now() + int(cfg("HEALTH_WAIT_SECONDS", "480"))
    poll = int(cfg("HEALTH_POLL_SECONDS", "15"))
    healthy = False
    while True:
        if deps.node_healthy(iid):
            healthy = True
            break
        if deps.now() >= deadline:
            break
        deps.sleep(poll)
    if not healthy:
        deps.alert("Node {} failed its health gate".format(iid),
                   "/__lb_health never returned 200 within the wait; launch abandoned.")
        deps.complete(detail, "ABANDON")
        return "abandoned:unhealthy"

    try:
        origins = deps.get_origins()
        deps.put_origins(with_origin(origins, name, ip, float(cfg("ORIGIN_WEIGHT", "1"))))
    except Exception as exc:
        deps.alert("Cloudflare pool add failed for {} ({})".format(iid, ip),
                   "{}. Launch abandoned; ASG will retry with a new node.".format(exc))
        deps.complete(detail, "ABANDON")
        return "abandoned:cloudflare"

    deps.complete(detail, "CONTINUE")
    return "added"


def on_terminate(detail, deps):
    iid = detail["EC2InstanceId"]
    name = origin_name(iid)
    result = "removed"
    try:
        origins = deps.get_origins()
        if not any(o.get("name") == name for o in origins):
            result = "not-in-pool"
        else:
            deps.put_origins(set_enabled(origins, name, False))
            deps.sleep(int(cfg("DRAIN_SECONDS", "45")))
            remaining, removed = without_origin(deps.get_origins(), name)
            deps.put_origins(remaining)
            if not removed:
                result = "disabled-last-origin"
                deps.alert("Last origin {} disabled, not removed".format(name),
                           "The Cloudflare pool has no other origin; it now has no healthy origin.")
    except Exception as exc:
        result = "cloudflare-failed"
        deps.alert("Cloudflare pool remove failed for {}".format(iid),
                   "{}. The node terminates anyway; the CF monitor will mark it down. "
                   "Reconcile will retry.".format(exc))
    deps.complete(detail, "CONTINUE")
    return result


def reconcile(deps):
    prefix = cfg("ORIGIN_PREFIX", "aws-")
    live = deps.in_service(cfg("ASG_NAME"))
    try:
        origins = deps.get_origins()
        changed = False
        for o in list(origins):
            name = o.get("name", "")
            if name.startswith(prefix) and name[len(prefix):] not in live:
                origins, removed = without_origin(origins, name)
                changed = True
        if changed:
            deps.put_origins(origins)
        names = set(o.get("name") for o in origins)
        missing = sorted(i for i in live if prefix + i not in names)
        if missing:
            deps.alert("InService nodes missing from Cloudflare pool",
                       ", ".join(missing) + " - check the launch hook logs.")
        return {"changed": changed, "missing": missing}
    except Exception as exc:
        deps.alert("Cloudflare pool reconcile failed", str(exc))
        raise


def handler(event, context, deps=None):
    deps = deps or AwsDeps()
    kind = event.get("detail-type")
    detail = event.get("detail") or {}
    if kind == LAUNCH:
        return on_launch(detail, deps)
    if kind == TERMINATE:
        return on_terminate(detail, deps)
    if kind == SCHEDULED:
        return reconcile(deps)
    print("ignored event: " + str(kind))
    return "ignored"
