"""
Congressional + Fund Manager Portfolio Monitor
===============================================
Full logic:
  - Runs every weekday at 9:45am ET via GitHub Actions
  - Biweekly $250 deposit every other Monday (auto-tracked)
  - Percentage-based allocation mirroring conviction size
  - 60% single-position cap · $25 minimum · cash guard
  - Dynamic top-5 refresh: congress every 21d, funds every 28d
  - Cross-tier consensus scoring (congress + fund manager = strongest signal)
  - Missed signal alerts when cash = $0
  - Day 60 final report comparing vs QQQ and VOO
  - Paper trades on Alpaca · Gmail alerts · Gemini AI analysis

Data sources:
  Quiver Quantitative  → congressional trades
  SEC EDGAR            → fund manager 13F filings
  Alpaca               → paper trade execution
  Gemini Flash         → ticker extraction / lightweight checks
  Gemini Pro           → full trade analysis in emails
"""

import os
import json
import time
import smtplib
import logging
import requests
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

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

GMAIL_ADDRESS      = os.environ["GMAIL_ADDRESS"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
NOTIFY_EMAIL       = os.environ["NOTIFY_EMAIL"]
GEMINI_API_KEY     = os.environ["GEMINI_API_KEY"]

# ── Gemini endpoints ──────────────────────────────────────────────────────────
GEMINI_FLASH_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
)
GEMINI_PRO_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"gemini-1.5-pro:generateContent?key={GEMINI_API_KEY}"
)

# ── Config ────────────────────────────────────────────────────────────────────
DEPOSIT_AMOUNT         = 250.00   # added every other Monday
DEPOSIT_INTERVAL_DAYS  = 14       # every 14 days
MAX_POSITION_PCT       = 0.60     # no single position > 60% of available cash
MIN_POSITION_USD       = 25.00    # skip allocations below this
BUY_SCORE_THRESHOLD    = 6        # minimum score to trigger a buy
MAX_LAG_DAYS           = 30       # ignore trades older than this at disclosure
CONGRESS_REFRESH_DAYS  = 21       # re-rank congressional targets every 3 weeks
FUND_REFRESH_DAYS      = 28       # re-rank fund targets every 4 weeks
TOP_N                  = 5        # number of targets per tier
TEST_PERIOD_DAYS       = 60       # paper trading validation window

# Conviction % map: Quiver range string → fraction of available cash to deploy
CONVICTION_MAP = {
    "$1K - $15K":    0.10,
    "$15K - $50K":   0.20,
    "$50K - $100K":  0.30,
    "$100K - $250K": 0.40,
    "$250K - $500K": 0.60,
    "$500K - $1M":   0.75,
    "$1M - $5M":     0.85,
    "$5M - $25M":    1.00,
    "$25M+":         1.00,
}

# ── Default targets (used on first run) ───────────────────────────────────────
DEFAULT_CONGRESS = [
    {"name": "Nancy Pelosi",    "quiver_name": "Nancy Pelosi"},
    {"name": "David Rouzer",    "quiver_name": "David Rouzer"},
    {"name": "Josh Gottheimer", "quiver_name": "Josh Gottheimer"},
    {"name": "Dan Crenshaw",    "quiver_name": "Dan Crenshaw"},
    {"name": "Ron Wyden",       "quiver_name": "Ron Wyden"},
]

DEFAULT_FUNDS = [
    {"name": "Bill Ackman / Pershing Square", "cik": "0001336528"},
    {"name": "Michael Burry / Scion",         "cik": "0001649339"},
    {"name": "Stan Druckenmiller / Duquesne", "cik": "0001536411"},
    {"name": "Warren Buffett / Berkshire",    "cik": "0001067983"},
    {"name": "Philippe Laffont / Coatue",     "cik": "0001336920"},
]

CACHE_FILE = "seen_trades.json"


# ═══════════════════════════════════════════════════════════════════════════════
# CACHE
# ═══════════════════════════════════════════════════════════════════════════════

def load_cache() -> dict:
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE) as f:
            return json.load(f)
    now = datetime.utcnow().isoformat()
    return {
        # Trade deduplication
        "seen": [],
        # Capital tracking
        "cash_balance":        0.00,       # starts at $0, first deposit adds $250
        "total_deposited":     0.00,
        "total_invested":      0.00,
        "last_deposit_date":   None,       # ISO string of last deposit Monday
        "first_deposit_date":  None,       # used to find alternating Mondays
        # Open positions: ticker → {amount_invested, entry_date, conviction_pct}
        "positions": {},
        # Missed signals log
        "missed_signals": [],
        # Dynamic target lists
        "congress_targets":      DEFAULT_CONGRESS,
        "fund_targets":          DEFAULT_FUNDS,
        "last_congress_refresh": None,
        "last_fund_refresh":     None,
        # Test tracking
        "start_date":            now,
        "benchmark_start": {
            "QQQ": None,   # filled on first run via API
            "VOO": None,
        },
    }


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


def days_since_iso(iso_str: str | None) -> int:
    if not iso_str:
        return 999
    try:
        return (datetime.utcnow() - datetime.fromisoformat(iso_str)).days
    except Exception:
        return 999


def days_into_test(cache: dict) -> int:
    try:
        start = datetime.fromisoformat(cache.get("start_date", datetime.utcnow().isoformat()))
        return (datetime.utcnow() - start).days
    except Exception:
        return 0


def is_deposit_monday(cache: dict) -> bool:
    """
    Returns True if today is a deposit Monday.
    Logic: today must be a Monday, AND either:
      - No deposit has ever been made (first deposit), OR
      - At least 14 days have passed since the last deposit.
    """
    today = datetime.utcnow()
    if today.weekday() != 0:   # 0 = Monday
        return False
    last = cache.get("last_deposit_date")
    if not last:
        return True   # first ever deposit
    return days_since_iso(last) >= DEPOSIT_INTERVAL_DAYS


def next_deposit_date(cache: dict) -> str:
    """Return a human-readable string for when the next deposit will happen."""
    last = cache.get("last_deposit_date")
    if not last:
        # Find next Monday
        today   = datetime.utcnow()
        days_to = (7 - today.weekday()) % 7 or 7
        nxt     = today + timedelta(days=days_to)
        return nxt.strftime("%B %d, %Y")
    try:
        last_dt = datetime.fromisoformat(last)
        nxt     = last_dt + timedelta(days=DEPOSIT_INTERVAL_DAYS)
        return nxt.strftime("%B %d, %Y")
    except Exception:
        return "Next Monday"


