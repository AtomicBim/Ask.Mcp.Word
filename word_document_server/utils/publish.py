"""
Utilities for publishing generated .docx files for download via HTTP.

A "published" file is a copy of a user document placed under MCP_FILES_DIR
with a UUID-based filename. It is served by the same FastMCP HTTP server
under MCP_FILES_URL_PREFIX (default: ``/files``) and reachable externally
through MCP_PUBLIC_BASE_URL (e.g. https://word-mcp.ai.atomsk.ru).

Public copies are purged automatically by a background thread after
MCP_FILES_TTL_HOURS (0 disables purging — not recommended in production).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def get_public_dir() -> Path:
    """Directory where published files live inside the container."""
    return Path(os.getenv("MCP_FILES_DIR", "/app/public_files"))


def get_url_prefix() -> str:
    """URL path prefix (no trailing slash) under which files are served."""
    return os.getenv("MCP_FILES_URL_PREFIX", "/files").rstrip("/") or "/files"


def get_base_url() -> str:
    """Public base URL of the MCP server (no trailing slash). May be empty."""
    return os.getenv("MCP_PUBLIC_BASE_URL", "").rstrip("/")


def get_ttl_hours() -> float:
    """How long a published file lives. 0 disables purging."""
    raw = os.getenv("MCP_FILES_TTL_HOURS", "24")
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid MCP_FILES_TTL_HOURS=%r, falling back to 24", raw)
        return 24.0


def ensure_public_dir() -> Path:
    """Make sure the public directory exists and return it."""
    d = get_public_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _permission_hint(target: Path, exc: OSError) -> str:
    """Render a `Permission denied` error with an operator-friendly hint.

    Bind-mounted directories on the host very often end up owned by ``root``
    (e.g. when ``docker compose up`` is the very first command that creates
    them) while the container itself runs as the unprivileged ``${UID}:${GID}``
    from ``.env``. The raw ``[Errno 13]`` message gives the user no clue
    how to fix that — this helper does.
    """
    try:
        uid = os.geteuid()
        gid = os.getegid()
        whoami = f"UID={uid} GID={gid}"
    except AttributeError:  # Windows: geteuid is not available
        whoami = "the container process"

    return (
        f"Permission denied writing to {target} as {whoami}: {exc}. "
        f"On the Docker host run: "
        f"chown -R \"$(id -u):$(id -g)\" "
        f"word_files logs public_files  "
        f"(and `docker compose restart word-mcp-server`)."
    )


def check_public_dir_writable() -> Optional[str]:
    """Verify we can write into ``MCP_FILES_DIR`` and return an error string
    (or ``None`` on success). Called once at server startup so misconfigured
    bind-mount permissions surface in the logs immediately, not only when
    the first user actually tries to publish a file.
    """
    public_dir = get_public_dir()
    try:
        public_dir.mkdir(parents=True, exist_ok=True)
        probe = public_dir / ".write_probe"
        probe.write_bytes(b"")
        probe.unlink(missing_ok=True)
        return None
    except PermissionError as exc:
        return _permission_hint(public_dir, exc)
    except OSError as exc:
        return f"Public dir {public_dir} is not usable: {exc}"


def publish_file(
    source_path: str,
    suggested_name: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Copy ``source_path`` into the public directory under a UUID-based name
    and return ``(public_url, on_disk_path, error)``.

    - ``public_url`` is ``None`` when ``MCP_PUBLIC_BASE_URL`` is not set;
      in that case ``on_disk_path`` is still returned and the caller should
      surface a warning to the user.
    - ``error`` is ``None`` on success, otherwise a human-readable message
      and the other two fields are ``None``.

    ``suggested_name`` is purely cosmetic — it is appended after the UUID
    so that download dialogs display something meaningful (e.g.
    ``report.docx`` becomes ``ab12cd34__report.docx``). Anything outside
    ``[A-Za-z0-9._-]`` is silently dropped to avoid path-traversal and
    Content-Disposition header injection.
    """
    src = Path(source_path)
    if not src.exists():
        return None, None, f"Source file {source_path} does not exist"
    if not src.is_file():
        return None, None, f"{source_path} is not a regular file"

    try:
        public_dir = ensure_public_dir()
    except PermissionError as exc:
        return None, None, _permission_hint(get_public_dir(), exc)
    except OSError as exc:
        return None, None, f"Cannot create public dir {get_public_dir()}: {exc}"

    token = uuid.uuid4().hex[:16]
    ext = src.suffix or ".docx"

    # Sanitised suffix from the original name — purely a UX nicety.
    suffix = ""
    raw_suffix = suggested_name or src.stem
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", raw_suffix).strip("_")
    if safe and _SAFE_NAME_RE.match(safe):
        # Trim to avoid silly long names; UUID already guarantees uniqueness.
        suffix = "__" + safe[:64]

    public_name = f"{token}{suffix}{ext}"
    dst = public_dir / public_name

    try:
        shutil.copy2(src, dst)
    except PermissionError as exc:
        return None, None, _permission_hint(public_dir, exc)
    except OSError as exc:
        return None, None, f"Failed to publish file: {exc}"

    base = get_base_url()
    if not base:
        logger.warning(
            "MCP_PUBLIC_BASE_URL is not set; published file is reachable only "
            "via filesystem path %s",
            dst,
        )
        return None, str(dst), None

    url = f"{base}{get_url_prefix()}/{public_name}"
    return url, str(dst), None


