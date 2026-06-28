"""
fetch_13f_bulk.py
==================
Standalone script: downloads SEC's quarterly Form 13F bulk dataset,
extracts holdings for our tracked fund managers, and outputs a clean
local JSON file ready to feed into scoring/refresh logic.

This replaces the per-manager live scraping approach (which was hitting
503s from SEC's index.htm pages) with SEC's own recommended bulk-data
access method — same underlying data, no live per-filer requests.

Output: 13f_holdings_cache.json
  {
    "quarter_file": "01mar2026-31may2026_form13f.zip",
    "fetched_at": "...",
    "managers": {
      "<CIK>": {
        "name_on_file": "...",
        "accession_number": "...",
        "report_date": "...",
        "holdings": [
          {"name_of_issuer": "...", "cusip": "...", "value": 12345.0,
           "shares": 100, "ticker": "..." (if resolved)},
          ...
        ],
        "total_value": 123456.0,
        "holdings_count": 42
      },
      ...
    },
    "unmatched_ciks": [...]   # CIKs we asked for but found 0 rows for
  }

Run via GitHub Actions — file is ~95MB compressed, ~1GB+ uncompressed,
so this needs real disk space and should NOT run on every daily cycle —
intended to run once per quarter when a new file is published, or on
manual trigger.
"""

import os
import csv
import json
import zipfile
import requests
from datetime import datetime, date

HEADERS = {"User-Agent": "PortfolioMonitor research@example.com"}

# ── Our tracked managers — CIKs we want to extract from the bulk file ─────────
# IMPORTANT: SEC's 13F data sets store CIK zero-padded to 10 digits as a
# string (e.g. "0001336528"), confirmed via debug_cik_formats() output —
# raw unpadded CIKs will silently never match. Keys are normalized to that
# exact format via .zfill(10) so lookups against SUBMISSION/INFOTABLE rows
# work directly without per-comparison reformatting.
#
# Split explicitly into CURRENT (the live roster monitor.py actually trades
# signals from) vs CANDIDATES (confirmed-active managers being evaluated for
# a persistence-based swap). This split is explicit rather than inferred
# from dict order/position, since relying on "the first N entries" silently
# breaks if anyone reorders or edits this list later.
_RAW_CURRENT_ROSTER = {
    "1336528": "Bill Ackman / Pershing Square",
    "1536411": "Stan Druckenmiller / Duquesne",
    "1067983": "Warren Buffett / Berkshire",
    "1135730": "Philippe Laffont / Coatue",
    "1656456": "David Tepper / Appaloosa",
}

# Candidates confirmed ACTIVE from verify_candidates.py — add more as needed
_RAW_CANDIDATE_POOL = {
    "1040273": "Dan Loeb / Third Point",
    "1517137": "Jeff Smith / Starboard Value",
    "1167483": "Chase Coleman / Tiger Global",
    "1747057": "Daniel Sundheim / D1 Capital",
    "1061165": "Lone Pine Capital",
    "1387322": "Whale Rock Capital",
    "1569049": "Light Street Capital",
    "1493215": "RTW Investments",
    "1263508": "Baker Bros Advisors",
    "1452689": "Christopher Hansen / Valiant Capital",
    "1510281": "Boaz Weinstein / Saba Capital",
    "1553733": "Glenn Greenberg / Brave Warrior",
    "1107310": "Ricky Sandler / Eminence Capital",
    "1138995": "Larry Robbins / Glenview Capital",
}

CURRENT_ROSTER_CIKS = {cik.zfill(10) for cik in _RAW_CURRENT_ROSTER}
TRACKED_MANAGERS = {
    cik.zfill(10): name
    for cik, name in {**_RAW_CURRENT_ROSTER, **_RAW_CANDIDATE_POOL}.items()
}


