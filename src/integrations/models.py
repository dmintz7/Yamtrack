"""Models for integration data."""

from django.conf import settings
from django.db import models
from django.utils import timezone
from app.models import MediaTypes, Item, Movie, Episode


class PlexAccount(models.Model):
    """Store Plex authentication and cached library data for a user."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="plex_account",
    )
    plex_token = models.CharField(max_length=255)
    plex_username = models.CharField(max_length=255)
    plex_account_id = models.CharField(max_length=255, blank=True, null=True)
    server_name = models.CharField(max_length=255, blank=True, null=True)
    machine_identifier = models.CharField(max_length=255, blank=True, null=True)
    sections = models.JSONField(default=list, blank=True)
    sections_refreshed_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Model options."""

        verbose_name = "Plex account"
        verbose_name_plural = "Plex accounts"

    def __str__(self):
        """Readable representation."""
        return f"PlexAccount({self.plex_username})"

    @property
    def is_connected(self):
        """Return True when we have a token stored."""
        return bool(self.plex_token)


class PocketCastsAccount(models.Model):
    """Store Pocket Casts authentication tokens for a user."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="pocketcasts_account",
    )
    access_token = models.TextField(
        blank=True,
        null=True,
        help_text="Encrypted JWT access token (cached from login)",
    )
    refresh_token = models.TextField(
        blank=True,
        null=True,
        help_text="Encrypted refresh token (cached from login)",
    )
    email = models.TextField(
        blank=True,
        null=True,
        help_text="Encrypted email address for login",
    )
    password = models.TextField(
        blank=True,
        null=True,
        help_text="Encrypted password for login",
    )
    token_expires_at = models.DateTimeField(null=True, blank=True)
    last_sync_at = models.DateTimeField(null=True, blank=True)
    connection_broken = models.BooleanField(
        default=False,
        help_text="True if connection is broken (refresh failed) but credentials are preserved",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Model options."""

        verbose_name = "Pocket Casts account"
        verbose_name_plural = "Pocket Casts accounts"

    def __str__(self):
        """Readable representation."""
        return f"PocketCastsAccount({self.user.username})"

    @property
    def is_connected(self):
        """Return True when we have a valid connection.
        
        A connection is valid if:
        - We have email AND password (can always re-login), OR
        - We have an access token (and it's not expired, or we have refresh token to renew it)
        - Connection is not marked as broken
        """
        # If we have credentials (email and password), we can always reconnect
        has_credentials = bool(self.email and self.password)

        # If connection is marked as broken and we don't have credentials, not connected
        if self.connection_broken and not has_credentials:
            return False

        # If we have credentials, we're connected (can always re-login)
        if has_credentials:
            return True

        # Legacy: check for access token
        if not self.access_token:
            return False

        # If connection is marked as broken, not connected
        if self.connection_broken:
            return False

        # If token is not expired, we're connected
        if not self.is_token_expired:
            return True

        # If token is expired but we have a refresh token, we can still refresh
        if self.refresh_token:
            return True

        # Token is expired and no refresh token - not connected
        return False

    @property
    def is_token_expired(self):
        """Return True if the token is expired."""
        if not self.token_expires_at:
            return False
        return timezone.now() >= self.token_expires_at


class LastFMAccount(models.Model):
    """Store Last.fm username and sync state for a user."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="lastfm_account",
    )
    lastfm_username = models.CharField(max_length=255)
    last_fetch_timestamp_uts = models.IntegerField(
        null=True,
        blank=True,
        help_text="Unix timestamp (seconds) of last successful poll",
    )
    last_sync_at = models.DateTimeField(null=True, blank=True)
    connection_broken = models.BooleanField(
        default=False,
        help_text="True if connection is broken (invalid username or persistent errors)",
    )
    failure_count = models.IntegerField(
        default=0,
        help_text="Number of consecutive failures",
    )
    last_error_code = models.CharField(
        max_length=10,
        blank=True,
        help_text="Last.fm API error code (e.g., '29' for rate limit)",
    )
    last_error_message = models.TextField(
        blank=True,
        help_text="Human-readable error message",
    )
    last_failed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Model options."""

        verbose_name = "Last.fm account"
        verbose_name_plural = "Last.fm accounts"

    def __str__(self):
        """Readable representation."""
        return f"LastFMAccount({self.lastfm_username})"

    @property
    def is_connected(self):
        """Return True when we have a valid connection."""
        return bool(self.lastfm_username) and not self.connection_broken


class TraktAccount(models.Model):
    """Store Trakt API client credentials for a user."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="trakt_account",
    )
    client_id = models.TextField(
        blank=True,
        null=True,
        help_text="Encrypted Trakt client ID",
    )
    client_secret = models.TextField(
        blank=True,
        null=True,
        help_text="Encrypted Trakt client secret",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Model options."""

        verbose_name = "Trakt account"
        verbose_name_plural = "Trakt accounts"

    def __str__(self):
        """Readable representation."""
        return f"TraktAccount({self.user.username})"

    @property
    def is_configured(self):
        """Return True when client credentials are stored."""
        return bool(self.client_id and self.client_secret)


class UnresolvedImport(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="unresolved_media",)
    metadata_source = models.CharField(max_length=20)
    metadata_source_identifier = models.CharField(max_length=128)
    media_type = models.CharField(max_length=20, choices=MediaTypes.choices)
    raw_data = models.JSONField(null=True, blank=True)
    found_metadata_source = models.CharField(max_length=128, blank=True, null=True)
    found_metadata_source_identifier = models.CharField(max_length=128, blank=True, null=True)


from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import models
from django.conf import settings


class PlexHistory(models.Model):
    """Store Plex watch history entries, matching plex-trakt.plex_views."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="plex_history", db_index=True)
    item = models.ForeignKey("app.Item", on_delete=models.CASCADE, blank=True, null=True, related_name="plex_history", db_index=True)
    plex_id = models.IntegerField()
    plex_history_id = models.IntegerField(unique=True, db_index=True)
    viewed_at = models.DateTimeField(db_index=True)
    device_id = models.IntegerField(blank=True, null=True)
    archived = models.BooleanField(default=False)
    matched_media_id = models.PositiveIntegerField(null=True, blank=True, db_index=True)
    matched_media_type = models.ForeignKey(ContentType, on_delete=models.CASCADE, null=True, blank=True, db_index=True, limit_choices_to=models.Q(app_label='app', model__in=['movie', 'episode']))
    matched_media = GenericForeignKey('matched_media_type', 'matched_media_id')

    def clean(self):
        """Ensure matched_media type matches item.media_type"""
        if not self.matched_media or not self.item:
            return

        expected_type = self.item.media_type
        actual_type = None

        if isinstance(self.matched_media, Movie):
            actual_type = MediaTypes.MOVIE.value
        elif isinstance(self.matched_media, Episode):
            actual_type = MediaTypes.EPISODE.value

        if actual_type != expected_type:
            raise ValidationError(f"Media type mismatch: item={expected_type}, matched={actual_type}")

    def save(self, *args, validate=True, **kwargs):
        if validate:
            self.full_clean()
        super().save(*args, **kwargs)

    class Meta:
        ordering = ["-viewed_at"]

        indexes = [
            models.Index(fields=["matched_media_id"]),
            models.Index(fields=["item", "viewed_at"]),
            models.Index(fields=["user", "viewed_at"]),
            models.Index(fields=["matched_media_type", "matched_media_id"]),
        ]

    def __str__(self):
        return f"{self.item} ({self.viewed_at})"
