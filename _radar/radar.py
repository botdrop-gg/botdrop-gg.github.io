#!/usr/bin/env python3
"""Refreshes https://getbotdrop.com/free-epic-games/ : games that are free to keep on Epic and Steam.

Run by .github/workflows/free-games-refresh.yml every 3 hours. Standard library only.
  python3 _radar/radar.py                # fetch, merge with last good data, re-render page + feed
  python3 _radar/radar.py --render-only  # re-render from _radar/data.json (no network)

Safety: a source that fails (network error, odd JSON) keeps its last good items; the page is only
rewritten when rendering succeeds; expired giveaways are dropped at render time.
Both stores' endpoints are public but undocumented, so they can change without notice.
"""
import datetime as dt, html, json, os, re, sys, urllib.parse, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "_radar", "data.json")
PAGE = os.path.join(ROOT, "free-epic-games", "index.html")
FEED = os.path.join(ROOT, "free-epic-games", "feed.xml")
BASE = "https://getbotdrop.com/"
URL = BASE + "free-epic-games/"
UA = {"User-Agent": "Mozilla/5.0 (compatible; BotDropFreeGames/1.0; +https://getbotdrop.com/free-epic-games/)"}
EPIC_API = "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions?locale=en-US&country=US&allowCountries=US"
STEAM_SEARCH = "https://store.steampowered.com/search/results/"
STEAM_ITEMS = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"
STEAM_CDN = "https://shared.akamai.steamstatic.com/store_item_assets/"
REFRESH_EVERY_H = 12   # rewrite "last checked" at least this often even when nothing changed
HISTORY_MAX = 40

def now_utc():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")
def parse(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(dt.timezone.utc) if s else None
def get_json(url, params=None):
    if params: url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r)

# ---------------- Epic ----------------
def epic_items(data, now):
    """-> list of giveaways (current and upcoming) that are 100% off."""
    out = []
    for e in data["data"]["Catalog"]["searchStore"]["elements"]:
        pr = e.get("promotions") or {}
        maps = (e.get("catalogNs") or {}).get("mappings") or []
        slug = next((m["pageSlug"] for m in maps if m.get("pageSlug") and m.get("pageType") == "productHome"), None) \
            or next((m["pageSlug"] for m in maps if m.get("pageSlug")), None) or (e.get("productSlug") or "").split("/")[0] or None
        title = (e.get("title") or "").strip()
        mystery = "mystery game" in title.lower()
        url = f"https://store.epicgames.com/en-US/p/{slug}" if slug and not mystery else "https://store.epicgames.com/en-US/free-games"
        imgs = {i.get("type"): i.get("url") for i in e.get("keyImages") or []}
        img = imgs.get("OfferImageWide") or imgs.get("DieselStoreFrontWide") or imgs.get("Thumbnail") or ""
        if img.startswith("https://cdn1.epicgames.com/") or img.startswith("https://cdn2.unrealengine.com/"):
            img = img.split("?")[0]
        else:
            img = ""
        for key in ("promotionalOffers", "upcomingPromotionalOffers"):
            for po in pr.get(key) or []:
                for o in po.get("promotionalOffers") or []:
                    if (o.get("discountSetting") or {}).get("discountPercentage") != 0:
                        continue
                    s, en = parse(o.get("startDate")), parse(o.get("endDate"))
                    if not s or not en or en <= now:
                        continue
                    if key == "promotionalOffers" and s > now:
                        continue
                    out.append(dict(store="Epic", id=f"epic:{e['id']}:{iso(s)}", title=title, url=url, img=img,
                                    start=iso(s), end=iso(en), kind="addon" if e.get("offerType") == "ADD_ON" else "game", base=""))
    # Epic always lists at least one giveaway; an empty result means the format changed.
    if not out:
        raise ValueError("Epic returned no 100%-off offers (format change?)")
    return out

