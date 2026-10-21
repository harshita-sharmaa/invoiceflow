"""Lambda behind API Gateway (HTTP API): the AP review queue.

GET  /summary                      counts and $ by status
GET  /invoices?status=EXCEPTION    list invoices with a status (default EXCEPTION)
GET  /invoices/{key}               one invoice (key = VENDOR#INVOICE_NUMBER, URL-encoded)
POST /invoices/{key}/resolve       {"decision": "APPROVED" | "REJECTED", "note": "..."}
"""
import json
import os
import urllib.parse
from decimal import Decimal

from matching import APPROVED, EXCEPTION, MATCHED, REJECTED
from store import DynamoStore

_store = None


def handler(event, context, store=None):
    global _store
    if store is None:
        _store = _store or DynamoStore(os.environ["PO_TABLE"], os.environ["INVOICE_TABLE"])
        store = _store

    route = event.get("routeKey", "")
    params = event.get("pathParameters") or {}
    key = urllib.parse.unquote(params.get("key", ""))

    if route == "GET /summary":
        return respond(200, summary(store.all_invoices()))

    if route == "GET /invoices":
        status = ((event.get("queryStringParameters") or {}).get("status") or EXCEPTION).upper()
        if status not in (MATCHED, EXCEPTION, APPROVED, REJECTED):
            return respond(400, {"error": f"unknown status {status}"})
        items = sorted(store.list_by_status(status), key=lambda i: -Decimal(i["amount_at_risk"]))
        return respond(200, {"status": status, "count": len(items), "invoices": items})

    if route == "GET /invoices/{key}":
        item = store.get_invoice(key)
        return respond(200, item) if item else respond(404, {"error": "invoice not found"})

    if route == "POST /invoices/{key}/resolve":
        try:
            body = json.loads(event.get("body") or "{}")
        except json.JSONDecodeError:
            return respond(400, {"error": "body must be JSON"})
        decision = str(body.get("decision", "")).upper()
        note = str(body.get("note", "")).strip()
        if decision not in (APPROVED, REJECTED) or not note:
            return respond(400, {"error": "need decision APPROVED or REJECTED and a note"})
        item = store.resolve(key, decision, note, reviewer(event))
        if item is None:
            return respond(409, {"error": "invoice is not an open exception"})
        return respond(200, item)

    return respond(404, {"error": f"no route {route}"})


def reviewer(event):
    """Who made the call: the IAM identity that signed the request (the API uses IAM auth)."""
    iam = ((event.get("requestContext") or {}).get("authorizer") or {}).get("iam") or {}
    return iam.get("userArn") or "unknown"


def summary(items):
    out = {}
    for i in items:
        row = out.setdefault(i["status"], {"invoices": 0, "amount": Decimal(0), "amount_at_risk": Decimal(0)})
        row["invoices"] += 1
        row["amount"] += Decimal(i["amount"])
        row["amount_at_risk"] += Decimal(i["amount_at_risk"])
    return out


def respond(code, body):
    return {"statusCode": code, "headers": {"Content-Type": "application/json"},
            "body": json.dumps(body, default=lambda d: float(d) if isinstance(d, Decimal) else str(d))}
