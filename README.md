# Congressional Trade Monitor
**Author:** Davin Kim  
**Status:** ✅ Complete — all modules built, 145 unit tests passing  
**Live dashboard:** GitHub Pages (rebuilt daily)  
**Stack:** Python, Requests, BeautifulSoup, pdfplumber, yfinance, smtplib, python-dotenv, google-genai (Gemini 2.5 Flash)  
**Purpose:** Personal-use automation tool that monitors congressional stock disclosures **and corporate insider open-market buys**, detects high-signal trading patterns — including tickers accumulated by Congress and company executives at the same time — sends one ranked email digest on schedule, tracks whether its own alerts actually beat the market, and provides a visual dashboard for exploratory analysis.

---

## What It Does

Congress members are required by the STOCK Act (2012) to publicly disclose stock trades within 45 days. This tool automates monitoring of those disclosures across both chambers, detects meaningful patterns, and alerts via email when a signal fires.

**Core insight driving the design:** The top-performer leaderboard is non-sticky year to year; none of the top performers of 2024 showed up in top performers of 2025. Instead of chasing one politician (e.g. Pelosi), broad monitoring with cluster detection is the smarter decision.

**Insider cross-referencing:** Beyond Congress, the monitor also scrapes open-market purchases by corporate insiders (via OpenInsider — Section 16 officers, so CEOs and CFOs but also VPs and other officers) and raises a **Cross-Signal** alert when the same ticker is being accumulated by both Congress and a company's own executives within the 45-day window — a stronger conviction signal than either source alone.

---

## Alert Tiers

| Tier | Signal | Trigger |
|------|--------|---------|
| ⚡ Cluster Alert | 2+ members buy same ticker within 45 days | Strongest congressional signal |
| 🏆 Win-Rate Alert | Member with >60% historical win rate files a new purchase | Individual quality filter |
| 👁️ Watchlist Alert | Specific named politician buys | Manual tracking |
| 🔗 Cross-Signal Alert | Same ticker bought by **both** Congress and a corporate insider within 45 days | Combined-conviction signal |

