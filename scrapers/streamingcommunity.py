#!/usr/bin/env python3
"""
StreamingCommunity: ricerca titoli + estrazione URL .m3u8 di origine.

Uso:
    python streamingcommunity.py -s "ciao"                       # solo lista risultati
    python streamingcommunity.py -s "ciao" -p 1 --episodes        # elenca stagioni/episodi (con nomi)
    python streamingcommunity.py -s "ciao" -p 1 --season 2        # tutti gli ep. della S2
    python streamingcommunity.py -s "ciao" -p 1 --season 2 --episode 3
    python streamingcommunity.py -s "ciao" -p 1 --episode all --json
    python streamingcommunity.py "<url watch>" --season 1 --episode 5
    python streamingcommunity.py "<url watch>?episode_id=185906"
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import html as html_mod
import json
import re
import sys
from urllib.parse import quote_plus, urlencode, urljoin, urlparse

import requests

BASE_DEFAULT = "https://streamingcommunityz.boo"
LANG_DEFAULT = "it"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

RE_IFRAME = re.compile(r'(?:src|data-src)="([^"]*/iframe/[^"]+)"')
RE_EPISODE = [
    re.compile(r"episode_id=(\d+)"),
    re.compile(r'"episode_id"\s*:\s*(\d+)'),
    re.compile(r"episode_id&quot;:(\d+)"),
    re.compile(r'data-episode-id="(\d+)"'),
]
RE_EMBED = re.compile(r"https?://(?:[\w-]+\.)*vixcloud\.(?:cc|co)/embed/[\w/?=&%.\-]*")
RE_EMBED_ID = re.compile(r"/embed/(\d+)")
RE_ATTR = re.compile(r"'(token|expires|asn)'\s*:\s*'([^']*)'")
RE_MASTER_URL = re.compile(r"window\.masterPlaylist[\s\S]{0,600}?url\s*:\s*'([^']+)'")
RE_VARIANT = re.compile(r"#EXT-X-STREAM-INF:([^\n]*)\n([^\n#]+)")
RE_PLAYLIST_REQ = re.compile(r"vixcloud\.(?:cc|co)/playlist/\d+")

RE_TITLE_LINK = re.compile(r'href="(?:https?://[^"]*)?/([a-z]{2})/titles/(\d+)-([^"?#]*)')
RE_ALT = re.compile(r'alt="([^"]{1,150})"')
RE_DATA_TITLE = re.compile(r'data-title="([^"]{1,150})"')
RE_TITLE_ATTR = re.compile(r'\btitle="([^"]{1,150})"')

RE_INERTIA = re.compile(r'data-page="([^"]+)"')
RE_TITLE_ID = re.compile(r"/(?:watch|titles)/(\d+)")
RE_EP_FROM_URL = re.compile(r"[?&]episode_id=(\d+)")

# marcatori di stagione nella pagina titolo
RE_SEASON_MARK = re.compile(
    r'(?:data-season-number|data-season-id|data-season)="(\d+)"'
    r'|id="(?:season|stagione)-(\d+)"'
    r'|>\s*(?:Stagione|Season)\s+(\d+)\s*<',
    re.I,
)
# link episodio: cattura attributi del tag e contenuto interno
RE_EP_LINK = re.compile(
    r'<a\b([^>]*?href="[^"]*?/watch/\d+\?episode_id=(\d+)"[^>]*)>([\s\S]{0,600}?)</a>',
    re.I,
)
RE_EP_NUM_ATTR = re.compile(r'data-episode-number="(\d+)"')
RE_EP_NUM_JSON = re.compile(r'"episode_number"\s*:\s*(\d+)')


class ScError(Exception):
    pass


# ------------------------------------------------------------------ utils

def parse_variants(text: str, base: str) -> list[dict]:
    out = []
    for attrs, uri in RE_VARIANT.findall(text):
        res = re.search(r"RESOLUTION=(\d+)x(\d+)", attrs)
        bw = re.search(r"BANDWIDTH=(\d+)", attrs)
        out.append({
            "url": urljoin(base, uri.strip()),
            "height": int(res.group(2)) if res else 0,
            "bandwidth": int(bw.group(1)) if bw else 0,
        })
    out.sort(key=lambda v: (v["height"], v["bandwidth"]), reverse=True)
    return out


def audio_tracks(text: str, base: str) -> list[str]:
    return [urljoin(base, u) for u in
            re.findall(r'#EXT-X-MEDIA:[^\n]*TYPE=AUDIO[^\n]*URI="([^"]+)"', text)]


def playlist_url_from_embed(html: str, embed_url: str) -> str:
    i = html.find("window.masterPlaylist")
    if i == -1:
        raise ScError("window.masterPlaylist assente (embed scaduto o protetto)")
    attrs = dict(RE_ATTR.findall(html[i:i + 800]))
    token, expires, asn = attrs.get("token"), attrs.get("expires"), attrs.get("asn", "")
    if not token:
        raise ScError("token assente nell'embed")
    m = RE_MASTER_URL.search(html)
    vid = RE_EMBED_ID.search(embed_url)
    base = m.group(1) if m else None
    if not base:
        if not vid:
            raise ScError("id video non ricavabile")
        base = f"https://{urlparse(embed_url).netloc}/playlist/{vid.group(1)}"
    params = {"token": token, "expires": expires, "h": "1", "scz": "1", "lang": "it"}
    if asn:
        params["asn"] = asn
    return base + "?" + urlencode(params)


def expand_spec(spec: str) -> list[int]:
    out: list[int] = []
    for part in spec.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out += list(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return list(dict.fromkeys(out))


def parse_picks(spec: str, n: int) -> list[int]:
    if spec.strip().lower() == "all":
        return list(range(1, n + 1))
    return [i for i in expand_spec(spec) if 1 <= i <= n]


def title_id_from_url(u: str) -> int | None:
    m = RE_TITLE_ID.search(u)
    return int(m.group(1)) if m else None


def iframe_for(watch_url: str, html: str, episode_id: int | None) -> str:
    p = urlparse(watch_url)
    lang = p.path.strip("/").split("/")[0] or LANG_DEFAULT
    tid = p.path.rstrip("/").split("/")[-1]
    if episode_id is not None:
        return f"{p.scheme}://{p.netloc}/{lang}/iframe/{tid}?episode_id={episode_id}"
    m = RE_IFRAME.search(html)
    if m:
        return urljoin(watch_url, m.group(1).replace("&amp;", "&"))
    ep = None
    for rx in RE_EPISODE:
        hit = rx.search(html)
        if hit:
            ep = int(hit.group(1))
            break
    url = f"{p.scheme}://{p.netloc}/{lang}/iframe/{tid}"
    if ep is not None:
        url += f"?episode_id={ep}"
    return url


def clean_name(raw: str | None) -> str:
    if not raw:
        return ""
    txt = html_mod.unescape(raw)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt[:150]


# ------------------------------------------------------------------ ricerca

def search_http(query: str, base: str, lang: str, limit: int) -> list[dict]:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "it-IT,it;q=0.9"})
    url = f"{base}/{lang}/search?q={quote_plus(query)}"
    html = s.get(url, timeout=25).text

    res, seen = [], set()
    for m in RE_TITLE_LINK.finditer(html):
        tid = int(m.group(2))
        if tid in seen:
            continue
        seen.add(tid)
        chunk = html[m.start():m.start() + 900]
        title = None
        for rx in (RE_DATA_TITLE, RE_ALT):
            t = rx.search(chunk)
            if t:
                title = t.group(1).strip()
                break
        res.append({
            "id": tid,
            "slug": m.group(3),
            "title": title or m.group(3).replace("-", " "),
            "type": "",
            "url": f"{base}/{m.group(1)}/watch/{tid}",
        })
        if len(res) >= limit:
            break
    if not res:
        raise ScError("nessun risultato nell'HTML")
    return res


def search_browser(query: str, base: str, lang: str, limit: int, headed: bool,
                   verbose: bool) -> list[dict]:
    from playwright.sync_api import sync_playwright

    url = f"{base}/{lang}/search?q={quote_plus(query)}"
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=not headed, args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(user_agent=UA, locale="it-IT",
                                  viewport={"width": 1600, "height": 1000},
                                  timezone_id="Europe/Rome")
        page = ctx.new_page()
        page.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_selector('a[href*="/titles/"]', timeout=20000)
        except Exception:
            pass
        page.wait_for_timeout(1200)

        raw = page.evaluate("""() => [...document.querySelectorAll('a[href*="/titles/"]')]
            .map(a => {
                const img = a.querySelector('img');
                const card = a.closest('.ssr-title-card, .cover, .card, .item, li, article')
                    || a.parentElement;
                const style = card && card.getAttribute('style') || '';
                const posterMatch = style.match(/--c[\\w-]*\\s*:\\s*url\\(['"]?([^'")]+)/);
                return {
                    href: a.getAttribute('href') || '',
                    title: (a.getAttribute('data-title') || a.getAttribute('title')
                            || (img && img.getAttribute('alt'))
                            || (card && card.textContent) || '').trim(),
                    type: (card && (card.getAttribute('data-type') || card.className)) || '',
                    poster: posterMatch ? posterMatch[1] : (img && img.src) || ''
                };
            })""")
        ctx.close()
        browser.close()

    res, seen = [], set()
    for item in raw:
        m = re.search(r"/titles/(\d+)-([^/?#]*)", item.get("href", ""))
        if not m:
            continue
        tid = int(m.group(1))
        if tid in seen:
            continue
        seen.add(tid)
        cls = (item.get("type") or "").lower()
        kind = "movie" if "movie" in cls or "film" in cls else ("tv" if "tv" in cls or "serie" in cls else "")
        title = re.sub(r"\s+", " ", item.get("title") or "").strip()[:120]
        res.append({
            "id": tid,
            "slug": m.group(2),
            "title": title or m.group(2).replace("-", " "),
            "type": kind,
            "poster": item.get("poster", ""),
            "url": f"{base}/{lang}/watch/{tid}",
        })
        if len(res) >= limit:
            break
    if not res:
        raise ScError("nessun risultato (Cloudflare o pagina vuota)")
    return res


def search(query: str, base: str, lang: str, limit: int, headed: bool,
           verbose: bool) -> list[dict]:
    try:
        return search_http(query, base, lang, limit)
    except Exception as e:
        print(f"[!] ricerca HTTP fallita: {e}", file=sys.stderr)
        print("[!] passo al browser...", file=sys.stderr)
        return search_browser(query, base, lang, limit, headed, verbose)


# ------------------------------------------------------------------ stagioni / episodi

def _unwrap(data):
    for _ in range(5):
        if isinstance(data, dict):
            if isinstance(data.get("props"), dict):
                data = data["props"]
                continue
            if isinstance(data.get("title"), dict):
                data = data["title"]
                continue
            if isinstance(data.get("data"), dict):
                data = data["data"]
                continue
        break
    return data


def _season_episodes_from_api(sid: int, base: str, session: requests.Session) -> list:
    for url in (f"{base}/api/seasons/{sid}/episodes", f"{base}/api/seasons/{sid}"):
        try:
            r = session.get(url, timeout=25)
        except Exception:
            continue
        if r.status_code != 200:
            continue
        try:
            data = r.json()
        except Exception:
            continue
        data = _unwrap(data)
        if isinstance(data, dict):
            data = data.get("episodes") or data.get("episode") or []
        if isinstance(data, list) and data:
            return data
    return []


def _episode_name(ep: dict) -> str:
    for key in ("name", "title", "episode_name", "label", "episode_title"):
        val = ep.get(key)
        if isinstance(val, str) and val.strip():
            return clean_name(val)
    return ""


def _normalize_seasons(data, session: requests.Session | None, base: str,
                       verbose: bool) -> list[dict]:
    data = _unwrap(data)
    if not isinstance(data, dict):
        return []
    seasons = data.get("seasons")
    if not seasons:
        seasons = data.get("season") or []
    if not isinstance(seasons, list):
        return []

    out: list[dict] = []
    for sn in seasons:
        if not isinstance(sn, dict):
            continue
        sid = sn.get("id")
        num = sn.get("number", sn.get("season_number"))
        if num is None:
            continue
        eps = sn.get("episodes")
        if not eps and sid is not None and session is not None:
            try:
                eps = _season_episodes_from_api(int(sid), base, session)
            except Exception as e:
                if verbose:
                    print(f"[!] stagione {num}: {e}", file=sys.stderr)
                eps = []

        eps_norm = []
        for idx, ep in enumerate(eps or [], 1):
            if not isinstance(ep, dict):
                continue
            eid = ep.get("id")
            if eid is None:
                continue
            en = ep.get("number", ep.get("episode_number", ep.get("ep")))
            eps_norm.append({
                "id": int(eid),
                "number": int(en) if en is not None else idx,
                "name": _episode_name(ep),
            })
        eps_norm.sort(key=lambda e: e["number"])
        out.append({"id": sid, "number": int(num), "episodes": eps_norm})

    out.sort(key=lambda s: s["number"])
    return out


def _seasons_from_inertia(html: str, session: requests.Session | None, base: str,
                          verbose: bool) -> list[dict]:
    m = RE_INERTIA.search(html)
    if not m:
        return []
    try:
        data = json.loads(html_mod.unescape(m.group(1)))
    except Exception:
        return []
    # The title page embeds season metadata, but its episodes are in loadedSeason
    # or the season-specific page; the older /api/seasons endpoint is gone.
    seasons = _normalize_seasons(data, None, base, verbose)
    props = data.get("props", {}) if isinstance(data, dict) else {}
    loaded = props.get("loadedSeason") if isinstance(props, dict) else None
    if isinstance(loaded, dict):
        loaded_rows = _normalize_seasons({"seasons": [loaded]}, None, base, verbose)
        if loaded_rows:
            loaded_row = loaded_rows[0]
            existing = next((row for row in seasons if row["number"] == loaded_row["number"]), None)
            if existing:
                existing["episodes"] = loaded_row["episodes"] or existing["episodes"]
            else:
                seasons.append(loaded_row)
                seasons.sort(key=lambda row: row["number"])
    return seasons


def _seasons_from_links(html: str) -> list[dict]:
    """Fallback: raggruppa i link episodio per stagione e ne legge il nome."""
    marks: list[tuple[int, int]] = []
    for m in RE_SEASON_MARK.finditer(html):
        val = next((g for g in m.groups() if g), None)
        if val:
            marks.append((m.start(), int(val)))

    buckets: dict[int, list[dict]] = {}
    for m in RE_EP_LINK.finditer(html):
        tag, inner = m.group(1), m.group(3)
        try:
            eid = int(m.group(2))
        except (TypeError, ValueError):
            continue

        num = 1
        for pos, sn in marks:
            if pos < m.start():
                num = sn
            else:
                break

        name = ""
        for rx in (RE_DATA_TITLE, RE_TITLE_ATTR, RE_ALT):
            t = rx.search(tag)
            if t:
                name = clean_name(t.group(1))
                break
        if not name:
            name = clean_name(inner)

        nm = RE_EP_NUM_ATTR.search(tag)
        buckets.setdefault(num, []).append({
            "id": eid,
            "number": int(nm.group(1)) if nm else 0,
            "name": name,
        })

    out: list[dict] = []
    for num, eps in sorted(buckets.items()):
        eps.sort(key=lambda e: e["number"])
        for idx, ep in enumerate(eps, 1):
            if not ep["number"]:
                ep["number"] = idx
        out.append({"id": None, "number": num, "episodes": eps})
    return out


def fetch_seasons(tid: int, base: str, lang: str, verbose: bool = False,
                  slug: str = "") -> list[dict]:
    """Ritorna [{'id','number','episodes':[{'id','number','name'}]}, ...]."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "it-IT,it;q=0.9",
        "Referer": f"{base}/{lang}/watch/{tid}",
        "X-Requested-With": "XMLHttpRequest",
    })

    errs: list[str] = []
    title_url = f"{base}/{lang}/titles/{tid}-{slug}" if slug else f"{base}/{lang}/titles/{tid}"
    endpoints = (
        f"{base}/api/titles/{tid}?lang={lang}",
        f"{base}/api/titles/{tid}/seasons?lang={lang}",
        title_url,
    )
    for url in endpoints:
        try:
            r = s.get(url, timeout=25)
        except Exception as e:
            errs.append(f"{url}: {e}")
            continue
        if r.status_code != 200:
            errs.append(f"{url}: HTTP {r.status_code}")
            continue

        if "json" in r.headers.get("content-type", ""):
            try:
                seasons = _normalize_seasons(r.json(), s, base, verbose)
            except Exception as e:
                errs.append(f"{url}: json non leggibile ({e})")
                continue
        else:
            seasons = _seasons_from_inertia(r.text, s, base, verbose)
            if not seasons:
                seasons = _seasons_from_links(r.text)

        if seasons:
            if slug:
                missing = [season for season in seasons if not season["episodes"]]
                def load_season(season):
                    url = f"{base}/{lang}/titles/{tid}-{slug}/season-{season['number']}"
                    try:
                        season_response = s.get(url, timeout=25)
                        if season_response.status_code != 200:
                            return season["number"], []
                        rows = _seasons_from_inertia(season_response.text, s, base, verbose)
                        hit = next((row for row in rows if row["number"] == season["number"]), None)
                        return season["number"], hit["episodes"] if hit else []
                    except Exception as error:
                        if verbose:
                            print(f"[!] stagione {season['number']}: {error}", file=sys.stderr)
                        return season["number"], []

                with ThreadPoolExecutor(max_workers=min(8, max(1, len(missing)))) as pool:
                    futures = [pool.submit(load_season, season) for season in missing]
                    episodes_by_number = dict(future.result() for future in as_completed(futures))
                for season in seasons:
                    if not season["episodes"]:
                        season["episodes"] = episodes_by_number.get(season["number"], [])
            return seasons
        errs.append(f"{url}: nessuna stagione")

    raise ScError("impossibile ottenere le stagioni (" + "; ".join(errs) + ")")


def print_seasons(title: str, seasons: list[dict]) -> None:
    print(f"\n{title}")
    for sn in seasons:
        print(f"  Stagione {sn['number']}")
        for ep in sn["episodes"]:
            suffix = f" - {ep['name']}" if ep["name"] else ""
            print(f"    {ep['number']}. (id {ep['id']}){suffix}")


def pick_episodes(seasons: list[dict], season_spec: str | None,
                  episode_spec: str | None) -> list[tuple[dict, dict]]:
    by_num = {s["number"]: s for s in seasons}
    nums = sorted(by_num)
    if season_spec:
        chosen = nums if season_spec.strip().lower() == "all" else \
            [n for n in nums if n in set(expand_spec(season_spec))]
    else:
        chosen = nums

    pairs: list[tuple[dict, dict]] = []
    for n in chosen:
        sn = by_num[n]
        if episode_spec:
            if episode_spec.strip().lower() == "all":
                sel = sn["episodes"]
            else:
                want = set(expand_spec(episode_spec))
                sel = [e for e in sn["episodes"] if e["number"] in want]
        else:
            sel = sn["episodes"]
        pairs += [(sn, e) for e in sel]
    return pairs


# ------------------------------------------------------------------ HTTP puro

def try_http(watch_url: str, episode_id: int | None, variant: int | None) -> dict:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    })

    if episode_id is None:
        m = RE_EP_FROM_URL.search(watch_url)
        if m:
            episode_id = int(m.group(1))

    html = s.get(watch_url, timeout=25).text
    iframe = iframe_for(watch_url, html, episode_id)

    r = s.get(iframe, headers={"Referer": watch_url}, timeout=25)
    em = RE_EMBED.search(r.text)
    if not em or not RE_EMBED_ID.search(em.group(0)):
        raise ScError("embed non valido o privo di id")
    embed = em.group(0).replace("&amp;", "&")

    r = s.get(embed, headers={"Referer": embed}, timeout=25)
    playlist = playlist_url_from_embed(r.text, embed)

    r = s.get(playlist, headers={"Referer": embed, "Accept": "*/*"}, timeout=25)
    if "#EXTM3U" not in r.text:
        raise ScError("playlist non valida")
    text = r.text
    variants = parse_variants(text, playlist)
    origin = playlist
    if variant:
        match = [v for v in variants if v["height"] == variant]
        if match:
            origin = match[0]["url"]
    elif variants:
        origin = variants[0]["url"]

    return {
        "watch": watch_url, "iframe": iframe, "embed": embed, "playlist": playlist,
        "origin": origin, "variants": variants,
        "audio_tracks": audio_tracks(text, playlist), "via": "http",
        "headers": {"Referer": embed, "User-Agent": UA},
    }


