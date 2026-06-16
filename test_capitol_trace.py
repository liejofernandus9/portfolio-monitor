"""
test_capitol_trace.py
======================
Tests Capitol Trace API specifically.
Checks:
  1. Authentication works
  2. Member lookup (free tier)
  3. Trades endpoint (may require paid tier)
  4. What the free tier actually returns for trades
"""

import os
import json
import requests

CAPITOL_TRACE_KEY = os.environ.get("CAPITOL_TRACE_KEY", "")

if not CAPITOL_TRACE_KEY:
    print("ERROR: CAPITOL_TRACE_KEY not set")
    exit(1)

print(f"Key loaded: {CAPITOL_TRACE_KEY[:6]}...{CAPITOL_TRACE_KEY[-4:]} ({len(CAPITOL_TRACE_KEY)} chars)")
print()

BASE = "https://api.capitoltrace.com/v1"
headers = {
    "Authorization": f"Bearer {CAPITOL_TRACE_KEY}",
    "Accept":        "application/json",
    "User-Agent":    "PortfolioMonitor/1.0",
}

TARGET_MEMBERS = ["Pelosi", "Gottheimer", "Crenshaw", "Rouzer", "Wyden"]

# ── Test 1: Basic connectivity ────────────────────────────────────────────────
print("=" * 55)
print("TEST 1: API connectivity — /v1/usage")
print("=" * 55)
try:
    resp = requests.get(f"{BASE}/usage", headers=headers, timeout=10)
    print(f"Status: {resp.status_code}")
    print(f"Response: {resp.text[:300]}")
    if resp.status_code == 401:
        print("❌ Key rejected — check CAPITOL_TRACE_KEY secret")
        exit(1)
    elif resp.status_code == 200:
        print("✅ API key valid")
except Exception as e:
    print(f"❌ {e}")
print()

# ── Test 2: Member search ─────────────────────────────────────────────────────
print("=" * 55)
print("TEST 2: Member lookup (free tier)")
print("=" * 55)
member_ids = {}
for name in TARGET_MEMBERS:
    try:
        resp = requests.get(
            f"{BASE}/members",
            params={"name": name, "limit": 5},
            headers=headers,
            timeout=10
        )
        print(f"\n  Search '{name}' → {resp.status_code}")
        if resp.status_code == 200:
            data    = resp.json()
            members = data.get("data", [])
            print(f"  ✅ Found {len(members)} member(s)")
            for m in members[:2]:
                bio_id = m.get("bioguide_id", m.get("id", ""))
                print(f"    {m.get('name','')} | ID: {bio_id} | {m.get('party','')} | {m.get('chamber','')}")
                if name.lower() in m.get("name","").lower() and bio_id:
                    member_ids[name] = bio_id
        elif resp.status_code == 402:
            print(f"  ❌ 402 — member search requires paid tier")
        elif resp.status_code == 403:
            print(f"  ❌ 403 — not authorized")
        else:
            print(f"  Response: {resp.text[:150]}")
    except Exception as e:
        print(f"  ❌ {e}")
print()

# ── Test 3: Trades endpoint ───────────────────────────────────────────────────
print("=" * 55)
print("TEST 3: Trades endpoint — /v1/members/:id/trades")
print("=" * 55)

# Try with found IDs first
for name, bio_id in member_ids.items():
    try:
        resp = requests.get(
            f"{BASE}/members/{bio_id}/trades",
            headers=headers,
            timeout=10
        )
        print(f"\n  {name} ({bio_id})/trades → {resp.status_code}")
        if resp.status_code == 200:
            trades = resp.json().get("data", [])
            print(f"  ✅ GOT {len(trades)} TRADES!")
            if trades:
                print(f"  Sample trade:")
                print(json.dumps(trades[0], indent=4)[:500])
        elif resp.status_code == 402:
            print(f"  ❌ 402 — trades require paid tier (Researcher $29/mo)")
        elif resp.status_code == 403:
            print(f"  ❌ 403 — not authorized for trades")
        else:
            print(f"  Response: {resp.text[:200]}")
    except Exception as e:
        print(f"  ❌ {e}")

# Also try generic trades endpoint if it exists
print()
for ep in ["/trades", "/congressional-trades", "/stock-trades"]:
    try:
        resp = requests.get(f"{BASE}{ep}", headers=headers, timeout=8)
        print(f"  {ep} → {resp.status_code}: {resp.text[:100]}")
    except Exception as e:
        print(f"  {ep} → Error: {e}")
print()

# ── Test 4: Executive orders (confirmed free) ─────────────────────────────────
print("=" * 55)
print("TEST 4: Executive orders — confirmed free endpoint")
print("=" * 55)
try:
    resp = requests.get(f"{BASE}/executive-orders", headers=headers, timeout=10)
    print(f"Status: {resp.status_code}")
    if resp.status_code == 200:
        data = resp.json()
        print(f"✅ Executive orders work — {len(data.get('data',[]))} results")
    else:
        print(f"Response: {resp.text[:200]}")
except Exception as e:
    print(f"❌ {e}")

print()
print("=" * 55)
print("Summary: check which tests showed ✅")
print("=" * 55)
