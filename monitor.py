"""
Congressional + Fund Manager Portfolio Monitor v3
==================================================
Changes from v2:
  - Data source: US Gov XML/JSON feeds (free, no API key)
  - No Gmail / email — replaced by dashboard_data.json
  - dashboard_data.json committed to GitHub repo each run
  - Claude artifact reads the JSON and renders the dashboard

Data sources:
  House PTR   → disclosures-clerk.house.gov (free XML)
  Senate eFD  → efts.senate.gov (free JSON)
  SEC EDGAR   → data.sec.gov (free JSON, fund 13F)
  Alpaca      → paper-api.alpaca.markets (paper trades)
  Gemini      → generativelanguage.googleapis.com (AI analysis)
"""

import os
import io
import json
import time
import zipfile
import logging
import requests
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ── Secrets ───────────────────────────────────────────────────────────────────
ALPACA_API_KEY     = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET_KEY  = os.environ["ALPACA_SECRET_KEY"]
ALPACA_BASE_URL    = "https://paper-api.alpaca.markets"
GEMINI_API_KEY     = os.environ["GEMINI_API_KEY"]
GITHUB_TOKEN       = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO        = "liejofernandus9/portfolio-monitor"

# ── Gemini endpoints ──────────────────────────────────────────────────────────
GEMINI_FLASH_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
)

# ── Config ────────────────────────────────────────────────────────────────────
DEPOSIT_AMOUNT        = 250.00
DEPOSIT_INTERVAL_DAYS = 14
MAX_POSITION_PCT      = 0.60
MIN_POSITION_USD      = 25.00
BUY_SCORE_THRESHOLD   = 6
MAX_LAG_DAYS          = 30
CONGRESS_REFRESH_DAYS = 21
FUND_REFRESH_DAYS     = 28
TOP_N                 = 5
TEST_PERIOD_DAYS      = 60

CONVICTION_MAP = {
    "1,001 - 15,000":     0.10,
    "15,001 - 50,000":    0.20,
    "50,001 - 100,000":   0.30,
    "100,001 - 250,000":  0.40,
    "250,001 - 500,000":  0.60,
    "500,001 - 1,000,000":0.75,
    "1,000,001 - 5,000,000": 0.85,
    "5,000,001":          1.00,
}

# ── Default targets ───────────────────────────────────────────────────────────
DEFAULT_CONGRESS = [
    {"name": "Nancy Pelosi",    "last_name": "Pelosi",     "chamber": "house"},
    {"name": "David Rouzer",    "last_name": "Rouzer",     "chamber": "house"},
    {"name": "Josh Gottheimer", "last_name": "Gottheimer", "chamber": "house"},
    {"name": "Dan Crenshaw",    "last_name": "Crenshaw",   "chamber": "house"},
    {"name": "Ron Wyden",       "last_name": "Wyden",      "chamber": "senate"},
]

DEFAULT_FUNDS = [
    {"name": "Bill Ackman / Pershing Square", "cik": "0001336528"},
    {"name": "Michael Burry / Scion",         "cik": "0001649339"},
    {"name": "Stan Druckenmiller / Duquesne", "cik": "0001536411"},
    {"name": "Warren Buffett / Berkshire",    "cik": "0001067983"},
    {"name": "Philippe Laffont / Coatue",     "cik": "0001336920"},
]

CACHE_FILE     = "seen_trades.json"
DASHBOARD_FILE = "dashboard_data.json"


# ═══════════════════════════════════════════════════════════════════════════════
# CACHE
# ═══════════════════════════════════════════════════════════════════════════════

def load_cache() -> dict:
    now      = datetime.utcnow().isoformat()
    defaults = {
        "seen":                  [],
        "cash_balance":          0.00,
        "total_deposited":       0.00,
        "total_invested":        0.00,
        "last_deposit_date":     None,
        "first_deposit_date":    None,
        "positions":             {},
        "missed_signals":        [],
        "signal_log":            [],   # full log for dashboard feed
        "trade_history":         [],   # every paper trade placed
        "congress_targets":      DEFAULT_CONGRESS,
        "fund_targets":          DEFAULT_FUNDS,
        "last_congress_refresh": None,
        "last_fund_refresh":     None,
        "start_date":            now,
        "benchmark_start":       {"QQQ": None, "VOO": None},
        "benchmark_current":     {"QQQ": None, "VOO": None},
    }
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE) as f:
            existing = json.load(f)
        for key, val in defaults.items():
            if key not in existing:
                existing[key] = val
        if not isinstance(existing.get("benchmark_start"), dict):
            existing["benchmark_start"] = {"QQQ": None, "VOO": None}
        existing["benchmark_start"].setdefault("QQQ", None)
        existing["benchmark_start"].setdefault("VOO", None)
        existing.setdefault("benchmark_current", {"QQQ": None, "VOO": None})
        existing.setdefault("signal_log",    [])
        existing.setdefault("trade_history", [])
        return existing
    return defaults


def save_cache(cache: dict):
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2, default=str)


# ── Date helpers ──────────────────────────────────────────────────────────────

