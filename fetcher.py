"""
fetcher.py — Congressional Trade Monitor
Fetches and normalizes stock trade disclosures from official government sources:

  Senate: efdsearch.senate.gov
    - POST to /search/report/data/ for PTR filing index (JSON)
    - GET each /search/view/ptr/{uuid}/ viewer page (HTML table — no PDF needed)

  House: disclosures-clerk.house.gov
    - POST to ViewMemberSearchResult for PTR filing index (HTML table)
    - GET + pdfplumber parse each PTR PDF

Public interface: fetch_all(days) -> list[dict]
All downstream modules use only this function.
"""

import re
import io
import requests
import pdfplumber
from bs4 import BeautifulSoup
from datetime import datetime, timedelta

# ── Config ────────────────────────────────────────────────────────────────────

RECENT_DAYS = 30

SENATE_HOME_URL   = "https://efdsearch.senate.gov/search/home/"
SENATE_SEARCH_URL = "https://efdsearch.senate.gov/search/"
SENATE_DATA_URL   = "https://efdsearch.senate.gov/search/report/data/"
SENATE_VIEW_BASE  = "https://efdsearch.senate.gov"

HOUSE_SEARCH_URL  = "https://disclosures-clerk.house.gov/FinancialDisclosure/ViewMemberSearchResult"
HOUSE_PDF_BASE    = "https://disclosures-clerk.house.gov/"

SENATE_FILING_LIMIT = 50   # max viewer pages to fetch per run
HOUSE_PDF_LIMIT     = 200  # max PDFs to parse per run

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


# ── Senate ────────────────────────────────────────────────────────────────────

def _get_senate_session() -> tuple[requests.Session, str]:
    """
    Open a session, agree to eFD terms, return (session, csrf_token).
    Must be called before any Senate data requests.
    """
    session = requests.Session()

    # Step 1: GET home to receive CSRF cookie
    session.get(SENATE_HOME_URL, headers=HEADERS, timeout=30)
    csrf = session.cookies.get("csrftoken", "")

    # Step 2: POST terms agreement — required before viewing any filing
    session.post(
        SENATE_HOME_URL,
        data={
            "prohibition_agreement": "1",
            "csrfmiddlewaretoken":   csrf,
        },
        headers={**HEADERS, "Referer": SENATE_HOME_URL},
        timeout=30,
    )
    # Refresh CSRF after agreement POST
    csrf = session.cookies.get("csrftoken", csrf)
    return session, csrf


def _get_senate_filings(session: requests.Session, csrf: str) -> list[dict]:
    """
    POST to Senate eFD data endpoint.
    Returns list of {name, date, view_url} for each PTR filing.
    """
    resp = session.post(
        SENATE_DATA_URL,
        data={
            "start":                "0",
            "length":               "100",
            "report_types":         "[11]",   # 11 = Periodic Transaction Report
            "submitted_start_date": "01/01/2024 00:00:00",
            "submitted_end_date":   "",
            "candidate_state":      "",
            "senator_state":        "",
            "office_id":            "",
            "first_name":           "",
            "last_name":            "",
            "csrfmiddlewaretoken":  csrf,
        },
        headers={
            **HEADERS,
            "Referer":          SENATE_SEARCH_URL,
            "X-Requested-With": "XMLHttpRequest",
        },
        timeout=30,
    )
    resp.raise_for_status()

    filings = []
    for row in resp.json().get("data", []):
        # row = [first, last, full_office, html_link_anchor, date_str]
        first    = row[0].strip()
        last     = row[1].strip()
        name     = f"{first} {last}".strip()
        date_str = row[4].strip()
        href_m   = re.search(r'href="(/search/view/ptr/[^"]+)"', row[3])
        if not href_m:
            continue  # paper filing — no electronic viewer
        filings.append({
            "name":     name,
            "date":     date_str,
            "view_url": SENATE_VIEW_BASE + href_m.group(1),
        })
    return filings


