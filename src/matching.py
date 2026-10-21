"""Three-way match: invoice vs. purchase order vs. goods receipt.

Pure Python with no AWS calls, so the same rules run in Lambda, in the local demo and in the tests.
"""
from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Optional

PRICE_TOLERANCE = Decimal("0.02")   # unit price may be up to 2% above the PO price
CENT = Decimal("0.01")

MATCHED = "MATCHED"            # ready to pay, no human needed
EXCEPTION = "EXCEPTION"        # on hold for AP review
APPROVED = "APPROVED"          # exception approved by a reviewer
REJECTED = "REJECTED"          # exception rejected by a reviewer

REASONS = {
    "DUPLICATE": "Same vendor and invoice number already received",
    "NO_PO": "PO number missing or not found",
    "VENDOR_MISMATCH": "Invoice vendor is not the vendor on the PO",
    "NOT_RECEIVED": "Nothing received against this PO yet",
    "QTY_OVER_RECEIVED": "Billed quantity is more than received and not yet billed",
    "PRICE_VARIANCE": "Unit price more than 2% above the PO price",
    "TOTAL_MISMATCH": "Invoice amount does not equal quantity x unit price",
}


def _dec(value):
    return Decimal(str(value).replace(",", "").strip() or "0")


@dataclass
class Invoice:
    vendor_id: str
    invoice_number: str
    po_number: str
    invoice_date: str
    qty: Decimal
    unit_price: Decimal
    amount: Decimal
    vendor_name: str = ""

    @property
    def key(self):
        return f"{self.vendor_id}#{self.invoice_number}"

    @classmethod
    def from_row(cls, row):
        """Build from one CSV row. Raises ValueError on a row that cannot be read."""
        try:
            inv = cls(vendor_id=row["vendor_id"].strip(), invoice_number=row["invoice_number"].strip(),
                      po_number=(row.get("po_number") or "").strip(), invoice_date=row["invoice_date"].strip(),
                      qty=_dec(row["qty"]), unit_price=_dec(row["unit_price"]), amount=_dec(row["amount"]),
                      vendor_name=(row.get("vendor_name") or "").strip())
        except (KeyError, ArithmeticError, AttributeError) as e:
            raise ValueError(f"unreadable invoice row: {row}") from e
        if not inv.vendor_id or not inv.invoice_number:
            raise ValueError(f"invoice row missing vendor or invoice number: {row}")
        return inv


@dataclass
class PurchaseOrder:
    po_number: str
    vendor_id: str
    item: str
    qty_ordered: Decimal
    unit_price: Decimal
    qty_received: Decimal = Decimal(0)
    qty_billed: Decimal = Decimal(0)    # already matched on earlier invoices

    @classmethod
    def from_item(cls, item):
        return cls(po_number=item["po_number"], vendor_id=item["vendor_id"], item=item.get("item", ""),
                   qty_ordered=_dec(item["qty_ordered"]), unit_price=_dec(item["unit_price"]),
                   qty_received=_dec(item.get("qty_received", 0)), qty_billed=_dec(item.get("qty_billed", 0)))

    @property
    def open_to_bill(self):
        return max(Decimal(0), self.qty_received - self.qty_billed)


@dataclass
class MatchResult:
    invoice: Invoice
    status: str
    reasons: List[str] = field(default_factory=list)
    amount_at_risk: Decimal = Decimal(0)   # $ that the PO and receipt do not support
    source: str = ""                        # file and line it came from, used to skip S3 re-deliveries
    record_key: str = ""

    def to_item(self):
        """Flat dict for DynamoDB and the CSV report."""
        inv = self.invoice
        return {
            "invoice_key": self.record_key or inv.key, "vendor_id": inv.vendor_id, "vendor_name": inv.vendor_name,
            "invoice_number": inv.invoice_number, "po_number": inv.po_number or "-", "invoice_date": inv.invoice_date,
            "qty": inv.qty, "unit_price": inv.unit_price, "amount": inv.amount, "status": self.status,
            "reasons": self.reasons, "amount_at_risk": self.amount_at_risk, "source": self.source,
        }


def match_invoice(inv: Invoice, po: Optional[PurchaseOrder], duplicate: bool = False) -> MatchResult:
    """Apply the match rules to one invoice. `po` is None when the PO was not found."""
    if duplicate:
        return MatchResult(inv, EXCEPTION, ["DUPLICATE"], inv.amount)
    if po is None:
        return MatchResult(inv, EXCEPTION, ["NO_PO"], inv.amount)
    if po.vendor_id != inv.vendor_id:
        return MatchResult(inv, EXCEPTION, ["VENDOR_MISMATCH"], inv.amount)

    reasons = []
    if po.qty_received == 0:
        reasons.append("NOT_RECEIVED")
    elif inv.qty > po.open_to_bill:
        reasons.append("QTY_OVER_RECEIVED")
    if inv.unit_price > po.unit_price * (1 + PRICE_TOLERANCE):
        reasons.append("PRICE_VARIANCE")
    if abs(inv.qty * inv.unit_price - inv.amount) > CENT:
        reasons.append("TOTAL_MISMATCH")

    if not reasons:
        return MatchResult(inv, MATCHED)
    supported = min(inv.qty, po.open_to_bill) * po.unit_price
    return MatchResult(inv, EXCEPTION, reasons, max(Decimal(0), inv.amount - supported).quantize(CENT))


def process_batch(invoices, store, source_prefix=""):
    """Match a file of invoices in order and save each result.

    Returns (results, skipped). A line already saved from the same file and line is skipped, so a
    re-delivered S3 event does not double-count billed quantities or flag the whole file as duplicates.
    """
    results, skipped = [], 0
    for line, inv in enumerate(invoices, start=2):     # line 1 is the CSV header
        source = f"{source_prefix}:{line}"
        existing = store.get_invoice(inv.key)
        if existing and existing.get("source") == source:
            skipped += 1
            continue
        if existing and store.get_invoice(f"{inv.key}#DUP#{source}"):
            skipped += 1
            continue

        po = store.get_po(inv.po_number) if inv.po_number else None
        result = match_invoice(inv, po, duplicate=existing is not None)
        result.source = source
        result.record_key = f"{inv.key}#DUP#{source}" if existing else inv.key
        store.save_result(result)
        if result.status == MATCHED:
            store.add_billed(po.po_number, inv.qty)
        results.append(result)
    return results, skipped


def summarize(results):
    """Counts and $ by status and by reason, for the alert email and the report."""
    out = {"invoices": len(results), "matched": 0, "exceptions": 0, "total_billed": Decimal(0),
           "auto_approved_amount": Decimal(0), "amount_at_risk": Decimal(0), "by_reason": {}}
    for r in results:
        out["total_billed"] += r.invoice.amount
        if r.status == MATCHED:
            out["matched"] += 1
            out["auto_approved_amount"] += r.invoice.amount
            continue
        out["exceptions"] += 1
        out["amount_at_risk"] += r.amount_at_risk
        for reason in r.reasons:
            row = out["by_reason"].setdefault(reason, {"invoices": 0, "amount_at_risk": Decimal(0)})
            row["invoices"] += 1
            row["amount_at_risk"] += r.amount_at_risk
    return out
