#!/usr/bin/env python3
"""
Server locale: AniList, TVmaze e Wikidata forniscono i metadati;
gli scraper vengono usati solo per risolvere gli stream durante la riproduzione.

    pip install fastapi uvicorn requests beautifulsoup4 playwright
    playwright install chromium
    python server.py --port 8000

Per le richieste ai servizi dati è possibile indicare un contatto tramite
TEENIX_CONTACT (indirizzo e-mail o URL del progetto).
"""
from __future__ import annotations

import argparse
import base64
import difflib
import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
SCRAPERS = ROOT / "scrapers"

if str(SCRAPERS) not in sys.path:
    sys.path.insert(0, str(SCRAPERS))

import animeunity as AU           # noqa: E402
import streamingcommunity as SC   # noqa: E402

UA = AU.UA
ANDROID_EMBEDDED = os.environ.get("TEENIX_ANDROID_EMBEDDED", "").strip().lower() in {
    "1", "true", "yes",
}
WIKIDATA_CONTACT = os.environ.get("TEENIX_CONTACT", os.environ.get("STREAMSCRAP_CONTACT", "")).strip()
WIKIDATA_UA = (
    f"Teenix/1.0 (local media catalog; contact: {WIKIDATA_CONTACT})"
    if WIKIDATA_CONTACT else "Teenix/1.0 (local media catalog)"
)
app = FastAPI(title="Teenix", docs_url=None, redoc_url=None)
logger = logging.getLogger(__name__)

OPENROUTER_MODELS = (
    "openrouter/free",
    "xingchen-agi/xing4.0-29b-a4b:free",
    "google/gemma-4-31b-it:free",
    "google/gemma-4-26b-a4b-it:free",
    "nvidia/nemotron-3.5-super-vl:free",
    "liquid/lfm-3-1.7b:free",
    "recursive/flint-v1.0:free",
)
ITALIAN_GENRES = {
    "action": "Azione",
    "adventure": "Avventura",
    "animation": "Animazione",
    "children": "Bambini",
    "comedy": "Commedia",
    "crime": "Crime",
    "drama": "Drammatico",
    "espionage": "Spionaggio",
    "family": "Famiglia",
    "food": "Cucina",
    "history": "Storico",
    "horror": "Horror",
    "legal": "Giudiziario",
    "medical": "Medical",
    "music": "Musicale",
    "mystery": "Mistero",
    "nature": "Natura",
    "romance": "Romantico",
    "science fiction": "Fantascienza",
    "science-fiction": "Fantascienza",
    "slice of life": "Vita quotidiana",
    "sports": "Sport",
    "supernatural": "Soprannaturale",
    "thriller": "Thriller",
    "travel": "Viaggi",
    "war": "Guerra",
    "western": "Western",
}


def _openrouter_key() -> str:
    for name in ("OPENROUTER_API_KEY", "open_router_key"):
        value = os.environ.get(name, "").strip()
        if value:
            return value

    env_file = ROOT / ".env"
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return ""
    except OSError as error:
        logger.warning("Impossibile leggere il file .env per OpenRouter: %s", type(error).__name__)
        return ""

    for line in lines:
        match = re.match(r"\s*(OPENROUTER_API_KEY|open_router_key)\s*=\s*(.*?)\s*$", line, re.I)
        if not match:
            continue
        value = match.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        else:
            value = value.split("#", 1)[0].strip()
        if value:
            return value
    return ""


OPENROUTER_API_KEY = _openrouter_key()

SESSION = requests.Session()
_adapter = requests.adapters.HTTPAdapter(pool_connections=32, pool_maxsize=32, max_retries=0)
SESSION.mount("https://", _adapter)
SESSION.mount("http://", _adapter)
SESSION.headers.update({"User-Agent": UA, "Accept-Language": "it-IT,it;q=0.9,en;q=0.8"})

_anime_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="animeunity")
_cache: dict[str, tuple[float, object]] = {}
_inflight: dict[str, threading.Event] = {}
_lock = threading.Lock()
_browser_slots = threading.Semaphore(2)


# ------------------------------------------------------------------ helper

def _cached(key: str, ttl: int, fn):
    """Cache con single-flight: richieste concorrenti sulla stessa chiave
    aspettano una sola esecuzione invece di duplicarla."""
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
        ev = _inflight.get(key)
        owner = ev is None
        if owner:
            ev = threading.Event()
            _inflight[key] = ev

    if not owner:
        ev.wait(30)
        with _lock:
            hit = _cache.get(key)
        if hit:
            return hit[1]

    try:
        value = fn()
        with _lock:
            _cache[key] = (now + ttl, value)
        return value
    finally:
        with _lock:
            _inflight.pop(key, None)
        ev.set()


def _fetch(target: str, referer: str, rng: str | None = None, attempts: int = 4):
    headers = {"Accept": "*/*", "Accept-Encoding": "identity"}
    if referer:
        headers["Referer"] = referer
        p = urlparse(referer)
        if p.scheme and p.netloc:
            headers["Origin"] = f"{p.scheme}://{p.netloc}"
    if rng:
        headers["Range"] = rng

    last: Exception | None = None
    for i in range(attempts):
        try:
            response = SESSION.get(target, headers=headers, stream=True,
                                   timeout=(10, 90), allow_redirects=True)
            if response.status_code not in (429, 500, 502, 503, 504) or i == attempts - 1:
                return response
            response.close()
            time.sleep(0.8 * (i + 1))
        except requests.RequestException as e:
            last = e
            if i < attempts - 1:
                time.sleep(0.8 * (i + 1))
    raise last


def _clean(s: str | None, limit: int = 200) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()[:limit]


def _looks_italian(text: str) -> bool:
    words = set(re.findall(r"[a-zàèéìòù]+", text.casefold()))
    markers = {
        "al", "alla", "alle", "allo", "anche", "attraverso", "con", "dalla",
        "dalle", "degli", "dei", "del", "della", "delle", "dello", "dove",
        "è", "gli", "il", "incontra", "la", "le", "mentre", "nel", "nella",
        "nelle", "nello", "non", "per", "più", "quando", "si", "sono", "sua",
        "suo", "una", "uno", "viene",
    }
    return len(words & markers) >= 2