def _parse_senate_viewer(
    session: requests.Session,
    view_url: str,
    senator_name: str,
) -> list[dict]:
    """
    GET a Senate PTR viewer page and parse the transaction HTML table.
    Returns normalized trade dicts. No PDF download needed.
    """
    try:
        resp = session.get(
            view_url,
            headers={**HEADERS, "Referer": SENATE_SEARCH_URL},
            timeout=30,
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"    ⚠ Could not fetch {view_url}: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")

    # The transaction table has a header row containing "Ticker"
    table = None
    for t in soup.find_all("table"):
        headers = [th.get_text(strip=True).lower() for th in t.find_all("th")]
        if "ticker" in headers:
            table = t
            break

    if not table:
        return []

    # Map header names to column indices
    headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
    col = {h: i for i, h in enumerate(headers)}

    trades = []
    for row in table.find_all("tr")[1:]:  # skip header row
        cells = [td.get_text(strip=True) for td in row.find_all("td")]
        if len(cells) < 4:
            continue

        ticker     = cells[col.get("ticker", 3)].strip().upper()
        asset_type = cells[col.get("asset type", 5)].strip().lower() if "asset type" in col else ""
        tx_type    = cells[col.get("type", 6)].strip() if "type" in col else ""
        amount     = cells[col.get("amount", 7)].strip() if "amount" in col else ""
        owner      = cells[col.get("owner", 2)].strip() if "owner" in col else ""
        date_raw   = cells[col.get("transaction date", 1)].strip() if "transaction date" in col else ""
        asset_name = cells[col.get("asset name", 4)].strip() if "asset name" in col else ""

        # Stock and option trades only (skip bonds, real estate, etc.)
        is_option = "option" in asset_type
        if asset_type and asset_type not in ("stock", "st", "") and not is_option:
            continue
        if not ticker or ticker in ("--", "N/A", ""):
            continue

        tx_date = _parse_date(date_raw)
        if tx_date is None:
            continue

        trades.append({
            "chamber":           "Senate",
            "representative":    senator_name,
            "ticker":            ticker,
            "asset_description": asset_name,
            "type":              _normalize_type(tx_type),
            "transaction_date":  tx_date.strftime("%Y-%m-%d"),
            "disclosure_date":   "",
            "amount":            amount,
            "ptr_link":          view_url,
            "owner":             owner,
            "asset_type":        "option" if is_option else "stock",
        })

    return trades


def fetch_senate(days: int = RECENT_DAYS) -> list[dict]:
    """Fetch Senate PTR trades. Returns normalized trade dicts."""
    print("Fetching Senate data from efdsearch.senate.gov...")
    session, csrf = _get_senate_session()
    print("  ✓ Session established")

    filings = _get_senate_filings(session, csrf)
    print(f"  ✓ {len(filings)} Senate PTR filings found")

    cutoff = datetime.now() - timedelta(days=days)
    recent = [f for f in filings if _parse_date(f["date"]) and _parse_date(f["date"]) >= cutoff]
    print(f"  ✓ {len(recent)} filings in last {days} days — parsing HTML...")

    to_parse = recent[:SENATE_FILING_LIMIT]
    trades = []
    for i, filing in enumerate(to_parse):
        name = _clean_name(filing["name"])
        filing_trades = _parse_senate_viewer(session, filing["view_url"], name)
        for t in filing_trades:
            tx_date = _parse_date(t["transaction_date"])
            if tx_date and tx_date >= cutoff:
                trades.append(t)
        if (i + 1) % 10 == 0:
            print(f"    ... {i+1}/{len(to_parse)}")

    print(f"  ✓ {len(trades)} Senate trades in last {days} days")
    return trades


# ── House ─────────────────────────────────────────────────────────────────────

