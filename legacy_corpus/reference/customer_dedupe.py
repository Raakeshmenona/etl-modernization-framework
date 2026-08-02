"""Reference for perl/customer_dedupe.pl."""
from __future__ import annotations

import re
from typing import Dict

from equivalence.perl_semantics import perl_sort, read_delimited


def _normalise(value: str) -> str:
    """s/^\\s+//; s/\\s+$//; s/\\s+/ /g; uc"""
    return re.sub(r"\s+", " ", value.strip()).upper()


def run(inputs: Dict[str, str]) -> str:
    rows = read_delimited(inputs["customers"])
    body = rows[1:]                                   # my $header = <$fh>;

    first_id_for_key: Dict[str, str] = {}
    dup_count: Dict[str, int] = {}
    row_for_id: Dict[str, str] = {}

    for f in body:
        cid, first, last, dob = f[0], f[1], f[2], f[3]
        first = _normalise(first)
        last = _normalise(last)
        key = "|".join([last, first, dob])

        if key in first_id_for_key:
            survivor = first_id_for_key[key]
            dup_count[survivor] = dup_count.get(survivor, 0) + 1
            continue

        first_id_for_key[key] = cid
        row_for_id[cid] = ",".join([cid, "%s %s" % (last, first), dob])

    out = ["customer_id,norm_name,dob,dup_count"]
    for cid in perl_sort(row_for_id.keys()):          # string sort, not numeric
        out.append("%s,%d" % (row_for_id[cid], dup_count.get(cid, 0)))
    return "\n".join(out) + "\n"
