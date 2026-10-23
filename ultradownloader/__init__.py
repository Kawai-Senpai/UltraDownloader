"""UltraDownloader's public YouTube API (dependencies load only when used)."""

from .youtube import BotCheckRequired, LocalYouTube, MediaError, NetworkPolicy, SubtitleUnavailable

__all__ = ["BotCheckRequired", "LocalYouTube", "MediaError", "NetworkPolicy", "SubtitleUnavailable"]
