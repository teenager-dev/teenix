#!/usr/bin/env python3
"""
AnimeSaturn: ricerca, lista episodi, estrazione sorgente video validata.

Uso:
    python animesaturn.py -s "naruto"
    python animesaturn.py "<url anime>" -e 1-100 --out naruto.jsonl
    python animesaturn.py "https://animesaturn.at/watch/naruto-wiufof/ep-1" --json
    python animesaturn.py "<url>" -e all --audio sub
    python animesaturn.py "<url ep>" -v --headed --debug

Catena di risoluzione:
    Il player non è nell'HTML grezzo e non è nemmeno un iframe: e' un div con il
    documento del player dentro, riempito dal JS dopo l'AJAX del click sul server.
    Quindi: click Server 1, 2, 3 ... (dub prima, poi sub) e, dopo ogni click,
    lettura di #player (video src, config audios, mp4/m3u8) piu' la rete.
    Audio: dub se disponibile, altrimenti sub (--audio per forzare).
    Host che rispondono 1027 (quota Cloudflare) vengono deprioritizzati.

Diagnostica:
    ogni fase e' loggata su stderr con i secondi trascorsi; --debug salva l'HTML
    in ./debug; --no-browser isola il solo percorso HTTP.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from urllib.parse import parse_qsl, quote_plus, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

BASE_DEFAULT = "https://animesaturn.at"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

RE_MEDIA = re.compile(r'https?://[^\s"\'<>\\)]+\.(?:mp4|m3u8)[^\s"\'<>\\)]*', re.I)
RE_EMBED = re.compile(r'(?:src|data-src|data-original)="(https?://[^"\']+/embed/[^"\']+)"', re.I)
RE_EMBED_ANY = re.compile(r'https?://[^\s"\'<>\\]+/embed/[\w\-./]+(?:\?[^\s"\'<>\\]*)?', re.I)
RE_EP_IN_URL = re.compile(r"/ep[-/](\d+(?:\.\d+)?)", re.I)
RE_SLUG_FROM_WATCH = re.compile(r"/watch/([^/]+)/ep[-/]", re.I)

AUDIO_ORDER = {"auto": ("dub", "sub"), "dub": ("dub", "sub"), "sub": ("sub", "dub")}

AD_HOSTS = ("epicgameshubham", "doubleclick", "googletagmanager", "googlesyndication",
            "adservice", "banner", "pixel")


class ScError(Exception):
    pass


class NoPlayer(ScError):
    """L'HTML non contiene il player: serve il browser."""


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    })
    return s


def get(s: requests.Session, url: str, referer: str | None = None, timeout: int = 30) -> str:
    h = {"Referer": referer} if referer else {}
    r = s.get(url, headers=h, timeout=(5, timeout), allow_redirects=True)
    r.raise_for_status()
    return r.text


def with_page(url: str, n: int) -> str:
    p = urlparse(url)
    q = [(k, v) for k, v in parse_qsl(p.query) if k != "page"] + [("page", str(n))]
    return urlunparse(p._replace(query=urlencode(q)))


def anime_url_from(url: str) -> str:
    p = urlparse(url)
    m = RE_SLUG_FROM_WATCH.search(p.path)
    if m:
        return f"{p.scheme}://{p.netloc}/anime/{m.group(1)}"
    return url


def host_of(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower()
    except Exception:
        return ""


def verify_source(url: str, referer: str, timeout: int = 20, tries: int = 2) -> tuple[bool, str]:
    """True se l'URL serve davvero un video. Un 429-HTML (Cloudflare 1027) e' un no."""
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
        is_html = b"<html" in low or b"<!doctype" in low or b"error 1027" in low or b"rate limited" in low
        if is_html:
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


def _scan_embeds(text: str) -> list[str]:
    text = text.replace("\\/", "/").replace("&amp;", "&")
    return list(dict.fromkeys(m.group(0) for m in RE_EMBED_ANY.finditer(text)))


def _scan_media(text: str) -> list[str]:
    text = text.replace("\\/", "/").replace("&amp;", "&")
    out = [m.group(0) for m in RE_MEDIA.finditer(text)]
    return [u for u in dict.fromkeys(out) if not any(h in u.lower() for h in AD_HOSTS)]


def _audios_from_text(html: str) -> dict:
    """Estrae il blocco 'audios: {...}' (config del player) dal testo."""
    i = html.find("audios:")
    if i < 0:
        return {}
    j = html.find("{", i)
    if j < 0:
        return {}
    depth = 0
    for k in range(j, len(html)):
        c = html[k]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[j:k + 1].replace("\\/", "/"))
                except Exception:
                    return {}
    return {}


