"""
Real company data from SEC EDGAR (10-K filings), written in the same spreadsheet format as
1-generate-data.py so 2-generate-questions.py can use it unchanged.

Source: https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json (free, no API key).
SEC requires every client to identify itself, so set a contact first:
    export SEC_USER_AGENT="your-project your-email@example.org"

How a cell is filled (see find_fact):
  * only facts from 10-K / 10-K/A filings are used
  * "year" = calendar year in which the fiscal year ends (Apple's FY ending Sep 2023 -> 2023);
    a year ending in the first week of January counts as the year before (J&J's 2021-01-03 -> 2020)
  * flows (revenue, capex, ...) must cover one fiscal year; balance-sheet items are read at fiscal year end
  * each item has a list of XBRL tags tried in order; the first one with a value wins
  * if the same period appears in several filings (restatements), the latest filing wins
  * nothing found -> the cell stays blank; 2-generate-questions.py skips blank cells

Outputs in output/real/:
  real_company_data.csv / .json   the spreadsheet (values in millions, like the synthetic one)
  real_company_sources.csv        only with --sources: tag, value, filing and date behind every number

Debugging tip: open https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json in a browser and
search for a tag name to see every value the company reported for it.
"""

import argparse
import csv
import json
import os
import urllib.request
from datetime import date, timedelta
from pathlib import Path

from core.paths import (
    REAL_COMPANIES_FILE,
    REAL_SOURCES_CSV,
    REAL_SPREADSHEET_CSV,
    REAL_SPREADSHEET_JSON,
)

# Raw items read from the filings: name -> (period type, XBRL unit, us-gaap tags to try in order).
# "duration" = covers the fiscal year (income / cash-flow statement), "instant" = balance at year end.
RAW_ITEMS = {
    "revenue": ("duration", "USD", [
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet",
    ]),
    "cost_of_goods_sold": ("duration", "USD", [
        "CostOfRevenue",  # first: Caterpillar uses CostOfGoodsAndServicesSold for a small side item
        "CostOfGoodsAndServicesSold",
        "CostOfGoodsSold",
    ]),
    "operating_income": ("duration", "USD", ["OperatingIncomeLoss"]),
    "pretax_income": ("duration", "USD", [
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
    ]),
    "income_tax_expense": ("duration", "USD", ["IncomeTaxExpenseBenefit"]),
    "capex": ("duration", "USD", ["PaymentsToAcquirePropertyPlantAndEquipment"]),
    "dividends_paid": ("duration", "USD", [
        "PaymentsOfDividendsCommonStock",
        "PaymentsOfDividends",
        "PaymentsOfOrdinaryDividends",
    ]),
    "total_assets": ("instant", "USD", ["Assets"]),
    "total_liabilities": ("instant", "USD", ["Liabilities"]),
    "total_equity": ("instant", "USD", [
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        "StockholdersEquity",
    ]),
    "cash": ("instant", "USD", [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ]),
    "accounts_receivable": ("instant", "USD", ["AccountsReceivableNetCurrent", "ReceivablesNetCurrent"]),
    "inventories": ("instant", "USD", ["InventoryNet"]),
    "short_term_investments": ("instant", "USD", [
        "ShortTermInvestments",
        "MarketableSecuritiesCurrent",
        "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
        "OtherShortTermInvestments",
    ]),
    "current_liabilities": ("instant", "USD", ["LiabilitiesCurrent"]),
    "shares_outstanding": ("instant", "shares", ["CommonStockSharesOutstanding"]),
}

ID_COLUMNS = ["company_name", "ticker", "sector", "country", "exchange", "year"]


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def download_company_facts(cik: int, user_agent: str) -> dict:
    """Download the EDGAR companyfacts JSON for one company (all its reported XBRL facts)."""
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
    print(f"  downloading {url}")
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


# ---------------------------------------------------------------------------
# Picking values
# ---------------------------------------------------------------------------

def is_one_year(fact: dict) -> bool:
    days = (date.fromisoformat(fact["end"]) - date.fromisoformat(fact["start"])).days
    return 350 <= days <= 380