def _translate_metadata(title: str, plot: str, genres: list[str] | None = None) -> dict:
    source = {
        "title": _clean(title, 200),
        "plot": _clean(plot, 1200),
        "genres": [_clean(genre, 80) for genre in (genres or [])[:30]],
    }
    if not any((source["title"], source["plot"], source["genres"])):
        return {**source, "translation_status": "not_needed"}
    if not OPENROUTER_API_KEY:
        return {**source, "translation_status": "not_configured"}

    cache_key = "openrouter:it:" + hashlib.sha256(
        json.dumps(source, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()

    def translate():
        last_error: Exception | None = None
        deadline = time.monotonic() + 12
        for model in OPENROUTER_MODELS:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                response = SESSION.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                        "HTTP-Referer": "http://127.0.0.1:8000",
                        "X-Title": "Teenix",
                        "Accept": "application/json",
                    },
                    json={
                        "model": model,
                        "temperature": 0.1,
                        "max_tokens": 900,
                        "reasoning": {"effort": "minimal", "exclude": True},
                        "response_format": {"type": "json_object"},
                        "messages": [
                            {
                                "role": "system",
                                "content": (
                                    "Traduci in italiano tutti i metadati forniti, soprattutto la "
                                    "trama e i generi. Traduci il titolo solo se esiste un titolo "
                                    "italiano ufficiale o comunemente usato; altrimenti conservalo. "
                                    "Non inventare informazioni. Restituisci esclusivamente un "
                                    "oggetto JSON valido con le chiavi title, plot e genres; genres "
                                    "deve restare un array con lo stesso numero di elementi. "
                                    "Nessuna spiegazione, analisi o testo fuori dal JSON."
                                ),
                            },
                            {"role": "user", "content": json.dumps(source, ensure_ascii=False)},
                        ],
                    },
                    timeout=(min(2, remaining), min(4, remaining)),
                )
                if response.status_code in {401, 402, 403}:
                    response.raise_for_status()
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("Risposta OpenRouter non valida.")
                choices = payload.get("choices")
                if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                    raise ValueError("Risposta OpenRouter senza completamento.")
                message = choices[0].get("message")
                if not isinstance(message, dict):
                    raise ValueError("Risposta OpenRouter senza messaggio.")
                content = message.get("content")
                if isinstance(content, list):
                    content = "".join(
                        part.get("text", "") for part in content if isinstance(part, dict)
                    )
                if not isinstance(content, str):
                    raise ValueError("Risposta di traduzione priva di testo.")
                content = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", content.strip(), flags=re.I)
                try:
                    translated = json.loads(content)
                except json.JSONDecodeError:
                    start, end = content.find("{"), content.rfind("}")
                    if start < 0 or end <= start:
                        raise
                    translated = json.loads(content[start:end + 1])
                if not isinstance(translated, dict):
                    raise ValueError("Formato della traduzione non valido.")
                translated_title = translated.get("title")
                translated_plot = translated.get("plot")
                translated_genres = translated.get("genres")
                if source["title"] and not isinstance(translated_title, str):
                    raise ValueError("Traduzione del titolo non valida.")
                if source["plot"] and not isinstance(translated_plot, str):
                    raise ValueError("Traduzione della trama non valida.")
                if source["genres"] and (
                    not isinstance(translated_genres, list)
                    or len(translated_genres) != len(source["genres"])
                    or not all(isinstance(genre, str) for genre in translated_genres)
                ):
                    raise ValueError("Traduzione dei generi non valida.")
                if source["plot"] and translated_plot.strip():
                    source_plot = re.sub(r"\s+", " ", source["plot"]).casefold()
                    result_plot = re.sub(r"\s+", " ", translated_plot).strip().casefold()
                    if (
                        difflib.SequenceMatcher(None, source_plot, result_plot).ratio() >= 0.92
                        and not _looks_italian(source["plot"])
                    ):
                        raise ValueError("Il modello ha restituito la trama senza tradurla.")
                translated_genres = [
                    ITALIAN_GENRES.get(_clean(genre, 80).casefold(), _clean(genre, 80))
                    for genre in translated_genres
                ] if source["genres"] else []
                return {
                    "title": _clean(translated_title, 200) if source["title"] else "",
                    "plot": _clean(translated_plot, 1200) if source["plot"] else "",
                    "genres": [_clean(genre, 80) for genre in translated_genres]
                    if source["genres"] else [],
                    "translation_status": "translated",
                }
            except (requests.RequestException, ValueError, TypeError, KeyError, IndexError) as error:
                last_error = error
                status_code = (
                    error.response.status_code
                    if isinstance(error, requests.HTTPError) and error.response is not None
                    else None
                )
                logger.info(
                    "Modello gratuito OpenRouter %s non riuscito (HTTP %s; %s).",
                    model, status_code or "n/d", type(error).__name__,
                )
                if status_code in {401, 402, 403}:
                    break
        raise RuntimeError("Traduzione OpenRouter non riuscita entro il limite di 12 secondi.") from last_error

    try:
        return _cached(cache_key, 86400, translate)
    except RuntimeError as error:
        logger.warning("%s Mantengo i metadati originali.", error)
        fallback = {**source, "translation_status": "failed"}
        with _lock:
            _cache[cache_key] = (time.time() + 60, fallback)
        return fallback


def _b64e(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def _b64d(s: str) -> str:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)).decode()


def _with_slot(fn):
    with _browser_slots:
        return fn()


def _anime_call(fn):
    """Esegue AnimeUnity sempre sullo stesso thread, richiesto da Playwright sync."""
    return _anime_pool.submit(lambda: _with_slot(fn)).result()


ANILIST_QUERY = """
query ($page: Int, $perPage: Int, $search: String, $sort: [MediaSort]) {
  Page(page: $page, perPage: $perPage) {
    pageInfo { currentPage hasNextPage }
    media(search: $search, type: ANIME, sort: $sort, isAdult: false) {
      id
      title { english romaji native }
      coverImage { extraLarge large }
      format
      seasonYear
      averageScore
      siteUrl
      episodes
    }
  }
}
"""


def _anilist_item(media: dict) -> dict:
    titles = media.get("title") or {}
    title = _clean(titles.get("english") or titles.get("romaji") or titles.get("native") or "", 200)
    lookup_title = _clean(titles.get("romaji") or titles.get("english") or titles.get("native") or title, 200)
    cover = media.get("coverImage") or {}
    metadata = [str(media["seasonYear"])] if media.get("seasonYear") else []
    if media.get("format"):
        metadata.append(media["format"].replace("_", " ").title())
    if media.get("episodes") and media.get("format") != "MOVIE":
        metadata.append(f"{media['episodes']} episodi")
    return {
        "kind": "anime",
        "id": f"anilist-{media['id']}",
        "slug": f"anilist-{media['id']}",
        "title": title or lookup_title,
        "lookup_title": lookup_title,
        "poster": cover.get("extraLarge") or cover.get("large") or "",
        "meta": " · ".join(metadata),
        "rating": f"{media['averageScore'] / 10:.1f}" if media.get("averageScore") else "",
        "site_url": media.get("siteUrl") or "",
        "episodes": media.get("episodes"),
        "catalog_source": "anilist",
        "badges": [],
    }


