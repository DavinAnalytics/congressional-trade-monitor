"""
test_senate_viewer.py — Parse transaction table from Senate PTR viewer HTML.
Run with: python test_senate_viewer.py
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
BASE = "https://efdsearch.senate.gov"

# Step 1+2: session + terms agreement
SESSION.get(f"{BASE}/search/home/", headers=HEADERS, timeout=30)
csrf = SESSION.cookies.get("csrftoken", "")
SESSION.post(
    f"{BASE}/search/home/",
    data={"prohibition_agreement": "1", "csrfmiddlewaretoken": csrf},
    headers={**HEADERS, "Referer": f"{BASE}/search/home/"},
    timeout=30,
)

# Step 3: fetch viewer page
VIEW_URL = f"{BASE}/search/view/ptr/4aa0094d-d9da-4a05-aa13-6d9f5d376105/"
r = SESSION.get(VIEW_URL, headers={**HEADERS, "Referer": f"{BASE}/search/"}, timeout=30)
soup = BeautifulSoup(r.text, "html.parser")

# Print all tables
print("=== ALL TABLES ===")
for i, table in enumerate(soup.find_all("table")):
    print(f"\n--- Table {i} ---")
    print(table.get_text(separator=" | ", strip=True)[:1000])

# Print any divs that look like transaction rows
print("\n=== DIVS WITH 'transaction' or 'asset' in class ===")
for div in soup.find_all(True, class_=True):
    classes = " ".join(div.get("class", []))
    if any(k in classes.lower() for k in ["transaction", "asset", "trade", "row"]):
        print(f"  <{div.name} class='{classes}'> {div.get_text(strip=True)[:200]}")

# Print full page text so we can see the data
print("\n=== FULL PAGE TEXT ===")
print(soup.get_text(separator="\n", strip=True)[:4000])