# ---------------- Steam ----------------
def steam_items(now):
    """Candidates from store search (free + specials), each confirmed through Steam's own store API."""
    ids, start = [], 0
    while start < 300:
        d = get_json(STEAM_SEARCH, {"maxprice": "free", "specials": 1, "infinite": 1, "json": 1, "cc": "us", "l": "english", "count": 50, "start": start})
        if "results_html" not in d or "total_count" not in d:
            raise ValueError("Steam search answered without results (format change?)")
        found = re.findall(r'data-ds-appid="(\d+)"', d["results_html"])
        ids += [int(a) for a in found if int(a) not in ids]
        start += 50
        if start >= int(d.get("total_count") or 0) or not found:
            break
    if not ids:
        return []
    items = steam_get(ids)
    parents = {}
    pids = sorted({(it.get("related_items") or {}).get("parent_appid") for it in items if (it.get("related_items") or {}).get("parent_appid")})
    if pids:
        parents = {it["appid"]: it.get("name", "") for it in steam_get(pids)}
    out = []
    for it in items:
        if not it.get("success") == 1 or not it.get("visible"):
            continue
        opts = [o for o in (it.get("purchase_options") or []) if o.get("is_free_to_keep") and o.get("discount_pct") == 100]
        if not opts:
            continue  # free weekend, free-to-play, or not actually 100% off
        o = opts[0]
        ends = o.get("free_to_keep_ends")
        en = dt.datetime.fromtimestamp(int(ends), dt.timezone.utc) if ends else None
        if en and en <= now:
            continue
        a = it.get("assets") or {}
        img = (STEAM_CDN + a["asset_url_format"].replace("${FILENAME}", a["header"])) if a.get("asset_url_format") and a.get("header") else ""
        pid = (it.get("related_items") or {}).get("parent_appid")
        out.append(dict(store="Steam", id=f"steam:{it['appid']}:{o.get('packageid')}", title=(it.get("name") or "").strip(),
                        url="https://store.steampowered.com/" + (it.get("store_url_path") or f"app/{it['appid']}/"), img=img,
                        start="", end=iso(en) if en else "", kind="game" if it.get("type") == 0 else "addon",
                        base=parents.get(int(pid), "") if pid else ""))
    return out

def steam_get(appids):
    out = []
    for i in range(0, len(appids), 50):
        q = {"ids": [{"appid": a} for a in appids[i:i + 50]], "context": {"language": "english", "country_code": "US"},
             "data_request": {"include_all_purchase_options": True, "include_assets": True}}
        got = get_json(STEAM_ITEMS, {"input_json": json.dumps(q)}).get("response", {}).get("store_items")
        if got is None:
            raise ValueError("Steam GetItems answered without store_items")
        out += got
    return out

# ---------------- data ----------------
def load():
    try:
        with open(DATA) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"checked": "", "changed": "", "sources": {}, "items": [], "history": []}

def refresh(d, now):
    old = {i["id"]: i for i in d.get("items", [])}
    items, status = [], {}
    for name, fn in (("Epic", lambda: epic_items(get_json(EPIC_API), now)), ("Steam", lambda: steam_items(now))):
        try:
            got = fn()
            status[name] = {"ok": True, "at": iso(now)}
        except Exception as e:  # keep last good data for this store
            print(f"::warning::{name} fetch failed, keeping last good data: {e}", file=sys.stderr)
            got = [i for i in old.values() if i["store"] == name]
            prev = (d.get("sources") or {}).get(name) or {}
            status[name] = {"ok": False, "at": prev.get("at", ""), "error": str(e)[:200]}
        for i in got:
            i["seen"] = old.get(i["id"], {}).get("seen") or iso(now)
        items += got
    items.sort(key=lambda i: (i["store"] != "Epic", i["start"] or "", i["end"] or "9", i["title"].lower()))
    hist = {h["id"]: h for h in d.get("history", [])}
    for i in items:
        hist[i["id"]] = {k: i[k] for k in ("id", "store", "title", "url", "start", "end", "kind", "base", "seen")}
    history = sorted(hist.values(), key=lambda h: h["seen"], reverse=True)[:HISTORY_MAX]
    strip = lambda L: [{k: v for k, v in x.items() if k != "seen"} for x in L]
    changed = strip(items) != strip(d.get("items", [])) or \
        {k: v["ok"] for k, v in status.items()} != {k: v.get("ok") for k, v in (d.get("sources") or {}).items()}
    last = parse(d.get("checked") or "")
    if changed or not last or (now - last).total_seconds() > REFRESH_EVERY_H * 3600:
        d.update(checked=iso(now), sources=status)
    if changed:
        d["changed"] = iso(now)
    d.update(items=items, history=history)
    return d, changed

# ---------------- render ----------------
E = lambda s: html.escape(str(s), quote=True)
def utc_label(s):
    t = parse(s)
    return t.strftime("%b %-d, %Y, %H:%M UTC") if t else ""

