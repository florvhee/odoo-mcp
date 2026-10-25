"""Validate supplied screenshot bytes without reading local files or fetching URLs."""

import base64
import binascii
from io import BytesIO
from pathlib import PurePosixPath, PureWindowsPath
import warnings

from PIL import Image

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
MAX_BASE64_LENGTH = 4 * ((MAX_IMAGE_BYTES + 2) // 3)
IMAGE_FORMATS = {"PNG": ("image/png", {".png"}), "JPEG": ("image/jpeg", {".jpg", ".jpeg"}),
                 "WEBP": ("image/webp", {".webp"})}


def validate_image(filename: str, data: str) -> str:
    """Return the detected MIME type after validating a bounded, static raster image."""
    if (not isinstance(filename, str) or not filename.strip() or len(filename) > 255
            or PurePosixPath(filename).name != filename or PureWindowsPath(filename).name != filename
            or any(ord(c) < 32 for c in filename)):
        raise ValueError("Provide a filename only, without a path or control characters.")
    if not isinstance(data, str) or not data or len(data) > MAX_BASE64_LENGTH:
        raise ValueError("Supply base64 image bytes, at most 5 MiB decoded; paths and URLs are not supported.")
    try:
        raw = base64.b64decode(data, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("Image data must be plain base64, without a data URL prefix.") from None
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("Image must contain between 1 byte and 5 MiB.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw), formats=list(IMAGE_FORMATS)) as img:
                mime, extensions = IMAGE_FORMATS[img.format]
                if PurePosixPath(filename).suffix.lower() not in extensions:
                    raise ValueError("Image filename extension must match its detected format.")
                if img.width * img.height > MAX_IMAGE_PIXELS or getattr(img, "n_frames", 1) != 1:
                    raise ValueError("Use a static screenshot of at most 20 megapixels.")
                img.verify()
            # verify() alone does not decode all image data.
            with Image.open(BytesIO(raw), formats=list(IMAGE_FORMATS)) as img:
                img.load()
    except (OSError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValueError("Invalid or oversized image. Supported formats: PNG, JPEG and WebP.") from None
    return mime
