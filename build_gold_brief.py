#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
The Gold Brief - dashboard builder (Step 3)

What it does, in plain English:
  1. Fetches fresh prices for Gold, Silver and Bitcoin (from Stooq, free, no key).
  2. Fetches recent headlines from quality news sources (RSS, free, no key).
  3. Rebuilds 'gold-brief.html' in this same folder so it's always fresh.

It uses ONLY Python's standard library. No installs, no subscriptions, no API
charges. If the internet is down or a source is quiet, it still builds the page
and just marks the missing bits as unavailable -- it never crashes.

Run it:   python build_gold_brief.py
Then open the gold-brief.html it prints, in your browser.
"""

import csv
import html
import io
import json
import os
import ssl
import sys
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

# ----------------------------------------------------------------------------
# CONFIG  -- the only part you'd ever tweak
# ----------------------------------------------------------------------------

OUTPUT_FILE = "gold-brief.html"   # written next to this script

# Yahoo Finance chart tickers (free, no key). GC=F gold futures, SI=F silver, BTC-USD bitcoin.
PRICE_SYMBOLS = [
    ("Gold",    "GC=F",    "$/oz"),
    ("Silver",  "SI=F",    "$/oz"),
    ("Bitcoin", "BTC-USD", "$"),
]

# Quality-source RSS feeds. If one ever goes quiet, the page still builds and
# you can swap the URL here. CNBC's feeds are the most reliable; the others are
# bonus gold/macro coverage.
NEWS_FEEDS = [
    ("CNBC Top News",  "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114"),
    ("CNBC Markets",   "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258"),
    ("Investing.com",  "https://www.investing.com/rss/news_11.rss"),
    ("FXStreet",       "https://www.fxstreet.com/rss/news"),
]

MAX_HEADLINES = 14
HTTP_TIMEOUT = 15  # seconds per request

# A small rotating "learn one thing" set -- one finance idea per day, plain English.
LEARN = [
    "Real interest rates (interest rates minus inflation) are gold's single biggest driver. Gold pays no interest, so when real rates rise, holding it 'costs' more and it tends to fall -- and vice-versa.",
    "Price reacts to the SURPRISE, not the headline. A 'strong' jobs number can lift gold if it was weaker than the market feared, because what's already expected is usually baked into the price.",
    "A stronger US dollar is usually a headwind for gold. Gold is priced in dollars, so when the dollar rises, gold gets pricier for buyers using other currencies, cooling demand.",
    "'Opportunity cost' is what you give up by holding gold instead of something that pays interest. When safe bonds pay a lot, that cost is high; when they pay little, gold looks more attractive.",
    "Risk-on vs risk-off: when investors feel brave (risk-on) money flows to stocks and crypto; when scared (risk-off) it often flows to safe havens like gold. Watch which mood the market is in.",
    "The Fed sits upstream of gold. Jobs and inflation data matter mainly because of how they change what the Fed is expected to do with interest rates next.",
    "Central banks have been big structural buyers of gold. This steady, price-insensitive demand can put a floor under the market even when traders are bearish.",
    "Silver tends to amplify gold's moves -- it's pulled by both safe-haven demand AND industrial growth, so it often swings harder in the same direction. A louder version of the same signal.",
    "CPI (Consumer Price Index) measures inflation. A hotter-than-expected reading usually means 'higher rates for longer', which is typically a headwind for gold.",
    "Macro sets the weather; your charts time the trade. News tells you the bias and which way the wind is blowing -- it doesn't tell you the exact entry.",
]


# ----------------------------------------------------------------------------
# Fetch helpers (robust: any failure returns None / [], never raises)
# ----------------------------------------------------------------------------

# Pretend to be a normal browser; some feeds reject the default Python agent.
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "Accept": "*/*",
}


def fetch(url):
    """Return decoded text for a URL, or None on any problem."""
    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=ctx) as r:
            raw = r.read()
        for enc in ("utf-8", "latin-1"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", "replace")
    except Exception as e:
        print("  ! could not fetch %s (%s)" % (url, e))
        return None


def get_prices():
    """Fetch Gold/Silver/Bitcoin from Yahoo Finance (Coinbase fallback for BTC)."""
    import urllib.parse
    out = []
    for name, sym, unit in PRICE_SYMBOLS:
        rec = {"name": name, "unit": unit, "price": None, "pct": None}
        url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
               + urllib.parse.quote(sym) + "?interval=1d&range=5d")
        text = fetch(url)
        if text:
            try:
                d = json.loads(text)
                meta = d["chart"]["result"][0]["meta"]
                price = meta.get("regularMarketPrice")
                prev = meta.get("chartPreviousClose") or meta.get("previousClose")
                if price is not None:
                    rec["price"] = float(price)
                    if prev:
                        rec["pct"] = (rec["price"] - float(prev)) / float(prev) * 100.0
            except Exception as e:
                print("  ! price parse failed for %s (%s)" % (sym, e))
        # Bulletproof fallback for Bitcoin if Yahoo is unavailable
        if rec["price"] is None and name == "Bitcoin":
            cb = fetch("https://api.coinbase.com/v2/prices/BTC-USD/spot")
            if cb:
                try:
                    rec["price"] = float(json.loads(cb)["data"]["amount"])
                except Exception:
                    pass
        out.append(rec)
    return out


def _text(elem):
    return (elem.text or "").strip() if elem is not None else ""


def get_news():
    """Fetch and merge headlines from the RSS feeds. Dedupe by title, newest first."""
    import xml.etree.ElementTree as ET
    items = []
    seen = set()
    for source, url in NEWS_FEEDS:
        text = fetch(url)
        if not text:
            continue
        try:
            root = ET.fromstring(text)
        except Exception as e:
            print("  ! could not parse feed %s (%s)" % (source, e))
            continue
        # Handle both RSS (<item>) and Atom (<entry>); ignore namespaces loosely.
        nodes = [el for el in root.iter() if el.tag.split("}")[-1] in ("item", "entry")]
        for n in nodes:
            title = link = pub = ""
            for c in n:
                tag = c.tag.split("}")[-1]
                if tag == "title" and not title:
                    title = _text(c)
                elif tag == "link" and not link:
                    link = _text(c) or c.attrib.get("href", "")
                elif tag in ("pubDate", "published", "updated") and not pub:
                    pub = _text(c)
            if not title or not link:
                continue
            key = title.lower()[:80]
            if key in seen:
                continue
            seen.add(key)
            when = None
            if pub:
                try:
                    when = parsedate_to_datetime(pub)
                except Exception:
                    try:
                        when = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                    except Exception:
                        when = None
            items.append({"source": source, "title": title, "link": link, "when": when})
    # newest first; normalize naive/aware datetimes to a comparable timestamp
    def _ts(it):
        dt = it["when"]
        if not dt:
            return 0.0
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        try:
            return dt.timestamp()
        except Exception:
            return 0.0
    items.sort(key=_ts, reverse=True)
    return items[:MAX_HEADLINES]


# ----------------------------------------------------------------------------
# HTML builder  (same warm, dark, editorial look as the original)
# ----------------------------------------------------------------------------

def domain_of(url):
    try:
        from urllib.parse import urlparse
        return urlparse(url).hostname.replace("www.", "")
    except Exception:
        return url


def fmt_price(p, unit):
    if p is None:
        return "&mdash;"
    if p >= 1000:
        return "${:,.0f}".format(p)
    return "${:,.2f}".format(p)


def fmt_pct(pct):
    if pct is None:
        return ("gb-flat", "&middot;")
    arrow = "\u25B2" if pct >= 0 else "\u25BC"
    cls = "gb-up" if pct >= 0 else "gb-down"
    return (cls, "%s %.2f%%" % (arrow, abs(pct)))


def build_html(prices, news, tip, built_at):
    e = html.escape

    price_cards = ""
    for p in prices:
        cls, pct_txt = fmt_pct(p["pct"])
        price_cards += (
            '<div class="gb-price">'
            '<div class="gb-pr-name">%s <span class="gb-pr-unit">%s</span></div>'
            '<div class="gb-pr-val">%s</div>'
            '<div class="gb-pr-chg %s">%s</div>'
            '</div>'
        ) % (e(p["name"]), e(p["unit"]), fmt_price(p["price"], p["unit"]), cls, pct_txt)

    if news:
        news_html = ""
        for it in news:
            when = ""
            if it["when"]:
                try:
                    when = it["when"].astimezone().strftime("%a %d %b, %H:%M")
                except Exception:
                    when = ""
            news_html += (
                '<a class="gb-src" href="%s" target="_blank" rel="noopener noreferrer">'
                '<span class="gb-src-domain">%s%s</span>'
                '<span class="gb-src-title">%s</span></a>'
            ) % (e(it["link"]), e(it["source"]),
                 (" &middot; " + e(when)) if when else "", e(it["title"]))
    else:
        news_html = ('<div class="gb-empty">No headlines came through this run '
                     '(internet down, or feeds quiet). Prices and your notes still work; '
                     'just run the script again later.</div>')

    out = TEMPLATE
    out = out.replace("__DATE__", e(datetime.now().strftime("%A, %d %B %Y")))
    out = out.replace("__BUILT__", e(built_at))
    out = out.replace("__PRICES__", price_cards)
    out = out.replace("__NEWS__", news_html)
    out = out.replace("__TIP__", e(tip))
    return out


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>The Gold Brief</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,500;9..144,600&family=Hanken+Grotesk:wght@400;500;600;700&display=swap');
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  --bg:#15110c; --bg2:#1c1711; --ink:#f0e7d6; --ink-soft:#b7ab95; --ink-faint:#8a7f6b;
  --gold:#d9b25a; --gold-deep:#b8923f; --line:rgba(240,231,214,0.09);
  --gold-soft:rgba(217,178,90,0.12); --up:#a6c98f; --down:#dd9572;
  min-height:100vh; background:var(--bg); color:var(--ink);
  font-family:'Hanken Grotesk',system-ui,-apple-system,sans-serif;
  position:relative; overflow-x:hidden; -webkit-font-smoothing:antialiased; }
.gb-glow { position:fixed; inset:0; pointer-events:none; z-index:0;
  background:radial-gradient(900px 500px at 75% -10%,rgba(217,178,90,0.10),transparent 60%),
             radial-gradient(700px 600px at 0% 100%,rgba(217,178,90,0.05),transparent 55%); }
.gb-wrap { position:relative; z-index:1; max-width:720px; margin:0 auto; padding:56px 24px 90px; }
.gb-kicker { font-size:11px; letter-spacing:0.28em; text-transform:uppercase; color:var(--gold);
  font-weight:600; margin-bottom:14px; display:flex; align-items:center; gap:9px; }
.gb-dot { width:6px; height:6px; border-radius:50%; background:var(--gold); }
.gb-title { font-family:'Fraunces',Georgia,serif; font-weight:600; font-size:46px; line-height:1.02; letter-spacing:-0.02em; }
.gb-title em { font-style:italic; color:var(--gold); }
.gb-sub { color:var(--ink-soft); font-size:15.5px; margin-top:12px; max-width:490px; line-height:1.55; }
.gb-date { font-size:13px; color:var(--ink-faint); letter-spacing:0.02em; margin-top:18px; }
.gb-rule { height:1px; background:linear-gradient(90deg,var(--gold-soft),transparent); margin:30px 0 0; }
.gb-label { font-size:11px; letter-spacing:0.22em; text-transform:uppercase; color:var(--ink-faint);
  font-weight:600; margin:40px 0 16px; }
.gb-card { background:var(--bg2); border:1px solid var(--line); border-radius:16px; padding:22px; }
.gb-prices { display:grid; grid-template-columns:repeat(3,1fr); gap:14px; }
.gb-price { background:var(--bg2); border:1px solid var(--line); border-radius:16px; padding:18px; }
.gb-pr-name { font-size:13px; color:var(--ink-soft); font-weight:600; }
.gb-pr-unit { color:var(--ink-faint); font-weight:400; }
.gb-pr-val { font-family:'Fraunces',Georgia,serif; font-size:26px; font-weight:500; margin-top:8px; letter-spacing:-0.01em; }
.gb-pr-chg { font-size:13px; font-weight:600; margin-top:4px; }
.gb-up { color:var(--up); } .gb-down { color:var(--down); } .gb-flat { color:var(--ink-faint); }
.gb-sources { display:flex; flex-direction:column; }
.gb-src { display:flex; flex-direction:column; gap:3px; padding:13px 0; border-bottom:1px solid var(--line);
  text-decoration:none; transition:opacity .15s ease; }
.gb-src:first-child { padding-top:0; } .gb-src:last-child { border-bottom:none; padding-bottom:0; }
.gb-src:hover { opacity:.72; }
.gb-src-domain { font-size:11px; letter-spacing:0.1em; text-transform:uppercase; color:var(--gold); font-weight:600; }
.gb-src-title { font-size:15px; color:var(--ink-soft); line-height:1.45; }
.gb-tip { background:linear-gradient(135deg,var(--gold-soft),rgba(217,178,90,0.03));
  border:1px solid var(--gold-soft); border-radius:16px; padding:22px 24px; }
.gb-tip-head { font-size:11px; letter-spacing:0.22em; text-transform:uppercase; color:var(--gold); font-weight:700; }
.gb-tip-body { font-family:'Fraunces',Georgia,serif; font-size:18px; line-height:1.5; margin-top:12px; color:var(--ink); }
.gb-journal { background:var(--bg2); border:1px solid var(--line); border-radius:16px; padding:24px; }
.gb-ta { width:100%; min-height:92px; resize:vertical; background:var(--bg); color:var(--ink);
  border:1px solid var(--line); border-radius:12px; padding:14px 16px; font-family:inherit; font-size:15px;
  line-height:1.55; outline:none; }
.gb-ta:focus { border-color:var(--gold-soft); }
.gb-ta::placeholder { color:var(--ink-faint); }
.gb-jrow { display:flex; justify-content:space-between; align-items:center; margin-top:14px; gap:12px; }
.gb-hint { font-size:12.5px; color:var(--ink-faint); line-height:1.5; }
.gb-save { font-family:inherit; font-size:14px; font-weight:600; color:#1a140b; background:var(--gold);
  border:none; border-radius:999px; padding:10px 22px; cursor:pointer; white-space:nowrap; }
.gb-save:hover { background:#e6c071; }
.gb-entry { padding:16px 0; border-bottom:1px solid var(--line); }
.gb-entry:last-child { border-bottom:none; padding-bottom:0; }
.gb-en-date { font-size:11px; letter-spacing:0.16em; text-transform:uppercase; color:var(--gold); font-weight:600; }
.gb-en-text { font-size:15px; color:var(--ink-soft); line-height:1.6; margin-top:6px; white-space:pre-wrap; }
.gb-del { background:none; border:none; color:var(--ink-faint); cursor:pointer; font-size:12px; padding:0; margin-top:8px; font-family:inherit; }
.gb-del:hover { color:var(--gold); }
.gb-empty { font-size:14px; color:var(--ink-faint); line-height:1.6; font-style:italic; }
.gb-foot { margin-top:48px; font-size:12px; color:var(--ink-faint); line-height:1.6; text-align:center; opacity:.8; }
@media (max-width:560px) { .gb-title { font-size:36px; } .gb-wrap { padding:40px 18px 70px; }
  .gb-prices { grid-template-columns:1fr; } }
</style>
</head>
<body>
<div class="gb-glow"></div>
<div class="gb-wrap">
  <div class="gb-kicker"><span class="gb-dot"></span> Markets &middot; Plain English</div>
  <h1 class="gb-title">The Gold <em>Brief</em></h1>
  <p class="gb-sub">Fresh prices and quality-source headlines, rebuilt on your own laptop. Write your read before you trade.</p>
  <div class="gb-date">__DATE__</div>
  <div class="gb-rule"></div>

  <div class="gb-label">Prices now</div>
  <div class="gb-prices">__PRICES__</div>

  <div class="gb-label">Latest from quality sources</div>
  <div class="gb-card gb-sources">__NEWS__</div>

  <div style="height:8px"></div>
  <div class="gb-tip"><div class="gb-tip-head">&#10022; Learn one thing</div><div class="gb-tip-body">__TIP__</div></div>

  <div class="gb-label">Your read</div>
  <div class="gb-journal">
    <textarea class="gb-ta" id="note" placeholder="Before you trade: where do you think gold goes next, and why? (one or two lines is plenty)"></textarea>
    <div class="gb-jrow">
      <span class="gb-hint">Saved on this device. Come back tomorrow and check yourself.</span>
      <button class="gb-save" onclick="saveNote()">Save note</button>
    </div>
    <div id="entries"></div>
  </div>

  <div class="gb-foot">Built __BUILT__ &middot; For learning and information only &mdash; not trading or financial advice.</div>
</div>

<script>
// Journal saved locally in your browser (works when opened straight from the file).
function esc(s){ return String(s==null?"":s).replace(/[&<>"']/g,function(c){return {"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c];}); }
function load(){ try { return JSON.parse(localStorage.getItem("goldbrief:entries")||"[]"); } catch(e){ return []; } }
function save(a){ try { localStorage.setItem("goldbrief:entries", JSON.stringify(a)); } catch(e){} }
var entries = load();
function render(){
  var c=document.getElementById("entries");
  if(!entries.length){ c.innerHTML='<div style="margin-top:20px"><span class="gb-empty">Your saved notes will appear here.</span></div>'; return; }
  var h='<div style="margin-top:24px;border-top:1px solid var(--line);padding-top:6px">';
  entries.forEach(function(en){ h+='<div class="gb-entry"><div class="gb-en-date">'+esc(en.date)+'</div><div class="gb-en-text">'+esc(en.text)+'</div><button class="gb-del" onclick="del('+en.id+')">Remove</button></div>'; });
  c.innerHTML=h+'</div>';
}
function saveNote(){
  var ta=document.getElementById("note"); var t=(ta.value||"").trim(); if(!t) return;
  entries.unshift({id:Date.now(), date:new Date().toLocaleDateString("en-US",{weekday:"long",year:"numeric",month:"long",day:"numeric"}), text:t});
  ta.value=""; save(entries); render();
}
function del(id){ entries=entries.filter(function(e){return e.id!==id;}); save(entries); render(); }
render();

// Hosted version: gently reload every 5 min to pick up the latest cloud build,
// but never while you're focused on or have text in the note box.
setInterval(function(){
  var ta=document.getElementById("note");
  var busy = ta && (document.activeElement===ta || (ta.value||"").trim()!=="");
  if(!busy){ location.reload(); }
}, 300000);
</script>
</body>
</html>
"""


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------

def main():
    here = os.path.dirname(os.path.abspath(__file__))
    out_path = os.path.join(here, OUTPUT_FILE)

    print("The Gold Brief - building your dashboard...")
    print("Fetching prices (gold / silver / bitcoin)...")
    prices = get_prices()
    print("Fetching headlines from quality sources...")
    news = get_news()

    tip = LEARN[datetime.now().timetuple().tm_yday % len(LEARN)]
    built_at = datetime.now().strftime("%A, %d %B %Y at %H:%M")

    html_out = build_html(prices, news, tip, built_at)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_out)

    got_prices = sum(1 for p in prices if p["price"] is not None)
    print("")
    print("Done. %d/%d prices, %d headlines." % (got_prices, len(prices), len(news)))
    print("Dashboard written to:")
    print("   " + out_path)
    print("Open that file in your browser (double-click it).")


if __name__ == "__main__":
    main()
