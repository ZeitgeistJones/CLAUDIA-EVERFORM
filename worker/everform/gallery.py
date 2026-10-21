"""Client for the public, read-only Claudia.gallery JSON API.

Only documented endpoints are used (see https://claudia.gallery/api/index.json).
"""

from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Iterable

import requests

from . import config

log = logging.getLogger("everform.gallery")

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


class GalleryError(RuntimeError):
    pass


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": config.USER_AGENT, "Accept": "application/json"})
    return s


def request_with_retry(
    session: requests.Session,
    url: str,
    *,
    retries: int = 4,
    timeout: tuple[float, float] = (10.0, 60.0),
    headers: dict | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> requests.Response:
    """GET with exponential backoff and jitter on transient failures."""
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = session.get(url, timeout=timeout, headers=headers or {})
            if r.status_code in RETRY_STATUS:
                raise GalleryError(f"HTTP {r.status_code} for {url}")
            if r.status_code >= 400 and r.status_code != 304:
                raise GalleryError(f"HTTP {r.status_code} for {url} (not retried)")
            return r
        except (requests.ConnectionError, requests.Timeout, GalleryError) as e:
            last = e
            if isinstance(e, GalleryError) and "not retried" in str(e):
                raise
            if attempt == retries:
                break
            delay = min(30.0, 2.0 ** (attempt + 1)) * (0.75 + random.random() * 0.5)
            log.warning("retrying %s in %.1fs after: %s", url, delay, e)
            sleep(delay)
    raise GalleryError(f"giving up on {url}: {last}")


def get_json(session: requests.Session, path_or_url: str, **kw: Any) -> Any:
    url = path_or_url if path_or_url.startswith("http") else config.GALLERY_URL + path_or_url
    r = request_with_retry(session, url, **kw)
    try:
        return r.json()
    except ValueError as e:
        raise GalleryError(f"invalid JSON from {url}: {e}") from e


@dataclass
class SourceRecord:
    id: str
    film: str
    film_title: str
    url_s: str
    url_m: str
    url_full: str
    page: str
    plate: bool
    her: bool
    cut: bool
    edited: bool
    verdict: str | None
    look: str | None
    framing: str
    kind: str
    w: int
    h: int
    prompt: str

    def fingerprint(self) -> str:
        """Fields that, if changed, mean the image itself may have changed."""
        return f"{self.url_m}|{self.w}x{self.h}|{int(self.edited)}"

    def to_dict(self) -> dict:
        return asdict(self)


def _abs(u: Any) -> str:
    if not isinstance(u, str) or not u:
        return ""
    return u if u.startswith("http") else config.GALLERY_URL + (u if u.startswith("/") else "/" + u)


def parse_record(raw: Any) -> SourceRecord | None:
    """Validate one item from /api/images.json. Returns None for unusable rows."""
    if not isinstance(raw, dict):
        return None
    rid = raw.get("id")
    film = raw.get("film")
    if not isinstance(rid, str) or not rid or not isinstance(film, str) or not film:
        return None
    url = raw.get("url")
    if isinstance(url, str):
        url = {"m": url}
    if not isinstance(url, dict):
        return None
    m = _abs(url.get("m") or url.get("full") or url.get("s"))
    if not m:
        return None

    def as_int(v: Any) -> int:
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    verdict = raw.get("verdict")
    return SourceRecord(
        id=rid,
        film=film,
        film_title=str(raw.get("film_title") or film),
        url_s=_abs(url.get("s")),
        url_m=m,
        url_full=_abs(url.get("full")),
        page=_abs(raw.get("page")),
        # Flags must be literally true; strings or 1s from a changed API are not trusted.
        plate=raw.get("plate") is True,
        her=raw.get("her") is True,
        cut=raw.get("cut") is True,
        edited=raw.get("edited") is True,
        verdict=verdict if isinstance(verdict, str) else None,
        look=raw.get("look") if isinstance(raw.get("look"), str) else None,
        framing=str(raw.get("framing") or ""),
        kind=str(raw.get("kind") or ""),
        w=as_int(raw.get("w")),
        h=as_int(raw.get("h")),
        prompt=str(raw.get("prompt") or ""),
    )


def is_film_canon(r: SourceRecord) -> bool:
    return r.plate is True and r.her is True


def is_full_archive(r: SourceRecord) -> bool:
    return r.her is True


MODE_FILTERS: dict[str, Callable[[SourceRecord], bool]] = {
    "canon": is_film_canon,
    "archive": is_full_archive,
}


def in_mode(r: SourceRecord, mode: str) -> bool:
    return MODE_FILTERS[mode](r)


def _extract_items(data: Any) -> tuple[list, str | None]:
    """Accept the current {items: [...]} shape and plausible future shapes."""
    nxt = None
    if isinstance(data, list):
        return data, None
    if not isinstance(data, dict):
        raise GalleryError("images payload is neither a list nor an object")
    for key in ("next", "next_page", "nextPage"):
        if isinstance(data.get(key), str) and data[key]:
            nxt = data[key]
    links = data.get("links")
    if isinstance(links, dict) and isinstance(links.get("next"), str):
        nxt = links["next"]
    for key in ("items", "images", "data", "results"):
        if isinstance(data.get(key), list):
            return data[key], nxt
    raise GalleryError("images payload has no recognisable item list")


def fetch_records(session: requests.Session, max_pages: int = 200) -> tuple[list[SourceRecord], dict]:
    """Fetch every image record, following pagination if the API ever adds it."""
    url: str | None = "/api/images.json"
    raws: list = []
    declared = None
    seen_urls = set()
    for _ in range(max_pages):
        if not url or url in seen_urls:
            break
        seen_urls.add(url)
        data = get_json(session, url)
        if isinstance(data, dict) and isinstance(data.get("count"), int) and declared is None:
            declared = data["count"]
        items, url = _extract_items(data)
        raws.extend(items)
    records: dict[str, SourceRecord] = {}
    bad = 0
    for raw in raws:
        rec = parse_record(raw)
        if rec is None:
            bad += 1
            continue
        records[rec.id] = rec
    if bad:
        log.warning("skipped %d malformed image records", bad)
    if declared is not None and len(records) < declared * 0.9:
        log.warning("API declared %d images but only %d parsed", declared, len(records))
    return list(records.values()), {"declared": declared, "parsed": len(records), "malformed": bad}


def fetch_films(session: requests.Session, records: Iterable[SourceRecord] = ()) -> list[dict]:
    """Films in the gallery's own order. Falls back to the order films appear in records."""
    films: list[dict] = []
    try:
        data = get_json(session, "/api/films.json")
        for f in (data.get("films") if isinstance(data, dict) else data) or []:
            if isinstance(f, dict) and isinstance(f.get("id"), str):
                films.append({
                    "id": f["id"],
                    "title": str(f.get("title") or f["id"]),
                    "released": f.get("released") if isinstance(f.get("released"), str) else None,
                    "page": _abs(f.get("page")),
                })
    except GalleryError as e:
        log.warning("films endpoint unavailable, deriving films from records: %s", e)
    known = {f["id"] for f in films}
    for r in records:
        if r.film not in known:
            known.add(r.film)
            films.append({"id": r.film, "title": r.film_title, "released": None, "page": ""})
    return films


STYLIZED_LOOK = re.compile(
    r"translucent|see-through|porcelain|mask of her face|anime|cel-shaded|cartoon|illustrat|drawn|hologram of her|made of the phone",
    re.I,
)

# Transparent / tinted eyewear often still segments as "face", so the pixel sunglass
# score misses it. The gallery prompt says when she's wearing them on her eyes.
_EYEWEAR = re.compile(
    r"\b(?:sunglasses|goggles|eyewear|shades)\b|"
    r"\b(?:shield|wraparound|rimless|tinted)\s+[^,]{0,24}(?:sunglasses|goggles)\b|"
    r"\b(?:virtual[- ]reality|vr)\s+headset\b|"
    r"\bheadset\s+(?:visor|with\s+[^,]{0,40}visor)\b",
    re.I,
)
_EYEWEAR_OFF = re.compile(
    r"pushed\s+up\b|"
    r"\bholding\b|"
    r"\bbeside\s+her\s+head\b|"
    r"\bat\s+chest\s+height\b|"
    r"\b(?:lift|lifting|lifts|remove|removing|removes|off)\b[^.]{0,40}\b(?:headset|sunglasses|goggles|visor)\b|"
    r"\b(?:headset|sunglasses|goggles|visor)\b[^.]{0,40}\b(?:off\s+her\s+head|in\s+her\s+hand)",
    re.I,
)


def worn_eyewear_reason(prompt: str | None) -> str | None:
    """If the prompt says eyewear is covering her eyes, return a short exclusion reason."""
    if not prompt or not _EYEWEAR.search(prompt):
        return None
    if _EYEWEAR_OFF.search(prompt):
        return None
    return "eyes hidden (sunglasses or visor)"


@dataclass
class CanonInfo:
    reference_ids: list[str]
    stylized_looks: set[str]
    looks: dict[str, str]


def fetch_canon(session: requests.Session) -> CanonInfo:
    """Official identity anchors and which looks are non-photographic depictions."""
    data = get_json(session, "/api/canon.json")
    if not isinstance(data, dict):
        raise GalleryError("canon payload is not an object")
    looks: dict[str, str] = {}
    for l in data.get("looks") or []:
        if isinstance(l, dict) and isinstance(l.get("id"), str):
            looks[l["id"]] = str(l.get("text") or "")
    av = data.get("avatar")
    if isinstance(av, dict):
        looks.setdefault("avatar", str(av.get("text") or "avatar"))
    stylized = {k for k, t in looks.items() if STYLIZED_LOOK.search(t)}
    refs = [x for x in (data.get("faces") or []) if isinstance(x, str)]
    return CanonInfo(reference_ids=refs, stylized_looks=stylized, looks=looks)
