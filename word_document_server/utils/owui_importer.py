"""
Import .docx files attached to a chat in Open WebUI into the local sandbox.

Open WebUI stores **two** versions of every uploaded file:

1. The original binary, reachable through ``GET /api/v1/files/{file_id}/content``.
   This is what we want for Word documents — a real .docx ZIP archive.
2. An extracted text representation used for RAG, reachable through
   ``GET /api/v1/files/{file_id}`` (returns JSON with ``data.content``).
   This is NOT what we want — calling it produces a plain text blob that
   python-docx cannot open.

This module talks only to the ``/content`` endpoint and then verifies
that the response really is a ZIP-based .docx via magic bytes, so we
never accidentally save extracted text under a ``.docx`` extension.

Threat model and defences:

- ``file_id`` is reflected into a URL → reject anything but ``[A-Za-z0-9._-]``
  before issuing the request (SSRF / path-traversal defence).
- ``save_as`` is reflected into a filesystem path → run it through
  :func:`_safe_destination` which resolves against ``destination_dir`` and
  refuses anything that escapes it.
- The downloaded payload is checked for the ZIP magic bytes
  ``PK\\x03\\x04`` / ``PK\\x05\\x06``. Empty archives (``PK\\x05\\x06`` only)
  are accepted because that is still a valid .docx with no entries.
- The file is written to ``*.tmp`` first and then ``os.replace``'d into
  its final name, so a crashed import never leaves a half-written file
  the LLM might try to open.
"""

from __future__ import annotations

import logging
import os
import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

import httpx

logger = logging.getLogger(__name__)


# ─── Public exceptions ──────────────────────────────────────────────────────


class OwuiImportError(Exception):
    """Base exception for all Open WebUI import failures."""


class OwuiConfigError(OwuiImportError):
    """OWUI_BASE_URL / OWUI_API_KEY missing or malformed."""


class OwuiAuthError(OwuiImportError):
    """OWUI returned 401 or 403 — bad / missing bearer token."""


class OwuiNotFoundError(OwuiImportError):
    """OWUI returned 404 — file_id unknown or original storage disabled."""


class OwuiContentError(OwuiImportError):
    """Downloaded payload is not a valid .docx (failed magic-byte check)."""


# ─── Config helpers ─────────────────────────────────────────────────────────


def _base_url() -> str:
    raw = (os.getenv("OWUI_BASE_URL") or "").strip().rstrip("/")
    if not raw:
        raise OwuiConfigError(
            "OWUI_BASE_URL is not set. "
            "Configure it on the MCP server (e.g. https://chat.example.com)."
        )
    if not raw.startswith(("http://", "https://")):
        raise OwuiConfigError(
            f"OWUI_BASE_URL must start with http:// or https:// (got {raw!r})."
        )
    return raw


def _api_key() -> str:
    raw = (os.getenv("OWUI_API_KEY") or "").strip()
    if not raw:
        raise OwuiConfigError(
            "OWUI_API_KEY is not set. "
            "Issue an API key in Open WebUI: Settings → Account → API Keys."
        )
    return raw


def _timeout() -> float:
    raw = os.getenv("OWUI_HTTP_TIMEOUT", "30")
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Invalid OWUI_HTTP_TIMEOUT=%r, falling back to 30", raw)
        return 30.0
    return max(1.0, value)


# ─── file_id validation ────────────────────────────────────────────────────

# OWUI uses UUIDs or short opaque tokens. Restrict to a permissive but safe
# character set; this stops anyone smuggling a slash into the URL.
_FILE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _is_valid_file_id(file_id: str) -> bool:
    if not isinstance(file_id, str) or not file_id:
        return False
    if any(ch.isspace() for ch in file_id):
        return False
    if "\x00" in file_id or "/" in file_id or "\\" in file_id:
        return False
    if ".." in file_id:
        return False
    return bool(_FILE_ID_RE.match(file_id))


# ─── Content-Disposition parser (RFC 6266) ─────────────────────────────────