def days_since(date_str: str) -> int:
    if not date_str:
        return 999
    try:
        return (datetime.utcnow() - datetime.strptime(date_str[:10], "%Y-%m-%d")).days
    except Exception:
        return 999


def days_since_iso(iso_str) -> int:
    if not iso_str:
        return 999
    try:
        return (datetime.utcnow() - datetime.fromisoformat(str(iso_str))).days
    except Exception:
        return 999


def days_into_test(cache: dict) -> int:
    try:
        start = datetime.fromisoformat(cache.get("start_date", datetime.utcnow().isoformat()))
        return (datetime.utcnow() - start).days
    except Exception:
        return 0


def is_deposit_monday(cache: dict) -> bool:
    today = datetime.utcnow()
    if today.weekday() != 0:
        return False
    last = cache.get("last_deposit_date")
    if not last:
        return True
    return days_since_iso(last) >= DEPOSIT_INTERVAL_DAYS


def next_deposit_date(cache: dict) -> str:
    last = cache.get("last_deposit_date")
    if not last:
        today    = datetime.utcnow()
        days_to  = (7 - today.weekday()) % 7 or 7
        return (today + timedelta(days=days_to)).strftime("%B %d, %Y")
    try:
        return (datetime.fromisoformat(last) + timedelta(days=14)).strftime("%B %d, %Y")
    except Exception:
        return "Next Monday"


# ═══════════════════════════════════════════════════════════════════════════════
# CONVICTION → ALLOCATION
# ═══════════════════════════════════════════════════════════════════════════════

def parse_conviction(range_str: str) -> float:
    if not range_str:
        return 0.20
    clean = range_str.replace("$", "").replace(",", "").replace(" ", "").lower()
    for key, pct in CONVICTION_MAP.items():
        key_clean = key.replace(",", "").replace(" ", "")
        if key_clean in clean:
            return pct
    try:
        digits = "".join(c for c in range_str if c.isdigit())
        if digits:
            val = float(digits[:10])
            if val < 15000:    return 0.10
            if val < 50000:    return 0.20
            if val < 100000:   return 0.30
            if val < 250000:   return 0.40
            if val < 500000:   return 0.60
            if val < 1000000:  return 0.75
            if val < 5000000:  return 0.85
            return 1.00
    except Exception:
        pass
    return 0.20


def calculate_allocation(conviction_pct: float, cash_balance: float) -> float:
    raw       = cash_balance * conviction_pct
    capped    = min(raw, cash_balance * MAX_POSITION_PCT)
    available = min(capped, cash_balance)
    if available < MIN_POSITION_USD:
        return 0.0
    return round(available, 2)


# ═══════════════════════════════════════════════════════════════════════════════
# GEMINI AI
# ═══════════════════════════════════════════════════════════════════════════════

def gemini_call(prompt: str) -> str:
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 700, "temperature": 0.3},
    }
    try:
        resp = requests.post(GEMINI_FLASH_URL, json=body, timeout=30)
        resp.raise_for_status()
        return resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        log.warning(f"Gemini failed: {e}")
        return ""


def extract_ticker(raw_text: str) -> str:
    prompt = (
        "Extract ONLY the stock ticker symbol from this text. "
        "Reply with just the ticker in capitals, nothing else. "
        f"If no clear ticker, reply UNKNOWN.\n\nText: {raw_text[:400]}"
    )
    result = gemini_call(prompt)
    if not result:
        return "UNKNOWN"
    ticker = result.strip().upper().split()[0]
    return ticker if ticker.isalpha() and len(ticker) <= 5 else "UNKNOWN"


def get_ai_analysis(trade_summary: str, score_result: dict,
                    allocation: float, cash_after: float) -> str:
    prompt = f"""You are a concise investment analyst. A portfolio monitor detected:

{trade_summary}

SIGNAL SCORE: {score_result['score']}/10
REASONS: {', '.join(score_result['reasons'])}
ACTION: {score_result['action']}
AMOUNT DEPLOYED: ${allocation:.2f}
CASH AFTER: ${cash_after:.2f}

Write exactly 3 short paragraphs:
1. What this trade signals strategically
2. What a retail investor with ${allocation:.2f} should do
3. Key risks to watch

Direct and specific. No disclaimers. No preamble."""
    result = gemini_call(prompt)
    return result if result else "Analysis unavailable."


def get_day60_verdict(cache: dict, positions: list) -> str:
    total_val    = sum(float(p.get("market_value", 0)) for p in positions)
    total_dep    = cache.get("total_deposited", 0)
    total_return = ((total_val - total_dep) / total_dep * 100) if total_dep else 0
    prompt = f"""A 60-day paper trading test just completed.

RESULTS:
- Total deposited: ${total_dep:.2f}
- Portfolio value: ${total_val:.2f}
- Total return: {total_return:+.1f}%
- Signals fired: {len(cache.get('seen', []))}
- Missed signals: {len(cache.get('missed_signals', []))}

Write a 3-paragraph verdict:
1. Performance vs QQQ (~5%) and VOO (~4%) over 60 days
2. Recommendation: deploy real money, adjust strategy, or redirect to index funds
3. What to watch in the next 60 days if going live

Be direct. Give a real recommendation."""
    return gemini_call(prompt) or "Verdict unavailable."