def resolve_published_path(filename: str) -> Optional[Path]:
    """
    Resolve ``filename`` (one path segment, no slashes) to an absolute path
    inside the public directory, or ``None`` if the request is unsafe or
    the file does not exist.

    Refuses any name containing path separators, ``..``, or NUL bytes —
    this is the only defence against path traversal because the HTTP
    layer accepts arbitrary strings for the ``{filename}`` placeholder.
    """
    if not filename or "/" in filename or "\\" in filename or "\x00" in filename:
        return None
    if filename in (".", "..") or filename.startswith("."):
        # No dotfiles, no parent refs.
        return None

    public_dir = get_public_dir().resolve()
    try:
        candidate = (public_dir / filename).resolve()
    except OSError:
        return None

    # Reject anything that escapes the public dir (symlink shenanigans etc.).
    try:
        candidate.relative_to(public_dir)
    except ValueError:
        return None

    if not candidate.is_file():
        return None
    return candidate


def _purge_once(now: float, ttl_seconds: float) -> int:
    """Single sweep of the public directory. Returns count of removed files."""
    removed = 0
    public_dir = get_public_dir()
    if not public_dir.exists():
        return 0
    cutoff = now - ttl_seconds
    for entry in public_dir.iterdir():
        try:
            if not entry.is_file():
                continue
            if entry.stat().st_mtime < cutoff:
                entry.unlink(missing_ok=True)
                removed += 1
        except OSError as exc:
            logger.debug("Could not check/remove %s: %s", entry, exc)
    if removed:
        logger.info("Purged %d expired published file(s)", removed)
    return removed


def _cleanup_loop(ttl_hours: float, interval_seconds: int) -> None:
    ttl_seconds = ttl_hours * 3600.0
    while True:
        try:
            _purge_once(time.time(), ttl_seconds)
        except Exception:  # pragma: no cover — daemon thread, must not die
            logger.exception("Unexpected error in published files cleanup loop")
        time.sleep(interval_seconds)


_cleanup_thread: Optional[threading.Thread] = None
_cleanup_lock = threading.Lock()


def start_cleanup_thread(interval_seconds: int = 3600) -> bool:
    """
    Start (idempotently) a daemon thread that purges files older than
    ``MCP_FILES_TTL_HOURS``. Returns ``True`` if a new thread was created,
    ``False`` if cleanup is disabled or already running.

    The thread is a daemon — it dies with the process and never blocks
    shutdown. We intentionally do not use asyncio.create_task because the
    FastMCP server may run on either uvicorn's event loop or stdio (no
    loop at all), and a plain thread works in both cases.
    """
    global _cleanup_thread
    ttl = get_ttl_hours()
    if ttl <= 0:
        logger.info("Published files cleanup disabled (MCP_FILES_TTL_HOURS=%s)", ttl)
        return False

    with _cleanup_lock:
        if _cleanup_thread is not None and _cleanup_thread.is_alive():
            return False
        _cleanup_thread = threading.Thread(
            target=_cleanup_loop,
            args=(ttl, interval_seconds),
            name="word-mcp-files-cleanup",
            daemon=True,
        )
        _cleanup_thread.start()
        logger.info(
            "Started published files cleanup thread (TTL=%.2fh, every %ds)",
            ttl,
            interval_seconds,
        )
        return True
