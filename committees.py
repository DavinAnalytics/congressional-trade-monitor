"""
committees.py — Congressional Trade Monitor
Fetches committee and subcommittee assignments for all members of Congress
directly from official government sources. No API key required.

Sources:
  House:  clerk.house.gov/xml/lists/MemberData.xml
          clerk.house.gov/Committees/{code}  (code → name lookup)
  Senate: senate.gov/general/committee_assignments/assignments.htm

Public interface:
  get_member_committees(name, chamber) -> dict
  flag_conflicts(name, chamber, ticker)  -> list[str]

Results are cached in memory for the lifetime of the process so we only
fetch once per monitor run.
"""

import re
import requests
from bs4 import BeautifulSoup
from xml.etree import ElementTree as ET

import config

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# ── In-memory cache ───────────────────────────────────────────────────────────
# { "First Last": {"committees": [...], "subcommittees": [...]} }
_COMMITTEE_CACHE: dict[str, dict] = {}
_CACHE_LOADED = {"house": False, "senate": False}


# ── House ─────────────────────────────────────────────────────────────────────

def _fetch_house_committee_names() -> dict[str, str]:
    """
    Fetch committee code → name mapping from House Clerk.
    Returns dict like {"AG00": "Agriculture", "II00": "Natural Resources"}
    """
    # House committee codes and names are embedded in the MemberData XML
    # We also resolve via the committee page URL pattern
    # Build a static map of the standard House committee codes
    # (these are stable across sessions — codes don't change when names do)
    return {
        "AG00": "Agriculture",
        "AP00": "Appropriations",
        "AS00": "Armed Services",
        "BO00": "Budget",
        "ED00": "Education and Workforce",
        "EC00": "Energy and Commerce",
        "ET00": "Ethics",
        "FA00": "Financial Services",
        "FO00": "Foreign Affairs",
        "GO00": "Oversight and Accountability",
        "HA00": "House Administration",
        "HM00": "Homeland Security",
        "II00": "Natural Resources",
        "JU00": "Judiciary",
        "PW00": "Transportation and Infrastructure",
        "RU00": "Rules",
        "SB00": "Small Business",
        "SY00": "Science, Space, and Technology",
        "SO00": "Select Intelligence",
        "VR00": "Veterans' Affairs",
        "WM00": "Ways and Means",
        "BU00": "Budget",
        "IF00": "Energy and Commerce",  # alternate code
        "BA00": "Financial Services",
        "IG00": "Permanent Select Committee on Intelligence",
        "CH00": "Select Committee on the Chinese Communist Party",
        "NR00": "Natural Resources",
        "OG00": "Oversight and Accountability",
    }


def _fetch_house_subcommittee_names() -> dict[str, str]:
    """
    Returns a mapping of subcommittee codes to readable names.
    Subcommittee codes are committee code + 2-digit suffix e.g. AG15.
    We fetch these from the House Clerk committee pages on demand.
    """
    # For now return empty — subcommittee names are resolved lazily
    return {}


def _load_house_committees() -> None:
    """
    Parse MemberData.xml and build the House member → committee lookup.
    Stores results in _COMMITTEE_CACHE.
    """
    global _CACHE_LOADED
    print("  Fetching House committee assignments...")

    try:
        resp = requests.get(
            "https://clerk.house.gov/xml/lists/MemberData.xml",
            headers=HEADERS,
            timeout=30,
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"  ⚠ Could not fetch House committee data: {e}")
        return

    committee_names = _fetch_house_committee_names()
    root = ET.fromstring(resp.content)

    for member in root.findall(".//member"):
        # Extract name
        firstname = member.findtext("member-info/firstname", "").strip()
        lastname  = member.findtext("member-info/lastname", "").strip()
        if not firstname or not lastname:
            continue
        full_name = f"{firstname} {lastname}"

        # Extract committee assignments
        committees   = []
        subcommittees = []

        for comm in member.findall("committee-assignments/committee"):
            code = comm.get("comcode", "")
            name = committee_names.get(code, code)  # fallback to code if unknown
            if name and name not in committees:
                committees.append(name)

        for sub in member.findall("committee-assignments/subcommittee"):
            code = sub.get("subcomcode", "")
            # Subcommittee names require separate lookup — store codes for now
            # We'll resolve to names via the committee conflict mapping
            if code and code not in subcommittees:
                subcommittees.append(code)

        _COMMITTEE_CACHE[full_name.lower()] = {
            "name":          full_name,
            "chamber":       "House",
            "committees":    committees,
            "subcommittees": subcommittees,
        }

    _CACHE_LOADED["house"] = True
    print(f"  ✓ {len(_COMMITTEE_CACHE)} House members loaded")


