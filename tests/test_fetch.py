"""Discovery must survive the ways hospitals actually write cms-hpt.txt.

The samples below mirror layouts observed on Maryland hospital domains in
September 2026. URLs are the real published ones (they are public pointers,
not prices). Where the layout was seen only through a renderer, the shape is
what matters, not the exact bytes.
"""

from pathlib import Path

from md_hospital_prices.fetch import parse_cms_hpt, sniff_format, read_sources, write_sources, SOURCE_COLUMNS

FREDERICK = """\
location-name: Frederick Health Hospital
source-page-url: https://www.frederickhealth.org/about/billing-financial-assistance/
mrf-url: https://www.frederickhealth.org/documents/billing%20and%20finance/52-0591612_FrederickHealthHospital_standardcharges.csv
contact-name: Someone
contact-email: someone@example.org
"""

HOPKINS = """\
location-name: Johns Hopkins Bayview Medical Center
source-page-url: https://www.hopkinsmedicine.org/patient-care/patients-visitors/billing-insurance/pay-bill/charges-fees
mrf-url: https://www.hopkinsmedicine.org/-/media/patient-care/documents/billing-insurance/charge-fees/521341890_johns-hopkins-bayview-medical-center_standardcharges.csv
contact-name: Someone
contact-email: someone@example.org

location-name: The Johns Hopkins Hospital
source-page-url: https://www.hopkinsmedicine.org/patient-care/patients-visitors/billing-insurance/pay-bill/charges-fees
mrf-url: https://www.hopkinsmedicine.org/-/media/patient-care/documents/billing-insurance/charge-fees/520591656_the-johns-hopkins-hospital_standardcharges.csv
contact-name: Someone
contact-email: someone@example.org
"""

# Holy Cross: the key is misspelled and the payload is a zip
HOLY_CROSS = """\
location-name: Holy Cross Health Silver Spring
source-page-url: https://www.holycrosshealth.org/for-patients/billing-financial-assistance-and-insurance/charge-estimates
mfr-url: https://hpt.trinity-health.org/520738041-1669417838_holy-cross-hospital-silver-spring_standardcharges.zip
contact-name: Someone
contact-email: someone@example.org

location-name: Holy Cross Health Germantown
source-page-url: https://www.holycrosshealth.org/for-patients/billing-financial-assistance-and-insurance/charge-estimates
mfr-url: https://hpt.trinity-health.org/520738041-1245655638_holy-cross-germantown-hospital_standardcharges.zip
contact-name: Someone
contact-email: someone@example.org
"""

# Mercy: every line double-spaced, Title Case keys, .ashx handler
MERCY = """\
**Mercy Health Services**

Location Name: Mercy Health Services

Source Page URL: https://mdmercy.com/patients-and-visitors/billing-and-insurance/hospital-charge-and-facility-fee-information

MRF URL: https://mdmercy.com/-/media/files/about-mercy/policies-and-documents/520591658_mercymedicalcenter_standardcharges.ashx

Contact Name: Someone

Contact Email: someone@example.org
"""

# LifeBridge-style: markdown headings and bullets
LIFEBRIDGE = """\
# Lifebridge Health Hospital Locations and Price Transparency Data

**Sinai Hospital of Baltimore**
- Source: https://www.lifebridgehealth.org/patients/price-transparency
- MRF: https://www.lifebridgehealth.org/sites/default/files/2026-04/52-0486540_Sinai-Hospital-of-Baltimore_standardcharges.csv
- Contact: Customer Service (someone@example.org)

**Northwest Hospital Center**
- Source: https://www.lifebridgehealth.org/patients/price-transparency
- MRF: https://www.lifebridgehealth.org/sites/default/files/2026-04/52-1372665_Northwest-Hospital-Center_standardcharges.csv
- Contact: Customer Service (someone@example.org)
"""

# MedStar-style: numbered list, shared contact block at the top
MEDSTAR = """\
# MedStar Health Price Transparency Data

**Contact Information:**
- Contact Name: Someone
- Contact Email: someone@example.org
- Source Page URL: https://www.medstarhealth.org/price-transparency-disclosures

**Hospital Locations and Standard Charges Files:**

1. MedStar Franklin Square Medical Center
   MRF URL: https://www.medstarhealth.org/-/media/project/mho/medstar/billing-and-insurance/2026/520608007_medstar_franklin_square_medical_center_standardcharges.csv

2. MedStar Union Memorial Hospital
   MRF URL: https://www.medstarhealth.org/-/media/project/mho/medstar/billing-and-insurance/2026/520591685_medstar_union_memorial_hospital_standardcharges.csv
"""

