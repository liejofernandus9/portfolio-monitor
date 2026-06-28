"""
verify_candidates.py
=====================
Verifies a candidate list of fund managers against SEC EDGAR.

For each candidate CIK, checks:
  1. Does the CIK resolve to a real, named entity on EDGAR
  2. Have they filed a 13F-HR recently (within ~200 days = roughly 2 quarters)
  3. How many holdings + total portfolio value in their most recent filing
  4. Top 5 holdings by value, for a sanity check on "concentrated long book" fit

Outputs a clean report: CONFIRMED ACTIVE / STALE / NOT FOUND for each candidate,
plus the real data needed to seed the 30-day refresh roster logic.

Run via GitHub Actions — data.sec.gov is not reachable from most sandboxed
dev environments, but works fine from GitHub's runners.
"""

import requests
import time
from datetime import datetime

HEADERS = {"User-Agent": "PortfolioMonitor research@example.com"}

# ── Candidate pool ────────────────────────────────────────────────────────────
# CIKs marked None need to be looked up — the script will attempt a name search
# fallback, but EDGAR's full-text search for company names is unreliable for
# fuzzy matches, so some may come back NOT FOUND and need manual CIK lookup.
CANDIDATES = {
    "Dan Loeb / Third Point":            "0001040273",  # confirmed via web search
    "Jeff Smith / Starboard Value":       None,
    "Paul Singer / Elliott Management":   None,
    "Carl Icahn / Icahn Enterprises":     None,
    "Jeffrey Ubben / Inclusive Capital":  None,
    "David Einhorn / Greenlight Capital": None,
    "Chase Coleman / Tiger Global":       None,
    "Daniel Sundheim / D1 Capital":       None,
    "Lone Pine Capital":                  None,
    "Viking Global / Andreas Halvorsen":  None,
    "Whale Rock Capital":                 None,
    "Light Street Capital":               None,
    "Seth Klarman / Baupost Group":       None,
    "RTW Investments":                    None,
    "Baker Bros Advisors":                None,
    "Cathie Wood / ARK Investment Mgmt":  None,
    "Christopher Hansen / Valiant Capital":None,
    "Boaz Weinstein / Saba Capital":      None,
    "Mick McGuire / Marcato Capital":     None,
    "Glenn Greenberg / Brave Warrior":    None,
    "Ricky Sandler / Eminence Capital":   None,
    "Larry Robbins / Glenview Capital":   None,
}

# Current roster — included so the verification report shows everyone
# on equal footing, current + candidates
CURRENT_ROSTER = {
    "Bill Ackman / Pershing Square": "0001336528",
    "Stan Druckenmiller / Duquesne": "0001536411",
    "Warren Buffett / Berkshire":    "0001067983",
    "Philippe Laffont / Coatue":     "0001135730",  # FIXED — was 0001336920 (Leidos Holdings, wrong entity)
    "David Tepper / Appaloosa":      "0001656456",  # REPLACED Burry — Scion deregistered Nov 2025
}


def search_cik_by_name(name: str) -> str | None:
    """
    Attempt to resolve a CIK via EDGAR's company search JSON endpoint.
    This is a best-effort fallback — fuzzy name matching on EDGAR is
    unreliable, so results should be treated as suggestions, not facts.
    """
    # Strip the "/ Fund Name" suffix and just search the person/fund name
    search_term = name.split("/")[-1].strip() if "/" in name else name
    url = "https://www.sec.gov/cgi-bin/browse-edgar"
    params = {
        "action": "getcompany",
        "company": search_term,
        "type": "13F-HR",
        "dateb": "",
        "owner": "include",
        "count": "10",
        "output": "atom",
    }
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        # crude extraction of first CIK from the atom feed
        text = resp.text
        if "CIK=" in text:
            start = text.find("CIK=") + 4
            end = text.find("&", start)
            cik_candidate = text[start:end] if end > start else text[start:start+10]
            digits = "".join(c for c in cik_candidate if c.isdigit())
            if digits:
                return digits.zfill(10)
        return None
    except Exception as e:
        print(f"  Search failed for '{search_term}': {e}")
        return None


