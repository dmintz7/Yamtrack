import logging
import re
from datetime import datetime, timezone
from typing import Dict, List

import requests

from django.conf import settings
from django.core.cache import cache

from app import helpers
from app.models import MediaTypes, Sources
from app.providers import services

logger = logging.getLogger(__name__)

base_url = "https://skyhook.sonarr.tv/v1"
default_lang = settings.TVDB_LANG or "en"

_CAMEL_CACHE: Dict[str, str] = {}


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #

def handle_error(error):
    error_resp = error.response
    try:
        error_json = error_resp.json()
        details = error_json.get("message") or error_json.get("error")
    except Exception:
        details = "TVDB Mirror returned no JSON error body."

    if error_resp is not None and error_resp.status_code in (522, 525):
        logger.warning(
            "Skyhook returned HTTP %s. Treating as 429 for retry logic.",
            error_resp.status_code,
        )
        error_resp.status_code = 429

    raise services.ProviderAPIError(
        Sources.TVDB.value,
        error,
        details=details,
    )


# --------------------------------------------------------------------------- #
# Utilities
# --------------------------------------------------------------------------- #

def camel_to_snake(name: str) -> str:
    if name in _CAMEL_CACHE:
        return _CAMEL_CACHE[name]

    s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    s2 = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1)
    result = s2.lower()
    _CAMEL_CACHE[name] = result
    return result


def rename_keys(d: dict) -> dict:
    return {camel_to_snake(k): v for k, v in d.items()}


def get_readable_duration(duration):
    if not duration:
        return None
    hours, minutes = divmod(int(duration), 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def parse_episode_date(episode_date, return_utc=False):
    from dateutil.parser import parse
    from django.utils import timezone
    if not episode_date:
        return None

    parsed = parse(episode_date)

    # Ensure datetime is aware
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, settings.TIME_ZONE)

    if return_utc:
        return parsed

    return timezone.localtime(parsed)


def get_image(images, cover_type):
    if not images:
        return None
    for img in images:
        if img.get("coverType") == cover_type:
            return img.get("url")
    return None


def get_show_tvid_from_episode(episode_id):
    """
    Fetch TVDB series ID for a given episode ID, or None if not found.
    Caches the result for 24 hours.
    """
    cache_key = f"tvdb_episode_show_{episode_id}"
    if cached := cache.get(cache_key):
        return cached

    try:
        url = f"https://api.thetvdb.com/episodes/{episode_id}"
        resp = services.api_request(Sources.TVDB.value, "GET", url)

        if "Error" in resp:
            logger.warning(f"TVDB episode {episode_id} not found: {resp['Error']}")
            return None

        series_id = resp.get("data", {}).get("seriesId")
        if series_id:
            cache.set(cache_key, series_id, 24 * 60 * 60)
        return series_id

    except Exception as e:
        import sys
        logger.error(f"Error fetching TVDB show ID for episode {episode_id} (line {sys.exc_info()[-1].tb_lineno}): {type(e).__name__}: {e}")
        return None


def index_episodes(episodes: List[dict]):
    by_season: Dict[int, List[dict]] = {}
    for ep in episodes:
        sn = ep.get("seasonNumber")
        if sn is not None:
            by_season.setdefault(sn, []).append(ep)
    return by_season


# --------------------------------------------------------------------------- #
# Core processing
# --------------------------------------------------------------------------- #

def search(media_type, query, page):
    """Search for media on TVDB."""
    try:
        cache_key = f"search_{Sources.TVDB.value}_{media_type}_{query}_{page}"
        data = cache.get(cache_key)

        if data is not None:
            return data

        url = f"{base_url}/tvdb/search/en/?term={query}"

        try:
            response = services.api_request(
                Sources.TVDB.value,
                "GET",
                url,
            )
        except requests.exceptions.HTTPError as error:
            handle_error(error)
            return helpers.format_search_response(page, 20, 0, [])

        results = []
        for media in response:
            results.append({
                "media_id": media.get("tvdbId"),
                "source": Sources.TVDB.value,
                "media_type": media_type,
                "title": media.get("title"),
                "image": get_image(media.get("images"), "Poster"),
                "year": (
                    media.get("firstAired")[:4]
                    if media.get("firstAired")
                    else None
                ),
            })

        total_results = len(response)
        per_page = 20  # TVDB search behaves like TMDB here

        data = helpers.format_search_response(
            page,
            per_page,
            total_results,
            results,
        )

        cache.set(cache_key, data)
        return data

    except Exception:
        logger.exception("TVDB search failed")
        return helpers.format_search_response(page, 20, 0, [])


