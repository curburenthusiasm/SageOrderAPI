"""Walmart-required segments are present in the baseline generators.

These guard the specific segments Walmart's supplier portal rejects without,
so the docs are correct even when the spec-guided (Claude) pass doesn't run.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from edi_agent.core.parser import parse                # noqa: E402
from edi_agent.core.validator import validate_document  # noqa: E402
from edi_agent.generators.base import build_sscc18      # noqa: E402
from edi_agent.generators.gen_855 import generate_855   # noqa: E402
from edi_agent.generators.gen_856 import generate_856   # noqa: E402
from edi_agent.generators.gen_810 import generate_810   # noqa: E402
from edi_agent.mappings import merge_mappings           # noqa: E402

SAMPLE = os.path.join(os.path.dirname(__file__), "sample_850.edi")


def _order():
    with open(SAMPLE) as fh:
        return parse(fh.read())


def _ship_mappings():
    return merge_mappings({
        "sku_prices": {"ABC123": 29.99, "DEF456": 49.99},
        "carrier_code": "UPSN", "service_level": "GROUND",
        "ship_date": "20250605", "payment_terms_days": 30,
        "gs1_company_prefix": "0712345",
        "tracking_numbers": ["1Z999AA10123456784"],
        "packages": [
            {"tracking": "1Z999AA10123456784", "weight_lbs": 12.5,
             "lines": [{"line_num": "1", "qty_shipped": 100},
                       {"line_num": "2", "qty_shipped": 50}]},
        ],
    })


def test_sscc18_check_digit_valid():
    sscc = build_sscc18("0712345", 7)
    assert len(sscc) == 18 and sscc.isdigit()
    # Verify the GS1 mod-10 check digit recomputes.
    body, check = sscc[:17], int(sscc[17])
    total = sum(int(c) * (3 if i % 2 == 0 else 1) for i, c in enumerate(reversed(body)))
    assert (10 - (total % 10)) % 10 == check


def test_856_has_walmart_segments():
    edi = generate_856(_order(), _ship_mappings())
    validate_document(edi).raise_if_failed()
    assert "MAN*GM*" in edi                       # UCC-128 SSCC carton label
    assert "N1*SF*" in edi                        # Ship From loop
    assert "PO4*" in edi                          # pack detail
    assert "TD1*CTN*1***G*12.5*LB" in edi         # carton count + gross weight
    # SSCC carries our GS1 prefix and is 18 digits.
    man = next(s for s in edi.split("~") if s.startswith("MAN*GM*"))
    sscc = man.split("*")[2]
    assert len(sscc) == 18 and sscc.startswith("0071234")


def test_855_has_walmart_segments():
    edi = generate_855(_order(), _ship_mappings())
    validate_document(edi).raise_if_failed()
    assert "FOB*PP" in edi                        # freight terms
    assert "DTM*010*20250605" in edi              # ship date echo


def test_810_has_walmart_segments():
    edi = generate_810(_order(), _ship_mappings())
    validate_document(edi).raise_if_failed()
    assert "FOB*PP" in edi                        # freight terms
    assert "ITD*01*3****" in edi and "*30" in edi  # Net 30 payment terms


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("All Walmart-segment tests passed.")
