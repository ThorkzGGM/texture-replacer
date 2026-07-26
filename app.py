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
# Many mobile games strip TypeTree / version — set a modern fallback
# Default suits many 32-bit / older mobile Unity games. Override via env.
FALLBACK_UNITY = os.environ.get("FALLBACK_UNITY_VERSION", "2018.4.36f1")

STATIC_DIR = Path(__file__).parent


# ---------------------------------------------------------------------------
# UnityPy helpers
# ---------------------------------------------------------------------------

def _configure_unitypy() -> None:
    try:
        UnityPy.config.FALLBACK_UNITY_VERSION = FALLBACK_UNITY
    except Exception:
        pass


def _load_env(data: bytes):
    """Load bundle bytes with fallbacks for stripped / odd headers."""
    _configure_unitypy()
    # Prefer BytesIO — some UnityPy builds treat raw bytes differently
    try:
        return UnityPy.load(io.BytesIO(data))
    except Exception as e1:
        logger.warning("load(BytesIO) failed: %s — retrying raw bytes", e1)
        return UnityPy.load(data)


def _type_name(obj) -> str:
    t = getattr(obj, "type", None)
    if t is None:
        return ""
    name = getattr(t, "name", None)
    if callable(name):
        try:
            return str(name()) or ""
        except Exception:
            return ""
    if name is not None:
        return str(name)
    return str(t)


def _read_texture_info(obj) -> dict | None:
    """
    Read Texture2D metadata safely.
    Prefer typetree (works when full class parse fails).
    """
    path_id = getattr(obj, "path_id", 0)
    name = f"pathid_{path_id}"
    width = 0
    height = 0

    # 1) typetree first (most resilient)
    try:
        tree = obj.read_typetree()
        if isinstance(tree, dict):
            name = tree.get("m_Name") or name
            width = int(tree.get("m_Width") or 0)
            height = int(tree.get("m_Height") or 0)
            return {"name": name, "width": width, "height": height, "path_id": path_id}
    except Exception as e:
        logger.debug("typetree failed path_id=%s: %s", path_id, e)

    # 2) full object read
    try:
        tex = obj.read()
        name = getattr(tex, "m_Name", None) or getattr(tex, "name", None) or name
        width = int(getattr(tex, "m_Width", 0) or 0)
        height = int(getattr(tex, "m_Height", 0) or 0)
        return {"name": name, "width": width, "height": height, "path_id": path_id}
    except Exception as e:
        logger.debug("read() failed path_id=%s: %s", path_id, e)

    return {"name": name, "width": width, "height": height, "path_id": path_id}


def list_textures(data: bytes) -> list[dict]:
    env = _load_env(data)
    objects = list(env.objects) if env.objects is not None else []
    if not objects:
        # Some bundles only expose objects via files
        for f in getattr(env, "files", {}).values():
            objs = getattr(f, "objects", None)
            if isinstance(objs, dict):
                objects.extend(objs.values())
            elif objs:
                objects.extend(list(objs))

    out = []
    errors = 0
    for obj in objects:
        try:
            if _type_name(obj) != "Texture2D":
                continue
            info = _read_texture_info(obj)
            if info:
                out.append(info)
        except Exception:
            errors += 1
            logger.exception("Skip object")
    if not out and errors:
        raise RuntimeError(
            f"Found objects but failed to parse Texture2D ({errors} errors). "
            f"Try setting FALLBACK_UNITY_VERSION env (current: {FALLBACK_UNITY})."
        )
    return out



