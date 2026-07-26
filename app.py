"""
Unity Asset Bundle Texture Replacer — Web Service
=================================================
No Telegram. Upload a .bundle + PNG(s), download the modified bundle.

Env:
  PORT          — set by Render (default 8080)
  MAX_BUNDLE_MB — default 80
  MAX_TEXTURE_MB — default 20
"""

from __future__ import annotations

import io
import logging
import os
import re
import tempfile
import zipfile
from pathlib import Path

import UnityPy
from aiohttp import web
from PIL import Image

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

MAX_BUNDLE_MB = int(os.environ.get("MAX_BUNDLE_MB", "80"))
MAX_TEXTURE_MB = int(os.environ.get("MAX_TEXTURE_MB", "20"))
PORT = int(os.environ.get("PORT", "8080"))

STATIC_DIR = Path(__file__).parent


# ---------------------------------------------------------------------------
# UnityPy helpers
# ---------------------------------------------------------------------------

def list_textures(data: bytes) -> list[dict]:
    env = UnityPy.load(data)
    out = []
    for obj in env.objects:
        if obj.type.name != "Texture2D":
            continue
        tex = obj.read()
        name = getattr(tex, "m_Name", None) or f"pathid_{obj.path_id}"
        out.append(
            {
                "name": name,
                "width": getattr(tex, "m_Width", 0),
                "height": getattr(tex, "m_Height", 0),
                "path_id": obj.path_id,
            }
        )
    return out


def replace_textures(bundle_bytes: bytes, replacements: dict[str, bytes]) -> bytes:
    """
    replacements: { m_Name_lower: png_bytes }
    Returns modified bundle bytes (lz4).
    """
    env = UnityPy.load(bundle_bytes)
    replaced = []

    for obj in env.objects:
        if obj.type.name != "Texture2D":
            continue
        tex = obj.read()
        name = getattr(tex, "m_Name", None) or ""
        key = name.lower()
        if key not in replacements:
            continue
        try:
            img = Image.open(io.BytesIO(replacements[key])).convert("RGBA")
            if hasattr(tex, "set_image"):
                tex.set_image(img)
            else:
                tex.image = img
            tex.save()
            replaced.append(name)
            logger.info("Replaced: %s", name)
        except Exception:
            logger.exception("Failed to replace %s", name)

    if not replaced:
        raise ValueError(
            "No textures matched. PNG filenames (without extension) must match Texture2D m_Name."
        )

    # Write to temp file then read back (UnityPy save API)
    with tempfile.NamedTemporaryFile(suffix=".bundle", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        env.save(pack="lz4", out_path=tmp_path)
        return Path(tmp_path).read_bytes()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def sanitize(name: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", name)[:120]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

async def index(request: web.Request) -> web.Response:
    html_path = STATIC_DIR / "index.html"
    return web.FileResponse(html_path)


async def health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def api_list(request: web.Request) -> web.Response:
    """POST multipart: field 'bundle' = asset bundle file → list Texture2D names."""
    reader = await request.multipart()
    bundle_bytes = None
    filename = "bundle"

    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name == "bundle":
            filename = part.filename or "bundle"
            bundle_bytes = await part.read(decode=False)

    if not bundle_bytes:
        return web.json_response({"error": "No bundle uploaded"}, status=400)

    size_mb = len(bundle_bytes) / (1024 * 1024)
    if size_mb > MAX_BUNDLE_MB:
        return web.json_response(
            {"error": f"Bundle too large ({size_mb:.1f} MB). Max {MAX_BUNDLE_MB} MB."},
            status=400,
        )

    try:
        textures = list_textures(bundle_bytes)
    except Exception as e:
        logger.exception("Parse failed")
        return web.json_response(
            {"error": f"Could not parse as Unity AssetBundle: {e}"},
            status=400,
        )

    return web.json_response(
        {
            "filename": filename,
            "size_mb": round(size_mb, 2),
            "textures": textures,
            "count": len(textures),
        }
    )


async def api_replace(request: web.Request) -> web.Response:
    """
    POST multipart:
      - bundle: the .bundle file
      - textures: one or more PNG files (filename stem = m_Name)
    Returns the modified .bundle as attachment.
    """
    reader = await request.multipart()
    bundle_bytes = None
    bundle_name = "bundle.bundle"
    replacements: dict[str, bytes] = {}

    while True:
        part = await reader.next()
        if part is None:
            break

        if part.name == "bundle":
            bundle_name = part.filename or "bundle.bundle"
            bundle_bytes = await part.read(decode=False)
        elif part.name in ("textures", "texture", "png", "file"):
            fname = part.filename or "tex.png"
            data = await part.read(decode=False)
            size_mb = len(data) / (1024 * 1024)
            if size_mb > MAX_TEXTURE_MB:
                return web.json_response(
                    {"error": f"Texture {fname} too large ({size_mb:.1f} MB)"},
                    status=400,
                )
            stem = Path(fname).stem.lower()
            replacements[stem] = data

    if not bundle_bytes:
        return web.json_response({"error": "No bundle uploaded"}, status=400)
    if not replacements:
        return web.json_response({"error": "No PNG textures uploaded"}, status=400)

    size_mb = len(bundle_bytes) / (1024 * 1024)
    if size_mb > MAX_BUNDLE_MB:
        return web.json_response(
            {"error": f"Bundle too large ({size_mb:.1f} MB)"},
            status=400,
        )

    try:
        out_bytes = replace_textures(bundle_bytes, replacements)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    except Exception as e:
        logger.exception("Replace failed")
        return web.json_response({"error": f"Replace failed: {e}"}, status=500)

    out_name = f"replaced_{sanitize(bundle_name)}"
    return web.Response(
        body=out_bytes,
        headers={
            "Content-Type": "application/octet-stream",
            "Content-Disposition": f'attachment; filename="{out_name}"',
            "Content-Length": str(len(out_bytes)),
        },
    )


def create_app() -> web.Application:
    app = web.Application(client_max_size=MAX_BUNDLE_MB * 1024 * 1024 + 20 * 1024 * 1024)
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_post("/api/list", api_list)
    app.router.add_post("/api/replace", api_replace)
    return app


if __name__ == "__main__":
    app = create_app()
    logger.info("Starting on 0.0.0.0:%s", PORT)
    web.run_app(app, host="0.0.0.0", port=PORT, print=None)
