"""stk web app (Streamlit). Run with:  streamlit run screener/web/app.py   (or: stk web)

All numbers and HTML come from data.py / tables.py / charts.py, so this file is
only layout and buttons.
"""

from __future__ import annotations

import sys
from pathlib import Path

# `streamlit run` runs this file as a plain script, so make the package importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import streamlit as st  # noqa: E402

from screener import __version__  # noqa: E402
from screener.core import alerts, db, fundamentals, indicators, notify, queries, screens, symbols, updater  # noqa: E402
from screener.core.quote import Quote  # noqa: E402
from screener.web import charts, data, tables  # noqa: E402

PAGES = ["Dashboard", "Stock", "Screener", "Watchlist", "Alerts", "Data"]
RANGES = {"3M": 63, "6M": 126, "1Y": 252, "2Y": 400}
LIVE_SECONDS = 5


def html(s: str) -> None:
    st.markdown(s, unsafe_allow_html=True)


def go_rerun() -> None:
    getattr(st, "rerun", None) and st.rerun()


# ------------------------------------------------------------------ dashboard
def live_block(conn) -> None:
    idx = data.index_quotes()
    if idx:
        html(tables.cards([(i["label"], f'{i["price"]:,.0f}', f'{i["change"]:+,.0f}  ({i["pct"]:+.2f}%)', data.pct_class(i["pct"])) for i in idx]))
    names = queries.watch_list(conn)
    if not names:
        st.caption("Your watchlist is empty. Add stocks on the Watchlist page.")
        return
    live = data.live_quotes(names)
    rows = []
    for r in data.watchlist_rows(conn, live):
        pct = r["pct"]
        rows.append([
            {"text": r["ticker"], "link": r["ticker"]},
            {"text": r["name"] or "-", "dim": True, "left": True},
            {"text": f'{r["price"]:,.2f}' if r["price"] is not None else "-"},
            {"text": f"{pct:+.2f}%" if pct is not None else "-", "cls": data.pct_class(pct)},
            {"html": charts.sparkline_svg(r["spark"]), "text": ""},
        ])
    html(tables.html_table(["Symbol", "Name", "Price", "Change", "30 days"], rows))
    any_live = any(isinstance(q, Quote) and q.source == "live" for q in live.values())
    st.caption("Live prices (can lag the exchange by a few minutes)." if any_live else "Showing last stored close. Live prices unavailable right now.")


def page_dashboard(conn) -> None:
    ms = data.market_status(conn)
    st.title("Dashboard")
    html(tables.pills([("● Market open" if ms["open"] else "● Market closed", "up" if ms["open"] else "down")]))
    if not ms["symbols"]:
        html(tables.banner("No stocks yet. Open the Data page and load the Nifty 500 list."))
        return
    if ms["behind"] or ms["indicators_stale"]:
        html(tables.banner(f'Stored prices are behind ({ms["behind"]} stocks). Open the Data page and update.'))

    live_on = st.sidebar.checkbox(f"Live refresh every {LIVE_SECONDS}s", value=ms["open"], key="live")
    def refreshing() -> None:  # its own connection: the fragment reruns on its own
        c = db.connect()
        try:
            live_block(c)
        finally:
            c.close()

    frag = getattr(st, "fragment", None)
    if frag:
        frag(run_every=LIVE_SECONDS if live_on else None)(refreshing)()
    else:
        refreshing()

    b = data.breadth(conn)
    if b:
        st.subheader("Market breadth")
        html(tables.cards([
            ("Advancing", b["advancing"], "", ""), ("Declining", b["declining"], "", ""),
            ("Oversold (RSI<30)", b["oversold"], "", ""), ("Overbought (RSI>70)", b["overbought"], "", ""),
            ("Volume spikes 2x+", b["volume_spikes"], "", ""), ("Near 52w high", b["near_high"], "", ""),
        ]))
        mv = data.movers(conn)
        left, right = st.columns(2)
        for col, title, key in ((left, "Top gainers", "gainers"), (right, "Top losers", "losers")):
            with col:
                st.subheader(title)
                rows = [[{"text": r["ticker"], "link": r["ticker"]}, {"text": f'{r["close"]:,.2f}'}, {"text": f'{r["chg_1d"]:+.1f}%', "cls": data.pct_class(r["chg_1d"])}] for r in mv[key]]
                html(tables.html_table(["Symbol", "Close", "1D"], rows))
        st.caption(f'Based on end-of-day data to {b["as_of"]}.')