# Duplicated from monitor.py — see note in main() on why this isn't imported.
# 13F filings report nameOfIssuer + cusip, never a ticker symbol directly,
# so this map resolves the company names we actually expect to see in our
# tracked managers' holdings to tradeable tickers.
ISSUER_NAME_TO_TICKER = {
    "AMAZON COM": "AMZN", "AMAZONCOM": "AMZN", "AMAZON COM INC": "AMZN",
    "MICROSOFT CORP": "MSFT", "MICROSOFT": "MSFT",
    "ALPHABET INC": "GOOGL", "ALPHABET INC-CL C": "GOOG", "ALPHABET INC-CL A": "GOOGL",
    "APPLE INC": "AAPL", "APPLE COMPUTER": "AAPL",
    "META PLATFORMS": "META", "META PLATFORMS INC": "META",
    "NVIDIA CORP": "NVDA", "NVIDIA CORPORATION": "NVDA",
    "BROADCOM INC": "AVGO", "BROADCOM": "AVGO",
    "TAIWAN SEMICONDUCTOR": "TSM", "TAIWAN SEMICONDUCTOR-SP ADR": "TSM",
    "MICRON TECHNOLOGY": "MU", "MICRON TECHNOLOGY INC": "MU",
    "UBER TECHNOLOGIES": "UBER", "UBER TECHNOLOGIES INC": "UBER",
    "ORACLE CORP": "ORCL", "ORACLE CORPORATION": "ORCL",
    "PALANTIR TECHNOLOGIES": "PLTR",
    "SALESFORCE INC": "CRM", "SALESFORCE COM": "CRM",
    "PALO ALTO NETWORKS": "PANW",
    "BANK OF AMERICA": "BAC", "BANK OF AMERICA CORP": "BAC",
    "AMERICAN EXPRESS": "AXP", "AMERICAN EXPRESS CO": "AXP",
    "COCA COLA": "KO", "COCA COLA CO": "KO",
    "CHEVRON CORP": "CVX", "CHEVRON CORPORATION": "CVX",
    "OCCIDENTAL PETROLEUM": "OXY",
    "MOODYS CORP": "MCO", "MOODY'S CORP": "MCO",
    "BROOKFIELD CORP": "BN", "BROOKFIELD": "BN",
    "RESTAURANT BRANDS INTL": "QSR", "RESTAURANT BRANDS": "QSR",
    "HILTON WORLDWIDE": "HLT", "HILTON WORLDWIDE HOLDINGS": "HLT",
    "CANADIAN PACIFIC": "CP", "CANADIAN PACIFIC KANSAS CITY": "CP",
    "NATERA INC": "NTRA",
    "INSMED INC": "INSM",
    "CAREDX INC": "CAI",
    "YPF SOCIEDAD ANONIMA": "YPF", "YPF SA": "YPF",
    "ISHARES MSCI BRAZIL": "EWZ",
    "STMICROELECTRONICS": "STM",
    "VISTRA CORP": "VST",
    "SANDISK CORP": "SNDK",
    "WHIRLPOOL CORP": "WHR",
    "ALIBABA GROUP": "BABA", "ALIBABA GROUP HOLDING": "BABA",
}

PERSISTENCE_FILE = "manager_performance_history.json"

# Manager must appear in 2+ prior cycles before being eligible to:
#   (a) trigger a swap as a candidate, OR
#   (b) be protected by persistence as a current manager.
# This is symmetric on purpose — a brand-new current manager (e.g. a
# recent replacement) gets the same 2-cycle grace period as a candidate
# before either side of a swap decision can be judged on persistence.
MIN_CYCLES_FOR_ELIGIBILITY = 2

ALPACA_API_KEY    = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "")
ALPACA_DATA_URL   = "https://data.alpaca.markets/v2/stocks"


