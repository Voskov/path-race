"""Runtime configuration — all tunables exposed via environment variables."""
import os


def _int(name, default):
    return int(os.environ.get(name, default))


def _float(name, default):
    return float(os.environ.get(name, default))


class Settings:
    # Deployment
    PATH_PREFIX = os.environ.get("PATH_PREFIX", "race-e97f41eb35828b03").strip("/")
    PORT = _int("PORT", 8000)
    DB_PATH = os.environ.get("DB_PATH", os.path.join("data", "pathrace.db"))

    # Tap UX
    DOUBLE_TAP_THRESHOLD_S = _int("DOUBLE_TAP_THRESHOLD_S", 7)
    UNDO_TOAST_MS = _int("UNDO_TOAST_MS", 5000)

    # Location filter
    LOCATION_MAX_ACCURACY_M = _float("LOCATION_MAX_ACCURACY_M", 75.0)  # worse => filter off
    LOCATION_STALE_MS = _int("LOCATION_STALE_MS", 30000)               # older fix => filter off
    LOCATION_FOLD_SIZE = _int("LOCATION_FOLD_SIZE", 3)                 # options kept above the fold

    # Stats — time-of-day split boundaries (local wall-clock, HH:MM)
    TOD_MORNING_BOUNDARY = os.environ.get("TOD_MORNING_BOUNDARY", "08:30")
    TOD_EVENING_BOUNDARY = os.environ.get("TOD_EVENING_BOUNDARY", "18:00")

    @property
    def prefix(self) -> str:
        # empty PATH_PREFIX => serve at the domain root
        return f"/{self.PATH_PREFIX}" if self.PATH_PREFIX else ""

    def client_config(self) -> dict:
        """Subset shipped to the browser."""
        return {
            "prefix": self.prefix or "/",
            "doubleTapThresholdMs": self.DOUBLE_TAP_THRESHOLD_S * 1000,
            "undoToastMs": self.UNDO_TOAST_MS,
            "locationMaxAccuracyM": self.LOCATION_MAX_ACCURACY_M,
            "locationStaleMs": self.LOCATION_STALE_MS,
            "locationFoldSize": self.LOCATION_FOLD_SIZE,
        }


settings = Settings()
