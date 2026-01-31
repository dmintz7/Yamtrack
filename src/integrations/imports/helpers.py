import base64
import datetime
import hashlib
import json
import logging
import time
from dateutil.parser import parse as parse_date
from collections import defaultdict

from cryptography.fernet import Fernet
from django.apps import apps
from django.conf import settings
from django.contrib import messages
from django.db.utils import OperationalError
from django.utils import timezone
from django_celery_beat.models import CrontabSchedule, PeriodicTask
from simple_history.utils import bulk_create_with_history

import app
from app.models import MediaTypes
from app.providers import tmdb
from app.providers import services
from app.models import Sources
from integrations.models import UnresolvedImport

logger = logging.getLogger(__name__)


class MediaImportError(Exception):
    """Custom exception for import errors."""


class MediaImportUnexpectedError(Exception):
    """Custom exception for unexpected import errors."""


LOCK_ERROR_SIGNALS = (
    "database is locked",
    "database table is locked",
    "database file is locked",
)

DISK_IO_ERROR_SIGNALS = (
    "disk i/o error",
    "disk i/o",
    "i/o error",
    "unable to open database file",
    "readonly database",
)


def is_lock_error(error):
    """Return True if the OperationalError was caused by a SQLite lock."""
    message = str(error).lower()
    return any(signal in message for signal in LOCK_ERROR_SIGNALS)


def is_disk_io_error(error):
    """Return True if the OperationalError was caused by a disk I/O error."""
    message = str(error).lower()
    return any(signal in message for signal in DISK_IO_ERROR_SIGNALS)


def is_retryable_error(error):
    """Return True if the OperationalError is retryable (lock or disk I/O)."""
    return is_lock_error(error) or is_disk_io_error(error)


def retry_on_lock(func, max_retries=5, base_delay=0.1, backoff=2.0):
    """Retry the callable when SQLite reports a lock or disk I/O error."""
    attempt = 0

    while True:
        try:
            return func()
        except OperationalError as error:
            if not is_retryable_error(error) or attempt >= max_retries:
                raise

            error_type = "disk I/O" if is_disk_io_error(error) else "lock"
            sleep_for = base_delay * (backoff**attempt)
            logger.warning(
                "Retrying database operation due to %s error (attempt %s/%s, sleeping %.2fs)",
                error_type,
                attempt + 1,
                max_retries,
                sleep_for,
            )
            time.sleep(sleep_for)
            attempt += 1


def get_existing_media(user):
    """Get all existing media for the user to check against during import."""
    excluded_types = [MediaTypes.SEASON.value, MediaTypes.EPISODE.value]
    valid_types = [value for value in MediaTypes.values if value not in excluded_types]
    existing = defaultdict(lambda: defaultdict(dict))

    for media_type in valid_types:
        media_model = apps.get_model(app_label="app", model_name=media_type)

        for media in media_model.objects.filter(user=user).select_related("item"):
            existing[media_type][media.item.source][media.item.media_id] = media

    counts = [
        f"{media_type}: {sum(len(source_dict) for source_dict in media_dict.values())}"
        for media_type, media_dict in existing.items()
    ]
    logger.debug("Existing media for user %s: %s", user.username, ", ".join(counts))
    return existing


def should_process_media(existing_media, to_delete, media_type, source, media_id, mode):
    """Determine if a media item should be processed based on mode."""
    exists = media_id in existing_media[media_type][source]

    if mode == "new" and exists:
        # In "new" mode, skip if media already exists
        logger.debug(
            "Skipping existing %s: %s (mode: new)",
            media_type,
            media_id,
        )
        return False

    if mode == "overwrite" and exists:
        # In "overwrite" mode, add to the deletion list
        logger.debug(
            "Adding existing %s to deletion list: %s (mode: overwrite)",
            media_type,
            media_id,
        )
        to_delete[media_type][source].add(media_id)

    return True