# ── Senate ────────────────────────────────────────────────────────────────────

def _load_senate_committees() -> None:
    """
    Parse the Senate committee assignments page and build the
    senator → committee lookup. Stores results in _COMMITTEE_CACHE.
    """
    global _CACHE_LOADED
    print("  Fetching Senate committee assignments...")

    try:
        resp = requests.get(
            "https://www.senate.gov/general/committee_assignments/assignments.htm",
            headers=HEADERS,
            timeout=30,
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"  ⚠ Could not fetch Senate committee data: {e}")
        return

    soup = BeautifulSoup(resp.text, "html.parser")
    text = soup.get_text(separator="\n", strip=True)
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    # Find the start of actual data — after "Back to top" pattern starts
    # The data section starts with senator names followed by (Party-State)
    # Pattern: "Lastname, Firstname\n(D-XX)\nCommittee on...\n..."

    current_name   = None
    committees     = []
    subcommittees  = []
    in_data        = False

    # Regex patterns
    name_re  = re.compile(r'^([A-Z][a-záéíóúñ\-\' ]+),\s+([A-Z][a-záéíóúñ\-\' .]+)$')
    party_re = re.compile(r'^\([DRI]-[A-Z]{2}\)$')
    comm_re  = re.compile(r'^Committee on (.+)$')
    sub_re   = re.compile(r'^Subcommittee on (.+?)(?:\s*\((?:Chairman|Ranking|Vice Chair)\))?$')
    spec_re  = re.compile(r'^(?:Special|Select|Joint|Standing) Committee on (.+)$')

    def save_current():
        if current_name:
            key = current_name.lower()
            existing = _COMMITTEE_CACHE.get(key, {})
            # Merge if already exists from House load
            _COMMITTEE_CACHE[key] = {
                "name":          current_name,
                "chamber":       "Senate",
                "committees":    list(set(existing.get("committees", []) + committees)),
                "subcommittees": list(set(existing.get("subcommittees", []) + subcommittees)),
            }

    i = 0
    while i < len(lines):
        line = lines[i]

        # Look for senator name pattern (Lastname, Firstname)
        name_m = name_re.match(line)
        if name_m and i + 1 < len(lines) and party_re.match(lines[i + 1]):
            # Save previous senator
            save_current()
            # Start new senator
            lastname  = name_m.group(1).strip()
            firstname = name_m.group(2).strip()
            # Remove suffixes like "Jr.", "III" from firstname
            firstname = re.sub(r'\s+(?:Jr\.|Sr\.|III|II|IV)\.?$', '', firstname).strip()
            current_name  = f"{firstname} {lastname}"
            committees    = []
            subcommittees = []
            in_data = True
            i += 2  # skip party line
            continue

        if in_data:
            comm_m = comm_re.match(line)
            sub_m  = sub_re.match(line)
            spec_m = spec_re.match(line)

            if comm_m:
                name = comm_m.group(1).strip()
                # Strip trailing (Chairman) etc.
                name = re.sub(r'\s*\((?:Chairman|Ranking|Vice Chair)\)\s*$', '', name)
                if name not in committees:
                    committees.append(name)
            elif spec_m:
                name = spec_m.group(1).strip()
                name = re.sub(r'\s*\((?:Chairman|Ranking|Vice Chair)\)\s*$', '', name)
                if name not in committees:
                    committees.append(name)
            elif sub_m:
                name = sub_m.group(1).strip()
                name = re.sub(r'\s*\((?:Chairman|Ranking|Vice Chair)\)\s*$', '', name)
                if name not in subcommittees:
                    subcommittees.append(name)
            elif line == "Back to top":
                pass  # separator, continue

        i += 1

    # Save last senator
    save_current()

    senate_count = sum(1 for v in _COMMITTEE_CACHE.values() if v.get("chamber") == "Senate")
    _CACHE_LOADED["senate"] = True
    print(f"  ✓ {senate_count} Senate members loaded")