def _anilist_page(
    page: int,
    search: str = "",
    limit: int = 30,
    sort: str = "POPULARITY_DESC",
) -> tuple[list[dict], int | None]:
    page = max(1, page)
    per_page = max(1, min(limit, 40))
    if sort not in {"POPULARITY_DESC", "TRENDING_DESC", "SCORE_DESC", "FAVOURITES_DESC"}:
        raise ValueError("Ordinamento AniList non valido.")
    cache_key = f"anilist:{sort}:{page}:{per_page}:{search.strip().casefold()}"

    def fetch():
        response = SESSION.post(
            "https://graphql.anilist.co",
            json={"query": ANILIST_QUERY, "variables": {
                "page": page, "perPage": per_page, "search": search.strip() or None,
                "sort": [sort],
            }},
            timeout=20,
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("errors"):
            raise RuntimeError("AniList ha restituito un errore GraphQL.")
        data = payload.get("data", {}).get("Page")
        if not isinstance(data, dict):
            raise RuntimeError("AniList ha restituito una risposta incompleta.")
        items = [
            _anilist_item(media) for media in data.get("media", [])
            if media.get("id") and (
                (media.get("title") or {}).get("english")
                or (media.get("title") or {}).get("romaji")
                or (media.get("title") or {}).get("native")
            )
        ]
        next_page = page + 1 if data.get("pageInfo", {}).get("hasNextPage") else None
        return items, next_page

    return _cached(cache_key, 900, fetch)


ANILIST_DETAIL_QUERY = """
query ($id: Int) {
  Media(id: $id, type: ANIME, isAdult: false) {
    id
    idMal
    title { english romaji native }
    coverImage { extraLarge large }
    description(asHtml: false)
    format
    episodes
    seasonYear
    averageScore
    genres
    siteUrl
  }
}
"""


def _wikipedia_episode_titles(media: dict, episode_count: int) -> dict[int, str]:
    titles = media.get("title") or {}
    anime_titles = list(dict.fromkeys(
        _clean(titles.get(key) or "", 180)
        for key in ("english", "romaji", "native")
        if _clean(titles.get(key) or "", 180)
    ))
    if not anime_titles:
        return {}

    cache_key = (
        "wikipedia:anime-episodes:" + str(episode_count) + ":"
        + "|".join(titlestr.casefold() for titlestr in anime_titles)
    )

    def fetch():
        localized: dict[int, str] = {}
        english: dict[int, str] = {}
        for language in ("it", "en"):
            target = localized if language == "it" else english
            for anime_title in anime_titles[:2]:
                search_query = (
                    f"{anime_title} episodi"
                    if language == "it"
                    else f'"List of {anime_title} episodes"'
                )
                candidates = []
                try:
                    response = _wikidata_request(
                        f"https://{language}.wikipedia.org/w/api.php",
                        params={"action": "query", "list": "search", "srsearch": search_query,
                                "srlimit": 30, "format": "json"},
                        timeout=15,
                    )
                    response.raise_for_status()
                    candidates.extend(response.json().get("query", {}).get("search", []))
                except (requests.RequestException, ValueError) as error:
                    logger.info("Ricerca episodi Wikipedia (%s) non riuscita: %s", language, error)
                candidates.sort(key=lambda result: (
                    int(re.search(r"(?:seasons?\s*)(\d+)", str(result.get("title") or ""), re.I).group(1))
                    if re.search(r"(?:seasons?\s*)(\d+)", str(result.get("title") or ""), re.I)
                    else 0
                ))

                seen_pages = set()
                for candidate in candidates:
                    page_id = candidate.get("pageid")
                    page_title = str(candidate.get("title") or "")
                    page_key = page_title.casefold()
                    if not page_id or page_key in seen_pages:
                        continue
                    seen_pages.add(page_key)
                    if not re.search(r"episod|episodes", page_title, re.I):
                        continue
                    title_similarity = max(
                        difflib.SequenceMatcher(
                            None,
                            re.sub(r"[^a-z0-9]+", " ", anime_title.casefold()).strip(),
                            re.sub(r"[^a-z0-9]+", " ", page_title.casefold()).strip(),
                        ).ratio()
                        for anime_title in anime_titles
                    )
                    if title_similarity < 0.28:
                        continue
                    try:
                        parsed = _wikidata_request(
                            f"https://{language}.wikipedia.org/w/api.php",
                            params={"action": "parse", "pageid": page_id, "prop": "text",
                                    "format": "json", "formatversion": 2},
                            timeout=20,
                        )
                        parsed.raise_for_status()
                        html = parsed.json().get("parse", {}).get("text", "")
                    except (requests.RequestException, ValueError) as error:
                        logger.info("Pagina episodi Wikipedia non leggibile (%s): %s", page_title, error)
                        continue
                    page_episodes = {}
                    next_number = max(target, default=0) + 1
                    for table in BeautifulSoup(html, "html.parser").select("table.wikitable"):
                        rows = table.select("tr")
                        header_row = next((
                            row for row in rows
                            if row.find("th")
                            and any(
                                re.search(r"title|titolo", cell.get_text(" ", strip=True), re.I)
                                for cell in row.find_all(["th", "td"], recursive=False)
                            )
                            and any(
                                re.search(r"overall|total|episode|episodio|no\.|orig\.|number|numero",
                                          cell.get_text(" ", strip=True), re.I)
                                for cell in row.find_all(["th", "td"], recursive=False)
                            )
                        ), None)
                        if not header_row:
                            continue
                        headers = [
                            re.sub(
                                r"\[\s*n?\s*\d+\s*\]", "",
                                _clean(cell.get_text(" ", strip=True), 80).casefold(),
                            ).strip()
                            for cell in header_row.find_all(["th", "td"], recursive=False)
                        ]
                        title_index = next(
                            (index for index, value in enumerate(headers)
                             if value in {"titolo italiano", "italian title"}),
                            None,
                        )
                        if title_index is None:
                            title_index = next(
                                (index for index, value in enumerate(headers)
                                 if value in {"titolo inglese", "english title"}),
                                None,
                            )
                        if title_index is None:
                            title_index = next(
                                (index for index, value in enumerate(headers)
                                 if value in {"titolo", "title", "episode title", "titolo episodio"}),
                                None,
                            )
                        if title_index is None:
                            title_index = next(
                                (index for index, value in enumerate(headers)
                                 if ("title" in value or "titolo" in value)
                                 and ("4kids" in value or "funimation" in value
                                      or not re.search(r"japanese|giapponese|transliteration", value))),
                                None,
                            )
                        if title_index is None:
                            continue
                        overall_index = next(
                            (index for index, value in enumerate(headers)
                             if "overall" in value or "totale" in value),
                            None,
                        )
                        if overall_index is None:
                            overall_index = next(
                                (index for index, value in enumerate(headers)
                                 if value in {"orig.", "orig", "original episode"}),
                                None,
                            )
                        number_index = overall_index if overall_index is not None else next(
                            (index for index, value in enumerate(headers)
                             if value in {"no.", "no", "#", "numero", "nº", "n°", "episode",
                                          "episodio", "episode number", "numero episodio"}),
                            None,
                        )
                        if number_index is None:
                            number_index = next(
                                (index for index, value in enumerate(headers)
                                 if value.startswith(("no.", "no ", "nº", "n°", "numero"))
                                 or (re.fullmatch(r"(?:episode|episodio)\s*(?:number|no\.?|#|n°)", value)
                                     and not re.search(r"span|total|count|date", value))),
                                None,
                            )
                        if number_index is None:
                            continue
                        for row in rows[rows.index(header_row) + 1:]:
                            cells = row.find_all(["th", "td"], recursive=False)
                            if max(title_index, number_index) >= len(cells):
                                continue
                            raw_number = _clean(cells[number_index].get_text(" ", strip=True), 30)
                            number_match = re.fullmatch(r"(\d{1,4})\s*(?:\[[^\]]+\])?", raw_number)
                            if not number_match:
                                continue
                            number = int(number_match.group(1))
                            raw_title = _clean(cells[title_index].get_text(" ", strip=True), 220)
                            quoted_titles = re.findall(r'["“]([^"”]+)["”]', raw_title)
                            if "4kids" in headers[title_index] and len(quoted_titles) > 1:
                                raw_title = quoted_titles[1]
                            elif quoted_titles:
                                raw_title = quoted_titles[0]
                            episode_title = _clean(raw_title, 160)
                            episode_title = re.split(
                                r"\s*(?:transliteration|romaji)\s*:", episode_title,
                                maxsplit=1, flags=re.I,
                            )[0]
                            episode_title = re.sub(r"\s*\[[^\]]+\]\s*$", "", episode_title).strip(" —–-")
                            if not episode_title or episode_title.casefold() in {"tba", "tbd", "da annunciare"}:
                                continue
                            if overall_index is None:
                                number = next_number
                            page_episodes[number] = episode_title
                            next_number = max(next_number, number + 1)
                    for number, episode_title in page_episodes.items():
                        target.setdefault(number, episode_title)
                    if episode_count and len(target) >= episode_count:
                        break
                if episode_count and len(target) >= episode_count:
                    break
        return {**english, **localized}

    try:
        return _cached(cache_key, 86400, fetch)
    except Exception as error:
        logger.warning("Titoli episodi Wikipedia non disponibili per %s: %s",
                       anime_titles[0], error)
        return {}


def _jikan_episode_data(media: dict, episode_count: int = 0) -> tuple[int, dict[int, str]]:
    mal_id = media.get("idMal")
    if not mal_id:
        return 0, {}
    cache_key = f"jikan:episodes:{int(mal_id)}:{episode_count}"

    def fetch():
        anime_response = SESSION.get(
            f"https://api.jikan.moe/v4/anime/{int(mal_id)}/full",
            timeout=15,
            headers={"Accept": "application/json", "User-Agent": WIKIDATA_UA},
        )
        anime_response.raise_for_status()
        jikan_count = int((anime_response.json().get("data") or {}).get("episodes") or 0)
        limit = min(max(jikan_count or episode_count, 0), 2000)
        titles: dict[int, str] = {}
        page = 1
        while page <= 20 and (not limit or len(titles) < limit):
            response = SESSION.get(
                f"https://api.jikan.moe/v4/anime/{int(mal_id)}/episodes",
                params={"page": page},
                timeout=15,
                headers={"Accept": "application/json", "User-Agent": WIKIDATA_UA},
            )
            response.raise_for_status()
            payload = response.json()
            for index, item in enumerate(payload.get("data") or []):
                number = int(item.get("mal_id") or ((page - 1) * 100 + index + 1))
                title = _clean(item.get("title_english") or item.get("title") or "", 160)
                if title and not re.fullmatch(r"episode\s+\d+", title, re.I):
                    titles[number] = title
            if not (payload.get("pagination") or {}).get("has_next_page"):
                break
            page += 1
            time.sleep(.36)
        return jikan_count, titles

    try:
        return _cached(cache_key, 86400, fetch)
    except Exception as error:
        logger.info("Titoli episodi Jikan non disponibili per AniList %s: %s", mal_id, error)
        return 0, {}


def _tvmaze_item(show: dict) -> dict:
    image = show.get("image") or {}
    premiered = show.get("premiered") or ""
    return {
        "kind": "serie",
        "id": f"tvmaze-{show['id']}",
        "slug": re.sub(r"[^a-z0-9]+", "-", _clean(show.get("name", "")).casefold()).strip("-"),
        "title": _clean(show.get("name", ""), 200),
        "poster": image.get("original") or image.get("medium") or "",
        "meta": "Serie TV" + (f" · {premiered[:4]}" if premiered else ""),
        "rating": str((show.get("rating") or {}).get("average") or ""),
        "genres": show.get("genres") or [],
        "plot": _clean(BeautifulSoup(show.get("summary") or "", "html.parser").get_text(" ", strip=True), 600),
        "catalog_source": "tvmaze",
        "badges": [],
    }


def _tvmaze_request(path: str, params: dict | None = None, timeout: int = 20):
    response = SESSION.get(
        f"https://api.tvmaze.com{path}",
        params=params,
        timeout=timeout,
        headers={"Accept": "application/json"},
    )
    response.raise_for_status()
    return response.json()


def _wikidata_request(
    url: str,
    params: dict,
    timeout: int | tuple[int, int] = 20,
    accept: str = "application/json",
    attempts: int = 3,
):
    response = None
    attempts = max(1, attempts)
    for attempt in range(attempts):
        try:
            response = SESSION.get(
                url, params=params, timeout=timeout,
                headers={"Accept": accept, "User-Agent": WIKIDATA_UA},
            )
        except requests.RequestException:
            if attempt == attempts - 1:
                raise
            time.sleep(attempt + 1)
            continue
        if response.status_code not in {429, 500, 502, 503, 504} or attempt == attempts - 1:
            return response
        retry_after = response.headers.get("Retry-After", "")
        try:
            delay = min(max(float(retry_after), 1), 10)
        except ValueError:
            delay = attempt + 1
        time.sleep(delay)
    return response


def _wikidata_entities(query: str, limit: int = 30) -> list[dict]:
    search = _wikidata_request(
        "https://www.wikidata.org/w/api.php",
        {
            "action": "wbsearchentities", "search": query, "language": "it",
            "uselang": "it", "format": "json", "type": "item",
            "limit": min(max(limit, 10), 30),
        },
    )
    search.raise_for_status()
    ids = [row.get("id") for row in search.json().get("search", []) if row.get("id")]
    if not ids:
        return []
    entities_response = _wikidata_request(
        "https://www.wikidata.org/w/api.php",
        params={
            "action": "wbgetentities", "ids": "|".join(ids), "props": "labels|descriptions|claims",
            "languages": "it|en", "format": "json",
        },
    )
    entities_response.raise_for_status()
    entities = entities_response.json().get("entities", {})
    results = []
    for entity_id in ids:
        entity = entities.get(entity_id, {})
        item = _wikidata_movie_item(entity_id, entity)
        if item:
            results.append(item)
        if len(results) >= limit:
            break
    return results


def _wikidata_movie_item(entity_id: str, entity: dict) -> dict | None:
    claims = entity.get("claims") or {}
    instance_ids = {
        claim.get("mainsnak", {}).get("datavalue", {}).get("value", {}).get("id")
        for claim in claims.get("P31", [])
    }
    if "Q11424" not in instance_ids:
        return None
    labels = entity.get("labels") or {}
    title = _clean((labels.get("it") or labels.get("en") or {}).get("value", ""), 200)
    if not title:
        return None
    image_claims = claims.get("P18") or []
    image_name = (image_claims[0].get("mainsnak", {}).get("datavalue", {}).get("value")
                  if image_claims else "")
    date_claims = claims.get("P577") or []
    date_value = (date_claims[0].get("mainsnak", {}).get("datavalue", {}).get("value", {}).get("time", "")
                  if date_claims else "")
    descriptions = entity.get("descriptions") or {}
    description = descriptions.get("it") or descriptions.get("en") or {}
    return {
        "kind": "serie",
        "id": f"wikidata-{entity_id}",
        "slug": re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-") or entity_id.casefold(),
        "title": title,
        "poster": f"https://commons.wikimedia.org/wiki/Special:FilePath/{requests.utils.quote(image_name)}?width=900"
                  if image_name else "",
        "meta": "Film" + (f" · {date_value[1:5]}" if date_value.startswith("+") else ""),
        "plot": _clean(description.get("value", ""), 600),
        "catalog_source": "wikidata",
        "badges": [],
    }


def _wikidata_fallback_movies(limit: int = 30) -> list[dict]:
    queries = (
        "The Godfather", "Inception", "Interstellar", "The Dark Knight",
        "The Matrix", "Spirited Away", "Parasite", "Titanic",
    )

    def search(query: str) -> list[str]:
        try:
            response = _wikidata_request(
                "https://www.wikidata.org/w/api.php",
                params={"action": "wbsearchentities", "search": query, "language": "en",
                        "uselang": "en", "format": "json", "type": "item", "limit": 3},
                timeout=(3, 4),
                attempts=1,
            )
            response.raise_for_status()
            return [row["id"] for row in response.json().get("search", []) if row.get("id")]
        except (requests.RequestException, ValueError) as error:
            logger.info("Ricerca alternativa film Wikidata (%s) non riuscita: %s", query, error)
            return []

    with ThreadPoolExecutor(max_workers=len(queries), thread_name_prefix="wikidata-search") as pool:
        searches = list(pool.map(search, queries))
    ids = [entity_id for result in searches for entity_id in result]
    ids = list(dict.fromkeys(ids))[:50]
    if not ids:
        return []
    response = _wikidata_request(
        "https://www.wikidata.org/w/api.php",
        params={"action": "wbgetentities", "ids": "|".join(ids),
                "props": "labels|descriptions|claims", "languages": "it|en", "format": "json"},
        timeout=(3, 5),
        attempts=1,
    )
    response.raise_for_status()
    entities = response.json().get("entities", {})
    items = [
        item for entity_id in ids
        if (item := _wikidata_movie_item(entity_id, entities.get(entity_id, {})))
    ]
    return items[:limit]


def _wikidata_entity(entity_id: str) -> dict | None:
    response = _wikidata_request(
        "https://www.wikidata.org/w/api.php",
        params={
            "action": "wbgetentities", "ids": entity_id, "props": "labels|descriptions|claims",
            "languages": "it|en", "format": "json",
        },
    )
    response.raise_for_status()
    entity = response.json().get("entities", {}).get(entity_id, {})
    return _wikidata_movie_item(entity_id, entity)


def _wikidata_popular_movies(limit: int = 30, offset: int = 0) -> list[dict]:
    query = """
    SELECT ?item ?itemLabel ?itemDescription (SAMPLE(?image) AS ?image) (MIN(?date) AS ?date) ?links WHERE {
      ?item wdt:P31 wd:Q11424; wikibase:sitelinks ?links.
      OPTIONAL { ?item wdt:P18 ?image. }
      OPTIONAL { ?item wdt:P577 ?date. }
      SERVICE wikibase:label { bd:serviceParam wikibase:language "it,en". }
    }
    GROUP BY ?item ?itemLabel ?itemDescription ?links
    ORDER BY DESC(?links)
    LIMIT %d
    OFFSET %d
    """ % (min(max(limit, 1), 50), max(offset, 0))
    response = _wikidata_request(
        "https://query.wikidata.org/sparql",
        params={"query": query, "format": "json"},
        timeout=(3, 5),
        accept="application/sparql-results+json",
        attempts=1,
    )
    response.raise_for_status()
    bindings = response.json().get("results", {}).get("bindings", [])
    items = []
    for row in bindings:
        entity_url = row.get("item", {}).get("value", "")
        entity_id = entity_url.rsplit("/", 1)[-1]
        title = _clean(row.get("itemLabel", {}).get("value", ""), 200)
        if not entity_id.startswith("Q") or not title:
            continue
        image = row.get("image", {}).get("value", "")
        date = row.get("date", {}).get("value", "")
        items.append({
            "kind": "serie", "id": f"wikidata-{entity_id}",
            "slug": re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-"),
            "title": title,
            "poster": f"{image}?width=900" if image else "",
            "meta": "Film" + (f" · {date[:4]}" if date else ""),
            "plot": _clean(row.get("itemDescription", {}).get("value", ""), 600),
            "catalog_source": "wikidata", "badges": [],
        })
    return items


# ------------------------------------------------------------------ API: home

@app.get("/api/home")
def api_home():
    def fetch_shows():
        return _cached(
            "tvmaze:home:0", 900,
            lambda: [
                _tvmaze_item(show) for show in sorted(
                    _tvmaze_request("/shows", {"page": 0}, timeout=8),
                    key=lambda item: (item.get("rating") or {}).get("average") or 0,
                    reverse=True,
                )[:30]
            ],
        )

    def fetch_movies():
        def load():
            try:
                return _wikidata_popular_movies(30)
            except Exception as error:
                logger.warning("Query film Wikidata non riuscita, uso la ricerca API: %s", error)
                fallback = _wikidata_fallback_movies(30)
                if fallback:
                    return fallback
                raise error

        return _cached("wikidata:home:movies:0", 3600, load)

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="home-catalog") as pool:
        shows_future = pool.submit(fetch_shows)
        movies_future = pool.submit(fetch_movies)
        results = []
        for source, future in (("TVmaze", shows_future), ("Wikidata", movies_future)):
            try:
                results.append((source, future.result(), None))
            except Exception as error:
                logger.warning("Catalogo %s non disponibile: %s", source, error)
                results.append((source, [], error))

    shows = results[0][1]
    movies = results[1][1]
    warnings = []
    for source, items, error in results:
        if error is None:
            continue
        if source == "TVmaze":
            warnings.append(f"Serie TV non disponibili da TVmaze ({type(error).__name__}).")
        else:
            warnings.append(
                f"Film non disponibili da Wikidata ({type(error).__name__}); "
                "riprova più tardi. Il contatto TEENIX_CONTACT è facoltativo."
            )
    if not shows and not movies:
        raise HTTPException(502, "Cataloghi TVmaze e Wikidata non disponibili: " + " ".join(warnings))
    sections = []
    if movies:
        sections.append({"title": "Film da scoprire", "items": movies})
        classics = [
            item for item in movies
            if (match := re.search(r"\b(18|19)\d{2}\b", item.get("meta", "")))
            and int(match.group()) <= 2000
        ]
        if classics:
            sections.append({"title": "Classici del cinema", "items": classics})
    if shows:
        sections.append({"title": "Serie TV popolari", "items": shows})
        genre_sections = (
            ("Drammi da scoprire", {"drama"}),
            ("Crime e mistero", {"crime", "mystery"}),
            ("Commedie", {"comedy"}),
            ("Fantasy e fantascienza", {"fantasy", "science-fiction"}),
            ("Azione e avventura", {"action", "adventure"}),
        )
        for title, genres in genre_sections:
            matching = [
                item for item in shows
                if genres.intersection(
                    str(genre).casefold() for genre in item.get("genres", [])
                )
            ]
            if matching:
                sections.append({"title": title, "items": matching})
    return {
        "sections": sections,
        "next_cursors": {
            "sc": 30 if shows else None,
            "movies": len(movies) if len(movies) == 30 else None,
        },
        "warnings": warnings,
    }