# ═══════════════════════════════════════════════════════════════════════════════
# DATA FETCHING — US GOVERNMENT SOURCES
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_house_trades(last_name: str) -> list:
    """
    Fetch House PTR filings from the official House Clerk XML feed.
    URL: https://disclosures-clerk.house.gov/FinancialDisclosure
    Downloads the current year's PTR ZIP, parses XML for the member.
    """
    year = datetime.utcnow().year
    url  = f"https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{year}FD.zip"
    headers = {"User-Agent": "PortfolioMonitor/1.0 research@example.com"}

    try:
        log.info(f"  Fetching House XML feed for {last_name}...")
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()

        trades = []
        with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
            # Find the XML file inside the zip
            xml_files = [f for f in z.namelist() if f.endswith(".xml")]
            if not xml_files:
                log.warning("  No XML found in House ZIP")
                return []

            with z.open(xml_files[0]) as xf:
                tree = ET.parse(xf)
                root = tree.getroot()

            # Parse each Member element
            for member in root.findall(".//Member"):
                name_el = member.find("Last")
                if name_el is None or last_name.lower() not in (name_el.text or "").lower():
                    continue
                # Each PTR has one or more transactions
                for ptr in member.findall(".//Ptr"):
                    for tx in ptr.findall(".//Transaction"):
                        ticker     = (tx.findtext("Ticker") or "").strip().upper()
                        tx_type    = tx.findtext("Type") or ""
                        tx_date    = tx.findtext("TransactionDate") or ""
                        file_date  = tx.findtext("FilingDate") or ptr.findtext("FilingDate") or ""
                        amount     = tx.findtext("Amount") or ""
                        asset      = tx.findtext("AssetName") or ""
                        trades.append({
                            "ticker":     ticker,
                            "type":       tx_type,
                            "trade_date": tx_date,
                            "filed_date": file_date,
                            "amount":     amount,
                            "asset":      asset,
                            "member":     f"{member.findtext('First','')} {name_el.text}".strip(),
                            "source":     "House Clerk (Official)",
                        })
        log.info(f"  Found {len(trades)} House trades for {last_name}")
        return trades[:20]

    except Exception as e:
        log.warning(f"  House XML fetch failed for {last_name}: {e}")
        return []


def fetch_senate_trades(last_name: str) -> list:
    """
    Fetch Senate PTR filings from the Senate eFD search endpoint.
    URL: https://efts.senate.gov/LATEST/search-index
    Free JSON API, no key required.
    """
    url     = "https://efts.senate.gov/LATEST/search-index"
    params  = {
        "q":          last_name,
        "report_types": "PTR",
        "filer_types": "Senator",
        "limit":      10,
    }
    headers = {"User-Agent": "PortfolioMonitor/1.0 research@example.com"}

    try:
        log.info(f"  Fetching Senate eFD for {last_name}...")
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        data     = resp.json()
        hits     = data.get("hits", {}).get("hits", [])
        trades   = []

        for hit in hits:
            src       = hit.get("_source", {})
            name      = src.get("first_name", "") + " " + src.get("last_name", "")
            file_date = src.get("date_filed", "")
            # Senate PTRs link to PDFs — we extract what metadata is available
            # Full transaction details require PDF parsing; return filing-level data
            trades.append({
                "ticker":     "PENDING",   # requires PDF parse
                "type":       "PTR Filing",
                "trade_date": file_date,
                "filed_date": file_date,
                "amount":     "See filing",
                "asset":      src.get("document_description", ""),
                "member":     name.strip(),
                "doc_id":     hit.get("_id", ""),
                "source":     "Senate eFD (Official)",
            })

        log.info(f"  Found {len(trades)} Senate filings for {last_name}")
        return trades

    except Exception as e:
        log.warning(f"  Senate eFD fetch failed for {last_name}: {e}")
        return []


def fetch_congressional_trades(member: dict) -> list:
    """Route to House or Senate fetcher based on chamber."""
    if member.get("chamber") == "senate":
        return fetch_senate_trades(member["last_name"])
    return fetch_house_trades(member["last_name"])


def fetch_fund_manager_filing(cik: str) -> list:
    headers = {"User-Agent": "PortfolioMonitor research@example.com"}
    url     = f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json"
    try:
        resp    = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        data    = resp.json()
        forms   = data.get("filings", {}).get("recent", {}).get("form", [])
        dates   = data.get("filings", {}).get("recent", {}).get("filingDate", [])
        acc_nos = data.get("filings", {}).get("recent", {}).get("accessionNumber", [])
        for form, date, acc in zip(forms, dates, acc_nos):
            if form == "13F-HR":
                return [{"filed_date": date, "acc_number": acc, "cik": cik}]
        return []
    except Exception as e:
        log.warning(f"SEC EDGAR failed for CIK {cik}: {e}")
        return []