# ═══════════════════════════════════════════════════════════════════════════════
# CONVICTION → ALLOCATION
# ═══════════════════════════════════════════════════════════════════════════════

def parse_conviction(range_str: str) -> float:
    """
    Convert a Quiver Quant range string into a conviction fraction (0.0–1.0).
    Falls back to 0.20 (moderate) if the range isn't recognised.
    """
    if not range_str:
        return 0.20
    for key, pct in CONVICTION_MAP.items():
        # Flexible matching — strips spaces and normalises
        if key.lower().replace(" ", "") in range_str.lower().replace(" ", ""):
            return pct
    # If a dollar amount is embedded, try to classify by magnitude
    try:
        digits = "".join(c for c in range_str if c.isdigit() or c == ".")
        if digits:
            val = float(digits)
            if val < 15_000:   return 0.10
            if val < 50_000:   return 0.20
            if val < 100_000:  return 0.30
            if val < 250_000:  return 0.40
            if val < 500_000:  return 0.60
            if val < 1_000_000: return 0.75
            if val < 5_000_000: return 0.85
            return 1.00
    except Exception:
        pass
    return 0.20


def calculate_allocation(conviction_pct: float, cash_balance: float) -> float:
    """
    Apply conviction % to available cash with three guards:
    1. Hard cap: no more than MAX_POSITION_PCT (60%) of cash in one trade
    2. Cash guard: can't spend more than what's available
    3. Minimum: skip if result < MIN_POSITION_USD ($25)
    Returns 0.0 if trade should be skipped.
    """
    raw        = cash_balance * conviction_pct
    capped     = min(raw, cash_balance * MAX_POSITION_PCT)
    available  = min(capped, cash_balance)
    if available < MIN_POSITION_USD:
        return 0.0
    return round(available, 2)


# ═══════════════════════════════════════════════════════════════════════════════
# GEMINI AI
# ═══════════════════════════════════════════════════════════════════════════════

def gemini_call(prompt: str, use_pro: bool = False) -> str:
    url  = GEMINI_PRO_URL if use_pro else GEMINI_FLASH_URL
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 700, "temperature": 0.3},
    }
    try:
        resp = requests.post(url, json=body, timeout=30)
        resp.raise_for_status()
        return resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        log.warning(f"Gemini ({'Pro' if use_pro else 'Flash'}) failed: {e}")
        return ""


def extract_ticker(raw_text: str) -> str:
    prompt = (
        "Extract ONLY the stock ticker symbol from this text. "
        "Reply with just the ticker in capitals, nothing else. "
        f"If no clear ticker, reply UNKNOWN.\n\nText: {raw_text[:400]}"
    )
    result = gemini_call(prompt, use_pro=False)
    if not result:
        return "UNKNOWN"
    ticker = result.strip().upper().split()[0]
    return ticker if ticker.isalpha() and len(ticker) <= 5 else "UNKNOWN"


def get_ai_analysis(trade_summary: str, score_result: dict,
                    allocation: float, cash_after: float) -> str:
    prompt = f"""You are a concise investment analyst. A portfolio monitor detected:

{trade_summary}

SIGNAL SCORE: {score_result['score']}/10
SCORING REASONS: {', '.join(score_result['reasons'])}
ACTION: {score_result['action']}
AMOUNT TO DEPLOY: ${allocation:.2f}
CASH REMAINING AFTER TRADE: ${cash_after:.2f}

Write exactly 3 short paragraphs:
1. What this trade signals strategically and why the conviction size matters
2. What a retail investor deploying ${allocation:.2f} should specifically do
   (include whether a limit order makes sense given the disclosure lag)
3. Key risks: lag risk, concentration, any macro context

Direct and specific. No disclaimers. No preamble."""
    result = gemini_call(prompt, use_pro=True)
    return result if result else "AI analysis unavailable — review signal manually."


def get_day60_verdict(cache: dict, positions: list) -> str:
    total_val    = sum(float(p.get("market_value", 0)) for p in positions)
    total_dep    = cache.get("total_deposited", 0)
    total_return = ((total_val - total_dep) / total_dep * 100) if total_dep else 0
    prompt = f"""A 60-day paper trading test just completed for a portfolio monitor
that tracks congressional disclosures and fund manager 13F filings.

RESULTS:
- Total deposited over 60 days: ${total_dep:.2f}
- Current portfolio value: ${total_val:.2f}
- Total return: {total_return:+.1f}%
- Signals fired: {len(cache.get('seen', []))} trades processed
- Missed signals (no cash): {len(cache.get('missed_signals', []))}

Write a 3-paragraph verdict:
1. Whether this strategy outperformed a simple QQQ/VOO buy-and-hold over 60 days
   (assume QQQ returned approximately 5% and VOO 4% in this period as a baseline)
2. Specific recommendation: deploy real money, adjust the strategy, or redirect
   the $250 biweekly to more index funds — with clear reasoning
3. If going live, what to watch for in the next 60 days

Be direct. Give a real recommendation."""
    result = gemini_call(prompt, use_pro=True)
    return result if result else "Verdict unavailable — review results manually."


# ═══════════════════════════════════════════════════════════════════════════════
# DYNAMIC TARGET REFRESH
# ═══════════════════════════════════════════════════════════════════════════════

def refresh_congress_targets(cache: dict) -> tuple:
    """Re-rank congressional targets by trailing 12-month buy activity."""
    log.info("Refreshing congressional targets...")
    url     = "https://api.quiverquant.com/beta/live/congresstrading"
    headers = {"Accept": "application/json", "User-Agent": "PortfolioMonitor/1.0"}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        all_trades = resp.json()
    except Exception as e:
        log.warning(f"Congress refresh fetch failed: {e}")
        return cache["congress_targets"], [], []

    cutoff = datetime.utcnow() - timedelta(days=365)
    scores: dict = {}

    for trade in all_trades:
        name = trade.get("Representative", "").strip()
        if not name:
            continue
        try:
            td = datetime.strptime(str(trade.get("TransactionDate", ""))[:10], "%Y-%m-%d")
        except Exception:
            continue
        if td < cutoff:
            continue
        age_days  = (datetime.utcnow() - td).days
        recency_w = max(0.1, 1 - (age_days / 365))
        tx        = str(trade.get("Transaction", "")).upper()
        type_w    = 1.5 if any(t in tx for t in ["BUY", "PURCHASE"]) else 1.0
        # Weight higher conviction trades more
        conviction = parse_conviction(str(trade.get("Range", "")))
        scores[name] = scores.get(name, 0) + (recency_w * type_w * (1 + conviction))

    if not scores:
        return cache["congress_targets"], [], []

    ranked    = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    new_top   = [{"name": n, "quiver_name": n} for n, _ in ranked[:TOP_N]]
    old_names = {t["name"] for t in cache.get("congress_targets", DEFAULT_CONGRESS)}
    new_names = {t["name"] for t in new_top}
    added     = [t for t in new_top if t["name"] not in old_names]
    dropped   = [t for t in cache.get("congress_targets", []) if t["name"] not in new_names]

    log.info(f"Congress refresh complete. Added: {[a['name'] for a in added]}, "
             f"Dropped: {[d['name'] for d in dropped]}")
    return new_top, added, dropped


