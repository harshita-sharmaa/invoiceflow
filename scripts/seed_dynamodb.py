"""Load the sample POs (with received quantities rolled up from receipts.csv) into the deployed PO table.

    python3 scripts/seed_dynamodb.py <PurchaseOrdersTableName>

The table name is in the `sam deploy` outputs. Needs boto3 and AWS credentials.
"""
import csv
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

import boto3

DATA = Path(__file__).resolve().parent.parent / "data"


def main(table_name):
    received = defaultdict(Decimal)
    with open(DATA / "receipts.csv") as f:
        for r in csv.DictReader(f):
            received[r["po_number"]] += Decimal(r["qty_received"])

    table = boto3.resource("dynamodb").Table(table_name)
    n = 0
    with open(DATA / "purchase_orders.csv") as f, table.batch_writer() as batch:
        for r in csv.DictReader(f):
            batch.put_item(Item={
                "po_number": r["po_number"], "vendor_id": r["vendor_id"], "vendor_name": r["vendor_name"],
                "item": r["item"], "qty_ordered": Decimal(r["qty_ordered"]), "unit_price": Decimal(r["unit_price"]),
                "ordered_on": r["ordered_on"], "qty_received": received[r["po_number"]], "qty_billed": Decimal(0),
            })
            n += 1
    print(f"Loaded {n} purchase orders into {table_name}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
