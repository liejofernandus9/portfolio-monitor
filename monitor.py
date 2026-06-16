"""
Portfolio Monitor v4
====================
Signal sources:
  - SEC EDGAR 13F    → fund manager holdings (quarterly, free)
  - Finnhub Form 4   → insider transactions (daily, free tier)

Watchlist:
  - Built from current 13F holdings of 5 fund managers
  - Refreshed every 28 days when new 13F filings detected
  - Tickers held by 2+ managers flagged as high-conviction

Scoring:
  - Insider buy on watchlist ticker     → base signal
  - C-suite / Chairman buy              → conviction bonus
  - Ticker in 2+ fund portfolios        → consensus bonus
  - New 13F position by fund manager    → fund signal
  - Both insider + fund signal on same ticker → cross-tier (strongest)

Capital:
  - $250 biweekly deposit every other Monday
  - Percentage-based allocation (conviction → % of cash)
  - 60% single-position cap · $25 minimum · cash guard
  - Proceeds from sells returned to cash immediately

Dashboard:
  - dashboard_data.json committed to GitHub via API after every run
  - Claude artifact fetches and renders it
"""

import os
import io
import json
import time
import base64
import zipfile
import logging
import requests
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
FINNHUB_API_KEY    = os.environ.get("FINNHUB_API_KEY", "")
GITHUB_TOKEN       = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO        = "liejofernandus9/portfolio-monitor"

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
)

# ── Config ────────────────────────────────────────────────────────────────────
DEPOSIT_AMOUNT        = 250.00
DEPOSIT_INTERVAL_DAYS = 14
MAX_POSITION_PCT      = 0.60     # max 60% of cash in one trade
MIN_POSITION_USD      = 25.00    # skip allocations below this
BUY_SCORE_THRESHOLD   = 6
FUND_REFRESH_DAYS     = 28       # rebuild watchlist every 28 days
TEST_PERIOD_DAYS      = 60
MAX_WATCHLIST_SIZE    = 40       # cap ticker watchlist at 40

# Insider transaction codes that signal buying
BUY_CODES  = {"P", "A"}         # Purchase, Award
SELL_CODES = {"S", "D"}         # Sale, Disposition

# C-suite roles that carry higher weight
CSUITE_ROLES = {
    "CEO", "CFO", "COO", "CTO", "CHAIRMAN", "PRESIDENT",
    "CHIEF EXECUTIVE", "CHIEF FINANCIAL", "CHIEF OPERATING"
}

# Conviction % from insider dollar amount
CONVICTION_MAP = [
    (5_000_000,  1.00),
    (1_000_000,  0.85),
    (500_000,    0.75),
    (250_000,    0.60),
    (100_000,    0.40),
    (50_000,     0.30),
    (15_000,     0.20),
    (1_000,      0.10),
]

# ── Parking strategy config ──────────────────────────────────────────────────
# When no signal fires, idle cash is parked in QQQ/VOO to stay invested
PARK_TICKER           = "QQQ"    # default parking ticker
PARK_THRESHOLD_HIGH   = 200.00   # park 70% if cash above this
PARK_THRESHOLD_MID    = 50.00    # park 50% if cash in this range
PARK_PCT_HIGH         = 0.70     # fraction to park when cash > $200
PARK_PCT_MID          = 0.50     # fraction to park when cash $50-$200
PARK_LABEL            = "PARKED" # position type label in dashboard

# ── Fund manager targets ──────────────────────────────────────────────────────
FUND_TARGETS = [
    {"name": "Bill Ackman / Pershing Square", "cik": "0001336528"},
    {"name": "Stan Druckenmiller / Duquesne", "cik": "0001536411"},
    {"name": "Warren Buffett / Berkshire",    "cik": "0001067983"},
    {"name": "Philippe Laffont / Coatue",     "cik": "0001336920"},
    {"name": "Michael Burry / Scion",         "cik": "0001649339"},
]

# Seed watchlist from Q1 2026 13F data — refreshed dynamically every 28 days
SEED_WATCHLIST = {
    # Ackman / Pershing Square (Q1 2026)
    "BN": ["Ackman"], "AMZN": ["Ackman"], "UBER": ["Ackman"],
    "MSFT": ["Ackman"], "QSR": ["Ackman"], "HLT": ["Ackman"],
    "CP": ["Ackman"], "GOOGL": ["Ackman"],
    # Druckenmiller / Duquesne (Q1 2026)
    "NTRA": ["Druckenmiller"], "AVGO": ["Druckenmiller"],
    "TSM": ["Druckenmiller"], "INSM": ["Druckenmiller"],
    "CAI": ["Druckenmiller"], "YPF": ["Druckenmiller"],
    "EWZ": ["Druckenmiller"], "STM": ["Druckenmiller"],
    # Buffett / Berkshire (Q1 2026)
    "AAPL": ["Buffett"], "BAC": ["Buffett"], "AXP": ["Buffett"],
    "KO": ["Buffett"], "CVX": ["Buffett"], "OXY": ["Buffett"],
    "MCO": ["Buffett"], "GOOGL": ["Buffett"],
    # Laffont / Coatue (latest)
    "META": ["Laffont"], "NVDA": ["Laffont"], "GOOG": ["Laffont"],
    "ORCL": ["Laffont"], "PLTR": ["Laffont"], "AMZN": ["Laffont"],
    "CRM": ["Laffont"], "PANW": ["Laffont"],
    # Burry / Scion (Q3 2025 — most recent filed)
    "MOH": ["Burry"], "LULU": ["Burry"], "SLM": ["Burry"],
    "HCA": ["Burry"], "C": ["Burry"],
}