def annual_report_facts(us_gaap: dict, tag: str, unit: str, period_type: str) -> list[dict]:
    """All 10-K facts for one tag, keeping only full-year durations or only instants."""
    facts = us_gaap.get(tag, {}).get("units", {}).get(unit, [])
    facts = [f for f in facts if f.get("form") in ("10-K", "10-K/A")]
    if period_type == "duration":
        return [f for f in facts if "start" in f and is_one_year(f)]
    return [f for f in facts if "start" not in f]


def fiscal_year_label(end: str) -> int:
    """Calendar year the fiscal year ends in; 52/53-week years ending in early January belong to the year before."""
    return (date.fromisoformat(end) - timedelta(days=7)).year


def fiscal_year_ends(us_gaap: dict) -> dict[int, str]:
    """year -> fiscal year end date, taken from the end dates of full-year facts."""
    ends: dict[int, str] = {}
    for period_type, unit, tags in RAW_ITEMS.values():
        if period_type != "duration":
            continue
        for tag in tags:
            for fact in annual_report_facts(us_gaap, tag, unit, "duration"):
                year = fiscal_year_label(fact["end"])
                ends[year] = max(ends.get(year, ""), fact["end"])
    return ends


def find_fact(us_gaap: dict, item: str, fy_end: str) -> dict | None:
    """The fact for one raw item at one fiscal year end, or None if the company did not report it."""
    period_type, unit, tags = RAW_ITEMS[item]
    for tag in tags:
        matches = [f for f in annual_report_facts(us_gaap, tag, unit, period_type) if f["end"] == fy_end]
        if matches:
            latest = max(matches, key=lambda f: f["filed"])
            return {"item": item, "tag": tag, "value": latest["val"], "end": fy_end,
                    "accn": latest["accn"], "filed": latest["filed"]}
    return None


# ---------------------------------------------------------------------------
# Building spreadsheet rows
# ---------------------------------------------------------------------------