def get_stock_price(ticker: str) -> float | None:
    headers = {
        "APCA-API-KEY-ID":     ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }
    try:
        resp = requests.get(
            f"https://data.alpaca.markets/v2/stocks/{ticker}/quotes/latest",
            headers=headers, timeout=10
        )
        resp.raise_for_status()
        return float(resp.json().get("quote", {}).get("ap", 0)) or None
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# DYNAMIC TARGET REFRESH
# ═══════════════════════════════════════════════════════════════════════════════

def refresh_congress_targets(cache: dict) -> tuple:
    """Re-rank using recent House XML data."""
    log.info("Refreshing congressional targets...")
    year    = datetime.utcnow().year
    url     = f"https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{year}FD.zip"
    headers = {"User-Agent": "PortfolioMonitor/1.0 research@example.com"}

    try:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        scores: dict = {}
        cutoff = datetime.utcnow() - timedelta(days=365)

        with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
            xml_files = [f for f in z.namelist() if f.endswith(".xml")]
            if not xml_files:
                return cache["congress_targets"], [], []
            with z.open(xml_files[0]) as xf:
                tree = ET.parse(xf)
                root = tree.getroot()

            for member in root.findall(".//Member"):
                first = member.findtext("First", "").strip()
                last  = member.findtext("Last", "").strip()
                name  = f"{first} {last}".strip()
                if not name:
                    continue
                for ptr in member.findall(".//Ptr"):
                    for tx in ptr.findall(".//Transaction"):
                        tx_date_str = tx.findtext("TransactionDate") or ""
                        try:
                            td = datetime.strptime(tx_date_str[:10], "%Y-%m-%d")
                        except Exception:
                            continue
                        if td < cutoff:
                            continue
                        age_days  = (datetime.utcnow() - td).days
                        recency_w = max(0.1, 1 - (age_days / 365))
                        tx_type   = (tx.findtext("Type") or "").upper()
                        type_w    = 1.5 if any(t in tx_type for t in ["PURCHASE", "BUY"]) else 1.0
                        conv      = parse_conviction(tx.findtext("Amount") or "")
                        scores[name] = scores.get(name, 0) + (recency_w * type_w * (1 + conv))

    except Exception as e:
        log.warning(f"Congress refresh failed: {e}")
        return cache["congress_targets"], [], []

    if not scores:
        return cache["congress_targets"], [], []

    ranked  = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    new_top = []
    for full_name, _ in ranked[:TOP_N]:
        parts     = full_name.split()
        last_name = parts[-1] if parts else full_name
        new_top.append({
            "name":      full_name,
            "last_name": last_name,
            "chamber":   "house",
        })

    old_names = {t["name"] for t in cache.get("congress_targets", DEFAULT_CONGRESS)}
    new_names = {t["name"] for t in new_top}
    added     = [t for t in new_top if t["name"] not in old_names]
    dropped   = [t for t in cache.get("congress_targets", []) if t["name"] not in new_names]

    log.info(f"Congress refresh: +{[a['name'] for a in added]} -{[d['name'] for d in dropped]}")
    return new_top, added, dropped


def refresh_fund_targets(cache: dict) -> tuple:
    log.info("Refreshing fund manager targets...")
    prompt = """List the 5 best-performing hedge fund portfolios via SEC 13F filings over the last 12 months.
Reply ONLY with a JSON array, no markdown, no extra text:
[{"name": "Manager / Fund", "cik": "10-digit-padded-CIK"}, ...]
Requirements: real CIK numbers, concentrated portfolios under 20 positions, must file 13F-HR."""
    result = gemini_call(prompt)
    if result:
        try:
            clean  = result.replace("```json", "").replace("```", "").strip()
            parsed = json.loads(clean)
            if isinstance(parsed, list) and len(parsed) >= 3:
                new_t     = parsed[:TOP_N]
                old_names = {t["name"] for t in cache.get("fund_targets", DEFAULT_FUNDS)}
                new_names = {t["name"] for t in new_t}
                added     = [t for t in new_t if t["name"] not in old_names]
                dropped   = [t for t in cache.get("fund_targets", []) if t["name"] not in new_names]
                return new_t, added, dropped
        except Exception as e:
            log.warning(f"Fund refresh parse failed: {e}")
    return cache.get("fund_targets", DEFAULT_FUNDS), [], []


# ═══════════════════════════════════════════════════════════════════════════════
# SIGNAL SCORING
# ═══════════════════════════════════════════════════════════════════════════════

