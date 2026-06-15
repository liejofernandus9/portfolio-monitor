"""
Congressional + Fund Manager Portfolio Monitor
===============================================
Data sources:
  - Quiver Quantitative API  → congressional trades (free, public)
  - SEC EDGAR API            → fund manager 13F filings (free, public)
  - Alpaca paper trading     → order execution
  - Gemini API               → AI analysis (free tier)
  - Gmail SMTP               → email alerts
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
# Hardcoded paper endpoint — avoids any secret formatting issues
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

# ── Portfolio config ──────────────────────────────────────────────────────────
TOTAL_BUDGET        = 500.00
SLOT_COUNT          = 5
SLOT_SIZE           = TOTAL_BUDGET / SLOT_COUNT   # $100 per slot
BUY_SCORE_THRESHOLD = 6
MAX_LAG_DAYS        = 30

# ── Targets ───────────────────────────────────────────────────────────────────
# Quiver Quant uses full names for congressional lookup
CONGRESS_TARGETS = [
    {"name": "Nancy Pelosi",    "quiver_name": "Nancy Pelosi"},
    {"name": "David Rouzer",    "quiver_name": "David Rouzer"},
    {"name": "Josh Gottheimer", "quiver_name": "Josh Gottheimer"},
    {"name": "Dan Crenshaw",    "quiver_name": "Dan Crenshaw"},
    {"name": "Ron Wyden",       "quiver_name": "Ron Wyden"},
]

FUND_TARGETS = [
    {"name": "Bill Ackman / Pershing Square", "cik": "0001336528"},
    {"name": "Michael Burry / Scion",         "cik": "0001649339"},
    {"name": "Stan Druckenmiller / Duquesne", "cik": "0001536411"},
    {"name": "Warren Buffett / Berkshire",    "cik": "0001067983"},
    {"name": "Philippe Laffont / Coatue",     "cik": "0001336920"},
]

CACHE_FILE = "seen_trades.json"

# ── Cache ─────────────────────────────────────────────────────────────────────

def load_cache() -> dict:
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE) as f:
            return json.load(f)
    return {
        "seen": [],
        "slots": {},
        "start_date": datetime.utcnow().isoformat()
    }


def save_cache(cache: dict):
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)


def days_since(date_str: str) -> int:
    try:
        return (datetime.utcnow() - datetime.strptime(date_str[:10], "%Y-%m-%d")).days
    except Exception:
        return 999


def days_into_test(cache: dict) -> int:
    try:
        start = datetime.fromisoformat(cache.get("start_date", datetime.utcnow().isoformat()))
        return (datetime.utcnow() - start).days
    except Exception:
        return 0


# ── Gemini ────────────────────────────────────────────────────────────────────

def gemini_call(prompt: str, use_pro: bool = False) -> str:
    url  = GEMINI_PRO_URL if use_pro else GEMINI_FLASH_URL
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 600, "temperature": 0.3},
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
    result = gemini_call(prompt, use_pro=False).strip().upper().split()[0] if gemini_call(prompt) else "UNKNOWN"
    return result if result.isalpha() and len(result) <= 5 else "UNKNOWN"


def get_ai_analysis(trade_summary: str, score_result: dict, slot_amount: float) -> str:
    prompt = f"""You are a concise investment analyst. A portfolio monitor detected this signal:

TRADE DETAILS:
{trade_summary}

SIGNAL SCORE: {score_result['score']}/10
REASONS: {', '.join(score_result['reasons'])}
ACTION: {score_result['action']}
ALLOCATION: ${slot_amount:.2f}

Write exactly 3 short paragraphs:
1. What this trade likely signals strategically
2. What a retail investor with ${slot_amount:.2f} should specifically do
3. Key risks to watch