# ------------------------------------------------------------------ browser

def try_browser(watch_url: str, episode_id: int | None, variant: int | None,
                headed: bool, verbose: bool) -> dict:
    from playwright.sync_api import sync_playwright

    if episode_id is None:
        m = RE_EP_FROM_URL.search(watch_url)
        if m:
            episode_id = int(m.group(1))

    if episode_id is not None:
        p = urlparse(watch_url)
        lang = p.path.strip("/").split("/")[0] or LANG_DEFAULT
        tid = p.path.rstrip("/").split("/")[-1]
        watch_url = f"{p.scheme}://{p.netloc}/{lang}/iframe/{tid}?episode_id={episode_id}"

    seen: list[str] = []
    embed_box: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=not headed, args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(user_agent=UA, locale="it-IT",
                                  viewport={"width": 1920, "height": 1080},
                                  timezone_id="Europe/Rome")
        page = ctx.new_page()
        page.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")

        def on_req(req):
            u = req.url
            if RE_PLAYLIST_REQ.search(u) and u not in seen:
                seen.append(u)
                if verbose:
                    print("[browser] playlist:", u, file=sys.stderr)
            if "/embed/" in u and "vixcloud" in u and u not in embed_box:
                embed_box.append(u)

        page.on("request", on_req)
        page.goto(watch_url, wait_until="domcontentloaded", timeout=60000)

        for _ in range(120):
            if seen:
                break
            page.wait_for_timeout(500)
            if not embed_box:
                for fr in page.frames:
                    if "vixcloud" in fr.url:
                        embed_box.append(fr.url)
                        break

        if not seen and embed_box:
            embed = embed_box[0]
            page.goto(embed, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)
            if not seen:
                seen.append(playlist_url_from_embed(page.content(), embed))

        if not seen:
            ctx.close()
            browser.close()
            raise ScError("nessuna playlist intercettata (Cloudflare o video non disponibile)")

        playlist = seen[0]
        embed = embed_box[0] if embed_box else playlist
        resp = ctx.request.get(playlist, headers={"Referer": embed}, timeout=30000)
        text = resp.text()
        if "#EXTM3U" not in text:
            ctx.close()
            browser.close()
            raise ScError(f"playlist non valida (HTTP {resp.status})")

        variants = parse_variants(text, playlist)
        origin = playlist
        if variant:
            match = [v for v in variants if v["height"] == variant]
            if match:
                origin = match[0]["url"]
        elif variants:
            origin = variants[0]["url"]

        res = {
            "watch": watch_url, "embed": embed, "playlist": playlist, "origin": origin,
            "variants": variants, "audio_tracks": audio_tracks(text, playlist),
            "via": "browser", "headers": {"Referer": embed, "User-Agent": UA},
        }
        ctx.close()
        browser.close()
        return res


