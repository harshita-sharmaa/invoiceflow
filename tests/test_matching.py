import json
import sys
import unittest
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import api_handler  # noqa: E402
from matching import EXCEPTION, MATCHED, Invoice, PurchaseOrder, match_invoice, process_batch  # noqa: E402
from store import InMemoryStore  # noqa: E402


def po(**kw):
    base = dict(po_number="PO-1", vendor_id="V1", item="Chair", qty_ordered=D(10), unit_price=D("100.00"),
                qty_received=D(10))
    base.update(kw)
    return PurchaseOrder(**base)


def inv(**kw):
    base = dict(vendor_id="V1", invoice_number="A-1", po_number="PO-1", invoice_date="2026-10-01",
                qty=D(10), unit_price=D("100.00"), amount=D("1000.00"))
    base.update(kw)
    return Invoice(**base)


class MatchRules(unittest.TestCase):
    def test_clean_invoice_matches(self):
        self.assertEqual(match_invoice(inv(), po()).status, MATCHED)

    def test_price_inside_tolerance_matches(self):
        r = match_invoice(inv(unit_price=D("101.50"), amount=D("1015.00")), po())
        self.assertEqual(r.status, MATCHED)

    def test_price_over_tolerance(self):
        r = match_invoice(inv(unit_price=D("110.00"), amount=D("1100.00")), po())
        self.assertEqual((r.status, r.reasons, r.amount_at_risk), (EXCEPTION, ["PRICE_VARIANCE"], D("100.00")))

    def test_billed_more_than_received(self):
        r = match_invoice(inv(), po(qty_received=D(6)))
        self.assertEqual((r.reasons, r.amount_at_risk), (["QTY_OVER_RECEIVED"], D("400.00")))

    def test_already_billed_quantity_counts(self):
        r = match_invoice(inv(qty=D(5), amount=D("500.00")), po(qty_billed=D(8)))
        self.assertEqual(r.reasons, ["QTY_OVER_RECEIVED"])

    def test_not_received(self):
        r = match_invoice(inv(), po(qty_received=D(0)))
        self.assertEqual((r.reasons, r.amount_at_risk), (["NOT_RECEIVED"], D("1000.00")))

    def test_total_typo(self):
        r = match_invoice(inv(amount=D("10000.00")), po())
        self.assertEqual((r.reasons, r.amount_at_risk), (["TOTAL_MISMATCH"], D("9000.00")))

    def test_no_po_and_wrong_vendor_hold_full_amount(self):
        self.assertEqual(match_invoice(inv(), None).reasons, ["NO_PO"])
        r = match_invoice(inv(), po(vendor_id="V2"))
        self.assertEqual((r.reasons, r.amount_at_risk), (["VENDOR_MISMATCH"], D("1000.00")))

    def test_several_reasons_at_once(self):
        r = match_invoice(inv(unit_price=D("120.00"), amount=D("1200.00")), po(qty_received=D(5)))
        self.assertEqual(r.reasons, ["QTY_OVER_RECEIVED", "PRICE_VARIANCE"])
        self.assertEqual(r.amount_at_risk, D("700.00"))

    def test_bad_row_raises(self):
        with self.assertRaises(ValueError):
            Invoice.from_row({"vendor_id": "V1", "invoice_number": "", "invoice_date": "x",
                              "qty": "1", "unit_price": "1", "amount": "1"})


class Batch(unittest.TestCase):
    def test_duplicate_and_billed_qty(self):
        store = InMemoryStore([po()])
        results, _ = process_batch([inv(qty=D(6), amount=D("600.00")), inv(qty=D(6), amount=D("600.00"))],
                                   store, "f.csv")
        self.assertEqual([r.status for r in results], [MATCHED, EXCEPTION])
        self.assertEqual(results[1].reasons, ["DUPLICATE"])
        self.assertEqual(store.get_po("PO-1").qty_billed, D(6))
        self.assertIn("V1#A-1", store.invoices)            # the original record is not overwritten
        self.assertEqual(store.invoices["V1#A-1"]["status"], MATCHED)

    def test_second_invoice_cannot_bill_the_same_receipt(self):
        store = InMemoryStore([po()])
        results, _ = process_batch([inv(), inv(invoice_number="A-2")], store, "f.csv")
        self.assertEqual(results[1].reasons, ["QTY_OVER_RECEIVED"])

    def test_redelivered_file_is_skipped(self):
        store = InMemoryStore([po()])
        batch = [inv(qty=D(4), amount=D("400.00")), inv(qty=D(4), amount=D("400.00"))]
        process_batch(batch, store, "f.csv")
        results, skipped = process_batch(batch, store, "f.csv")
        self.assertEqual((len(results), skipped), (0, 2))
        self.assertEqual(store.get_po("PO-1").qty_billed, D(4))
        self.assertEqual(len(store.invoices), 2)

    def test_same_invoice_in_a_new_file_is_duplicate(self):
        store = InMemoryStore([po()])
        process_batch([inv()], store, "oct.csv")
        results, _ = process_batch([inv()], store, "nov.csv")
        self.assertEqual(results[0].reasons, ["DUPLICATE"])


class Api(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryStore([po()])
        process_batch([inv(unit_price=D("110.00"), amount=D("1100.00"))], self.store, "f.csv")

    def call(self, route, key=None, body=None, query=None):
        event = {"routeKey": route, "pathParameters": {"key": key} if key else None,
                 "queryStringParameters": query, "body": json.dumps(body) if body else None,
                 "requestContext": {"authorizer": {"iam": {"userArn": "arn:aws:iam::1:user/ap-clerk"}}}}
        r = api_handler.handler(event, None, store=self.store)
        return r["statusCode"], json.loads(r["body"])

    def test_list_and_get(self):
        code, body = self.call("GET /invoices")
        self.assertEqual((code, body["count"]), (200, 1))
        code, body = self.call("GET /invoices/{key}", key="V1%23A-1")
        self.assertEqual((code, body["reasons"]), (200, ["PRICE_VARIANCE"]))

    def test_resolve_once(self):
        code, body = self.call("POST /invoices/{key}/resolve", key="V1#A-1",
                               body={"decision": "approved", "note": "Price increase agreed by buyer"})
        self.assertEqual((code, body["status"], body["reviewed_by"]),
                         (200, "APPROVED", "arn:aws:iam::1:user/ap-clerk"))
        code, _ = self.call("POST /invoices/{key}/resolve", key="V1#A-1", body={"decision": "REJECTED", "note": "x"})
        self.assertEqual(code, 409)

    def test_resolve_needs_note(self):
        code, _ = self.call("POST /invoices/{key}/resolve", key="V1#A-1", body={"decision": "APPROVED"})
        self.assertEqual(code, 400)

    def test_summary(self):
        code, body = self.call("GET /summary")
        self.assertEqual(body["EXCEPTION"]["amount_at_risk"], 100.0)


if __name__ == "__main__":
    unittest.main()