def extract_texture_png(data: bytes, path_id: int | None = None, name: str | None = None) -> tuple[bytes, str, int, int]:
    """
    Extract one Texture2D as PNG bytes.
    Match by path_id (preferred) or m_Name (case-insensitive).
    Returns (png_bytes, name, width, height).
    """
    env = _load_env(data)
    objects = list(env.objects) if env.objects is not None else []
    target_name = (name or "").lower()

    for obj in objects:
        if _type_name(obj) != "Texture2D":
            continue
        if path_id is not None and getattr(obj, "path_id", None) != path_id:
            continue

        # name filter if no path_id
        if path_id is None and target_name:
            info = _read_texture_info(obj)
            if not info or str(info["name"]).lower() != target_name:
                continue

        try:
            tex = obj.read()
        except Exception as e:
            raise RuntimeError(f"Could not read Texture2D: {e}") from e

        tex_name = getattr(tex, "m_Name", None) or getattr(tex, "name", None) or f"pathid_{obj.path_id}"
        w = int(getattr(tex, "m_Width", 0) or 0)
        h = int(getattr(tex, "m_Height", 0) or 0)

        try:
            img = tex.image
        except Exception as e:
            raise RuntimeError(f"Could not decode texture image: {e}") from e

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue(), str(tex_name), w, h

    raise ValueError("Texture not found in bundle")


def replace_textures(bundle_bytes: bytes, replacements: dict[str, bytes]) -> bytes:
    """
    replacements: { m_Name_lower: png_bytes }
    Returns modified bundle bytes (lz4).
    """
    env = _load_env(bundle_bytes)
    replaced = []

    objects = list(env.objects) if env.objects is not None else []
    for obj in objects:
        if _type_name(obj) != "Texture2D":
            continue
        try:
            # Need full object for image write
            tex = obj.read()
        except Exception:
            # Fallback: only typetree-known name, skip write
            try:
                tree = obj.read_typetree()
                name = (tree or {}).get("m_Name") or ""
            except Exception:
                name = ""
            if name.lower() in replacements:
                logger.error("Matched %s but obj.read() failed — cannot replace", name)
            continue

        name = getattr(tex, "m_Name", None) or getattr(tex, "name", None) or ""
        key = str(name).lower()
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
            "No textures matched or could be written. "
            "PNG filenames (without extension) must match Texture2D m_Name."
        )

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



async def api_extract(request: web.Request) -> web.Response:
    """
    POST multipart:
      - bundle: asset bundle
      - path_id: optional int
      - name: optional texture m_Name
    Returns PNG image.
    """
    reader = await request.multipart()
    bundle_bytes = None
    path_id = None
    name = None

    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name == "bundle":
            bundle_bytes = await part.read(decode=False)
        elif part.name == "path_id":
            raw = (await part.read(decode=False)).decode("utf-8", errors="ignore").strip()
            if raw:
                path_id = int(raw)
        elif part.name == "name":
            name = (await part.read(decode=False)).decode("utf-8", errors="ignore").strip() or None

    if not bundle_bytes:
        return web.json_response({"error": "No bundle uploaded"}, status=400)
    if path_id is None and not name:
        return web.json_response({"error": "Provide path_id or name"}, status=400)

    size_mb = len(bundle_bytes) / (1024 * 1024)
    if size_mb > MAX_BUNDLE_MB:
        return web.json_response({"error": f"Bundle too large ({size_mb:.1f} MB)"}, status=400)

    try:
        png, tex_name, w, h = extract_texture_png(bundle_bytes, path_id=path_id, name=name)
    except Exception as e:
        logger.exception("Extract failed")
        return web.json_response({"error": str(e)}, status=400)

    safe = sanitize(tex_name) + ".png"
    return web.Response(
        body=png,
        headers={
            "Content-Type": "image/png",
            "Content-Disposition": f'inline; filename="{safe}"',
            "X-Texture-Name": tex_name,
            "X-Texture-Width": str(w),
            "X-Texture-Height": str(h),
        },
    )


def create_app() -> web.Application:
    app = web.Application(client_max_size=MAX_BUNDLE_MB * 1024 * 1024 + 20 * 1024 * 1024)
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_post("/api/list", api_list)
    app.router.add_post("/api/extract", api_extract)
    app.router.add_post("/api/replace", api_replace)
    return app


if __name__ == "__main__":
    app = create_app()
    logger.info("Starting on 0.0.0.0:%s", PORT)
    web.run_app(app, host="0.0.0.0", port=PORT, print=None)
