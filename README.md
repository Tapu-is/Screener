# stk - personal NSE/BSE stock screener

A stock tool for your terminal, built for Termux (works anywhere with Python 3.9+).

- **Phase 1:** `stk quote` - latest price for any NSE/BSE stock.
- **Phase 2:** a local database of daily prices for the Nifty 500, with `update`, `history`, `info`, `find` and a watchlist.

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

Other options: `stk quote RELIANCE --bse`, `stk quote 500325` (BSE code), `stk quote ^NSEI`, `stk quote RELIANCE --watch 30` (refresh every 30s), `stk update TCS` (one stock), `stk update --full` (re-download everything), `stk update --years 5`.

## How the data is handled

- **First run:** 2 years of daily candles per stock. Each stock is saved as soon as it downloads, so Ctrl+C or a dropped connection loses nothing. Run `stk update` again to continue.
- **Daily runs:** only the days since the last stored date (with a 7-day overlap to pick up corrections). Stocks already current are skipped with no network call.
- **Splits and bonuses:** if one happened recently, that stock's history is re-downloaded in full so old prices are re-adjusted.
- **Only finished days are stored.** Before 3:45 PM IST on a weekday, today's still-forming candle is ignored.
- **Failures** (delisted or renamed symbols, rate limits) are logged and shown in `stk status`; the rest carry on.
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
3. Indicators (RSI, SMA 50/200, MACD, volume spikes) and the `stk screen` command with saved screens
4. Telegram alerts and scheduled runs
5. Streamlit web dashboard sharing the same core

## Disclaimer

Personal tool for information only. Not investment advice. Data can be delayed, incomplete or wrong.