# Match ``filename*=charset'lang'percent-encoded`` (preferred form).
_CD_EXT_RE = re.compile(
    r"""filename\*\s*=\s*
        (?P<charset>[\w!#$%&+\-^_`{}~.]+)        # token: charset
        '                                         # apostrophe separator
        (?P<lang>[\w!#$%&+\-^_`{}~.]*)            # token: language (may be empty)
        '
        (?P<value>[^;]*)                          # percent-encoded value
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Match ``filename="quoted"`` or ``filename=token``.
_CD_QUOTED_RE = re.compile(r'filename\s*=\s*"((?:[^"\\]|\\.)*)"', re.IGNORECASE)
_CD_TOKEN_RE = re.compile(r"filename\s*=\s*([^;\s]+)", re.IGNORECASE)


def _parse_content_disposition(header: Optional[str]) -> Optional[str]:
    """
    Return the filename from a Content-Disposition header, preferring the
    RFC 6266 ``filename*`` form so Cyrillic / Unicode names survive.

    Returns ``None`` if the header is missing or no recognisable filename
    is present.
    """
    if not header:
        return None

    # 1. RFC 6266 extended form.
    m = _CD_EXT_RE.search(header)
    if m:
        charset = (m.group("charset") or "utf-8").strip() or "utf-8"
        encoded = m.group("value") or ""
        try:
            return urllib.parse.unquote(encoded, encoding=charset, errors="strict")
        except (LookupError, UnicodeDecodeError):
            # Fall through to legacy parsing on a broken charset / encoding.
            logger.debug("Failed to decode filename* with charset=%s", charset)

    # 2. Quoted legacy form.
    m = _CD_QUOTED_RE.search(header)
    if m:
        # Unescape ``\"`` and ``\\`` inside the quoted string.
        return re.sub(r"\\(.)", r"\1", m.group(1))

    # 3. Bare token form.
    m = _CD_TOKEN_RE.search(header)
    if m:
        return m.group(1)

    return None


# ─── Filename sanitisation ─────────────────────────────────────────────────


_BAD_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")
_TRIM_RE = re.compile(r"^[._\s]+|[._\s]+$")


def _sanitize_basename(name: str, *, default: str = "imported.docx") -> str:
    """
    Return a safe basename ending with ``.docx``. The result is guaranteed
    to contain only ``[A-Za-z0-9._-]`` and to have no path separators.
    """
    if not name:
        return default

    # Strip any directory component a malicious server could try to send.
    basename = name.replace("\\", "/").rsplit("/", 1)[-1]

    # Split into stem/ext BEFORE character substitution so we don't lose
    # the original extension boundary when the stem is purely non-ASCII.
    # ``Otчёт.docx`` → stem="Отчёт", ext="docx"; without this, the dot
    # gets glued to a leading-underscore stem and the suffix is dropped
    # by the leading-junk trimmer below.
    stem, dot, ext = basename.rpartition(".")
    if not dot:
        stem, ext = basename, ""

    safe_stem = _BAD_CHARS_RE.sub("_", stem)
    safe_stem = _TRIM_RE.sub("", safe_stem)

    if not safe_stem or safe_stem in {".", ".."}:
        # Fully non-ASCII or pathological → just give up on the original
        # name and use the default; the file is still uniquely findable
        # by the LLM because we return the chosen path back to it.
        return default

    safe_stem = safe_stem[:200]

    # Force .docx no matter what the upstream said — this stops the LLM
    # from receiving e.g. ``payload.exe`` because OWUI returned a wrong
    # Content-Disposition.
    return f"{safe_stem}.docx"


# ─── Sandbox-safe destination ──────────────────────────────────────────────


def _safe_destination(destination_dir: Path, basename: str) -> Path:
    """
    Resolve ``destination_dir / basename`` and assert the result stays
    inside ``destination_dir``. Raises ``OwuiImportError`` on escape.

    This is a defence-in-depth measure: ``basename`` has already been
    sanitised, so this should never trip in practice — but if it does,
    we want a clean error and not a silent write outside the sandbox.
    """
    try:
        root = destination_dir.resolve(strict=True)
    except FileNotFoundError as exc:
        raise OwuiImportError(
            f"Destination directory {destination_dir} does not exist"
        ) from exc

    candidate = (root / basename).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise OwuiImportError(
            f"Refusing to write outside the sandbox: {candidate} ⊄ {root}"
        ) from exc

    return candidate


# ─── HTTP layer ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Fetched:
    blob: bytes
    source_name: Optional[str]


def fetch_file_bytes(file_id: str) -> _Fetched:
    """
    Download the original binary for ``file_id`` from OWUI Files API.

    Returns the raw bytes plus the source filename (if any) extracted
    from the Content-Disposition header. Raises a subclass of
    :class:`OwuiImportError` on any failure.
    """
    if not _is_valid_file_id(file_id):
        raise OwuiImportError(
            f"Invalid file_id: must match [A-Za-z0-9._-]{{1,128}} (got {file_id!r})."
        )

    base = _base_url()
    token = _api_key()
    url = f"{base}/api/v1/files/{file_id}/content"

    headers = {
        "Authorization": f"Bearer {token}",
        # Some forks 406 on Accept absent; "*/*" is universally accepted.
        "Accept": "*/*",
    }

    try:
        with httpx.Client(timeout=_timeout()) as client:
            response = client.get(url, headers=headers, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise OwuiImportError(
            f"Could not reach Open WebUI at {base}: {exc}"
        ) from exc

    if response.status_code in (401, 403):
        raise OwuiAuthError(
            f"OWUI rejected the request ({response.status_code}). "
            f"Check OWUI_API_KEY: it must be a valid bearer token issued by "
            f"the same Open WebUI instance as OWUI_BASE_URL."
        )
    if response.status_code == 404:
        raise OwuiNotFoundError(
            f"OWUI does not have a binary for file_id={file_id!r} (404). "
            f"Either the id is wrong, or Open WebUI is configured to drop "
            f"original files after extraction."
        )
    if response.status_code >= 400:
        # Truncate body to keep error messages bounded.
        snippet = (response.text or "")[:200].replace("\n", " ")
        raise OwuiImportError(
            f"OWUI returned HTTP {response.status_code}: {snippet}"
        )

    source_name = _parse_content_disposition(
        response.headers.get("content-disposition")
    )
    return _Fetched(blob=response.content, source_name=source_name)


# ─── High-level entry point ────────────────────────────────────────────────


# .docx is a ZIP container. Two valid ZIP signatures exist; the second is
# an empty central directory, which python-docx rejects anyway but we
# accept here so the error surfaces from python-docx with its own message
# rather than from us.
_ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06")


@dataclass(frozen=True)
class ImportedFile:
    """Result of a successful import."""

    stored_path: Path
    basename: str
    bytes_written: int
    source_name: Optional[str]


def import_from_owui(
    file_id: str,
    destination_dir: os.PathLike,
    save_as: Optional[str] = None,
) -> ImportedFile:
    """
    Download ``file_id`` from OWUI, validate it is a .docx, and write it
    atomically into ``destination_dir``.

    The final filename is chosen by priority: ``save_as`` > server-provided
    name (Content-Disposition) > ``f"{file_id}.docx"``. The result always
    ends with ``.docx`` and contains only safe characters.
    """
    dest_dir = Path(destination_dir)
    fetched = fetch_file_bytes(file_id)

    if not fetched.blob.startswith(_ZIP_MAGIC):
        # Truncate to first byte preview for the error message.
        preview = fetched.blob[:4].hex() if fetched.blob else "(empty)"
        raise OwuiContentError(
            f"OWUI returned a payload that is not a .docx (no ZIP signature, "
            f"first 4 bytes = {preview}). Make sure you pass the file_id of "
            f"a .docx attachment and that Open WebUI keeps original files."
        )

    chosen = save_as or fetched.source_name or f"{file_id}.docx"
    basename = _sanitize_basename(chosen)
    target = _safe_destination(dest_dir, basename)

    # Atomic write: tmp file in the same dir → os.replace.
    tmp = target.with_name(target.name + ".tmp")
    try:
        tmp.write_bytes(fetched.blob)
        os.replace(tmp, target)
    except OSError as exc:
        # Best-effort cleanup of the half-written tmp file.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise OwuiImportError(
            f"Failed to write imported file to {target}: {exc}"
        ) from exc

    return ImportedFile(
        stored_path=target,
        basename=basename,
        bytes_written=len(fetched.blob),
        source_name=fetched.source_name,
    )