def get_stock_prices_30d_ago_and_now(tickers: list[str]) -> dict:
    """
    Fetch current price and price ~30 days ago for a list of tickers via
    Alpaca's historical bars endpoint. Returns
    {ticker: {"then": float, "now": float}}.

    IMPORTANT — corrected after a real failed run (0/24 tickers resolved):
      1. The endpoint is a single multi-symbol GET at /v2/stocks/bars with
         a comma-separated `symbols` param — NOT /v2/stocks/{ticker}/bars
         per-symbol, which doesn't exist as a path and was silently
         failing every single call.
      2. Free/paper accounts need feed=iex explicitly; without it, requests
         can be rejected or return empty depending on default feed tier.
      3. The response shape is {"bars": {"AAPL": [...], "MSFT": [...]}} —
         a dict keyed by symbol, not a flat list — confirmed against
         Alpaca's own documented examples before writing this.

    Batches tickers (Alpaca allows multiple symbols per call) rather than
    one request per ticker, which is both correct and far fewer API calls.
    """
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        print("[warn] Alpaca credentials not set — skipping performance calc",
              flush=True)
        return {}

    headers = {
        "APCA-API-KEY-ID":     ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }
    from datetime import timedelta
    end   = datetime.utcnow()
    start = end - timedelta(days=35)  # small buffer before the 30d mark

    prices = {}
    # Batch in groups to keep individual request/response sizes reasonable
    BATCH_SIZE = 20
    for i in range(0, len(tickers), BATCH_SIZE):
        batch = tickers[i:i + BATCH_SIZE]
        try:
            resp = requests.get(
                f"{ALPACA_DATA_URL}/bars",
                params={
                    "symbols":   ",".join(batch),
                    "start":     start.strftime("%Y-%m-%d"),
                    "end":       end.strftime("%Y-%m-%d"),
                    "timeframe": "1Day",
                    "feed":      "iex",  # required on free/paper plans
                    "limit":     60,
                },
                headers=headers, timeout=15,
            )
            if resp.status_code != 200:
                print(f"    [warn] Alpaca bars request failed for batch "
                      f"{batch}: HTTP {resp.status_code} — {resp.text[:200]}",
                      flush=True)
                continue

            bars_by_symbol = resp.json().get("bars", {})
            for ticker, bars in bars_by_symbol.items():
                if not bars or len(bars) < 2:
                    continue
                prices[ticker] = {
                    "then": float(bars[0]["c"]),
                    "now":  float(bars[-1]["c"]),
                }

        except Exception as e:
            print(f"    [warn] Alpaca bars fetch exception for batch "
                  f"{batch}: {e}", flush=True)
            continue

        time.sleep(0.3)

    return prices


def compute_basket_return(holdings: list[dict], prices: dict,
                          manager_name: str = "") -> float | None:
    """
    Reconstruct a manager's weighted basket return over the trailing window,
    using each holding's disclosed dollar value as its weight.

    Returns None only if we have almost nothing to go on. NOTE: our
    ISSUER_NAME_TO_TICKER map is small (~37 entries) relative to how many
    distinct holdings large managers report (Buffett: 90, Coatue: 198,
    Saba: 390) — most holdings will NOT resolve to a ticker on any given
    manager. A strict percentage-of-total-holdings threshold (e.g. 20%)
    would fail almost every manager on every run given the map's current
    size. Instead we require a modest absolute minimum of resolved,
    meaningfully-weighted positions, and log the resolution rate plainly
    so a consistently low rate is visible rather than silently swallowed.
    """
    total_weight = 0.0
    weighted_return = 0.0
    resolved_count = 0
    total_disclosed_value = sum(h.get("value", 0) for h in holdings)

    for h in holdings:
        ticker = h.get("ticker")
        if not ticker or ticker not in prices:
            continue
        p = prices[ticker]
        if p["then"] <= 0:
            continue
        pct_return = (p["now"] - p["then"]) / p["then"]
        weight = h.get("value", 0)
        if weight <= 0:
            continue
        weighted_return += pct_return * weight
        total_weight += weight
        resolved_count += 1

    coverage_pct = (total_weight / total_disclosed_value * 100) if total_disclosed_value > 0 else 0

    print(f"    [perf] {manager_name}: resolved {resolved_count}/{len(holdings)} "
          f"holdings, covering {coverage_pct:.1f}% of disclosed portfolio value "
          f"by weight", flush=True)

    # Fail safe on a genuinely thin sample (almost nothing resolved) or
    # near-zero value coverage — but don't demand an unrealistic absolute
    # count given our map's current size. This threshold itself is a
    # judgment call, not a verified-correct number — revisit once the
    # issuer map is expanded and we can see real coverage rates across
    # several cycles.
    MIN_RESOLVED_HOLDINGS = 3
    MIN_VALUE_COVERAGE_PCT = 5.0

    if resolved_count < MIN_RESOLVED_HOLDINGS or coverage_pct < MIN_VALUE_COVERAGE_PCT:
        print(f"    [perf] {manager_name}: EXCLUDED — below minimum threshold "
              f"({MIN_RESOLVED_HOLDINGS} holdings / {MIN_VALUE_COVERAGE_PCT}% "
              f"coverage)", flush=True)
        return None

    return (weighted_return / total_weight) * 100  # as a percentage


