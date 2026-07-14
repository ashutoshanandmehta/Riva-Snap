"""Image preprocessing: normalize orientation and size before the vision call.

Downscaling to ~1024px on the long edge keeps vision-token cost low and
latency in the 2-4s band without hurting food recognition accuracy.
"""

import base64
import io

from PIL import Image, ImageOps

MAX_LONG_EDGE = 1024
JPEG_QUALITY = 85


def prepare_image(raw: bytes) -> str:
    """Returns a base64-encoded, EXIF-corrected, downscaled JPEG."""
    image = Image.open(io.BytesIO(raw))
    # Apply EXIF rotation so phone photos are upright for the model.
    image = ImageOps.exif_transpose(image)

    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")

    longest = max(image.size)
    if longest > MAX_LONG_EDGE:
        scale = MAX_LONG_EDGE / longest
        new_size = (round(image.width * scale), round(image.height * scale))
        image = image.resize(new_size, Image.LANCZOS)

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=JPEG_QUALITY)
    return base64.b64encode(buffer.getvalue()).decode("ascii")
