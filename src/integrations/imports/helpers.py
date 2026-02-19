import base64
import datetime
import hashlib
import json
import logging
import time

import requests
from dateutil.parser import parse as parse_date
from collections import defaultdict

from cryptography.fernet import Fernet
from django.apps import apps
from django.conf import settings
from django.contrib import messages
from django.core.cache import cache
from django.db.utils import OperationalError
from django.utils import timezone
from django_celery_beat.models import CrontabSchedule, PeriodicTask
from simple_history.utils import bulk_create_with_history

import app
from app.models import MediaTypes, ExternalID, MetadataSources
from app.providers import tmdb, services
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
    def __init__(self, trakt_data=None, trakt_class=None, history_record=None):
        """
        trakt_data: dict (from Trakt)
        trakt_class: optional Trakt API client
        history_record: optional Plex EpisodeHistory object
        """
        self.trakt = trakt_class
        self.trakt_data = trakt_data
        self.history = history_record

        # Extract IDs safely
        if trakt_data:
            self.trakt_episode_id = trakt_data.get("episode", {}).get("ids", {}).get("trakt")
            self.tvdb_episode_id = trakt_data.get("episode", {}).get("ids", {}).get("tvdb")
            self.tmdb_show_id = trakt_data.get("show", {}).get("ids", {}).get("tmdb")
            self.episode_title = trakt_data.get("episode", {}).get("title")
        elif history_record:
            self.trakt_episode_id = self.history.ids.get("trakt")
            self.tvdb_episode_id = self.history.ids.get("tvdb")
            self.tmdb_show_id = self.history.show_ids.get("tmdb")
            self.episode_title = getattr(history_record, "title", None)
            self.season_number = getattr(history_record, "seasonNumber", None) or getattr(history_record, "parentIndex", None)
            self.episode_number = getattr(history_record, "episodeNumber", None) or getattr(history_record, "index", None)
        else:
            self.trakt_episode_id = None
            self.tvdb_episode_id = None
            self.tmdb_show_id = None
            self.episode_title = None

        self.show_data = None

    def resolve(self):
        """
        Resolve the episode using airdate, TMDb show data, or Plex history record.
        """
        episode_airdate = None
        if self.tvdb_episode_id:
            episode_airdate = self._fetch_tvdb_airdate()
        if self.trakt_episode_id and not episode_airdate:
            episode_airdate = self._fetch_trakt_airdate()

        if self.history:
            originally_available = getattr(self.history.plex_obj, "originallyAvailableAt", None)
            if originally_available:
                episode_airdate = originally_available.date()

        # --- Fetch TMDb show data ---
        if self.tmdb_show_id:
            self.show_data = self._fetch_show_data()

        else:
            logger.warning("No TMDb show ID or history record")
            return None

        season_number = self._match_season(episode_airdate)
        if not season_number:
            logger.warning("No matching season found for airdate %s", episode_airdate)
            return None

        matched_episode = self._match_episode(season_number, airdate=episode_airdate, title=self.episode_title)
        if matched_episode:
            logger.info(f"Matched episode: Season {season_number} Episode {matched_episode.get('episode_number')} ({matched_episode.get('name')})")
            return matched_episode

        logger.warning(f"No matching episode found for Season {season_number} - {self.episode_title}")
        return None

    def _fetch_trakt_airdate(self):
        if not self.trakt_episode_id or not self.trakt:
            return None
        cache_key = f"trakt_episode_airdate_{self.trakt_episode_id}"
        try:
            data = cache.get(cache_key)
            if not data and self.trakt:
                data = self.trakt._make_api_request(f"{self.trakt.base_url}/episodes/{self.trakt_episode_id}?extended=full")
                cache.set(cache_key, data)
        except Exception:
            cache.set(cache_key, None)
            return None

        first_aired = data.get("first_aired")
        if not first_aired:
            return None

        airdate = parse_date(first_aired)
        return airdate.date() if airdate else None

    def _fetch_tvdb_airdate(self):
        if not self.tvdb_episode_id:
            return None
        cache_key = f"tvdb_episode_airdate_{self.tvdb_episode_id}"
        try:
            data = cache.get(cache_key)
            if not data:
                import requests
                resp = requests.get(f"https://api.thetvdb.com/episodes/{self.tvdb_episode_id}")
                if resp.status_code != 200:
                    cache.set(cache_key, None)
                    return None
                data = resp.json().get("data")
                cache.set(cache_key, data)
        except Exception:
            cache.set(cache_key, None)
            return None

        if not data:
            return None

        first_aired = data.get("firstAired")
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
        episodes = resp.get(f"season/{season_number}", {}).get("episodes", [])
        return episodes

    def _match_season(self, episode_airdate):
        seasons = self.show_data.get("related", {}).get("seasons", [])
        for season in reversed(seasons):
            season_number = season.get("season_number")
            first_air = season.get("first_air_date")
            if not first_air:
                continue

            first_air_date = parse_date(first_air) if isinstance(first_air, str) else first_air
            if not first_air_date:
                continue

            if first_air_date.date() <= episode_airdate:
                return season_number

        return None

    def _match_episode(self, season_number, airdate=None, title=None):
        episodes = self._fetch_season_episodes(season_number)
        if airdate:
            for ep in episodes:
                if ep.get("air_date") and parse_date(ep["air_date"]).date() == airdate:
                    return ep
        if title:
            title_norm = title.lower()
            for ep in episodes:
                if ep.get("name", "").lower() == title_norm:
                    return ep
        return None


