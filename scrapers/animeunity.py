#!/usr/bin/env python3
"""
AnimeUnity: ricerca titoli, lista episodi, estrazione playlist HLS validata.

Uso:
    python animeunity.py -s "naruto"
    python animeunity.py "<url anime>" -e 1-12 --out naruto.jsonl
    python animeunity.py "<url anime>?ep=<id>" --json
    python animeunity.py "<url anime>" -e 1 --variant 1080

Flusso:
    ricerca:  /archivio?title=<query>            -> card a.overlay-wrapper
    scheda:   /anime/<id>-<slug>/                -> #embed[src] con token
              (gli ep-id non stanno nell'HTML: si ricostruiscono dal primo
               episodio reso e si calibrano sul titolo "Episodio N";
               in alternativa /info_api?id=<id>)
    episodio: /anime/<id>-<slug>/?ep=<epId>      -> #embed[src]
              -> /embed/<id>?token=...           -> window.masterPlaylist
              -> /playlist/<id>?...              -> master m3u8 -> variante migliore.
    Un window.downloadUrl diretto, se presente, vince sulla playlist.
    window.streams (Server1 ub=1 / Server2 ab=1) sono sorgenti alternative.

Trasporto: prima requests; se Cloudflare restituisce challenge o pagina vuota,
la stessa URL viene presa con Chromium headless (contesto unico riusato) e si
parsa il DOM reale. Le versioni ITA sono voci separate ("... (ITA)"): --audio
ita le mette in cima. Host che rispondono 1027 vanno in coda.
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import re
import sys
import time
from urllib.parse import quote_plus, urlencode, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE_DEFAULT = "https://www.animeunity.co"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

RE_MEDIA = re.compile(r'https?://[^\s"\'<>\\)]+\.(?:mp4|m3u8)[^\s"\'<>\\)]*', re.I)
RE_EMBED_ANY = re.compile(r'https?://[^\s"\'<>\\]+/embed/[\w\-./]+(?:\?[^\s"\'<>\\]*)?', re.I)
RE_ANIME_URL = re.compile(r"/anime/(\d+)-([^/?#]+)", re.I)
RE_EP_PARAM = re.compile(r"(?:^|&)ep=(\d+)", re.I)
RE_WINDOW_ANIME = re.compile(r"window\.anime\s*=\s*", re.I)
RE_ATTR = re.compile(r"'(token|expires|asn)'\s*:\s*'([^']*)'")
RE_MASTER_URL = re.compile(r"window\.masterPlaylist[\s\S]{0,600}?url\s*:\s*'([^']+)'")
RE_EMBED_ID = re.compile(r"/embed/(\d+)")
RE_DOWNLOAD = re.compile(r"window\.downloadUrl\s*=\s*'([^']+)'")
RE_STREAMS = re.compile(r"window\.streams\s*=\s*(\[[\s\S]{0,2000}?\])\s*;")
RE_VARIANT = re.compile(r"#EXT-X-STREAM-INF:([^\n]*)\n([^\n#]+)")
RE_EP_TITLE = re.compile(r"Episodio\s+(\d+(?:\.\d+)?)", re.I)

CF_MARKERS = (
    "just a moment",
    "_cf_chl",
    "cf_chl_opt",
    "challenge-platform",
    "enable javascript and cookies to continue",
    "checking your browser",
)

AD_HOSTS = ("epicgameshubham", "doubleclick", "googletagmanager", "googlesyndication",
            "adservice", "banner", "pixel", "histats")

CACHE_TTL = 180.0
_cache: dict[str, tuple[float, object]] = {}


class ScError(Exception):
    pass


class NoPlayer(ScError):
    """La pagina non espone episodi/video: serve il browser."""


# ------------------------------------------------------------------ cache

def cache_get(key: str):
    hit = _cache.get(key)
    if not hit:
        return None
    ts, val = hit
    if time.time() - ts > CACHE_TTL:
        _cache.pop(key, None)
        return None
    return val


def cache_put(key: str, val) -> None:
    _cache[key] = (time.time(), val)


# ------------------------------------------------------------------ trasporto

def session() -> requests.Session:
    """Sessione browser-like. X-Requested-With NON globale: se lo mandi su
    /archivio il server risponde in modalita' ajax e non e' detto che
    restituisca le card."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    })
    return s


_SESSION: requests.Session | None = None


def shared_session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        _SESSION = session()
    return _SESSION


