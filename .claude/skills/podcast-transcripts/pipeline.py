#!/usr/bin/env python3
"""Resolve podcast episode pages, transcribe their audio, and write markdown."""

from __future__ import annotations

import argparse
import difflib
import html as html_lib
import json
import re
import shlex
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit

import requests


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36"
)
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 1
PAGE_TIMEOUT = 60

# Feed-of-last-resort per domain, used only when the normal strategy chain fails
# (e.g. the site's own /feed/ endpoint is broken or the page omits an RSS link).
DOMAIN_FEED_OVERRIDES = {
    "gaintractionpodcast.com": "https://feeds.transistor.fm/gain-traction",
    "ratchetandwrench.com": "https://feed.podbean.com/ratchetandwrenchradio/feed.xml",
    "wrenchway.com": "https://rss.buzzsprout.com/940384.rss",
}
DEEPGRAM_TIMEOUT = 900
DEEPGRAM_URL = (
    "https://api.deepgram.com/v1/listen?model=nova-3&smart_format=true&"
    "paragraphs=true&diarize=true"
)

AUDIO_RE = re.compile(
    r'https?://[^"\' <>\\]+\.(?:mp3|m4a|wav)[^"\' <>\\]*',
    re.IGNORECASE,
)
APPLE_EPISODE_RE = re.compile(
    r"podcasts\.apple\.com/[a-z]{2}/podcast/[^\"']*id(\d+)[^\"']*[?&]i=(\d+)",
    re.IGNORECASE,
)
APPLE_SHOW_RE = re.compile(
    r"podcasts\.apple\.com/[a-z]{2}/podcast/[^\"']*id(\d+)[^\"']*",
    re.IGNORECASE,
)
RSS_LINK_RE = re.compile(
    r"<link\b(?=[^>]*\btype\s*=\s*['\"]application/rss\+xml['\"])(?=[^>]*\bhref\s*=\s*['\"]([^'\"]+)['\"])[^>]*>",
    re.IGNORECASE,
)
PODBEAN_EMBED_RE = re.compile(
    r'https?://[^"\' <>\\]*podbean\.com/player-v2/[^"\' <>\\]*',
    re.IGNORECASE,
)
BUZZSPROUT_EMBED_RE = re.compile(
    r'https?://[^"\' <>\\]*buzzsprout\.com/(\d+)[^"\' <>\\]*',
    re.IGNORECASE,
)
KNOWN_HOST_EMBED_PATTERNS = [
    ("libsyn", re.compile(r'https?://[^"\' <>\\]*libsyn[^"\' <>\\]*', re.IGNORECASE)),
    (
        "megaphone",
        re.compile(r'https?://[^"\' <>\\]*megaphone[^"\' <>\\]*', re.IGNORECASE),
    ),
    (
        "simplecast",
        re.compile(r'https?://[^"\' <>\\]*simplecast[^"\' <>\\]*', re.IGNORECASE),
    ),
    (
        "transistor",
        re.compile(r'https?://[^"\' <>\\]*transistor[^"\' <>\\]*', re.IGNORECASE),
    ),
    ("omny", re.compile(r'https?://[^"\' <>\\]*omny[^"\' <>\\]*', re.IGNORECASE)),
    (
        "spreaker",
        re.compile(r'https?://[^"\' <>\\]*spreaker[^"\' <>\\]*', re.IGNORECASE),
    ),
    ("art19", re.compile(r'https?://[^"\' <>\\]*art19[^"\' <>\\]*', re.IGNORECASE)),
    (
        "captivate",
        re.compile(r'https?://[^"\' <>\\]*captivate[^"\' <>\\]*', re.IGNORECASE),
    ),
]


class ResolutionError(RuntimeError):
    """A page was fetched but could not be resolved to an episode audio item."""