def cleanup_existing_media(to_delete, user):
    """Delete existing media if in overwrite mode."""
    for media_type, sources in to_delete.items():
        if not sources:
            continue

        model = apps.get_model(app_label="app", model_name=media_type)
        total_deleted = 0

        for source, media_ids in sources.items():
            if not media_ids:
                continue

            deleted_count, _ = retry_on_lock(
                lambda: model.objects.filter(
                    item__media_id__in=media_ids,
                    item__source=source,
                    user=user,
                ).delete(),
            )
            total_deleted += deleted_count

        if total_deleted > 0:
            logger.info(
                "Deleted %s %s objects for user %s in overwrite mode",
                total_deleted,
                media_type,
                user,
            )


def update_season_references(seasons, user):
    """Update season references with actual TV instances.

    When bulk_create skips existing TV shows, seasons would still reference
    the unsaved TV instances. This updates those references to point to
    the existing TV shows in the database, preventing the ValueError about
    unsaved related objects during bulk creation of seasons.
    """
    # Get existing TV shows from database
    existing_tv = {
        tv.item.media_id: tv
        for tv in app.models.TV.objects.filter(
            user=user,
            item__media_id__in=[season.item.media_id for season in seasons],
        )
    }

    # Update references
    for season in seasons:
        media_id = season.item.media_id
        if media_id in existing_tv:
            season.related_tv = existing_tv[media_id]
            logger.debug(
                "Updated new season %s with existing TV %s",
                season,
                existing_tv[media_id],
            )


def update_episode_references(episodes, user):
    """Update episode references with actual Season instances.

    When bulk_create skips existing seasons, episodes would still reference
    the unsaved season instances. This updates those references to point to
    the existing seasons in the database, preventing the ValueError about
    unsaved related objects during bulk creation of episodes.
    """
    # Create mapping of season instances
    existing_seasons = {
        (season.item.media_id, season.item.season_number): season
        for season in app.models.Season.objects.filter(
            user=user,
            item__media_id__in={episode.item.media_id for episode in episodes},
        )
    }

    # Update references
    for episode in episodes:
        season_key = (
            episode.item.media_id,
            episode.item.season_number,
        )
        if season_key in existing_seasons:
            episode.related_season = existing_seasons[season_key]
            logger.debug(
                "Updated new episode %s with existing season %s",
                episode,
                existing_seasons[season_key],
            )


def bulk_create_media(bulk_media_list, user):
    """Bulk create all media objects."""
    for media_type, bulk_media in bulk_media_list.items():
        if not bulk_media:
            continue

        model = apps.get_model(app_label="app", model_name=media_type)

        logger.info("Bulk importing %s", media_type)

        # Update references for seasons and episodes
        if media_type == MediaTypes.SEASON.value:
            logger.info("Updating references for season to existing TV shows")
            update_season_references(bulk_media, user)
        elif media_type == MediaTypes.EPISODE.value:
            logger.info(
                "Updating references for episodes to existing TV seasons",
            )
            update_episode_references(bulk_media, user)

        retry_on_lock(
            lambda: bulk_create_with_history(
                bulk_media,
                model,
                batch_size=500,
                default_user=user,
            ),
        )


def create_import_schedule(
    username,
    request,
    mode,
    frequency,
    import_time,
    source,
    token=None,
    extra_kwargs=None,
):
    """Create an import schedule.

    extra_kwargs: Optional dictionary of additional task kwargs to persist.
    """
    try:
        import_time = (
            datetime.datetime.strptime(import_time, "%H:%M")
            .astimezone(
                timezone.get_default_timezone(),
            )
            .time()
        )
    except ValueError:
        messages.error(request, "Invalid import time.")
        return

    task_name = f"Import from {source} for {username} at {import_time} {frequency}"
    if PeriodicTask.objects.filter(name=task_name).exists():
        messages.error(
            request,
            "The same import task is already scheduled.",
        )
        return

    crontab, _ = CrontabSchedule.objects.get_or_create(
        hour=import_time.hour,
        minute=import_time.minute,
        day_of_week="*" if frequency == "daily" else "*/2",
        timezone=timezone.get_default_timezone(),
    )

    kwargs = {
        "username": username,
        "user_id": request.user.id,
        "mode": mode,
    }

    if token:
        kwargs["token"] = token
    if extra_kwargs:
        kwargs.update(extra_kwargs)

    # Create new periodic task
    PeriodicTask.objects.create(
        name=task_name,
        task=f"Import from {source}",
        crontab=crontab,
        kwargs=json.dumps(kwargs),
        start_time=timezone.now(),
    )
    messages.success(request, f"{source} import task scheduled.")