Be direct. No disclaimers. No preamble."""
    result = gemini_call(prompt, use_pro=True)
    return result if result else "AI analysis unavailable — review signal manually."


# ── Quiver Quantitative — congressional trades ────────────────────────────────

def fetch_congressional_trades(politician_name: str) -> list:
    """
    Fetch recent congressional trades from Quiver Quantitative's public endpoint.
    Returns list of trade dicts.
    """
    # Quiver's public congressional trading endpoint (no API key required)
    url = "https://api.quiverquant.com/beta/live/congresstrading"
    headers = {
        "Accept":     "application/json",
        "User-Agent": "PortfolioMonitor/1.0",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        all_trades = resp.json()
        # Filter to just this politician, most recent 10
        filtered = [
            t for t in all_trades
            if politician_name.lower() in t.get("Representative", "").lower()
        ]
        return filtered[:10]
    except Exception as e:
        log.warning(f"Quiver fetch failed for {politician_name}: {e}")
        return []


# ── SEC EDGAR — fund manager 13F ──────────────────────────────────────────────

def fetch_fund_manager_trades(cik: str) -> list:
    headers = {"User-Agent": "PortfolioMonitor research@example.com"}
    url = f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json"
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        data    = resp.json()
        forms   = data.get("filings", {}).get("recent", {}).get("form", [])
        dates   = data.get("filings", {}).get("recent", {}).get("filingDate", [])
        acc_nos = data.get("filings", {}).get("recent", {}).get("accessionNumber", [])
        for form, date, acc in zip(forms, dates, acc_nos):
            if form == "13F-HR":
                return [{"type": "13F", "filed_date": date, "acc_number": acc, "cik": cik}]
        return []
    except Exception as e:
        log.warning(f"SEC EDGAR failed for CIK {cik}: {e}")
        return []


# ── Signal scoring ────────────────────────────────────────────────────────────

def score_signal(ticker: str, trade_type: str, lag_days: int,
                 matching_congress: int, matching_funds: int) -> dict:
    score   = 0
    reasons = []
    t_upper = trade_type.upper()

    if any(t in t_upper for t in ["BUY", "PURCHASE", "CALL"]):
        score += 3
        reasons.append("Strong buy-type signal (+3)")
    elif any(t in t_upper for t in ["SELL", "PUT"]):
        score -= 2
        reasons.append("Sell/put signal (−2)")
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
        reasons.append(f"Stale — {lag_days}d, likely priced in (−2)")

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
    elif score <= 5 and any(t in trade_type.upper() for t in ["SELL", "PUT"]):
        action = "SELL"

    return {"score": score, "action": action, "reasons": reasons}


# ── Alpaca ────────────────────────────────────────────────────────────────────

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


def place_paper_trade(ticker: str, side: str, dollar_amount: float) -> dict:
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
        log.info(f"Paper trade: {side.upper()} ${dollar_amount:.2f} of {ticker}")
        return result
    except Exception as e:
        log.error(f"Alpaca order failed for {ticker}: {e}")
        return {"error": str(e)}


# ── Slot manager ──────────────────────────────────────────────────────────────

def get_available_slot(cache: dict) -> float:
    return SLOT_SIZE if len(cache.get("slots", {})) < SLOT_COUNT else 0.0

def assign_slot(cache: dict, ticker: str, amount: float):
    cache.setdefault("slots", {})[ticker] = {
        "amount": amount, "entered": datetime.utcnow().isoformat()
    }

def free_slot(cache: dict, ticker: str):
    cache.setdefault("slots", {}).pop(ticker, None)


# ── Email ─────────────────────────────────────────────────────────────────────

def send_email(subject: str, html_body: str):
    msg            = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_ADDRESS
    msg["To"]      = NOTIFY_EMAIL
    msg.attach(MIMEText(html_body, "html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            s.sendmail(GMAIL_ADDRESS, NOTIFY_EMAIL, msg.as_string())
        log.info(f"Email sent: {subject}")
    except Exception as e:
        log.error(f"Gmail failed: {e}")


def build_alert_email(trade: dict, score_result: dict, ai_analysis: str,
                      order_result: dict, positions: list, cache: dict) -> str:
    action       = score_result["action"]
    score        = score_result["score"]
    color_map    = {"BUY": "#16a34a", "SELL": "#dc2626", "WATCH": "#d97706"}
    ac           = color_map.get(action, "#64748b")
    day_num      = days_into_test(cache)
    end_date     = (datetime.utcnow() + timedelta(days=60 - day_num)).strftime("%b %d, %Y")
    reasons_li   = "".join(f"<li>{r}</li>" for r in score_result["reasons"])
    order_status = "✅ Paper trade placed" if "id" in order_result else "⚠️ Skipped"
    order_id     = order_result.get("id", "N/A")

    pos_rows = ""
    for p in positions:
        pl    = float(p.get("unrealized_pl", 0))
        plpct = float(p.get("unrealized_plpc", 0)) * 100
        c     = "#16a34a" if pl >= 0 else "#dc2626"
        pos_rows += (
            f"<tr><td style='padding:6px 10px;font-weight:600'>{p['symbol']}</td>"
            f"<td style='padding:6px 10px'>${float(p['market_value']):.2f}</td>"
            f"<td style='padding:6px 10px;color:{c}'>${pl:+.2f} ({plpct:+.1f}%)</td></tr>"
        )

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
       background:#f8f7f4;margin:0;padding:20px;color:#1a1a1a}}
  .w{{max-width:600px;margin:0 auto}}
  .h{{background:#1a1a1a;color:#fff;padding:20px 24px;border-radius:10px 10px 0 0}}
  .b{{background:#fff;padding:24px;border:1px solid #e8e5e0}}
  .f{{background:#f1f0ed;padding:12px 24px;border-radius:0 0 10px 10px;
      font-size:12px;color:#888;border:1px solid #e8e5e0;border-top:none}}
  .badge{{display:inline-block;padding:4px 14px;border-radius:20px;
          font-weight:700;font-size:14px;color:#fff;background:{ac}}}
  .card{{background:#f8f7f4;border:1px solid #e8e5e0;border-radius:8px;
         padding:14px 16px;margin:12px 0}}
  .card h3{{margin:0 0 8px;font-size:11px;text-transform:uppercase;
            letter-spacing:.06em;color:#888}}
  .bar{{background:#f1f0ed;border-radius:8px;height:10px;overflow:hidden;margin:8px 0}}
  .fill{{height:100%;border-radius:8px;background:{ac};width:{score*10}%}}
  table{{width:100%;border-collapse:collapse;font-size:13px}}
  th{{text-align:left;padding:6px 10px;background:#f1f0ed;font-size:11px;
      text-transform:uppercase;letter-spacing:.06em;color:#888}}
  td{{border-bottom:1px solid #f1f0ed}}
</style></head><body><div class="w">
  <div class="h">
    <h1 style="margin:0;font-size:18px">📊 Portfolio Monitor Alert</h1>
    <p style="margin:4px 0 0;font-size:12px;color:#aaa">
      {datetime.utcnow().strftime('%B %d, %Y · %H:%M UTC')} ·
      Paper Trading · Day {day_num}/60 · Test ends {end_date}
    </p>
  </div>
  <div class="b">
    <div style="margin-bottom:20px">
      <span class="badge">{action}</span>
      <span style="font-size:22px;font-weight:600;margin-left:12px">
        {trade.get('ticker','?')}
      </span>
      <span style="font-size:14px;color:#888;margin-left:8px">
        {trade.get('politician_name','?')} · {trade.get('trade_type','?')}
      </span>
    </div>
    <div class="card">
      <h3>Signal Score</h3>
      <div style="font-size:28px;font-weight:700">{score}
        <span style="font-size:16px;color:#888">/10</span></div>
      <div class="bar"><div class="fill"></div></div>
      <ul style="margin:8px 0 0;padding-left:18px;font-size:13px;
                 color:#555;line-height:1.8">{reasons_li}</ul>
    </div>
    <div class="card">
      <h3>Trade Details</h3>
      <table>
        <tr><th>Field</th><th>Value</th></tr>
        <tr><td style="padding:6px 10px">Source</td>
            <td style="padding:6px 10px">{trade.get('source','?')}</td></tr>
        <tr><td style="padding:6px 10px">Trade date</td>
            <td style="padding:6px 10px">{trade.get('trade_date','?')}</td></tr>
        <tr><td style="padding:6px 10px">Disclosed</td>
            <td style="padding:6px 10px">{trade.get('disclosed_date','?')}</td></tr>
        <tr><td style="padding:6px 10px">Amount range</td>
            <td style="padding:6px 10px">{trade.get('amount','?')}</td></tr>
        <tr><td style="padding:6px 10px">Paper order</td>
            <td style="padding:6px 10px">{order_status} · {order_id}</td></tr>
      </table>
    </div>
    <div class="card">
      <h3>AI Analysis (Gemini Pro)</h3>
      <p style="font-size:14px;line-height:1.7;white-space:pre-wrap;color:#333;margin:0">
        {ai_analysis}
      </p>
    </div>
    <div class="card">
      <h3>Current Paper Positions</h3>
      {"<p style='font-size:13px;color:#888;margin:0'>No open positions yet.</p>"
       if not positions else
       f"<table><tr><th>Ticker</th><th>Value</th><th>P&L</th></tr>{pos_rows}</table>"}
    </div>
    <div style="background:#fffbeb;border:1px solid #fbbf24;border-radius:8px;
                padding:14px 16px;margin-top:12px">
      <strong>👤 Your action:</strong>
      <p style="margin:6px 0 0;font-size:13px;line-height:1.6">
        This is a <strong>paper trade only</strong>. No real money moved.
        After Day 60, if results beat QQQ, mirror this in your real brokerage.
      </p>
    </div>
  </div>
  <div class="f">
    Portfolio Monitor · Paper trading · 60-day validation · Real deployment after {end_date}
  </div>
</div></body></html>"""


