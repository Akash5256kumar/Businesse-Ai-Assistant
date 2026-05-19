from __future__ import annotations

_SHOP_PROMPTS: dict[str, str] = {
    "kirana": """\
══════════════════════════════════════════════
SHOP TYPE: KIRANA / GROCERY STORE
══════════════════════════════════════════════
Units      : kg, gram, litre, packet, piece, bori, paava (250g), adha (500g), pao
Common items: aata, chawal, dal, tel, sugar, cheeni, namak, ghee, maida, besan,
              suji, poha, chana, rajma, sarso, masala, mirchi, haldi, jeera,
              dhaniya, sabzi, doodh, paneer, biscuit, namkeen, soap, shampoo
Pricing    : MRP-based, per kg, per packet, loose rate
Credit     : udhaar is very common — "baad mein dega", "mahine mein dega"
Brands     : Tata, Patanjali, Amul, Fortune, Aashirvaad, MDH, Everest
Notes      : Loose items sold by weight. Multiple small items in one transaction is normal.
""",

    "hardware": """\
══════════════════════════════════════════════
SHOP TYPE: HARDWARE / BUILDING MATERIALS
══════════════════════════════════════════════
Units      : piece, pcs, kg, feet, meter, bundle, bag, box, dozen, gauge, number (no.)
Common items: rod, sariya, cement, paint, tile, brick, sand, fitting, pipe, wire,
              screw, nut, bolt, hinge, lock, switch, PVC, GI pipe, angle iron,
              sheet, plywood, putty, primer, drill bit
Pricing    : bulk rate, per piece, per kg, per bag, per bundle
Brands     : Fenesta, Berger, Asian Paints, Tata Steel, Ambuja, ACC, L&T
Gauge/size : "10 no. rod", "3/4 inch pipe", "8 gauge wire" — extract as item name
Notes      : Size/gauge is part of the item name. Bulk orders with discount are common.
             "fitting" without further detail = plumbing fitting.
""",

    "medical": """\
══════════════════════════════════════════════
SHOP TYPE: MEDICAL / PHARMACY
══════════════════════════════════════════════
Units      : strip, tablet, capsule, bottle, vial, tube, box, sachet, ml, mg
Common items: paracetamol, crocin, dolo, combiflam, amoxicillin, azithromycin,
              omeprazole, pan-d, digene, ors, b-complex, vitamin c, insulin,
              dressing, bandage, syringe, gloves
Pricing    : MRP-based, per strip, per bottle
Brands     : Cipla, Sun Pharma, Abbott, Dr Reddy, Mankind, GSK, Pfizer
Notes      : Brand names and generic names both valid — treat as same product.
             Batch no. and expiry may appear in notes — preserve them.
             "strip" = blister pack of tablets. Prescription items need customer record.
""",

    "garments": """\
══════════════════════════════════════════════
SHOP TYPE: CLOTH / GARMENTS
══════════════════════════════════════════════
Units      : meter, metre, piece, set, dozen, pair, yard
Common items: cotton, silk, linen, georgette, chiffon, saree, suit, kurta,
              shirt cloth, pant cloth, dupatta, blouse piece, lehenga, fabric,
              ready-made, stitched, unstitched
Pricing    : per meter, per piece, per set, per dozen
Notes      : "2 meter cotton 80 rupay meter" = 2m at ₹80/m = ₹160.
             Seasonal items (winter, summer, festive) are common.
             By-the-piece pricing for ready-made garments.
             Pattern/design names may appear — keep in item name.
""",

    "general": "",
}


def get_shop_context(shop_type: str) -> str:
    return _SHOP_PROMPTS.get(shop_type.lower(), _SHOP_PROMPTS["general"])