All alerts from a run are sent as **one digest email**, ranked by a 0–100 conviction
score so the strongest signal leads the subject line. The top-ranked alerts also carry
a Gemini AI context block (see [AI-Powered Features](#ai-powered-features-gemini-25-flash)).

### Alert eligibility

`analyzer.alertable_trades()` decides what is even allowed to become a signal, and every
detector runs on its output. Two exclusions, both about intent:

| Filter | Config | Why |
|---|---|---|
| Sales | `ALERT_ON_SALES = False` | Congressional selling is dominated by tax-loss harvesting, scheduled liquidations and diversification. A member selling says far less about their view of a company than a member buying. |
| Ticker sprays | `REBALANCE_MIN_TICKERS = 8` | A member filing 8+ distinct tickers on one date is moving a portfolio, not making a call. The largest single filing observed is **294 tickers on one day**; roughly three-quarters of the trade log sits in such blocks, and they are not confined to sales. Same-day filings cluster at 1–2, 4–6, 8 and 31 tickers, so 8 keeps up to six same-day picks as plausibly deliberate. |

**Excluded is not deleted.** These trades never enter the seen-state, so `history.record_control`
samples them into the control arm and keeps scoring them on the same horizons as real alerts.
That is what makes the decision reversible on evidence rather than on taste — if the monthly
review eventually shows the excluded arm outperforming, flip the constant back.

The sales filter applies to **alert detection only**. The rebalancing filter also applies to
`compute_win_rates` — a win rate built from portfolio moves measures a diversified basket
against SPY, which converges on a coin flip by construction, and `win_rates` feeds the
conviction score's track-record component, so that noise would leak into ranking. It cut the
scoreable purchase pool from 911 to 354 and the leaderboard from 10 rows to 7. Alan Armstrong
fell from 87/319 to below the floor, and John Boozman — previously the top entry at 67%
(12/18) — dropped off once most of his scored purchases turned out to be rebalancing filings.
Members who pick individual names (Gottheimer, Delaney, Taylor, Salazar) barely moved.

Direction is deliberately **not** filtered in `compute_win_rates`: `_score_trade` already scores
purchases only, and that must hold regardless of `ALERT_ON_SALES`. The dashboard trade log and
the control arm still see every trade.

### New listings cannot form a cluster

A cluster alert assumes that members converging on one ticker independently is evidence they
believe the same thing. For a ticker that has only just started trading that inference is
invalid: a new listing is a common external event every member reacts to at once, so breadth
measures the size of the news rather than the strength of the conviction.

`SPCX` is the worked example. Four members — Moskowitz, Meuser, McGuire, Cisneros — bought
Space Exploration Technologies between 2026-06-12 and 2026-06-18. The first of those dates is
**the stock's first day of trading**. Scored as a cluster it comes out at **60.3**, which would
have ranked second of every alert in the period on the strength of having four members instead
of two. Its actual return by 2026-08-28 was **−12% to −27% against SPY's +3.7%**, an excess of
**−16.1%** measured from the earliest buy.

`detect_cluster_alerts` therefore drops any cluster whose ticker had less than
`NEW_LISTING_DAYS = 30` of price history when the window opened. Two properties matter:

- **It fails open.** `_is_new_listing` returns False when yfinance has nothing to say, so a
  price-feed outage can never silently suppress alerts. The guard fires only on positive
  evidence of a new listing.
- **Suppressed is not deleted**, on the same principle as the eligibility filters above. The
  trades appear in no alert, so `history.record_control` sweeps them into the control arm and
  keeps scoring them — if new listings turn out to outperform, the record will say so.

Run against the full 180-day log the guard removes exactly one cluster (SPCX) and leaves the
other 21 — all long-listed names — untouched.

### Conviction score

Each alert is scored from the evidence behind it, weights in `config.SCORE_WEIGHTS`:

| Component | Why it matters |
|-----------|----------------|
| Tier base | Cross-signal > cluster > win-rate > watchlist |
| Disclosed size | Log-scaled midpoint of the **congressional** disclosure ranges — separates a $1K token buy from $500K. Insider dollars are excluded so a whale insider buy cannot masquerade as congressional conviction; the insider leg earns credit via participants and seniority instead. |
| Participants | Distinct members, plus insiders on cross-signals |
| Freshness | Decays as disclosure lag approaches 45 days |
| Track record | Best historical win rate among participating members |
| Seniority | CEO/CFO outranks other officers (VPs, etc.) on cross-signals |

**Disclosure lag** is surfaced on every alert. The STOCK Act allows up to 45 days
between execution and disclosure, so a trade disclosed 4 days later and one disclosed
44 days later are very different signals — the second is often already priced in. Each
alert also shows the ticker's move since the trade date against SPY, so you can see
immediately whether the move already happened. Stale alerts are downranked and
labeled, never hidden.

Alert header color in the dashboard reflects trade direction: **green** for net buy
activity, **red** for net sell activity — independent of tier.

---

## Quickstart

```bash
# Create the project's virtual environment (once)
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt

# Set up credentials
cp .env.example .env
# Edit .env with your Gmail sender, app password, and recipient

# Preview or rebuild the dashboard (no credentials required)
./.venv/bin/python export.py

# Preview filtered OpenInsider insider buys
./.venv/bin/python openinsider_fetcher.py

# Test one full cycle (fetch → analyze → ranked digest email)
./.venv/bin/python monitor.py --once

# Send the weekly digest email
./.venv/bin/python monitor.py --summary

# Score past alerts against SPY, print findings and the performance summary
./.venv/bin/python monitor.py --performance

# Email the monthly "is the monitor actually working?" review
./.venv/bin/python monitor.py --monthly

# Run forever (polls every 4 hours)
./.venv/bin/python monitor.py

# Preview the digest layout as HTML without sending or calling Gemini
./.venv/bin/python notifier.py

# Run the test suite
./.venv/bin/pytest tests/
```

> All commands use `./.venv/bin/python` explicitly, so they run against the
> project's own environment regardless of what `python3` points at on your
> PATH. If you prefer a shorter prompt, activate the venv first with
> `source .venv/bin/activate` and then plain `python` refers to the same
> interpreter.

---

## Dashboard (GitHub Pages)

`export.py` writes `site/index.html` — one self-contained file with the data inlined — from the data the daily GitHub Actions run has already fetched. Published to GitHub Pages on every run.

```bash
./.venv/bin/python monitor.py --once --export   # poll + rebuild the dashboard
./.venv/bin/python export.py                    # standalone rebuild (re-fetches)
```

Everything is computed once a day in CI, and congressional disclosures lag up to 45 days by law, so there is nothing a live server could show that a file written this morning cannot. The daily job pays the fetch cost once and serves an instant page afterwards. No build step, no Node — Python writes the HTML.

Interactivity is client-side, so filtering and sorting are instant: search and sector/type/chamber/window filters over the full trade log, sortable columns everywhere, expandable signal cards, and inline SVG price-vs-SPY charts. The page also carries the performance and edge-tracking view.

**The page shows every live signal, not just the day's new ones.** `analyze()` suppresses alerts it has already emailed, so the list `monitor.poll()` hands to the exporter is a one-day delta — publishing that directly left the dashboard reading "0 signals" on any quiet day, which is most days. `export.current_signals()` re-runs the four detectors over the same window and marks the ones that fired this morning as `new`. The detectors are pure; only `analyze()` touches the seen-state, so rebuilding the picture here cannot silence the next real run.

**One-time setup:** Settings → Pages → Source → **GitHub Actions**. Until that is set the deploy step fails; it is marked `continue-on-error` so a missing Pages config cannot fail the alert run. Note the published page is public if the repo is — it is derived from public disclosures, but the conviction scores, win-rate leaderboard and alert history are visible to anyone with the URL.

---

## File Structure

```
congressional-trade-monitor/
├── config.py            # Watchlist, alert thresholds, email settings (safe to commit)
├── fetcher.py           # House + Senate data fetchers
├── openinsider_fetcher.py # OpenInsider CEO/CFO open-market buy scraper (value + market-cap filtered)
├── analyzer.py          # Cluster + cross-signal detection, conviction scoring, win-rate leaderboard
├── history.py           # Fired-alert log + forward performance vs SPY
├── review.py            # Turns performance data into plain-English findings + actions
├── committees.py        # Committee assignments + conflict detection (official gov sources)
├── notifier.py          # Ranked digest formatting and sending
├── monitor.py           # Main polling loop
├── export.py            # Builds the GitHub Pages dashboard from the daily run's data
├── site/
│   └── template.html    # Dashboard HTML template (index.html is generated)
├── .env                 # Your credentials — gitignored, never committed
├── .env.example         # Credential template — committed, no real values
├── .gitignore           # Blocks .env and seen_trades.json from git
├── requirements.txt     # ./.venv/bin/python -m pip install -r requirements.txt
├── seen_trades.json     # Auto-created state file — gitignored
├── .github/
│   └── workflows/
│       └── monitor.yml  # Daily 6 AM PST — alerts Mon-Sat, digest on Sunday
└── README.md
```

---

## Automated Scheduling (GitHub Actions)

The monitor runs automatically on GitHub's servers, allowing it to run without the local computer.

| Day | Schedule | What it does |
|-----|----------|--------------|
| Monday – Saturday | 6:00 AM PST | Fetches both chambers, detects signals, sends one ranked digest of new alerts |
| 1st of month | 6:00 AM PST | **In addition** to that day's alerts, sends the monthly review (see below). Reads the existing logs — no fetching — so it costs seconds |
| Sunday | 6:00 AM PST | Sends a weekly digest: sector accumulation vs distribution table, top signals of the week, alert performance vs SPY, and Gemini-grounded legislative intelligence |

Both are handled by a single `monitor.yml` workflow. The script checks the day of week and runs `--once` or `--summary` accordingly.

**State persistence:** `seen_trades.json` (dedup keys) and `alert_history.json` (fired-alert log) are stored in the same private GitHub Gist between runs. Each run loads the Gist at start and saves back on completion — no extra credentials are needed for the history file.

**State retention:** the Gist API silently truncates file contents past ~1MB, which would corrupt state rather than fail loudly, so both files are bounded:

| File | Bound | Rationale |
|------|-------|-----------|
| `seen_trades.json` | Keys whose newest trade date is older than `SEEN_RETENTION_DAYS` (120) are pruned on every save | Alerts only fire on trades inside `FETCH_DAYS` (45), so older keys can never re-match. The margin absorbs late and amended filings. Keys with no parseable date are always kept — dropping a live key would re-alert it. |
| `alert_history.json` | Newest `HISTORY_MAX_RECORDS` (3000) retained | Roughly a year of alerts, far more than the 30–90 days needed to calibrate `SCORE_WEIGHTS`. |
| `control_trades.json` | Same cap; `CONTROL_SAMPLE_PER_RUN` (20) sampled per run | The un-alerted comparison group. |

**Manual trigger:** The workflow has a `workflow_dispatch` trigger. You can run it on demand from the GitHub Actions tab at any time.

---

## AI-Powered Features (Gemini 2.5 Flash)

All AI features use Google Search grounding, so Gemini pulls real-time search results rather than relying on training data. Every feature degrades gracefully — if `GEMINI_API_KEY` is not set, the email still sends with all deterministic content intact and the AI blocks are simply omitted.

The model is overridable with the optional `GEMINI_MODEL` env var (default `gemini-2.5-flash`), so a future model deprecation is a config change rather than a code change. Google Search grounding has a stricter free-tier quota than plain generation; when a grounded call hits a `429` quota error, the call automatically retries **without** grounding so the email still receives AI context (sourced from the model's training data instead of live search).

**Output cleanup.** Raw Gemini responses are not fit to email as-is, and all three failure modes below were seen in live digests. `_clean_ai_text()` handles them before anything is rendered:

| Problem | Seen as | Fix |
|---------|---------|-----|
| Grounding citation markers leak into the prose | `TSM raised its outlook. [cite: 1, 2, 3]` | Stripped, including the unterminated `[cite: 1, 2,` a truncated response leaves behind |
| The model repeats its whole answer | The same paragraph twice in one block | Repeated sentences dropped, first occurrence kept |
| The token ceiling cuts it off mid-thought | `...ahead of schedule due to orders from major AI chip` | Trimmed back to the last complete sentence |

The prompts also ask explicitly for no repetition and no citation markers. Output ceilings are `AI_ALERT_MAX_TOKENS` (450) and `AI_WEEKLY_MAX_TOKENS` (600) — 220 was too tight once the prompt carried the full enriched context. Raising them costs nothing, because the free tier is rationed by *requests per day*, not tokens per request, and one run makes at most `DIGEST_AI_TOP_N + 1` calls.

All AI-generated text is passed through `html.escape()` before being inserted into HTML email bodies, so any HTML characters in Gemini's response are rendered as literal text rather than markup.

### Alert Context (top-ranked alerts in each digest)

`generate_alert_context()` in `notifier.py` makes one grounded Gemini call asking why a signal might be forming right now. The response (2–3 sentences) appears in the digest as a blue **"AI Context · Gemini + Google Search"** block below the trades table:

> *"Jensen Huang testified before the Senate Commerce Committee on AI export controls on June 17. The Semiconductor Export Reform Act cleared committee markup June 18. Two of the three congressional buyers sit on Science & Technology subcommittees with direct chip-policy authority."*

Only the top `config.DIGEST_AI_TOP_N` (default 3) alerts by conviction score get a call, so a noisy day cannot burn the grounding quota on low-conviction signals. The prompt is given the full enriched picture — disclosed size, trade dates, disclosure lag, insider names and titles, and committee conflicts — rather than just a ticker and member names, which is what keeps the output specific.

**Cost:** ~$0.035 per call (Google Search grounding). Free tier covers 1,500 grounded calls/day — well within limits for personal use.

### Weekly Digest (Sunday email)

The Sunday digest is rebuilt from a sparse alert list into a four-section intelligence report:

| Section | Source |
|---------|--------|
| Stats (Trades / Alerts / Sectors Active) | Deterministic |
| **Sector Activity table** — buy count, sell count, net ▲▼ per sector | Deterministic from `config.SECTOR_TICKERS` |
| **Strongest Signals** — top 5 alerts with color-coded tier badges | From `alerts` list |
| **Legislative Intelligence** — 3–4 grounded bullet points on bills/regulatory actions that advanced this week and their sector implications | `generate_weekly_intelligence()` → Gemini + Google Search |

**Cost:** 1 grounded call per Sunday ≈ $0.035/week ≈ $1.82/year.

---

## Data Sources

Congressional and committee data comes directly from official U.S. government sources; corporate insider buys come from OpenInsider (a free aggregator of SEC Form 4 filings). No paid APIs, no keys required, no paywalls.

| Source | Endpoint | Method |
|--------|----------|--------|
| Senate | `efdsearch.senate.gov` | Session POST (CSRF + terms agreement) → HTML table parsing |
| House | `disclosures-clerk.house.gov` | POST filing index → pdfplumber PDF parsing |
| Corporate Insiders (CEO/CFO) | `openinsider.com` | Pre-filtered screener (open-market buys, CEO+CFO, 45 days) → HTML table parsing |
| Committee Assignments | `clerk.house.gov/xml/lists/MemberData.xml` | XML parsing — House members + committee codes |
| Committee Assignments | `senate.gov/general/committee_assignments/assignments.htm` | HTML parsing — Senate members + committees |

### Why not the popular free APIs?

Every third-party aggregator was evaluated and rejected during development:

- **House/Senate Stock Watcher** — Dead as of early 2026. S3 bucket 403, domain unreachable.
- **Financial Modeling Prep (FMP)** — Congressional endpoints paywalled after August 2025.
- **Capitol Trades** — No public API.
- **Capitol Trace** — Auth failures.
- **Quiver Quantitative** — Requires authentication.

**Decision:** Build directly against official government sources. Permanent, free, zero third-party dependency.

---

## Architecture

### Senate: HTML parsing (no PDF needed)
The Senate eFD viewer pages render transaction data as a clean HTML table. No PDF download required.

**Session flow:**
1. GET `/search/home/` → receive CSRF cookie
2. POST `/search/home/` with `prohibition_agreement=1` → unlock filing access
3. POST `/search/report/data/` with CSRF → get JSON filing index (91 filings found in test)
4. GET each `/search/view/ptr/{uuid}/` → parse HTML transaction table

### House: PDF parsing
House PTR filings are only available as PDFs. The Clerk search endpoint returns server-rendered HTML (confirmed — not a React SPA), so a plain POST gives us the full filing index. Each PDF is parsed with pdfplumber using regex to extract ticker, type, date, and amount.

**Asset-type tags.** Each row carries a bracketed code, and `HOUSE_ASSET_TYPES` decides which survive: `[ST]` stock, `[OP]` option, `[OT]` other securities. Bonds (`[GS]`, `[CS]`) and private holdings (`[PS]`, `[OI]`) are dropped — they have no tradeable ticker to price against SPY.

`[OT]` is kept because **the House form has no ETF tag**; every House ETF is filed as "other securities". While `[OT]` was excluded, a Senate ETF purchase appeared in the feed and an identical House one did not — across a 180-day window that asymmetry read as 1 ETF trade in 1,131 House rows against 7 in 469 Senate rows, and it meant a House member buying a gold or silver fund was structurally invisible. The Senate needs no equivalent handling: eFD types ETFs as `Stock`, so they were never being dropped there.

**Owner codes.** The Senate viewer has an Owner column reading `Self` / `Spouse` / `Joint` /
`Child`; the House form instead prefixes the metadata cell with `SP`, `DC` or `JT` and leaves it
blank for the filer's own holdings. The House parser was hardcoding `owner: ""` for every trade,
so 39% of House rows silently lost the fact that the trade was not the member's own — three of
the four SpaceX buys above were a spouse, a dependent child and a trust. `HOUSE_OWNERS` maps the
codes onto the Senate's vocabulary so both chambers read the same downstream, where the notifier
and dashboard were already prepared to render it. Requiring whitespace after the code is
what stops a name like `SPX Technologies` from being read as a spouse trade.

`[OT]` also carries non-traded funds — BDCs, unlisted REITs — which have no ticker in the asset cell and so still fall out at the existing ticker check. Tickers written without parentheses (`Invesco QQQ`, `NYSEARCA: DIA`) are still missed; widening the ticker regex to catch them would risk reading bare words like `NEW` as tickers across the whole feed, which is a worse trade than missing an occasional row.

### Committee conflict detection
`committees.py` fetches committee assignments for all 535 members from official government XML and HTML sources. On **every** alert tier — cluster, cross-signal, win-rate, and watchlist — each participating member's committees and subcommittees are cross-referenced against sector-to-committee mappings in `config.py`. If a member sits on a committee with oversight authority over the traded ticker's sector, the conflict is flagged on that alert's card in the digest.

**The sector map is keyed on industry, not ticker.** `analyzer.sector_map()` resolves a ticker to
its market industry and maps that through `config.INDUSTRY_SECTORS`. The hand-maintained ticker
list this replaced covered **101 tickers against 631 actually traded — 12.8%**, so more than
three-quarters of alertable trades could never flag a conflict no matter who traded them. It had
also rotted in the ways such lists do: `L3H` is not a ticker (L3Harris is `LHX`), `DISH` was
delisted, `GOOGL` was listed but `GOOG` was not — so one share class flagged and the other did
not — and 20 of its 101 entries never appeared in the log at all.

Keying on industry inverts the maintenance problem. A ticker list grows without limit and needs a
new entry for every listing; the industry taxonomy is bounded, so 71 industry rows classify
**65.8% of tickers and 70.1% of trades**, and the table does not go stale when a company lists.
Conflicts flagged on deliberate purchases went from **19 to 67**.

| | Ticker list | Industry map |
|---|---|---|
| Tickers classified | 81 / 631 (12.8%) | 415 / 631 (65.8%) |
| Trades classified | 21.5% | 70.1% |
| Alertable trades classified | 22.4% | 68.7% |
| Conflicts on deliberate purchases | 19 | 67 |

`SECTOR_TICKERS` survives as a deliberately short **override** list, checked first. Funds carry no
industry at all, and a few operating companies file under one that hides what they are — `MSTR`
is "Software", `COIN` is "Capital Markets". Lookups are cached in `ticker_sectors.json` through
the same Gist-backed state store as the rest, so a run costs one call per never-before-seen
ticker and nothing for one already known.

Industries no committee oversees are deliberately absent from the map. Home improvement retail,
apparel and leisure resolve to no sector and raise no flag, which is the honest answer rather
than a forced one.

**Keyword matching is word-bounded.** `flag_conflicts` used plain substring containment, which
matched `"Technology"` inside `"Biotechnology"` and made an agriculture biotech subcommittee
oversee every tech holding its members touched — 15 false flags across the log, all of them on
one senator. Whole-word matching removes those while keeping real phrase hits like
`"Telecommunications and Media"`.

**The keywords themselves were audited against the roster**, by listing every assignment name
each one actually matches across all 506 members rather than reasoning about what they ought to
catch. That turned up two kinds of rot:

*Keywords matching nothing.* `"Science and Technology"` (the committee is called *Science, Space,
and Technology*), `"Export"`, `"Medicare"`, `"Medicaid"` and `"Pharmaceutical"` matched zero
assignments — dead entries that read as coverage.

*Keywords matching far too much.* `"Consumer Protection"` reached Senate Banking's *Financial
Institutions and Consumer Protection*, flagging consumer **finance** oversight as tech oversight;
the Commerce subcommittee it was meant to catch already matches on `"Technology"` and `"Data
Privacy"`, so dropping it costs nothing. `"Technology"` alone spanned 147 member-slots including
*Emergency Management and Technology*, too wide for Semiconductors or Telecom. `"Agriculture"`
put all 123 of that committee's slots under Crypto when the CFTC nexus is one subcommittee,
*Commodity Markets*. `"Infrastructure"`, `"Energy and Commerce"` under Mining, and the rest are
listed in `COMMITTEE_SECTORS`.

`AMBIGUOUS_PHRASES` handles the one case whole-word matching cannot. `"Intelligence"` must reach
*Defense Intelligence and Overhead Architecture* but not *Digital Assets, Financial Technology,
and Artificial Intelligence* — the word is genuinely present in both, so the phrase is removed
from the assignment name before matching and the name is still reported as it is actually called.

Together these cut flagged deliberate purchases from 67 to **49** while every legitimate flag
survives: SpaceX still flags Armed Services for Cisneros and McGuire, Whitehouse still flags
Semiconductors on MU, Gottheimer still flags Tech on MSFT and — via his real intelligence
assignments rather than the AI panel — Defense on LMT.

**Committee names come from the XML, not a lookup table.** `MemberData.xml` carries a `<committees>` block defining every code it later references, so both committee and subcommittee names are parsed from the same document as the assignments and cannot drift out of sync with them. An earlier hand-written code→name map had `FA00` as Financial Services (it is Foreign Affairs) and `SO00` as Intelligence (it is Ethics), and was missing `SM00`, `ZS00` and `QJ00` entirely — which both showed raw codes in the UI and produced false Financial Services conflict flags for all 54 Foreign Affairs members. Subcommittees now resolve to real names too, so they can actually match the sector keywords in `config.COMMITTEE_SECTORS`; previously they were stored as codes like `FA05` and could never match anything.

**Name format fix:** Trade disclosures return names as `"Last, First Middle"` (e.g. `"Taylor, David J."`) while the committee cache keys names as `"First Last"`. `get_member_committees()` detects the comma-separated format and retries with both `"First Middle Last"` and `"First Last"` (dropping the middle initial), dramatically improving committee coverage. `display_name()` applies the same flip for presentation so the House and Senate feeds do not show two naming conventions side by side; it leaves suffix commas (`"A. Mitchell McConnell, Jr."`) alone. Display only — dedup keys and history records keep the raw name, so normalizing cannot re-fire old alerts.

**Example:** Whitehouse sells NVDA → flagged for sitting on Commerce/Science/Transportation, International Trade subcommittee (chip export policy), and Emerging Threats and Capabilities.

### Unified output schema
Both chambers normalize to the same dict so all downstream modules are chamber-agnostic:

```python
{
    "chamber":           "Senate" | "House",
    "representative":    "Sheldon Whitehouse",
    "ticker":            "NVDA",
    "asset_description": "NVIDIA Corporation - Common Stock",
    "type":              "purchase" | "sale" | "sale_partial",
    "transaction_date":  "2026-05-08",
    "disclosure_date":   "2026-06-02",
    "amount":            "$100,001 - $250,000",
    "ptr_link":          "https://...",
    "owner":             "Self" | "Spouse" | "Joint" | "Child",
    "asset_type":        "stock" | "option" | "other",
}
```

### Insider extension & cross-signal detection
`openinsider_fetcher.py` scrapes a pre-filtered OpenInsider screener (open-market purchases only, CEO + CFO titles, last 45 days) and parses the results HTML table into the same flat-dict shape used elsewhere, tagged with `"source": "insider"`:

```python
{
    "source":           "insider",
    "name":             "Andrew Anagnost",
    "title":            "Pres, CEO",
    "company":          "Autodesk, Inc.",
    "ticker":           "ADSK",
    "type":             "purchase",
    "transaction_date": "2026-06-16",
    "disclosure_date":  "2026-06-16",
    "shares":           2460,
    "price":            202.66,
    "value":            498544.0,
    "amount":           "+$498,544",
    "ptr_link":         "https://openinsider.com/ADSK",
}
```

**Pagination.** openinsider serves at most 100 rows per page regardless of the `rows=` parameter, and the screener routinely matches more — page 2 comes back full of distinct filings. Reading only page 1 silently discarded the rest. `_fetch_screener_rows()` reads up to `FETCH_MAX_PAGES` (3), stopping early on a short page and de-duplicating rows that appear on more than one. This raised coverage from 37 to 121 buys (67 distinct tickers), and **doubled CEO/CFO coverage on its own** — truncation had been costing high-seniority signal, not just volume.

**Feed resilience.** openinsider is a single unauthenticated endpoint and does time out in practice. Each page retries `FETCH_ATTEMPTS` (3) times with linear backoff. If page 1 fails outright it raises `InsiderFetchError` rather than returning an empty list; if a *later* page fails, the rows already collected are kept, since a partial outage should not cost the whole run.

### Chamber feed outages

`fetch_all` degrades to one chamber rather than crashing. On 2026-08-18 senate.gov began
returning `403 Forbidden` to the GitHub Actions runner — the same requests succeeded from a
residential connection, so it is datacenter-IP blocking, not a broken scraper. The unhandled
`HTTPError` took the whole run down: House trades, the insider feed, the digest and the
dashboard were all lost to a Senate outage.

Both chamber index fetches now retry `FETCH_ATTEMPTS` times with linear backoff and raise
`ChamberFetchError` on exhaustion. `fetch_all` catches it per chamber, appends a warning that
travels into the digest, and continues. Losing **both** chambers still raises — no
congressional data at all is an outage, not a quiet market.

Senate committee assignments come from a separate senate.gov page that 403s under the same
conditions. That path already degraded gracefully (a printed warning); the effect is that
senators temporarily lose committee-conflict flags while House members keep theirs.

That distinction matters: "the feed is down" makes cross-signals impossible, while "no insiders bought anything" is a real market observation, and collapsing the two silently switches off the strongest signal in the monitor. Callers degrade rather than crash — `monitor.poll()` continues with congressional alerts and prints `CROSS-SIGNAL DETECTION DISABLED FOR THIS RUN`, passing a warning banner into the digest email so a thin digest is never mistaken for a quiet market; the dashboard shows the same warning and renders its remaining panels.

**Noise filtering at the scraper boundary** (so analyzer, notifier, and dashboard all receive a clean list):
- **Minimum trade value** — drops buys under `MIN_TRADE_VALUE` ($50k), removing micro-cap penny-stock noise.
- **Minimum market cap** — the OpenInsider screener exposes no market-cap parameter, so market cap is looked up per unique ticker via yfinance and buys under `MIN_MARKET_CAP_M` ($300M, overridable via the `MIN_MARKET_CAP_M` env var) are dropped. Tickers with no market-cap data are kept, so a transient yfinance miss never silently discards a legitimate large-cap.

**Cross-signal detection** (`find_cross_signals` / `detect_cross_cluster_alerts` in `analyzer.py`) groups congressional purchases and insider buys by ticker and fires a 🔗 alert when both appear on the same ticker within `CLUSTER_DAYS` (45).

Proximity is **pairwise**: a trade joins the signal if it falls within the window of at least one trade on the *other* side. Measuring one span across every trade on the ticker — the original implementation — let a single unrelated older congressional buy push a genuinely tight Congress/insider pairing outside the window and discard the entire signal. The match carries only the trades in the overlap, so the "N days apart" figure describes the alert's actual contents.

The alert card lists congressional buys and insider buys in separate tables. Existing congressional detectors are untouched — the cross-signal path is purely additive and runs alongside them in `monitor.poll()`.

### Win-Rate Calculation
Uses yfinance to pull stock price on `transaction_date`, compares 30/60/90-day forward returns vs. SPY benchmark. A trade is a win if the member outperformed SPY. Minimum 10 scored trades required before a member qualifies for win-rate alerts.

All price lookups are memoized per process (`analyzer._PRICE_CACHE`), so SPY is downloaded once per date rather than once per scored trade.

### Alert performance tracking

Win rates measure *members*. `history.py` measures the *monitor itself*: every fired alert is appended to `alert_history.json` with its conviction score, tier, and entry price at fire time. Once a forward window elapses, `score_history()` fills in the return, SPY's return, and the excess over the same span.

```
$ python monitor.py --performance

Alert performance vs SPY over 60 days
  38 recorded · 6 still maturing

  By tier
    cross_cluster     9 scored · trade-date 67% / +4.2% · actionable 56% / +1.1%
    cluster          14 scored · trade-date 50% / +0.8% · actionable 43% / -0.4%
    watchlist         9 scored · trade-date 44% / -1.1% · actionable 44% / -1.3%

  By direction
    buy              21 scored · trade-date 57% / +2.1% · actionable 52% / +0.5%
    sell             11 scored · trade-date 45% / -0.6% · actionable 45% / -0.7%

  By conviction score
    0-40              7 scored · trade-date 43% / -0.9% · actionable 43% / -1.0%
    40-70            16 scored · trade-date 50% / +1.1% · actionable 44% / -0.2%
    70+               9 scored · trade-date 67% / +4.4% · actionable 56% / +1.4%

  Cross-signals by insider seniority
    CEO/CFO           5 scored · trade-date 80% / +6.1% · actionable 60% / +2.2%
    other/dir.        4 scored · trade-date 50% / +1.0% · actionable 25% / -0.9%
```

**Is it real, or luck?** An average edge cannot on its own be told apart from chance — with a handful of alerts and normal market noise, +2% arises routinely. `bootstrap()` resamples the edge values with replacement `BOOTSTRAP_ITERATIONS` (5000) times, producing the spread of averages the same data could plausibly have given. If that interval clears zero the effect is real; if it straddles zero there is no evidence either way, however good the headline looks. `bootstrap_difference()` does the same for *alerted minus un-alerted* — the sharpest question the data can answer, and the actual verdict on the detectors. Both are seeded, so the interval does not jitter between runs, and both return nothing below `BOOTSTRAP_MIN_SAMPLES` (5) rather than pretending to a result.

**Does it hold across horizons?** Every alert is already scored at 30/60/90 days. A genuine signal points the same way at all three; an effect that appears at exactly one horizon and vanishes at the others is almost always the window landing on a lucky stretch. `horizon_view()` prints all three and flags a sign flip, which also guards against quoting whichever column flatters the result.

### Monthly review

The weekly digest carries a brief performance section; the **monthly review** (`--monthly`, sent on the 1st) is a separate email whose only question is *should I keep doing this*. `review.py` turns the numbers into plain-English findings, each with a copy-pasteable instruction:

```
🔴  WATCHLIST alerts are losing money
    23 matured WATCHLIST alerts averaged -2.9% (95% confidence: -3.4% to -2.4%).
    The whole confidence range is below zero, so this tier is reliably picking
    losers rather than merely being unhelpful.
    → To act on this, send me: "disable the watchlist alert tier (23 alerts, -2.9% edge, ...)"
```

Separating it is deliberate rather than cosmetic. Bundled into the weekly digest this section gets skimmed — it sits at the bottom, it barely moves week to week, and it competes with signals you might act on today. Alone, once a month, it arrives at roughly the rate the data actually changes, and checking a confidence interval monthly against pre-set rules is far harder to fool yourself with than checking it weekly and eventually catching the week it happens to clear zero.

Nothing recommends a config change below `REC_MIN_SAMPLES` (20) matured alerts — showing a wide interval is honest, advising a change off ten data points is not.

**The control group.** Every run also records a sample of congressional trades that did **not** trigger an alert (`control_trades.json`), scored through the identical code path. This is the null hypothesis made concrete: an alert hit rate on its own is uninterpretable, because if un-alerted trades perform just as well then the detectors contribute nothing and the headline number is merely the base rate of congressional trading. `--performance` leads with the comparison.

**Two baselines.** Every record is scored twice. `entry_*` measures from the politician's trade date — it answers "was their trade good". `act_*` measures from the day the alert actually reached you, which is the first moment you could have acted. Because disclosure lags by up to 45 days, the trade-date figure includes a return you had no way to capture; the gap between the two *is* the cost of disclosure lag. **Only the actionable column says whether the monitor is worth running.**

**Edge, not excess.** Raw excess return answers "did the stock beat SPY", which is only the right question for a buy signal. A sell cluster predicts *under*performance, so a stock that beats SPY means that signal was **wrong**. `edge_N` flips the sign for sells, so it reads uniformly as "how far the signal was right" and is safe to aggregate across directions. Alerts recorded before direction tracking existed cannot be scored either way and are excluded from aggregation rather than silently inverted.

The tier breakdown says which signal types earn their place. The score breakdown is a check on the conviction weights themselves — if the 70+ bucket does not outperform the 0-40 bucket, the weights in `config.SCORE_WEIGHTS` are miscalibrated and should be changed. Nothing here is meaningful until roughly 30–60 days of alerts have matured.

*(Numbers above are illustrative of the output format, not measured results.)*

### State management
`seen_trades.json` tracks every trade that has already triggered an alert using a `representative|ticker|date|type` key. On each poll, only truly new trades fire alerts without duplicate emails. Cross-signal alerts dedupe against the same file using a `crosscluster|ticker|<participants>` key, so an overlap re-fires only when a new buyer joins it.

---

## Key Design Decisions

**Build all three alert tiers in one pass** rather than shipping cluster-only first. Adding win-rate later would require refactoring the alert schema that notifier and monitor are already built against.

**PDF parsing over Selenium** for the House. The Clerk search is server-rendered HTML accessible via a plain POST. No headless browser needed.

**Senate HTML over PDF** for the Senate. The eFD viewer renders transactions directly in an HTML table, making PDF download unnecessary and parsing cleaner.

**Official government sources for committee data** rather than third-party APIs. Both the House Clerk XML and Senate.gov HTML are free, permanent, and require no authentication.

**Measure the monitor, not the politicians.** Every alert is scored twice — from the trade date and from the day the alert reached you — and against a control group of trades that never alerted. It would have been easier to report "X% of our alerts beat SPY", and that number is close to meaningless: it cannot distinguish the detectors working from the base rate of congressional trading, and it credits returns the disclosure lag made uncapturable. The harder measurement is the only one that answers whether the tool is worth running.

**Decision rules written before the data arrived.** The thresholds in `review.py` were set against an empty log. Choosing them after seeing which tiers performed would have guaranteed they looked good.

**Fail loudly on a dead feed.** An unreachable insider feed raises rather than returning an empty list, because "the feed is down" and "no insiders bought anything" mean opposite things and used to be indistinguishable — silently disabling the strongest signal in the system.

**Rate limiting by design**: polls every 4 hours (6 requests/day per source), 200 PDF cap per run, `seen_trades.json` prevents re-downloading already-processed filings.

---

## Verified Output (August 8, 2026)

From a live GitHub Actions run plus local measurement on the same day:

```
Senate:  473 trades (last 180 days, 50 filings parsed)
House:  1233 trades (last 180 days, 200 PDFs parsed)
Total:  1706 trades · 150 in the 45-day alert window · Runtime: ~8.5 minutes
Insider: 121 buys across 67 tickers (299 filings read over 3 screener pages)

Digest — 8 signals, ranked by conviction:
  #1  [CROSS-SIGNAL] TSM   67/100   1 congressional buy + 3 insider buys, 24d apart
        Congress ~$8K · Insider ~$73K · lag 0d (fresh) · -3.2% since trade (SPY +2.4%) ✗
  #2  [CLUSTER]      NVDA  62/100   3 members selling, 2026-06-30 → 2026-07-21
        ~$73K · lag 1d (fresh) · +11.9% since trade (SPY +3.5%) ✗
        ⚠ Whitehouse: Commerce, Science, and Transportation (oversees Semiconductors — NVDA)
  #3  [CLUSTER]      MKL   55/100   2 members selling
  ... down to 52/100
```

The ✗ marks are the direction check: NVDA rose 8.4% against SPY *after* three members
sold it, so that signal was wrong so far — the same percentage would be a ✓ on a buy.

## Testing

```bash
./.venv/bin/pytest tests/
```

145 tests, no network — yfinance and the Gist are monkeypatched out, so the suite runs offline and deterministically. Coverage is deliberately concentrated on the logic that is easy to get quietly wrong rather than on plumbing:

| Area | What it pins |
|------|--------------|
| Amount parsing | Disclosure brackets, open-ended `$50,000,000 +`, unparseable input |
| Disclosure lag | Both chambers, missing dates, and that a disclosure predating the trade reads as 0 rather than negative |
| Conviction scoring | Ordering (size, breadth, freshness), that unknown lag scores between fresh and stale, and that the score sizes on congressional dollars only |
| Cross-signals | Pairwise proximity — including the regression that a stale outlier must not veto a tight pairing |
| Direction | That a sell alert's edge is inverted, the bug that made sell signals record backwards |
| Two baselines | That the actionable entry excludes the return eaten by disclosure lag |
| Control group | Exclusion of alerted trades, sampling cap, per-day stability |
| Significance | That obvious noise reads as "no evidence" and a clear effect reads as real |
| State retention | Pruning by a key's *newest* date, and that undated keys are never dropped |
| Feed resilience | Retry, pagination past the 100-row cap, and that an outage is not an empty result |
| House asset tags | That ETFs survive under `[OT]`, bonds and untickered funds do not, and an `[OT]` row cannot leak its dates to the row below it |
| House owner codes | That `SP`/`DC`/`JT` map to the Senate's words, a bare row is the filer's own, and an asset name starting with those letters is not misread |
| New-listing guard | That an IPO-week cluster is suppressed, a long-listed one is not, and an unavailable price feed fails open rather than silencing alerts |
| Sector resolution | That industry drives the sector, an override beats it, funds and unmapped industries resolve to nothing, and a ticker is looked up only once |
| Conflict keywords | That a keyword cannot match inside a longer word, that whole phrases still match, that an AI panel is not defense intelligence, and that consumer *finance* oversight is not tech oversight |

---

## Configuration (config.py)

```python
CLUSTER_MIN_MEMBERS = 2        # members needed for cluster alert (lowered from 3)
CLUSTER_DAYS        = 45       # rolling window (extended from 30)
WIN_RATE_MIN        = 0.60     # 60% win rate threshold
WIN_RATE_MIN_TRADES = 10       # minimum scored trades
WIN_RATE_PRIMARY    = 60       # days forward vs SPY
POLL_INTERVAL_SECONDS = 14400  # 4 hours
FETCH_DAYS          = 45       # alert window
WATCHLIST           = [        # members whose any trade triggers an alert
    "Nancy Pelosi",
    "Josh Gottheimer",
    "Dan Crenshaw",
    "Tommy Tuberville",
    "Mark Warner",
    "Brian Mast",
]
SECTOR_TICKERS      = {...}    # sector → ticker mappings for conflict detection
COMMITTEE_SECTORS   = {...}    # committee keywords → sector mappings

# ── Conviction scoring ──
SCORE_WEIGHTS       = {...}    # max points per component (see Alert Tiers above)
SCORE_DOLLAR_CAP    = 1_000_000  # congressional dollars earning full size points
SCORE_PARTICIPANT_CAP = 5      # participants earning full breadth points
DIGEST_AI_TOP_N     = 3        # alerts per digest that get a Gemini blurb
AI_ALERT_MAX_TOKENS = 450      # output ceiling per alert blurb
AI_WEEKLY_MAX_TOKENS = 600     # output ceiling for the weekly intelligence

# ── Performance tracking ──
HISTORY_FILE        = "alert_history.json"   # fired alerts, in the same Gist
CONTROL_FILE        = "control_trades.json"  # un-alerted comparison group
CONTROL_SAMPLE_PER_RUN = 20    # control trades sampled each run
SEEN_RETENTION_DAYS = 120      # prune seen-keys that can no longer match
HISTORY_MAX_RECORDS = 3000     # cap both logs under the Gist truncation limit
BOOTSTRAP_ITERATIONS = 5_000   # resamples for the confidence interval
BOOTSTRAP_MIN_SAMPLES = 5      # below this, report "too few" instead of a number
REC_MIN_SAMPLES     = 20       # below this, the monthly review advises nothing
```

**Watchlist rationale:** Pelosi (Paul's options trades historically correlated with legislation), Gottheimer (semiconductor trades near CHIPS Act votes, Financial Services committee), Crenshaw (defense/energy trades, Armed Services committee), Tuberville (defense/energy trades, Senate Armed Services, single-handedly held up military appointments), Warner (tech/finance background, Senate Intelligence + Finance committees), Mast (active defense sector trades, Armed Services committee).

---

## Email Setup (Gmail)

Credentials are stored in `.env`, NOT in source code.

1. Create a dedicated Gmail account for sending alerts
2. Enable 2FA on that account
3. Go to **myaccount.google.com/apppasswords** → create App Password
4. Copy `.env.example` to `.env` and fill in your values:
   ```
   ALERT_EMAIL_SENDER=your_alert_account@gmail.com
   ALERT_EMAIL_PASSWORD=xxxx xxxx xxxx xxxx
   ALERT_EMAIL_RECIPIENTS=your_email@gmail.com
   GEMINI_API_KEY=your_gemini_api_key   # optional — get one free at aistudio.google.com
   # Optional: override the Gemini model (default gemini-2.5-flash)
   GEMINI_MODEL=gemini-2.5-flash
   # Optional: minimum market cap (in $M) for OpenInsider CEO/CFO buys (default 300)
   MIN_MARKET_CAP_M=300
   ```
   For GitHub Actions, also add `GEMINI_API_KEY` as a repository secret (Settings → Secrets and variables → Actions).

Test with: `./.venv/bin/python notifier.py`

**Why this approach:** `config.py` is safe to commit publicly. `.env` is gitignored and stays on your machine only. Anyone cloning the repo copies `.env.example` to `.env` and adds their own credentials.

---

## AI-Assisted Development Note

Built with AI pair-programming assistance. All architectural decisions, source evaluation, and executive calls made by Davin Kim. Key decisions documented throughout this README.