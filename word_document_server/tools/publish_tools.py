"""
MCP tool for publishing generated .docx files so Open WebUI can download them.

The tool copies a file from the working directory (``WORD_FILES_PATH`` /
container CWD) into the public files directory and returns a fully-qualified
HTTPS URL that the chat front-end can render as a download link.
"""

from typing import Optional

from word_document_server.utils.file_utils import ensure_docx_extension
from word_document_server.utils import publish as _publish


async def publish_word_file(
    filename: str,
    download_name: Optional[str] = None,
) -> str:
    """
    Publish a .docx for download.

    Args:
        filename: Path to a .docx inside the working directory (relative
            paths are resolved against the container's CWD, which is
            ``WORD_FILES_PATH``).
        download_name: Optional cosmetic name embedded into the public
            filename (after a UUID) so the user sees something meaningful
            in the download dialog. Only ``[A-Za-z0-9._-]`` is preserved.

    Returns:
        A human-readable string containing the public download URL on
        success, or an explanatory error message on failure. If
        ``MCP_PUBLIC_BASE_URL`` is not configured, the on-disk path of
        the published copy is returned with a warning instead of a URL.
    """
    filename = ensure_docx_extension(filename)

    url, on_disk, error = _publish.publish_file(
        source_path=filename,
        suggested_name=download_name,
    )

    if error:
        return f"Failed to publish {filename}: {error}"

    if url:
        return f"File published: {url}"

    # No public base URL — return the path with a warning so the model
    # can at least tell the user where the file ended up.
    return (
        f"File copied to {on_disk}, but MCP_PUBLIC_BASE_URL is not set, "
        f"so no download URL can be returned. Configure MCP_PUBLIC_BASE_URL "
        f"on the server to enable HTTP downloads."
    )