# ---------------------------------------------------------------------- stock
def page_stock(conn) -> None:
    st.title("Stock")
    default = st.session_state.pop("stock_query", "") or ""
    text = st.text_input("Search by symbol or company name", value=default, placeholder="e.g. TCS or tata motors", key="stock_text").strip()
    if not text:
        st.caption("Type a symbol or a company name.")
        return
    hits = data.search(conn, text)
    if not hits:
        st.warning(f'Nothing matches "{text}" in your database.')
        return
    exact = [h for h in hits if h["ticker"].upper() == text.upper()]
    if exact:
        ticker = exact[0]["ticker"]
    elif len(hits) == 1:
        ticker = hits[0]["ticker"]
    else:
        ticker = st.selectbox("Matches", [h["ticker"] for h in hits], format_func=lambda t: next(f'{h["ticker"]}  {h["name"] or ""}' for h in hits if h["ticker"] == t))
    page = data.stock_page(conn, ticker, days=max(RANGES.values()))
    if page is None or not page["has_data"]:
        st.warning("No stored prices for this stock yet. Update it on the Data page.")
        return
    sym, ind = page["symbol"], page["indicators"]

    st.subheader(f'{sym["ticker"]}  ·  {sym["name"] or ""}')
    st.caption(sym["industry"] or "")
    live = data.live_quotes([sym["ticker"]]).get(sym["ticker"])
    last = page["bars"][-1]
    price, pct, tag = last["close"], (ind or {}).get("chg_1d"), f'close {last["date"]}'
    if isinstance(live, Quote):
        price, pct, tag = live.price, live.change_pct, "live" if live.source == "live" else "saved"
    html(tables.cards([("Price", f"₹{price:,.2f}", f'{pct:+.2f}% ({tag})' if pct is not None else tag, data.pct_class(pct))]))

    c1, c2 = st.columns([3, 1])
    with c2:
        if page["in_watchlist"]:
            if st.button("★ Remove from watchlist", key="wl_rm"):
                queries.watch_remove(conn, [sym["ticker"]])
                go_rerun()
        elif st.button("☆ Add to watchlist", key="wl_add"):
            queries.watch_add(conn, [sym["ticker"]])
            go_rerun()
    with c1:
        span = st.radio("Range", list(RANGES), index=1, horizontal=True, key="range")
    days = RANGES[span]
    bars = page["bars"][-days:]
    cut = len(page["bars"]) - len(bars)
    html(charts.candlestick_svg(bars, {k: v[cut:] for k, v in page["averages"].items()}))

    st_ = page["stats"]
    if st_:
        html(charts.range_bar_html(st_.low52, st_.high52, st_.close))

    left, right = st.columns(2)
    with left:
        st.subheader("Signals")
        notes = data.trend_notes(ind)
        html(tables.pills(notes) if notes else "<p>No indicators yet. Update prices on the Data page.</p>")
        if st_:
            rows = [(f"Change {k}", *data.fmt("chg_1d", v)) for k, v in st_.chg.items()]
            html(tables.kv_table(rows))
    with right:
        st.subheader("Fundamentals")
        rows = data.fundamentals_rows(page["fundamentals"])
        if rows:
            html(tables.kv_table(rows))
        else:
            st.caption("No fundamentals stored. Use the Data page to download them.")


# --------------------------------------------------------------------- screens
def page_screener(conn) -> None:
    st.title("Screener")
    if not conn.execute("SELECT 1 FROM prices_daily LIMIT 1").fetchone():
        st.warning("No prices stored yet. Use the Data page first.")
        return
    stale = indicators.stale_symbol_ids(conn)
    if stale:
        with st.spinner("Calculating indicators..."):
            indicators.refresh(conn)

    choices = data.screen_choices(conn)
    labels = {c["name"]: f'{c["name"]}  ({c["source"]})' for c in choices}
    name = st.selectbox("Screen", ["(none - my own filters)"] + list(labels), format_func=lambda n: labels.get(n, n))
    base = None
    if name in labels:
        base, _ = screens.get(conn, name)
        st.caption(base.get("description") or screens.describe(base))

    fields = data.numeric_fields() + ["industry"]
    rows = []
    with st.expander("Add your own filters", expanded=name not in labels):
        for i in range(4):
            a, b, c = st.columns([3, 1, 2])
            f = a.selectbox("Field", [""] + fields, key=f"f{i}", format_func=lambda x: "" if not x else f"{x}  ({data.field_label(x)})", label_visibility="collapsed")
            op = b.selectbox("Op", ["<", "<=", ">", ">=", "==", "!=", "contains"], index=2, key=f"o{i}", label_visibility="collapsed")
            v = c.text_input("Value", key=f"v{i}", placeholder="value", label_visibility="collapsed")
            if f and v.strip():
                rows.append((f, op, v))
        s1, s2, s3 = st.columns(3)
        sort = s1.selectbox("Sort by", ["(default)"] + data.numeric_fields(), key="sort")
        order = s2.selectbox("Order", ["(default)", "desc", "asc"], key="order")
        limit = s3.number_input("Rows", min_value=5, max_value=500, value=25, step=5, key="limit")
    if base is None and not rows:
        st.caption("Pick a screen above or add at least one filter.")
        return
    try:
        defn = data.build_definition(base, rows, None if sort == "(default)" else sort, None if order == "(default)" else order, int(limit))
        screens.validate(defn)
    except screens.ScreenError as exc:
        st.error(str(exc))
        return
    if screens.uses_fundamentals(defn) and fundamentals.coverage(conn)["with_pe"] == 0:
        st.warning("This screen needs fundamentals. Download them on the Data page first.")
        return
    res = screens.run(conn, defn)
    st.markdown(f"**{res.total}** match{'' if res.total == 1 else 'es'} · data to {res.as_of}")
    st.caption(screens.describe(defn))
    if res.total:
        t = data.screen_table(res)
        html(tables.html_table(t["columns"], t["rows"]))
        if res.total > len(res.rows):
            st.caption(f"Showing {len(res.rows)} of {res.total}. Raise 'Rows' or download the CSV.")
        st.download_button("Download all matches (CSV)", data.screen_csv(res), file_name="screen.csv", mime="text/csv")
    with st.expander("Save this screen"):
        n = st.text_input("Name (letters, numbers, - and _)", key="save_name")
        if st.button("Save", key="save_btn") and n:
            try:
                st.success(f"{screens.save(conn, n, defn)} screen {n.lower()}")
            except screens.ScreenError as exc:
                st.error(str(exc))