def verify_manager(name: str, cik: str | None, fetch_holdings: bool = True) -> dict:
    """
    Check a single manager against EDGAR. Returns a result dict with
    status, real company name on file, filing date, holdings count, and value.

    fetch_holdings=False skips the heavier holdings XML fetch entirely —
    use this to isolate whether a hang/slowdown is in the lightweight
    submissions API call or the heavier per-filing holdings fetch.
    """
    result = {
        "name": name,
        "cik": cik,
        "status": "NOT FOUND",
        "company_name_on_file": None,
        "most_recent_13f_date": None,
        "days_old": None,
        "holdings_count": None,
        "portfolio_value": None,
        "top_holdings": [],
        "note": "",
    }

    # If no CIK provided, attempt a name-based search
    if not cik:
        print(f"    [debug] No CIK on file — attempting name search...", flush=True)
        found_cik = search_cik_by_name(name)
        if found_cik:
            cik = found_cik
            result["cik"] = cik
            result["note"] = "CIK auto-resolved via name search — verify manually"
        else:
            result["note"] = "Could not auto-resolve CIK — needs manual lookup"
            return result

    # Pull submissions history (lightweight — should be fast)
    print(f"    [debug] Fetching submissions JSON for CIK {cik}...", flush=True)
    try:
        sub_url = f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json"
        resp = requests.get(sub_url, headers=HEADERS, timeout=10)
        print(f"    [debug] Submissions response: {resp.status_code}", flush=True)

        if resp.status_code == 404:
            result["note"] = f"CIK {cik} does not exist on EDGAR"
            return result
        resp.raise_for_status()
        data = resp.json()

        result["company_name_on_file"] = data.get("name", "UNKNOWN")

        forms   = data.get("filings", {}).get("recent", {}).get("form", [])
        dates   = data.get("filings", {}).get("recent", {}).get("filingDate", [])
        acc_nos = data.get("filings", {}).get("recent", {}).get("accessionNumber", [])

        most_recent_13f = None
        acc_no = None
        for form, date, acc in zip(forms, dates, acc_nos):
            if form == "13F-HR":
                most_recent_13f = date
                acc_no = acc
                break

        if not most_recent_13f:
            result["status"] = "NO 13F-HR FOUND"
            result["note"] = "Entity exists but has never filed 13F-HR (wrong entity, or files differently)"
            return result

        filed_date = datetime.strptime(most_recent_13f, "%Y-%m-%d")
        days_old   = (datetime.utcnow() - filed_date).days

        result["most_recent_13f_date"] = most_recent_13f
        result["days_old"] = days_old
        result["status"] = "ACTIVE" if days_old < 200 else "STALE"

        # Try to pull actual holdings from the filing for a sanity check
        if acc_no and fetch_holdings:
            print(f"    [debug] Fetching holdings for acc_no {acc_no}...", flush=True)
            time.sleep(0.5)
            holdings = fetch_13f_holdings_summary(cik, acc_no)
            result["holdings_count"]   = holdings.get("count")
            result["portfolio_value"]  = holdings.get("value")
            result["top_holdings"]     = holdings.get("top_holdings", [])
            print(f"    [debug] Holdings fetch complete: {holdings.get('count')} positions", flush=True)
        elif not fetch_holdings:
            print(f"    [debug] Skipping holdings fetch (disabled for this run)", flush=True)

    except Exception as e:
        result["note"] = f"Error: {e}"
        print(f"    [debug] Exception during verify: {e}", flush=True)

    return result


def fetch_13f_holdings_summary(cik: str, acc_no: str, retries: int = 2) -> dict:
    """
    Fetch the actual 13F information table XML and return a summary:
    holdings count, total value, top 5 by value.

    NOTE: 13F filings report nameOfIssuer + cusip, NOT a ticker field —
    the SEC schema has no ticker element. Top holdings are reported by
    issuer name; ticker resolution (where needed) happens elsewhere.

    Retries on 503 (EDGAR rate limiting / temporary unavailability) with
    increasing backoff, since hammering 28 managers back to back can
    trigger temporary throttling even at conservative request rates.
    """
    import re
    import xml.etree.ElementTree as ET

    acc_clean = acc_no.replace("-", "")
    index_url = (
        f"https://www.sec.gov/Archives/edgar/data/"
        f"{int(cik)}/{acc_clean}/{acc_clean}-index.htm"
    )
    out = {"count": 0, "value": 0, "top_holdings": []}

    for attempt in range(retries):
        try:
            idx = requests.get(index_url, headers=HEADERS, timeout=8)
            if idx.status_code == 503:
                wait = 3 * (attempt + 1)
                print(f"    [debug] 503 on index page, attempt {attempt+1}/{retries}, "
                      f"waiting {wait}s...")
                time.sleep(wait)
                continue
            idx.raise_for_status()
            break
        except requests.exceptions.HTTPError as e:
            if attempt == retries - 1:
                print(f"    [debug] Index page fetch failed after {retries} attempts: {e}")
                return out
            time.sleep(2 * (attempt + 1))
    else:
        return out

    try:
        xml_match = re.search(
            r'href="(/Archives/edgar/data/[^"]+(?:infotable|13f)[^"]*\.xml)"',
            idx.text, re.IGNORECASE
        )
        if not xml_match:
            xml_match = re.search(
                r'href="(/Archives/edgar/data/[^"]+\.xml)"',
                idx.text, re.IGNORECASE
            )
        if not xml_match:
            print(f"    [debug] No XML found in index for CIK {cik}, acc {acc_no}")
            return out

        xml_url = "https://www.sec.gov" + xml_match.group(1)
        time.sleep(1.0)  # space out the second request more conservatively

        for attempt in range(retries):
            xml_resp = requests.get(xml_url, headers=HEADERS, timeout=8)
            if xml_resp.status_code == 503:
                wait = 3 * (attempt + 1)
                print(f"    [debug] 503 on XML fetch, attempt {attempt+1}/{retries}, "
                      f"waiting {wait}s...")
                time.sleep(wait)
                continue
            xml_resp.raise_for_status()
            break
        else:
            return out

        root = ET.fromstring(xml_resp.content)
        holdings = []
        for info in root.findall(".//{*}infoTable"):
            name_el  = info.find("{*}nameOfIssuer")
            value_el = info.find("{*}value")
            cusip_el = info.find("{*}cusip")

            name  = name_el.text.strip() if name_el is not None and name_el.text else "?"
            value = float(value_el.text) if value_el is not None and value_el.text else 0
            cusip = cusip_el.text.strip() if cusip_el is not None and cusip_el.text else ""

            holdings.append({"name": name, "cusip": cusip, "value": value})

        out["count"] = len(holdings)
        out["value"] = sum(h["value"] for h in holdings)
        out["top_holdings"] = sorted(holdings, key=lambda x: x["value"], reverse=True)[:5]

        if out["count"] == 0:
            print(f"    [debug] XML parsed but 0 infoTable entries found for CIK {cik}")

    except Exception as e:
        print(f"    [debug] 13F XML parse failed for CIK {cik}: {e}")

    return out