# ------------------------------------------------------------------ ricerca

def search(query: str, base: str, limit: int = 20) -> list[dict]:
    s = session()
    soup = BeautifulSoup(get(s, f"{base}/filter?keyword={quote_plus(query)}"), "html.parser")

    out, seen = [], set()
    for a in soup.select('a[href*="/anime/"], a[href*="/watch/"]'):
        href = a.get("href", "")
        m = RE_SLUG_FROM_WATCH.search(href)
        if m:
            slug = m.group(1)
        elif "/anime/" in href:
            slug = href.split("/anime/", 1)[1].strip("/").rstrip("/")
        else:
            continue
        if not slug or slug in seen:
            continue
        seen.add(slug)

        title = (a.get("title") or "").strip()
        if not title:
            img = a.find("img")
            title = (img.get("alt") if img and img.get("alt") else "").strip()
        if not title:
            card = a.find_parent(["div", "li", "article"])
            node = card.select_one(".name, .title, h2, h3") if card else None
            title = (node.get_text(" ", strip=True) if node else "") or slug.replace("-", " ")
        out.append({"slug": slug, "title": re.sub(r"\s+", " ", title)[:140],
                    "url": f"{base}/anime/{slug}"})
        if len(out) >= limit:
            break
    if not out:
        raise ScError("nessun risultato")
    return out


# ------------------------------------------------------------------ episodi