def load_persistence_history() -> dict:
    if os.path.exists(PERSISTENCE_FILE):
        with open(PERSISTENCE_FILE) as f:
            return json.load(f)
    return {"cycles": []}  # list of {"cycle_date": ..., "scores": {cik: {...}}}


def save_persistence_history(history: dict):
    with open(PERSISTENCE_FILE, "w") as f:
        json.dump(history, f, indent=2)


def evaluate_swaps(history: dict, current_cycle_scores: dict,
                   current_roster_ciks: set) -> list[dict]:
    """
    Apply the persistence-based swap rule:
      - A manager/candidate needs MIN_CYCLES_FOR_ELIGIBILITY prior cycles
        of data (not counting this one) before being eligible on EITHER
        side of a swap decision.
      - A swap only fires if a candidate outranked a specific current
        manager in BOTH this cycle and the immediately preceding cycle.

    Returns a list of recommended swaps: [{"out": cik, "in": cik, "reason": ...}]
    Does not apply the swap automatically — surfaces it for review/logging,
    since this changes what monitor.py treats as the "current" roster.
    """
    prior_cycles = history.get("cycles", [])
    if len(prior_cycles) < 1:
        print("[info] No prior cycle history yet — first cycle, no swaps possible.",
              flush=True)
        return []

    last_cycle = prior_cycles[-1]["scores"]

    # Build cycle-count per CIK across all history (for bootstrap eligibility)
    cycle_counts = {}
    for cycle in prior_cycles:
        for cik in cycle["scores"]:
            cycle_counts[cik] = cycle_counts.get(cik, 0) + 1

    def is_eligible(cik: str) -> bool:
        return cycle_counts.get(cik, 0) >= MIN_CYCLES_FOR_ELIGIBILITY

    # Rank this cycle's scores (higher return = better rank)
    ranked_this_cycle = sorted(
        current_cycle_scores.items(),
        key=lambda kv: kv[1].get("return_30d", float("-inf")),
        reverse=True,
    )
    ranked_last_cycle = sorted(
        last_cycle.items(),
        key=lambda kv: kv[1].get("return_30d", float("-inf")),
        reverse=True,
    )
    rank_this = {cik: i for i, (cik, _) in enumerate(ranked_this_cycle)}
    rank_last = {cik: i for i, (cik, _) in enumerate(ranked_last_cycle)}

    swaps = []
    current_managers = [c for c in current_roster_ciks if c in current_cycle_scores]
    candidates        = [c for c in current_cycle_scores if c not in current_roster_ciks]

    for current_cik in current_managers:
        if not is_eligible(current_cik):
            print(f"[info] {current_cycle_scores[current_cik]['name']} not yet "
                  f"eligible to lose roster spot (only "
                  f"{cycle_counts.get(current_cik,0)} cycle(s) tracked) — protected.",
                  flush=True)
            continue

        worst_rank_this = rank_this.get(current_cik, -1)

        for cand_cik in candidates:
            if not is_eligible(cand_cik):
                continue
            if cand_cik not in last_cycle or current_cik not in last_cycle:
                continue  # can't confirm persistence without both cycles present

            outranked_this  = rank_this.get(cand_cik, 999) < worst_rank_this
            outranked_last  = rank_last.get(cand_cik, 999) < rank_last.get(current_cik, -1)

            if outranked_this and outranked_last:
                swaps.append({
                    "out": current_cik,
                    "out_name": current_cycle_scores[current_cik]["name"],
                    "in": cand_cik,
                    "in_name": current_cycle_scores[cand_cik]["name"],
                    "reason": (
                        f"{current_cycle_scores[cand_cik]['name']} outranked "
                        f"{current_cycle_scores[current_cik]['name']} in both "
                        f"this cycle and the prior cycle "
                        f"({current_cycle_scores[cand_cik]['return_30d']:.1f}% vs "
                        f"{current_cycle_scores[current_cik]['return_30d']:.1f}% "
                        f"this cycle)"
                    ),
                })

    return swaps


