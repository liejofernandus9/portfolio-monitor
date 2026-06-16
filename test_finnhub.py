"""
test_finnhub.py
===============
Run this FIRST via GitHub Actions to verify:
1. Your Finnhub API key works at all (basic quote test)
2. Whether congressional-trading endpoint is accessible on free tier
3. What the exact response looks like

Add this to your repo, then create a test workflow to run it once.
"""

import os
import json
import requests

FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY", "")

if not FINNHUB_KEY:
    print("ERROR: FINNHUB_API_KEY environment variable not set")
    exit(1)

print(f"Key loaded: {FINNHUB_KEY[:4]}...{FINNHUB_KEY[-4:]} ({len(FINNHUB_KEY)} chars)")
print()

BASE = "https://finnhub.io/api/v1"
headers = {"X-Finnhub-Token": FINNHUB_KEY}   # header auth is more reliable than query param

# ── Test 1: Basic quote (definitely free tier) ────────────────────────────────
print("=" * 50)
print("TEST 1: Basic stock quote for AAPL (free tier)")
print("=" * 50)
try:
    resp = requests.get(f"{BASE}/quote", params={"symbol": "AAPL"}, headers=headers, timeout=10)
    print(f"Status: {resp.status_code}")
    print(f"Response: {resp.text[:300]}")
    if resp.status_code == 200:
        data = resp.json()
        print(f"✅ Current price: ${data.get('c', 'N/A')}")
    elif resp.status_code == 401:
        print("❌ 401 — API key is invalid or not being passed correctly")
        print("   Check your FINNHUB_API_KEY GitHub secret")
        exit(1)
except Exception as e:
    print(f"❌ Exception: {e}")
print()

# ── Test 2: Company news (free tier) ─────────────────────────────────────────
print("=" * 50)
print("TEST 2: Company news for AAPL (free tier)")
print("=" * 50)
try:
    resp = requests.get(
        f"{BASE}/company-news",
        params={"symbol": "AAPL", "from": "2026-06-01", "to": "2026-06-16"},
        headers=headers, timeout=10
    )
    print(f"Status: {resp.status_code}")
    data = resp.json() if resp.status_code == 200 else resp.text
    if isinstance(data, list):
        print(f"✅ Got {len(data)} news items")
    else:
        print(f"Response: {str(data)[:200]}")
except Exception as e:
    print(f"❌ Exception: {e}")
print()

# ── Test 3: Congressional trading — the key test ─────────────────────────────
print("=" * 50)
print("TEST 3: Congressional trading for NVDA")
print("=" * 50)
try:
    resp = requests.get(
        f"{BASE}/stock/congressional-trading",
        params={"symbol": "NVDA", "from": "2026-01-01", "to": "2026-06-16"},
        headers=headers, timeout=10
    )
    print(f"Status: {resp.status_code}")
    print(f"Raw response: {resp.text[:500]}")
    if resp.status_code == 200:
        data = resp.json()
        trades = data.get("data", [])
        print(f"✅ Got {len(trades)} congressional trades for NVDA")
        if trades:
            print(f"Sample trade: {json.dumps(trades[0], indent=2)}")
    elif resp.status_code == 401:
        print("❌ 401 — API key rejected for this endpoint")
        print("   This endpoint may require a different key format")
    elif resp.status_code == 403:
        print("❌ 403 — This endpoint is premium only on your plan")
    elif resp.status_code == 402:
        print("❌ 402 — Payment required — premium endpoint")
except Exception as e:
    print(f"❌ Exception: {e}")
print()

# ── Test 4: Insider transactions (alternative if congressional fails) ──────────
print("=" * 50)
print("TEST 4: Insider transactions for NVDA (free tier alternative)")
print("=" * 50)
try:
    resp = requests.get(
        f"{BASE}/stock/insider-transactions",
        params={"symbol": "NVDA"},
        headers=headers, timeout=10
    )
    print(f"Status: {resp.status_code}")
    if resp.status_code == 200:
        data = resp.json()
        tx = data.get("data", [])
        print(f"✅ Got {len(tx)} insider transactions for NVDA")
        if tx:
            print(f"Sample: {json.dumps(tx[0], indent=2)}")
    else:
        print(f"Response: {resp.text[:200]}")
except Exception as e:
    print(f"❌ Exception: {e}")

print()
print("Done. Share these results to determine next steps.")