def get(s: requests.Session, url: str, referer: str | None = None, timeout: int = 25,
        api: bool = False) -> str:
    h = {}
    if referer:
        h["Referer"] = referer
        h["Sec-Fetch-Site"] = "same-origin"
        h["Sec-Fetch-Mode"] = "navigate"
    if api:
        h["X-Requested-With"] = "XMLHttpRequest"
        h["Accept"] = "application/json, */*"
    r = s.get(url, headers=h, timeout=(5, timeout), allow_redirects=True)
    r.raise_for_status()
    return r.text


# -- browser (riusato) ------------------------------------------------------

_BW: dict[str, object] = {"pw": None, "browser": None, "ctx": None}


def _browser_ctx():
    if _BW["ctx"] is None:
        from playwright.sync_api import sync_playwright

        _BW["pw"] = sync_playwright().start()
        _BW["browser"] = _BW["pw"].chromium.launch(
            headless=True, args=["--disable-blink-features=AutomationControlled"])
        ctx = _BW["browser"].new_context(
            user_agent=UA, locale="it-IT", viewport={"width": 1600, "height": 900},
            timezone_id="Europe/Rome")
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        _BW["ctx"] = ctx
    return _BW["ctx"]


def _browser_close() -> None:
    for key in ("ctx", "browser"):
        try:
            if _BW.get(key):
                _BW[key].close()  # type: ignore[union-attr]
        except Exception:
            pass
        _BW[key] = None
    try:
        if _BW.get("pw"):
            _BW["pw"].stop()  # type: ignore[union-attr]
    except Exception:
        pass
    _BW["pw"] = None


atexit.register(_browser_close)


def browser_html(url: str, referer: str | None = None, timeout: int = 25,
                 wait_sel: str = "#anime-wrapper, #embed, #video-top, #episode-nav") -> str:
    ctx = _browser_ctx()
    page = ctx.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=max(8, timeout) * 1000,
                  referer=referer)
        if wait_sel:
            try:
                page.wait_for_selector(wait_sel, timeout=9000)
            except Exception:
                pass
        page.wait_for_timeout(500)
        return page.content()
    finally:
        try:
            page.close()
        except Exception:
            pass


def looks_blocked(html: str) -> bool:
    if not html or len(html) < 1500:
        return True
    low = html[:6000].lower()
    return any(m in low for m in CF_MARKERS)


def fetch_html(url: str, referer: str | None = None, timeout: int = 25,
               allow_browser: bool = True, wait_sel: str = "",
               must_contain: tuple[str, ...] = (), log=None) -> str:
    """requests prima; se la risposta e' una challenge o non contiene i marcatori
    attesi, la pagina viene ripresa col browser."""
    s = shared_session()
    try:
        if not s.cookies:
            try:
                get(s, base_of(url), timeout=timeout)
            except Exception:
                pass
        html = get(s, url, referer=referer, timeout=timeout)
    except Exception as e:
        if not allow_browser:
            raise ScError(f"richiesta fallita ({type(e).__name__})")
        if log:
            log(f"http KO ({type(e).__name__}) -> browser")
        return browser_html(url, referer, timeout, wait_sel or "#app")

    blocked = looks_blocked(html) or any(m not in html for m in must_contain)
    if not blocked:
        return html
    if not allow_browser:
        return html
    if log:
        log("pagina ridotta/challenge -> browser")
    return browser_html(url, referer, timeout, wait_sel or "#app")


# ------------------------------------------------------------------ utils