def request_with_retry(
    method: str,
    url: str,
    *,
    timeout: int,
    headers: Optional[Dict[str, str]] = None,
    **kwargs: Any,
) -> requests.Response:
    """Make an HTTP API call, retrying three total attempts with backoff."""

    method = method.upper()
    last_error: Optional[Exception] = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            if method == "GET":
                response = requests.get(url, headers=headers, timeout=timeout, **kwargs)
            elif method == "POST":
                response = requests.post(url, headers=headers, timeout=timeout, **kwargs)
            else:
                raise ValueError(f"unsupported HTTP method: {method}")
            response.raise_for_status()
            return response
        except Exception as exc:  # requests and HTTP-status failures are retriable API failures.
            last_error = exc
            if attempt == RETRY_ATTEMPTS - 1:
                break
            time.sleep(RETRY_BACKOFF_SECONDS * (2**attempt))

    assert last_error is not None
    raise RuntimeError(f"{method} {url} failed after 3 attempts: {last_error}") from last_error


class PageMetadataParser(HTMLParser):
    """Small HTML parser for title and OpenGraph metadata."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: List[str] = []
        self.meta: Dict[str, str] = {}
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        attributes = {
            key.lower(): html_lib.unescape(value or "") for key, value in attrs
        }
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = (attributes.get("property") or attributes.get("name") or "").lower()
            content = attributes.get("content", "").strip()
            if key and content and key not in self.meta:
                self.meta[key] = content

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)


class RSSLinkParser(HTMLParser):
    """Find an RSS link tag without depending on attribute ordering."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.href: Optional[str] = None

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag.lower() != "link" or self.href:
            return
        attributes = {
            key.lower(): html_lib.unescape(value or "") for key, value in attrs
        }
        link_type = attributes.get("type", "").lower().split(";", 1)[0].strip()
        if link_type == "application/rss+xml" and attributes.get("href"):
            self.href = attributes["href"]


def page_metadata(markup: str) -> Dict[str, str]:
    parser = PageMetadataParser()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:
        # A malformed page can still contain a usable direct audio URL.
        pass

    title_tag = " ".join("".join(parser.title_parts).split())
    og_title = parser.meta.get("og:title", "").strip()
    title = og_title or title_tag
    description = parser.meta.get("og:description") or parser.meta.get("description", "")
    show = parser.meta.get("og:site_name") or parser.meta.get("author", "")
    return {
        "title": html_lib.unescape(title).strip(),
        "description": strip_html(description),
        "show": html_lib.unescape(show).strip(),
    }


