"""The tidy output schema — one row per hospital / code / code type / setting /
billing class / payer / plan / rate type.

Everything downstream depends on this grain, so it is defined once, here.
"""

from dataclasses import dataclass, asdict, fields
from typing import Optional

# Rate types. gross, discounted_cash, min and max are hospital-wide, so they
# carry no payer or plan. Only `negotiated` does.
RATE_GROSS = "gross"
RATE_CASH = "discounted_cash"
RATE_NEGOTIATED = "negotiated"
RATE_MIN = "min"
RATE_MAX = "max"
# The hospital's own dollar estimate of what a payer will actually allow, used
# when the contract is an algorithm/percentage. Frederick Health reports its
# HSCRC all-payer rate ONLY here, with negotiated_dollar blank.
RATE_ESTIMATED = "estimated"

PAYERLESS_RATE_TYPES = {RATE_GROSS, RATE_CASH, RATE_MIN, RATE_MAX}


@dataclass(slots=True)
class ChargeRow:
    # who
    hospital_name: str
    hospital_ein: Optional[str]
    # what
    code: str
    code_type: str
    description: Optional[str]
    setting: Optional[str]
    billing_class: Optional[str]
    # who pays
    payer_name: Optional[str]
    plan_name: Optional[str]
    # how much
    rate_type: str
    rate_dollar: Optional[float]
    rate_percent: Optional[float]
    contracting_method: Optional[str]
    # provenance
    last_updated_on: Optional[str]
    source_file: str

    def as_dict(self) -> dict:
        return asdict(self)


COLUMNS = [f.name for f in fields(ChargeRow)]