def base_of(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def anime_url_from(url: str) -> str:
    p = urlparse(url)
    m = RE_ANIME_URL.search(p.path)
    if m:
        return f"{p.scheme}://{p.netloc}/anime/{m.group(1)}-{m.group(2)}"
    return url


def anime_id_from(url: str) -> str | None:
    m = RE_ANIME_URL.search(urlparse(url).path)
    return m.group(1) if m else None


def ep_id_from(url: str) -> str | None:
    m = RE_EP_PARAM.search(urlparse(url).query)
    return m.group(1) if m else None


def watch_url(anime_url: str, ep_id: str | int) -> str:
    return f"{anime_url.rstrip('/')}/?ep={ep_id}"


def host_of(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower()
    except Exception:
        return ""


def unescape(s: str) -> str:
    return s.replace("\\/", "/").replace("&amp;", "&")


def verify_source(url: str, referer: str, timeout: int = 20, tries: int = 2) -> tuple[bool, str]:
    why = "?"
    for n in range(tries):
        if n:
            time.sleep(1.2)
        try:
            r = requests.get(
                url,
                headers={"User-Agent": UA, "Referer": referer,
                         "Accept": "*/*", "Range": "bytes=0-2047"},
                stream=True, timeout=(4, timeout), allow_redirects=True,
            )
            status = r.status_code
            ctype = (r.headers.get("Content-Type") or "").lower()
            head = next(r.iter_content(2048), b"") or b""
            r.close()
        except Exception as e:
            why = f"eccezione: {type(e).__name__}"
            continue

        low = head[:400].lower()
        if b"<html" in low or b"<!doctype" in low or b"error 1027" in low or b"rate limited" in low:
            tag = "1027/quota" if (b"1027" in low or b"rate limited" in low) else "html"
            return False, f"HTTP {status} {tag}"
        if status == 429:
            why = "HTTP 429"
            continue
        if status not in (200, 206):
            return False, f"HTTP {status}"
        if head[4:8] in (b"ftyp", b"moov", b"mdat") or "video/" in ctype or "octet-stream" in ctype:
            return True, "ok"
        if ".m3u8" in url.lower() or b"#extm3u" in head.lower() or "mpegurl" in ctype:
            return True, "ok (m3u8)"
        return False, f"content-type {ctype or '?'}"
    return False, why


# ------------------------------------------------------------------ playlist

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


def playlist_query(token: str, expires: str | None, asn: str | None) -> str:
    params = {"token": token, "expires": expires, "h": "1", "scz": "1", "lang": "it"}
    if asn:
        params["asn"] = asn
    return urlencode(params)


def playlist_url_from_embed(html: str, embed_url: str) -> str:
    i = html.find("window.masterPlaylist")
    if i == -1:
        raise ScError("window.masterPlaylist assente (embed scaduto o protetto)")
    attrs = dict(RE_ATTR.findall(html[i:i + 900]))
    token, expires, asn = attrs.get("token"), attrs.get("expires"), attrs.get("asn", "")
    if not token:
        raise ScError("token assente nell'embed")
    m = RE_MASTER_URL.search(html)
    vid = RE_EMBED_ID.search(embed_url)
    if m:
        base = unescape(m.group(1))
    elif vid:
        base = f"https://{urlparse(embed_url).netloc}/playlist/{vid.group(1)}"
    else:
        raise ScError("id video non ricavabile")
    return base + "?" + playlist_query(token, expires, asn)


def alternative_streams(html: str, embed_url: str) -> list[str]:
    """Server1 (ub=1) / Server2 (ab=1) esposti in window.streams, stesso token."""
    i = html.find("window.masterPlaylist")
    if i == -1:
        return []
    attrs = dict(RE_ATTR.findall(html[i:i + 900]))
    token, expires = attrs.get("token"), attrs.get("expires")
    if not token:
        return []
    m = RE_STREAMS.search(html)
    if not m:
        return []
    try:
        items = json.loads(m.group(1))
    except Exception:
        return []
    out = []
    for it in items:
        if not isinstance(it, dict) or not it.get("url"):
            continue
        u = unescape(it["url"])
        sep = "&" if "?" in u else "?"
        out.append(u + sep + playlist_query(token, expires, attrs.get("asn", "")))
    return out


def _load_playlist(url: str, referer: str, timeout: int) -> str | None:
    try:
        pr = requests.get(url, headers={"User-Agent": UA, "Referer": referer, "Accept": "*/*"},
                          timeout=(4, timeout), allow_redirects=True)
    except Exception:
        return None
    return pr.text if "#EXTM3U" in pr.text else None


def da_embed(embed: str, variant: int | None, timeout: int, log) -> dict | None:
    """mp4 diretto (downloadUrl) oppure master playlist -> variante richiesta."""
    try:
        r = requests.get(embed, headers={"User-Agent": UA, "Referer": embed},
                         timeout=(4, timeout), allow_redirects=True)
        r.raise_for_status()
    except Exception as e:
        raise ScError(f"embed non raggiungibile ({type(e).__name__})")
    html = r.text

    mp4 = RE_DOWNLOAD.search(html)
    if mp4:
        url = unescape(mp4.group(1))
        if log:
            log("downloadUrl diretto")
        ok, why = verify_source(url, embed, timeout)
        if log:
            log(f"{'OK' if ok else 'no'} {why}")
        if ok:
            return {"origin": url, "kind": "mp4", "variants": [], "audio_tracks": [],
                    "playlist": None, "embed": embed}

    candidates: list[str] = []
    try:
        candidates.append(playlist_url_from_embed(html, embed))
    except ScError as e:
        if log:
            log(str(e))
    candidates += alternative_streams(html, embed)

    for playlist in candidates:
        if log:
            log(f"playlist {urlparse(playlist).netloc}")
        text = _load_playlist(playlist, embed, timeout)
        if not text:
            if log:
                log("playlist non valida")
            continue

        variants = parse_variants(text, playlist)
        origin = playlist
        if variant:
            hit = [v for v in variants if v["height"] == variant]
            if hit:
                origin = hit[0]["url"]
        elif variants:
            origin = variants[0]["url"]

        ok, why = verify_source(origin, embed, timeout)
        if log:
            log(f"{'OK' if ok else 'no'} {why}")
        if ok:
            return {"origin": origin, "kind": "hls", "variants": variants,
                    "audio_tracks": audio_tracks(text, playlist), "playlist": playlist,
                    "embed": embed}

    return None


# ------------------------------------------------------------------ embed

def extract_embeds(html: str, page_url: str) -> list[str]:
    out: list[str] = []
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select("#embed[src], iframe[src]"):
        src = (node.get("src") or "").strip()
        if "/embed/" in src:
            out.append(unescape(urljoin(page_url, src)))
    for m in RE_EMBED_ANY.findall(html):
        out.append(unescape(urljoin(page_url, m)))
    seen, uniq = set(), []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


# ------------------------------------------------------------------ ricerca

def _parse_search(html: str, base: str) -> list[dict]:
    """Accetta sia HTML pieno sia frammento sia JSON (con o senza campo html)."""
    body = html.lstrip()
    if body[:1] in ("{", "["):
        try:
            data = json.loads(body)
        except Exception:
            data = None
        if isinstance(data, dict):
            for key in ("html", "data", "results", "anime"):
                inner = data.get(key)
                if isinstance(inner, str) and "/anime/" in inner:
                    html = inner
                    break
                if isinstance(inner, list):
                    out = _rows_from_json(inner, base)
                    if out:
                        return out
        elif isinstance(data, list):
            out = _rows_from_json(data, base)
            if out:
                return out

    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for a in soup.select('#anime-wrapper a.overlay-wrapper, a.overlay-wrapper, a[href*="/anime/"]'):
        m = RE_ANIME_URL.search(a.get("href", ""))
        if not m:
            continue
        aid = int(m.group(1))
        if aid in seen:
            continue
        seen.add(aid)

        title = ""
        img = a.find("img")
        node = a.select_one(".title")
        if node:
            title = node.get_text(" ", strip=True)
        if not title:
            if img and img.get("alt"):
                title = img["alt"].strip()
        if not title:
            title = a.get("data-title") or a.get("title") or m.group(2).replace("-", " ")

        poster = ""
        if img:
            poster = (img.get("src") or img.get("data-src")
                      or img.get("data-original") or "").strip()
            if poster:
                poster = urljoin(base, poster)

        card = a.find_parent(class_="anime-item") or a.parent
        year = ""
        if card is not None:
            y = re.search(r"\b(19|20)\d{2}\b", card.get_text(" ", strip=True))
            if y:
                year = y.group(0)

        out.append({
            "id": aid,
            "slug": f"{m.group(1)}-{m.group(2)}",
            "title": re.sub(r"\s+", " ", title)[:140],
            "year": year,
            "poster": poster,
            "url": f"{base}/anime/{m.group(1)}-{m.group(2)}",
        })
    return out


def _rows_from_json(items: list, base: str) -> list[dict]:
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        href = it.get("url") or it.get("link") or ""
        m = RE_ANIME_URL.search(href)
        if not m and it.get("id") is not None:
            slug = it.get("slug") or it.get("title") or ""
            href = f"/anime/{it['id']}-{slug}"
            m = RE_ANIME_URL.search(href)
        if not m:
            continue
        poster = it.get("poster") or it.get("image") or it.get("cover") or ""
        if isinstance(poster, dict):
            poster = poster.get("url") or poster.get("src") or ""
        out.append({
            "id": int(m.group(1)),
            "slug": f"{m.group(1)}-{m.group(2)}",
            "title": re.sub(r"\s+", " ", str(it.get("title") or it.get("name") or m.group(2)))[:140],
            "year": str(it.get("year") or ""),
            "poster": urljoin(base, str(poster)) if poster else "",
            "url": f"{base}/anime/{m.group(1)}-{m.group(2)}",
        })
    return out


def search(query: str, base: str, limit: int = 20, audio: str = "auto",
           allow_browser: bool = True, timeout: int = 25, log=None) -> list[dict]:
    key = f"search:{base}:{query}"
    hit = cache_get(key)
    if hit is not None:
        res = list(hit)  # type: ignore[arg-type]
    else:
        url = f"{base}/archivio?title={quote_plus(query)}"
        html = fetch_html(url, referer=base + "/", timeout=timeout,
                          allow_browser=allow_browser,
                          wait_sel="#anime-wrapper a.overlay-wrapper", log=log)
        res = _parse_search(html, base)
        if not res and allow_browser:
            if log:
                log("nessuna card -> riprovo col browser")
            html = browser_html(url, base + "/", timeout,
                                "#anime-wrapper a.overlay-wrapper")
            res = _parse_search(html, base)
        if not res:
            raise ScError("nessun risultato")
        cache_put(key, res)

    res = list(res)
    if audio in ("ita", "dub", "auto"):
        res.sort(key=lambda r: 0 if re.search(r"\bita\b", r["title"].lower()) else 1)
    elif audio == "sub":
        res.sort(key=lambda r: (
            0 if re.search(r"\bsub\b", r["title"].lower()) else 1,
            1 if re.search(r"\bita\b", r["title"].lower()) else 0,
        ))
    return res[:limit]


# ------------------------------------------------------------------ episodi

def _eps_da_window_anime(html: str) -> list[dict]:
    m = RE_WINDOW_ANIME.search(html)
    if not m:
        return []
    i = html.find("{", m.end())
    if i < 0:
        return []
    depth = 0
    for k in range(i, len(html)):
        c = html[k]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    data = json.loads(html[i:k + 1])
                except Exception:
                    return []
                out = []
                for ep in data.get("episodes") or []:
                    if not isinstance(ep, dict) or ep.get("id") is None:
                        continue
                    num = ep.get("number")
                    try:
                        n = float(num)
                    except (TypeError, ValueError):
                        n = None
                    out.append({"number": n, "ep_id": str(ep["id"]),
                                "label": f"Episodio {num}", "flags": []})
                return out
    return []


def _eps_da_info_api(s: requests.Session, base: str, anime_id: str, referer: str,
                     timeout: int) -> list[dict]:
    for path in ("/info_api?id={}", "/info_api.php?id={}"):
        for api in (True, False):
            url = base + path.format(anime_id)
            try:
                raw = get(s, url, referer=referer, timeout=timeout, api=api)
                data = json.loads(raw)
            except Exception:
                continue
            eps = []
            for ep in data.get("episodes") or []:
                if not isinstance(ep, dict) or ep.get("id") is None:
                    continue
                num = ep.get("number")
                try:
                    n = float(num)
                except (TypeError, ValueError):
                    n = None
                eps.append({"number": n, "ep_id": str(ep["id"]),
                            "label": f"Episodio {num}", "flags": []})
            if eps:
                return eps
    return []


def _eps_da_dom(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for node in soup.select(".episode-wrapper .episode-item"):
        txt = node.get_text(" ", strip=True)
        m = re.search(r"(\d+(?:\.\d+)?)", txt)
        if not m:
            continue
        out.append({"number": float(m.group(1)), "label": f"Episodio {m.group(1)}",
                    "flags": []})
    return out


def _first_embed_id(html: str) -> int | None:
    node = BeautifulSoup(html, "html.parser").select_one("#embed[src], iframe[src]")
    src = node.get("src") if node else None
    if not src:
        m = RE_EMBED_ANY.search(html)
        src = m.group(0) if m else None
    if not src:
        return None
    m = RE_EMBED_ID.search(unescape(src))
    return int(m.group(1)) if m else None


def _active_number(html: str) -> float | None:
    node = BeautifulSoup(html, "html.parser").select_one("#video-top .title")
    if not node:
        return None
    m = RE_EP_TITLE.search(node.get_text(" ", strip=True))
    return float(m.group(1)) if m else None


def _calibrate(anime: str, first_id: int, active: float, timeout: int,
               allow_browser: bool, verbose: bool = False) -> int | None:
    for d in (0, 1, -1, 2, -2, 3, -3, 4, -4, 5, -5, 8, -8):
        probe = first_id + d
        if probe <= 0:
            continue
        try:
            html = fetch_html(watch_url(anime, probe), referer=anime, timeout=timeout,
                              allow_browser=allow_browser, must_contain=("#embed",),
                              wait_sel="#embed[src]")
        except Exception:
            continue
        num = _active_number(html)
        if verbose:
            print(f"[calibra] ep={probe} -> {num}", file=sys.stderr)
        if num == active:
            return d
    return None


def episodes(anime_url: str, timeout: int = 25, verbose: bool = False,
             allow_browser: bool = True, log=None) -> list[dict]:
    anime = anime_url_from(anime_url)
    key = f"eps:{anime}"
    hit = cache_get(key)
    if hit is not None:
        return list(hit)  # type: ignore[arg-type]

    html = fetch_html(anime, referer=base_of(anime), timeout=timeout,
                      allow_browser=allow_browser,
                      must_contain=("episode-wrapper",), wait_sel="#episode-nav", log=log)

    eps = _eps_da_window_anime(html)
    if verbose:
        print(f"[episodi] window.anime: {len(eps)}", file=sys.stderr)

    if not eps:
        aid = anime_id_from(anime)
        if aid:
            try:
                eps = _eps_da_info_api(shared_session(), base_of(anime), aid, anime, timeout)
                if verbose:
                    print(f"[episodi] info_api: {len(eps)}", file=sys.stderr)
            except Exception as e:
                if verbose:
                    print(f"[episodi] info_api KO ({type(e).__name__})", file=sys.stderr)

    if not eps:
        eps = _eps_da_dom(html)
        if verbose:
            print(f"[episodi] dom: {len(eps)}", file=sys.stderr)
        if eps:
            first_id = _first_embed_id(html)
            active = _active_number(html)
            if first_id is None or active is None:
                raise ScError("griglia episodi senza id allineabili")
            delta = _calibrate(anime, first_id, active, timeout, allow_browser, verbose)
            if delta is None:
                raise ScError("ep-id non allineabili alla numerazione")
            if verbose:
                print(f"[episodi] scostamento {delta}", file=sys.stderr)
            for e in eps:
                n = e["number"]
                e["ep_id"] = str(first_id + delta + int(round(n - active)))

    if not eps:
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.select('a[href*="?ep="]'):
            eid = ep_id_from(a.get("href", ""))
            if not eid:
                continue
            label = (a.get("title") or a.get_text(" ", strip=True) or "").strip()
            m = re.search(r"(\d+(?:\.\d+)?)", label)
            eps.append({"number": float(m.group(1)) if m else None, "ep_id": eid,
                        "label": label or f"Episodio {eid}", "flags": []})

    if not eps:
        raise ScError("nessun episodio trovato")

    visti, uniq = set(), []
    for e in eps:
        eid = e.get("ep_id")
        if not eid or eid in visti:
            continue
        visti.add(eid)
        e["url"] = watch_url(anime, eid)
        uniq.append(e)
    uniq.sort(key=lambda e: (e["number"] is None, e["number"] or 0))
    cache_put(key, uniq)
    return list(uniq)


# ------------------------------------------------------------------ risoluzione

def resolve_http(watch: str, variant: int | None = None, timeout: int = 20,
                 log=None, dead_hosts: set[str] | None = None,
                 allow_browser: bool = True) -> dict:
    t0 = time.time()
    dead = dead_hosts if dead_hosts is not None else set()

    def L(msg: str) -> None:
        if log:
            log(f"{msg} [{time.time() - t0:.1f}s]")

    ep_id = ep_id_from(watch)
    if not ep_id:
        raise NoPlayer("URL senza ?ep=<id>")
    anime = anime_url_from(watch)

    L(f"pagina ep {ep_id}")
    html = fetch_html(watch_url(anime, ep_id), referer=anime, timeout=timeout,
                      allow_browser=allow_browser, must_contain=("/embed/",),
                      wait_sel="#embed[src]", log=L)

    embeds = extract_embeds(html, watch)
    if not embeds:
        raise ScError("nessun embed nella pagina")

    for embed in embeds:
        if host_of(embed) in dead:
            continue
        L(f"embed {host_of(embed)}")
        try:
            got = da_embed(embed, variant, timeout, L)
        except Exception as e:
            L(f"embed KO ({type(e).__name__})")
            got = None
        if got:
            got.update({"watch": watch, "via": "http",
                        "headers": {"Referer": embed, "User-Agent": UA}})
            return got
        dead.add(host_of(embed))
    raise ScError("nessuna sorgente valida dall'embed")


def resolve_browser(watch: str, headed: bool, verbose: bool, variant: int | None = None,
                    timeout: int = 20, budget: int = 90, log=None,
                    dead_hosts: set[str] | None = None) -> dict:
    t0 = time.time()
    dead = dead_hosts if dead_hosts is not None else set()

    def L(msg: str) -> None:
        if log:
            log(f"{msg} [{time.time() - t0:.1f}s]")

    embs: list[str] = []
    hits: list[str] = []

    ctx = _browser_ctx()
    page = ctx.new_page()
    try:
        def on_req(req):
            u = req.url
            if "/embed/" in u and u not in embs:
                embs.append(u)
                L(f"embed -> {u[:70]}")
            if (".mp4" in u or ".m3u8" in u) and u not in hits:
                if not any(h in u.lower() for h in AD_HOSTS):
                    hits.append(u)

        page.on("request", on_req)
        L("goto")
        page.goto(watch, wait_until="domcontentloaded", timeout=timeout * 4000)
        page.wait_for_timeout(2500)

        for sel in (".play", "#player", "video"):
            if embs or hits:
                break
            try:
                page.click(sel, timeout=1500)
            except Exception:
                pass
            page.wait_for_timeout(1200)

        if not embs and not hits:
            try:
                src = page.evaluate("""() => {
                    const f = document.querySelector('#embed[src], iframe');
                    return f ? f.getAttribute('src') : null;
                }""")
                if src:
                    embs.append(urljoin(watch, unescape(src)))
                    L(f"iframe -> {src[:70]}")
            except Exception:
                pass
        referer = page.url or watch
    finally:
        try:
            page.close()
        except Exception:
            pass

    for emb in list(dict.fromkeys(embs)):
        if host_of(emb) in dead:
            continue
        try:
            got = da_embed(emb, variant, timeout, L)
        except Exception as e:
            L(f"embed KO ({type(e).__name__})")
            continue
        if got:
            got.update({"watch": watch, "via": "browser",
                        "headers": {"Referer": emb, "User-Agent": UA}})
            return got
        dead.add(host_of(emb))

    for url in list(dict.fromkeys(hits)):
        ok, why = verify_source(url, referer, timeout)
        if log:
            log(f"rete {'OK' if ok else 'no'} {why}")
        if ok:
            return {"watch": watch, "origin": url, "kind": "hls" if ".m3u8" in url else "mp4",
                    "variants": [], "audio_tracks": [], "playlist": None, "embed": None,
                    "via": "browser", "headers": {"Referer": referer, "User-Agent": UA}}

    raise ScError("nessuna sorgente valida dal browser")


def resolve(watch: str, variant: int | None, force_browser: bool, headed: bool,
            verbose: bool, retries: int, sleep: float, timeout: int, log=None,
            allow_browser: bool = True, budget: int = 90,
            dead_hosts: set[str] | None = None) -> dict:
    last = None
    skip_http = force_browser
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(sleep * (2 ** attempt))
            if verbose:
                print(f"[retry {attempt}] {watch}", file=sys.stderr)
        try:
            if not skip_http:
                try:
                    return resolve_http(watch, variant, timeout, log, dead_hosts,
                                        allow_browser)
                except NoPlayer as e:
                    last = e
                    skip_http = True
                    if log:
                        log("URL senza ?ep -> browser")
                except Exception as e:
                    last = e
                    if log:
                        log(f"HTTP fallito: {type(e).__name__}: {e}")
                    if not allow_browser:
                        continue
            if not allow_browser:
                continue
            return resolve_browser(watch, headed, verbose, variant, timeout, budget, log, dead_hosts)
        except Exception as e:
            last = e
    raise last if last else ScError("risoluzione fallita")


# ------------------------------------------------------------------ utils CLI

def parse_picks(spec: str, eps: list[dict]) -> list[dict]:
    if spec.strip().lower() == "all":
        return eps
    wanted: list[float] = []
    for part in spec.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            lo, hi = float(a), float(b)
            wanted += [e["number"] for e in eps if e["number"] is not None and lo <= e["number"] <= hi]
        else:
            t = float(part)
            wanted += [e["number"] for e in eps if e["number"] == t]
    picked, seen = [], set()
    for n in wanted:
        for e in eps:
            if e["number"] == n and e["url"] not in seen:
                seen.add(e["url"])
                picked.append(e)
    return picked


def chunks(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield i // size + 1, seq[i:i + size]


def load_done(path: str) -> set[str]:
    done = set()
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    done.add(json.loads(line)["watch"])
                except Exception:
                    pass
    return done


def main() -> int:
    ap = argparse.ArgumentParser(description="AnimeUnity: ricerca, episodi, playlist HLS")
    ap.add_argument("target", nargs="?")
    ap.add_argument("-s", "--search")
    ap.add_argument("-p", "--pick")
    ap.add_argument("-e", "--episodes", help="1-12 | 1,5 | all")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--chunk", type=int, default=50)
    ap.add_argument("--out")
    ap.add_argument("-n", "--limit", type=int, default=20)
    ap.add_argument("--base", default=BASE_DEFAULT)
    ap.add_argument("--sleep", type=float, default=1.0)
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--timeout", type=int, default=25)
    ap.add_argument("--budget", type=int, default=90)
    ap.add_argument("--audio", choices=("auto", "ita", "dub", "sub"), default="auto")
    ap.add_argument("--variant", type=int, help="altezza, es. 1080")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--browser", action="store_true")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    log = lambda m: print(f"[{m}]", end=" ", file=sys.stderr, flush=True)
    dead: set[str] = set()
    allow_browser = not a.no_browser

    if a.search:
        try:
            res = search(a.search, a.base, a.limit, a.audio, allow_browser, a.timeout, log)
        except Exception as e:
            print(f"errore: {e}", file=sys.stderr)
            return 1
        for i, r in enumerate(res, 1):
            year = f" [{r['year']}]" if r.get("year") else ""
            print(f"{i:2}. {r['title']}{year} (id {r['id']})")
        if a.json and not a.pick:
            print(json.dumps(res, indent=2, ensure_ascii=False))
            return 0
        pick = a.pick or (input("\nnumero: ").strip() if sys.stdin.isatty() else "")
        if not pick:
            return 0
        try:
            target = res[int(pick) - 1]["url"]
        except (ValueError, IndexError):
            print("selezione non valida", file=sys.stderr)
            return 1
    else:
        if not a.target:
            ap.error("serve un URL oppure -s QUERY")
        target = a.target

    if ep_id_from(target):
        try:
            r = resolve(target, a.variant, a.browser, a.headed, a.verbose, a.retries,
                        a.sleep, a.timeout, log, allow_browser, a.budget, dead)
        except Exception as e:
            print(f"errore: {e}", file=sys.stderr)
            return 1
        print(json.dumps(r, indent=2, ensure_ascii=False) if a.json else r["origin"])
        return 0

    try:
        eps = episodes(target, a.timeout, a.verbose, allow_browser, log)
    except Exception as e:
        print(f"errore: {e}", file=sys.stderr)
        return 1
    print(f"episodi trovati: {len(eps)}", file=sys.stderr)

    spec = "all" if a.all else a.episodes
    if spec is None and sys.stdin.isatty():
        print("\n".join(f"{i:4}. {e['label']}" for i, e in enumerate(eps, 1)))
        spec = input("\nepisodi (es. 1-12 / all / invio = nessuno): ").strip()
        if not spec:
            return 0
    if spec is None:
        for i, e in enumerate(eps, 1):
            print(f"{i:4}. {e['label']}")
        return 0

    picked = parse_picks(spec, eps)
    if not picked:
        print("nessun episodio corrisponde", file=sys.stderr)
        return 1

    done = load_done(a.out)
    if done:
        salto = len([e for e in picked if e["url"] in done])
        picked = [e for e in picked if e["url"] not in done]
        print(f"salto {salto} gia' fatti", file=sys.stderr)

    sink = open(a.out, "a", encoding="utf-8") if a.out else None
    risultati: list[dict] = []
    errori = 0
    try:
        for n, block in chunks(picked, max(1, a.chunk)):
            print(f"\n=== blocco {n}: {block[0]['label']} -> {block[-1]['label']} ===",
                  file=sys.stderr)
            for e in block:
                print(f"  {e['label']} ... ", end="", file=sys.stderr, flush=True)
                try:
                    r = resolve(e["url"], a.variant, a.browser, a.headed, a.verbose, a.retries,
                                a.sleep, a.timeout, log, allow_browser, a.budget, dead)
                except Exception as ex:
                    errori += 1
                    print(f"errore: {ex}", file=sys.stderr)
                    continue
                r["label"] = e["label"]
                r["number"] = e["number"]
                print(f"ok ({r['via']})", file=sys.stderr)
                risultati.append(r)
                if sink:
                    sink.write(json.dumps(r, ensure_ascii=False) + "\n")
                    sink.flush()
                elif not a.json:
                    print(r["origin"])
                time.sleep(a.sleep)
    finally:
        if sink:
            sink.close()

    print(f"\nfatti {len(risultati)}, errori {errori}", file=sys.stderr)
    if a.json:
        print(json.dumps(risultati, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())