# ------------------------------------------------------------------- watchlist
def page_watchlist(conn) -> None:
    st.title("Watchlist")
    add = st.text_input("Add symbols (comma or space separated)", key="wl_text", placeholder="TCS, INFY, CUPID")
    if st.button("Add", key="wl_go") and add.strip():
        added = queries.watch_add(conn, add.replace(",", " ").split())
        st.success(f"Added: {', '.join(added)}" if added else "Already on your watchlist.")
    rows = data.watchlist_rows(conn)
    if not rows:
        st.caption("Nothing here yet.")
        return
    table = []
    for r in rows:
        pct = r["pct"]
        table.append([
            {"text": r["ticker"], "link": r["ticker"] if r["known"] else None},
            {"text": r["name"] or ("not in database" if not r["known"] else "-"), "dim": True, "left": True},
            {"text": f'{r["price"]:,.2f}' if r["price"] is not None else "-"},
            {"text": f"{pct:+.2f}%" if pct is not None else "-", "cls": data.pct_class(pct)},
            {"text": f'{r["rsi"]:.0f}' if r.get("rsi") is not None else "-"},
            {"html": charts.sparkline_svg(r["spark"]), "text": ""},
        ])
    html(tables.html_table(["Symbol", "Name", "Last close", "1D", "RSI", "30 days"], table))
    gone = st.multiselect("Remove", [r["ticker"] for r in rows], key="wl_rm_sel")
    if gone and st.button("Remove selected", key="wl_rm_go"):
        queries.watch_remove(conn, gone)
        go_rerun()


# ---------------------------------------------------------------------- alerts
def page_alerts(conn) -> None:
    st.title("Alerts")
    st.caption("Telegram: " + ("set up" if notify.configured() else "not set up (run `stk telegram setup` in a terminal)"))
    with st.form("alert_form"):
        a, b, c = st.columns([2, 2, 1])
        ticker = a.text_input("Symbol", placeholder="TCS")
        kind = b.selectbox("When", list(alerts.KINDS), format_func=lambda k: f"{k}: {alerts.KINDS[k][1]}")
        value = c.text_input("Value", placeholder="2200")
        if st.form_submit_button("Add alert"):
            try:
                alerts.add(conn, ticker, kind, value)
                st.success("Alert added")
            except alerts.AlertError as exc:
                st.error(str(exc))
    items = alerts.list_all(conn)
    if not items:
        st.caption("No alerts yet.")
        return
    rows = [[{"text": str(x["id"])}, {"text": alerts.describe(x)},
             {"text": "watching" if x["active"] else f'fired {(x["triggered_at"] or "")[:16]}', "cls": "up" if x["active"] else "warn"}] for x in items]
    html(tables.html_table(["ID", "Alert", "Status"], rows))
    c1, c2, c3 = st.columns(3)
    if c1.button("Check now"):
        with st.spinner("Checking..."):
            fired = alerts.check(conn)
        if fired:
            for f in fired:
                st.warning(f["message"])
            if notify.configured():
                try:
                    notify.send("stk alerts\n" + "\n".join(f"- {f['message']}" for f in fired))
                    st.caption("Sent to Telegram.")
                except notify.NotifyError as exc:
                    st.caption(f"Telegram: {exc}")
        else:
            st.info("No alerts fired.")
    ids = st.multiselect("Select alerts", [x["id"] for x in items], key="al_sel")
    if c2.button("Re-arm selected") and ids:
        alerts.reset(conn, ids)
        go_rerun()
    if c3.button("Delete selected") and ids:
        alerts.remove(conn, ids)
        go_rerun()


