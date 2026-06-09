"""
MCP tool that imports a .docx attached in Open WebUI into the local sandbox.

The actual download / validation / atomic write lives in
:mod:`word_document_server.utils.owui_importer`. This module is only the
thin tool-shaped facade: it picks the destination directory, converts
exceptions to user-facing strings, and returns a relative path the LLM
can immediately feed into the other Word tools (``get_document_info``,
``add_paragraph`` etc.).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from word_document_server.utils import owui_importer
from word_document_server.utils.owui_importer import (
    OwuiAuthError,
    OwuiConfigError,
    OwuiContentError,
    OwuiImportError,
    OwuiNotFoundError,
)


def _destination_dir() -> Path:
    """
    Pick where freshly imported files should land.

    In Docker (streamable-http transport) the working directory is
    ``WORD_FILES_PATH`` (``/app/word_files``) by default, so simply using
    CWD is correct and keeps the file visible to every other tool.

    The ``WORD_FILES_PATH`` env var overrides CWD when explicitly set;
    this lets stdio runs target a specific directory without ``cd``-ing.
    """
    configured = os.getenv("WORD_FILES_PATH")
    if configured:
        return Path(configured)
    return Path.cwd()


def import_word_from_owui(
    file_id: str,
    save_as: Optional[str] = None,
) -> str:
    """
    Import a .docx attached in Open WebUI into the local sandbox.

    Args:
        file_id: The OWUI ``file_id`` of an attached .docx. The model
            cannot read this from the chat context automatically — the
            user (or an OWUI filter) must surface it explicitly.
        save_as: Optional preferred filename. Any directory components
            are stripped and the result is sanitised to
            ``[A-Za-z0-9._-]`` plus a forced ``.docx`` extension.

    Returns:
        On success, a relative POSIX-style path inside ``WORD_FILES_PATH``
        that other tools accept verbatim (e.g. ``"report.docx"``). On
        failure, a single-line error message starting with ``"Error: "``.
    """
    try:
        dest_dir = _destination_dir()
        dest_dir.mkdir(parents=True, exist_ok=True)

        imported = owui_importer.import_from_owui(
            file_id=file_id,
            destination_dir=dest_dir,
            save_as=save_as,
        )

        # Return a path the LLM can feed back into other tools. We make it
        # relative to dest_dir when possible — otherwise absolute as a
        # safe fallback.
        try:
            rel = imported.stored_path.relative_to(dest_dir.resolve())
            return rel.as_posix()
        except ValueError:
            return str(imported.stored_path)

    except OwuiConfigError as exc:
        return f"Error: {exc}"
    except OwuiAuthError as exc:
        return f"Error: {exc}"
    except OwuiNotFoundError as exc:
        return f"Error: {exc}"
    except OwuiContentError as exc:
        return f"Error: {exc}"
    except OwuiImportError as exc:
        return f"Error: {exc}"