def score_signal(ticker: str, trade_type: str, lag_days: int,
                 matching_congress: int, matching_funds: int) -> dict:
    score   = 0
    reasons = []
    t_upper = trade_type.upper()

    if any(t in t_upper for t in ["BUY", "PURCHASE", "CALL"]):
        score += 3
        reasons.append("Strong buy-type signal (+3)")
    elif any(t in t_upper for t in ["SELL", "PUT", "SALE"]):
        score -= 2
        reasons.append("Sell / put signal (−2)")
    else:
        score += 1
        reasons.append("Neutral trade type (+1)")

    if lag_days <= 7:
        score += 2
        reasons.append(f"Fresh — {lag_days}d lag (+2)")
    elif lag_days <= 20:
        score += 1
        reasons.append(f"Moderate lag — {lag_days}d (+1)")
    elif lag_days <= MAX_LAG_DAYS:
        score -= 1
        reasons.append(f"Getting stale — {lag_days}d (−1)")
    else:
        score -= 2
        reasons.append(f"Stale — {lag_days}d (−2)")

    if matching_congress >= 2:
        score += 2
        reasons.append(f"{matching_congress} congress members on same ticker (+2)")

    if matching_congress >= 1 and matching_funds >= 1:
        score += 3
        reasons.append("⚡ Cross-tier consensus: congress + fund manager (+3)")
    elif matching_funds >= 2:
        score += 2
        reasons.append(f"{matching_funds} fund managers on same ticker (+2)")

    score  = max(0, min(10, score))
    action = "WATCH"
    if score >= BUY_SCORE_THRESHOLD:
        action = "BUY"
    elif score <= 5 and any(t in t_upper for t in ["SELL", "PUT", "SALE"]):
        action = "SELL"

    return {"score": score, "action": action, "reasons": reasons}


# ═══════════════════════════════════════════════════════════════════════════════
# ALPACA PAPER TRADING
# ═══════════════════════════════════════════════════════════════════════════════

def get_open_positions() -> list:
    headers = {
        "APCA-API-KEY-ID":     ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }
    try:
        resp = requests.get(
            f"{ALPACA_BASE_URL}/v2/positions",
            headers=headers, timeout=10
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.error(f"Alpaca positions failed: {e}")
        return []


def place_paper_order(ticker: str, side: str, dollar_amount: float) -> dict:
    headers = {
        "APCA-API-KEY-ID":     ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
        "Content-Type":        "application/json",
    }
    order = {
        "symbol":        ticker,
        "notional":      str(round(dollar_amount, 2)),
        "side":          side,
        "type":          "market",
        "time_in_force": "day",
    }
    try:
        resp = requests.post(
            f"{ALPACA_BASE_URL}/v2/orders",
            headers=headers, json=order, timeout=15
        )
        resp.raise_for_status()
        result = resp.json()
        log.info(f"Paper {side.upper()}: ${dollar_amount:.2f} of {ticker}")
        return result
    except Exception as e:
        log.error(f"Alpaca order failed for {ticker}: {e}")
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════════
# DASHBOARD DATA — write JSON for Claude artifact
# ═══════════════════════════════════════════════════════════════════════════════

def build_dashboard_data(cache: dict, positions: list) -> dict:
    """
    Build the full dashboard_data.json payload.
    Claude artifact fetches this and renders the dashboard.
    """
    day_num   = days_into_test(cache)
    total_val = sum(float(p.get("market_value", 0)) for p in positions)
    total_pl  = sum(float(p.get("unrealized_pl", 0)) for p in positions)
    total_dep = cache.get("total_deposited", 0)

    # Benchmark returns
    qqq_start   = cache.get("benchmark_start", {}).get("QQQ")
    qqq_current = cache.get("benchmark_current", {}).get("QQQ")
    voo_start   = cache.get("benchmark_start", {}).get("VOO")
    voo_current = cache.get("benchmark_current", {}).get("VOO")

    qqq_return = ((qqq_current - qqq_start) / qqq_start * 100) if qqq_start and qqq_current else None
    voo_return = ((voo_current - voo_start) / voo_start * 100) if voo_start and voo_current else None
    our_return = ((total_val - total_dep) / total_dep * 100) if total_dep > 0 else None

    # Enrich positions with entry data from cache
    enriched_positions = []
    for p in positions:
        sym      = p.get("symbol", "")
        pos_data = cache.get("positions", {}).get(sym, {})
        enriched_positions.append({
            "symbol":         sym,
            "market_value":   float(p.get("market_value", 0)),
            "unrealized_pl":  float(p.get("unrealized_pl", 0)),
            "unrealized_plpc":float(p.get("unrealized_plpc", 0)) * 100,
            "qty":            float(p.get("qty", 0)),
            "avg_entry":      float(p.get("avg_entry_price", 0)),
            "amount_invested":pos_data.get("amount_invested", 0),
            "conviction_pct": pos_data.get("conviction_pct", 0),
            "entry_date":     pos_data.get("entry_date", ""),
        })

    return {
        "generated_at":   datetime.utcnow().isoformat(),
        "test_day":       day_num,
        "test_total_days":TEST_PERIOD_DAYS,
        "test_ends":      (datetime.utcnow() + timedelta(days=TEST_PERIOD_DAYS - day_num)).strftime("%B %d, %Y"),

        # Capital summary
        "cash_balance":   round(cache.get("cash_balance", 0), 2),
        "total_deposited":round(total_dep, 2),
        "total_invested": round(cache.get("total_invested", 0), 2),
        "portfolio_value":round(total_val, 2),
        "unrealized_pl":  round(total_pl, 2),
        "next_deposit":   next_deposit_date(cache),

        # Returns
        "our_return_pct": round(our_return, 2) if our_return is not None else None,
        "qqq_return_pct": round(qqq_return, 2) if qqq_return is not None else None,
        "voo_return_pct": round(voo_return, 2) if voo_return is not None else None,
        "qqq_price_start":qqq_start,
        "voo_price_start":voo_start,

        # Positions
        "positions": enriched_positions,

        # Signal log (last 50 signals for feed)
        "signal_log": cache.get("signal_log", [])[-50:],

        # Trade history (all paper trades)
        "trade_history": cache.get("trade_history", []),

        # Missed signals
        "missed_signals": cache.get("missed_signals", [])[-20:],

        # Watchlist
        "congress_targets": cache.get("congress_targets", DEFAULT_CONGRESS),
        "fund_targets":     cache.get("fund_targets", DEFAULT_FUNDS),
        "last_congress_refresh": cache.get("last_congress_refresh"),
        "last_fund_refresh":     cache.get("last_fund_refresh"),
        "congress_refresh_in": max(0, CONGRESS_REFRESH_DAYS - days_since_iso(cache.get("last_congress_refresh"))),
        "fund_refresh_in":     max(0, FUND_REFRESH_DAYS - days_since_iso(cache.get("last_fund_refresh"))),

        # Day 60 verdict if available
        "day60_verdict": cache.get("day60_verdict"),
    }


def commit_dashboard(data: dict):
    """
    Write dashboard_data.json and commit it to the GitHub repo
    so the Claude artifact can fetch it via raw.githubusercontent.com.
    """
    with open(DASHBOARD_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)
    log.info(f"Dashboard data written to {DASHBOARD_FILE}")

    # Commit using git (GitHub Actions has git pre-configured)
    try:
        subprocess.run(["git", "config", "user.email", "monitor@github.com"], check=True)
        subprocess.run(["git", "config", "user.name",  "Portfolio Monitor"], check=True)
        subprocess.run(["git", "add", DASHBOARD_FILE], check=True)
        # Check if there's actually something to commit
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            capture_output=True
        )
        if result.returncode != 0:
            subprocess.run(
                ["git", "commit", "-m",
                 f"📊 Dashboard update — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"],
                check=True
            )
            subprocess.run(["git", "push"], check=True)
            log.info("Dashboard committed and pushed to GitHub")
        else:
            log.info("No dashboard changes to commit")
    except subprocess.CalledProcessError as e:
        log.error(f"Git commit failed: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# SIGNAL LOG HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def log_signal(cache: dict, signal_type: str, ticker: str, member_name: str,
               trade_type: str, score: int, conviction_pct: float,
               allocation: float, amount_str: str, analysis: str,
               order_id: str = "", reason: str = ""):
    """Append a signal event to the cache signal log for the dashboard."""
    cache.setdefault("signal_log", []).append({
        "timestamp":     datetime.utcnow().isoformat(),
        "type":          signal_type,   # BUY, SELL, WATCH, MISSED, DEPOSIT, REFRESH
        "ticker":        ticker,
        "source":        member_name,
        "trade_type":    trade_type,
        "score":         score,
        "conviction_pct":conviction_pct,
        "allocation":    allocation,
        "amount_range":  amount_str,
        "analysis":      analysis,
        "order_id":      order_id,
        "reason":        reason,
    })


