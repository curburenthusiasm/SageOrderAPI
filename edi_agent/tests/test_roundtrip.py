"""Roundtrip test: parse the sample 850, generate all four docs, validate them."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from edi_agent.core.parser import parse, EDIParseError  # noqa: E402
from edi_agent.core.validator import validate_document    # noqa: E402
from edi_agent.generators.gen_997 import generate_997     # noqa: E402
from edi_agent.generators.gen_855 import generate_855     # noqa: E402
from edi_agent.generators.gen_856 import generate_856     # noqa: E402
from edi_agent.generators.gen_810 import generate_810     # noqa: E402
from edi_agent.mappings import merge_mappings             # noqa: E402

SAMPLE = os.path.join(os.path.dirname(__file__), "sample_850.edi")


def load_order():
    with open(SAMPLE) as fh:
        return parse(fh.read())


def test_parse_850():
    order = load_order()
    assert order.po_number == "4500012345"
    assert order.po_date == "20250529"
    assert len(order.lines) == 2
    assert order.ship_to.name == "ACME RETAIL STORE #12"
    assert order.ship_to.city == "ANYTOWN"
    assert order.vendor.id == "JEFF01"
    assert order.partner_isa_id == "ACMERETAIL"
    assert order.receiver_isa_id == "JEFFCOFIBRES"

    l1 = order.lines[0]
    assert l1.qty_ordered == 100
    assert l1.uom == "EA"
    assert l1.vendor_part == "ABC123"
    assert l1.buyer_part == "BUY-A"
    assert l1.upc == "012345678905"
    assert l1.description == "PREMIUM FIBER WIDGET"
    assert abs(l1.unit_price - 29.99) < 1e-6

    assert order.requested_ship_date == "20250605"
    assert order.requested_delivery_date == "20250610"


def test_parse_rejects_bad_ctt():
    bad = (
        "ISA*00*          *00*          *ZZ*A              *ZZ*B              "
        "*250529*1200*U*00401*000000001*0*P*>~GS*PO*A*B*20250529*1200*1*X*004010~"
        "ST*850*0001~BEG*00*SA*PO1**20250529~"
        "N1*ST*X*92*1~PO1*1*5*EA*1.00~CTT*9~SE*5*0001~GE*1*1~IEA*1*000000001~"
    )
    try:
        parse(bad)
        assert False, "expected EDIParseError on CTT mismatch"
    except EDIParseError as exc:
        assert "CTT" in str(exc)


def test_generate_997():
    order = load_order()
    edi = generate_997(order)
    validate_document(edi).raise_if_failed()
    assert "ST*997*" in edi
    assert "AK1*PO*1" in edi
    assert "AK2*850*0001" in edi
    assert "AK5*A" in edi
    # Mirrors inbound interchange control number.
    assert "000000001" in edi


def test_generate_855():
    order = load_order()
    mappings = merge_mappings({"sku_prices": {"ABC123": 31.50}, "acknowledgment_code": "AC"})
    edi = generate_855(order, mappings)
    validate_document(edi).raise_if_failed()
    assert "ST*855*" in edi
    assert "BAK*00*AC*4500012345" in edi
    assert "PO1*1*100*EA*31.50" in edi   # overridden price applied
    assert "CTT*2*150" in edi             # qty hash 100 + 50


def test_generate_856():
    order = load_order()
    mappings = merge_mappings({
        "ship_date": "20250605", "carrier_code": "UPSN", "ship_method": "GROUND",
        "tracking_numbers": {"4500012345": ["1Z999AA10123456784"]},
    })
    edi = generate_856(order, mappings)
    validate_document(edi).raise_if_failed()
    assert "ST*856*" in edi
    assert "BSN*00*" in edi
    assert "HL*1**S" in edi
    assert "HL*2*1*O" in edi
    assert "HL*3*2*I" in edi
    assert "TD5*" in edi
    assert "1Z999AA10123456784" in edi


def test_generate_810():
    order = load_order()
    mappings = merge_mappings({
        "sku_prices": {"ABC123": 29.99, "DEF456": 49.99},
        "invoice_number": "INV1001", "invoice_date": "20250605",
        "carrier_code": "UPSN",
        "tracking_numbers": {"4500012345": ["1Z999AA10123456784"]},
    })
    edi = generate_810(order, mappings)
    validate_document(edi).raise_if_failed()
    assert "ST*810*" in edi
    assert "BIG*20250605*INV1001*20250529*4500012345" in edi
    # Total = 100*29.99 + 50*49.99 = 2999.00 + 2499.50 = 5498.50 -> 549850 cents
    assert "TDS*549850" in edi


def test_full_pipeline():
    """Parse once, then produce every document and validate each."""
    order = load_order()
    mappings = merge_mappings({
        "sku_prices": {"ABC123": 29.99, "DEF456": 49.99},
        "carrier_code": "UPSN", "ship_method": "GROUND",
        "ship_date": "20250605",
        "tracking_numbers": {"4500012345": ["1Z999AA10123456784"]},
    })
    for edi in (generate_997(order), generate_855(order, mappings),
                generate_856(order, mappings), generate_810(order, mappings)):
        result = validate_document(edi)
        assert result.ok, result.errors


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("All roundtrip tests passed.")
