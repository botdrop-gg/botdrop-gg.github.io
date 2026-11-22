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

# Manual copy for Epic/Steam offers that are not a paid game going free.
# Key by the Epic product slug (the /p/... part of the store URL) or by the Epic offer id.
# {start} and {end} become the offer's UTC times. Set free_game to False to keep the
# offer off "free games" headings, counts, recent-history, and JSON-LD.
OVERRIDES = {
    "firestone-online-idle-rpg-bfd04b": {
        "badge": "In-game bonus",
        "when": "Free-to-play game. Epic offer: in-game item bundle (advertised US$100 value), {start} – {end}. Install and log in through Epic; new players must reach stage 15.",
        "feed_title_upcoming": "Coming to Epic: Firestone in-game bonus (free-to-play game)",
        "feed_title": "Epic in-game bonus: Firestone (free-to-play game)",
        "feed_summary": "Firestone – Idle Clicker Online RPG is a free-to-play game. The Epic offer is an in-game item bundle (advertised US$100 value), {start} – {end}. Install and log in through Epic; new players must reach stage 15. Check the store page before claiming.",
        "free_game": False,
    },
}

def override_for(i):
    keys = []
    m = re.search(r"/p/([^/?#]+)", i.get("url") or "")
    if m:
        keys.append(m.group(1))
    iid = i.get("id") or ""
    if iid:
        keys.append(iid)
        parts = iid.split(":")
        if len(parts) >= 2 and parts[1]:
            keys.append(parts[1])
    for k in keys:
        if k in OVERRIDES:
            return OVERRIDES[k]
    return None

def is_free_game(i):
    ov = override_for(i)
    return not ov or ov.get("free_game", True)

def fill_override(text, i, html_times=False):
    def fmt(s):
        if not s:
            return "see the store page"
        return f'<time datetime="{s}" data-t>{utc_label(s)}</time>' if html_times else utc_label(s)
    return text.replace("{start}", fmt(i.get("start"))).replace("{end}", fmt(i.get("end")))

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
    live_ids = {i["id"] for i in items}
    def _keep_hist(h):
        s = parse(h.get("start"))
        # Upcoming announcements that vanished from the store feed were often swapped out.
        if s and s > now and h["id"] not in live_ids:
            return False
        return True
    history = [h for h in hist.values() if _keep_hist(h)]
    history = sorted(history, key=lambda h: h["seen"], reverse=True)[:HISTORY_MAX]
    strip = lambda L: [{k: v for k, v in x.items() if k != "seen"} for x in L]
    changed = strip(items) != strip(d.get("items", [])) or \
        {k: v["ok"] for k, v in status.items()} != {k: v.get("ok") for k, v in (d.get("sources") or {}).items()}
    last = parse(d.get("checked") or "")
    force = os.environ.get("RADAR_FORCE") == "true"  # manual run with "force" ticked: always refresh "last checked"
    if changed or force or not last or (now - last).total_seconds() > REFRESH_EVERY_H * 3600:
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
    ov = override_for(i)
    if ov and ov.get("when"):
        when = fill_override(ov["when"], i, html_times=True)
        btn = f"See it on {store}"
    elif mode == "up":
        when = f'Free from <time datetime="{i["start"]}" data-t>{utc_label(i["start"])}</time>' + \
               (f' to <time datetime="{i["end"]}" data-t>{utc_label(i["end"])}</time>' if i["end"] else "")
        btn = f"See it on {store}"
    else:
        when = (f'Free until <time datetime="{i["end"]}" data-t>{utc_label(i["end"])}</time>' if i["end"] else "End time: see the store page")
        btn = f"Get it on {store}"
    base = f'<p class="rgb">Needs the base game: {E(i["base"])}</p>' if i.get("base") else ""
    badges = f'<span class="st st-{store.lower()}">{store}{what}</span>'
    if ov and ov.get("badge"):
        badges = f'<div class="stg">{badges}<span class="st st-bonus">{E(ov["badge"])}</span></div>'
    return (f'<li class="rg" data-end="{i["end"]}" data-start="{i["start"] if mode == "up" else ""}">'
            f'<a class="rgl" href="{E(i["url"])}" target="_blank" rel="noopener" tabindex="-1" aria-hidden="true">{img_tag(i, lazy)}</a>'
            f'<div class="rgc">{badges}<h3>{E(i["title"])}</h3>'
            f'{base}<p class="rgw">{when}</p><a class="rgbtn" href="{E(i["url"])}" target="_blank" rel="noopener">{btn}<span class="vh"> ({E(i["title"])})</span></a></div></li>')