def resolve(watch_url: str, episode_id: int | None, variant: int | None,
            force_browser: bool, headed: bool, verbose: bool) -> dict:
    if not force_browser:
        try:
            return try_http(watch_url, episode_id, variant)
        except Exception as e:
            print(f"[!] HTTP puro fallito: {e}", file=sys.stderr)
            print("[!] passo al browser...", file=sys.stderr)
    return try_browser(watch_url, episode_id, variant, headed, verbose)


def resolve_title(base: str, lang: str, title: dict, a, out: list) -> None:
    """Risolve un titolo: intero se film, oppure le stagioni/episodi richiesti."""
    want_eps = bool(a.episodes or a.season or a.episode)
    if not want_eps:
        res = resolve(title["url"], a.episode_id, a.variant,
                      a.browser, a.headed, a.verbose)
        res["title"] = title["title"]
        out.append(res)
        if not a.json:
            print(res["origin"])
        return

    seasons = []
    try:
        seasons = fetch_seasons(title["id"], base, lang, a.verbose, title.get("slug", ""))
    except Exception as e:
        print(f"  errore stagioni: {e}", file=sys.stderr)

    if not seasons:
        print("  [!] nessuna stagione: risolvo il titolo intero", file=sys.stderr)
        res = resolve(title["url"], a.episode_id, a.variant,
                      a.browser, a.headed, a.verbose)
        res["title"] = title["title"]
        out.append(res)
        if not a.json:
            print(res["origin"])
        return

    if a.episodes and not a.season and not a.episode:
        print_seasons(title["title"], seasons)
        return

    pairs = pick_episodes(seasons, a.season, a.episode)
    if not pairs:
        print("  [!] nessun episodio selezionato", file=sys.stderr)
        return

    for sn, ep in pairs:
        name = f" - {ep['name']}" if ep["name"] else ""
        label = f"{title['title']} S{sn['number']:02d}E{ep['number']:02d}{name}"
        print(f"  -> {label}", file=sys.stderr)
        url = f"{title['url']}?episode_id={ep['id']}"
        try:
            res = resolve(url, ep["id"], a.variant, a.browser, a.headed, a.verbose)
        except Exception as e:
            print(f"    errore: {e}", file=sys.stderr)
            continue
        res["title"] = title["title"]
        res["season"] = sn["number"]
        res["episode"] = ep["number"]
        res["episode_name"] = ep["name"]
        out.append(res)
        if not a.json:
            print(res["origin"])