def strip_html(value: str, limit: int = 500) -> str:
    """Remove tags, normalize whitespace, and cap descriptions at 500 chars."""

    text = html_lib.unescape(value or "")
    text = re.sub(r"<[^>]*>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def normalize_title(value: str) -> str:
    """Lowercase a title and remove punctuation and whitespace."""

    normalized = unicodedata.normalize("NFKD", value or "").lower()
    return "".join(char for char in normalized if char.isalnum())


def slugify(value: str) -> str:
    """Create the required lowercase, hyphenated, 80-character filename slug."""

    normalized = (
        unicodedata.normalize("NFKD", value or "")
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    slug = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
    slug = re.sub(r"-+", "-", slug)[:80].rstrip("-")
    return slug or "episode"


def _fallback_title(page_url: str) -> str:
    path = unquote(urlsplit(page_url).path).rstrip("/")
    segment = path.rsplit("/", 1)[-1] if path else ""
    segment = re.sub(r"\.(?:html?|php)$", "", segment, flags=re.IGNORECASE)
    segment = re.sub(r"[-_]+", " ", segment).strip()
    return segment or "Untitled episode"


def _page_slug_words(page_url: str) -> List[str]:
    path = unquote(urlsplit(page_url).path).rstrip("/")
    segment = path.rsplit("/", 1)[-1] if path else ""
    segment = re.sub(r"\.[a-z0-9]{1,8}$", "", segment, flags=re.IGNORECASE)
    return re.findall(r"[a-z0-9]+", segment.lower())


@dataclass
class FeedEpisode:
    title: str
    audio_url: str
    description: str
    pub_date: str


def _local_name(tag: Any) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].lower()


def _element_text(element: Optional[ET.Element]) -> str:
    if element is None:
        return ""
    return "".join(element.itertext()).strip()


def _direct_child(element: ET.Element, names: Iterable[str]) -> Optional[ET.Element]:
    wanted = {name.lower() for name in names}
    for child in list(element):
        if _local_name(child.tag) in wanted:
            return child
    return None


def parse_feed(feed_text: str) -> Tuple[str, List[FeedEpisode]]:
    """Parse RSS channel metadata and episode enclosures."""

    try:
        root = ET.fromstring(feed_text)
    except ET.ParseError as exc:
        raise ResolutionError(f"RSS feed XML could not be parsed: {exc}") from exc

    channel = next((element for element in root.iter() if _local_name(element.tag) == "channel"), None)
    show_element = _direct_child(channel, {"title"}) if channel is not None else _direct_child(root, {"title"})
    show = strip_html(_element_text(show_element), limit=500)

    episodes: List[FeedEpisode] = []
    for item in root.iter():
        if _local_name(item.tag) != "item":
            continue
        title = _element_text(_direct_child(item, {"title"}))
        enclosure = _direct_child(item, {"enclosure"})
        audio_url = (enclosure.attrib.get("url", "").strip() if enclosure is not None else "")
        if not audio_url:
            continue
        description_element = _direct_child(item, {"description"})
        if not _element_text(description_element):
            description_element = _direct_child(item, {"summary"})
        pub_date = _element_text(_direct_child(item, {"pubdate", "published", "date"}))
        episodes.append(
            FeedEpisode(
                title=html_lib.unescape(title).strip(),
                audio_url=html_lib.unescape(audio_url).strip(),
                description=strip_html(_element_text(description_element)),
                pub_date=html_lib.unescape(pub_date).strip(),
            )
        )

    if not episodes:
        raise ResolutionError("RSS feed contains no items with enclosure audio")
    return show, episodes


def _match_feed_episode(
    episodes: List[FeedEpisode], page_title: str, page_url: str
) -> FeedEpisode:
    """Match by title first, then by words in the page URL slug."""

    title_target = normalize_title(page_title)
    best_ratio = 0.0
    if title_target:
        scored = [
            (
                difflib.SequenceMatcher(None, title_target, normalize_title(episode.title)).ratio(),
                episode,
            )
            for episode in episodes
            if episode.title
        ]
        if scored:
            best_ratio, best_episode = max(scored, key=lambda pair: pair[0])
            if best_ratio >= 0.6:
                return best_episode

    slug_words = _page_slug_words(page_url)
    if slug_words:
        generic = {"podcast", "podcasts", "episode", "episodes", "listen", "show"}
        meaningful = [word for word in slug_words if word not in generic]
        if not meaningful:
            meaningful = slug_words
        slug_set = set(meaningful)
        slug_target = normalize_title(" ".join(slug_words))
        slug_scored: List[Tuple[float, FeedEpisode]] = []
        for episode in episodes:
            item_words = set(re.findall(r"[a-z0-9]+", episode.title.lower()))
            overlap = len(slug_set.intersection(item_words))
            if overlap:
                ratio = difflib.SequenceMatcher(
                    None, slug_target, normalize_title(episode.title)
                ).ratio()
                score = overlap / max(len(slug_set), 1) + ratio / 10
                slug_scored.append((score, episode))
        if slug_scored:
            return max(slug_scored, key=lambda pair: pair[0])[1]

    if title_target:
        raise ResolutionError(
            f"no RSS episode matched title (best title ratio {best_ratio:.2f}; required 0.60)"
        )
    raise ResolutionError("no RSS episode matched the page URL slug")


def _extract_rss_link(markup: str, page_url: str) -> Optional[str]:
    head_match = re.search(r"<head\b[^>]*>(.*?)</head\s*>", markup, re.IGNORECASE | re.DOTALL)
    scope = head_match.group(1) if head_match else markup

    parser = RSSLinkParser()
    try:
        parser.feed(scope)
        parser.close()
    except Exception:
        pass
    href = parser.href
    if not href:
        regex_match = RSS_LINK_RE.search(scope)
        href = regex_match.group(1) if regex_match else None
    return urljoin(page_url, html_lib.unescape(href)) if href else None


class Resolver:
    """Page resolver with in-memory Apple lookup and RSS feed caches."""

    def __init__(self) -> None:
        self.lookup_cache: Dict[str, str] = {}
        self.feed_cache: Dict[str, Tuple[str, List[FeedEpisode]]] = {}

    def lookup_feed_url(self, collection_id: str) -> str:
        if collection_id in self.lookup_cache:
            return self.lookup_cache[collection_id]

        lookup_url = f"https://itunes.apple.com/lookup?id={quote(collection_id)}"
        response = request_with_retry(
            "GET", lookup_url, timeout=PAGE_TIMEOUT, headers={"User-Agent": USER_AGENT}
        )
        try:
            payload = response.json()
        except Exception as exc:
            raise ResolutionError(f"Apple lookup returned invalid JSON: {exc}") from exc
        results = payload.get("results") if isinstance(payload, dict) else None
        feed_url = results[0].get("feedUrl") if results else None
        if not feed_url:
            raise ResolutionError(f"Apple lookup returned no feedUrl for collection {collection_id}")
        self.lookup_cache[collection_id] = str(feed_url)
        return str(feed_url)

    def fetch_feed(self, feed_url: str) -> Tuple[str, List[FeedEpisode]]:
        if feed_url in self.feed_cache:
            return self.feed_cache[feed_url]

        response = request_with_retry(
            "GET", feed_url, timeout=PAGE_TIMEOUT, headers={"User-Agent": USER_AGENT}
        )
        feed = parse_feed(response.text)
        self.feed_cache[feed_url] = feed
        return feed


def _record(
    page_url: str,
    *,
    show: str = "",
    episode_title: str = "",
    audio_url: str = "",
    description: str = "",
    pub_date: str = "",
    status: str = "resolved",
) -> Dict[str, str]:
    return {
        "page_url": page_url,
        "show": show,
        "episode_title": episode_title,
        "audio_url": audio_url,
        "description": strip_html(description),
        "pub_date": pub_date,
        "status": status,
    }


def _feed_record(
    page_url: str,
    page_metadata_values: Dict[str, str],
    show: str,
    episode: FeedEpisode,
) -> Dict[str, str]:
    return _record(
        page_url,
        show=show or page_metadata_values.get("show", ""),
        episode_title=episode.title or page_metadata_values.get("title", ""),
        audio_url=episode.audio_url,
        description=episode.description,
        pub_date=episode.pub_date,
    )


def resolve_page(page_url: str, resolver: Resolver) -> Dict[str, str]:
    """Resolve one page using the ordered extraction strategy chain."""

    response = request_with_retry(
        "GET",
        page_url,
        timeout=PAGE_TIMEOUT,
        headers={"User-Agent": USER_AGENT},
    )
    markup = html_lib.unescape(response.text or "")
    metadata = page_metadata(markup)
    page_title = metadata.get("title", "")
    display_title = page_title or _fallback_title(page_url)

    # 1. Direct audio URL in the page HTML.
    direct_audio = AUDIO_RE.search(markup)
    if direct_audio:
        return _record(
            page_url,
            show=metadata.get("show", ""),
            episode_title=display_title,
            audio_url=direct_audio.group(0),
            description=metadata.get("description", ""),
        )

    # 2. Apple episode link: lookup the collection, then match its RSS item.
    apple_episode = APPLE_EPISODE_RE.search(markup)
    if apple_episode:
        collection_id = apple_episode.group(1)
        feed_url = resolver.lookup_feed_url(collection_id)
        show, episodes = resolver.fetch_feed(feed_url)
        episode = _match_feed_episode(episodes, page_title, page_url)
        return _feed_record(page_url, metadata, show, episode)

    # 3. Apple show link without an episode query parameter.
    apple_show: Optional[re.Match[str]] = None
    for candidate in APPLE_SHOW_RE.finditer(markup):
        if not re.search(r"[?&]i=\d+", candidate.group(0), re.IGNORECASE):
            apple_show = candidate
            break
    if apple_show:
        collection_id = apple_show.group(1)
        feed_url = resolver.lookup_feed_url(collection_id)
        show, episodes = resolver.fetch_feed(feed_url)
        episode = _match_feed_episode(episodes, page_title, page_url)
        return _feed_record(page_url, metadata, show, episode)

    # 4. Known host embeds. The first matching host determines the strategy.
    podbean = PODBEAN_EMBED_RE.search(markup)
    if podbean:
        embed_url = podbean.group(0)
        episode_key = parse_qs(urlsplit(embed_url).query).get("i", [None])[0]
        if not episode_key or not episode_key.endswith("-pb"):
            raise ResolutionError("Podbean player embed has no i=<episodeKey>-pb value")
        player_url = "https://www.podbean.com/player-v2/?i=" + quote(
            unquote(episode_key), safe="-_.~"
        )
        player_response = request_with_retry(
            "GET", player_url, timeout=PAGE_TIMEOUT, headers={"User-Agent": USER_AGENT}
        )
        player_markup = html_lib.unescape(player_response.text or "")
        player_audio = AUDIO_RE.search(player_markup)
        if not player_audio:
            raise ResolutionError("Podbean player did not contain an mp3/m4a/wav URL")
        return _record(
            page_url,
            show=metadata.get("show", ""),
            episode_title=display_title,
            audio_url=player_audio.group(0),
            description=metadata.get("description", ""),
        )

    buzzsprout = BUZZSPROUT_EMBED_RE.search(markup)
    if buzzsprout:
        feed_url = f"https://rss.buzzsprout.com/{buzzsprout.group(1)}.rss"
        show, episodes = resolver.fetch_feed(feed_url)
        episode = _match_feed_episode(episodes, page_title, page_url)
        return _feed_record(page_url, metadata, show, episode)

    for _host, host_pattern in KNOWN_HOST_EMBED_PATTERNS:
        if host_pattern.search(markup):
            feed_url = _override_feed(page_url) or _extract_rss_link(markup, page_url)
            if not feed_url:
                raise ResolutionError(
                    "known host embed found, but no application/rss+xml link was present"
                )
            show, episodes = resolver.fetch_feed(feed_url)
            episode = _match_feed_episode(episodes, page_title, page_url)
            return _feed_record(page_url, metadata, show, episode)

    feed_url = _override_feed(page_url)
    if feed_url:
        show, episodes = resolver.fetch_feed(feed_url)
        episode = _match_feed_episode(episodes, page_title, page_url)
        return _feed_record(page_url, metadata, show, episode)

    raise ResolutionError("no direct audio URL, Apple link, or known host embed found")


def _override_feed(page_url: str) -> Optional[str]:
    host = (urlsplit(page_url).hostname or "").removeprefix("www.")
    return DOMAIN_FEED_OVERRIDES.get(host)


def _repo_root() -> Path:
    # .claude/skills/podcast-transcripts/pipeline.py -> repository root.
    return Path(__file__).resolve().parents[3]


def read_deepgram_key() -> str:
    """Read DEEPGRAM_API_KEY from the repository .env without requiring export."""

    env_path = _repo_root() / ".env"
    if not env_path.is_file():
        raise RuntimeError(f"missing .env at repository root: {env_path}")

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, separator, raw_value = line.partition("=")
        if separator and name.strip() == "DEEPGRAM_API_KEY":
            value = raw_value.strip()
            try:
                parsed = shlex.split(value, comments=True, posix=True)
                value = parsed[0] if parsed else ""
            except ValueError:
                value = value.strip("\"'")
            if value:
                return value
            break
    raise RuntimeError("DEEPGRAM_API_KEY is missing or empty in the repository .env")


def _transcript_from_response(payload: Dict[str, Any]) -> str:
    results = payload.get("results", {}) if isinstance(payload, dict) else {}
    channels = results.get("channels", []) if isinstance(results, dict) else []
    if not channels:
        raise RuntimeError("Deepgram response has no channels")
    alternatives = channels[0].get("alternatives", [])
    if not alternatives:
        raise RuntimeError("Deepgram response has no alternatives")
    alternative = alternatives[0]
    paragraphs_container = alternative.get("paragraphs", {})
    paragraphs = (
        paragraphs_container.get("paragraphs", [])
        if isinstance(paragraphs_container, dict)
        else []
    )

    if paragraphs:
        blocks: List[Tuple[str, List[str]]] = []
        for paragraph in paragraphs:
            if not isinstance(paragraph, dict):
                continue
            speaker = str(paragraph.get("speaker", 0))
            sentences = paragraph.get("sentences", [])
            sentence_texts: List[str] = []
            for sentence in sentences if isinstance(sentences, list) else []:
                if isinstance(sentence, dict):
                    text = str(sentence.get("text", "")).strip()
                else:
                    text = str(sentence).strip()
                if text:
                    sentence_texts.append(text)
            paragraph_text = " ".join(sentence_texts).strip()
            if not paragraph_text:
                paragraph_text = str(paragraph.get("text", "")).strip()
            if not paragraph_text:
                continue
            if blocks and blocks[-1][0] == speaker:
                blocks[-1][1].append(paragraph_text)
            else:
                blocks.append((speaker, [paragraph_text]))
        if blocks:
            return "\n\n".join(
                f"Speaker {speaker}:\n{' '.join(paragraphs_for_speaker)}"
                for speaker, paragraphs_for_speaker in blocks
            )

    transcript = str(alternative.get("transcript", "")).strip()
    if transcript:
        return transcript
    raise RuntimeError("Deepgram response has no paragraph or raw transcript text")


def transcribe_audio(audio_url: str, api_key: str) -> str:
    try:
        response = request_with_retry(
            "POST",
            DEEPGRAM_URL,
            timeout=DEEPGRAM_TIMEOUT,
            headers={
                "Authorization": f"Token {api_key}",
                "Content-Type": "application/json",
            },
            json={"url": audio_url},
        )
    except Exception:
        # Some hosts (e.g. Buzzsprout) intermittently refuse Deepgram's URL
        # fetcher with a 400. Download the audio ourselves and send the bytes.
        audio = request_with_retry(
            "GET",
            audio_url,
            timeout=DEEPGRAM_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        )
        response = request_with_retry(
            "POST",
            DEEPGRAM_URL,
            timeout=DEEPGRAM_TIMEOUT,
            headers={
                "Authorization": f"Token {api_key}",
                "Content-Type": "audio/mpeg",
            },
            data=audio.content,
        )
    try:
        payload = response.json()
    except Exception as exc:
        raise RuntimeError(f"Deepgram returned invalid JSON: {exc}") from exc
    return _transcript_from_response(payload)


def _write_resolved(path: Path, entries: List[Dict[str, str]]) -> None:
    path.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _load_resolved(path: Path) -> Dict[str, Dict[str, str]]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"could not read checkpoint {path}: {exc}") from exc
    if not isinstance(payload, list):
        raise RuntimeError(f"checkpoint {path} must contain a JSON list")
    return {
        str(entry["page_url"]): dict(entry)
        for entry in payload
        if isinstance(entry, dict) and entry.get("page_url")
    }


