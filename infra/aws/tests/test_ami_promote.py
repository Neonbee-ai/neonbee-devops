import os
import unittest

from _load import load

ap = load("ami_promote")
P = "/neonbee/prod/ami/current"
PIPE = "arn:aws:imagebuilder:ap-south-1:1:image-pipeline/nb-prod-node-monthly"


class FakeDeps(object):
    def __init__(self, params=None, ami="ami-new", pipeline=PIPE, desired=2):
        self.params = dict(params or {})
        self.ami = ami
        self.pipeline = pipeline
        self._desired = desired
        self.refreshes = 0
        self.alerts = []

    def get_param(self, name):
        return self.params.get(name)

    def put_param(self, name, value, ami=False):
        self.params[name] = value

    def image_ami(self, arn):
        return self.ami, self.pipeline

    def desired(self, asg):
        return self._desired

    def start_refresh(self, asg):
        self.refreshes += 1
        return "rf-{}".format(self.refreshes)

    def alert(self, title, text, severity="attention"):
        self.alerts.append((title, severity))


def image_event(status="AVAILABLE"):
    return {"detail-type": ap.IMAGE_EVENT, "resources": ["arn:image/1"],
            "detail": {"state": {"status": status}}}


def refresh_event(kind, rid="rf-1"):
    return {"detail-type": kind, "detail": {"InstanceRefreshId": rid,
                                            "AutoScalingGroupName": "nb-prod-asg"}}


class AmiPromoteTest(unittest.TestCase):
    def setUp(self):
        os.environ.update({"AMI_PARAM": P, "ASG_NAME": "nb-prod-asg",
                           "PIPELINE_NAME": "nb-prod-node-monthly", "AUTO_REFRESH": "true"})

    def test_given_new_image_when_available_then_current_rotates_to_previous_and_refresh_starts(self):
        deps = FakeDeps(params={P: "ami-old"})
        self.assertEqual(ap.handler(image_event(), None, deps), "promoted")
        self.assertEqual(deps.params[P], "ami-new")
        self.assertEqual(deps.params[P + "-previous"], "ami-old")
        self.assertEqual(deps.params[P + "-refresh"], "rf-1|promote|ami-new")
        self.assertEqual(deps.alerts[0][1], "good")

    def test_given_asg_at_zero_when_image_available_then_promoted_without_refresh(self):
        deps = FakeDeps(params={P: "ami-old"}, desired=0)
        ap.handler(image_event(), None, deps)
        self.assertEqual(deps.params[P], "ami-new")
        self.assertEqual(deps.refreshes, 0)
        self.assertNotIn(P + "-refresh", deps.params)

    def test_given_auto_refresh_off_when_image_available_then_no_refresh(self):
        os.environ["AUTO_REFRESH"] = "false"
        deps = FakeDeps(params={P: "ami-old"})
        ap.handler(image_event(), None, deps)
        self.assertEqual(deps.refreshes, 0)

    def test_given_image_from_another_pipeline_when_available_then_ignored(self):
        deps = FakeDeps(params={P: "ami-old"}, pipeline="arn:...:image-pipeline/other")
        self.assertEqual(ap.handler(image_event(), None, deps), "ignored:other-pipeline")
        self.assertEqual(deps.params[P], "ami-old")

    def test_given_same_ami_already_current_when_available_then_nothing_changes(self):
        deps = FakeDeps(params={P: "ami-new", P + "-previous": "ami-old"})
        self.assertEqual(ap.handler(image_event(), None, deps), "already-current")
        self.assertEqual(deps.params[P + "-previous"], "ami-old")

    def test_given_non_available_state_when_image_event_then_ignored(self):
        self.assertEqual(ap.handler(image_event("BUILDING"), None, FakeDeps()), "ignored:state")

    def test_given_our_promote_refresh_failed_when_event_then_previous_restored_and_rollback_refresh(self):
        deps = FakeDeps(params={P: "ami-new", P + "-previous": "ami-old",
                                P + "-refresh": "rf-1|promote|ami-new"})
        self.assertEqual(ap.handler(refresh_event(ap.REFRESH_FAILED), None, deps), "rolled-back")
        self.assertEqual(deps.params[P], "ami-old")
        self.assertEqual(deps.params[P + "-refresh"], "rf-1|rollback|ami-old")
        self.assertEqual(deps.refreshes, 1)

    def test_given_our_promote_refresh_cancelled_when_event_then_rolls_back_too(self):
        deps = FakeDeps(params={P: "ami-new", P + "-previous": "ami-old",
                                P + "-refresh": "rf-1|promote|ami-new"})
        self.assertEqual(ap.handler(refresh_event(ap.REFRESH_CANCELLED), None, deps), "rolled-back")

    def test_given_rollback_refresh_failed_when_event_then_alert_only_no_loop(self):
        deps = FakeDeps(params={P: "ami-old", P + "-previous": "ami-old",
                                P + "-refresh": "rf-1|rollback|ami-old"})
        self.assertEqual(ap.handler(refresh_event(ap.REFRESH_FAILED), None, deps), "rollback-failed")
        self.assertEqual(deps.refreshes, 0)
        self.assertEqual(len(deps.alerts), 1)

    def test_given_refresh_not_started_by_us_when_failed_then_ignored(self):
        deps = FakeDeps(params={P + "-refresh": "rf-9|promote|ami-x"})
        self.assertEqual(ap.handler(refresh_event(ap.REFRESH_FAILED, "rf-1"), None, deps), "ignored:not-ours")

    def test_given_no_previous_ami_when_promote_refresh_failed_then_alert_without_change(self):
        deps = FakeDeps(params={P: "ami-new", P + "-refresh": "rf-1|promote|ami-new"})
        self.assertEqual(ap.handler(refresh_event(ap.REFRESH_FAILED), None, deps), "no-previous")
        self.assertEqual(deps.params[P], "ami-new")

    def test_given_our_refresh_succeeded_when_event_then_info_alert(self):
        deps = FakeDeps(params={P + "-refresh": "rf-1|promote|ami-new"})
        self.assertEqual(ap.handler(refresh_event(ap.REFRESH_SUCCEEDED), None, deps), "succeeded")
        self.assertEqual(deps.alerts[0][1], "good")


if __name__ == "__main__":
    unittest.main()
