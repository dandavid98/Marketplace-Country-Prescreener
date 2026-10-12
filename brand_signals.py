"""Generic / placeholder listing-brand detection.

Sellers who don't want to (or can't) name a real brand fill Walmart's Brand
attribute with a placeholder: "Generic", "Unbranded", "NOBRAND", "N/A" ...
The "Generic listing brands only" scan filter uses this to keep only those.

Matching is case-insensitive AND ignores spaces/punctuation, so "No Brand",
"no-brand", "NO_BRAND" and "NoBrand" all collapse to "nobrand". An empty brand
is deliberately NOT generic: that usually means the page failed to load, not
that the seller declared no brand.
"""

from __future__ import annotations

import re

# Stored pre-normalized (lowercase, letters/digits only) -- see normalize_brand.
GENERIC_BRANDS: frozenset[str] = frozenset({
    # "generic" family
    "generic", "generics", "genericbrand", "genericbrands", "genericunbranded",
    # "unbranded" family
    "unbranded", "unbrand", "unbrandedgeneric", "notbranded", "nonbrand", "nonbranded",
    "brandless", "withoutbrand",
    # "no brand" family ("No Brand", "NOBRAND", "no-brand", "No Brand Name")
    "nobrand", "nobrands", "nobranded", "nobrandname", "noname", "nonamebrand",
    # unknown / unspecified placeholders
    "unknown", "unknownbrand", "brandunknown", "unspecified", "notspecified",
    "na", "none", "null", "nil", "notapplicable", "doesnotapply",
    "other", "others",
    # generic manufacturing labels
    "oem", "whitelabel", "privatelabel",
})


def normalize_brand(brand: str) -> str:
    """Lowercase and drop everything but letters/digits ("N/A" -> "na")."""
    return re.sub(r"[^a-z0-9]", "", (brand or "").lower())


def is_generic_brand(brand: str) -> bool:
    """True if the listing's Brand attribute is a generic placeholder."""
    return normalize_brand(brand) in GENERIC_BRANDS
