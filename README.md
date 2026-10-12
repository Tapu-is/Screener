# stk - personal NSE/BSE stock screener

A stock tool for your terminal, built for Termux (works anywhere with Python 3.9+).

- **Phase 1:** `stk quote` - latest price for any NSE/BSE stock.
- **Phase 2:** a local database of daily prices for the Nifty 500, with `update`, `history`, `info`, `find` and a watchlist.
- **Phase 3:** indicators (RSI, moving averages, MACD, 52-week, volume) and `stk screen`: built-in, custom and saved screens.

Prices come from Yahoo Finance and are end-of-day (stored) or delayed by a few minutes (`quote`). This is for information, not for fast trading decisions.

## Install on Termux

```sh
pkg install git
git clone https://github.com/<your-username>/<your-repo>.git stk
cd stk
sh setup.sh
stk doctor
```

## First-time setup (Phase 2)

```sh
stk init       # creates the database and downloads the Nifty 500 list
stk update     # first run downloads 2 years of prices (a few minutes, use Wi-Fi)
stk status     # check what is stored
```

If `stk init` can't reach NSE, download the Nifty 500 constituents CSV from niftyindices.com in your browser and run `stk init --file ~/storage/downloads/ind_nifty500list.csv`. Any CSV with a `Symbol` column works, so you can load your own list too.

## Daily use

```sh
stk update                  # after market close (3:45 PM IST): fetches only the new days
stk quote RELIANCE          # live-ish price card
stk quote TCS INFY HDFCBANK # compact table
stk history TCS -d 20       # last 20 sessions from the database (works offline)
stk history TCS --ohlc      # include open, high, low
stk info RELIANCE           # 52-week range, 1D/1W/1M/6M/1Y change, volume vs average
stk find tata               # search symbols and company names
stk watch add TCS INFY      # build a watchlist
stk watch                   # live prices for the whole watchlist
stk watch remove INFY
```

### Screens (Phase 3)

```sh
stk screen                       # list all screens
stk screen oversold              # run a built-in screen
stk screen --rsi-below 30 --above-sma 200          # your own filters
stk screen --where "rsi14<35" --where "close>sma200" --where "vol_ratio>=1.5"
stk screen oversold --industry bank --names        # narrow a screen, show company names
stk screen save mydip --rsi-below 30 --above-sma 200   # save your filters
stk screen mydip                 # run it later
stk screen show mydip            # see its definition
stk screen delete mydip
stk screen fields                # everything you can filter on
stk screen breakout --limit 100 --csv ~/breakouts.csv  # more rows, and export every match
```

Built-in screens: `oversold`, `overbought`, `golden-cross`, `death-cross`, `breakout`, `near-high`, `near-low`, `volume-surge`, `macd-bullish`, `macd-bearish`, `trend-up`, `gainers`, `losers`.

Filters you can use with `--where`: a field, a comparison (`< <= > >= == !=`) and a number or another field, for example `chg_1m<-5`, `close>sma50*1.02`, `industry~bank` (`~` means "contains"). Run `stk screen fields` for the full list. Stocks with an empty value for a field never match a condition on it (a stock with under 200 days of history has no 200-day average, so it is skipped by `--above-sma 200`).

Results are sorted by average traded value (most liquid first) unless the screen or `--sort` says otherwise. `stk info RELIANCE` now also shows RSI, position against the 50/200-day averages, crosses, MACD and volume.

How the numbers are defined: RSI is the 14-day Wilder RSI; moving averages are simple averages of closes; MACD is 12/26/9; a golden cross is the 50-day average moving above the 200-day (death cross is the reverse); the 52-week range uses the last 252 sessions; volume ratio compares the latest session with the average of the 20 before it; `breakout` means the close is above the highest high of the previous 251 sessions. Indicators are recalculated automatically by `stk update` and, if needed, by `stk screen`.

Screens flag candidates from end-of-day data. They are a starting point for your own research, not advice.

### More options

`stk quote RELIANCE --bse`, `stk quote 500325` (BSE code), `stk quote ^NSEI`, `stk quote RELIANCE --watch 30` (refresh every 30s), `stk update TCS` (one stock), `stk update --full` (re-download everything), `stk update --years 5`, `stk update --retry-missing` (ask again about stocks Yahoo had no data for), `stk probe` (diagnose Yahoo 429 errors).

## How the data is handled

- **First run:** 2 years of daily candles per stock. Each stock is saved as soon as it downloads, so Ctrl+C or a dropped connection loses nothing. Run `stk update` again to continue.
- **Daily runs:** only the days since the last stored date (with a 7-day overlap to pick up corrections). Stocks already current are skipped with no network call.
- **Splits and bonuses:** if one happened recently, that stock's history is re-downloaded in full so old prices are re-adjusted.
- **Only finished days are stored.** Before 3:45 PM IST on a weekday, today's still-forming candle is ignored.
- **Failures** (network problems, rate limits) are logged and shown in `stk status`; the rest carry on.
- **Stocks Yahoo doesn't know** (placeholders, renamed or very new listings) are marked on the first update and skipped for 30 days, so they don't show up as failures every time.
- Stored data is NSE (`.NS`). BSE-only stocks aren't covered yet.
- Exchange holidays aren't modelled. After a holiday `stk update` may re-check stocks and find nothing new, which is harmless.
- Data lives in `~/.local/share/stk/stk.db` (override with `STK_HOME`).

## Update the app

```sh
cd stk && git pull
```

## Tests

```sh
python -m unittest discover -s tests -v
```

## Roadmap

1. `quote` (done)
2. Database, Nifty 500 list, daily updater, history/info/find/watchlist (done)
3. Indicators and `stk screen` with built-in, custom and saved screens (done)
4. Telegram alerts and scheduled runs
5. Fundamentals (P/E, ROE) from a second data source
6. Streamlit web dashboard sharing the same core

## Disclaimer

Personal tool for information only. Not investment advice. Data can be delayed, incomplete or wrong.
