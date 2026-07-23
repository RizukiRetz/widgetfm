"""
imgfixer.py — Album art processor for Discord widget bannerwidgettop.

Removes the thin top strip and rounds the top-right corner of album art
so it sits neatly inside Discord's widget frame, then uploads the result
to a Discord webhook for hosting on Discord's own CDN.

Algorithm ported from D.W.I.F. (Discord Widget Image Fixer) by AjaxFNC-YT:
https://github.com/AjaxFNC-YT/D.W.I.F

Requires: Pillow  (pip install Pillow)
"""

import io
import math
import time

import requests
from PIL import Image, ImageDraw


# ── D.W.I.F. calibration constants ────────────────────────────────────────────
# These mirror the reference values from the original JS implementation.
_REF_SIZE          = 512
_TOP_STRIP_BASE    = 17
_RADIUS_BASE       = 36
_CAL_W, _CAL_H     = 1844, 853   # "large" calibration resolution

_TOP_STRIP_EXP = math.log(54 / 17) / math.log(
    math.sqrt(_CAL_W * _CAL_H) / _REF_SIZE
)
_RADIUS_EXP = math.log(172 / 36) / math.log(
    math.sqrt(_CAL_W * _CAL_H) / _REF_SIZE
)

# ── In-memory cache ───────────────────────────────────────────────────────────
# { original_url: (cdn_url, uploaded_at_unix_timestamp) }
_cache: dict[str, tuple[str, float]] = {}


# ── Core image processing ─────────────────────────────────────────────────────

def _auto_value(base: float, exponent: float, w: int, h: int) -> int:
    """Scale a base pixel value to the actual image size."""
    factor = math.sqrt(w * h) / _REF_SIZE
    return max(0, round(base * (factor ** exponent)))


def _corner_mask(radius: int) -> Image.Image:
    """
    Build a top-right corner cutout tile (radius × radius, RGBA).
    Transparent where the rounded corner should be, opaque elsewhere.
    """
    mask = Image.new("L", (radius, radius), 255)  # fully opaque
    draw = ImageDraw.Draw(mask)
    # Circle centred at the bottom-left of the tile cuts the upper-left area
    draw.ellipse((-radius, 0, radius, 2 * radius), fill=0)
    tile = Image.new("RGBA", (radius, radius), (0, 0, 0, 0))
    tile.putalpha(mask)
    return tile


def _process_frame(frame: Image.Image, top_strip: int, radius: int) -> Image.Image:
    """
    Apply the top-strip removal and top-right corner rounding to a single frame.

    - top_strip : number of pixels hidden at the very top (transparent band)
    - radius    : corner-rounding radius in pixels
    """
    w, h = frame.size
    frame  = frame.convert("RGBA")
    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))

    # Paste only the visible region (below the top strip)
    visible = frame.crop((0, top_strip, w, h))
    canvas.paste(visible, (0, top_strip))

    # Cut the top-right corner
    clamped_r = min(radius, w, max(h - top_strip, 0))
    if clamped_r > 0:
        corner = _corner_mask(clamped_r).convert("RGBA")
        temp   = canvas.copy()
        temp.paste((0, 0, 0, 0), (w - clamped_r, top_strip), corner)
        canvas = temp

    return canvas


def _process_image(img_bytes: bytes) -> bytes:
    """
    Apply the imgfixer algorithm to raw image bytes.
    Returns a PNG byte string with transparency preserved.
    """
    img       = Image.open(io.BytesIO(img_bytes))
    w, h      = img.size
    top_strip = _auto_value(_TOP_STRIP_BASE, _TOP_STRIP_EXP, w, h)
    radius    = _auto_value(_RADIUS_BASE,    _RADIUS_EXP,    w, h)
    processed = _process_frame(img, top_strip, radius)
    buf       = io.BytesIO()
    processed.save(buf, "PNG")
    return buf.getvalue()


# ── Discord webhook upload ────────────────────────────────────────────────────

def _upload_to_webhook(png_bytes: bytes, webhook_url: str) -> str:
    """
    POST a PNG to a Discord webhook and return the CDN attachment URL.

    The webhook must be in a channel only you can see — it acts as a
    private image bucket. Each call posts a new message with the image
    attached and returns its cdn.discordapp.com URL.
    """
    r = requests.post(
        webhook_url,
        files   = {"file": ("banner.png", png_bytes, "image/png")},
        params  = {"wait": "true"},   # wait for Discord to return the message JSON
        timeout = 15,
    )
    r.raise_for_status()
    return r.json()["attachments"][0]["url"]


# ── Public API ────────────────────────────────────────────────────────────────

def fix_banner_url(
    original_url:       str,
    webhook_url:        str,
    reupload_interval:  int = 72_000,   # 20 hours in seconds
) -> str:
    """
    Return a DWIF-processed Discord CDN URL for the given album art URL.

    Caches results in memory. Re-uploads automatically when:
      - The URL has never been processed before (first time), OR
      - The cached CDN URL is older than reupload_interval seconds
        (Discord signed CDN URLs expire at ~24 h; 20 h gives a safety margin).

    Falls back silently to the original Last.FM URL if anything fails
    (Pillow import error, network issue, webhook misconfigured, etc.)
    so the widget always shows *some* image.

    Parameters
    ----------
    original_url      : Last.FM CDN URL for the album art
    webhook_url       : Discord webhook URL used as a private image bucket
    reupload_interval : seconds before forcing a CDN URL refresh (default 20 h)
    """
    if not original_url or not webhook_url:
        return original_url

    cached_cdn, uploaded_at = _cache.get(original_url, (None, 0.0))
    age_seconds = time.time() - uploaded_at

    # Return the cached URL if it is still fresh enough
    if cached_cdn and age_seconds < reupload_interval:
        return cached_cdn

    try:
        img_bytes = requests.get(original_url, timeout=10).content
        png_bytes = _process_image(img_bytes)
        cdn_url   = _upload_to_webhook(png_bytes, webhook_url)

        _cache[original_url] = (cdn_url, time.time())

        action = "Re-uploaded" if cached_cdn else "Processed & uploaded"
        print(f"[imgfixer] {action} banner → {cdn_url}")
        return cdn_url

    except Exception as exc:
        print(f"[imgfixer] Failed ({exc}) — falling back to wsrv.nl proxy")
        clean = original_url.replace("https://", "")
        return f"https://wsrv.nl/?url={clean}&output=jpg&q=85"
