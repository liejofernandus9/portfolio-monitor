name: Portfolio Monitor

on:
  schedule:
    # 9am ET weekdays (1pm UTC — accounts for ET = UTC-4 in summer)
    - cron: "0 13 * * 1-5"
  workflow_dispatch:
    # Allows you to manually trigger a run from the GitHub Actions tab anytime

jobs:
  run-monitor:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout repo
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Restore trade cache
        uses: actions/cache@v4
        with:
          path: seen_trades.json
          key: seen-trades-${{ runner.os }}
          restore-keys: |
            seen-trades-

      - name: Run monitor
        env:
          ALPACA_API_KEY:     ${{ secrets.ALPACA_API_KEY }}
          ALPACA_SECRET_KEY:  ${{ secrets.ALPACA_SECRET_KEY }}
          ALPACA_ENDPOINT:    ${{ secrets.ALPACA_ENDPOINT }}
          GMAIL_ADDRESS:      ${{ secrets.GMAIL_ADDRESS }}
          GMAIL_APP_PASSWORD: ${{ secrets.GMAIL_APP_PASSWORD }}
          NOTIFY_EMAIL:       ${{ secrets.NOTIFY_EMAIL }}
          GEMINI_API_KEY:     ${{ secrets.GEMINI_API_KEY }}
        run: python monitor.py

      - name: Save trade cache
        uses: actions/cache@v4
        with:
          path: seen_trades.json
          key: seen-trades-${{ runner.os }}