def _markdown_path(out_dir: Path, entry: Dict[str, str]) -> Path:
    return out_dir / f"{slugify(entry.get('episode_title', ''))}.md"


def _parse_markdown_checkpoint(path: Path) -> Optional[Dict[str, str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    title_match = re.search(r"^#\s+(.+?)\s*$", text, re.MULTILINE)
    show_match = re.search(
        r"^\*\*Show:\*\*\s*(.*?)\s+\|\s+\*\*Published:\*\*\s*(.*?)\s*$",
        text,
        re.MULTILINE,
    )
    source_match = re.search(r"^\*\*Source page:\*\*\s*(.*?)\s*$", text, re.MULTILINE)
    audio_match = re.search(r"^\*\*Audio:\*\*\s*(.*?)\s*$", text, re.MULTILINE)
    if not title_match or not source_match:
        return None
    description_match = re.search(
        r"^\*\*Description:\*\*\s*(.*?)\s*$", text, re.MULTILINE
    )
    return _record(
        source_match.group(1).strip(),
        show=show_match.group(1).strip() if show_match else "",
        episode_title=title_match.group(1).strip(),
        audio_url=audio_match.group(1).strip() if audio_match else "",
        description=description_match.group(1).strip() if description_match else "",
        pub_date=show_match.group(2).strip() if show_match else "",
        status="transcribed",
    )


def _discover_markdown_checkpoints(out_dir: Path) -> Dict[str, Tuple[Path, Dict[str, str]]]:
    checkpoints: Dict[str, Tuple[Path, Dict[str, str]]] = {}
    for path in sorted(out_dir.glob("*.md")):
        if path.name == "index.md":
            continue
        entry = _parse_markdown_checkpoint(path)
        if entry:
            checkpoints[entry["page_url"]] = (path, entry)
    return checkpoints


def _write_markdown(path: Path, entry: Dict[str, str], transcript: str) -> None:
    content = (
        f"# {entry.get('episode_title', '')}\n"
        f"**Show:** {entry.get('show', '')} | **Published:** {entry.get('pub_date', '')}\n"
        f"**Source page:** {entry.get('page_url', '')}\n"
        f"**Audio:** {entry.get('audio_url', '')}\n\n"
        f"**Description:** {entry.get('description', '')}\n\n"
        "---\n\n"
        "## Transcript\n"
        f"{transcript.rstrip()}\n"
    )
    path.write_text(content, encoding="utf-8")


def _table_cell(value: str) -> str:
    return (value or "").replace("|", "\\|").replace("\n", " ").strip()


def _write_index(out_dir: Path, entries: List[Dict[str, str]]) -> None:
    lines = [
        "# Automotive Podcast Transcripts",
        "",
        "| Episode | Show | Source | File |",
        "| --- | --- | --- | --- |",
    ]
    for entry in entries:
        path = _markdown_path(out_dir, entry)
        if not path.is_file():
            continue
        episode = _table_cell(entry.get("episode_title", ""))
        show = _table_cell(entry.get("show", ""))
        source_url = entry.get("page_url", "")
        lines.append(
            f"| {episode} | {show} | [Source]({source_url}) | "
            f"[{path.name}]({path.name}) |"
        )
    out_dir.joinpath("index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _failure_record(page_url: str) -> Dict[str, str]:
    return _record(page_url, status="failed")


def _summary(
    entries: List[Dict[str, str]], failures: List[Tuple[str, str]], out_dir: Path
) -> None:
    # An item that resolved to audio but failed during transcription still counts
    # as resolved, which makes the stage totals useful when a rerun is needed.
    resolved_count = sum(bool(entry.get("audio_url")) for entry in entries)
    transcribed_count = sum(
        entry.get("status") == "transcribed" and _markdown_path(out_dir, entry).is_file()
        for entry in entries
    )
    reasons = "; ".join(f"{page_url}: {reason}" for page_url, reason in failures)
    suffix = f" ({reasons})" if reasons else ""
    print(
        f"Summary: {resolved_count} resolved, {transcribed_count} transcribed, "
        f"{len(failures)} failed{suffix}"
    )


def run_pipeline(urls_file: Path, out_dir: Path, only_resolve: bool = False) -> int:
    if not urls_file.is_file():
        raise RuntimeError(f"urls file not found: {urls_file}")
    raw_urls = [line.strip() for line in urls_file.read_text(encoding="utf-8").splitlines()]
    urls: List[str] = []
    seen = set()
    for url in raw_urls:
        if not url or url.startswith("#"):
            continue
        if not url.startswith(("http://", "https://")):
            raise RuntimeError(f"invalid podcast page URL (expected http/https): {url}")
        if url not in seen:
            urls.append(url)
            seen.add(url)
    if not urls:
        raise RuntimeError(f"urls file contains no podcast page URLs: {urls_file}")

    if out_dir.exists() and not out_dir.is_dir():
        raise RuntimeError(f"output path is not a directory: {out_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)

    checkpoint_path = out_dir / "resolved.json"
    previous = _load_resolved(checkpoint_path)
    markdown_checkpoints = _discover_markdown_checkpoints(out_dir)
    all_cached = all(
        page_url in markdown_checkpoints
        or (
            previous.get(page_url) is not None
            and _markdown_path(out_dir, previous[page_url]).is_file()
        )
        for page_url in urls
    )
    deepgram_key = ""
    if not only_resolve and not all_cached:
        # Validate the paid API input before any uncached page is processed.
        deepgram_key = read_deepgram_key()

    resolver = Resolver()
    entries: List[Dict[str, str]] = []
    failures: List[Tuple[str, str]] = []

    for page_url in urls:
        prior = previous.get(page_url)
        cached_path: Optional[Path] = None
        cached_entry: Optional[Dict[str, str]] = None
        if page_url in markdown_checkpoints:
            cached_path, cached_entry = markdown_checkpoints[page_url]
        elif prior and _markdown_path(out_dir, prior).is_file():
            cached_path = _markdown_path(out_dir, prior)
            cached_entry = _parse_markdown_checkpoint(cached_path)

        if cached_path and cached_path.is_file():
            entry = dict(prior or cached_entry or _failure_record(page_url))
            if cached_entry:
                for key, value in cached_entry.items():
                    if not entry.get(key):
                        entry[key] = value
            entry["status"] = "transcribed"
            entries.append(entry)
            print(f"cached {page_url}")
            continue

        if prior and prior.get("audio_url") and prior.get("status") in {
            "resolved",
            "transcribed",
            "failed",
        }:
            entry = dict(prior)
            entry["status"] = "resolved"
            entries.append(entry)
            print(f"resolved-cached {page_url}")
            _write_resolved(checkpoint_path, entries)
            continue

        try:
            entry = resolve_page(page_url, resolver)
            print(f"resolved {page_url}")
        except Exception as exc:
            reason = str(exc) or exc.__class__.__name__
            entry = _failure_record(page_url)
            failures.append((page_url, reason))
            print(f"failed {page_url}: {reason}", file=sys.stderr)
        entries.append(entry)
        _write_resolved(checkpoint_path, entries)

    # Always leave a complete resolution checkpoint, including cached entries.
    _write_resolved(checkpoint_path, entries)
    if only_resolve:
        _summary(entries, failures, out_dir)
        return 0

    candidates = [
        (index, entry)
        for index, entry in enumerate(entries)
        if entry.get("audio_url") and entry.get("status") == "resolved"
    ]
    with ThreadPoolExecutor(max_workers=4) as executor:
        future_to_index = {
            executor.submit(transcribe_audio, entry["audio_url"], deepgram_key): index
            for index, entry in candidates
        }
        for future in as_completed(future_to_index):
            index = future_to_index[future]
            entry = entries[index]
            try:
                transcript = future.result()
                output_path = _markdown_path(out_dir, entry)
                _write_markdown(output_path, entry, transcript)
                entry["status"] = "transcribed"
                _write_resolved(checkpoint_path, entries)
                print(f"transcribed {entry.get('page_url', '')}")
            except Exception as exc:
                reason = str(exc) or exc.__class__.__name__
                entry["status"] = "failed"
                failures.append((entry.get("page_url", ""), reason))
                _write_resolved(checkpoint_path, entries)
                print(f"failed {entry.get('page_url', '')}: {reason}", file=sys.stderr)

    _write_index(out_dir, entries)
    _write_resolved(checkpoint_path, entries)
    _summary(entries, failures, out_dir)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Resolve podcast pages, transcribe audio with Deepgram, and write markdown."
    )
    parser.add_argument("--urls-file", required=True, help="Text file with one podcast page URL per line")
    parser.add_argument("--out-dir", required=True, help="Output directory for checkpoints and markdown")
    parser.add_argument(
        "--only-resolve",
        action="store_true",
        help="Resolve pages and write resolved.json without calling Deepgram",
    )
    args = parser.parse_args(argv)
    try:
        return run_pipeline(Path(args.urls_file), Path(args.out_dir), args.only_resolve)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