def join_with_commas_and(items):
    """Join a list of items with commas and 'and'."""
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def fernet():
    """Derive a stable 32-byte key from Django's SECRET_KEY.

    Uses SHA-256 then urlsafe_b64encode to satisfy Fernet.
    """
    digest = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(value):
    """Return url-safe encrypted string."""
    return fernet().encrypt(value.encode()).decode()


def decrypt(token):
    """Decrypt value that was encrypted with `encrypt`."""
    return fernet().decrypt(token.encode()).decode()


class TMDBResolver:
    def __init__(self, trakt_data, trakt_class):
        self.trakt = trakt_class
        self.trakt_data = trakt_data
        self.trakt_episode_id = trakt_data["episode"]["ids"]["trakt"]
        self.tmdb_show_id = trakt_data["show"]["ids"]["tmdb"]
        self.episode_title = trakt_data["episode"]["title"]
        self.show_data = None

    def resolve(self):
        if not self.trakt_episode_id or not self.tmdb_show_id:
            logger.warning("Missing TVDB episode ID or TMDB show ID")
            return None

        episode_airdate = self._fetch_trakt_airdate()
        if not episode_airdate:
            logger.warning("Failed to fetch episode airdate from Trakt")
            return None

        self.show_data = self._fetch_show_data()
        if not self.show_data:
            logger.warning("Failed to fetch show data from TMDB")
            return None

        season_number = self._match_season(episode_airdate)
        if not season_number:
            logger.warning("No matching season found for airdate %s", episode_airdate)
            return None

        matched_episode = self._match_episode(season_number, airdate=episode_airdate)
        if matched_episode:
            logger.info(f"Matched episode: Season {season_number} Episode {matched_episode.get('episode_number')} ({matched_episode.get('name')})")
            return matched_episode

        logger.warning("No matching episode found for airdate/title")
        return None

    def _fetch_trakt_airdate(self):
        """Fetch the airdate of an episode from Trakt using the episode ID."""
        try:
            data = self.trakt._make_api_request(f"{self.trakt.base_url}/episodes/{self.trakt_episode_id}?extended=full")
        except Exception:
            return None

        first_aired = data.get("first_aired")
        if not first_aired:
            return None

        airdate = parse_date(first_aired)
        return airdate.date() if airdate else None

    def _fetch_show_data(self):
        logger.debug("Fetching TMDB show data for show ID %s", self.tmdb_show_id)
        return tmdb.tv(self.tmdb_show_id)

    def _fetch_season_episodes(self, season_number):
        logger.debug("Fetching episodes for season %s of show %s", season_number, self.tmdb_show_id)
        resp = tmdb.tv_with_seasons(self.tmdb_show_id, [season_number])
        episodes = resp.get(f"season/{season_number}", []).get("episodes", [])
        return episodes

    def _match_season(self, episode_airdate):
        seasons = self.show_data.get("related", []).get("seasons", [])
        for season in reversed(seasons):
            season_number = season["season_number"]
            first_air = season.get("first_air_date")
            if not first_air:
                continue
            first_air_date = first_air.date()
            if first_air_date <= episode_airdate:
                logger.debug("Matched season %s (first air date %s)", season_number, first_air_date)
                return season_number
        return None

    def _match_episode(self, season_number, airdate=None, title=None):
        episodes = self._fetch_season_episodes(season_number)

        if airdate:
            for ep in episodes:
                if ep.get("air_date") and parse_date(ep["air_date"]).date() == airdate:
                    logger.debug("Found episode by airdate: %s", ep.get("name"))
                    return ep

        if title:
            title_norm = title.lower()
            for ep in episodes:
                ep_title = ep.get("name", "").lower()
                if ep_title == title_norm:
                    logger.debug("Found episode by title: %s", ep.get("name"))
                    return ep

        logger.warning("No episode matched for season %s", season_number)
        return None
      
