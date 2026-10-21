"""Build the synthetic Westbridge College data: purchase orders, receipts and one month of vendor invoices.

The base values come from a fixed seed. The invoice problems (price changes, over-billing, duplicates, ...)
are planted on purpose at fixed positions so the results in the README stay the same on every run.
"""
import csv
import random
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
random.seed(26)

VENDORS = {
    "V100": ("Campus Office Supply", "CO", [("Copy paper (case)", 42), ("Toner cartridge", 118), ("Desk organizer", 24)]),
    "V110": ("Bayline IT Solutions", "BIT", [("Laptop", 1149), ("Docking station", 219), ("Monitor 27in", 289)]),
    "V120": ("Redwood Furniture Co", "RF", [("Classroom chair", 96), ("Faculty desk", 640), ("Bookshelf", 210)]),
    "V130": ("Summit Lab Equipment", "SLE", [("Microscope", 865), ("Lab goggles (box)", 58), ("Beaker set", 74)]),
    "V140": ("Coastal Facilities Services", "CFS", [("HVAC filter", 36), ("Floor cleaner (drum)", 155), ("LED panel", 88)]),
    "V150": ("Pioneer Print & Copy", "PPC", [("Brochure print run", 640), ("Banner", 120), ("Course packet", 18)]),
    "V160": ("Golden Gate AV Systems", "GGAV", [("Projector", 1290), ("Wireless mic kit", 349), ("HDMI switcher", 165)]),
    "V170": ("Mission Catering", "MC", [("Event lunch (per head)", 21), ("Coffee service", 95), ("Snack tray", 48)]),
}

N_PO = 40
PARTIAL = {3, 10, 17, 24, 31, 38}      # received about half
NOT_RECEIVED = {5, 19, 33, 36}         # nothing received yet

# Planted invoice problems, by PO index.
PRICE_UP = {2: "1.08", 13: "1.12", 22: "1.06", 29: "1.10"}   # billed above PO price (over 2% tolerance)
PRICE_DRIFT = {7: "1.01", 26: "1.015"}                       # small drift inside tolerance -> still matches
OVERBILL = {3, 17, 31}                                       # partial receipt, billed the full PO quantity
BILLED_NOT_RECEIVED = {5, 19, 33}                            # invoiced before anything arrived
WRONG_VENDOR = 15                                            # invoice cites another vendor's PO
TYPO_TOTAL = 11                                              # line total keyed with a slipped decimal
DUPLICATE_OF = [8, 21]                                       # resent later with the same invoice number


def money(x):
    return Decimal(x).quantize(Decimal("0.01"))


def main():
    vendor_ids = list(VENDORS)
    start = date(2026, 8, 3)
    pos, receipts, invoices = [], [], []

    for i in range(N_PO):
        vid = random.choice(vendor_ids)
        item, list_price = random.choice(VENDORS[vid][2])
        unit = money(list_price * random.uniform(0.95, 1.05))
        qty = random.randint(2, 12) if list_price > 500 else random.randint(5, 60)
        ordered = start + timedelta(days=random.randint(0, 50))
        po = {"po_number": f"PO-26-{1001 + i}", "vendor_id": vid, "vendor_name": VENDORS[vid][0],
              "item": item, "qty_ordered": qty, "unit_price": unit, "ordered_on": ordered}
        pos.append(po)

        if i in NOT_RECEIVED:
            continue
        got = max(1, qty // 2) if i in PARTIAL else qty
        received_on = ordered + timedelta(days=random.randint(5, 20))
        receipts.append({"receipt_id": f"GR-{5001 + len(receipts)}", "po_number": po["po_number"],
                         "received_on": received_on, "qty_received": got})
        po["_received"], po["_received_on"] = got, received_on

    seq = 4400
    for i, po in enumerate(pos):
        if i in NOT_RECEIVED and i not in BILLED_NOT_RECEIVED:
            continue
        seq += random.randint(1, 9)
        vid = po["vendor_id"]
        qty = po["qty_ordered"] if (i in OVERBILL or i in BILLED_NOT_RECEIVED) else po["_received"]
        price = po["unit_price"]
        if i in PRICE_UP:
            price = money(price * Decimal(PRICE_UP[i]))
        if i in PRICE_DRIFT:
            price = money(price * Decimal(PRICE_DRIFT[i]))
        amount = money(price * qty)
        if i == TYPO_TOTAL:
            amount = money(amount * 10)
        if i == WRONG_VENDOR:
            vid = next(v for v in vendor_ids if v != po["vendor_id"])
        inv_date = po.get("_received_on", po["ordered_on"] + timedelta(days=7)) + timedelta(days=random.randint(1, 6))
        invoices.append({"vendor_id": vid, "vendor_name": VENDORS[vid][0],
                         "invoice_number": f"{VENDORS[vid][1]}-{seq}", "po_number": po["po_number"],
                         "invoice_date": inv_date, "qty": qty, "unit_price": price, "amount": amount})

    # Invoices with no usable PO: one left blank, one citing a PO that does not exist.
    for po_number, vid, desc_price, qty in [("", "V170", Decimal("21.00"), 85), ("PO-26-1099", "V140", Decimal("155.00"), 4)]:
        seq += 7
        invoices.append({"vendor_id": vid, "vendor_name": VENDORS[vid][0], "invoice_number": f"{VENDORS[vid][1]}-{seq}",
                         "po_number": po_number, "invoice_date": date(2026, 10, 9), "qty": qty,
                         "unit_price": desc_price, "amount": money(desc_price * qty)})

    # Duplicates: the same invoice resent at the end of the month.
    by_po = {inv["po_number"]: inv for inv in invoices}
    for i in DUPLICATE_OF:
        dup = dict(by_po[pos[i]["po_number"]])
        dup["invoice_date"] = date(2026, 10, 27)
        invoices.append(dup)

    write(DATA / "purchase_orders.csv", pos,
          ["po_number", "vendor_id", "vendor_name", "item", "qty_ordered", "unit_price", "ordered_on"])
    write(DATA / "receipts.csv", receipts, ["receipt_id", "po_number", "received_on", "qty_received"])
    write(DATA / "invoices_2026-10.csv", invoices,
          ["vendor_id", "vendor_name", "invoice_number", "po_number", "invoice_date", "qty", "unit_price", "amount"])
    print(f"{len(pos)} POs, {len(receipts)} receipts, {len(invoices)} invoices -> {DATA}")


def write(path, rows, cols):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    main()
