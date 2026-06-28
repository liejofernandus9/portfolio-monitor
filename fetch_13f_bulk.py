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
# Numeric CIK as it appears in the 13F data sets (no leading zeros padding
# assumed here — we normalize both sides when matching).
TRACKED_MANAGERS = {
    "1336528": "Bill Ackman / Pershing Square",
    "1536411": "Stan Druckenmiller / Duquesne",
    "1067983": "Warren Buffett / Berkshire",
    "1135730": "Philippe Laffont / Coatue",
    "1656456": "David Tepper / Appaloosa",
    # Candidates confirmed ACTIVE from verify_candidates.py — add more as needed
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

OUTPUT_FILE = "13f_holdings_cache.json"
DOWNLOAD_DIR = "13f_bulk_temp"


def get_current_quarter_url() -> tuple[str, str]:
    """
    Compute the current quarter's bulk file URL based on today's date.
    SEC publishes 4x/year: files run Mar-May, Jun-Aug, Sep-Nov, Dec-Feb.
    Returns (url, filename).
    """
    today = date.today()
    year  = today.year

    # Determine which 3-month window we're in, per SEC's publish schedule
    if today.month in (3, 4, 5):
        start, end_month, end_day, end_year = f"01mar{year}", "may", 31, year
    elif today.month in (6, 7, 8):
        start, end_month, end_day, end_year = f"01jun{year}", "aug", 31, year
    elif today.month in (9, 10, 11):
        start, end_month, end_day, end_year = f"01sep{year}", "nov", 30, year
    else:  # Dec, Jan, Feb — spans into next year
        start_year = year if today.month == 12 else year - 1
        start = f"01dec{start_year}"
        end_year = year if today.month == 12 else year
        end_month, end_day = "feb", 28  # 28 is safe; SEC's naming doesn't adjust for leap years in filename

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

    # ── Step 6: write clean output JSON ───────────────────────────────────────
    output = {
        "quarter_file": filename,
        "fetched_at": datetime.utcnow().isoformat(),
        "managers": results,
        "unmatched_ciks": [TRACKED_MANAGERS[c] for c in missing_ciks],
    }

    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\n[info] Output written to {OUTPUT_FILE}", flush=True)
    print("=" * 70, flush=True)
    print("DONE", flush=True)
    print("=" * 70, flush=True)


if __name__ == "__main__":
    main()
