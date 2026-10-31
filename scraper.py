"""
EGX daily data scraper v2
=========================
Runs in a real Chromium (Playwright) so the EGX site's Imperva challenge
passes, then calls the site's own WebService.asmx endpoints from inside
the page context (same session cookies):

  - GetInvestorTables?Lang=ar&SB=<n>      buy/sell/net per nationality & group
        SB: 2 = stocks, 3 = bonds, 4 = T-bills  (1 = all, kept for continuity)
  - InvestorNatColumnChart?Lang=ar&SB=<n> trading share % per nationality

Outputs (all keyed by the Africa/Cairo date):
  - EGX_History.csv      v1-compatible wide table (SB=1 totals, by group)
  - EGX_Instruments.csv  long format per instrument (stocks/bonds/T-bills)
  - EGX_Indices.csv      EGX 30/70/100 closes + % change, CBE USD buy/sell

Sanity checks before anything is written:
  - per instrument, the three nationality nets must sum to ~0
  - every response must be a non-empty JSON array

Failures are retried (page reload + refetch) up to MAX_ATTEMPTS times;
if the EGX data still cannot be fetched the script exits non-zero and
no file is touched.
"""

import asyncio
import json
import os
import re
import sys
from datetime import datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import pandas as pd

BASE = "https://www.egx.com.eg"
PAGE = BASE + "/ar/InvestorsTypeCharts.aspx"
INDICES_PAGE = BASE + "/ar/Indices.aspx"
CBE_PAGE = "https://www.cbe.org.eg/ar/economic-research/statistics/cbe-exchange-rates"

CAIRO = ZoneInfo("Africa/Cairo")
MAX_ATTEMPTS = 3

GROUP_NAMES = {"1": "اجمالي", "2": "افراد", "3": "مؤسسات"}
INSTRUMENTS = {2: "اسهم", 3: "سندات", 4: "اذون خزانة"}
NAT_ORDER = ["مصريين", "عرب", "اجانب"]

HISTORY_CSV = "EGX_History.csv"
INSTRUMENTS_CSV = "EGX_Instruments.csv"
INDICES_CSV = "EGX_Indices.csv"

JS_FETCH = """
async (url) => { try { const r = await fetch(url, {
  method: "GET", credentials: "include",
  headers: {"Accept": "*/*", "X-Security-Request": "required",
            "X-Requested-With": "XMLHttpRequest"}
}); return {status: r.status, body: await r.text()};
} catch(e) { return {status: 0, body: "ERR:" + e.message}; } }
"""


def norm_type(t: str) -> str:
    return str(t).strip().replace("أجانب", "اجانب")


async def open_egx_page(browser):
    ctx = await browser.new_context(
        proxy=proxy_from_env(),
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
        viewport={"width": 1280, "height": 900}, locale="ar",
        extra_http_headers={"Accept-Language": "ar,en-US;q=0.9"},
    )
    page = await ctx.new_page()
    try:
        await page.goto(PAGE, wait_until="domcontentloaded", timeout=30000)
    except Exception:
        pass
    await asyncio.sleep(12)  # let the Imperva/TSPD challenge settle
    return ctx, page


async def fetch_json(page, path):
    res = await page.evaluate(JS_FETCH, BASE + path)
    body = (res or {}).get("body", "")
    if not body.strip().startswith("["):
        raise ValueError(f"Bad response from {path}: {body[:120]!r}")
    return json.loads(body)


def check_nets(rows, label):
    """The three nationality nets (total group) must cancel out."""
    nets = [float(r.get("Net", 0) or 0) for r in rows
            if str(r.get("Group", "")) == "1"]
    if len(nets) == 3 and abs(sum(nets)) > 1.0:
        raise ValueError(f"{label}: nets do not sum to zero: {nets}")


def parse_tables(rows):
    """Return {(group, nationality): (buy, sell, net)}."""
    out = {}
    for row in rows:
        grp = GROUP_NAMES.get(str(row.get("Group", "")), "?")
        typ = norm_type(row.get("Type", "?"))
        out[(grp, typ)] = (row.get("Buy", 0), row.get("Sell", 0),
                           row.get("Net", 0))
    return out