def minus(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None else a - b


def millions(x: float | None) -> float | None:
    return None if x is None else round(x / 1_000_000, 1)


def build_row(company: dict, year: int, raw: dict[str, float | None]) -> dict:
    """Turn raw filing values (in USD / shares) into one spreadsheet row (in millions).

    Three columns are computed so the pipeline's derived concepts match the filing:
      operating_expenses     = revenue - cost_of_goods_sold - operating_income
      non_operating_expenses = operating_income - pretax_income
      income_tax (a rate)    = income_tax_expense / pretax_income
    """
    liabilities = raw["total_liabilities"]
    if liabilities is None:  # many companies do not tag total liabilities
        liabilities = minus(raw["total_assets"], raw["total_equity"])

    pretax = raw["pretax_income"]
    tax_rate = None
    if pretax and raw["income_tax_expense"] is not None:
        tax_rate = round(raw["income_tax_expense"] / pretax, 4)

    return {
        "company_name": company["name"],
        "ticker": company["ticker"],
        "sector": company["sector"],
        "country": company["country"],
        "exchange": company["exchange"],
        "year": year,
        "revenue": millions(raw["revenue"]),
        "cost_of_goods_sold": millions(raw["cost_of_goods_sold"]),
        "operating_expenses": millions(minus(minus(raw["revenue"], raw["cost_of_goods_sold"]), raw["operating_income"])),
        "non_operating_expenses": millions(minus(raw["operating_income"], pretax)),
        "income_tax": tax_rate,
        "total_assets": millions(raw["total_assets"]),
        "total_liabilities": millions(liabilities),
        "total_equity": millions(raw["total_equity"]),
        "cash": millions(raw["cash"]),
        "accounts_receivable": millions(raw["accounts_receivable"]),
        "inventories": millions(raw["inventories"]),
        "short_term_investments": millions(raw["short_term_investments"]),
        "current_liabilities": millions(raw["current_liabilities"]),
        "capex": millions(raw["capex"]),
        "dividends_paid": millions(raw["dividends_paid"]),
        "shares_outstanding": millions(raw["shares_outstanding"]),
        "stock_price": None,  # not in filings
        "employees": None,    # not in XBRL data
    }


def fetch_company(company: dict, years: list[int], user_agent: str) -> tuple[list[dict], list[dict]]:
    """Rows and source facts for one company."""
    us_gaap = download_company_facts(company["cik"], user_agent)["facts"]["us-gaap"]
    fy_ends = fiscal_year_ends(us_gaap)
    rows, sources = [], []
    for year in years:
        if year not in fy_ends:
            print(f"  {year}: no 10-K data, skipped")
            continue
        facts = {item: find_fact(us_gaap, item, fy_ends[year]) for item in RAW_ITEMS}
        if facts["revenue"] is None:
            print(f"  {year}: no revenue found, skipped")
            continue
        raw = {item: (fact["value"] if fact else None) for item, fact in facts.items()}
        rows.append(build_row(company, year, raw))
        for fact in facts.values():
            if fact:
                sources.append({"company_name": company["name"], "year": year, **fact})
        missing = [item for item, fact in facts.items() if fact is None]
        print(f"  {year} (FY end {fy_ends[year]}): missing {missing or 'nothing'}")
        if raw["cost_of_goods_sold"] is not None and raw["cost_of_goods_sold"] < 0.05 * raw["revenue"]:
            print(f"  WARNING {year}: cost of goods sold is under 5% of revenue ({facts['cost_of_goods_sold']['tag']}); "
                  "probably the wrong tag")
    return rows, sources


# ---------------------------------------------------------------------------
# Writing outputs
# ---------------------------------------------------------------------------

def write_csv(path: Path, rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)  # None is written as an empty cell


def write_json(path: Path, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def print_coverage(rows: list[dict], columns: list[str]) -> None:
    print("\nFilled cells per column:")
    for col in columns:
        filled = sum(row[col] is not None for row in rows)
        print(f"  {col:<24} {filled:>3}/{len(rows)}")


def main():
    parser = argparse.ArgumentParser(description="Fetch real company financials from SEC EDGAR 10-K filings.")
    parser.add_argument("--start-year", type=int, default=2020)
    parser.add_argument("--end-year", type=int, default=2025)
    parser.add_argument("--companies", type=Path, default=REAL_COMPANIES_FILE, help="JSON list of companies with CIKs.")
    parser.add_argument("--sources", action="store_true", help=f"Also write {REAL_SOURCES_CSV.name} (where every number came from).")
    args = parser.parse_args()

    user_agent = os.environ.get("SEC_USER_AGENT")
    if not user_agent:
        raise SystemExit('Set SEC_USER_AGENT first, e.g.  export SEC_USER_AGENT="my-project me@example.org"')

    REAL_SPREADSHEET_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(args.companies, encoding="utf-8") as f:
        companies = json.load(f)["define"]
    years = list(range(args.start_year, args.end_year + 1))

    all_rows, all_sources = [], []
    for company in companies:
        print(f"{company['name']} (CIK {company['cik']})")
        rows, sources = fetch_company(company, years, user_agent)
        all_rows += rows
        all_sources += sources
    if not all_rows:
        raise SystemExit("No rows found. Check the CIKs and the year range.")

    columns = list(all_rows[0])
    write_csv(REAL_SPREADSHEET_CSV, all_rows)
    # JSON copy is the spreadsheet context the benchmark feeds to the LLM: leave missing cells out.
    write_json(REAL_SPREADSHEET_JSON, [{k: v for k, v in row.items() if v is not None} for row in all_rows])

    print_coverage(all_rows, [c for c in columns if c not in ID_COLUMNS])
    print(f"\nWrote {len(all_rows)} company-year rows to {REAL_SPREADSHEET_CSV}")
    if args.sources:
        write_csv(REAL_SOURCES_CSV, all_sources)
        print(f"Sources of every value: {REAL_SOURCES_CSV}")


if __name__ == "__main__":
    main()