@app.get("/api/home/more")
def api_home_more(source: str = "sc", cursor: int = Query(0, ge=0)):
    if source == "movies":
        try:
            items = _cached(
                f"wikidata:home:movies:{cursor}", 3600,
                lambda: _wikidata_popular_movies(30, cursor),
            )
        except Exception as error:
            raise HTTPException(502, f"Catalogo film non disponibile: {type(error).__name__}") from error
        return {
            "items": items,
            "next_cursor": cursor + len(items) if len(items) == 30 else None,
        }
    if source == "au":
        try:
            items, next_cursor = _anilist_page(cursor)
        except Exception as anilist_error:
            raise HTTPException(502, f"AniList non disponibile: {type(anilist_error).__name__}") from anilist_error
        return {"items": items, "next_cursor": next_cursor}
    if source != "sc":
        raise HTTPException(400, "sorgente catalogo non valida")
    if source != "sc":
        raise HTTPException(400, "sorgente catalogo non valida")
    try:
        page = cursor // 250
        offset = cursor % 250
        shows = _cached(
            f"tvmaze:home:page:{page}", 900,
            lambda: _tvmaze_request("/shows", {"page": page}, timeout=8),
        )
    except Exception as error:
        raise HTTPException(502, f"Catalogo TVmaze non disponibile: {type(error).__name__}") from error
    batch = shows[offset:offset + 40]
    items = [_tvmaze_item(show) for show in batch]
    next_cursor = None
    if offset + len(batch) < len(shows):
        next_cursor = cursor + len(batch)
    elif len(shows) >= 250:
        next_cursor = (page + 1) * 250
    return {"items": items, "next_cursor": next_cursor}