def main():
    import sys
    print("=" * 70, flush=True)
    print("FUND MANAGER CANDIDATE VERIFICATION", flush=True)
    print(f"Run at: {datetime.utcnow().isoformat()} UTC", flush=True)
    print("=" * 70, flush=True)

    # Toggle: set to False to skip the heavier holdings fetch entirely and
    # ONLY do the lightweight filing-date check. Use this first to isolate
    # whether a hang is in the submissions API call or the holdings fetch.
    FETCH_HOLDINGS = True

    all_managers = {**CURRENT_ROSTER, **CANDIDATES}
    results = []

    print(f"\nVerifying {len(all_managers)} managers ({len(CURRENT_ROSTER)} current + "
          f"{len(CANDIDATES)} candidates)...", flush=True)
    print(f"Holdings fetch enabled: {FETCH_HOLDINGS}\n", flush=True)

    for idx, (name, cik) in enumerate(all_managers.items(), 1):
        is_current = name in CURRENT_ROSTER
        tag = "[CURRENT]" if is_current else "[CANDIDATE]"
        t0 = time.time()
        print(f"\n[{idx}/{len(all_managers)}] {tag} Checking: {name} "
              f"(started {datetime.utcnow().strftime('%H:%M:%S')} UTC)...", flush=True)

        result = verify_manager(name, cik, fetch_holdings=FETCH_HOLDINGS)
        result["is_current"] = is_current
        results.append(result)

        elapsed = time.time() - t0
        print(f"  [{idx}/{len(all_managers)}] Done in {elapsed:.1f}s", flush=True)

        status_icon = {
            "ACTIVE": "✅",
            "STALE": "⚠️",
            "NOT FOUND": "❌",
            "NO 13F-HR FOUND": "❌",
        }.get(result["status"], "❓")

        print(f"  {status_icon} Status: {result['status']}")
        if result["company_name_on_file"]:
            print(f"  Company on file: {result['company_name_on_file']}")
        if result["most_recent_13f_date"]:
            print(f"  Most recent 13F-HR: {result['most_recent_13f_date']} "
                  f"({result['days_old']} days ago)")
        if result["holdings_count"]:
            print(f"  Holdings: {result['holdings_count']} positions, "
                  f"${result['portfolio_value']:,.0f} thousand total value")
        if result["top_holdings"]:
            top_str = ", ".join(
                f"{h['name'][:20]} (${h['value']:,.0f}K)"
                for h in result["top_holdings"][:3]
            )
            print(f"  Top holdings: {top_str}")
        if result["note"]:
            print(f"  Note: {result['note']}")

        time.sleep(1.5)  # more conservative spacing — avoid tripping EDGAR's rate limit
                          # across 28 managers x 2 requests each in one run

    # ── Summary report ────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)

    active_current   = [r for r in results if r["is_current"] and r["status"] == "ACTIVE"]
    stale_current    = [r for r in results if r["is_current"] and r["status"] != "ACTIVE"]
    active_candidates = [r for r in results if not r["is_current"] and r["status"] == "ACTIVE"]
    failed_candidates = [r for r in results if not r["is_current"] and r["status"] != "ACTIVE"]

    print(f"\nCurrent roster — ACTIVE: {len(active_current)}/{len(CURRENT_ROSTER)}")
    for r in stale_current:
        print(f"  ⚠️ {r['name']}: {r['status']} — {r['note']}")

    print(f"\nCandidates — CONFIRMED ACTIVE: {len(active_candidates)}/{len(CANDIDATES)}")
    for r in active_candidates:
        val = f"${r['portfolio_value']:,.0f}K" if r['portfolio_value'] else "?"
        print(f"  ✅ {r['name']} (CIK {r['cik']}) — {r['holdings_count']} holdings, {val}")

    print(f"\nCandidates — FAILED VERIFICATION: {len(failed_candidates)}/{len(CANDIDATES)}")
    for r in failed_candidates:
        print(f"  ❌ {r['name']}: {r['status']} — {r['note']}")

    print("\n" + "=" * 70)
    print("Copy the ACTIVE candidates list above to build the verified longlist.")
    print("=" * 70)


if __name__ == "__main__":
    main()