# Adventist-style: friendly heading, then a legal `Location:` line
ADVENTIST = """\
**Shady Grove Medical Center**
- Location: Adventist HealthCare, Inc. d.b.a Adventist HealthCare Shady Grove Medical Center
- Source: https://www.adventisthealthcare.com/patients-visitors/billing-financial/price-transparency/
- MRF: https://www.adventisthealthcare.com/app/files/public/5234573e-a91f-456f-919e-dd37620e586c/52-1532556_AdventistHealthCareShadyGroveMedicalCenter_StandardCharges.csv
- Contact: Revenue Cycle, someone@example.org
"""

JSON_FORM = """\
[{"location-name": "Example Hospital", "source-page-url": "https://example.org/prices",
  "mrf-url": "https://example.org/12-3456789_example_standardcharges.csv"}]
"""


def names(locs):
    return [l.name for l in locs]


def test_single_block():
    locs = parse_cms_hpt(FREDERICK)
    assert names(locs) == ["Frederick Health Hospital"]
    assert locs[0].mrf_url.endswith("52-0591612_FrederickHealthHospital_standardcharges.csv")
    assert locs[0].source_page_url.startswith("https://www.frederickhealth.org/about")


def test_multiple_blocks_separated_by_blank_lines():
    locs = parse_cms_hpt(HOPKINS)
    assert names(locs) == ["Johns Hopkins Bayview Medical Center", "The Johns Hopkins Hospital"]


def test_mfr_url_typo_is_accepted():
    locs = parse_cms_hpt(HOLY_CROSS)
    assert len(locs) == 2
    assert all(l.mrf_url.endswith(".zip") for l in locs)


def test_double_spaced_title_case_keys():
    locs = parse_cms_hpt(MERCY)
    assert names(locs) == ["Mercy Health Services"]
    assert locs[0].mrf_url.endswith(".ashx")


def test_markdown_headings_and_bullets():
    locs = parse_cms_hpt(LIFEBRIDGE)
    assert names(locs) == ["Sinai Hospital of Baltimore", "Northwest Hospital Center"]


def test_numbered_list_with_shared_contact_block():
    locs = parse_cms_hpt(MEDSTAR)
    assert names(locs) == ["MedStar Franklin Square Medical Center", "MedStar Union Memorial Hospital"]
    assert "520608007" in locs[0].mrf_url


def test_first_name_wins_over_legal_entity_line():
    locs = parse_cms_hpt(ADVENTIST)
    assert names(locs) == ["Shady Grove Medical Center"]


def test_json_form():
    locs = parse_cms_hpt(JSON_FORM)
    assert names(locs) == ["Example Hospital"]


def test_relative_url_is_resolved_against_the_txt_location():
    locs = parse_cms_hpt("location-name: X\nmrf-url: /files/12-3456789_x_standardcharges.csv\n",
                         base="https://example.org/cms-hpt.txt")
    assert locs[0].mrf_url == "https://example.org/files/12-3456789_x_standardcharges.csv"


def test_sniff_format(tmp_path: Path):
    (tmp_path / "a").write_bytes(b"PK\x03\x04junk")
    (tmp_path / "b").write_bytes(b"\xef\xbb\xbfhospital_name,last_updated_on\nX,2026\n")
    (tmp_path / "c").write_bytes(b'  {"a": 1}')
    assert sniff_format(tmp_path / "a") == "zip"
    assert sniff_format(tmp_path / "b") == "csv"
    assert sniff_format(tmp_path / "c") == "json"


def test_sources_round_trip_keeps_hand_edits(tmp_path: Path):
    p = tmp_path / "sources.csv"
    rows = [{c: "" for c in SOURCE_COLUMNS} | {
        "hospital_name": "X", "mrf_url": "https://example.org/x.csv",
        "include": "no", "notes": "out of state"}]
    write_sources(p, rows)
    back = read_sources(p)
    assert back[0]["include"] == "no" and back[0]["notes"] == "out of state"


def test_discover_keeps_hand_captured_rows_when_a_domain_fails(tmp_path: Path, monkeypatch):
    """UMMS 403s scripts; its rows were pasted by hand and must survive re-runs."""
    from md_hospital_prices import fetch as F
    seeds = tmp_path / "seeds.csv"
    seeds.write_text("system,domain\nUMMS,www.umms.org\n")
    sources = tmp_path / "sources.csv"
    rows = [{c: "" for c in SOURCE_COLUMNS} | {
        "hospital_name": "UMMC", "system": "UMMS", "domain": "www.umms.org", "include": "yes",
        "mrf_url": "https://www.umms.org/standard-charges/x_standardcharges.csv", "notes": "by hand"}]
    write_sources(sources, rows)
    monkeypatch.setattr(F, "discover_domain", lambda domain, session=None: ([], "discovery-http-403"))
    monkeypatch.setattr(F.time, "sleep", lambda s: None)
    out = F.discover_all(seeds, sources, verbose=False)
    assert len(out) == 1 and out[0]["hospital_name"] == "UMMC" and out[0]["notes"] == "by hand"