@app.get("/api/anime/home")
def api_anime_home():
    categories = (
        ("Anime popolari", "POPULARITY_DESC"),
        ("In tendenza", "TRENDING_DESC"),
        ("Più apprezzati", "SCORE_DESC"),
        ("I più seguiti", "FAVOURITES_DESC"),
    )
    sections = []
    errors = []
    for title, sort in categories:
        try:
            items, _ = _anilist_page(1, limit=24, sort=sort)
        except Exception as error:
            logger.warning("Categoria AniList %s non disponibile: %s", title, error)
            errors.append(f"{title} non disponibile ({type(error).__name__}).")
            continue
        if items:
            sections.append({"title": title, "items": items})
    if not sections:
        message = "AniList non disponibile."
        if errors:
            message += " " + " ".join(errors)
        raise HTTPException(502, message)
    if not any(section["items"] for section in sections):
        raise HTTPException(502, "Il catalogo anime non ha restituito titoli.")
    return {"sections": sections, "next_cursor": None, "warnings": errors}


# ------------------------------------------------------------------ API: search

@app.get("/api/search")
def api_search(q: str = Query(..., min_length=2), kind: str = "anime",
               media_type: str = "all", limit: int = 30):
    q = q.strip()
    limit = max(1, min(limit, 40))
    if kind not in {"anime", "serie"} or media_type not in {"all", "movie", "tv"}:
        raise HTTPException(400, "tipo di catalogo non valido")
    key = f"search:{kind}:{media_type}:{q.lower()}:{limit}"
    if kind == "anime":
        try:
            items = _cached(key, 180, lambda: _anilist_page(1, q, limit)[0])
        except Exception as error:
            raise HTTPException(502, f"Ricerca AniList non riuscita: {type(error).__name__}") from error
        return {"items": items}

    items = []
    errors = []
    if media_type in {"all", "tv"}:
        try:
            shows = _cached(
                f"tvmaze:search:{q.casefold()}:{limit}", 300,
                lambda: _tvmaze_request("/search/shows", {"q": q})[:limit],
            )
            items.extend(_tvmaze_item(row["show"]) for row in shows if row.get("show"))
        except Exception as error:
            errors.append(("TVmaze", error))
    if media_type in {"all", "movie"}:
        try:
            items.extend(_cached(
                f"wikidata:search:{q.casefold()}:{limit}", 900,
                lambda: _wikidata_entities(q, limit),
            ))
        except Exception as error:
            errors.append(("Wikidata", error))
    if errors and not items:
        source, error = errors[0]
        raise HTTPException(502, f"Ricerca metadati {source} non riuscita: {type(error).__name__}") from error
    return {"items": items, "warnings": [
        f"{source} non disponibile ({type(error).__name__})."
        for source, error in errors
    ]}