def img_tag(i, lazy):
    if not i.get("img"):
        return f'<div class="rgi rgph" aria-hidden="true">{"🎮" if i["store"] == "Epic" else "🕹️"}</div>'
    if i["store"] == "Epic":
        src = i["img"] + "?resize=1&w=480&h=270&quality=medium"
        ss = f' srcset="{E(src)} 480w, {E(i["img"] + "?resize=1&w=800&h=450&quality=medium")} 800w" sizes="(max-width:600px) 92vw, 260px"'
    else:
        src, ss = i["img"], ""
    load = ' loading="lazy"' if lazy else ' fetchpriority="high"'
    return f'<img class="rgi" src="{E(src)}"{ss} width="480" height="270" alt=""{load} decoding="async">'

def card(i, mode, lazy):
    store = i["store"]
    what = " add-on" if i["kind"] == "addon" else ""
    if mode == "up":
        when = f'Free from <time datetime="{i["start"]}" data-t>{utc_label(i["start"])}</time>' + \
               (f' to <time datetime="{i["end"]}" data-t>{utc_label(i["end"])}</time>' if i["end"] else "")
        btn = f"See it on {store}"
    else:
        when = (f'Free until <time datetime="{i["end"]}" data-t>{utc_label(i["end"])}</time>' if i["end"] else "End time: see the store page")
        btn = f"Get it on {store}"
    base = f'<p class="rgb">Needs the base game: {E(i["base"])}</p>' if i.get("base") else ""
    return (f'<li class="rg" data-end="{i["end"]}" data-start="{i["start"] if mode == "up" else ""}">'
            f'<a class="rgl" href="{E(i["url"])}" target="_blank" rel="noopener" tabindex="-1" aria-hidden="true">{img_tag(i, lazy)}</a>'
            f'<div class="rgc"><span class="st st-{store.lower()}">{store}{what}</span><h3>{E(i["title"])}</h3>'
            f'{base}<p class="rgw">{when}</p><a class="rgbtn" href="{E(i["url"])}" target="_blank" rel="noopener">{btn}<span class="vh"> ({E(i["title"])})</span></a></div></li>')

def split(d, now):
    cur, up = [], []
    for i in d["items"]:
        en, s = parse(i["end"]), parse(i["start"])
        if en and en <= now:
            continue
        (up if s and s > now else cur).append(i)
    return cur, up

def render_list(d, now):
    cur, up = split(d, now)
    games = [i for i in cur if i["kind"] == "game" or i["store"] == "Epic"]
    addons = [i for i in cur if i["store"] == "Steam" and i["kind"] != "game"]
    n = 0
    def ul(L, mode):
        nonlocal n
        out = []
        for i in L:
            out.append(card(i, mode, n >= 2)); n += 1
        return '<ul class="rgs">' + "".join(out) + "</ul>"
    h = [f'<h2 id="now">Free to keep right now <span class="cnt">{len(games)}</span></h2>']
    h.append(ul(games, "now") if games else '<p class="rgnone">Nothing is free to keep on Epic or Steam at this moment. Epic usually starts a new giveaway every week.</p>')
    if not any(i["store"] == "Steam" for i in games):
        h.append('<p class="rgnote">No full Steam games are free to keep right now. Steam giveaways are rare and short, which is why we check every 3 hours.</p>')
    h.append(f'<h2 id="next">Coming next on Epic <span class="cnt">{len(up)}</span></h2>')
    h.append(ul(up, "up") if up else '<p class="rgnone">Epic hasn&rsquo;t announced the next free games yet. They usually appear a week ahead.</p>')
    if addons:
        h.append(f'<h2 id="addons">Free Steam add-ons <span class="cnt">{len(addons)}</span></h2><p class="rgnote">100% off and yours to keep, but each one only works if you own its base game.</p>')
        h.append(ul(addons, "now"))
    src = d.get("sources") or {}
    bad = [k for k, v in src.items() if not v.get("ok")]
    stat = (f'<p class="rgstat" role="status">Last checked: <time datetime="{d["checked"]}" data-t>{utc_label(d["checked"])}</time>.'
            ' We check Epic and Steam every 3 hours.')
    for k in bad:
        at = src[k].get("at")
        stat += f' <strong>{k} didn&rsquo;t answer on the last check</strong>, so its games are from ' + \
                (f'<time datetime="{at}" data-t>{utc_label(at)}</time>' if at else "an earlier check") + '.'
    return stat + "</p>" + "".join(h), cur, up