def build_summary_email(positions: list, cache: dict) -> str:
    total_val = sum(float(p.get("market_value", 0)) for p in positions)
    total_pl  = sum(float(p.get("unrealized_pl", 0)) for p in positions)
    pl_color  = "#16a34a" if total_pl >= 0 else "#dc2626"
    day_num   = days_into_test(cache)

    pos_rows = ""
    for p in positions:
        pl    = float(p.get("unrealized_pl", 0))
        plpct = float(p.get("unrealized_plpc", 0)) * 100
        c     = "#16a34a" if pl >= 0 else "#dc2626"
        pos_rows += (
            f"<tr><td style='padding:6px 10px;font-weight:600'>{p['symbol']}</td>"
            f"<td style='padding:6px 10px'>${float(p['market_value']):.2f}</td>"
            f"<td style='padding:6px 10px;color:{c}'>${pl:+.2f} ({plpct:+.1f}%)</td></tr>"
        )

    return f"""<!DOCTYPE html><html><body
  style="font-family:-apple-system,sans-serif;background:#f8f7f4;
         padding:20px;color:#1a1a1a">
  <div style="max-width:520px;margin:0 auto">
    <div style="background:#1a1a1a;color:#fff;padding:16px 20px;
                border-radius:10px 10px 0 0">
      <h2 style="margin:0;font-size:16px">📈 Daily Summary — Day {day_num}/60</h2>
      <p style="margin:3px 0 0;font-size:12px;color:#aaa">
        {datetime.utcnow().strftime('%B %d, %Y')} · No new signals today
      </p>
    </div>
    <div style="background:#fff;padding:20px;border:1px solid #e8e5e0;
                border-radius:0 0 10px 10px">
      <div style="display:flex;gap:12px;margin-bottom:16px">
        <div style="flex:1;background:#f8f7f4;border-radius:8px;padding:12px">
          <div style="font-size:10px;color:#888;text-transform:uppercase;
                      letter-spacing:.06em">Portfolio</div>
          <div style="font-size:20px;font-weight:600">${total_val:.2f}</div>
        </div>
        <div style="flex:1;background:#f8f7f4;border-radius:8px;padding:12px">
          <div style="font-size:10px;color:#888;text-transform:uppercase;
                      letter-spacing:.06em">Unrealised P&L</div>
          <div style="font-size:20px;font-weight:600;color:{pl_color}">
            ${total_pl:+.2f}
          </div>
        </div>
        <div style="flex:1;background:#f8f7f4;border-radius:8px;padding:12px">
          <div style="font-size:10px;color:#888;text-transform:uppercase;
                      letter-spacing:.06em">Test progress</div>
          <div style="font-size:20px;font-weight:600">{day_num}/60</div>
        </div>
      </div>
      {"<p style='font-size:13px;color:#888'>No open positions. Watching for signals.</p>"
       if not positions else
       "<table style='width:100%;border-collapse:collapse;font-size:13px'>"
       "<tr style='background:#f1f0ed'>"
       "<th style='padding:6px 10px;text-align:left;font-size:11px;"
       "text-transform:uppercase;letter-spacing:.06em;color:#888'>Ticker</th>"
       "<th style='padding:6px 10px;text-align:left;font-size:11px;"
       "text-transform:uppercase;letter-spacing:.06em;color:#888'>Value</th>"
       "<th style='padding:6px 10px;text-align:left;font-size:11px;"
       "text-transform:uppercase;letter-spacing:.06em;color:#888'>P&L</th></tr>"
       f"{pos_rows}</table>"
      }
      <p style="margin-top:14px;font-size:12px;color:#888">
        Checked 5 congressional members + 5 fund managers.<br>
        No actionable signals today. Next check tomorrow at 9:45am ET.
      </p>
    </div>
  </div>
</body></html>"""


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log.info("=" * 60)
    log.info("Portfolio Monitor — daily run")
    log.info(datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"))
    log.info("=" * 60)

    cache     = load_cache()
    positions = get_open_positions()
    signals   = 0
    congress_tickers: dict = {}

    # ── Congressional trades via Quiver Quant ─────────────────────────────────
    for member in CONGRESS_TARGETS:
        log.info(f"Checking: {member['name']}")
        trades = fetch_congressional_trades(member["quiver_name"])
        time.sleep(1)

        for trade in trades:
            # Build a stable unique ID from key fields
            trade_id = f"{member['name']}-{trade.get('Ticker','')}-{trade.get('TransactionDate','')}"

            if trade_id in cache.get("seen", []):
                continue

            ticker     = str(trade.get("Ticker", "")).upper().strip()
            trade_type = str(trade.get("Transaction", "unknown"))
            trade_date = str(trade.get("TransactionDate", ""))
            disclosed  = str(trade.get("DisclosureDate", ""))
            amount     = str(trade.get("Range", "unknown"))

            if not ticker or not ticker.isalpha() or len(ticker) > 5:
                ticker = extract_ticker(json.dumps(trade)[:400])

            if not ticker or ticker == "UNKNOWN":
                cache["seen"].append(trade_id)
                continue

            lag = days_since(trade_date)
            congress_tickers.setdefault(ticker, [])
            congress_tickers[ticker].append(member["name"])

            if lag > MAX_LAG_DAYS:
                log.info(f"  Skipping stale: {ticker} ({lag}d)")
                cache["seen"].append(trade_id)
                continue

            log.info(f"  New: {member['name']} → {trade_type} {ticker} ({lag}d lag)")

            score_result = score_signal(
                ticker, trade_type, lag,
                matching_congress=len(congress_tickers[ticker]),
                matching_funds=0,
            )
            log.info(f"  Score: {score_result['score']}/10 → {score_result['action']}")

            if score_result["action"] == "BUY":
                slot = get_available_slot(cache)
                if slot == 0:
                    log.info(f"  All slots full — skipping {ticker}")
                    cache["seen"].append(trade_id)
                    save_cache(cache)
                    continue

                summary  = (
                    f"Politician: {member['name']}\nTicker: {ticker}\n"
                    f"Trade type: {trade_type}\nAmount: {amount}\n"
                    f"Trade date: {trade_date}\nDisclosed: {disclosed}\n"
                    f"Lag: {lag} days\n"
                    f"Others on same ticker: {', '.join(congress_tickers[ticker])}"
                )
                analysis = get_ai_analysis(summary, score_result, slot)
                order    = place_paper_trade(ticker, "buy", slot)
                if "id" in order:
                    assign_slot(cache, ticker, slot)

                trade_data = {
                    "ticker": ticker, "politician_name": member["name"],
                    "trade_type": trade_type, "trade_date": trade_date,
                    "disclosed_date": disclosed, "amount": amount,
                    "source": "Quiver Quantitative (Congressional)",
                }
                html = build_alert_email(
                    trade_data, score_result, analysis, order, positions, cache
                )
                send_email(
                    f"⚡ [{score_result['action']}] {ticker} — "
                    f"Score {score_result['score']}/10 · {member['name']} · "
                    f"Day {days_into_test(cache)}/60",
                    html,
                )
                signals += 1

            elif score_result["action"] == "SELL":
                held = [p for p in positions if p.get("symbol") == ticker]
                if held:
                    val      = float(held[0].get("market_value", 0))
                    order    = place_paper_trade(ticker, "sell", val)
                    free_slot(cache, ticker)
                    analysis = get_ai_analysis(
                        f"SELL: {member['name']} sold {ticker}. We hold this.",
                        score_result, val
                    )
                    trade_data = {
                        "ticker": ticker, "politician_name": member["name"],
                        "trade_type": trade_type, "trade_date": trade_date,
                        "disclosed_date": disclosed, "amount": amount,
                        "source": "Quiver Quantitative (Congressional)",
                    }
                    html = build_alert_email(
                        trade_data, score_result, analysis, order, positions, cache
                    )
                    send_email(
                        f"🔴 [SELL] {ticker} — {member['name']} exited · "
                        f"Day {days_into_test(cache)}/60",
                        html,
                    )
                    signals += 1

            cache["seen"].append(trade_id)
            save_cache(cache)

    # ── Fund manager 13F checks ───────────────────────────────────────────────
    for fund in FUND_TARGETS:
        log.info(f"Checking 13F: {fund['name']}")
        filings = fetch_fund_manager_trades(fund["cik"])
        time.sleep(1)

        for filing in filings:
            filing_id = filing.get("acc_number", "")
            if filing_id in cache.get("seen", []):
                continue

            log.info(f"  New 13F: {fund['name']} filed {filing.get('filed_date')}")

            if congress_tickers:
                ticker_list = ", ".join(congress_tickers.keys())
                overlap = gemini_call(
                    f"Which ONE of these tickers most commonly appears in "
                    f"{fund['name']}'s known portfolio: {ticker_list}? "
                    f"Reply with one ticker only or NONE.",
                    use_pro=False
                ).strip().upper().split()[0] if congress_tickers else "NONE"

                if (overlap and overlap not in ("NONE", "UNKNOWN")
                        and overlap in congress_tickers):
                    log.info(f"  Cross-tier overlap: {overlap}")
                    score_result = score_signal(
                        overlap, "BUY", 45,
                        matching_congress=len(congress_tickers[overlap]),
                        matching_funds=1,
                    )
                    if score_result["action"] == "BUY":
                        slot = get_available_slot(cache)
                        if slot > 0:
                            summary  = (
                                f"Cross-tier: {fund['name']} 13F overlaps "
                                f"with congressional buys on {overlap}"
                            )
                            analysis = get_ai_analysis(summary, score_result, slot)
                            order    = place_paper_trade(overlap, "buy", slot)
                            if "id" in order:
                                assign_slot(cache, overlap, slot)
                            trade_data = {
                                "ticker": overlap,
                                "politician_name": fund["name"],
                                "trade_type": "13F Cross-tier BUY",
                                "trade_date": filing.get("filed_date", "?"),
                                "disclosed_date": filing.get("filed_date", "?"),
                                "amount": "See 13F filing",
                                "source": "SEC EDGAR (Fund Manager 13F)",
                            }
                            html = build_alert_email(
                                trade_data, score_result, analysis,
                                order, positions, cache
                            )
                            send_email(
                                f"⚡ [CROSS-TIER] {overlap} — Congress + "
                                f"{fund['name']} · Day {days_into_test(cache)}/60",
                                html,
                            )
                            signals += 1

            cache["seen"].append(filing_id)
            save_cache(cache)

    # ── Daily summary if no signals ───────────────────────────────────────────
    positions = get_open_positions()
    if signals == 0:
        log.info("No signals — sending daily summary")
        html = build_summary_email(positions, cache)
        send_email(
            f"[Monitor] Day {days_into_test(cache)}/60 · No new signals · "
            f"Portfolio ${sum(float(p.get('market_value',0)) for p in positions):.2f} · "
            f"{datetime.utcnow().strftime('%b %d')}",
            html,
        )
    else:
        log.info(f"Done — {signals} signal(s) fired")

    save_cache(cache)


if __name__ == "__main__":
    main()