DASHBOARD_FILE = "dashboard_data.json"
CACHE_FILE     = "seen_trades.json"


# ═══════════════════════════════════════════════════════════════════════════════
# CACHE
# ═══════════════════════════════════════════════════════════════════════════════

def load_cache() -> dict:
    now      = datetime.utcnow().isoformat()
    defaults = {
        "seen":               [],
        "cash_balance":       0.00,
        "total_deposited":    0.00,
        "total_invested":     0.00,
        "last_deposit_date":  None,
        "first_deposit_date": None,
        "positions":          {},
        "missed_signals":     [],
        "signal_log":         [],
        "trade_history":      [],
        "watchlist":          _merge_seed_watchlist({}),
        "fund_holdings":      {},  # cik → list of tickers
        "last_fund_refresh":  None,
        "start_date":         now,
        "benchmark_start":    {"QQQ": None, "VOO": None},
        "benchmark_current":  {"QQQ": None, "VOO": None},
        "day60_verdict":      None,
    }
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE) as f:
            existing = json.load(f)
        for key, val in defaults.items():
            if key not in existing:
                existing[key] = val
        existing.setdefault("watchlist", defaults["watchlist"])
        existing.setdefault("fund_holdings", {})
        existing["benchmark_start"].setdefault("QQQ", None)
        existing["benchmark_start"].setdefault("VOO", None)
        existing.setdefault("benchmark_current", {"QQQ": None, "VOO": None})
        return existing
    return defaults


def save_cache(cache: dict):
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2, default=str)


def _merge_seed_watchlist(existing: dict) -> dict:
    """Merge seed watchlist into existing, preserving existing manager lists."""
    result = {}
    for ticker, managers in SEED_WATCHLIST.items():
        result[ticker] = list(set(existing.get(ticker, []) + managers))
    for ticker, managers in existing.items():
        if ticker not in result:
            result[ticker] = managers
    return result


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
        today   = datetime.utcnow()
        days_to = (7 - today.weekday()) % 7 or 7
        return (today + timedelta(days=days_to)).strftime("%B %d, %Y")
    try:
        return (datetime.fromisoformat(last) + timedelta(days=14)).strftime("%B %d, %Y")
    except Exception:
        return "Next Monday"


# ═══════════════════════════════════════════════════════════════════════════════
# ALLOCATION
# ═══════════════════════════════════════════════════════════════════════════════

def parse_conviction_from_amount(dollar_amount: float) -> float:
    for threshold, pct in CONVICTION_MAP:
        if dollar_amount >= threshold:
            return pct
    return 0.10


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

def gemini_call(prompt: str, retries: int = 3) -> str:
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 700, "temperature": 0.3},
    }
    for attempt in range(retries):
        try:
            resp = requests.post(GEMINI_URL, json=body, timeout=30)
            if resp.status_code == 429:
                wait = 15 * (attempt + 1)
                log.warning(f"Gemini rate limited — waiting {wait}s")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        except Exception as e:
            log.warning(f"Gemini attempt {attempt+1} failed: {e}")
            if attempt < retries - 1:
                time.sleep(5)
    return ""


def get_ai_analysis(signal_summary: str, score: int, reasons: list,
                    allocation: float, cash_after: float) -> str:
    prompt = f"""You are a concise investment analyst. A portfolio monitor detected:

{signal_summary}

SIGNAL SCORE: {score}/10
REASONS: {', '.join(reasons)}
AMOUNT TO DEPLOY: ${allocation:.2f}
CASH REMAINING AFTER: ${cash_after:.2f}

Write exactly 3 short paragraphs:
1. What this signal means strategically — why is the insider buying now?
2. What a retail investor deploying ${allocation:.2f} should specifically do
3. Key risks: position size, market context, signal lag

Direct and specific. No disclaimers. No preamble."""
    result = gemini_call(prompt)
    return result if result else "AI analysis unavailable."


def get_day60_verdict(cache: dict, positions: list) -> str:
    total_val = sum(float(p.get("market_value", 0)) for p in positions)
    total_dep = cache.get("total_deposited", 0)
    ret_pct   = ((total_val - total_dep) / total_dep * 100) if total_dep else 0
    prompt = f"""A 60-day paper trading test completed using insider transactions
and fund manager 13F signals.

RESULTS:
- Total deposited: ${total_dep:.2f}
- Portfolio value: ${total_val:.2f}
- Return: {ret_pct:+.1f}%
- Signals fired: {len(cache.get('signal_log', []))}
- Missed (no cash): {len(cache.get('missed_signals', []))}

Write 3 paragraphs:
1. Performance vs QQQ (~5%) and VOO (~4%) over 60 days
2. Clear recommendation: deploy real $250 biweekly, adjust strategy, or redirect to QQQ
3. What to watch in the next 60 days if going live

Be direct. Give a real recommendation."""
    return gemini_call(prompt) or "Verdict unavailable."