def process_tv(response):
    try:
        rating = response.get("rating") or {}
        seasons = response.get("seasons") or []
        episodes = response.get("episodes") or []

        now = datetime.now(timezone.utc)

        last_episode = None
        next_episode = None
        formatted_episodes = []

        for ep in episodes:
            air_dt = parse_episode_date(ep.get("airDateUtc"), return_utc=True)

            if air_dt:
                if air_dt <= now:
                    last_episode = ep
                elif not next_episode:
                    next_episode = ep

            f = rename_keys(ep)
            f["air_date"] = air_dt
            f["media_type"] = MediaTypes.EPISODE
            f["source"] = Sources.TVDB.value
            f["episode_id"] = ep.get("tvdbId")
            f["media_id"] = ep.get("tvdbShowId")
            formatted_episodes.append(f)

        response["_episodes_by_season"] = index_episodes(episodes)

        return {
            "media_id": response["tvdbId"],
            "slug": response["slug"],
            "source": Sources.TVDB.value,
            "source_url": f"https://thetvdb.com/series/{response['slug']}",
            "media_type": MediaTypes.TV.value,
            "title": response.get("title"),
            "max_progress": len(episodes),
            "image": get_image(response.get("images"), "Poster"),
            "synopsis": response.get("overview") or "No synopsis available.",
            "genres": response.get("genres"),
            "score": round(float(rating.get("value", 0)), 1),
            "score_count": int(rating.get("count", 0)),
            "details": {
                "format": "TV",
                "first_air_date": response.get("firstAired"),
                "last_air_date": response.get("lastAired"),
                "status": response.get("status"),
                "seasons": len(seasons),
                "episodes": len(episodes),
                "runtime": get_readable_duration(response.get("runtime")),
                "studios": response.get("network"),
                "country": get_country(response.get("originalCountry")),
                "languages": get_languages(response.get("language")),
            },
            "related": {
                "seasons": get_season_data(response),
                "episodes": formatted_episodes,
                "recommendations": [],
            },
            "tvdb_id": response["tvdbId"],
            "last_episode_season": last_episode["seasonNumber"] if last_episode else None,
            "next_episode_season": next_episode["seasonNumber"] if next_episode else None,
        }
    except Exception:
        logger.exception("process_tv failed")
        return None


def process_season(season_number, media_id):
    try:
        show_data = get_show_data(media_id)
        season_eps = [ ep for ep in show_data["episodes"] if ep['seasonNumber'] == season_number ]

        if not season_eps:
            return None

        season_eps = [rename_keys(ep) for ep in season_eps]
        season_eps.sort(key=lambda e: e.get("episode_number", 0))

        runtimes = [e.get("runtime") or 0 for e in season_eps]

        return {
            "source": Sources.TVDB.value,
            "media_type": MediaTypes.SEASON.value,
            "media_id": media_id,
            "season_number": season_number,
            "season_title": f"Season {season_number}",
            "image": get_image(show_data.get("images"), "Poster"),
            "synopsis": "No synopsis available.",
            "max_progress": season_eps[-1]["episode_number"],
            "details": {
                "episodes": len(season_eps),
                "runtime": get_readable_duration(sum(runtimes) / len(runtimes)),
                "total_runtime": get_readable_duration(sum(runtimes)),
            },
            "episodes": season_eps,
        }
    except Exception:
        logger.exception("process_season failed")
        return None


def process_episodes(season_metadata, episodes_in_db):
    """
    Process TVDB episodes for a season and merge with tracked history.
    """
    episodes_metadata = []

    # Index tracked episodes by episode_number
    tracked_episodes = {}
    for ep in episodes_in_db:
        episode_number = ep.item.episode_number
        tracked_episodes.setdefault(episode_number, []).append(ep)

    for episode in season_metadata.get("episodes", []):
        episode_number = episode.get("episode_number")

        # Normalize air_date (TVDB usually already parsed, but be safe)
        air_date = episode.get("air_date")
        if air_date and isinstance(air_date, str):
            try:
                from dateutil.parser import parse
                from django.utils import timezone

                parsed = parse(air_date)
                if timezone.is_naive(parsed):
                    parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
                air_date = parsed
            except Exception:
                pass

        episodes_metadata.append(
            {
                "media_id": season_metadata["media_id"],
                "media_type": MediaTypes.EPISODE.value,
                "source": Sources.TVDB.value,
                "season_number": season_metadata["season_number"],
                "episode_number": episode_number,
                "air_date": air_date,
                "image": episode.get("image"),  # TVDB already gives full URLs
                "title": episode.get("name"),
                "overview": episode.get("overview"),
                "history": tracked_episodes.get(episode_number, []),
                "runtime": get_readable_duration(episode.get("runtime")),
            }
        )

    return episodes_metadata


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #

def get_show_data(media_id):
    key = f"{Sources.TVDB.value}_RAW_SHOW_{media_id}"
    if cached := cache.get(key):
        return cached

    url = f"{base_url}/tvdb/shows/{default_lang}/{media_id}"
    try:
        response = services.api_request(Sources.TVDB.value, "GET", url)
        cache.set(key, response, 86400)
        return response
    except requests.exceptions.HTTPError as e:
        handle_error(e)
    except Exception:
        logger.exception("get_show_data failed")
        return None


def tv(media_id):
    proc_key = f"{Sources.TVDB.value}_PROC_SHOW_{media_id}"
    if cached := cache.get(proc_key):
        return cached

    data = process_tv(get_show_data(media_id))
    cache.set(proc_key, data, 86400)
    return data


def tv_with_seasons(media_id, season_numbers):
    tv_data = tv(media_id)
    if not season_numbers:
        return tv_data

    fetched = {}
    for n in season_numbers:
        key = f"{Sources.TVDB.value}_SEASON_{media_id}_{n}"
        season = cache.get(key)
        if not season:
            season = process_season(n, media_id)
            season = enrich_season_with_tv_data(season, tv_data, media_id, n)
            cache.set(key, season, 86400)
        fetched[f"season/{n}"] = season

    return tv_data | fetched


def get_season_data(data):
    """
    Build lightweight season metadata for a show.
    Uses pre-indexed episodes for O(1) season lookups.
    """
    try:
        seasons = data.get("seasons") or []
        episodes_by_season = data.get("_episodes_by_season") or {}

        show_media_id = data["tvdbId"]
        show_title = data["title"]
        fallback_image = get_image(data.get("images"), "Poster")

        final = []

        for season in seasons:
            season_number = season.get("seasonNumber")
            if season_number is None:
                continue

            season_eps = episodes_by_season.get(season_number, [])

            if season_eps:
                # episodes are not guaranteed sorted
                season_eps_sorted = sorted(
                    season_eps,
                    key=lambda e: e.get("episodeNumber") or 0
                )
                first_air = parse_episode_date(
                    season_eps_sorted[0].get("airDateUtc")
                )
                last_air = parse_episode_date(
                    season_eps_sorted[-1].get("airDateUtc")
                )
                episode_count = len(season_eps_sorted)
            else:
                first_air = last_air = None
                episode_count = 0

            season_image = (
                season.get("image")
                or get_image(season.get("images"), "Poster")
                or fallback_image
            )

            final.append({
                "source": Sources.TVDB.value,
                "media_type": MediaTypes.SEASON.value,
                "media_id": show_media_id,
                "title": show_title,
                "season_number": season_number,
                "season_title": f"Season {season_number}",
                "image": season_image,
                "first_air_date": first_air,
                "last_air_date": last_air,
                "max_progress": episode_count,
            })

        return final

    except Exception:
        logger.exception("get_season_data failed")
        return []


# --------------------------------------------------------------------------- #
# Metadata helpers
# --------------------------------------------------------------------------- #

def enrich_season_with_tv_data(season_data, tv_data, media_id, season_number):
    if not season_data:
        return None

    season_data.update({
        "media_id": media_id,
        "source_url": f"https://www.thetvdb.com/series/{tv_data['slug']}/seasons/official/{season_number}",
        "title": tv_data["title"],
        "tvdb_id": tv_data["tvdb_id"],
        "genres": tv_data["genres"],
    })

    if season_data.get("synopsis") == "No synopsis available.":
        season_data["synopsis"] = tv_data["synopsis"]

    if not season_data.get("image"):
        season_data["image"] = tv_data.get("image")

    return season_data


# --------------------------------------------------------------------------- #
# Countries / languages
# --------------------------------------------------------------------------- #

def get_country(code: str):
    if not code:
        return None
    try:
        import pycountry
        country = pycountry.countries.get(alpha_3=code.upper())
        return country.name if country else code
    except ImportError:
        return code


def get_languages(code: str):
    if not code:
        return None
    try:
        import pycountry
        lang = pycountry.languages.get(alpha_3=code.lower())
        return [lang.name] if lang else [code]
    except ImportError:
        return [code]