def parse_shares(rows):
    """InvestorNatColumnChart rows {Type, Buy, Sell} (Buy/Sell are % shares)
    -> {nationality: share %} using the average-of-buy/sell convention.
    Note: the chart endpoint spells Egyptians "مصرين", the tables "مصريين"."""
    out = {}
    for row in rows:
        typ = norm_type(row.get("Type", "?")).replace("مصرين", "مصريين")
        buy = float(row.get("Buy", 0) or 0)
        sell = float(row.get("Sell", 0) or 0)
        out[typ] = round((buy + sell) / 2, 2)
    return out


async def fetch_all(page):
    """Fetch every instrument table + shares; validate; return parsed data."""
    data = {}
    for sb in (1, 2, 3, 4):
        rows = await fetch_json(
            page, f"/WebService.asmx/GetInvestorTables?Lang=ar&SB={sb}")
        if not rows:
            raise ValueError(f"SB={sb}: empty table")
        check_nets(rows, f"SB={sb}")
        data[sb] = parse_tables(rows)
    shares = {}
    for sb in (2, 3, 4):
        try:
            srows = await fetch_json(
                page, f"/WebService.asmx/InvestorNatColumnChart?Lang=ar&SB={sb}")
            shares[sb] = parse_shares(srows)
        except Exception as e:
            print(f"share % unavailable for SB={sb}: {e}")
            shares[sb] = {}
    return data, shares


# ---------- indices & CBE (best effort: blanks if a source fails) ----------

async def fetch_indices(ctx):
    """EGX 30/70/100 close + daily % from the site's Indices page, which
    renders each index as: EGX 30 ... القيمة : 49596.14 ... التغير : 0.64 %"""
    out = {"EGX30": None, "EGX30_pct": None, "EGX70": None, "EGX70_pct": None,
           "EGX100": None, "EGX100_pct": None}
    page = await ctx.new_page()
    try:
        await page.goto(INDICES_PAGE, wait_until="domcontentloaded",
                        timeout=30000)
        await asyncio.sleep(6)
        txt = await page.inner_text("body")
        for label, key in (("EGX 30", "EGX30"), ("EGX 70", "EGX70"),
                           ("EGX 100", "EGX100")):
            i = txt.find(label)
            if i < 0:
                continue
            window = txt[i:i + 300]
            mv = re.search(r"القيمة\s*:\s*([\d,]+\.?\d*)", window)
            mc = re.search(r"التغير\s*:\s*(-?[\d.]+)\s*%", window)
            if mv:
                out[key] = float(mv.group(1).replace(",", ""))
            if mc:
                out[key + "_pct"] = float(mc.group(1))
    except Exception as e:
        print(f"indices unavailable: {e}")
    finally:
        await page.close()
    return out


