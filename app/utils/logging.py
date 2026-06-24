import logging
import threading
from logging.handlers import RotatingFileHandler

from app.config import get_settings

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Tag our handlers so we can detect them and stay idempotent even across
# processes/reloads that share a logger registry.
_HANDLER_FLAG = "_aita_handler"

_configured = False
_lock = threading.Lock()


def setup_logging() -> None:
    # Double-checked locking: the app runs an APScheduler thread pool, so
    # get_logger()/setup_logging() can be called concurrently at startup.
    # Without the lock both threads race past the flag and attach the handlers
    # twice, duplicating every log line and rotating the same file twice.
    global _configured
    if _configured:
        return

    with _lock:
        if _configured:
            return

        settings = get_settings()
        # logging may run before init_db()/ensure_dirs(), so make sure the log
        # directory exists before opening the file handler.
        settings.log_dir.mkdir(parents=True, exist_ok=True)
        log_file = settings.log_dir / "pipeline.log"

        root = logging.getLogger()
        root.setLevel(settings.log_level.upper())

        formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)

        # Drop any handlers we previously installed (e.g. on reload) so we never
        # accumulate duplicates alongside uvicorn's own handlers.
        for handler in list(root.handlers):
            if getattr(handler, _HANDLER_FLAG, False):
                root.removeHandler(handler)

        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        setattr(file_handler, _HANDLER_FLAG, True)
        root.addHandler(file_handler)

        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        setattr(stream_handler, _HANDLER_FLAG, True)
        root.addHandler(stream_handler)

        _configured = True


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)