# ------------------------------------------------------------------ main

def main() -> int:
    ap = argparse.ArgumentParser(description="Ricerca e estrazione stream da StreamingCommunity")
    ap.add_argument("url", nargs="?", help="URL watch (se non usi -s)")
    ap.add_argument("-s", "--search", help="testo da cercare")
    ap.add_argument("-p", "--pick", help="indici da risolvere: 1 | 1,3-5 | all")
    ap.add_argument("-n", "--limit", type=int, default=20, help="max risultati (default 20)")
    ap.add_argument("--base", default=BASE_DEFAULT)
    ap.add_argument("--lang", default=LANG_DEFAULT)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--variant", type=int, help="altezza, es. 720")
    ap.add_argument("--episode-id", type=int, help="forza l'id episodio")
    ap.add_argument("-e", "--episodes", action="store_true", help="elenca stagioni ed episodi")
    ap.add_argument("--season", help="stagioni: 1 | 1,3-5 | all")
    ap.add_argument("--episode", help="episodi: 1 | 1,3-5 | all")
    ap.add_argument("--browser", action="store_true", help="usa subito il browser")
    ap.add_argument("--headed", action="store_true", help="browser visibile")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    # ---- ricerca
    if a.search:
        try:
            results = search(a.search, a.base, a.lang, a.limit, a.headed, a.verbose)
        except Exception as e:
            print(f"errore: {e}", file=sys.stderr)
            return 1

        for i, r in enumerate(results, 1):
            kind = f"[{r['type']}]" if r["type"] else ""
            print(f"{i:2}. {r['title']} {kind} (id {r['id']})")

        if a.json and not a.pick:
            print(json.dumps(results, indent=2, ensure_ascii=False))
            return 0

        pick = a.pick
        if pick is None and sys.stdin.isatty():
            pick = input("\nquali risolvo? (es. 1,3-5 / all / invio = nessuno): ").strip()
        if not pick:
            return 0

        try:
            idxs = parse_picks(pick, len(results))
        except ValueError:
            print("selezione non valida", file=sys.stderr)
            return 1

        out = []
        for i in idxs:
            r = results[i - 1]
            print(f"\n[{i}] {r['title']}", file=sys.stderr)
            try:
                resolve_title(a.base, a.lang, r, a, out)
            except Exception as e:
                print(f"  errore: {e}", file=sys.stderr)

        if a.json:
            print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0

    # ---- url diretto
    if not a.url:
        ap.error("serve un URL oppure -s QUERY")

    tid = title_id_from_url(a.url)
    if tid is None:
        print("errore: URL senza id titolo", file=sys.stderr)
        return 1

    if a.episodes or a.season or a.episode:
        title = {"id": tid, "title": str(tid), "url": a.url.split("?")[0]}
        out: list = []
        try:
            resolve_title(a.base, a.lang, title, a, out)
        except Exception as e:
            print(f"errore: {e}", file=sys.stderr)
            return 1
        if a.json:
            print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0

    try:
        res = resolve(a.url, a.episode_id, a.variant, a.browser, a.headed, a.verbose)
    except Exception as e:
        print(f"errore: {e}", file=sys.stderr)
        return 1

    if a.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        print(res["origin"])
        if res["variants"]:
            print("varianti:", ", ".join(f'{v["height"]}p' for v in res["variants"]), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