def refresh_fund_targets(cache: dict) -> tuple:
    """Ask Gemini Pro to re-rank top fund managers by recent 13F performance."""
    log.info("Refreshing fund manager targets...")
    prompt = """List the 5 best-performing publicly disclosed hedge fund or institutional
investor portfolios (via SEC 13F filings) over the last 12 months based on their
disclosed equity holdings performance.

Reply ONLY with a JSON array, no other text, no markdown fences:
[
  {"name": "Manager Name / Fund Name", "cik": "SEC CIK number padded to 10 digits"},
  ...
]

Requirements:
- Real SEC CIK numbers only
- Concentrated portfolios (under 20 positions) — more mirrorable for retail investors
- Must file 13F-HR with the SEC
- Focus on equity-heavy portfolios, not macro/FX funds"""

    result = gemini_call(prompt, use_pro=True)
    new_targets = None
    if result:
        try:
            clean  = result.replace("```json", "").replace("```", "").strip()
            parsed = json.loads(clean)
            if isinstance(parsed, list) and len(parsed) >= 3:
                new_targets = parsed[:TOP_N]
        except Exception as e:
            log.warning(f"Fund refresh parse failed: {e}")

    if not new_targets:
        log.warning("Fund refresh returned no valid data — keeping existing targets")
        return cache.get("fund_targets", DEFAULT_FUNDS), [], []

    old_names = {t["name"] for t in cache.get("fund_targets", DEFAULT_FUNDS)}
    new_names = {t["name"] for t in new_targets}
    added     = [t for t in new_targets if t["name"] not in old_names]
    dropped   = [t for t in cache.get("fund_targets", []) if t["name"] not in new_names]

    log.info(f"Fund refresh complete. Added: {[a['name'] for a in added]}, "
             f"Dropped: {[d['name'] for d in dropped]}")
    return new_targets, added, dropped


# ═══════════════════════════════════════════════════════════════════════════════
# DATA FETCHING
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_congressional_trades(politician_name: str) -> list:
    url     = "https://api.quiverquant.com/beta/live/congresstrading"
    headers = {"Accept": "application/json", "User-Agent": "PortfolioMonitor/1.0"}
    try:
        resp       = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        all_trades = resp.json()
        filtered   = [
            t for t in all_trades
            if politician_name.lower() in t.get("Representative", "").lower()
        ]
        return filtered[:10]
    except Exception as e:
        log.warning(f"Quiver fetch failed for {politician_name}: {e}")
        return []


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


def get_benchmark_price(ticker: str) -> float | None:
    """Fetch current price for QQQ/VOO from Alpaca for benchmark tracking."""
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
# SIGNAL SCORING
# ═══════════════════════════════════════════════════════════════════════════════

def score_signal(ticker: str, trade_type: str, lag_days: int,
                 matching_congress: int, matching_funds: int) -> dict:
    score   = 0
    reasons = []
    t_upper = trade_type.upper()

    # Trade type
    if any(t in t_upper for t in ["BUY", "PURCHASE", "CALL"]):
        score += 3
        reasons.append("Strong buy-type signal (+3)")
    elif any(t in t_upper for t in ["SELL", "PUT"]):
        score -= 2
        reasons.append("Sell / put signal (−2)")
    else:
        score += 1
        reasons.append("Neutral trade type (+1)")

    # Freshness
    if lag_days <= 7:
        score += 2
        reasons.append(f"Fresh disclosure — {lag_days}d lag (+2)")
    elif lag_days <= 20:
        score += 1
        reasons.append(f"Moderate lag — {lag_days}d (+1)")
    elif lag_days <= MAX_LAG_DAYS:
        score -= 1
        reasons.append(f"Getting stale — {lag_days}d (−1)")
    else:
        score -= 2
        reasons.append(f"Stale — {lag_days}d, likely priced in (−2)")

    # Congressional consensus
    if matching_congress >= 2:
        score += 2
        reasons.append(f"{matching_congress} congress members on same ticker (+2)")

    # Cross-tier consensus (strongest signal)
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
    elif score <= 5 and any(t in t_upper for t in ["SELL", "PUT"]):
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