# ------------------------------------------------------------------ API: dettagli

@app.get("/api/anime/{slug}")
def api_anime(slug: str, anilist_id: str = "", lookup_title: str = ""):
    match = re.fullmatch(r"(?:anilist-)?(\d+)", anilist_id or slug)
    if not match:
        query = _clean(lookup_title or slug.replace("-", " "), 200)
        if len(query) < 2:
            raise HTTPException(400, "identificativo AniList non valido")
        try:
            candidates, _ = _anilist_page(1, query, 1)
        except Exception as error:
            raise HTTPException(502, f"Ricerca AniList non riuscita: {type(error).__name__}") from error
        match = re.fullmatch(r"anilist-(\d+)", str(candidates[0].get("id", ""))) if candidates else None
        if not match:
            raise HTTPException(404, "Anime non trovato su AniList.")
    media_id = int(match.group(1))

    try:
        def fetch_media():
            response = SESSION.post(
                "https://graphql.anilist.co",
                json={"query": ANILIST_DETAIL_QUERY, "variables": {"id": media_id}},
                timeout=20,
                headers={"Accept": "application/json", "User-Agent": WIKIDATA_UA},
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("errors"):
                raise RuntimeError("AniList ha restituito un errore GraphQL.")
            media = payload.get("data", {}).get("Media")
            if not isinstance(media, dict):
                raise LookupError("Anime non trovato su AniList.")
            return media

        media = _cached(f"anilist:detail:{media_id}", 900, fetch_media)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except Exception as error:
        raise HTTPException(502, f"Dettagli AniList non disponibili: {type(error).__name__}") from error

    item = _anilist_item(media)
    count = int(media.get("episodes") or 0)
    episodes = []
    wikipedia_titles: dict[int, str] = {}
    jikan_count = 0
    jikan_titles: dict[int, str] = {}
    if media.get("format") != "MOVIE":
        episode_count = min(count, 2000)
        wikipedia_titles = _wikipedia_episode_titles(media, episode_count)
        episode_count = min(max(episode_count, max(wikipedia_titles, default=0)), 2000)
        if not episode_count or len(wikipedia_titles) < episode_count:
            jikan_count, jikan_titles = _jikan_episode_data(media, episode_count)
        episode_count = min(max(episode_count, jikan_count), 2000)
        combined_titles = {**jikan_titles, **wikipedia_titles}
        episodes = [
            {"number": number, "label": combined_titles.get(number) or f"Episodio {number}"}
            for number in range(1, episode_count + 1)
        ]

    plot = BeautifulSoup(media.get("description") or "", "html.parser").get_text(" ", strip=True)[:600]
    genres = media.get("genres") or []
    translated = _translate_metadata(item["title"], plot, genres)
    return {
        "kind": "anime",
        "slug": slug,
        "title": item["title"],
        "display_title": translated["title"] or item["title"],
        "translation_status": translated["translation_status"],
        "poster": item["poster"],
        "plot": translated["plot"] or plot,
        "genres": translated["genres"] or genres,
        "format": media.get("format") or "",
        "season_year": media.get("seasonYear"),
        "rating": item["rating"],
        "site_url": item["site_url"],
        "badges": [],
        "anilist_id": media_id,
        "mal_id": media.get("idMal"),
        "episodes_error": "Non è stato possibile recuperare il numero di episodi da AniList, Wikipedia o MyAnimeList."
        if not count and not episodes else "",
        "episodes_source": (
            "Wikipedia e MyAnimeList" if wikipedia_titles and jikan_titles else
            "Wikipedia" if wikipedia_titles else
            "MyAnimeList" if jikan_count or jikan_titles else
            "AniList"
        ),
        "translation_status": translated["translation_status"],
        "episodes": episodes,
    }


@app.get("/api/related")
def api_related(title: str = Query(..., min_length=2), kind: str = "serie", anilist_id: int = 0):
    title = _clean(title, 200)
    sections = []
    if kind == "anime":
        try:
            media_id = anilist_id
            if not media_id:
                matches, _ = _anilist_page(1, title, 1)
                if matches:
                    match = re.fullmatch(r"anilist-(\d+)", str(matches[0].get("id", "")))
                    media_id = int(match.group(1)) if match else 0

            def fetch_anilist_related():
                response = SESSION.post(
                    "https://graphql.anilist.co",
                    json={"query": "query ($id: Int) { Media(id: $id, type: ANIME) { recommendations(perPage: 12, sort: RATING_DESC) { nodes { mediaRecommendation { id type isAdult title { english romaji native } coverImage { extraLarge large } format seasonYear averageScore siteUrl } } } } }",
                          "variables": {"id": media_id}},
                    timeout=15, headers={"Accept": "application/json"},
                )
                response.raise_for_status()
                nodes = response.json().get("data", {}).get("Media", {}).get("recommendations", {}).get("nodes", [])
                media = [node.get("mediaRecommendation") for node in nodes if isinstance(node, dict)]
                return [_anilist_item(item) for item in media
                        if item and item.get("id") and item.get("type") == "ANIME" and not item.get("isAdult")]

            related = _cached(f"anilist:related:{media_id}", 900, fetch_anilist_related) if media_id else []
            if related:
                sections.append({"title": "Anime simili", "items": related[:10]})
        except Exception as error:
            logger.info("Raccomandazioni AniList non disponibili per %s: %s", title, error)

    return {"sections": sections}


@app.get("/api/serie/{tid}")
def api_serie(tid: str, slug: str = ""):
    if tid.startswith("tvmaze-"):
        numeric_id = tid.removeprefix("tvmaze-")
        if not numeric_id.isdigit():
            raise HTTPException(400, "identificativo TVmaze non valido")
        try:
            show = _cached(
                f"tvmaze:detail:{numeric_id}", 900,
                lambda: _tvmaze_request(f"/shows/{numeric_id}", {"embed": "episodes"}),
            )
        except Exception as error:
            raise HTTPException(502, f"Dettagli TVmaze non disponibili: {type(error).__name__}") from error
        image = show.get("image") or {}
        seasons = {}
        for episode in (show.get("_embedded") or {}).get("episodes", []):
            number = episode.get("season")
            episode_number = episode.get("number")
            if not isinstance(number, int) or not isinstance(episode_number, int):
                continue
            seasons.setdefault(str(number), []).append({
                "number": episode_number,
                "label": _clean(episode.get("name") or f"Episodio {episode_number}", 120),
                "poster": ((episode.get("image") or {}).get("medium") or ""),
                "plot": _clean(BeautifulSoup(episode.get("summary") or "", "html.parser").get_text(" ", strip=True), 300),
            })
        season_numbers = sorted((int(number) for number in seasons))
        title = _clean(show.get("name", ""), 200)
        plot = _clean(BeautifulSoup(show.get("summary") or "", "html.parser").get_text(" ", strip=True), 600)
        genres = show.get("genres") or []
        translated = _translate_metadata(title, plot, genres)
        return {
            "kind": "serie",
            "id": tid,
            "slug": slug,
            "title": title,
            "display_title": translated["title"] or title,
            "translation_status": translated["translation_status"],
            "poster": image.get("original") or image.get("medium") or "",
            "plot": translated["plot"] or plot,
            "genres": translated["genres"] or genres,
            "seasons": [f"Stagione {number}" for number in season_numbers],
            "episodes": seasons,
            "site_url": show.get("url") or "",
            "catalog_source": "tvmaze",
        }

    if tid.startswith("wikidata-"):
        entity_id = tid.removeprefix("wikidata-")
        if not re.fullmatch(r"Q\d+", entity_id):
            raise HTTPException(400, "identificativo Wikidata non valido")
        try:
            item = _cached(
                f"wikidata:detail:{entity_id}", 3600,
                lambda: _wikidata_entity(entity_id),
            )
        except Exception as error:
            raise HTTPException(502, f"Dettagli Wikidata non disponibili: {type(error).__name__}") from error
        if not item:
            raise HTTPException(404, "Film non trovato su Wikidata.")
        translated = _translate_metadata(item["title"], item["plot"])
        return {
            "kind": "movie",
            "id": tid,
            "slug": item["slug"],
            "title": item["title"],
            "display_title": translated["title"] or item["title"],
            "translation_status": translated["translation_status"],
            "poster": item["poster"],
            "plot": translated["plot"] or item["plot"],
            "genres": [],
            "seasons": [],
            "episodes": {},
            "site_url": f"https://www.wikidata.org/wiki/{entity_id}",
            "catalog_source": "wikidata",
        }

    if tid.isdigit() and slug:
        title_query = _clean(slug.replace("-", " "), 200)
        try:
            matches = _tvmaze_request("/search/shows", {"q": title_query})
        except Exception as error:
            raise HTTPException(502, f"Ricerca titolo TVmaze non riuscita: {type(error).__name__}") from error
        if matches:
            return api_serie(f"tvmaze-{matches[0]['show']['id']}", slug)

    raise HTTPException(400, "sorgente dei metadati non valida")


# ------------------------------------------------------------------ API: play

@app.get("/api/play/anime")
def api_play_anime(url: str = "", audio: str = "ita", anilist_id: int = 0,
                   title: str = "", episode: int = 1):
    if audio not in {"ita", "sub", "auto"}:
        raise HTTPException(400, "preferenza audio anime non valida")
    if not url and anilist_id:
        try:
            media = _cached(f"anilist:detail:{anilist_id}", 900, lambda: _fetch_anilist_media(anilist_id))
            anime_title = _clean(
                title or (media.get("title") or {}).get("romaji")
                or (media.get("title") or {}).get("english")
                or (media.get("title") or {}).get("native"),
                200,
            )
            if not anime_title:
                raise LookupError("Titolo anime non disponibile su AniList.")
            results = _anime_call(lambda: AU.search(
                anime_title, AU.BASE_DEFAULT, limit=20, audio=audio,
                allow_browser=not ANDROID_EMBEDDED, timeout=25))
            normalized_query = re.sub(r"[^a-z0-9]+", " ", anime_title.casefold()).strip()
            ranked = [
                (
                    difflib.SequenceMatcher(
                        None, normalized_query,
                        re.sub(r"\b(?:ita|sub|dub)\b", " ",
                               re.sub(r"[^a-z0-9]+", " ", str(item.get("title", "")).casefold())).strip(),
                    ).ratio(),
                    item,
                )
                for item in results
            ]
            score, best = max(ranked, key=lambda match: match[0], default=(0, None))
            if not best or score < 0.3:
                raise LookupError("Anime non trovato sulla sorgente video.")
            anime_url = f"{AU.BASE_DEFAULT}/anime/{best['slug']}/"
            available_episodes = _anime_call(lambda: AU.episodes(
                anime_url, timeout=25, allow_browser=not ANDROID_EMBEDDED))
            selected = next(
                (item for item in available_episodes if int(item.get("number") or 0) == episode),
                None,
            )
            if not selected or not selected.get("url"):
                raise LookupError(f"Episodio {episode} non disponibile sulla sorgente video.")
            url = selected["url"]
        except LookupError as error:
            raise HTTPException(404, str(error)) from error
        except Exception as error:
            raise HTTPException(502, f"Risoluzione sorgente anime non riuscita: {type(error).__name__}") from error

    parsed = urlparse(url)
    anime_host = urlparse(AU.BASE_DEFAULT).netloc.lower()
    if (parsed.scheme != "https" or parsed.netloc.lower() != anime_host
            or not AU.anime_id_from(url)):
        raise HTTPException(400, "url episodio non valido")
    try:
        r = _anime_call(lambda: AU.resolve(
            url, None, False, False, False, 1, 0.5, 25,
            allow_browser=not ANDROID_EMBEDDED, budget=60))
    except Exception as e:
        raise HTTPException(502, f"risoluzione fallita: {type(e).__name__}")
    source = r.get("playlist") if r.get("playlist") and r.get("variants") else r["origin"]
    return {"source": source, "referer": r["headers"].get("Referer", url),
            "audio": audio, "via": r.get("via"), "kind": r.get("kind"),
            "variants": r.get("variants", [])}


def _fetch_anilist_media(media_id: int) -> dict:
    response = SESSION.post(
        "https://graphql.anilist.co",
        json={"query": ANILIST_DETAIL_QUERY, "variables": {"id": media_id}},
        timeout=20,
        headers={"Accept": "application/json"},
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("errors"):
        raise RuntimeError("AniList ha restituito un errore GraphQL.")
    media = payload.get("data", {}).get("Media")
    if not isinstance(media, dict):
        raise LookupError("Anime non trovato su AniList.")
    return media


def _sc_episode_id(tid: int, season: int, ep: int) -> int | None:
    if ANDROID_EMBEDDED:
        seasons = SC.fetch_seasons(tid, SC.BASE_DEFAULT, SC.LANG_DEFAULT)
        selected_season = next((row for row in seasons if row["number"] == season), None)
        if selected_season is None:
            return None
        selected_episode = next(
            (row for row in selected_season["episodes"] if row["number"] == ep),
            None,
        )
        return selected_episode["id"] if selected_episode else None

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True,
                                     args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(user_agent=UA, locale="it-IT",
                                  viewport={"width": 1600, "height": 900})
        page = ctx.new_page()
        page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        src = ""
        try:
            page.goto(f"{SC.BASE_DEFAULT}/it/watch/{tid}", wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2200)
            if season > 1:
                try:
                    values = page.eval_on_selector_all(
                        ".season-selector select option", "els => els.map(e => e.value)")
                    if len(values) >= season:
                        page.locator(".season-selector select").evaluate(
                            "(select, value) => { select.value = value; select.dispatchEvent(new Event('change', { bubbles: true })); }",
                            values[season - 1],
                        )
                        page.wait_for_timeout(1800)
                except Exception:
                    pass
            page.evaluate("""(ep) => {
                const items = [...document.querySelectorAll('.episode-slider .slider-item')];
                const hit = items.find(it => ((it.querySelector('.number') || {}).textContent || '').trim() === String(ep));
                if (hit && hit.querySelector('a')) hit.querySelector('a').click();
            }""", ep)
            page.wait_for_timeout(2200)
            src = page.evaluate("""() => {
                const f = document.querySelector('.watch iframe, iframe[src*="/iframe/"]');
                return f ? (f.getAttribute('src') || '') : '';
            }""")
        finally:
            ctx.close()
            browser.close()

    m = re.search(r"episode_id=(\d+)", src or "")
    return int(m.group(1)) if m else None


@app.get("/api/play/serie")
def api_play_serie(tid: str, title: str = "", season: int = 1, ep: int = 1,
                   audio: str = "it",
                   variant: int | None = None):
    if audio not in {"it", "original", "auto"}:
        raise HTTPException(400, "preferenza audio non valida")
    stream_id = int(tid) if tid.isdigit() else 0
    if not stream_id:
        title = _clean(title, 200)
        if len(title) < 2:
            raise HTTPException(400, "titolo non valido per la ricerca dello stream")
        try:
            if ANDROID_EMBEDDED:
                candidates = _with_slot(lambda: SC.search_http(
                    title, SC.BASE_DEFAULT, SC.LANG_DEFAULT, 20))
            else:
                candidates = _with_slot(lambda: SC.search(
                    title, SC.BASE_DEFAULT, SC.LANG_DEFAULT, 20,
                    headed=False, verbose=False))
            normalized_title = re.sub(r"[^a-z0-9]+", " ", title.casefold()).strip()
            ranked = [
                (difflib.SequenceMatcher(
                    None, normalized_title,
                    re.sub(r"[^a-z0-9]+", " ", str(candidate.get("title", "")).casefold()).strip(),
                ).ratio(), candidate)
                for candidate in candidates
            ]
            score, selected = max(ranked, key=lambda match: match[0], default=(0, None))
            if not selected or score < 0.3:
                raise LookupError("Titolo non trovato sulla sorgente video.")
            stream_id = int(selected["id"])
        except LookupError as error:
            raise HTTPException(404, str(error)) from error
        except Exception as error:
            raise HTTPException(502, f"Ricerca della sorgente video non riuscita: {type(error).__name__}") from error

    watch = f"{SC.BASE_DEFAULT}/it/watch/{stream_id}"
    eid = None
    if season > 1 or ep > 1:
        try:
            eid = _with_slot(lambda: _sc_episode_id(stream_id, season, ep))
        except Exception as error:
            logger.warning(
                "Impossibile identificare l'episodio sulla sorgente video "
                "(tid=%d season=%d ep=%d): %s",
                stream_id, season, ep, error,
            )
            raise HTTPException(502, "Impossibile identificare l'episodio sulla sorgente video.") from error
        if eid is None:
            raise HTTPException(404, "Episodio non trovato sulla sorgente video.")

    try:
        r = _with_slot(lambda: SC.try_http(watch, eid, variant))
    except Exception as http_error:
        if ANDROID_EMBEDDED:
            raise HTTPException(
                502,
                "La sorgente video richiede un browser non incluso nell'app TV.",
            ) from http_error
        try:
            r = _with_slot(lambda: SC.try_browser(watch, eid, variant, False, False))
        except Exception as browser_error:
            logger.exception(
                "Risoluzione episodio StreamingCommunity fallita (tid=%d season=%d ep=%d; "
                "HTTP=%s: %s)",
                stream_id, season, ep, type(http_error).__name__, http_error,
            )
            raise HTTPException(
                502,
                "risoluzione fallita "
                f"(HTTP: {type(http_error).__name__}: {http_error}; "
                f"browser: {type(browser_error).__name__}: {browser_error})",
            ) from browser_error

    source = r.get("playlist") if r.get("playlist") and r.get("variants") else r["origin"]
    return {"source": source, "referer": r["headers"].get("Referer", watch),
            "variants": r.get("variants", []), "via": r.get("via"), "episode_id": eid}


# ------------------------------------------------------------------ API: proxy

def _proxy_url(target: str, referer: str) -> str:
    return f"/api/proxy?u={_b64e(target)}&r={_b64e(referer)}"


def _rewrite_playlist(text: str, base: str, referer: str) -> str:
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            out.append(line)
            continue
        if s.startswith("#"):
            out.append(re.sub(r'URI="([^"]+)"',
                              lambda m: f'URI="{_proxy_url(urljoin(base, m.group(1)), referer)}"', line))
            continue
        out.append(_proxy_url(urljoin(base, s), referer))
    return "\n".join(out)


@app.get("/api/proxy")
def api_proxy(request: Request, u: str, r: str = ""):
    target = _b64d(u)
    referer = _b64d(r) if r else ""
    p = urlparse(target)
    if p.scheme not in ("http", "https"):
        raise HTTPException(400, "url non valido")

    is_playlist = p.path.lower().endswith(".m3u8")
    rng = None if is_playlist else request.headers.get("range")

    try:
        up = _fetch(target, referer, rng)
    except requests.RequestException as e:
        raise HTTPException(504, f"upstream irraggiungibile: {type(e).__name__}")

    ctype = (up.headers.get("Content-Type") or "").lower()

    if is_playlist or "mpegurl" in ctype:
        try:
            text = up.text
        except requests.RequestException:
            up.close()
            raise HTTPException(504, "playlist non leggibile")
        finally:
            up.close()

        if "#EXTM3U" not in text[:400] and referer:
            alt = None
            try:
                alt = _fetch(target, "", None, attempts=2)
                alt_text = alt.text
                if "#EXTM3U" in alt_text[:400]:
                    text, referer = alt_text, ""
            except requests.RequestException:
                pass
            finally:
                if alt is not None:
                    alt.close()

        if "#EXTM3U" not in text[:400]:
            raise HTTPException(502, "playlist non valida (CDN lento o bloccato)")

        return Response(_rewrite_playlist(text, target, referer or target),
                        media_type="application/vnd.apple.mpegurl",
                        headers={"Cache-Control": "no-store",
                                 "Access-Control-Allow-Origin": "*"})

    passthrough = {k: v for k, v in up.headers.items()
                   if k.lower() in ("content-type", "content-length",
                                    "content-range", "accept-ranges")}
    passthrough["Access-Control-Allow-Origin"] = "*"
    passthrough["Access-Control-Expose-Headers"] = "Accept-Ranges, Content-Length, Content-Range"
    if "image" in ctype:
        passthrough["Cache-Control"] = "public, max-age=86400"

    def body():
        try:
            yield from up.iter_content(65536)
        finally:
            up.close()

    return StreamingResponse(body(), status_code=up.status_code, headers=passthrough)


# ------------------------------------------------------------------ statico

app.mount("/web", StaticFiles(directory=WEB), name="web")


@app.get("/api/health")
def api_health():
    return {"status": "ok"}


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)


def main() -> int:
    ap = argparse.ArgumentParser(description="Teenix locale")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args()
    import uvicorn
    try:
        uvicorn.run(app, host=a.host, port=a.port, log_level="warning")
    except KeyboardInterrupt:
        print("\nServer arrestato correttamente.")
    finally:
        SESSION.close()
        _anime_pool.submit(AU._browser_close).result()
        _anime_pool.shutdown(wait=True, cancel_futures=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