OUTPUT_FILE = "13f_holdings_cache.json"
DOWNLOAD_DIR = "13f_bulk_temp"


def get_current_quarter_url() -> tuple[str, str]:
    """
    Compute the most recently COMPLETED quarter's bulk file URL.

    SEC publishes 4x/year, shortly after each window closes:
      Dec-Feb window  -> published ~March
      Mar-May window  -> published ~June
      Jun-Aug window  -> published ~September
      Sep-Nov window  -> published ~December

    So "today" tells us which window most recently CLOSED, not the
    window we're currently inside of. E.g. on June 28, the Mar-May
    window closed May 31 and should now be published; the Jun-Aug
    window won't be published until ~September.

    Returns (url, filename).
    """
    today = date.today()
    year  = today.year

    if today.month in (3, 4, 5):
        # We're inside Mar-May -> most recent COMPLETED window is Dec-Feb
        start_year = year - 1
        start, end_month, end_day, end_year = f"01dec{start_year}", "feb", 28, year
    elif today.month in (6, 7, 8):
        # We're inside Jun-Aug -> most recent COMPLETED window is Mar-May
        start, end_month, end_day, end_year = f"01mar{year}", "may", 31, year
    elif today.month in (9, 10, 11):
        # We're inside Sep-Nov -> most recent COMPLETED window is Jun-Aug
        start, end_month, end_day, end_year = f"01jun{year}", "aug", 31, year
    else:  # Dec, Jan, Feb
        # We're inside Dec-Feb -> most recent COMPLETED window is Sep-Nov
        start_year = year if today.month == 12 else year - 1
        end_year   = start_year
        start, end_month, end_day = f"01sep{start_year}", "nov", 30

    filename = f"{start}-{end_day}{end_month}{end_year}_form13f.zip"
    url = f"https://www.sec.gov/files/structureddata/data/form-13f-data-sets/{filename}"
    return url, filename