def _parse_eps(html: str, page_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for a in soup.select("a.an-ep[data-num], a.an-ep, a[href*='/ep-'], a[href*='/ep/']"):
        href = a.get("href", "")
        if not href or href in seen:
            continue
        seen.add(href)

        num = None
        if a.get("data-num"):
            try:
                num = float(a["data-num"])
            except ValueError:
                pass
        if num is None:
            m = RE_EP_IN_URL.search(href)
            if m:
                num = float(m.group(1))

        label = (a.get("title") or a.get_text(" ", strip=True) or "").strip()
        flags = []
        if str(a.get("data-sub")) == "1":
            flags.append("sub")
        if str(a.get("data-dub")) == "1":
            flags.append("dub")
        out.append({
            "number": num,
            "label": re.sub(r"\s+", " ", label) or f"Episodio {num}",
            "flags": flags,
            "url": urljoin(page_url, href),
        })
    return out


def _next_page_links(html: str) -> set[int]:
    soup = BeautifulSoup(html, "html.parser")
    nums = set()
    for a in soup.select('a[href*="page="], [data-page]'):
        v = a.get("data-page")
        if v and str(v).isdigit():
            nums.add(int(v))
            continue
        m = re.search(r"[?&]page=(\d+)", a.get("href", ""))
        if m:
            nums.add(int(m.group(1)))
    return nums


def episodes(anime_url: str, max_pages: int = 200, verbose: bool = False) -> list[dict]:
    s = session()
    found: dict[str, dict] = {}
    first_html = None
    page = 1

    while page <= max_pages:
        html = get(s, with_page(anime_url, page)) if page > 1 else get(s, anime_url)
        if first_html is None:
            first_html = html
        batch = _parse_eps(html, anime_url)
        new = [e for e in batch if e["url"] not in found]
        if verbose:
            print(f"[episodi] pagina {page}: {len(batch)} link, {len(new)} nuovi", file=sys.stderr)
        if not new:
            break
        for e in new:
            found[e["url"]] = e
        nxt = _next_page_links(html)
        if page + 1 in nxt or (not nxt and len(batch) >= 15 and page == 1):
            page += 1
        else:
            break

    if not found:
        raise ScError("nessun episodio trovato")
    eps = list(found.values())
    eps.sort(key=lambda e: (e["number"] is None, e["number"] or 0, e["url"]))
    return eps


# ------------------------------------------------------------------ sorgenti

def _source_json_url(embed: str) -> str:
    p = urlparse(embed)
    path = p.path.rstrip("/")
    if not path.endswith("source.json"):
        path += "/source.json"
    return urlunparse(p._replace(path=path, query="", fragment=""))


def _audios_from_json(embed: str, verbose: bool = False, timeout: int = 15) -> dict:
    url = _source_json_url(embed)
    last = "?"
    for n in range(2):
        if n:
            time.sleep(1.2)
        r = requests.get(url, headers={"User-Agent": UA, "Referer": embed,
                                       "Accept": "application/json, */*"},
                         timeout=(4, timeout), allow_redirects=True)
        if r.status_code in (429, 403):
            last = f"HTTP {r.status_code}"
            continue
        r.raise_for_status()
        data = r.json()
        if verbose:
            print(f"[source.json] {url[:100]} -> {list((data.get('audios') or {}).keys())}", file=sys.stderr)
        return data.get("audios") or {}
    raise ScError(f"source.json {last}")


def _audios_from_html(embed: str, timeout: int = 15) -> dict:
    r = requests.get(embed, headers={"User-Agent": UA, "Referer": BASE_DEFAULT + "/"},
                     timeout=(4, timeout), allow_redirects=True)
    r.raise_for_status()
    return _audios_from_text(r.text)


def _flatten(audios: dict, audio: str) -> tuple[list[str], list[dict]]:
    a = audios.get(audio) or {}
    urls: list[str] = []
    for prov in a.get("providers") or []:
        for part in prov.get("parts") or []:
            if part.get("url"):
                urls.append(part["url"].replace("\\/", "/"))
    return urls, (a.get("subtitles") or [])


def _pick(urls: list[str], audio: str, referer: str, timeout: int, log,
          dead_hosts: set[str]) -> dict | None:
    """Primo URL verificato; host gia' morti per quota finiscono in coda."""
    if not urls:
        return None
    def rank(u: str) -> int:
        return 1 if host_of(u) in dead_hosts else 0
    ordered = sorted(dict.fromkeys(urls), key=rank)
    soft = None
    for url in ordered:
        if any(h in url.lower() for h in AD_HOSTS):
            continue
        if log:
            log(f"verifica {audio}")
        ok, why = verify_source(url, referer, timeout)
        if log:
            log(f"{'OK' if ok else 'no'} {why}")
        if "quota" in why:
            dead_hosts.add(host_of(url))
        if ok:
            return {"audio": audio, "source": url, "referer": referer}
        if soft is None and why == "HTTP 429":
            soft = {"audio": audio, "source": url, "referer": referer}
    return soft


def _pick_audios(audios: dict, audio: str, referer: str, timeout: int, log,
                 dead_hosts: set[str]) -> dict | None:
    soft = None
    for lang in AUDIO_ORDER.get(audio, AUDIO_ORDER["auto"]):
        urls, subs = _flatten(audios, lang)
        got = _pick(urls, lang, referer, timeout, log, dead_hosts)
        if got:
            got["subtitles"] = [x.get("url") for x in subs if x.get("url")]
            got["sources"] = urls
            if "verifica" not in "":  # no-op, tiene la forma
                pass
            if verify_source.__name__:
                pass
            if got.get("source"):
                # soft e' indistinguibile: se e' un 429 lo lasciamo decidere al chiamante
                pass
            return got
    return soft


def resolve_http(episode_url: str, verbose: bool = False, audio: str = "auto",
                 timeout: int = 15, log=None, debug_dir: str | None = None,
                 dead_hosts: set[str] | None = None) -> dict:
    t0 = time.time()
    dead = dead_hosts if dead_hosts is not None else set()

    def L(msg: str) -> None:
        if log:
            log(f"{msg} [{time.time() - t0:.1f}s]")

    s = session()
    L("GET pagina")
    html = get(s, episode_url, timeout=timeout)
    L(f"pagina {len(html)}B")
    if debug_dir:
        with open(os.path.join(debug_dir, "ep.html"), "w", encoding="utf-8") as f:
            f.write(html)

    embeds = []
    for m in RE_EMBED.finditer(html):
        embeds.append(urljoin(episode_url, m.group(1).replace("&amp;", "&")))
    embeds += _scan_embeds(html)
    embeds = [e for e in dict.fromkeys(embeds) if "/embed/" in e]
    L(f"{len(embeds)} player")
    if not embeds:
        raise NoPlayer("nessun player nell'HTML")

    for emb in embeds:
        L(f"embed {emb.rsplit('/', 1)[-1][:40]}")
        audios = {}
        try:
            audios = _audios_from_json(emb, verbose, timeout)
        except Exception as e:
            L(f"source.json KO ({type(e).__name__})")
            try:
                audios = _audios_from_html(emb, timeout)
            except Exception as e2:
                L(f"embed KO ({type(e2).__name__})")
        got = _pick_audios(audios, audio, emb, timeout, L, dead)
        if got:
            return {"episode": episode_url, "audio": got["audio"], "source": got["source"],
                    "sources": got.get("sources", [got["source"]]),
                    "subtitles": got.get("subtitles", []), "via": "http",
                    "headers": {"Referer": got["referer"], "User-Agent": UA}}

    raise ScError("nessun media valido dai player della pagina")


def _server_order(page, audio: str) -> list[dict]:
    items = page.evaluate("""() => [...document.querySelectorAll('#w-servers li[data-link-id]')]
        .map((el, i) => ({i, type: (el.closest('fieldset')||{}).dataset?.type || '',
                          name: el.getAttribute('aria-label') || ''}))""")
    order = []
    for lang in AUDIO_ORDER.get(audio, AUDIO_ORDER["auto"]):
        order += [x for x in items if x["type"] == lang]
    order += [x for x in items if x not in order]
    return order


def _current_embed(page) -> str | None:
    return page.evaluate("""() => {
        const f = document.querySelector('#player iframe');
        const s = f && f.getAttribute('src');
        if (s) return s;
        const d = document.querySelector('#player [src]');
        return d ? d.getAttribute('src') : null;
    }""")


def _harvest(page, embed: str, deadline: float) -> tuple[dict, list[str]]:
    """Config o media dal frame se esiste, altrimenti dal DOM di #player."""
    while time.time() < deadline:
        frame = None
        try:
            base = (embed or "").split("?")[0]
            for f in page.frames:
                if f.url and base and base[:70] in f.url:
                    frame = f
                    break
        except Exception:
            frame = None

        if frame is not None:
            try:
                html = frame.content()
            except Exception:
                html = ""
            if html:
                audios = _audios_from_text(html)
                if audios:
                    return audios, []
                med = _scan_media(html)
                if med:
                    return {}, med
            try:
                src = frame.evaluate("""() => {
                    const v = document.querySelector('video');
                    return v ? (v.currentSrc || v.getAttribute('src')) : null;
                }""")
            except Exception:
                src = None
            if src and (".mp4" in src or ".m3u8" in src):
                return {}, [src]
        else:
            try:
                data = page.evaluate("""() => {
                    const p = document.querySelector('#player');
                    if (!p) return {html: '', src: null};
                    const v = p.querySelector('video');
                    return {html: p.innerHTML.slice(0, 400000),
                            src: v ? (v.currentSrc || v.getAttribute('src')) : null};
                }""")
            except Exception:
                data = {}
            if data.get("src") and (".mp4" in data["src"] or ".m3u8" in data["src"]):
                return {}, [data["src"]]
            html = data.get("html") or ""
            if html:
                audios = _audios_from_text(html)
                if audios:
                    return audios, []
                med = _scan_media(html)
                if med:
                    return {}, med
        time.sleep(0.4)
    return {}, []


def resolve_browser(episode_url: str, headed: bool, verbose: bool, audio: str = "auto",
                    timeout: int = 15, budget: int = 90, log=None,
                    dead_hosts: set[str] | None = None) -> dict:
    from playwright.sync_api import sync_playwright

    t0 = time.time()
    deadline = t0 + budget
    dead = dead_hosts if dead_hosts is not None else set()

    def L(msg: str) -> None:
        if log:
            log(f"{msg} [{time.time() - t0:.1f}s]")

    hits: list[str] = []
    embs: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=not headed, args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(user_agent=UA, locale="it-IT",
                                  viewport={"width": 1600, "height": 900},
                                  timezone_id="Europe/Rome")
        page = ctx.new_page()
        page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")

        def on_req(req):
            if ".mp4" in req.url or ".m3u8" in req.url:
                if req.url not in hits and not any(h in req.url.lower() for h in AD_HOSTS):
                    hits.append(req.url)

        def on_resp(resp):
            if "ajax/" not in resp.url and "episode" not in resp.url:
                return
            try:
                body = resp.text()
            except Exception:
                return
            for e in _scan_embeds(body):
                if e not in embs:
                    embs.append(e)
                    L(f"ajax -> {e[:70]}")

        page.on("request", on_req)
        page.on("response", on_resp)

        L("goto")
        page.goto(episode_url, wait_until="domcontentloaded", timeout=timeout * 4000)
        page.wait_for_timeout(2000)

        first = _current_embed(page)
        if first:
            embs.append(first)
            L(f"player iniziale -> {first[:70]}")

        order = _server_order(page, audio)
        L(f"server: {len(order)} (audio={audio})")

        for srv in order:
            if time.time() > deadline:
                L("budget esaurito")
                break
            try:
                page.locator("#w-servers li[data-link-id]").nth(srv["i"]).click(timeout=4000)
            except Exception:
                continue
            L(f"click {srv['name'] or srv['i']}")
            page.wait_for_timeout(700)

            emb = _current_embed(page)
            if emb and emb not in embs:
                embs.append(emb)
                L(f"iframe -> {emb[:70]}")

            audios, med = _harvest(page, emb or "", min(deadline, time.time() + 8))
            got = _pick_audios(audios, audio, emb or episode_url, timeout, L, dead) if audios else None
            if got:
                ctx.close()
                browser.close()
                return {"episode": episode_url, "audio": got["audio"], "source": got["source"],
                        "sources": got.get("sources", [got["source"]]),
                        "subtitles": got.get("subtitles", []), "via": "browser",
                        "headers": {"Referer": got["referer"], "User-Agent": UA}}

            picked = _pick(med, audio, emb or episode_url, timeout, L, dead)
            if picked:
                ctx.close()
                browser.close()
                return {"episode": episode_url, "audio": picked["audio"], "source": picked["source"],
                        "sources": med, "subtitles": [], "via": "browser",
                        "headers": {"Referer": picked["referer"], "User-Agent": UA}}

        referer = page.url or episode_url
        ctx.close()
        browser.close()

    for url in list(dict.fromkeys(hits)):
        ok, why = verify_source(url, referer, timeout)
        if log:
            log(f"rete {'OK' if ok else 'no'} {why}")
        if "quota" in why:
            dead.add(host_of(url))
        if ok or why == "HTTP 429":
            return {"episode": episode_url, "audio": audio, "source": url, "sources": [url],
                    "subtitles": [], "via": "browser",
                    "headers": {"Referer": referer, "User-Agent": UA}}

    raise ScError("nessuna sorgente valida dal browser")


def resolve(episode_url: str, force_browser: bool, headed: bool, verbose: bool,
            audio: str = "auto", retries: int = 2, sleep: float = 1.0,
            timeout: int = 15, log=None, debug_dir: str | None = None,
            allow_browser: bool = True, budget: int = 90,
            dead_hosts: set[str] | None = None) -> dict:
    last = None
    skip_http = force_browser
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(sleep * (2 ** attempt))
            if verbose:
                print(f"[retry {attempt}] {episode_url}", file=sys.stderr)
        try:
            if not skip_http:
                try:
                    return resolve_http(episode_url, verbose, audio, timeout, log, debug_dir, dead_hosts)
                except NoPlayer as e:
                    last = e
                    skip_http = True
                    if log:
                        log("HTML senza player -> browser")
                except Exception as e:
                    last = e
                    if log:
                        log(f"HTTP fallito: {type(e).__name__}: {e}")
                    if not allow_browser:
                        continue
            if not allow_browser:
                continue
            return resolve_browser(episode_url, headed, verbose, audio, timeout, budget, log, dead_hosts)
        except Exception as e:
            last = e
    raise last if last else ScError("risoluzione fallita")


# ------------------------------------------------------------------ utils

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
            wanted += [e["number"] for e in eps
                       if e["number"] is not None and lo <= e["number"] <= hi]
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
                    done.add(json.loads(line)["episode"])
                except Exception:
                    pass
    return done


def main() -> int:
    ap = argparse.ArgumentParser(description="AnimeSaturn: ricerca, episodi, sorgente video")
    ap.add_argument("target", nargs="?")
    ap.add_argument("-s", "--search")
    ap.add_argument("-p", "--pick")
    ap.add_argument("-e", "--episodes", help="1-100 | 101-200 | 1,5 | all")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--chunk", type=int, default=100)
    ap.add_argument("--out")
    ap.add_argument("--max-pages", type=int, default=200)
    ap.add_argument("-n", "--limit", type=int, default=20)
    ap.add_argument("--base", default=BASE_DEFAULT)
    ap.add_argument("--sleep", type=float, default=1.0)
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--timeout", type=int, default=15)
    ap.add_argument("--budget", type=int, default=90, help="secondi massimi per episodio nel browser")
    ap.add_argument("--audio", choices=("auto", "dub", "sub"), default="auto")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--browser", action="store_true")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    debug_dir = None
    if a.debug:
        debug_dir = os.path.join(os.getcwd(), "debug")
        os.makedirs(debug_dir, exist_ok=True)
    log = lambda m: print(f"[{m}]", end=" ", file=sys.stderr, flush=True)
    dead_hosts: set[str] = set()

    if a.search:
        try:
            res = search(a.search, a.base, a.limit)
        except Exception as e:
            print(f"errore: {e}", file=sys.stderr)
            return 1
        for i, r in enumerate(res, 1):
            print(f"{i:2}. {r['title']}")
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

    if RE_EP_IN_URL.search(target):
        try:
            r = resolve(target, a.browser, a.headed, a.verbose, a.audio, a.retries, a.sleep,
                        a.timeout, log, debug_dir, not a.no_browser, a.budget, dead_hosts)
        except Exception as e:
            print(f"errore: {e}", file=sys.stderr)
            return 1
        print(json.dumps(r, indent=2, ensure_ascii=False) if a.json else r["source"])
        return 0

    anime = anime_url_from(target)
    try:
        eps = episodes(anime, a.max_pages, a.verbose)
    except Exception as e:
        print(f"errore: {e}", file=sys.stderr)
        return 1
    print(f"episodi trovati: {len(eps)}", file=sys.stderr)

    spec = "all" if a.all else a.episodes
    if spec is None and sys.stdin.isatty():
        print("\n".join(f"{i:4}. {e['label']}" for i, e in enumerate(eps, 1)))
        spec = input("\nepisodi (es. 1-100 / all / invio = nessuno): ").strip()
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
        skipped = len([e for e in picked if e["url"] in done])
        picked = [e for e in picked if e["url"] not in done]
        print(f"salto {skipped} già fatti", file=sys.stderr)

    sink = open(a.out, "a", encoding="utf-8") if a.out else None
    results: list[dict] = []
    errors = 0
    try:
        for n, block in chunks(picked, max(1, a.chunk)):
            print(f"\n=== blocco {n}: {block[0]['label']} -> {block[-1]['label']} "
                  f"({len(block)} ep) ===", file=sys.stderr)
            for e in block:
                print(f"  {e['label']} ... ", end="", file=sys.stderr, flush=True)
                try:
                    r = resolve(e["url"], a.browser, a.headed, a.verbose, a.audio, a.retries, a.sleep,
                                a.timeout, log, debug_dir, not a.no_browser, a.budget, dead_hosts)
                except Exception as ex:
                    errors += 1
                    print(f"errore: {ex}", file=sys.stderr)
                    continue
                r["label"] = e["label"]
                r["number"] = e["number"]
                print(f"ok ({r['audio']}, {r['via']})", file=sys.stderr)
                results.append(r)
                if sink:
                    sink.write(json.dumps(r, ensure_ascii=False) + "\n")
                    sink.flush()
                elif not a.json:
                    print(r["source"])
                time.sleep(a.sleep)
    finally:
        if sink:
            sink.close()

    print(f"\nfatti {len(results)}, errori {errors}", file=sys.stderr)
    if a.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())