def split(d, now):
    cur, up = [], []
    for i in d["items"]:
        en, s = parse(i["end"]), parse(i["start"])
        if en and en <= now:
            continue
        (up if s and s > now else cur).append(i)
    return cur, up

def past_games(d, now, store=None):
    """Ended full-game giveaways, newest first. Skips add-ons and swapped upcoming announcements."""
    live_ids = {i["id"] for i in d.get("items", [])}
    out = []
    for h in d.get("history", []):
        if h.get("kind") == "addon" or not is_free_game(h):
            continue
        if store and h.get("store") != store:
            continue
        s, en = parse(h.get("start")), parse(h.get("end"))
        if s and s > now and h["id"] not in live_ids:
            continue
        if en and en <= now:
            out.append(h)
    out.sort(key=lambda h: (h.get("end") or "", h.get("start") or ""), reverse=True)
    return out

def hist_table(rows):
    if not rows:
        return ""
    out = ['<table class="rghist"><thead><tr><th>Game</th><th>Store</th><th>Free from</th><th>Free until</th></tr></thead><tbody>']
    for h in rows:
        start = f'<time datetime="{h["start"]}" data-t>{utc_label(h["start"])}</time>' if h.get("start") else "See the store page"
        end = f'<time datetime="{h["end"]}" data-t>{utc_label(h["end"])}</time>' if h.get("end") else "See the store page"
        out.append(
            f'<tr><td><a href="{E(h["url"])}" target="_blank" rel="noopener">{E(h["title"])}</a></td>'
            f'<td>{E(h["store"])}</td><td>{start}</td><td>{end}</td></tr>'
        )
    out.append("</tbody></table>")
    return "".join(out)