def _get_house_filings(year: int) -> list[dict]:
    """POST to House Clerk and parse the HTML table of PTR filings."""
    resp = requests.post(
        HOUSE_SEARCH_URL,
        data={
            "LastName":   "",
            "FirstName":  "",
            "FilingYear": str(year),
            "State":      "",
            "District":   "",
            "checkbox":   "PTR",
            "action":     "ViewResults",
        },
        headers=HEADERS,
        timeout=30,
    )
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    filings = []
    for row in soup.select("tr[role='row']"):
        name_cell   = row.select_one("td.memberName a")
        office_cell = row.select_one("td[data-label='Office']")
        if not name_cell:
            continue
        filings.append({
            "name":    _clean_name(name_cell.get_text(strip=True)),
            "office":  office_cell.get_text(strip=True) if office_cell else "",
            "pdf_url": HOUSE_PDF_BASE + name_cell["href"],
        })
    return filings


def _parse_house_pdf(pdf_url: str, member_name: str) -> list[dict]:
    """Download and parse a House PTR PDF with pdfplumber."""
    try:
        resp = requests.get(pdf_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        print(f"    ⚠ Could not fetch {pdf_url}: {e}")
        return []

    try:
        with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
            full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    except Exception as e:
        print(f"    ⚠ Could not parse PDF {pdf_url}: {e}")
        return []

    lines = full_text.splitlines()

    # Asset type tag "[ST]" (stock) or "[OP]" (option), with the ticker "(XXXX)"
    # just before it. The asset cell usually wraps, so the tag, ticker, and the
    # type/date/amount columns land on two adjacent lines in varying combinations.
    tag_re    = re.compile(r'\[(ST|OP)\]')
    ticker_re = re.compile(r'\(([A-Z]{1,5})\)')
    # Transaction type (+ optional "(partial)") immediately followed by the
    # transaction and notification dates — the adjacency marks the metadata cell
    # (longer codes ordered first so "SP" wins over "S").
    meta_re = re.compile(
        r'\b(SP|SB|S|P)(\s*\(partial\))?\s+(\d{2}/\d{2}/\d{4})\s+(\d{2}/\d{2}/\d{4})'
    )
    full_amount_re = re.compile(r'\$[\d,]+\s*-\s*\$[\d,]+')
    low_amount_re  = re.compile(r'(\$[\d,]+)\s*-\s*$')
    any_amount_re  = re.compile(r'\$[\d,]+\+?')

    trades = []
    seen = set()

    for i, line in enumerate(lines):
        tag = tag_re.search(line)
        if not tag:
            continue
        code = tag.group(1)

        # The metadata cell (type + dates) is on the tag line, or — when the asset
        # name wraps — a few lines above it. Walk up through asset-name
        # continuation lines, stopping at the previous row's tag to stay in-row.
        meta = meta_re.search(line)
        start = i
        if not meta:
            for j in range(i - 1, max(-1, i - 4), -1):
                if tag_re.search(lines[j]):
                    break
                meta = meta_re.search(lines[j])
                if meta:
                    start = j
                    break
            if not meta:
                continue

        meta_line = lines[start]
        wrapped = start != i
        block = " ".join(lines[start:i + 1])

        tickers = ticker_re.findall(block)
        if not tickers:
            continue
        ticker = tickers[-1].upper()

        tx_date = _parse_date(meta.group(3))
        if tx_date is None:
            continue

        key = (meta_line.strip(), line.strip())
        if key in seen:
            continue
        seen.add(key)

        # Amount is a full "$low - $high" range, or a low bound on the meta line
        # whose high bound wrapped onto the tag line.
        fm = full_amount_re.search(meta_line)
        if fm:
            amount = fm.group(0)
        elif wrapped and (lm := low_amount_re.search(meta_line)) and (hm := any_amount_re.search(line)):
            amount = f"{lm.group(1)} - {hm.group(0)}"
        else:
            am = any_amount_re.search(meta_line)
            amount = am.group(0) if am else ""

        tx_type = "sp" if meta.group(2) else meta.group(1)

        trades.append({
            "chamber":           "House",
            "representative":    member_name,
            "ticker":            ticker,
            "asset_description": " ".join(block.split())[:100],
            "type":              _normalize_type(tx_type),
            "transaction_date":  tx_date.strftime("%Y-%m-%d"),
            "disclosure_date":   meta.group(4),
            "amount":            amount,
            "ptr_link":          pdf_url,
            "owner":             "",
            "asset_type":        "option" if code == "OP" else "stock",
        })

    return trades


def fetch_house(days: int = RECENT_DAYS) -> list[dict]:
    """Fetch House PTR trades. Returns normalized trade dicts."""
    current_year = datetime.now().year
    years = [current_year]
    if datetime.now().month == 1:
        years.append(current_year - 1)

    all_filings = []
    for year in years:
        print(f"Fetching House PTR index for {year}...")
        filings = _get_house_filings(year)
        print(f"  ✓ {len(filings)} PTR filings found")
        all_filings.extend(filings)

    seen, unique = set(), []
    for f in all_filings:
        if f["pdf_url"] not in seen:
            seen.add(f["pdf_url"])
            unique.append(f)

    unique.sort(key=lambda f: _extract_filing_id(f["pdf_url"]), reverse=True)
    to_parse = unique[:HOUSE_PDF_LIMIT]

    print(f"  Parsing {len(to_parse)} most recent PDFs...")
    cutoff = datetime.now() - timedelta(days=days)

    trades = []
    for i, filing in enumerate(to_parse):
        pdf_trades = _parse_house_pdf(filing["pdf_url"], filing["name"])
        for t in pdf_trades:
            tx_date = _parse_date(t["transaction_date"])
            if tx_date and tx_date >= cutoff:
                trades.append(t)
        if (i + 1) % 20 == 0:
            print(f"    ... {i+1}/{len(to_parse)}")

    print(f"  ✓ {len(trades)} House trades in last {days} days")
    return trades


# ── Unified entry point ───────────────────────────────────────────────────────

def fetch_all(days: int = RECENT_DAYS) -> list[dict]:
    """
    Fetch both chambers. Returns unified sorted list of trade dicts.
    This is the only function analyzer.py needs to call.
    """
    senate_trades = fetch_senate(days)
    house_trades  = fetch_house(days)
    all_trades    = senate_trades + house_trades
    all_trades.sort(key=lambda t: t["transaction_date"], reverse=True)
    print(f"\n✓ Total: {len(all_trades)} trades across both chambers "
          f"(last {days} days)")
    return all_trades


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_date(raw: str) -> datetime | None:
    if not raw:
        return None
    raw = raw.strip()[:10]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _normalize_type(raw: str) -> str:
    r = raw.strip().lower()
    if r in ("purchase", "p", "buy"):
        return "purchase"
    if r in ("sale", "s", "sell", "sale_full", "sale (full)"):
        return "sale"
    if r in ("sp", "sb", "sale_partial", "partial", "sale (partial)"):
        return "sale_partial"
    return r


def _clean_name(raw: str) -> str:
    name = re.sub(r"Hon\.\.?\s*", "", raw)
    return " ".join(name.split())


def _extract_filing_id(pdf_url: str) -> int:
    m = re.search(r'/(\d+)\.pdf$', pdf_url)
    return int(m.group(1)) if m else 0


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    trades = fetch_all(days=RECENT_DAYS)

    print(f"\n{'═'*60}")
    print(f"  Sample trades (first 5)")
    print(f"{'═'*60}")
    for t in trades[:5]:
        print(f"\n  [{t['chamber']}] {t['representative']}")
        print(f"    Ticker:  {t['ticker']}")
        print(f"    Type:    {t['type']}")
        print(f"    Date:    {t['transaction_date']}")
        print(f"    Amount:  {t['amount']}")
        print(f"    Owner:   {t['owner']}")
        print(f"    Link:    {t['ptr_link']}")

    print(f"\n✓ fetcher.py complete. Paste output to Claude to continue.\n")


if __name__ == "__main__":
    main()