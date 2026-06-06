"""
test_senate.py — Test Senate eFD search with proper CSRF handling.
Run with: python test_senate.py
"""

import requests
from bs4 import BeautifulSoup

SESSION = requests.Session()
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

def get_csrf_token():
    """GET the search page to obtain a valid CSRF token and cookie."""
    resp = SESSION.get(
        "https://efdsearch.senate.gov/search/",
        headers=HEADERS,
        timeout=30,
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    token = soup.find("input", {"name": "csrfmiddlewaretoken"})
    if token:
        return token["value"]
    # Also check cookies
    return SESSION.cookies.get("csrftoken", "")

def fetch_senate_filings(csrf_token: str) -> dict:
    """POST to Senate eFD search for recent PTR filings."""
    resp = SESSION.post(
        "https://efdsearch.senate.gov/search/report/data/",
        data={
            "start": "0",
            "length": "10",
            "report_types": "[11]",   # 11 = Periodic Transaction Report
            "submitted_start_date": "01/01/2026 00:00:00",
            "submitted_end_date": "",
            "candidate_state": "",
            "senator_state": "",
            "office_id": "",
            "first_name": "",
            "last_name": "",
            "csrfmiddlewaretoken": csrf_token,
        },
        headers={
            **HEADERS,
            "Referer": "https://efdsearch.senate.gov/search/",
            "X-Requested-With": "XMLHttpRequest",
        },
        timeout=30,
    )
    print(f"Status: {resp.status_code}")
    print(f"Response preview:\n{resp.text[:2000]}")
    return resp

if __name__ == "__main__":
    print("Step 1: Getting CSRF token...")
    token = get_csrf_token()
    print(f"  Token: {token[:20]}..." if token else "  No token found in page")
    print(f"  Cookies: {dict(SESSION.cookies)}")

    print("\nStep 2: Fetching Senate PTR filings...")
    fetch_senate_filings(token)
