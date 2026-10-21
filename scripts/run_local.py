"""Run the full match on the sample data without an AWS account.

Uses the same matching rules and the same handler code as Lambda, with an in-memory store and fake S3/SNS.
Writes outputs/match_results.csv, outputs/exceptions_by_reason.csv and outputs/alert_email.txt.
"""
import csv
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import match_handler  # noqa: E402
from matching import REASONS, PurchaseOrder  # noqa: E402
from store import InMemoryStore  # noqa: E402

DATA, OUT = ROOT / "data", ROOT / "outputs"


def load_store():
    received = defaultdict(Decimal)
    with open(DATA / "receipts.csv") as f:
        for r in csv.DictReader(f):
            received[r["po_number"]] += Decimal(r["qty_received"])
    with open(DATA / "purchase_orders.csv") as f:
        pos = [PurchaseOrder.from_item(dict(r, qty_received=received[r["po_number"]])) for r in csv.DictReader(f)]
    return InMemoryStore(pos)


class FakeS3:
    def __init__(self, files):
        self.files = files

    def get_object(self, Bucket, Key):
        data = self.files[Key]
        return {"Body": type("Body", (), {"read": lambda self: data})()}

    def put_object(self, Bucket, Key, Body, **_):
        self.files[Key] = Body


class FakeSNS:
    def __init__(self):
        self.sent = []

    def publish(self, TopicArn, Subject, Message):
        self.sent.append((Subject, Message))


def main():
    import os
    os.environ.setdefault("ALERT_TOPIC_ARN", "local")
    key = "incoming/invoices_2026-10.csv"
    s3 = FakeS3({key: (DATA / "invoices_2026-10.csv").read_bytes()})
    sns, store = FakeSNS(), load_store()
    event = {"Records": [{"s3": {"bucket": {"name": "local"}, "object": {"key": key}}}]}

    match_handler.handler(event, None, store=store, s3=s3, sns=sns)

    OUT.mkdir(exist_ok=True)
    (OUT / "match_results.csv").write_bytes(s3.files["reports/invoices_2026-10-results.csv"])
    subject, message = sns.sent[0]
    (OUT / "alert_email.txt").write_text(f"Subject: {subject}\n\n{message}\n")

    with open(OUT / "exceptions_by_reason.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["reason", "description", "invoices", "amount_at_risk"])
        rows = defaultdict(lambda: [0, Decimal(0)])
        for item in store.list_by_status("EXCEPTION"):
            for reason in item["reasons"]:
                rows[reason][0] += 1
                rows[reason][1] += item["amount_at_risk"]
        for reason, (n, amt) in sorted(rows.items(), key=lambda kv: -kv[1][1]):
            w.writerow([reason, REASONS[reason], n, f"{amt:.2f}"])

    # Second delivery of the same S3 event: nothing should change.
    again = match_handler.handler(event, None, store=store, s3=s3, sns=FakeSNS())
    print(f"Subject: {subject}\n\n{message}\n")
    print(f"Re-delivered event: {again['summaries'][0]['already_processed']} lines skipped, "
          f"{again['summaries'][0]['invoices']} re-processed")
    print(f"Wrote {OUT}/match_results.csv, exceptions_by_reason.csv, alert_email.txt")


if __name__ == "__main__":
    main()