def bulk_create_unresolved(unresolved, batch_size=500):
    """Bulk create UnresolvedImport objects with deduplication."""
    if not unresolved:
        logger.info("No unresolved imports to create, nothing to do.")
        return

    logger.info(f"Processing {len(unresolved)} unresolved entries...")
    seen = set()
    unique_unresolved = []
    for entry in unresolved:
        key = (entry.user_id, entry.metadata_source, entry.metadata_source_identifier, entry.media_type)
        if key in seen:
            continue
        seen.add(key)
        unique_unresolved.append(entry)

    keys_to_check = [
        (u.user_id, u.metadata_source, u.metadata_source_identifier, u.media_type)
        for u in unique_unresolved
    ]

    existing_keys = set(
        UnresolvedImport.objects.filter(
            user_id__in={k[0] for k in keys_to_check},
            metadata_source__in={k[1] for k in keys_to_check},
            media_type__in={k[3] for k in keys_to_check},
        ).values_list(
            "user_id",
            "metadata_source",
            "metadata_source_identifier",
            "media_type",
        )
    )

    to_create = []
    conflicts = []

    for entry in unique_unresolved:
        key = (entry.user_id, entry.metadata_source, entry.metadata_source_identifier, entry.media_type)
        if key in existing_keys:
            conflicts.append(entry)
        else:
            to_create.append(entry)

    logger.info(f"Detected {len(conflicts)} conflicts before insert.")

    # Insert non-conflicting entries in bulk
    if to_create:
        retry_on_lock(
            lambda: UnresolvedImport.objects.bulk_create(
                to_create,
                batch_size=batch_size,
            )
        )

    logger.info(f"Finished creating {len(to_create)} new unresolved entries in batches of {batch_size}.")


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

def bulk_create_external_ids(external_ids, batch_size=1000):
    """Bulk create ExternalID objects with deduplication."""
    if not external_ids:
        return

    # Deduplicate by (item_id, source, source_id)
    seen = set()
    unique_external_ids = []
    for ext in external_ids:
        key = (ext.item_id, ext.metadata_source.value, ext.metadata_source_identifier)
        if key in seen:
            continue
        seen.add(key)
        unique_external_ids.append(ext)

    retry_on_lock(
        lambda: ExternalID.objects.bulk_create(
            unique_external_ids,
            batch_size=batch_size,
            ignore_conflicts=True,
        )
    )

def get_metadata_by_priority(media_type, source_ids: dict, title, season_number=None, source_found=None):
    """
    Try metadata providers in priority order.
    source_ids = {"tmdb": "123", "tvdb": "456"}
    """

    from app.config import get_property
    for source in get_property(media_type, "sources"):
        if source_found is not None and source_found != source.value:
            continue
        source_key = source.value
        source_id = source_ids.get(source_key)
        if not source_id:
            continue

        metadata = get_metadata(media_type, source_key, source_id, title, season_number)
        if metadata:
            return metadata, source, source_id

    return None, None,None


def get_metadata(media_type, source_key, source_id, title, season_number=None):
    """Get metadata for a media item."""
    try:
        kwargs = {}
        if season_number is not None:
            kwargs["season_numbers"] = [season_number]

        return services.get_media_metadata(
            media_type,
            source_id,
            source_key,
            **kwargs,
        )
    except KeyError as e:
        if 'season/' in e.args[0]:
            logger.debug(f"Ignoring unknown season {e.args[0]} for {source_key} {source_id} {title}")
            return None
        raise
    except services.ProviderAPIError as error:
        if error.status_code == requests.codes.not_found:
            return None
        raise


def queue_external_ids(external_ids, ids_dict, item):
    """Queue ExternalID objects for bulk creation with error handling."""
    valid_sources = {choice.value for choice in MetadataSources}

    for source_key, source_id in ids_dict.items():
        try:
            if source_key not in valid_sources or not source_id:
                continue

            # Convert source to enum safely
            source_enum = MetadataSources(source_key)

            external_ids.append(
                ExternalID(
                    item=item,
                    metadata_source=source_enum,
                    metadata_source_identifier=str(source_id),
                )
            )
        except ValueError:
            logger.warning(f"Skipping invalid external metadata source '{source_key}' for item {getattr(item, 'id', '<unknown>')}")
        except Exception as e:
            logger.error(f"Failed to queue external ID {source_key}:{source_id} for item {getattr(item, 'id', '<unknown>')}: {e}")


def get_or_create_item(
    media_type,
    source_key,
    source_id,
    metadata,
    season_number=None,
    episode_number=None,
):
    source_enum = MetadataSources(source_key)
    ext_qs = ExternalID.objects.select_related("item").filter(metadata_source=source_enum, metadata_source_identifier=str(source_id), item__media_type=media_type,)
    if ext_qs:
        item = ext_qs.first().item
    else:
        item_kwargs = {
            "media_id": source_id,
            "source": source_key,
            "media_type": media_type,
        }

        if season_number is not None:
            item_kwargs["season_number"] = season_number

        if episode_number is not None:
            item_kwargs["episode_number"] = episode_number

        defaults = {
            "title": metadata["title"],
            "image": metadata["image"],
        }

        item, _ = app.models.Item.objects.get_or_create(
            **item_kwargs,
            defaults=defaults,
        )

    return item
