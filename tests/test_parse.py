"""Prove the parser lands on the tidy grain: one row = one price.

Fixtures are synthetic. The prices in them are made up on purpose so that a
real hospital's numbers can never leak into a test expectation.
"""

from pathlib import Path

import pytest

from md_hospital_prices.parse import (
    decode_wide_header,
    detect_format,
    ein_from_filename,
    parse_file,
    read_metadata,
    _to_float,
)

FIXTURES = Path(__file__).parent / "fixtures"
WIDE = FIXTURES / "52-0591612_test-wide_standardcharges.csv"
TALL = FIXTURES / "52-1234567_test-tall_standardcharges.csv"


# --- small units -----------------------------------------------------------

def test_ein_comes_from_the_filename():
    assert ein_from_filename(WIDE) == "52-0591612"
    assert ein_from_filename(Path("no_ein_here.csv")) is None


@pytest.mark.parametrize(
    "raw,expected",
    [("$1,000.00", 1000.0), ("42.5", 42.5), ("", None), ("N/A", None),
     ("NULL", None), ("-", None), ("  612.50 ", 612.5)],
)
def test_money_parsing(raw, expected):
    assert _to_float(raw) == expected


def test_blank_is_none_not_zero():
    """A blank rate means 'no contract with this payer', not a free procedure."""
    assert _to_float("") is None
    assert _to_float("") != 0.0


def test_unknown_columns_are_reported_not_dropped():
    _plain, _payers, unknown = decode_wide_header(
        ["description", "standard_charge|gross", "weird|thing|here"]
    )
    assert unknown == ["weird|thing|here"]


# --- format detection ------------------------------------------------------

def test_format_detection():
    assert detect_format(["description", "payer_name", "standard_charge|gross"]) == "tall"
    assert detect_format(
        ["description", "standard_charge|Aetna|Choice POS II|negotiated_dollar"]
    ) == "wide"


def test_metadata_row_is_read():
    meta = read_metadata(WIDE)
    assert meta["hospital_name"] == "Test Wide Hospital"
    assert meta["last_updated_on"] == "2026-01-01"


# --- the grain ------------------------------------------------------------

def test_wide_file_row_count():
    rows = list(parse_file(WIDE))
    # 2 codes x (4 hospital-wide + 2 negotiated + 1 estimated)
    assert len(rows) == 14


def test_payer_without_a_rate_is_not_emitted():
    rows = list(parse_file(WIDE))
    assert all(r.payer_name != "UnitedHealthcare" for r in rows)


def test_one_row_is_exactly_one_price():
    """The grain Merton specified: one hospital, one code, one code type, one
    setting, one billing class, one payer, one plan, one rate type."""
    rows = list(parse_file(WIDE))
    negotiated = [
        r for r in rows
        if r.code == "99213" and r.payer_name == "Aetna" and r.rate_type == "negotiated"
    ]
    assert len(negotiated) == 1
    r = negotiated[0]
    assert r.hospital_name == "Test Wide Hospital"
    assert r.hospital_ein == "52-0591612"
    assert r.code_type == "CPT"
    assert r.setting == "outpatient"
    assert r.billing_class == "professional"
    assert r.plan_name == "Choice POS II"
    assert r.rate_dollar == 612.5
    assert r.rate_percent is None
    assert r.contracting_method == "fee schedule"
    assert r.last_updated_on == "2026-01-01"
    assert r.source_file == WIDE.name


def test_percent_rates_land_in_their_own_column():
    rows = list(parse_file(WIDE))
    cf = [r for r in rows if r.payer_name == "CareFirst" and r.code == "99213"
          and r.rate_type == "negotiated"]
    assert len(cf) == 1
    assert cf[0].rate_dollar is None
    assert cf[0].rate_percent == 42.5
    assert cf[0].contracting_method == "percent of total billed charges"


def test_hospital_wide_rates_have_no_payer():
    rows = list(parse_file(WIDE))
    gross = [r for r in rows if r.code == "99213" and r.rate_type == "gross"]
    assert len(gross) == 1
    assert gross[0].payer_name is None and gross[0].plan_name is None
    assert gross[0].rate_dollar == 1000.0


