"""Where POs and match results live: DynamoDB in AWS, a dict for the local demo and tests.

Both classes have the same methods, so matching.process_batch and the handlers do not care which one they get.
"""
from decimal import Decimal

from matching import EXCEPTION, PurchaseOrder


class InMemoryStore:
    def __init__(self, pos=None):
        self.pos = {p.po_number: p for p in (pos or [])}
        self.invoices = {}

    def get_po(self, po_number):
        return self.pos.get(po_number)

    def add_billed(self, po_number, qty):
        self.pos[po_number].qty_billed += qty

    def get_invoice(self, key):
        return self.invoices.get(key)

    def save_result(self, result):
        item = result.to_item()
        self.invoices[item["invoice_key"]] = item

    def list_by_status(self, status):
        return [i for i in self.invoices.values() if i["status"] == status]

    def all_invoices(self):
        return list(self.invoices.values())

    def resolve(self, key, status, note, reviewer):
        """Move an EXCEPTION to APPROVED/REJECTED. Returns the updated item, or None if it was not open."""
        item = self.invoices.get(key)
        if not item or item["status"] != EXCEPTION:
            return None
        item.update(status=status, review_note=note, reviewed_by=reviewer)
        return item


class DynamoStore:
    def __init__(self, po_table, invoice_table, resource=None):
        if resource is None:
            import boto3
            resource = boto3.resource("dynamodb")
        self.pos = resource.Table(po_table)
        self.invoices = resource.Table(invoice_table)

    def get_po(self, po_number):
        item = self.pos.get_item(Key={"po_number": po_number}).get("Item")
        return PurchaseOrder.from_item(item) if item else None

    def add_billed(self, po_number, qty):
        self.pos.update_item(Key={"po_number": po_number}, UpdateExpression="ADD qty_billed :q",
                             ExpressionAttributeValues={":q": Decimal(qty)})

    def get_invoice(self, key):
        return self.invoices.get_item(Key={"invoice_key": key}).get("Item")

    def save_result(self, result):
        self.invoices.put_item(Item=result.to_item())

    def list_by_status(self, status):
        from boto3.dynamodb.conditions import Key
        items, kwargs = [], {"IndexName": "status-index", "KeyConditionExpression": Key("status").eq(status)}
        while True:
            page = self.invoices.query(**kwargs)
            items += page["Items"]
            if "LastEvaluatedKey" not in page:
                return items
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def all_invoices(self):
        items, kwargs = [], {}
        while True:
            page = self.invoices.scan(**kwargs)
            items += page["Items"]
            if "LastEvaluatedKey" not in page:
                return items
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def resolve(self, key, status, note, reviewer):
        from botocore.exceptions import ClientError
        try:
            return self.invoices.update_item(
                Key={"invoice_key": key},
                UpdateExpression="SET #s = :new, review_note = :n, reviewed_by = :r",
                ConditionExpression="#s = :open",   # only open exceptions can be resolved, and only once
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":new": status, ":n": note, ":r": reviewer, ":open": EXCEPTION},
                ReturnValues="ALL_NEW",
            )["Attributes"]
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return None
            raise