def render_list(d, now):
    cur, up = split(d, now)
    epic_now = [i for i in cur if i["store"] == "Epic" and is_free_game(i)]
    epic_up = [i for i in up if i["store"] == "Epic" and is_free_game(i)]
    epic_bonus = [i for i in cur + up if i["store"] == "Epic" and not is_free_game(i)]
    steam_now = [i for i in cur if i["store"] == "Steam" and i["kind"] == "game"]
    addons = [i for i in cur if i["store"] == "Steam" and i["kind"] != "game"]
    week_ago = now - dt.timedelta(days=7)
    steam_week_past = [h for h in past_games(d, now, "Steam") if parse(h.get("end")) and parse(h["end"]) > week_ago]
    n = 0
    def ul(L, mode):
        nonlocal n
        out = []
        for i in L:
            out.append(card(i, mode, n >= 2)); n += 1
        return '<ul class="rgs">' + "".join(out) + "</ul>"
    h = [f'<h2 id="now">Free Epic games right now <span class="cnt">{len(epic_now)}</span></h2>']
    h.append(ul(epic_now, "now") if epic_now else
             '<p class="rgnone">Nothing is free to keep on the Epic Games Store at this moment. Epic usually starts a new giveaway every week.</p>')
    h.append(f'<h2 id="next">Coming next on Epic <span class="cnt">{len(epic_up)}</span></h2>')
    h.append(ul(epic_up, "up") if epic_up else
             '<p class="rgnone">Epic hasn&rsquo;t announced the next free games yet. They usually appear a week ahead.</p>')
    if epic_bonus:
        h.append(f'<h2 id="bonuses">In-game bonuses on Epic <span class="cnt">{len(epic_bonus)}</span></h2>')
        h.append('<p class="rgnote">These are not paid games going free. Each card explains the offer.</p>')
        bonus_html = []
        for i in epic_bonus:
            s = parse(i.get("start"))
            bonus_html.append(card(i, "up" if s and s > now else "now", n >= 2))
            n += 1
        h.append('<ul class="rgs">' + "".join(bonus_html) + "</ul>")
    steam_week_n = len(steam_now) + len(steam_week_past)
    h.append(f'<h2 id="steam">Free Steam games this week <span class="cnt">{steam_week_n}</span></h2>')
    h.append('<p class="rgnote">Full games that are or were 100% off to keep in the last 7 days (US store). Steam giveaways are rare and often last only a few hours, which is why we check every 3 hours. Free weekends and free-to-play titles aren&rsquo;t listed.</p>')
    if steam_now:
        h.append(ul(steam_now, "now"))
    if steam_week_past:
        h.append(hist_table(steam_week_past))
    if not steam_now and not steam_week_past:
        h.append('<p class="rgnone">No full Steam games are free to keep this week.</p>')
        older = past_games(d, now, "Steam")[:5]
        if older:
            h.append('<p class="rgnote">Recent Steam giveaways we&rsquo;ve recorded:</p>')
            h.append(hist_table(older))
    if addons:
        h.append(f'<h2 id="addons">Free Steam add-ons <span class="cnt">{len(addons)}</span></h2><p class="rgnote">100% off and yours to keep, but each one only works if you own its base game.</p>')
        h.append(ul(addons, "now"))
    past_epic = past_games(d, now, "Epic")
    h.append(f'<h2 id="recent">Recent Epic Games free games <span class="cnt">{len(past_epic)}</span></h2>')
    h.append('<p class="rgnote">Past 100%-off Epic giveaways we&rsquo;ve recorded for the US store, newest first. Announced upcoming games that were later swapped out are not listed. Always check the store page: dates can vary by region.</p>')
    h.append(hist_table(past_epic) if past_epic else '<p class="rgnone">No past Epic giveaways recorded yet.</p>')
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
    games = [i for i in cur if is_free_game(i)]
    ld = {"@context": "https://schema.org", "@type": "WebPage", "@id": URL + "#page", "url": URL,
          "name": "Epic Games Free Games List: Now and Next | BotDrop", "dateModified": d.get("changed") or d.get("checked"),
          "isPartOf": {"@id": BASE + "#org"},
          "mainEntity": {"@type": "ItemList", "name": "Epic Games free games list", "numberOfItems": len(games),
                         "itemListElement": [{"@type": "ListItem", "position": n + 1, "name": i["title"], "url": i["url"]} for n, i in enumerate(games)]}}
    return '<script type="application/ld+json">' + json.dumps(ld, ensure_ascii=False).replace("</", "<\\/") + "</script>"

def render_feed(d):
    esc = lambda s: html.escape(str(s), quote=False)
    upd = max([h["seen"] for h in d["history"]] or [d.get("checked") or iso(now_utc())])
    out = ['<?xml version="1.0" encoding="utf-8"?>', '<feed xmlns="http://www.w3.org/2005/Atom">',
           "<title>Epic Games free games list, plus Steam (BotDrop)</title>",
           "<subtitle>One entry per new Epic or Steam giveaway. Checked every 3 hours. Always confirm on the store page before claiming.</subtitle>",
           f'<link rel="alternate" type="text/html" href="{URL}"/>', f'<link rel="self" type="application/atom+xml" href="{URL}feed.xml"/>',
           f"<id>{URL}</id>", f"<updated>{upd}</updated>", "<author><name>BotDrop</name><uri>https://getbotdrop.com/</uri></author>",
           f'<icon>{BASE}favicon.svg</icon>']
    for h in d["history"]:
        kind = " add-on" if h["kind"] == "addon" else ""
        until = f" until {utc_label(h['end'])}" if h["end"] else ""
        start = parse(h["start"])
        upcoming = start and start > parse(h["seen"])
        ov = override_for(h)
        if ov and (ov.get("feed_title") or ov.get("feed_title_upcoming") or ov.get("feed_summary")):
            if upcoming and ov.get("feed_title_upcoming"):
                title = ov["feed_title_upcoming"]
            elif ov.get("feed_title"):
                title = ov["feed_title"]
            else:
                title = f"{'Coming to' if upcoming else 'On'} {h['store']}: {h['title']}"
            if ov.get("feed_summary"):
                summ = fill_override(ov["feed_summary"], h)
            else:
                summ = (f"{h['title']} ({h['store']}{kind})" + (f" from {utc_label(h['start'])}" if upcoming else "") + f"{until}."
                        + " Check the store page before claiming.")
        else:
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