# ═══════════════════════════════════════════════════════════════════════════════
# SEC EDGAR — 13F HOLDINGS PARSER
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_13f_holdings(cik: str) -> list[str]:
    """
    Fetch the actual ticker list from the most recent 13F-HR filing via SEC EDGAR.
    Returns list of ticker symbols held by this manager.
    """
    headers = {"User-Agent": "PortfolioMonitor research@example.com"}

    # Step 1: Get most recent 13F accession number
    sub_url  = f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json"
    try:
        resp = requests.get(sub_url, headers=headers, timeout=15)
        resp.raise_for_status()
        data    = resp.json()
        forms   = data.get("filings", {}).get("recent", {}).get("form", [])
        dates   = data.get("filings", {}).get("recent", {}).get("filingDate", [])
        acc_nos = data.get("filings", {}).get("recent", {}).get("accessionNumber", [])

        acc_no = None
        for form, date, acc in zip(forms, dates, acc_nos):
            if form == "13F-HR":
                acc_no = acc
                break

        if not acc_no:
            return []

    except Exception as e:
        log.warning(f"SEC EDGAR submissions failed for CIK {cik}: {e}")
        return []

    # Step 2: Fetch the actual 13F XML filing to get holdings
    acc_clean = acc_no.replace("-", "")
    index_url = (
        f"https://www.sec.gov/Archives/edgar/full-index/"
        f"cgi-bin/browse-edgar?action=getcompany&CIK={cik}"
        f"&type=13F-HR&dateb=&owner=include&count=1&search_text="
    )

    # Use the direct filing index
    filing_url = (
        f"https://www.sec.gov/Archives/edgar/data/"
        f"{int(cik)}/{acc_clean}/"
    )

    try:
        idx_resp = requests.get(
            f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json",
            headers=headers, timeout=15
        )
        # Try to find the infotable XML in the filing
        # Build URL to the primary document
        primary_url = (
            f"https://www.sec.gov/Archives/edgar/data/"
            f"{int(cik)}/{acc_clean}/{acc_clean}-index.htm"
        )
        idx = requests.get(primary_url, headers=headers, timeout=15)

        # Look for infotable XML link
        import re
        xml_match = re.search(
            r'href="(/Archives/edgar/data/[^"]+infotable[^"]*\.xml)"',
            idx.text, re.IGNORECASE
        )
        if not xml_match:
            # Try alternate pattern
            xml_match = re.search(
                r'href="(/Archives/edgar/data/[^"]+\.xml)"',
                idx.text, re.IGNORECASE
            )

        if xml_match:
            xml_url = "https://www.sec.gov" + xml_match.group(1)
            xml_resp = requests.get(xml_url, headers=headers, timeout=15)
            xml_resp.raise_for_status()

            # Parse the infotable XML for tickers
            root    = ET.fromstring(xml_resp.content)
            ns      = {"ns": root.tag.split("}")[0].strip("{") if "}" in root.tag else ""}
            tickers = []

            for info in root.findall(".//{*}infoTable"):
                ticker_el = info.find("{*}ticker") or info.find("ticker")
                if ticker_el is not None and ticker_el.text:
                    t = ticker_el.text.strip().upper()
                    if t and t.isalpha() and len(t) <= 5:
                        tickers.append(t)

            log.info(f"  Parsed {len(tickers)} tickers from 13F XML for CIK {cik}")
            return list(set(tickers))

    except Exception as e:
        log.warning(f"  13F XML parse failed for CIK {cik}: {e}")

    return []


def rebuild_watchlist(cache: dict) -> dict:
    """
    Rebuild the ticker watchlist from current 13F holdings of all 5 managers.
    Tickers held by multiple managers get higher priority.
    Called every 28 days.
    """
    log.info("Rebuilding watchlist from 13F holdings...")
    new_watchlist: dict[str, list] = {}

    for fund in FUND_TARGETS:
        cik         = fund["cik"]
        short_name  = fund["name"].split("/")[0].strip().split()[-1]  # e.g. "Ackman"
        tickers     = fetch_13f_holdings(cik)
        time.sleep(1)

        if tickers:
            cache["fund_holdings"][cik] = tickers
            log.info(f"  {short_name}: {len(tickers)} holdings → watchlist")
            for ticker in tickers:
                new_watchlist.setdefault(ticker, [])
                if short_name not in new_watchlist[ticker]:
                    new_watchlist[ticker].append(short_name)
        else:
            # Fall back to cached holdings if fresh fetch fails
            cached_tickers = cache.get("fund_holdings", {}).get(cik, [])
            log.info(f"  {short_name}: using {len(cached_tickers)} cached tickers")
            for ticker in cached_tickers:
                new_watchlist.setdefault(ticker, [])
                if short_name not in new_watchlist[ticker]:
                    new_watchlist[ticker].append(short_name)

    # If we got nothing (all fetches failed), preserve existing watchlist
    if not new_watchlist:
        log.warning("Watchlist rebuild failed — keeping existing watchlist")
        return cache.get("watchlist", _merge_seed_watchlist({}))

    # Always ensure seed tickers are included as baseline
    merged = _merge_seed_watchlist(new_watchlist)

    # Cap at MAX_WATCHLIST_SIZE, prioritising multi-manager tickers
    sorted_tickers = sorted(
        merged.items(),
        key=lambda x: len(x[1]),
        reverse=True
    )[:MAX_WATCHLIST_SIZE]

    final = dict(sorted_tickers)
    log.info(
        f"Watchlist rebuilt: {len(final)} tickers "
        f"({sum(1 for v in final.values() if len(v) >= 2)} held by 2+ managers)"
    )
    return final