async def fetch_cbe(ctx):
    """Official USD buy/sell from the CBE rates page (browser session only)."""
    out = {"USD_buy": None, "USD_sell": None, "USD_date": None}
    page = await ctx.new_page()
    try:
        await page.goto(CBE_PAGE, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(4)
        txt = await page.inner_text("body")
        m = re.search(r"الأسعار لتاريخ:\s*([0-9/]+)", txt)
        if m:
            out["USD_date"] = m.group(1)
            m = re.search(r"دولار أمريكى\s+([\d.]+)\s+([\d.]+)", txt)
            if m:
                out["USD_buy"] = float(m.group(1))
                out["USD_sell"] = float(m.group(2))
    except Exception as e:
        print(f"CBE rate unavailable: {e}")
    finally:
        await page.close()
    return out


# ---------- writers ----------

def upsert_csv(path, new_rows, key_cols, sort_cols):
    df_new = pd.DataFrame(new_rows)
    if os.path.exists(path):
        df_old = pd.read_csv(path, encoding="utf-8-sig")
        mask = pd.Series(True, index=df_old.index)
        for kc in key_cols:
            mask &= df_old[kc].astype(str) == str(new_rows[0][kc])
        df_all = pd.concat([df_old[~mask], df_new], ignore_index=True)
    else:
        df_all = df_new
    df_all = df_all.sort_values(sort_cols).reset_index(drop=True)
    df_all.to_csv(path, index=False, encoding="utf-8-sig")
    return df_all


def history_row(data_all, today):
    rec = {"تاريخ": today}
    for (grp, typ), (buy, sell, net) in data_all.items():
        rec[f"{grp}_{typ}_شراء"] = buy
        rec[f"{grp}_{typ}_بيع"] = sell
        rec[f"{grp}_{typ}_صافي"] = net
    groups = ["اجمالي", "افراد", "مؤسسات"]
    metrics = ["شراء", "بيع", "صافي"]
    ordered = ["تاريخ"] + [f"{g}_{t}_{m}" for g in groups
                           for t in NAT_ORDER for m in metrics]
    ordered = [c for c in ordered if c in rec] + [c for c in rec
                                                  if c not in ordered]
    return {c: rec[c] for c in ordered}


def instrument_rows(data, shares, today):
    rows = []
    for sb, name in INSTRUMENTS.items():
        for (grp, typ), (buy, sell, net) in sorted(data[sb].items()):
            rows.append({
                "تاريخ": today, "أداة": name, "مجموعة": grp, "جنسية": typ,
                "شراء": buy, "بيع": sell, "صافي": net,
                "نسبة_من_التداول": shares.get(sb, {}).get(typ, ""),
            })
    return rows


def proxy_from_env():
    """Use HTTPS_PROXY when present (e.g. behind a corporate proxy);
    returns None on plain connections such as GitHub Actions runners."""
    raw = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
    if not raw:
        return None
    u = urlparse(raw)
    proxy = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
    if u.username:
        proxy["username"] = u.username
    if u.password:
        proxy["password"] = u.password
    return proxy


async def run_diag(browser):
    """TEMPORARY diagnostics: capture how the Indices page itself calls
    WebService.asmx (esp. getIndexChartData / getIndexData parameters),
    then commit the capture to diag_output.txt. Never raises."""
    import subprocess
    lines = []
    try:
        ctx = await browser.new_context(ignore_https_errors=True)
        page = await ctx.new_page()
        reqs, resps = [], []

        def on_req(r):
            if "asmx" in r.url or r.resource_type in ("xhr", "fetch"):
                reqs.append(f"REQ {r.method} {r.url}")
        page.on("request", on_req)

        async def on_resp(resp):
            if "asmx" in resp.url:
                try:
                    body = await resp.text()
                except Exception:
                    body = "<unreadable>"
                resps.append(f"RESP {resp.status} {resp.url}\n{body[:600]}")
        page.on("response", lambda r: asyncio.create_task(on_resp(r)))

        await page.goto(INDICES_PAGE, wait_until="domcontentloaded",
                        timeout=30000)
        await asyncio.sleep(10)
        html = await page.content()
        for term in ("gtk", "getIndexChartData", "getIndexData",
                     "getIndexChartDDLData", "IndexChart"):
            start, hits = 0, 0
            while hits < 6:
                i = html.find(term, start)
                if i < 0:
                    break
                lines.append(
                    f"HTML[{term}] ...{html[max(0, i - 200):i + 250]}...")
                start, hits = i + 1, hits + 1
        hidden = await page.evaluate(
            "() => Array.from(document.querySelectorAll("
            "'input[type=hidden]')).map(e => e.id + '=' + "
            "(e.value || '').slice(0, 150))")
        lines.append("HIDDEN: " + " | ".join(hidden))
        perf = await page.evaluate(
            "() => performance.getEntriesByType('resource')"
            ".map(e => e.name)")
        lines.append("RESOURCES:\n" + "\n".join(perf))
        for target in ("lnkEGX70EWI", "lnkEGX100EWI"):
            before = len(reqs)
            try:
                await page.evaluate(
                    f"__doPostBack('ctl00$C$MI${target}', '')")
            except Exception as e:
                lines.append(f"POSTBACK {target} failed: {e}")
            await asyncio.sleep(8)
            lines.append(
                f"AFTER {target}: new requests: {reqs[before:]}")
        await asyncio.sleep(2)
        lines.append("ALL REQUESTS:\n" + "\n".join(reqs))
        lines.append("ALL RESPONSES:\n" + "\n\n".join(resps))
        await ctx.close()
    except Exception as e:
        lines.append(f"DIAG FAILED: {e}")
    try:
        with open("diag_output.txt", "w", encoding="utf-8") as f:
            f.write("\n\n".join(lines))
        for cmd in (["git", "config", "user.name", "github-actions[bot]"],
                    ["git", "config", "user.email",
                     "github-actions[bot]@users.noreply.github.com"],
                    ["git", "add", "diag_output.txt"],
                    ["git", "commit", "-m", "diag: index API capture"],
                    ["git", "push", "origin", "HEAD:main"]):
            subprocess.run(cmd, check=False)
    except Exception as e:
        print(f"diag save failed: {e}")


async def main():
    today = datetime.now(CAIRO).date().isoformat()
    print(f"Running for (Cairo): {today}")
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                  "--disable-blink-features=AutomationControlled"])
        await run_diag(browser)
        last_err = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            ctx = None
            try:
                ctx, page = await open_egx_page(browser)
                data, shares = await fetch_all(page)
                indices = await fetch_indices(ctx)
                cbe = await fetch_cbe(ctx)
                break
            except Exception as e:
                last_err = e
                print(f"attempt {attempt} failed: {e}")
                if attempt == MAX_ATTEMPTS:
                    await browser.close()
                    print("No data. Nothing written.")
                    sys.exit(1)
                await asyncio.sleep(10)
            finally:
                if ctx is not None:
                    await ctx.close()
        await browser.close()

    # ---- sanity guards: never archive a stale or broken snapshot ----
    new_hist = history_row(data[1], today)
    # (a) Totals identical to the previous recorded day = no new session
    #     (weekend / holiday / manual re-run): succeed without writing.
    if os.path.exists(HISTORY_CSV):
        prev = pd.read_csv(HISTORY_CSV, encoding="utf-8-sig")
        prev = prev[prev["تاريخ"].astype(str) != today]
        if len(prev):
            last = prev.iloc[-1]
            value_cols = [c for c in new_hist if c != "تاريخ"]
            if all(float(new_hist[c]) == float(last[c]) for c in value_cols):
                print(f"No new session data: totals identical to "
                      f"{last['تاريخ']}. Nothing written.")
                return
    # (b) Indices must be complete and plausible vs the last recorded
    #     close; a broken parse (e.g. the weekend page layout) fails loudly.
    closes = [indices["EGX30"], indices["EGX70"], indices["EGX100"]]
    if any(v is None for v in closes):
        print(f"Indices incomplete: {indices}. Nothing written.")
        sys.exit(1)
    if os.path.exists(INDICES_CSV):
        prev_idx = pd.read_csv(INDICES_CSV, encoding="utf-8-sig")
        prev_idx = prev_idx[prev_idx["تاريخ"].astype(str) != today]
        if len(prev_idx):
            last30 = float(prev_idx.iloc[-1]["EGX30_اغلاق"])
            if not (0.8 * last30 <= indices["EGX30"] <= 1.2 * last30):
                print(f"EGX30 close {indices['EGX30']} implausible vs last "
                      f"{last30}. Nothing written.")
                sys.exit(1)

    df = upsert_csv(HISTORY_CSV, [new_hist],
                    ["تاريخ"], ["تاريخ"])
    print(f"{HISTORY_CSV}: saved {today}. Total days: {len(df)}")

    rows = instrument_rows(data, shares, today)
    # replace the whole day across instruments
    if os.path.exists(INSTRUMENTS_CSV):
        old = pd.read_csv(INSTRUMENTS_CSV, encoding="utf-8-sig")
        old = old[old["تاريخ"].astype(str) != today]
        allrows = pd.concat([old, pd.DataFrame(rows)], ignore_index=True)
    else:
        allrows = pd.DataFrame(rows)
    allrows = allrows.sort_values(["تاريخ", "أداة", "مجموعة"]).reset_index(drop=True)
    allrows.to_csv(INSTRUMENTS_CSV, index=False, encoding="utf-8-sig")
    print(f"{INSTRUMENTS_CSV}: saved {today} ({len(rows)} rows).")

    idx_row = {"تاريخ": today,
               "EGX30_اغلاق": indices["EGX30"], "EGX30_تغير": indices["EGX30_pct"],
               "EGX70_اغلاق": indices["EGX70"], "EGX70_تغير": indices["EGX70_pct"],
               "EGX100_اغلاق": indices["EGX100"], "EGX100_تغير": indices["EGX100_pct"],
               "دولار_شراء": cbe["USD_buy"], "دولار_بيع": cbe["USD_sell"],
               "تاريخ_سعر_الدولار": cbe["USD_date"]}
    upsert_csv(INDICES_CSV, [idx_row], ["تاريخ"], ["تاريخ"])
    print(f"{INDICES_CSV}: saved {today}. indices={indices} cbe={cbe}")


if __name__ == "__main__":
    asyncio.run(main())