def test_second_code_on_the_same_row_is_kept():
    rows = list(parse_file(WIDE))
    rc = [r for r in rows if r.code == "0510"]
    assert len(rc) == 7
    assert {r.code_type for r in rc} == {"RC"}


def test_rows_without_a_code_are_skipped():
    rows = list(parse_file(WIDE))
    assert all(r.code for r in rows)


# --- tall ------------------------------------------------------------------

def test_tall_file_does_not_duplicate_hospital_wide_rates():
    rows = list(parse_file(TALL))
    # gross/cash/min/max repeat on all three payer rows but must be emitted once
    assert len([r for r in rows if r.rate_type == "gross"]) == 1
    assert len([r for r in rows if r.rate_type == "min"]) == 1
    # 4 hospital-wide + 2 negotiated + 1 estimated (United has nothing)
    assert len(rows) == 7


def test_tall_and_wide_agree():
    """Same prices, two file layouts, same tidy answer."""
    def key(r):
        return (r.code, r.code_type, r.payer_name, r.plan_name, r.rate_type,
                r.rate_dollar, r.rate_percent)

    wide = {key(r) for r in parse_file(WIDE) if r.code == "99213"}
    tall = {key(r) for r in parse_file(TALL)}
    assert wide == tall


# --- zip -------------------------------------------------------------------

ZIPPED = FIXTURES / "52-0738041_test-zipped_standardcharges.zip"


def test_csv_inside_zip_is_read_without_extracting():
    from md_hospital_prices.parse import csv_member_of
    assert csv_member_of(ZIPPED) == "52-0738041_test-zipped_standardcharges.csv"
    assert csv_member_of(TALL) is None
    rows = list(parse_file(ZIPPED))
    assert len(rows) == 7
    assert rows[0].hospital_ein == "52-0738041"          # from the zip's name
    assert rows[0].source_file == ZIPPED.name
    assert rows[0].hospital_name == "Test Tall Hospital"  # from the CSV inside


def test_ten_digit_npi_is_not_mistaken_for_an_ein():
    assert ein_from_filename(Path("1477517225_annearundelmedicalcenter_standardcharges.csv")) is None
    assert ein_from_filename(Path("520738041-1669417838_holy-cross_standardcharges.zip")) == "52-0738041"
    assert ein_from_filename(Path("52-0607949-MeritusMedicalCenter-standardcharges-06102026.csv")) == "52-0607949"


# --- lessons from the first two real files ------------------------------------

def test_estimated_amount_is_its_own_rate_type():
    """Frederick Health reports its all-payer rate only in estimated_amount."""
    rows = list(parse_file(TALL))
    est = [r for r in rows if r.rate_type == "estimated"]
    assert len(est) == 1
    assert est[0].payer_name == "CareFirst" and est[0].rate_dollar == 425.0
    assert est[0].rate_percent is None


def test_tall_dedupe_is_per_item_not_per_code(tmp_path: Path):
    """Two chargemaster items on the same CPT with different gross charges
    must both survive. Shady Grove has 15 items on 99213."""
    src = (
        "hospital_name,last_updated_on\nT,2026-01-01\n"
        "description,code|1,code|1|type,modifiers,setting,payer_name,plan_name,"
        "standard_charge|gross,standard_charge|negotiated_dollar\n"
        "Item A,99213,CPT,,Both,P,Q,100.00,80.00\n"
        "Item A,99213,CPT,,Both,P2,Q2,100.00,70.00\n"   # same item, second payer
        "Item B,99213,CPT,25,Both,P,Q,150.00,120.00\n"  # different item, same CPT
    )
    f = tmp_path / "12-3456789_t_standardcharges.csv"
    f.write_text(src)
    rows = list(parse_file(f))
    gross = [r for r in rows if r.rate_type == "gross"]
    assert sorted(r.rate_dollar for r in gross) == [100.0, 150.0]
    assert len([r for r in rows if r.rate_type == "negotiated"]) == 3
