"""Lambda: runs when an invoice CSV lands in s3://<bucket>/incoming/.

Matches every invoice, saves results to DynamoDB, writes a results CSV to reports/, and emails AP
through SNS when anything needs review.
"""
import csv
import io
import json
import logging
import os
import urllib.parse

from matching import REASONS, Invoice, process_batch, summarize
from store import DynamoStore

log = logging.getLogger()
log.setLevel(logging.INFO)

REPORT_COLUMNS = ["invoice_key", "vendor_id", "vendor_name", "invoice_number", "po_number", "invoice_date",
                  "qty", "unit_price", "amount", "status", "reasons", "amount_at_risk"]


def handler(event, context, store=None, s3=None, sns=None):
    if store is None:
        import boto3
        store = DynamoStore(os.environ["PO_TABLE"], os.environ["INVOICE_TABLE"])
        s3, sns = boto3.client("s3"), boto3.client("sns")

    summaries = []
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8-sig")

        invoices, bad_rows = [], []
        for line, row in enumerate(csv.DictReader(io.StringIO(body)), start=2):
            try:
                invoices.append(Invoice.from_row(row))
            except ValueError as e:
                bad_rows.append(line)
                log.warning("line %s skipped: %s", line, e)

        results, skipped = process_batch(invoices, store, source_prefix=key)
        summary = summarize(results)
        summary.update(file=key, bad_rows=bad_rows, already_processed=skipped)
        log.info(json.dumps(summary, default=str))

        report_key = "reports/" + key.rsplit("/", 1)[-1].replace(".csv", "") + "-results.csv"
        s3.put_object(Bucket=bucket, Key=report_key, Body=to_csv(results).encode(), ContentType="text/csv")

        if summary["exceptions"] or bad_rows:
            sns.publish(TopicArn=os.environ["ALERT_TOPIC_ARN"],
                        Subject=f"InvoiceFlow: {summary['exceptions']} invoices need review",
                        Message=alert_text(summary, bucket, report_key))
        summaries.append(summary)
    return {"files": len(summaries), "summaries": json.loads(json.dumps(summaries, default=str))}


def to_csv(results):
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=REPORT_COLUMNS, extrasaction="ignore")
    w.writeheader()
    for r in results:
        item = r.to_item()
        item["reasons"] = "; ".join(item["reasons"])
        w.writerow(item)
    return out.getvalue()


def alert_text(s, bucket, report_key):
    lines = [
        f"File: {s['file']}",
        f"Invoices: {s['invoices']}  |  matched and cleared for payment: {s['matched']} (${s['auto_approved_amount']:,.2f})",
        f"On hold for review: {s['exceptions']}  |  $ not supported by PO/receipt: ${s['amount_at_risk']:,.2f}",
        "",
        "By reason:",
    ]
    for reason, row in sorted(s["by_reason"].items(), key=lambda kv: -kv[1]["amount_at_risk"]):
        lines.append(f"  {reason:<18} {row['invoices']:>3} invoices  ${row['amount_at_risk']:>12,.2f}  {REASONS[reason]}")
    if s["bad_rows"]:
        lines.append(f"\nUnreadable rows (not processed): lines {s['bad_rows']}")
    lines.append(f"\nFull results: s3://{bucket}/{report_key}")
    return "\n".join(lines)