# ------------------------------------------------------------------------ data
def page_data(conn) -> None:
    st.title("Data")
    h = data.data_health(conn)
    s, f = h["status"], h["fundamentals"]
    html(tables.cards([
        ("Stocks", s["symbols"], f'{s["with_data"]} with prices', ""),
        ("Latest prices", s["latest_date"] or "-", f'expected {h["expected"]}', "up" if s["behind"] == 0 and s["latest_date"] else "warn"),
        ("Behind", s["behind"], "", "warn" if s["behind"] else ""),
        ("Fundamentals", f["stored"], f'{f["with_pe"]} with a P/E', ""),
    ]))
    st.caption(f"Database: {s['path']}")

    st.subheader("1. Stock list")
    if st.button("Download Nifty 500 list" if not s["symbols"] else "Refresh Nifty 500 list"):
        try:
            rows = symbols.parse_csv(symbols.download())
            added, updated = symbols.load(conn, rows, deactivate_missing=len(rows) >= 100)
            st.success(f"{len(rows)} symbols ({added} new)")
        except symbols.SymbolListError as exc:
            st.error(f"{exc}  You can also upload a CSV with a Symbol column below.")
    up = st.file_uploader("...or upload your own CSV (needs a Symbol column)", type="csv")
    if up is not None and st.button("Load uploaded list"):
        try:
            rows = symbols.parse_csv(up.getvalue().decode("utf-8-sig"))
            added, _ = symbols.load(conn, rows)
            st.success(f"{len(rows)} symbols ({added} new)")
        except symbols.SymbolListError as exc:
            st.error(str(exc))

    st.subheader("2. Prices")
    st.caption("The first run downloads 2 years for every stock and takes several minutes. Later runs only fetch what is new.")
    if st.button("Update prices"):
        bar = st.progress(0.0, text="Starting...")

        def tick(i: int, n: int, ticker: str) -> None:
            bar.progress(min(i / max(n, 1), 1.0), text=f"{ticker}  ({i}/{n})")

        res = updater.update(conn, progress=tick)
        indicators.refresh(conn)
        bar.progress(1.0, text="Done")
        st.success(f"Updated {res.ok} · already current {res.skipped} · failed {len(res.failures)}")
        if res.aborted:
            st.warning("The data source kept refusing, so the run stopped early. Wait a few minutes and try again.")
        for t, m in res.failures[:8]:
            st.caption(f"✗ {m}")

    st.subheader("3. Fundamentals")
    st.caption("P/E, ROE, margins, growth. Whole list takes about 7 minutes; once a week is enough.")
    scope = st.radio("For", ["My watchlist", "All stocks"], horizontal=True, key="fund_scope")
    if st.button("Download fundamentals"):
        names = None if scope == "All stocks" else queries.watch_list(conn)
        if names == []:
            st.warning("Your watchlist is empty.")
        else:
            bar = st.progress(0.0, text="Starting...")
            res = fundamentals.refresh(conn, names, progress=lambda i, n, t: bar.progress(min(i / max(n, 1), 1.0), text=f"{t}  ({i}/{n})"))
            bar.progress(1.0, text="Done")
            st.success(f"Downloaded {res.ok} · recent already {res.skipped} · no data at source {res.empty} · failed {len(res.failed)}")
            if res.aborted:
                st.warning("The data source kept refusing. Run the fundamentals check in a terminal: stk fundamentals probe")


# ------------------------------------------------------------------------ main
def main() -> None:
    st.set_page_config(page_title="stk", page_icon="📈", layout="wide")
    html(tables.CSS)
    qp = st.query_params
    if "page" not in st.session_state:
        if qp.get("stock"):
            st.session_state["page"] = "Stock"
            st.session_state["stock_query"] = qp.get("stock")
        else:
            st.session_state["page"] = "Dashboard"
    st.sidebar.markdown("### 📈 stk")
    st.sidebar.caption(f"NSE/BSE screener · v{__version__}")
    page = st.sidebar.radio("Go to", PAGES, key="page", label_visibility="collapsed")

    conn = db.connect()
    try:
        {
            "Dashboard": page_dashboard, "Stock": page_stock, "Screener": page_screener,
            "Watchlist": page_watchlist, "Alerts": page_alerts, "Data": page_data,
        }[page](conn)
    finally:
        conn.close()
    st.sidebar.caption("For information only, not investment advice. Prices can lag the exchange.")


if __name__ == "__main__":  # `streamlit run` executes the script as __main__
    main()