def place_paper_buy(ticker: str, dollar_amount: float) -> dict:
    headers = {
        "APCA-API-KEY-ID":     ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
        "Content-Type":        "application/json",
    }
    order = {
        "symbol":        ticker,
        "notional":      str(round(dollar_amount, 2)),
        "side":          "buy",
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
        log.info(f"Paper BUY: ${dollar_amount:.2f} of {ticker} | ID: {result.get('id')}")
        return result
    except Exception as e:
        log.error(f"Alpaca BUY failed for {ticker}: {e}")
        return {"error": str(e)}


def place_paper_sell(ticker: str, dollar_amount: float) -> dict:
    headers = {
        "APCA-API-KEY-ID":     ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
        "Content-Type":        "application/json",
    }
    order = {
        "symbol":        ticker,
        "notional":      str(round(dollar_amount, 2)),
        "side":          "sell",
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
        log.info(f"Paper SELL: ${dollar_amount:.2f} of {ticker} | ID: {result.get('id')}")
        return result
    except Exception as e:
        log.error(f"Alpaca SELL failed for {ticker}: {e}")
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════════
# EMAIL BUILDERS
# ═══════════════════════════════════════════════════════════════════════════════

def send_email(subject: str, html_body: str):
    msg            = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_ADDRESS
    msg["To"]      = NOTIFY_EMAIL
    msg.attach(MIMEText(html_body, "html"))
    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as s:
            s.ehlo()
            s.starttls()
            s.ehlo()
            s.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            s.sendmail(GMAIL_ADDRESS, NOTIFY_EMAIL, msg.as_string())
        log.info(f"Email sent: {subject}")
    except Exception as e:
        log.error(f"Gmail failed: {e}")


def _base_styles() -> str:
    return """
    body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
         background:#f8f7f4;margin:0;padding:20px;color:#1a1a1a}
    .w{max-width:600px;margin:0 auto}
    .h{background:#1a1a1a;color:#fff;padding:20px 24px;border-radius:10px 10px 0 0}
    .h h1{margin:0;font-size:18px}
    .h p{margin:4px 0 0;font-size:12px;color:#aaa}
    .b{background:#fff;padding:24px;border:1px solid #e8e5e0}
    .f{background:#f1f0ed;padding:12px 24px;border-radius:0 0 10px 10px;
       font-size:12px;color:#888;border:1px solid #e8e5e0;border-top:none}
    .card{background:#f8f7f4;border:1px solid #e8e5e0;border-radius:8px;
          padding:14px 16px;margin:12px 0}
    .card h3{margin:0 0 8px;font-size:11px;text-transform:uppercase;
             letter-spacing:.06em;color:#888}
    .metrics{display:flex;gap:10px;margin-bottom:16px}
    .metric{flex:1;background:#f8f7f4;border-radius:8px;padding:12px}
    .metric-label{font-size:10px;color:#888;text-transform:uppercase;
                  letter-spacing:.06em;margin-bottom:4px}
    .metric-value{font-size:18px;font-weight:600}
    table{width:100%;border-collapse:collapse;font-size:13px}
    th{text-align:left;padding:6px 10px;background:#f1f0ed;font-size:11px;
       text-transform:uppercase;letter-spacing:.06em;color:#888}
    td{border-bottom:1px solid #f1f0ed;padding:6px 10px}
    .green{color:#16a34a} .red{color:#dc2626} .amber{color:#d97706}
    """


def build_trade_alert_email(trade: dict, score_result: dict, ai_analysis: str,
                            order_result: dict, conviction_pct: float,
                            allocation: float, cash_before: float,
                            cash_after: float, positions: list,
                            cache: dict) -> str:
    action       = score_result["action"]
    score        = score_result["score"]
    colors       = {"BUY": "#16a34a", "SELL": "#dc2626", "WATCH": "#d97706"}
    ac           = colors.get(action, "#64748b")
    day_num      = days_into_test(cache)
    end_date     = (datetime.utcnow() + timedelta(days=TEST_PERIOD_DAYS - day_num)).strftime("%b %d, %Y")
    reasons_li   = "".join(f"<li style='margin-bottom:4px'>{r}</li>" for r in score_result["reasons"])
    order_status = "✅ Paper trade placed" if "id" in order_result else "⚠️ Order failed"
    order_id     = order_result.get("id", "N/A")[:16] + "..." if order_result.get("id") else "N/A"

    pos_rows = ""
    for p in positions:
        pl    = float(p.get("unrealized_pl", 0))
        plpct = float(p.get("unrealized_plpc", 0)) * 100
        c     = "green" if pl >= 0 else "red"
        pos_rows += (
            f"<tr><td style='font-weight:600'>{p['symbol']}</td>"
            f"<td>${float(p['market_value']):.2f}</td>"
            f"<td class='{c}'>${pl:+.2f} ({plpct:+.1f}%)</td></tr>"
        )

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>{_base_styles()}
  .badge{{display:inline-block;padding:4px 14px;border-radius:20px;
          font-weight:700;font-size:14px;color:#fff;background:{ac}}}
  .bar{{background:#f1f0ed;border-radius:8px;height:10px;overflow:hidden;margin:8px 0}}
  .fill{{height:100%;border-radius:8px;background:{ac};width:{score*10}%}}
  .action-box{{background:#fffbeb;border:1px solid #fbbf24;border-radius:8px;
               padding:14px 16px;margin-top:12px;font-size:13px;line-height:1.6}}
</style></head><body><div class="w">
  <div class="h">
    <h1>📊 Trade Alert — {trade.get('ticker','?')}</h1>
    <p>{datetime.utcnow().strftime('%B %d, %Y · %H:%M UTC')} ·
       Paper Trading · Day {day_num}/{TEST_PERIOD_DAYS} · Ends {end_date}</p>
  </div>
  <div class="b">

    <!-- Action + ticker -->
    <div style="margin-bottom:20px">
      <span class="badge">{action}</span>
      <span style="font-size:24px;font-weight:600;margin-left:12px">
        {trade.get('ticker','?')}
      </span>
      <span style="font-size:13px;color:#888;margin-left:8px">
        {trade.get('source_name','?')} · {trade.get('trade_type','?')}
      </span>
    </div>

    <!-- Score -->
    <div class="card">
      <h3>Signal Score</h3>
      <div style="font-size:28px;font-weight:700">{score}
        <span style="font-size:16px;color:#888">/10</span>
      </div>
      <div class="bar"><div class="fill"></div></div>
      <ul style="margin:8px 0 0;padding-left:18px;font-size:13px;
                 color:#555;line-height:1.8">{reasons_li}</ul>
    </div>

    <!-- Capital allocation -->
    <div class="card">
      <h3>Capital Allocation</h3>
      <div class="metrics">
        <div class="metric">
          <div class="metric-label">Conviction</div>
          <div class="metric-value">{int(conviction_pct*100)}%</div>
        </div>
        <div class="metric">
          <div class="metric-label">Allocated</div>
          <div class="metric-value green">${allocation:.2f}</div>
        </div>
        <div class="metric">
          <div class="metric-label">Cash before</div>
          <div class="metric-value">${cash_before:.2f}</div>
        </div>
        <div class="metric">
          <div class="metric-label">Cash after</div>
          <div class="metric-value {"amber" if cash_after < 50 else ""}">${cash_after:.2f}</div>
        </div>
      </div>
      <table>
        <tr><th>Field</th><th>Value</th></tr>
        <tr><td>Range disclosed</td><td>{trade.get('amount','?')}</td></tr>
        <tr><td>Trade date</td><td>{trade.get('trade_date','?')}</td></tr>
        <tr><td>Disclosed date</td><td>{trade.get('disclosed_date','?')}</td></tr>
        <tr><td>Paper order</td><td>{order_status} · {order_id}</td></tr>
        <tr><td>Next deposit</td><td>{next_deposit_date(cache)}</td></tr>
      </table>
    </div>

    <!-- AI analysis -->
    <div class="card">
      <h3>AI Analysis (Gemini Pro)</h3>
      <p style="font-size:14px;line-height:1.7;white-space:pre-wrap;
                color:#333;margin:0">{ai_analysis}</p>
    </div>

    <!-- Open positions -->
    <div class="card">
      <h3>Current Paper Positions</h3>
      {"<p style='font-size:13px;color:#888;margin:0'>No open positions yet.</p>"
       if not positions else
       f"<table><tr><th>Ticker</th><th>Value</th><th>P&L</th></tr>{pos_rows}</table>"}
    </div>

    <div class="action-box">
      <strong>👤 Your action:</strong> This is a <strong>paper trade only</strong>.
      No real money moved. After Day {TEST_PERIOD_DAYS}, if results beat QQQ,
      mirror this trade manually in your real brokerage.
    </div>

  </div>
  <div class="f">
    Portfolio Monitor · Paper trading · {TEST_PERIOD_DAYS}-day validation · Ends {end_date}
  </div>
</div></body></html>"""


def build_missed_signal_email(trade: dict, score_result: dict, conviction_pct: float,
                               needed: float, cache: dict) -> str:
    day_num  = days_into_test(cache)
    nxt      = next_deposit_date(cache)
    cash     = cache.get("cash_balance", 0)
    reasons_li = "".join(f"<li>{r}</li>" for r in score_result["reasons"])
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>{_base_styles()}</style></head><body><div class="w">
  <div class="h">
    <h1>⚠️ Missed Signal — {trade.get('ticker','?')}</h1>
    <p>{datetime.utcnow().strftime('%B %d, %Y · %H:%M UTC')} ·
       Day {day_num}/{TEST_PERIOD_DAYS} · Paper Trading</p>
  </div>
  <div class="b">
    <div class="card">
      <h3>Signal Details</h3>
      <p style="font-size:14px;margin:0 0 12px">
        A <strong>score {score_result['score']}/10</strong> signal fired on
        <strong>{trade.get('ticker','?')}</strong> from
        {trade.get('source_name','?')} but could not be executed.
      </p>
      <table>
        <tr><th>Field</th><th>Value</th></tr>
        <tr><td>Ticker</td><td><strong>{trade.get('ticker','?')}</strong></td></tr>
        <tr><td>Trade type</td><td>{trade.get('trade_type','?')}</td></tr>
        <tr><td>Conviction</td><td>{int(conviction_pct*100)}%</td></tr>
        <tr><td>Would have allocated</td><td class="amber">${needed:.2f}</td></tr>
        <tr><td>Cash available</td><td class="red">${cash:.2f}</td></tr>
        <tr><td>Shortfall</td><td class="red">${needed - cash:.2f}</td></tr>
        <tr><td>Next deposit</td><td class="green">{nxt} (+$250.00)</td></tr>
      </table>
    </div>
    <div class="card">
      <h3>Score Breakdown</h3>
      <ul style="margin:0;padding-left:18px;font-size:13px;
                 color:#555;line-height:1.8">{reasons_li}</ul>
    </div>
    <div style="background:#fce8e8;border:1px solid #f5c6cb;border-radius:8px;
                padding:14px 16px;margin-top:12px;font-size:13px;line-height:1.6">
      <strong>📋 For your records:</strong> This signal has been logged.
      The next deposit of $250 arrives <strong>{nxt}</strong>.
      If this ticker is still showing strong signals then, consider acting on it.
    </div>
  </div>
  <div class="f">Portfolio Monitor · Paper trading · Missed signal log</div>
</div></body></html>"""


def build_deposit_email(amount: float, new_balance: float,
                        positions: list, cache: dict) -> str:
    day_num  = days_into_test(cache)
    nxt      = next_deposit_date(cache)
    total_pl = sum(float(p.get("unrealized_pl", 0)) for p in positions)
    pl_cls   = "green" if total_pl >= 0 else "red"

    pos_rows = ""
    for p in positions:
        pl    = float(p.get("unrealized_pl", 0))
        plpct = float(p.get("unrealized_plpc", 0)) * 100
        c     = "green" if pl >= 0 else "red"
        pos_rows += (
            f"<tr><td style='font-weight:600'>{p['symbol']}</td>"
            f"<td>${float(p['market_value']):.2f}</td>"
            f"<td class='{c}'>${pl:+.2f} ({plpct:+.1f}%)</td></tr>"
        )

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>{_base_styles()}</style></head><body><div class="w">
  <div class="h">
    <h1>💵 Biweekly Deposit — $250 Added</h1>
    <p>{datetime.utcnow().strftime('%B %d, %Y')} · Day {day_num}/{TEST_PERIOD_DAYS}
       · Paper Trading</p>
  </div>
  <div class="b">
    <div class="metrics">
      <div class="metric">
        <div class="metric-label">Deposited</div>
        <div class="metric-value green">+${amount:.2f}</div>
      </div>
      <div class="metric">
        <div class="metric-label">Cash available</div>
        <div class="metric-value">${new_balance:.2f}</div>
      </div>
      <div class="metric">
        <div class="metric-label">Total deposited</div>
        <div class="metric-value">${cache.get('total_deposited', 0):.2f}</div>
      </div>
      <div class="metric">
        <div class="metric-label">Portfolio P&L</div>
        <div class="metric-value {pl_cls}">${total_pl:+.2f}</div>
      </div>
    </div>
    <div class="card">
      <h3>Current Positions</h3>
      {"<p style='font-size:13px;color:#888;margin:0'>No open positions — full $250 available for signals.</p>"
       if not positions else
       f"<table><tr><th>Ticker</th><th>Value</th><th>P&L</th></tr>{pos_rows}</table>"}
    </div>
    <p style="font-size:13px;color:#888;margin-top:16px">
      The script will now check for signals with your refreshed cash balance.
      Next deposit: <strong>{nxt}</strong>
    </p>
  </div>
  <div class="f">Portfolio Monitor · $250 biweekly deposits · Every other Monday</div>
</div></body></html>"""


def build_refresh_email(tier: str, added: list, dropped: list,
                        new_targets: list, cache: dict) -> str:
    day_num     = days_into_test(cache)
    interval    = 21 if "Congress" in tier else 28
    added_rows  = "".join(
        f"<tr><td style='color:#16a34a;font-weight:600'>✅ {t['name']}</td>"
        f"<td style='color:#16a34a'>Added</td></tr>" for t in added
    )
    dropped_rows = "".join(
        f"<tr><td style='color:#dc2626;font-weight:600'>❌ {t['name']}</td>"
        f"<td style='color:#dc2626'>Removed</td></tr>" for t in dropped
    )
    current_rows = "".join(
        f"<tr><td style='font-weight:500'>{i+1}. {t['name']}</td></tr>"
        for i, t in enumerate(new_targets)
    )
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>{_base_styles()}</style></head><body><div class="w">
  <div class="h">
    <h1>🔄 Watchlist Refreshed — {tier}</h1>
    <p>{datetime.utcnow().strftime('%B %d, %Y')} · Day {day_num}/{TEST_PERIOD_DAYS}
       · Auto-refresh every {interval} days</p>
  </div>
  <div class="b">
    {"<div class='card'><h3>Changes</h3><table>" + added_rows + dropped_rows + "</table></div>"
     if added or dropped else
     "<div class='card'><p style='font-size:13px;color:#888;margin:0'>"
     "No changes — same top 5 confirmed for another cycle.</p></div>"}
    <div class="card">
      <h3>Now Tracking (Top {TOP_N})</h3>
      <table>{current_rows}</table>
    </div>
    <p style="font-size:12px;color:#888;margin-top:12px">
      Next refresh in {interval} days. No action required from you.
    </p>
  </div>
  <div class="f">Portfolio Monitor · Dynamic target refresh</div>
</div></body></html>"""


def build_daily_summary_email(positions: list, cache: dict,
                               missed_today: list) -> str:
    total_val    = sum(float(p.get("market_value", 0)) for p in positions)
    total_pl     = sum(float(p.get("unrealized_pl", 0)) for p in positions)
    pl_cls       = "green" if total_pl >= 0 else "red"
    cash         = cache.get("cash_balance", 0)
    day_num      = days_into_test(cache)
    nxt_deposit  = next_deposit_date(cache)
    total_dep    = cache.get("total_deposited", 0)

    days_to_cr = max(0, CONGRESS_REFRESH_DAYS - days_since_iso(cache.get("last_congress_refresh")))
    days_to_fr = max(0, FUND_REFRESH_DAYS - days_since_iso(cache.get("last_fund_refresh")))

    c_names = ", ".join(t["name"].split()[-1] for t in cache.get("congress_targets", DEFAULT_CONGRESS))
    f_names = ", ".join(t["name"].split("/")[0].strip() for t in cache.get("fund_targets", DEFAULT_FUNDS))

    pos_rows = ""
    for p in positions:
        pl    = float(p.get("unrealized_pl", 0))
        plpct = float(p.get("unrealized_plpc", 0)) * 100
        c     = "green" if pl >= 0 else "red"
        pos_rows += (
            f"<tr><td style='font-weight:600'>{p['symbol']}</td>"
            f"<td>${float(p['market_value']):.2f}</td>"
            f"<td class='{c}'>${pl:+.2f} ({plpct:+.1f}%)</td></tr>"
        )

    missed_rows = ""
    for m in missed_today:
        missed_rows += (
            f"<tr><td style='font-weight:600'>{m['ticker']}</td>"
            f"<td>{m['reason']}</td>"
            f"<td class='amber'>${m.get('needed', 0):.2f} needed</td></tr>"
        )

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>{_base_styles()}</style></head><body><div class="w">
  <div class="h">
    <h1>📈 Daily Summary — Day {day_num}/{TEST_PERIOD_DAYS}</h1>
    <p>{datetime.utcnow().strftime('%B %d, %Y')} · No new signals today</p>
  </div>
  <div class="b">
    <div class="metrics">
      <div class="metric">
        <div class="metric-label">Portfolio value</div>
        <div class="metric-value">${total_val:.2f}</div>
      </div>
      <div class="metric">
        <div class="metric-label">Unrealised P&L</div>
        <div class="metric-value {pl_cls}">${total_pl:+.2f}</div>
      </div>
      <div class="metric">
        <div class="metric-label">Cash available</div>
        <div class="metric-value">${cash:.2f}</div>
      </div>
      <div class="metric">
        <div class="metric-label">Total deposited</div>
        <div class="metric-value">${total_dep:.2f}</div>
      </div>
    </div>

    <!-- Positions -->
    <div class="card">
      <h3>Open Positions</h3>
      {"<p style='font-size:13px;color:#888;margin:0'>No open positions.</p>"
       if not positions else
       f"<table><tr><th>Ticker</th><th>Value</th><th>P&L</th></tr>{pos_rows}</table>"}
    </div>

    <!-- Missed signals today -->
    {"<div class='card'><h3>Signals Watched (Not Traded)</h3>"
     f"<table><tr><th>Ticker</th><th>Reason</th><th>Would Need</th></tr>"
     f"{missed_rows}</table></div>" if missed_today else ""}

    <!-- Tracking info -->
    <div class="card">
      <h3>Currently Tracking</h3>
      <p style="font-size:12px;color:#555;line-height:1.8;margin:0">
        🏛️ <strong>Congress:</strong> {c_names}<br>
        🏦 <strong>Funds:</strong> {f_names}<br>
        🔄 Congress refresh in {days_to_cr}d ·
           Fund refresh in {days_to_fr}d<br>
        💵 Next deposit: <strong>{nxt_deposit}</strong>
      </p>
    </div>

    <p style="font-size:12px;color:#888;margin-top:8px">
      Next check tomorrow at 9:45am ET.
    </p>
  </div>
  <div class="f">Portfolio Monitor · Paper trading · Day {day_num}/{TEST_PERIOD_DAYS}</div>
</div></body></html>"""


def build_day60_email(verdict: str, positions: list, cache: dict) -> str:
    total_val = sum(float(p.get("market_value", 0)) for p in positions)
    total_dep = cache.get("total_deposited", 0)
    total_ret = ((total_val - total_dep) / total_dep * 100) if total_dep else 0
    pl_cls    = "green" if total_ret >= 0 else "red"
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>{_base_styles()}</style></head><body><div class="w">
  <div class="h">
    <h1>🏁 Day 60 — Final Verdict</h1>
    <p>{datetime.utcnow().strftime('%B %d, %Y')} · Paper Trading Test Complete</p>
  </div>
  <div class="b">
    <div class="metrics">
      <div class="metric">
        <div class="metric-label">Total deposited</div>
        <div class="metric-value">${total_dep:.2f}</div>
      </div>
      <div class="metric">
        <div class="metric-label">Portfolio value</div>
        <div class="metric-value">${total_val:.2f}</div>
      </div>
      <div class="metric">
        <div class="metric-label">Total return</div>
        <div class="metric-value {pl_cls}">{total_ret:+.1f}%</div>
      </div>
      <div class="metric">
        <div class="metric-label">Signals fired</div>
        <div class="metric-value">{len(cache.get('seen', []))}</div>
      </div>
    </div>
    <div class="card">
      <h3>AI Verdict (Gemini Pro)</h3>
      <p style="font-size:14px;line-height:1.7;white-space:pre-wrap;
                color:#333;margin:0">{verdict}</p>
    </div>
    <div style="background:#e6f4ea;border:1px solid #a8d5b5;border-radius:8px;
                padding:14px 16px;margin-top:12px;font-size:13px;line-height:1.6">
      <strong>👤 Your decision point:</strong> Review the verdict above.
      If going live — fund your real brokerage with $250 and mirror the next
      strong signal manually. If not — redirect the $250 biweekly to QQQ.
    </div>
  </div>
  <div class="f">Portfolio Monitor · 60-day test complete</div>
</div></body></html>"""


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    log.info("=" * 60)
    log.info("Portfolio Monitor — daily run")
    log.info(datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"))
    log.info("=" * 60)

    cache     = load_cache()
    positions = get_open_positions()
    signals   = 0
    missed_today: list = []

    # ── 1. Biweekly deposit check ─────────────────────────────────────────────
    if is_deposit_monday(cache):
        prev_balance = cache.get("cash_balance", 0.0)
        cache["cash_balance"]       = round(prev_balance + DEPOSIT_AMOUNT, 2)
        cache["total_deposited"]    = round(cache.get("total_deposited", 0) + DEPOSIT_AMOUNT, 2)
        cache["last_deposit_date"]  = datetime.utcnow().isoformat()
        if not cache.get("first_deposit_date"):
            cache["first_deposit_date"] = datetime.utcnow().isoformat()
        log.info(f"Deposit: +${DEPOSIT_AMOUNT:.2f} → cash = ${cache['cash_balance']:.2f}")

        # Capture benchmark prices on first deposit
        if not cache["benchmark_start"]["QQQ"]:
            cache["benchmark_start"]["QQQ"] = get_benchmark_price("QQQ")
            cache["benchmark_start"]["VOO"] = get_benchmark_price("VOO")
            log.info(f"Benchmark prices captured: QQQ={cache['benchmark_start']['QQQ']}, "
                     f"VOO={cache['benchmark_start']['VOO']}")

        save_cache(cache)
        send_email(
            f"💵 Deposit — $250 added · Cash now ${cache['cash_balance']:.2f} · "
            f"Day {days_into_test(cache)}/{TEST_PERIOD_DAYS}",
            build_deposit_email(DEPOSIT_AMOUNT, cache["cash_balance"], positions, cache),
        )

    # ── 2. Target list refresh ────────────────────────────────────────────────
    if days_since_iso(cache.get("last_congress_refresh")) >= CONGRESS_REFRESH_DAYS:
        new_c, added, dropped = refresh_congress_targets(cache)
        cache["congress_targets"]      = new_c
        cache["last_congress_refresh"] = datetime.utcnow().isoformat()
        save_cache(cache)
        send_email(
            f"🔄 Watchlist Update — Congressional Top {TOP_N} · "
            f"Day {days_into_test(cache)}/{TEST_PERIOD_DAYS}",
            build_refresh_email("Congressional Portfolios", added, dropped, new_c, cache),
        )

    if days_since_iso(cache.get("last_fund_refresh")) >= FUND_REFRESH_DAYS:
        new_f, added, dropped = refresh_fund_targets(cache)
        cache["fund_targets"]      = new_f
        cache["last_fund_refresh"] = datetime.utcnow().isoformat()
        save_cache(cache)
        send_email(
            f"🔄 Watchlist Update — Fund Manager Top {TOP_N} · "
            f"Day {days_into_test(cache)}/{TEST_PERIOD_DAYS}",
            build_refresh_email("Fund Manager Portfolios", added, dropped, new_f, cache),
        )

    congress_targets = cache.get("congress_targets", DEFAULT_CONGRESS)
    fund_targets     = cache.get("fund_targets", DEFAULT_FUNDS)
    congress_tickers: dict = {}   # ticker → [member names] for consensus tracking

    # ── 3. Congressional disclosures ──────────────────────────────────────────
    for member in congress_targets:
        log.info(f"Checking congressional: {member['name']}")
        trades = fetch_congressional_trades(member["quiver_name"])
        time.sleep(1)

        for trade in trades:
            trade_id = (
                f"{member['name']}-"
                f"{trade.get('Ticker','')}-"
                f"{trade.get('TransactionDate','')}"
            )
            if trade_id in cache.get("seen", []):
                continue

            ticker     = str(trade.get("Ticker", "")).upper().strip()
            trade_type = str(trade.get("Transaction", "unknown"))
            trade_date = str(trade.get("TransactionDate", ""))
            disclosed  = str(trade.get("DisclosureDate", ""))
            amount_str = str(trade.get("Range", ""))

            # Ticker cleanup
            if not ticker or not ticker.isalpha() or len(ticker) > 5:
                ticker = extract_ticker(json.dumps(trade)[:400])
            if not ticker or ticker == "UNKNOWN":
                cache["seen"].append(trade_id)
                continue

            lag = days_since(trade_date)

            # Track for consensus
            congress_tickers.setdefault(ticker, [])
            congress_tickers[ticker].append(member["name"])

            if lag > MAX_LAG_DAYS:
                log.info(f"  Stale ({lag}d): {ticker} — skipping")
                cache["seen"].append(trade_id)
                continue

            log.info(f"  New: {member['name']} → {trade_type} {ticker} "
                     f"({amount_str}) lag={lag}d")

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
                    # Not enough cash or below minimum
                    needed = round(cash_before * conviction_pct, 2)
                    reason = (
                        f"Below ${MIN_POSITION_USD:.0f} minimum"
                        if cash_before > 0 else "No cash available"
                    )
                    log.info(f"  MISSED: {ticker} — {reason} "
                             f"(needed ${needed:.2f}, have ${cash_before:.2f})")
                    missed_today.append({
                        "ticker": ticker, "reason": reason,
                        "needed": max(needed, MIN_POSITION_USD),
                        "score": score_result["score"],
                    })
                    cache["missed_signals"].append({
                        "date": datetime.utcnow().isoformat(),
                        "ticker": ticker, "reason": reason,
                        "score": score_result["score"],
                    })
                    # Send missed signal email if score was high
                    if score_result["score"] >= 7:
                        trade_data = {
                            "ticker": ticker, "source_name": member["name"],
                            "trade_type": trade_type, "amount": amount_str,
                        }
                        send_email(
                            f"⚠️ Missed Signal — {ticker} score {score_result['score']}/10 "
                            f"· No cash · Day {days_into_test(cache)}/{TEST_PERIOD_DAYS}",
                            build_missed_signal_email(
                                trade_data, score_result, conviction_pct,
                                max(needed, MIN_POSITION_USD), cache
                            ),
                        )
                else:
                    cash_after = round(cash_before - allocation, 2)
                    summary    = (
                        f"Source: {member['name']} (Congressional)\n"
                        f"Ticker: {ticker}\nTrade type: {trade_type}\n"
                        f"Disclosed range: {amount_str}\nConviction: {int(conviction_pct*100)}%\n"
                        f"Trade date: {trade_date}\nDisclosed: {disclosed}\n"
                        f"Disclosure lag: {lag} days\n"
                        f"Other members on same ticker: "
                        f"{', '.join(congress_tickers[ticker])}"
                    )
                    analysis = get_ai_analysis(summary, score_result, allocation, cash_after)
                    order    = place_paper_buy(ticker, allocation)

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

                    trade_data = {
                        "ticker": ticker, "source_name": member["name"],
                        "trade_type": trade_type, "amount": amount_str,
                        "trade_date": trade_date, "disclosed_date": disclosed,
                    }
                    send_email(
                        f"⚡ [BUY] {ticker} — Score {score_result['score']}/10 · "
                        f"${allocation:.2f} · {int(conviction_pct*100)}% conviction · "
                        f"Day {days_into_test(cache)}/{TEST_PERIOD_DAYS}",
                        build_trade_alert_email(
                            trade_data, score_result, analysis, order,
                            conviction_pct, allocation, cash_before,
                            cash_after, positions, cache
                        ),
                    )
                    signals += 1

            elif score_result["action"] == "SELL":
                held = [p for p in positions if p.get("symbol") == ticker]
                if held:
                    val        = float(held[0].get("market_value", 0))
                    order      = place_paper_sell(ticker, val)
                    cash_after = round(cache.get("cash_balance", 0) + val, 2)
                    if "id" in order:
                        cache["cash_balance"] = cash_after
                        cache.get("positions", {}).pop(ticker, None)
                    summary  = (
                        f"SELL SIGNAL: {member['name']} sold {ticker}.\n"
                        f"We hold this position (value: ${val:.2f}).\n"
                        f"Trade type: {trade_type}\nLag: {lag} days"
                    )
                    conviction_pct = parse_conviction(amount_str)
                    analysis = get_ai_analysis(
                        summary, score_result, val, cash_after
                    )
                    trade_data = {
                        "ticker": ticker, "source_name": member["name"],
                        "trade_type": trade_type, "amount": amount_str,
                        "trade_date": trade_date, "disclosed_date": disclosed,
                    }
                    send_email(
                        f"🔴 [SELL] {ticker} — {member['name']} exited · "
                        f"Proceeds ${val:.2f} → cash ${cash_after:.2f} · "
                        f"Day {days_into_test(cache)}/{TEST_PERIOD_DAYS}",
                        build_trade_alert_email(
                            trade_data, score_result, analysis, order,
                            conviction_pct, val, cache.get("cash_balance", 0) - val,
                            cash_after, positions, cache
                        ),
                    )
                    signals += 1

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

            # Check overlap with today's congressional signals
            if congress_tickers:
                overlap_raw = gemini_call(
                    f"Which ONE of these tickers most commonly appears in "
                    f"{fund['name']}'s known portfolio: "
                    f"{', '.join(congress_tickers.keys())}? "
                    f"Reply with one ticker only or NONE.",
                    use_pro=False
                )
                overlap = overlap_raw.strip().upper().split()[0] if overlap_raw else "NONE"

                if overlap not in ("NONE", "UNKNOWN") and overlap in congress_tickers:
                    log.info(f"  Cross-tier signal: {overlap}")
                    score_result = score_signal(
                        overlap, "BUY", 45,
                        matching_congress=len(congress_tickers[overlap]),
                        matching_funds=1,
                    )
                    if score_result["action"] == "BUY":
                        conviction_pct = 0.40   # treat 13F cross-tier as medium conviction
                        cash_before    = cache.get("cash_balance", 0.0)
                        allocation     = calculate_allocation(conviction_pct, cash_before)

                        if allocation > 0:
                            cash_after = round(cash_before - allocation, 2)
                            summary    = (
                                f"Cross-tier signal: {fund['name']} 13F overlaps "
                                f"with congressional buys on {overlap}.\n"
                                f"Congress members on {overlap}: "
                                f"{', '.join(congress_tickers[overlap])}"
                            )
                            analysis = get_ai_analysis(
                                summary, score_result, allocation, cash_after
                            )
                            order = place_paper_buy(overlap, allocation)
                            if "id" in order:
                                cache["cash_balance"] = cash_after
                                cache["total_invested"] = round(
                                    cache.get("total_invested", 0) + allocation, 2
                                )
                            trade_data = {
                                "ticker": overlap,
                                "source_name": fund["name"],
                                "trade_type": "13F Cross-tier BUY",
                                "amount": "See 13F filing",
                                "trade_date": filing.get("filed_date", "?"),
                                "disclosed_date": filing.get("filed_date", "?"),
                            }
                            send_email(
                                f"⚡ [CROSS-TIER] {overlap} — Congress + {fund['name']} · "
                                f"${allocation:.2f} · Day {days_into_test(cache)}/{TEST_PERIOD_DAYS}",
                                build_trade_alert_email(
                                    trade_data, score_result, analysis, order,
                                    conviction_pct, allocation, cash_before,
                                    cash_after, positions, cache
                                ),
                            )
                            signals += 1

            cache["seen"].append(filing_id)
            save_cache(cache)

    # ── 5. Day 60 check ───────────────────────────────────────────────────────
    day_num = days_into_test(cache)
    if day_num >= TEST_PERIOD_DAYS:
        log.info("Day 60 reached — generating final verdict")
        positions = get_open_positions()
        verdict   = get_day60_verdict(cache, positions)
        send_email(
            f"🏁 Day 60 Final Report — "
            f"${sum(float(p.get('market_value',0)) for p in positions):.2f} portfolio value",
            build_day60_email(verdict, positions, cache),
        )

    # ── 6. Daily summary if no trade signals fired ────────────────────────────
    elif signals == 0:
        positions = get_open_positions()
        log.info("No signals today — sending daily summary")
        send_email(
            f"[Monitor] Day {day_num}/{TEST_PERIOD_DAYS} · No signals · "
            f"Cash ${cache.get('cash_balance',0):.2f} · "
            f"Portfolio ${sum(float(p.get('market_value',0)) for p in positions):.2f} · "
            f"{datetime.utcnow().strftime('%b %d')}",
            build_daily_summary_email(positions, cache, missed_today),
        )
    else:
        log.info(f"Run complete — {signals} signal(s) fired")

    save_cache(cache)
    log.info("Done.")


if __name__ == "__main__":
    main()