def render_ld(d, cur):
    ld = {"@context": "https://schema.org", "@type": "WebPage", "@id": URL + "#page", "url": URL,
          "name": "Free Epic Games right now, plus Steam giveaways", "dateModified": d.get("changed") or d.get("checked"),
          "isPartOf": {"@id": BASE + "#org"},
          "mainEntity": {"@type": "ItemList", "name": "Games free to keep right now", "numberOfItems": len(cur),
                         "itemListElement": [{"@type": "ListItem", "position": n + 1, "name": i["title"], "url": i["url"]} for n, i in enumerate(cur)]}}
    return '<script type="application/ld+json">' + json.dumps(ld, ensure_ascii=False).replace("</", "<\\/") + "</script>"

def render_feed(d):
    esc = lambda s: html.escape(str(s), quote=False)
    upd = max([h["seen"] for h in d["history"]] or [d.get("checked") or iso(now_utc())])
    out = ['<?xml version="1.0" encoding="utf-8"?>', '<feed xmlns="http://www.w3.org/2005/Atom">',
           "<title>Free games to keep on Epic and Steam (BotDrop)</title>",
           "<subtitle>One entry per new Epic or Steam giveaway. Checked every 3 hours. Always confirm on the store page before claiming.</subtitle>",
           f'<link rel="alternate" type="text/html" href="{URL}"/>', f'<link rel="self" type="application/atom+xml" href="{URL}feed.xml"/>',
           f"<id>{URL}</id>", f"<updated>{upd}</updated>", "<author><name>BotDrop</name><uri>https://getbotdrop.com/</uri></author>",
           f'<icon>{BASE}favicon.svg</icon>']
    for h in d["history"]:
        kind = " add-on" if h["kind"] == "addon" else ""
        until = f" until {utc_label(h['end'])}" if h["end"] else ""
        start = parse(h["start"])
        upcoming = start and start > parse(h["seen"])
        title = f"{'Coming to' if upcoming else 'Free on'} {h['store']}: {h['title']}"
        summ = (f"{h['title']} ({h['store']}{kind}) is free to keep" + (f" from {utc_label(h['start'])}" if upcoming else "") + f"{until}."
                + (f" Needs the base game: {h['base']}." if h.get("base") else "") + " Check the store page before claiming.")
        out += ["<entry>", f"<title>{esc(title)}</title>", f'<link rel="alternate" type="text/html" href="{esc(h["url"])}"/>',
                f"<id>tag:getbotdrop.com,2026:free-games/{esc(h['id'])}</id>", f"<updated>{h['seen']}</updated>",
                f"<published>{h['seen']}</published>", f"<summary>{esc(summ)}</summary>", "</entry>"]
    out.append("</feed>")
    return "\n".join(out) + "\n"

def replace_block(page, name, content):
    a, b = f"<!--radar:{name}:start-->", f"<!--radar:{name}:end-->"
    if a not in page or b not in page:
        raise ValueError(f"marker {name} missing from page")
    return re.sub(re.escape(a) + ".*?" + re.escape(b), lambda m: a + content + b, page, count=1, flags=re.S)

def render(d, now):
    with open(PAGE) as f:
        page = f.read()
    body, cur, up = render_list(d, now)
    new = replace_block(page, "list", body)
    new = replace_block(new, "ld", render_ld(d, cur))
    feed = render_feed(d)
    assert "</html>" in new and new.count("<!--radar:list:start-->") == 1
    for path, text in ((PAGE, new), (FEED, feed)):
        old = open(path).read() if os.path.exists(path) else None
        if old != text:
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                f.write(text)
            os.replace(tmp, path)
    return cur, up

def main():
    now = now_utc()
    d = load()
    if "--render-only" not in sys.argv:
        d, changed = refresh(d, now)
        if not d.get("checked"):
            print("No data yet and nothing fetched; leaving the page alone."); return 0
        tmp = DATA + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f, indent=1, ensure_ascii=False); f.write("\n")
        os.replace(tmp, DATA)
        print("changed" if changed else "no change in the list")
    cur, up = render(d, now)
    print(f"now: {len(cur)} | upcoming: {len(up)}")
    for i in cur: print(f"  [{i['store']}{'/addon' if i['kind']=='addon' else ''}] {i['title']} until {i['end'] or '?'}")
    for i in up: print(f"  [next {i['store']}] {i['title']} {i['start']} -> {i['end']}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