def download_bulk_file(url: str, filename: str) -> str:
    """Download the quarterly ZIP. Returns local path. Skips if already cached."""
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    local_path = os.path.join(DOWNLOAD_DIR, filename)

    if os.path.exists(local_path):
        print(f"[info] Using cached download: {local_path} "
              f"({os.path.getsize(local_path)/1e6:.1f} MB)", flush=True)
        return local_path

    print(f"[info] Downloading {url} ...", flush=True)
    resp = requests.get(url, headers=HEADERS, stream=True, timeout=120)
    resp.raise_for_status()

    total = int(resp.headers.get("content-length", 0))
    downloaded = 0
    with open(local_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            f.write(chunk)
            downloaded += len(chunk)
            if total:
                pct = downloaded / total * 100
                print(f"\r[info] Downloaded {downloaded/1e6:.1f}/{total/1e6:.1f} MB "
                      f"({pct:.0f}%)", end="", flush=True)
    print(flush=True)
    print(f"[info] Download complete: {downloaded/1e6:.1f} MB", flush=True)
    return local_path


def extract_relevant_files(zip_path: str) -> dict:
    """
    Extract only the files we need from the ZIP (SUBMISSION + INFOTABLE)
    rather than unzipping everything, to save disk space and time.
    Returns dict of {filename: extracted_path}.
    """
    needed = {}
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        print(f"[info] ZIP contains {len(names)} files: {names}", flush=True)

        for name in names:
            upper = name.upper()
            if "SUBMISSION" in upper or "INFOTABLE" in upper:
                extract_path = os.path.join(DOWNLOAD_DIR, name)
                z.extract(name, DOWNLOAD_DIR)
                needed[upper.split(".")[0]] = extract_path
                print(f"[info] Extracted: {name}", flush=True)

    return needed


def load_tsv(path: str) -> list[dict]:
    """SEC's 13F data sets are tab-delimited .tsv files, not CSV."""
    rows = []
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            rows.append(row)
    return rows


# ── Debug helper ──────────────────────────────────────────────────────────────
def debug_cik_formats(submission_path: str, sample_ciks: list[str]):
    """
    Diagnostic: print how CIK actually appears in the SUBMISSION file,
    confirming exact format/padding/type before matching.
    """
    rows = load_tsv(submission_path)
    print(f"\n[debug] First 5 raw CIK values from SUBMISSION.tsv:")
    for row in rows[:5]:
        raw = row.get("CIK", "")
        print(f"  repr={raw!r} type=str len={len(raw)}")

    print(f"\n[debug] Searching for our known CIKs in any form...")
    all_ciks_seen = set(row.get("CIK", "") for row in rows)
    for target in sample_ciks:
        # Try exact, zero-padded to 10, and stripped-of-leading-zeros variants
        variants = {
            target,
            target.zfill(10),
            target.lstrip("0"),
            str(int(target)) if target.isdigit() else target,
        }
        matches = variants & all_ciks_seen
        print(f"  Target {target}: variants tried {variants} -> "
              f"{'FOUND: ' + str(matches) if matches else 'NOT FOUND in any form'}")


def main():
    print("=" * 70, flush=True)
    print("13F BULK DATA FETCH", flush=True)
    print(f"Run at: {datetime.utcnow().isoformat()} UTC", flush=True)
    print("=" * 70, flush=True)

    # ── Step 1: figure out which quarterly file we need ───────────────────────
    url, filename = get_current_quarter_url()
    print(f"\n[info] Target file: {filename}", flush=True)
    print(f"[info] URL: {url}", flush=True)

    # ── Step 2: download it (or use cache) ────────────────────────────────────
    try:
        zip_path = download_bulk_file(url, filename)
    except Exception as e:
        print(f"[error] Download failed: {e}", flush=True)
        print("[info] This may mean the current quarter's file isn't published "
              "yet — SEC publishes shortly after quarter-end. Falling back is "
              "not implemented yet; check sec.gov/data-research/sec-markets-data/"
              "form-13f-data-sets for the latest available file.", flush=True)
        return

    # ── Step 3: extract only SUBMISSION + INFOTABLE ───────────────────────────
    print(f"\n[info] Extracting relevant files from ZIP...", flush=True)
    extracted = extract_relevant_files(zip_path)

    submission_path = extracted.get("SUBMISSION")
    infotable_path  = extracted.get("INFOTABLE")

    if not submission_path or not infotable_path:
        print(f"[error] Could not find SUBMISSION/INFOTABLE in ZIP. "
              f"Found keys: {list(extracted.keys())}", flush=True)
        return

    # ── Step 4: load SUBMISSION to find each manager's latest accession # ─────
    print(f"\n[info] Loading SUBMISSION table...", flush=True)
    submissions = load_tsv(submission_path)
    print(f"[info] SUBMISSION has {len(submissions)} rows. "
          f"Sample columns: {list(submissions[0].keys()) if submissions else 'EMPTY'}",
          flush=True)

    # Diagnostic: confirm exact CIK format before attempting the match —
    # this caught a real mismatch (padding/type) on the previous run
    debug_cik_formats(submission_path, ["1336528", "1067983", "1536411"])

    # Map CIK -> most recent matching submission (by report date)
    cik_to_submission = {}
    for row in submissions:
        cik = str(row.get("CIK", "")).strip()
        if cik in TRACKED_MANAGERS:
            existing = cik_to_submission.get(cik)
            this_date = row.get("PERIODOFREPORT", "")
            if not existing or this_date > existing.get("PERIODOFREPORT", ""):
                cik_to_submission[cik] = row

    print(f"\n[info] Matched {len(cik_to_submission)}/{len(TRACKED_MANAGERS)} "
          f"tracked managers in this quarter's SUBMISSION table", flush=True)

    found_ciks = set(cik_to_submission.keys())
    missing_ciks = set(TRACKED_MANAGERS.keys()) - found_ciks
    if missing_ciks:
        print(f"[warn] No submission found this quarter for: "
              f"{[TRACKED_MANAGERS[c] for c in missing_ciks]}", flush=True)

    # ── Step 5: load INFOTABLE and pull holdings for matched accession #s ─────
    print(f"\n[info] Loading INFOTABLE (this may take a moment — large file)...",
          flush=True)

    target_accessions = {
        row["ACCESSION_NUMBER"]: cik
        for cik, row in cik_to_submission.items()
        if "ACCESSION_NUMBER" in row
    }

    results = {cik: {
        "name_on_file": TRACKED_MANAGERS[cik],
        "accession_number": cik_to_submission[cik].get("ACCESSION_NUMBER", ""),
        "report_date": cik_to_submission[cik].get("PERIODOFREPORT", ""),
        "holdings": [],
        "total_value": 0.0,
        "holdings_count": 0,
    } for cik in found_ciks}

    matched_rows = 0
    with open(infotable_path, "r", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            acc = row.get("ACCESSION_NUMBER", "")
            if acc not in target_accessions:
                continue

            cik = target_accessions[acc]
            try:
                value = float(row.get("VALUE", 0) or 0)
            except ValueError:
                value = 0.0
            try:
                shares = float(row.get("SSHPRNAMT", 0) or 0)
            except ValueError:
                shares = 0.0

            results[cik]["holdings"].append({
                "name_of_issuer": row.get("NAMEOFISSUER", "").strip(),
                "cusip": row.get("CUSIP", "").strip(),
                "value": value,
                "shares": shares,
            })
            results[cik]["total_value"] += value
            matched_rows += 1

    for cik in results:
        results[cik]["holdings_count"] = len(results[cik]["holdings"])
        results[cik]["holdings"].sort(key=lambda h: h["value"], reverse=True)

    print(f"\n[info] Matched {matched_rows} holding rows across "
          f"{len(found_ciks)} managers", flush=True)

    for cik, data in results.items():
        print(f"  {data['name_on_file']}: {data['holdings_count']} holdings, "
              f"${data['total_value']:,.0f} thousand total value", flush=True)

    # ── Step 6: resolve tickers and compute 30-day basket performance ─────────
    print(f"\n[info] Resolving tickers and computing 30-day performance...",
          flush=True)

    # Issuer-name-to-ticker resolution, duplicated from monitor.py rather
    # than imported — importing monitor.py directly would execute its
    # top-level os.environ["ALPACA_API_KEY"] etc. and crash this script
    # if those exact secrets aren't present in this job's environment.
    # Keeping this script genuinely standalone, as intended.
    def resolve_ticker_from_issuer(issuer_name: str) -> str | None:
        if not issuer_name:
            return None
        n = issuer_name.upper().strip()
        for suffix in [", INC.", " INC.", " INC", ", CORP.", " CORP.", " CORP",
                       ", CO.", " CO.", " CO", " LTD", " LLC", ", L.P.", " LP",
                       " PLC", " SA", " AG", " HOLDINGS", " HOLDING"]:
            if n.endswith(suffix):
                n = n[: -len(suffix)]
        n = n.replace(".", "").replace(",", "").strip()
        if n in ISSUER_NAME_TO_TICKER:
            return ISSUER_NAME_TO_TICKER[n]
        if issuer_name.upper().strip() in ISSUER_NAME_TO_TICKER:
            return ISSUER_NAME_TO_TICKER[issuer_name.upper().strip()]
        return None

    all_tickers_needed = set()
    for cik, data in results.items():
        for h in data["holdings"]:
            t = resolve_ticker_from_issuer(h["name_of_issuer"])
            h["ticker"] = t
            if t:
                all_tickers_needed.add(t)

    print(f"[info] Resolved tickers for performance calc across "
          f"{len(all_tickers_needed)} unique symbols", flush=True)

    prices = get_stock_prices_30d_ago_and_now(list(all_tickers_needed))
    print(f"[info] Fetched price data for {len(prices)}/{len(all_tickers_needed)} "
          f"tickers", flush=True)

    current_cycle_scores = {}
    for cik, data in results.items():
        ret = compute_basket_return(data["holdings"], prices, data["name_on_file"])
        current_cycle_scores[cik] = {
            "name": data["name_on_file"],
            "return_30d": ret,
        }
        if ret is not None:
            print(f"  {data['name_on_file']}: {ret:+.2f}% (30d basket return)",
                  flush=True)
        else:
            print(f"  {data['name_on_file']}: insufficient price data — "
                  f"excluded from this cycle's ranking", flush=True)

    # Drop entries with no resolvable return — can't rank what we can't measure
    current_cycle_scores = {
        cik: v for cik, v in current_cycle_scores.items()
        if v["return_30d"] is not None
    }

    # ── Step 7: persistence-based swap evaluation ─────────────────────────────
    print(f"\n[info] Loading performance history and evaluating swaps...",
          flush=True)
    history = load_persistence_history()

    current_roster_ciks = CURRENT_ROSTER_CIKS

    recommended_swaps = evaluate_swaps(history, current_cycle_scores, current_roster_ciks)

    if recommended_swaps:
        print(f"\n[info] {len(recommended_swaps)} swap(s) recommended:", flush=True)
        for s in recommended_swaps:
            print(f"  SWAP OUT: {s['out_name']} -> SWAP IN: {s['in_name']}", flush=True)
            print(f"    Reason: {s['reason']}", flush=True)
    else:
        print(f"\n[info] No swaps recommended this cycle (either no persistence "
              f"agreement, or managers still in bootstrap period).", flush=True)

    # Append this cycle to history and save
    history["cycles"].append({
        "cycle_date": datetime.utcnow().strftime("%Y-%m-%d"),
        "scores": current_cycle_scores,
    })
    save_persistence_history(history)
    print(f"\n[info] Performance history saved — {len(history['cycles'])} "
          f"total cycles tracked", flush=True)

    # ── Step 8: write clean output JSON ───────────────────────────────────────
    output = {
        "quarter_file": filename,
        "fetched_at": datetime.utcnow().isoformat(),
        "managers": results,
        "unmatched_ciks": [TRACKED_MANAGERS[c] for c in missing_ciks],
        "performance_scores": current_cycle_scores,
        "recommended_swaps": recommended_swaps,
    }

    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\n[info] Output written to {OUTPUT_FILE}", flush=True)
    print("=" * 70, flush=True)
    print("DONE", flush=True)
    print("=" * 70, flush=True)


if __name__ == "__main__":
    main()
