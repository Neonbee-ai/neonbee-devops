import json
import os
import unittest

from _load import load

tn = load("teams_notify")


def sns_event(message, subject=None):
    return {"Records": [{"Sns": {"Subject": subject, "Message": message}}]}


class TeamsNotifyTest(unittest.TestCase):
    def setUp(self):
        tn._cache.clear()
        os.environ["TEAMS_WEBHOOK_SECRET_ARN"] = "arn:secret:teams"
        os.environ["ENV_NAME"] = "nb-prod"
        self.posts = []
        self.secret_reads = []

    def get_secret(self, arn):
        self.secret_reads.append(arn)
        return "https://example.webhook.office.com/hook/abc"

    def post(self, url, payload):
        self.posts.append((url, payload))
        return 200

    def card_texts(self, i=0):
        body = self.posts[i][1]["attachments"][0]["content"]["body"]
        return body[0], body[1]

    def test_given_cloudwatch_alarm_when_handled_then_posts_alarm_name_state_and_red_colour(self):
        msg = json.dumps({"AlarmName": "nb-prod-cpu-high", "NewStateValue": "ALARM",
                          "NewStateReason": "CPU 91%"})
        tn.handler(sns_event(msg), None, self.get_secret, self.post)
        title, text = self.card_texts()
        self.assertEqual(title["text"], "[nb-prod] nb-prod-cpu-high is ALARM")
        self.assertEqual(title["color"], "attention")
        self.assertEqual(text["text"], "CPU 91%")

    def test_given_alarm_back_to_ok_when_handled_then_colour_is_good(self):
        msg = json.dumps({"AlarmName": "a", "NewStateValue": "OK", "NewStateReason": "fine"})
        tn.handler(sns_event(msg), None, self.get_secret, self.post)
        self.assertEqual(self.card_texts()[0]["color"], "good")

    def test_given_internal_alert_json_when_handled_then_uses_its_title_text_and_severity(self):
        msg = json.dumps({"source": "cf-lb-sync", "title": "CF failed", "text": "HTTP 403",
                          "severity": "attention"})
        tn.handler(sns_event(msg), None, self.get_secret, self.post)
        title, text = self.card_texts()
        self.assertEqual(title["text"], "[nb-prod] CF failed")
        self.assertEqual(text["text"], "HTTP 403")

    def test_given_budget_plain_text_when_handled_then_uses_subject_and_raw_text(self):
        tn.handler(sns_event("Actual cost is 82% of 350 USD", "AWS Budgets: nb-prod-monthly"),
                   None, self.get_secret, self.post)
        title, text = self.card_texts()
        self.assertEqual(title["text"], "[nb-prod] AWS Budgets: nb-prod-monthly")
        self.assertIn("82%", text["text"])
        self.assertEqual(title["color"], "warning")

    def test_given_two_records_when_handled_then_posts_twice_and_reads_secret_once(self):
        event = {"Records": sns_event("a")["Records"] + sns_event("b")["Records"]}
        out = tn.handler(event, None, self.get_secret, self.post)
        self.assertEqual(out, {"sent": 2})
        self.assertEqual(len(self.secret_reads), 1)

    def test_given_placeholder_secret_when_handled_then_skips_without_posting(self):
        tn._cache.clear()
        out = tn.handler(sns_event("x"), None, lambda arn: "REPLACE_ME", self.post)
        self.assertEqual(out, {"sent": 0, "skipped": True})
        self.assertEqual(self.posts, [])

    def test_given_secret_without_value_when_handled_then_skips_without_posting(self):
        tn._cache.clear()
        def missing(arn):
            raise KeyError("ResourceNotFoundException")
        out = tn.handler(sns_event("x"), None, missing, self.post)
        self.assertEqual(out, {"sent": 0, "skipped": True})
        self.assertEqual(self.posts, [])

    def test_given_teams_returns_400_when_handled_then_raises_so_lambda_errors_alarm_fires(self):
        with self.assertRaises(RuntimeError):
            tn.handler(sns_event("x"), None, self.get_secret, lambda u, p: 400)

    def test_given_very_long_text_when_card_built_then_text_is_truncated(self):
        card = tn.build_card("t", "x" * 5000, "good")
        self.assertEqual(len(card["attachments"][0]["content"]["body"][1]["text"]), 3500)


if __name__ == "__main__":
    unittest.main()