def log_trade(cache: dict, ticker: str, side: str, amount: float,
              order_id: str, member_name: str, conviction_pct: float):
    """Append to trade history for the dashboard trade log."""
    cache.setdefault("trade_history", []).append({
        "timestamp":     datetime.utcnow().isoformat(),
        "ticker":        ticker,
        "side":          side,
        "amount":        amount,
        "order_id":      order_id,
        "source":        member_name,
        "conviction_pct":conviction_pct,
        "status":        "filled",
    })


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    log.info("=" * 60)
    log.info("Portfolio Monitor v3 — daily run")
    log.info(datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"))
    log.info("=" * 60)

    cache     = load_cache()
    positions = get_open_positions()
    signals   = 0
    congress_tickers: dict = {}   # ticker → [member names]

    # ── 1. Biweekly deposit ───────────────────────────────────────────────────
    if is_deposit_monday(cache):
        prev = cache.get("cash_balance", 0.0)
        cache["cash_balance"]      = round(prev + DEPOSIT_AMOUNT, 2)
        cache["total_deposited"]   = round(cache.get("total_deposited", 0) + DEPOSIT_AMOUNT, 2)
        cache["last_deposit_date"] = datetime.utcnow().isoformat()
        if not cache.get("first_deposit_date"):
            cache["first_deposit_date"] = datetime.utcnow().isoformat()
        log.info(f"Deposit +${DEPOSIT_AMOUNT:.2f} → cash = ${cache['cash_balance']:.2f}")

        # Capture benchmark prices on first deposit
        if not cache["benchmark_start"]["QQQ"]:
            cache["benchmark_start"]["QQQ"] = get_stock_price("QQQ")
            cache["benchmark_start"]["VOO"] = get_stock_price("VOO")
            log.info(f"Benchmarks: QQQ={cache['benchmark_start']['QQQ']}, VOO={cache['benchmark_start']['VOO']}")

        log_signal(cache, "DEPOSIT", "", "System", "Deposit",
                   0, 0, DEPOSIT_AMOUNT, "",
                   f"Biweekly deposit of ${DEPOSIT_AMOUNT:.2f}. "
                   f"Cash balance now ${cache['cash_balance']:.2f}. "
                   f"Next deposit: {next_deposit_date(cache)}")
        save_cache(cache)

    # Update current benchmark prices every run
    qqq_now = get_stock_price("QQQ")
    voo_now = get_stock_price("VOO")
    if qqq_now:
        cache["benchmark_current"]["QQQ"] = qqq_now
    if voo_now:
        cache["benchmark_current"]["VOO"] = voo_now

    # ── 2. Target refresh ─────────────────────────────────────────────────────
    if days_since_iso(cache.get("last_congress_refresh")) >= CONGRESS_REFRESH_DAYS:
        new_c, added, dropped = refresh_congress_targets(cache)
        cache["congress_targets"]      = new_c
        cache["last_congress_refresh"] = datetime.utcnow().isoformat()
        change_note = ""
        if added or dropped:
            change_note = (f"Added: {[a['name'] for a in added]}. "
                           f"Removed: {[d['name'] for d in dropped]}.")
        log_signal(cache, "REFRESH", "", "System", "Congress Refresh",
                   0, 0, 0, "", f"Congressional watchlist refreshed. {change_note}")
        save_cache(cache)

    if days_since_iso(cache.get("last_fund_refresh")) >= FUND_REFRESH_DAYS:
        new_f, added, dropped = refresh_fund_targets(cache)
        cache["fund_targets"]      = new_f
        cache["last_fund_refresh"] = datetime.utcnow().isoformat()
        change_note = ""
        if added or dropped:
            change_note = (f"Added: {[a['name'] for a in added]}. "
                           f"Removed: {[d['name'] for d in dropped]}.")
        log_signal(cache, "REFRESH", "", "System", "Fund Refresh",
                   0, 0, 0, "", f"Fund manager watchlist refreshed. {change_note}")
        save_cache(cache)

    congress_targets = cache.get("congress_targets", DEFAULT_CONGRESS)
    fund_targets     = cache.get("fund_targets", DEFAULT_FUNDS)

    # ── 3. Congressional disclosures ──────────────────────────────────────────
    for member in congress_targets:
        log.info(f"Checking: {member['name']}")
        trades = fetch_congressional_trades(member)
        time.sleep(1)

        for trade in trades:
            ticker     = trade.get("ticker", "").upper().strip()
            trade_type = trade.get("type", "unknown")
            trade_date = trade.get("trade_date", "")
            disclosed  = trade.get("filed_date", "")
            amount_str = trade.get("amount", "")

            # Build a stable trade ID
            trade_id = f"{member['name']}-{ticker}-{trade_date}-{trade_type}"

            if trade_id in cache.get("seen", []):
                continue

            # Skip pending/non-stock rows (Senate PDFs)
            if ticker == "PENDING" or not ticker or not ticker.isalpha() or len(ticker) > 5:
                ticker = extract_ticker(json.dumps(trade)[:400])
            if not ticker or ticker == "UNKNOWN":
                cache["seen"].append(trade_id)
                continue

            lag = days_since(trade_date)
            congress_tickers.setdefault(ticker, [])
            congress_tickers[ticker].append(member["name"])

            if lag > MAX_LAG_DAYS:
                log.info(f"  Stale ({lag}d): {ticker}")
                cache["seen"].append(trade_id)
                continue

            log.info(f"  New: {member['name']} → {trade_type} {ticker} ({amount_str}) lag={lag}d")

            score_result = score_signal(
                ticker, trade_type, lag,
                matching_congress=len(congress_tickers[ticker]),
                matching_funds=0,
            )
            log.info(f"  Score: {score_result['score']}/10 → {score_result['action']}")

            if score_result["action"] == "BUY":
                conviction_pct = parse_conviction(amount_str)
                cash_before    = cache.get("cash_balance", 0.0)
                allocation     = calculate_allocation(conviction_pct, cash_before)

                if allocation == 0.0:
                    needed = round(cash_before * conviction_pct, 2)
                    reason = "Below $25 minimum" if cash_before > 0 else "No cash available"
                    log.info(f"  MISSED: {ticker} — {reason}")
                    cache.setdefault("missed_signals", []).append({
                        "date": datetime.utcnow().isoformat(),
                        "ticker": ticker, "reason": reason,
                        "score": score_result["score"],
                        "needed": max(needed, MIN_POSITION_USD),
                    })
                    log_signal(cache, "MISSED", ticker, member["name"], trade_type,
                               score_result["score"], conviction_pct, 0, amount_str,
                               reason, reason=reason)
                else:
                    cash_after = round(cash_before - allocation, 2)
                    summary    = (
                        f"Source: {member['name']}\nTicker: {ticker}\n"
                        f"Trade type: {trade_type}\nRange: {amount_str}\n"
                        f"Conviction: {int(conviction_pct*100)}%\n"
                        f"Trade date: {trade_date}\nLag: {lag} days\n"
                        f"Other members on {ticker}: {', '.join(congress_tickers[ticker])}"
                    )
                    analysis = get_ai_analysis(summary, score_result, allocation, cash_after)
                    order    = place_paper_order(ticker, "buy", allocation)

                    if "id" in order:
                        cache["cash_balance"]   = cash_after
                        cache["total_invested"] = round(cache.get("total_invested", 0) + allocation, 2)
                        cache.setdefault("positions", {})[ticker] = {
                            "amount_invested": allocation,
                            "conviction_pct":  conviction_pct,
                            "entry_date":      datetime.utcnow().isoformat(),
                        }
                        log_trade(cache, ticker, "buy", allocation,
                                  order.get("id", ""), member["name"], conviction_pct)

                    log_signal(cache, "BUY", ticker, member["name"], trade_type,
                               score_result["score"], conviction_pct, allocation,
                               amount_str, analysis, order.get("id", ""))
                    signals += 1

            elif score_result["action"] == "SELL":
                held = [p for p in positions if p.get("symbol") == ticker]
                if held:
                    val        = float(held[0].get("market_value", 0))
                    order      = place_paper_order(ticker, "sell", val)
                    cash_after = round(cache.get("cash_balance", 0) + val, 2)
                    if "id" in order:
                        cache["cash_balance"] = cash_after
                        cache.get("positions", {}).pop(ticker, None)
                        log_trade(cache, ticker, "sell", val,
                                  order.get("id", ""), member["name"], 0)
                    analysis = get_ai_analysis(
                        f"SELL: {member['name']} sold {ticker}. We hold ${val:.2f}.",
                        score_result, val, cash_after
                    )
                    log_signal(cache, "SELL", ticker, member["name"], trade_type,
                               score_result["score"], 0, val, amount_str, analysis,
                               order.get("id", ""))
                    signals += 1
            else:
                # WATCH — log it for the dashboard feed
                log_signal(cache, "WATCH", ticker, member["name"], trade_type,
                           score_result["score"], parse_conviction(amount_str), 0,
                           amount_str, f"Score {score_result['score']}/10 — below buy threshold.")

            cache["seen"].append(trade_id)
            save_cache(cache)

    # ── 4. Fund manager 13F checks ────────────────────────────────────────────
    for fund in fund_targets:
        log.info(f"Checking 13F: {fund['name']}")
        filings = fetch_fund_manager_filing(fund["cik"])
        time.sleep(1)

        for filing in filings:
            filing_id = filing.get("acc_number", "")
            if filing_id in cache.get("seen", []):
                continue

            log.info(f"  New 13F: {fund['name']} filed {filing.get('filed_date')}")

            if congress_tickers:
                overlap_result = gemini_call(
                    f"Which ONE of these tickers most likely appears in "
                    f"{fund['name']}'s portfolio: {', '.join(congress_tickers.keys())}? "
                    f"Reply with one ticker only or NONE."
                )
                overlap = overlap_result.strip().upper().split()[0] if overlap_result else "NONE"

                if overlap not in ("NONE", "UNKNOWN") and overlap in congress_tickers:
                    log.info(f"  Cross-tier: {overlap}")
                    score_result = score_signal(
                        overlap, "BUY", 45,
                        matching_congress=len(congress_tickers[overlap]),
                        matching_funds=1,
                    )
                    if score_result["action"] == "BUY":
                        conviction_pct = 0.40
                        cash_before    = cache.get("cash_balance", 0.0)
                        allocation     = calculate_allocation(conviction_pct, cash_before)
                        if allocation > 0:
                            cash_after = round(cash_before - allocation, 2)
                            summary    = (
                                f"Cross-tier: {fund['name']} 13F overlaps "
                                f"with congressional buys on {overlap}."
                            )
                            analysis = get_ai_analysis(summary, score_result, allocation, cash_after)
                            order    = place_paper_order(overlap, "buy", allocation)
                            if "id" in order:
                                cache["cash_balance"] = cash_after
                                cache["total_invested"] = round(
                                    cache.get("total_invested", 0) + allocation, 2
                                )
                                log_trade(cache, overlap, "buy", allocation,
                                          order.get("id", ""), fund["name"], conviction_pct)
                            log_signal(cache, "BUY", overlap, fund["name"],
                                       "13F Cross-tier", score_result["score"],
                                       conviction_pct, allocation, "13F filing",
                                       analysis, order.get("id", ""))
                            signals += 1

            cache["seen"].append(filing_id)
            save_cache(cache)

    # ── 5. Day 60 verdict ─────────────────────────────────────────────────────
    day_num = days_into_test(cache)
    if day_num >= TEST_PERIOD_DAYS and not cache.get("day60_verdict"):
        positions = get_open_positions()
        log.info("Day 60 — generating final verdict")
        verdict = get_day60_verdict(cache, positions)
        cache["day60_verdict"] = verdict
        log_signal(cache, "DAY60", "", "System", "Final Verdict",
                   0, 0, 0, "", verdict)
        save_cache(cache)

    # ── 6. Build and commit dashboard ─────────────────────────────────────────
    positions      = get_open_positions()
    dashboard_data = build_dashboard_data(cache, positions)
    commit_dashboard(dashboard_data)

    save_cache(cache)
    log.info(f"Done — {signals} signal(s) fired today")


if __name__ == "__main__":
    main()