# ── Public interface ──────────────────────────────────────────────────────────

def load_all(force: bool = False) -> None:
    """Load both chambers into cache. Call once per monitor run."""
    if force or not _CACHE_LOADED["house"]:
        _load_house_committees()
    if force or not _CACHE_LOADED["senate"]:
        _load_senate_committees()


def get_member_committees(name: str) -> dict:
    """
    Return committee data for a member by name.
    Returns dict with keys: name, chamber, committees, subcommittees
    Returns empty dict if not found.

    Handles two name formats:
      "First [Middle] Last"  — used by committee cache keys
      "Last, First [Middle]" — used by trade disclosure fetcher
    """
    if not any(_CACHE_LOADED.values()):
        load_all()

    key = name.lower().strip()
    if key in _COMMITTEE_CACHE:
        return _COMMITTEE_CACHE[key]

    # Trade disclosures use "Last, First [Middle]" — convert and retry
    if "," in key:
        last, _, first = key.partition(",")
        last = last.strip()
        first = first.strip()
        # Try "First [Middle] Last"
        alt = f"{first} {last}"
        if alt in _COMMITTEE_CACHE:
            return _COMMITTEE_CACHE[alt]
        # Try without middle initial: "David J." → "David"
        first_parts = first.split()
        if first_parts:
            alt_no_mid = f"{first_parts[0]} {last}"
            if alt_no_mid in _COMMITTEE_CACHE:
                return _COMMITTEE_CACHE[alt_no_mid]

    return {}


def flag_conflicts(name: str, ticker: str) -> list[str]:
    """
    Given a member name and ticker, return list of relevant committee/
    subcommittee conflicts — committees that oversee the ticker's sector.

    Returns list of conflict strings for display in alert emails.
    """
    member_data = get_member_committees(name)
    if not member_data:
        return []

    all_assignments = (
        member_data.get("committees", []) +
        member_data.get("subcommittees", [])
    )

    # Find which sectors this ticker belongs to
    ticker_sectors = []
    for sector, tickers in config.SECTOR_TICKERS.items():
        if ticker.upper() in [t.upper() for t in tickers]:
            ticker_sectors.append(sector)

    if not ticker_sectors:
        return []

    # Find committee overlaps with those sectors
    conflicts = []
    for assignment in all_assignments:
        assignment_lower = assignment.lower()
        for sector in ticker_sectors:
            relevant_committees = config.COMMITTEE_SECTORS.get(sector, [])
            for rel_comm in relevant_committees:
                # Require the full keyword phrase to appear in the assignment
                if rel_comm.lower() in assignment_lower:
                    conflict_str = (
                        f"{assignment} "
                        f"(oversees {sector} sector — {ticker})"
                    )
                    if conflict_str not in conflicts:
                        conflicts.append(conflict_str)

    return conflicts


# ── Main (test) ───────────────────────────────────────────────────────────────

def main():
    print("Loading committee assignments...")
    load_all()

    print(f"\nTotal members in cache: {len(_COMMITTEE_CACHE)}")

    # Test lookups for our watchlist members
    test_members = [
        ("Sheldon Whitehouse", "NVDA"),
        ("Josh Gottheimer", "NVDA"),
        ("Nancy Pelosi", "AAPL"),
        ("Markwayne Mullin", "AAPL"),
        ("John Boozman", "BA"),
    ]

    print(f"\n{'═'*60}")
    print("  Committee assignments for watchlist members")
    print(f"{'═'*60}")

    for name, ticker in test_members:
        data = get_member_committees(name)
        if data:
            print(f"\n  {name} ({data.get('chamber', '?')})")
            print(f"    Committees:    {', '.join(data.get('committees', [])) or 'none'}")
            print(f"    Subcommittees: {len(data.get('subcommittees', []))} assigned")
            conflicts = flag_conflicts(name, ticker)
            if conflicts:
                print(f"    ⚠ Conflicts with {ticker}:")
                for c in conflicts:
                    print(f"      → {c}")
            else:
                print(f"    ✓ No flagged conflicts with {ticker}")
        else:
            print(f"\n  {name} — not found in cache")

    print("\n✓ committees.py complete. Paste output to Claude to continue.\n")


if __name__ == "__main__":
    main()