"""Private file storage for attendance selfies and documents.

Uploads are written under ``FILE_STORAGE_DIR``, which sits outside the repository and
is never served as a static directory. The only way to read one back is a short-lived
HMAC-signed token (``GET /v1/files/{token}``), so a leaked URL expires instead of
becoming a permanent public link to a field worker's photograph.

Swapping this for S3/GCS later means reimplementing ``store_upload`` and ``build_url``
against pre-signed URLs; nothing outside this module knows where the bytes live.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Final

from app.core.config import Settings
from app.core.errors import ValidationFailedError

IMAGE_EXTENSIONS: Final = {".jpg", ".jpeg", ".png", ".webp", ".heic"}
DOCUMENT_EXTENSIONS: Final = IMAGE_EXTENSIONS | {".pdf"}

CONTENT_TYPES: Final = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".pdf": "application/pdf",
}


class SignedUrlError(Exception):
    """The token was malformed, tampered with, or has expired."""


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def store_file(
    settings: Settings,
    *,
    data: bytes,
    filename: str | None,
    category: str,
    allowed_extensions: set[str],
    field_name: str,
) -> str:
    """Persist ``data`` and return its storage key.

    The key is a server-generated path. The client's filename is used only to pick an
    extension — never to build the path, so a crafted name cannot escape the directory.
    """
    extension = PurePosixPath(filename or "").suffix.lower()
    if extension not in allowed_extensions:
        raise ValidationFailedError(
            "Unsupported file type.",
            code="UNSUPPORTED_FILE_TYPE",
            details=[
                {"field": field_name, "issue": f"allowed: {', '.join(sorted(allowed_extensions))}"}
            ],
        )

    payload = data
    if not payload:
        raise ValidationFailedError(
            "The uploaded file is empty.",
            code="EMPTY_FILE",
            details=[{"field": field_name, "issue": "must not be empty"}],
        )
    if len(payload) > settings.max_upload_bytes:
        raise ValidationFailedError(
            "The uploaded file is too large. Compress it and try again.",
            code="FILE_TOO_LARGE",
            details=[{"field": field_name, "issue": f"maximum {settings.max_upload_bytes} bytes"}],
        )

    key = f"{category}/{uuid.uuid4().hex}{extension}"
    destination = settings.file_storage_dir / key
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)
    return key


def build_url(settings: Settings, key: str) -> str:
    """Return an absolute, short-lived, signed URL for ``key``."""
    expires_at = int(time.time()) + settings.file_url_ttl_seconds
    body = _b64encode(json.dumps({"k": key, "e": expires_at}, separators=(",", ":")).encode())
    signature = _b64encode(_sign(settings, body))
    return f"{settings.public_base_url.rstrip('/')}/v1/files/{body}.{signature}"


def resolve_token(settings: Settings, token: str) -> tuple[Path, str]:
    """Validate ``token`` and return the file path plus its content type."""
    try:
        body, signature = token.split(".", 1)
        if not hmac.compare_digest(_b64encode(_sign(settings, body)), signature):
            raise SignedUrlError("bad signature")
        claims = json.loads(_b64decode(body))
        key = str(claims["k"])
        expires_at = int(claims["e"])
    except SignedUrlError:
        raise
    except Exception as exc:  # malformed base64, JSON, or missing claims
        raise SignedUrlError("malformed token") from exc

    if expires_at < int(time.time()):
        raise SignedUrlError("expired")

    root = settings.file_storage_dir.resolve()
    path = (root / key).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise SignedUrlError("unknown file")

    return path, CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


def _sign(settings: Settings, body: str) -> bytes:
    return hmac.new(
        settings.file_url_secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256
    ).digest()