# ═══════════════════════════════════════════════════════════════════════════════
# SEC EDGAR — NEW 13F FILING CHECK
# ═══════════════════════════════════════════════════════════════════════════════

def check_new_13f_filing(cik: str) -> dict | None:
    """Return filing info if there's a new 13F-HR not yet seen in cache."""
    headers = {"User-Agent": "PortfolioMonitor research@example.com"}
    try:
        resp    = requests.get(
            f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json",
            headers=headers, timeout=15
        )
        resp.raise_for_status()
        data    = resp.json()
        forms   = data.get("filings", {}).get("recent", {}).get("form", [])
        dates   = data.get("filings", {}).get("recent", {}).get("filingDate", [])
        acc_nos = data.get("filings", {}).get("recent", {}).get("accessionNumber", [])
        for form, date, acc in zip(forms, dates, acc_nos):
            if form == "13F-HR":
                return {"filed_date": date, "acc_number": acc, "cik": cik}
        return None
    except Exception as e:
        log.warning(f"13F check failed for CIK {cik}: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# FINNHUB — INSIDER TRANSACTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_insider_transactions(ticker: str, from_date: str) -> list:
    """
    Fetch Form 4 insider transactions for a ticker from Finnhub free tier.
    Free tier: per-symbol lookups, up to 60 calls/min.
    """
    if not FINNHUB_API_KEY:
        return []

    try:
        resp = requests.get(
            "https://finnhub.io/api/v1/stock/insider-transactions",
            params={"symbol": ticker, "from": from_date},
            headers={"X-Finnhub-Token": FINNHUB_API_KEY},
            timeout=10
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", [])
    except Exception as e:
        log.warning(f"Finnhub insider fetch failed for {ticker}: {e}")
        return []


# ═══════════════════════════════════════════════════════════════════════════════
# SIGNAL SCORING
# ═══════════════════════════════════════════════════════════════════════════════

def score_insider_signal(ticker: str, tx: dict,
                         watchlist: dict, fund_signals: set) -> dict:
    """
    Score a single insider transaction 0–10.

    Scoring:
      +3  buy transaction code (P=purchase, A=award)
      +2  C-suite / Chairman buyer
      +1  ticker held by 2+ fund managers
      +2  ticker held by 3+ fund managers
      +3  fund manager also bought this ticker this cycle (cross-tier)
      -2  sell transaction code
      -1  small transaction (<$15K)
    """
    score   = 0
    reasons = []

    tx_code = str(tx.get("transactionCode", "")).upper().strip()
    name    = str(tx.get("name", "")).upper()
    role    = str(tx.get("officerTitle", "")).upper()
    shares  = abs(float(tx.get("change", 0) or 0))
    price   = float(tx.get("transactionPrice", 0) or 0)
    amount  = shares * price

    # Transaction type
    if tx_code in BUY_CODES:
        score += 3
        reasons.append(f"Insider buy ({tx_code}) (+3)")
    elif tx_code in SELL_CODES:
        score -= 2
        reasons.append(f"Insider sell ({tx_code}) (−2)")
    else:
        score += 1
        reasons.append(f"Neutral transaction ({tx_code}) (+1)")

    # C-suite bonus
    if any(r in role for r in CSUITE_ROLES):
        score += 2
        reasons.append(f"C-suite buyer: {role.title()} (+2)")

    # Fund manager consensus
    managers = watchlist.get(ticker, [])
    if len(managers) >= 3:
        score += 2
        reasons.append(f"Held by {len(managers)} fund managers (+2)")
    elif len(managers) >= 2:
        score += 1
        reasons.append(f"Held by {len(managers)} fund managers (+1)")

    # Cross-tier: fund manager AND insider both buying
    if ticker in fund_signals:
        score += 3
        reasons.append("⚡ Cross-tier: fund manager + insider both buying (+3)")

    # Small transaction penalty
    if amount > 0 and amount < 15_000:
        score -= 1
        reasons.append(f"Small transaction (${amount:,.0f}) (−1)")

    score  = max(0, min(10, score))
    action = "WATCH"
    if score >= BUY_SCORE_THRESHOLD and tx_code in BUY_CODES:
        action = "BUY"
    elif score <= 5 and tx_code in SELL_CODES:
        action = "SELL"

    return {
        "score":   score,
        "action":  action,
        "reasons": reasons,
        "amount":  amount,
        "role":    role.title(),
        "name":    tx.get("name", ""),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# ALPACA
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
# SIGNAL LOGGING
# ═══════════════════════════════════════════════════════════════════════════════

def log_signal(cache: dict, signal_type: str, ticker: str, source: str,
               score: int, conviction_pct: float, allocation: float,
               analysis: str, order_id: str = "", reason: str = "",
               role: str = "", insider_name: str = ""):
    cache.setdefault("signal_log", []).append({
        "timestamp":     datetime.utcnow().isoformat(),
        "type":          signal_type,
        "ticker":        ticker,
        "source":        source,
        "score":         score,
        "conviction_pct":conviction_pct,
        "allocation":    allocation,
        "analysis":      analysis,
        "order_id":      order_id,
        "reason":        reason,
        "role":          role,
        "insider_name":  insider_name,
    })


def log_trade(cache: dict, ticker: str, side: str, amount: float,
              order_id: str, source: str, conviction_pct: float):
    cache.setdefault("trade_history", []).append({
        "timestamp":      datetime.utcnow().isoformat(),
        "ticker":         ticker,
        "side":           side,
        "amount":         amount,
        "order_id":       order_id,
        "source":         source,
        "conviction_pct": conviction_pct,
    })


# ═══════════════════════════════════════════════════════════════════════════════
# DASHBOARD
# ═══════════════════════════════════════════════════════════════════════════════

def build_dashboard_data(cache: dict, positions: list) -> dict:
    day_num   = days_into_test(cache)
    total_val = sum(float(p.get("market_value", 0)) for p in positions)
    total_pl  = sum(float(p.get("unrealized_pl", 0)) for p in positions)
    total_dep = cache.get("total_deposited", 0)

    qqq_start   = cache["benchmark_start"].get("QQQ")
    qqq_current = cache["benchmark_current"].get("QQQ")
    voo_start   = cache["benchmark_start"].get("VOO")
    voo_current = cache["benchmark_current"].get("VOO")

    qqq_ret = ((qqq_current - qqq_start) / qqq_start * 100) if qqq_start and qqq_current else None
    voo_ret = ((voo_current - voo_start) / voo_start * 100) if voo_start and voo_current else None
    our_ret = ((total_val - total_dep) / total_dep * 100) if total_dep > 0 else None

    enriched = []
    for p in positions:
        sym      = p.get("symbol", "")
        pos_data = cache.get("positions", {}).get(sym, {})
        enriched.append({
            "symbol":          sym,
            "market_value":    float(p.get("market_value", 0)),
            "unrealized_pl":   float(p.get("unrealized_pl", 0)),
            "unrealized_plpc": float(p.get("unrealized_plpc", 0)) * 100,
            "avg_entry":       float(p.get("avg_entry_price", 0)),
            "amount_invested": pos_data.get("amount_invested", 0),
            "conviction_pct":  pos_data.get("conviction_pct", 0),
            "entry_date":      pos_data.get("entry_date", ""),
            "fund_managers":   cache.get("watchlist", {}).get(sym, []),
            "parked":          pos_data.get("parked", False),
        })

    # Watchlist summary — multi-manager tickers highlighted
    watchlist_summary = [
        {
            "ticker":    ticker,
            "managers":  managers,
            "consensus": len(managers) >= 2,
        }
        for ticker, managers in sorted(
            cache.get("watchlist", {}).items(),
            key=lambda x: len(x[1]), reverse=True
        )
    ]

    return {
        "generated_at":      datetime.utcnow().isoformat(),
        "test_day":          day_num,
        "test_total_days":   TEST_PERIOD_DAYS,
        "test_ends":         (datetime.utcnow() + timedelta(
                                days=TEST_PERIOD_DAYS - day_num
                             )).strftime("%B %d, %Y"),
        "cash_balance":      round(cache.get("cash_balance", 0), 2),
        "total_deposited":   round(total_dep, 2),
        "total_invested":    round(cache.get("total_invested", 0), 2),
        "portfolio_value":   round(total_val, 2),
        "unrealized_pl":     round(total_pl, 2),
        "next_deposit":      next_deposit_date(cache),
        "our_return_pct":    round(our_ret, 2) if our_ret is not None else None,
        "qqq_return_pct":    round(qqq_ret, 2) if qqq_ret is not None else None,
        "voo_return_pct":    round(voo_ret, 2) if voo_ret is not None else None,
        "positions":         enriched,
        "signal_log":        cache.get("signal_log", [])[-50:],
        "trade_history":     cache.get("trade_history", []),
        "missed_signals":    cache.get("missed_signals", [])[-20:],
        "watchlist":         watchlist_summary,
        "watchlist_size":    len(cache.get("watchlist", {})),
        "multi_manager_count": sum(
            1 for v in cache.get("watchlist", {}).values() if len(v) >= 2
        ),
        "fund_targets":      FUND_TARGETS,
        "last_fund_refresh": cache.get("last_fund_refresh"),
        "fund_refresh_in":   max(0, FUND_REFRESH_DAYS - days_since_iso(
                                 cache.get("last_fund_refresh")
                             )),
        "day60_verdict":     cache.get("day60_verdict"),
    }


def commit_dashboard(data: dict):
    with open(DASHBOARD_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)

    if not GITHUB_TOKEN:
        log.warning("No GITHUB_TOKEN — dashboard not committed")
        return

    try:
        with open(DASHBOARD_FILE, "rb") as f:
            encoded = base64.b64encode(f.read()).decode("utf-8")

        api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{DASHBOARD_FILE}"
        headers = {
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept":        "application/vnd.github.v3+json",
        }
        get_resp = requests.get(api_url, headers=headers, timeout=10)
        sha      = get_resp.json().get("sha") if get_resp.status_code == 200 else None

        payload = {
            "message": f"📊 Dashboard — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
            "content": encoded,
        }
        if sha:
            payload["sha"] = sha

        put_resp = requests.put(api_url, headers=headers, json=payload, timeout=15)
        if put_resp.status_code in (200, 201):
            log.info("Dashboard committed via GitHub API")
        else:
            log.error(f"Dashboard commit failed: {put_resp.status_code}")
    except Exception as e:
        log.error(f"Dashboard commit error: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# PARKING STRATEGY HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def get_park_allocation(cash_balance: float) -> float:
    """
    Calculate how much idle cash to park in QQQ when no signal fires.
    Returns dollar amount to park, 0 if below minimum threshold.
    """
    if cash_balance > PARK_THRESHOLD_HIGH:
        return round(cash_balance * PARK_PCT_HIGH, 2)
    elif cash_balance > PARK_THRESHOLD_MID:
        return round(cash_balance * PARK_PCT_MID, 2)
    return 0.0


def is_parked_position(cache: dict, ticker: str) -> bool:
    """Returns True if a position was auto-parked (placeholder), not signal-driven."""
    return cache.get("positions", {}).get(ticker, {}).get("parked", False)


def liquidate_parking(cache: dict, positions: list) -> float:
    """
    Sell all parked positions to free cash for a real signal.
    Returns total dollar proceeds freed up.
    """
    total_freed = 0.0
    parked = [p for p in positions if is_parked_position(cache, p.get("symbol", ""))]
    for p in parked:
        ticker = p.get("symbol", "")
        val    = float(p.get("market_value", 0))
        if val < MIN_POSITION_USD:
            continue
        order = place_paper_order(ticker, "sell", val)
        if "id" in order:
            cache["cash_balance"] = round(cache.get("cash_balance", 0) + val, 2)
            cache.get("positions", {}).pop(ticker, None)
            total_freed += val
            log.info(f"  Liquidated parked {ticker}: ${val:.2f} returned to cash")
            log_trade(cache, ticker, "sell", val,
                      order.get("id", ""), "Parking liquidation", 0)
    return total_freed



# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    log.info("=" * 60)
    log.info("Portfolio Monitor v4 — Fund Manager + Insider Signals")
    log.info(datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"))
    log.info("=" * 60)

    cache     = load_cache()
    positions = get_open_positions()
    signals   = 0

    # ── 1. Biweekly deposit ───────────────────────────────────────────────────
    if is_deposit_monday(cache):
        prev = cache.get("cash_balance", 0.0)
        cache["cash_balance"]      = round(prev + DEPOSIT_AMOUNT, 2)
        cache["total_deposited"]   = round(cache.get("total_deposited", 0) + DEPOSIT_AMOUNT, 2)
        cache["last_deposit_date"] = datetime.utcnow().isoformat()
        if not cache.get("first_deposit_date"):
            cache["first_deposit_date"] = datetime.utcnow().isoformat()
        log.info(f"Deposit +${DEPOSIT_AMOUNT:.2f} → cash = ${cache['cash_balance']:.2f}")

        if not cache["benchmark_start"]["QQQ"]:
            cache["benchmark_start"]["QQQ"] = get_stock_price("QQQ")
            cache["benchmark_start"]["VOO"] = get_stock_price("VOO")
            log.info(f"Benchmarks: QQQ={cache['benchmark_start']['QQQ']}, "
                     f"VOO={cache['benchmark_start']['VOO']}")

        log_signal(cache, "DEPOSIT", "", "System", 0, 0, DEPOSIT_AMOUNT,
                   f"Biweekly deposit +${DEPOSIT_AMOUNT:.2f}. "
                   f"Cash = ${cache['cash_balance']:.2f}. "
                   f"Next deposit: {next_deposit_date(cache)}")
        save_cache(cache)

    # Update benchmark prices every run
    qqq_now = get_stock_price("QQQ")
    voo_now = get_stock_price("VOO")
    if qqq_now:
        cache["benchmark_current"]["QQQ"] = qqq_now
    if voo_now:
        cache["benchmark_current"]["VOO"] = voo_now

    # ── 2. Watchlist refresh (every 28 days) ──────────────────────────────────
    if days_since_iso(cache.get("last_fund_refresh")) >= FUND_REFRESH_DAYS:
        log.info("28-day cycle: refreshing watchlist from 13F holdings...")
        new_watchlist              = rebuild_watchlist(cache)
        cache["watchlist"]         = new_watchlist
        cache["last_fund_refresh"] = datetime.utcnow().isoformat()

        multi = sum(1 for v in new_watchlist.values() if len(v) >= 2)
        log_signal(
            cache, "REFRESH", "", "System", 0, 0, 0,
            f"Watchlist rebuilt from 13F filings: {len(new_watchlist)} tickers, "
            f"{multi} held by 2+ managers. "
            f"Next refresh in {FUND_REFRESH_DAYS} days."
        )
        save_cache(cache)
    else:
        days_left = FUND_REFRESH_DAYS - days_since_iso(cache.get("last_fund_refresh"))
        log.info(f"Watchlist refresh in {days_left} days — using cached watchlist "
                 f"({len(cache.get('watchlist', {}))} tickers)")

    watchlist = cache.get("watchlist", _merge_seed_watchlist({}))

    # ── 3. Check for new 13F filings → fund signals ──────────────────────────
    # Track which tickers have fresh fund manager buying this cycle
    fund_signals: set[str] = set()

    log.info("Checking for new 13F filings...")
    for fund in FUND_TARGETS:
        filing = check_new_13f_filing(fund["cik"])
        time.sleep(0.5)

        if not filing:
            continue

        filing_id = filing.get("acc_number", "")
        if filing_id in cache.get("seen", []):
            continue

        log.info(f"  New 13F: {fund['name']} filed {filing.get('filed_date')}")

        # Get their current holdings and flag those tickers as fund signals
        tickers = fetch_13f_holdings(fund["cik"])
        for t in tickers:
            if t in watchlist:
                fund_signals.add(t)

        # If this is genuinely new, update the watchlist immediately
        short_name = fund["name"].split("/")[0].strip().split()[-1]
        for t in tickers:
            watchlist.setdefault(t, [])
            if short_name not in watchlist[t]:
                watchlist[t].append(short_name)

        log.info(f"  {fund['name']}: {len(tickers)} holdings, "
                 f"{len(fund_signals)} overlap with watchlist")

        log_signal(
            cache, "13F", "", fund["name"], 0, 0, 0,
            f"New 13F filing by {fund['name']} ({filing.get('filed_date')}). "
            f"Holdings overlap with {len(fund_signals)} watchlist tickers. "
            f"Flagged for cross-tier scoring."
        )
        cache["seen"].append(filing_id)
        save_cache(cache)

    # ── 4. Insider transaction checks ─────────────────────────────────────────
    log.info(f"Checking insider transactions for {len(watchlist)} watchlist tickers...")

    from_date    = (datetime.utcnow() - timedelta(days=3)).strftime("%Y-%m-%d")
    calls_made   = 0
    max_calls    = 50   # stay well within 60/min free tier limit

    # Prioritise multi-manager tickers first
    sorted_watchlist = sorted(
        watchlist.items(),
        key=lambda x: len(x[1]),
        reverse=True
    )

    for ticker, managers in sorted_watchlist:
        if calls_made >= max_calls:
            log.info(f"Rate limit reached ({max_calls} calls) — stopping ticker loop")
            break

        transactions = fetch_insider_transactions(ticker, from_date)
        calls_made  += 1
        time.sleep(0.6)   # ~100 calls/min max, we do 50 → plenty of headroom

        for tx in transactions:
            tx_id = f"{ticker}-{tx.get('name','')}-{tx.get('transactionDate','')}-{tx.get('transactionCode','')}"
            if tx_id in cache.get("seen", []):
                continue

            tx_code = str(tx.get("transactionCode", "")).upper().strip()
            if tx_code not in BUY_CODES and tx_code not in SELL_CODES:
                cache["seen"].append(tx_id)
                continue

            score_result = score_insider_signal(ticker, tx, watchlist, fund_signals)
            log.info(
                f"  {ticker}: {tx.get('name','')} ({score_result['role']}) "
                f"code={tx_code} score={score_result['score']}/10 → {score_result['action']}"
            )

            if score_result["action"] == "BUY":
                conviction_pct = parse_conviction_from_amount(score_result["amount"])
                cash_before    = cache.get("cash_balance", 0.0)
                allocation     = calculate_allocation(conviction_pct, cash_before)

                if allocation == 0.0:
                    needed = round(cash_before * conviction_pct, 2)
                    reason = "Below $25 minimum" if cash_before > 0 else "No cash available"
                    log.info(f"  MISSED {ticker}: {reason}")
                    cache.setdefault("missed_signals", []).append({
                        "date":   datetime.utcnow().isoformat(),
                        "ticker": ticker,
                        "reason": reason,
                        "score":  score_result["score"],
                        "needed": max(needed, MIN_POSITION_USD),
                    })
                    log_signal(
                        cache, "MISSED", ticker,
                        f"{score_result['insider_name']} ({score_result['role']})",
                        score_result["score"], conviction_pct, 0,
                        reason, reason=reason,
                        role=score_result["role"],
                        insider_name=score_result["name"],
                    )
                else:
                    # Liquidate any parked positions to free up cash first
                    parked_proceeds = liquidate_parking(cache, positions)
                    if parked_proceeds > 0:
                        log.info(f"  Liquidated parking for ${parked_proceeds:.2f} to fund signal")
                        # Recalculate with freed cash
                        cash_before = cache.get("cash_balance", 0.0)
                        allocation  = calculate_allocation(conviction_pct, cash_before)

                    cash_after = round(cash_before - allocation, 2)
                    summary    = (
                        f"Ticker: {ticker}\n"
                        f"Insider: {tx.get('name','')} ({score_result['role']})\n"
                        f"Transaction: {tx_code} ({score_result['amount']:,.0f} USD)\n"
                        f"Date: {tx.get('transactionDate','')}\n"
                        f"Fund managers holding {ticker}: {', '.join(managers)}\n"
                        f"Fund signal active: {'Yes ⚡' if ticker in fund_signals else 'No'}"
                    )
                    analysis = get_ai_analysis(
                        summary, score_result["score"],
                        score_result["reasons"], allocation, cash_after
                    )
                    order = place_paper_order(ticker, "buy", allocation)

                    if "id" in order:
                        cache["cash_balance"]   = cash_after
                        cache["total_invested"] = round(
                            cache.get("total_invested", 0) + allocation, 2
                        )
                        cache.setdefault("positions", {})[ticker] = {
                            "amount_invested": allocation,
                            "conviction_pct":  conviction_pct,
                            "entry_date":      datetime.utcnow().isoformat(),
                        }
                        log_trade(cache, ticker, "buy", allocation,
                                  order.get("id", ""), score_result["name"],
                                  conviction_pct)

                    log_signal(
                        cache, "BUY", ticker,
                        f"{score_result['name']} ({score_result['role']})",
                        score_result["score"], conviction_pct, allocation,
                        analysis, order.get("id", ""),
                        role=score_result["role"],
                        insider_name=score_result["name"],
                    )
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
                                  order.get("id", ""), score_result["name"], 0)
                    analysis = get_ai_analysis(
                        f"SELL: {score_result['name']} ({score_result['role']}) "
                        f"sold {ticker}. We hold ${val:.2f}.",
                        score_result["score"], score_result["reasons"],
                        val, cash_after
                    )
                    log_signal(
                        cache, "SELL", ticker,
                        f"{score_result['name']} ({score_result['role']})",
                        score_result["score"], 0, val, analysis,
                        order.get("id", ""),
                        role=score_result["role"],
                        insider_name=score_result["name"],
                    )
                    signals += 1

            cache["seen"].append(tx_id)
            save_cache(cache)

    # ── 5. Park idle cash if no signals fired ────────────────────────────────
    # Refresh positions after any trades placed above
    positions = get_open_positions()

    if signals == 0:
        cash = cache.get("cash_balance", 0.0)
        park_amount = get_park_allocation(cash)

        # Check if we already have a parked position in QQQ
        already_parked = any(
            p.get("symbol") == PARK_TICKER and is_parked_position(cache, PARK_TICKER)
            for p in positions
        )

        if park_amount >= MIN_POSITION_USD and not already_parked:
            log.info(f"No signals today — parking ${park_amount:.2f} in {PARK_TICKER}")
            order = place_paper_order(PARK_TICKER, "buy", park_amount)

            if "id" in order:
                cache["cash_balance"]   = round(cash - park_amount, 2)
                cache["total_invested"] = round(
                    cache.get("total_invested", 0) + park_amount, 2
                )
                cache.setdefault("positions", {})[PARK_TICKER] = {
                    "amount_invested": park_amount,
                    "conviction_pct":  0,
                    "entry_date":      datetime.utcnow().isoformat(),
                    "parked":          True,   # flag as placeholder
                }
                log_trade(cache, PARK_TICKER, "buy", park_amount,
                          order.get("id",""), "Auto-park (no signal)", 0)
                log_signal(
                    cache, "PARKED", PARK_TICKER, "System", 0, 0, park_amount,
                    f"No qualifying signals today. Parked ${park_amount:.2f} "
                    f"in {PARK_TICKER} to keep cash working. "
                    f"${cache['cash_balance']:.2f} kept liquid for incoming signals. "
                    f"Position will be liquidated automatically when a real signal fires.",
                )
                log.info(f"  Parked ${park_amount:.2f} in {PARK_TICKER} | "
                         f"Cash remaining: ${cache['cash_balance']:.2f}")
        elif already_parked:
            log.info(f"Already have a parked position in {PARK_TICKER} — skipping")
        else:
            log.info(f"Cash ${cash:.2f} below park threshold — keeping fully liquid")

        save_cache(cache)

    elif signals > 0:
        # Real signal fired — liquidate any parked positions first next run
        # (they were already liquidated inline above during signal processing)
        pass

    # ── 6. Day 60 check ───────────────────────────────────────────────────────
    if days_into_test(cache) >= TEST_PERIOD_DAYS and not cache.get("day60_verdict"):
        positions = get_open_positions()
        verdict   = get_day60_verdict(cache, positions)
        cache["day60_verdict"] = verdict
        log_signal(cache, "DAY60", "", "System", 0, 0, 0, verdict)
        save_cache(cache)

    # ── 7. Build and commit dashboard ─────────────────────────────────────────
    positions      = get_open_positions()
    dashboard_data = build_dashboard_data(cache, positions)
    commit_dashboard(dashboard_data)

    save_cache(cache)
    log.info(f"Done — {signals} signal(s) fired, {calls_made} Finnhub calls made")


if __name__ == "__main__":
    main()