def bulk_create_unresolved(unresolved, batch_size=500):
    """Bulk create UnresolvedImport objects with deduplication."""
    if not unresolved:
        return

    seen = set()
    unique_unresolved = []
    for entry in unresolved:
        key = (entry.user_id, entry.metadata_source, entry.metadata_source_identifier, entry.media_type)
        if key in seen:
            continue
        seen.add(key)
        unique_unresolved.append(entry)

    retry_on_lock(
        lambda: UnresolvedImport.objects.bulk_create(
            unique_unresolved,
            batch_size=batch_size,
            ignore_conflicts=True,
        )
    )


def initiate_unresolved_import(user):
    unresolved_entries = UnresolvedImport.objects.filter(user=user, found_metadata_source__isnull=False, found_metadata_source_identifier__isnull=False)
    logger.info("Found %d unresolved entries for user %s", unresolved_entries.count(), user.username)

    warnings = []
    import_counts = {}  # flat counts per media type

    for unresolved in unresolved_entries:
        data = unresolved.raw_data
        media_type = unresolved.media_type
        found_metadata_source = unresolved.found_metadata_source
        found_metadata_source_identifier = unresolved.found_metadata_source_identifier

        # Initialize count for this media type
        import_counts.setdefault(media_type, 0)

        # Fix source ID mapping
        if media_type == MediaTypes.MOVIE.value:
            data.setdefault("movie", {}).setdefault("ids", {})[found_metadata_source] = str(found_metadata_source_identifier)
        elif media_type == MediaTypes.TV.value:
            data.setdefault("show", {}).setdefault("ids", {})[found_metadata_source] = str(found_metadata_source_identifier)
        elif media_type == MediaTypes.EPISODE.value:
            data.setdefault("episode", {}).setdefault("ids", {})[found_metadata_source] = str(found_metadata_source_identifier)
        else:
            warnings.append(f"Unrecognized media type for unresolved import: {unresolved}")
            continue

        try:
            if process_unresolved_import_entry(media_type, user, unresolved.metadata_source, data, found_metadata_source):
                unresolved.delete()
                import_counts[media_type] += 1  # flat count for successful imports
                logger.info("Successfully processed unresolved media: %s", unresolved)
            else:
                warnings.append(f"Failed to process unresolved media: {unresolved}")
        except Exception as e:
            warnings.append(f"Error processing {unresolved}: {e}")
            logger.exception("Failed to reprocess %s", unresolved)

    return import_counts, warnings


def process_unresolved_import_entry(media_type, user, source, data, matched_source):
    from integrations.imports.trakt import TraktImporter
    """
    Dispatches unresolved media to the appropriate importer.
    Returns True if successfully processed.
    """
    importers = {
       "trakt": TraktImporter,
    }

    if source not in importers:
        logger.error("No importer available for source: %s", source)
        return False

    importer_class = importers[source]
    importer = importer_class(user, user, mode="all")

    try:
        if source == "trakt":
            if media_type == MediaTypes.MOVIE.value:
                importer.process_watched_movie(data)
            elif media_type in (MediaTypes.TV.value, MediaTypes.EPISODE.value):
                logger.info((data, matched_source))
                importer.process_watched_episode(data)
            else:
                return False
        else:
            return False
        if importer.bulk_media:
            bulk_create_media(importer.bulk_media, user)
            importer.bulk_media = defaultdict(list)
            return True
        else:
            return False
    except Exception as e:
        logger.exception("Error processing media via importer %s: %s", importer_class.__name__, e)
        return False
