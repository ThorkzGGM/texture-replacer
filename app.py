"""
Unity Asset Bundle Texture Replacer — Web Service
=================================================
Session-based: upload → list → click texture → extract/replace (in session)
→ Build & download full bundle when done.

Env: PORT, MAX_BUNDLE_MB, MAX_TEXTURE_MB, FALLBACK_UNITY_VERSION
"""

from __future__ import annotations

import io
import logging
import os
import re
import secrets
import tempfile
import time
from pathlib import Path

import UnityPy
from aiohttp import web
from PIL import Image

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

MAX_BUNDLE_MB = int(os.environ.get("MAX_BUNDLE_MB", "80"))
MAX_TEXTURE_MB = int(os.environ.get("MAX_TEXTURE_MB", "20"))
PORT = int(os.environ.get("PORT", "8080"))
FALLBACK_UNITY = os.environ.get("FALLBACK_UNITY_VERSION", "2018.4.36f1")
SESSION_TTL_SEC = int(os.environ.get("SESSION_TTL_SEC", "3600"))
STATIC_DIR = Path(__file__).parent

# session_id -> {bundle_bytes, filename, replaced: [names], created}
SESSIONS: dict[str, dict] = {}


def _purge_old_sessions() -> None:
    now = time.time()
    dead = [k for k, v in SESSIONS.items() if now - v.get("created", now) > SESSION_TTL_SEC]
    for k in dead:
        SESSIONS.pop(k, None)


# ---------------------------------------------------------------------------
# UnityPy helpers
# ---------------------------------------------------------------------------

def _configure_unitypy() -> None:
    try:
        UnityPy.config.FALLBACK_UNITY_VERSION = FALLBACK_UNITY
    except Exception:
        pass


def _save_env_bytes(env) -> bytes:
    """Serialize the full AssetBundle as bytes (not an inner CAB)."""
    from UnityPy.files import BundleFile

    files = list(getattr(env, "files", {}).items())
    bundle_candidates, other = [], []
    for fname, fobj in files:
        sig = getattr(fobj, "signature", None)
        if isinstance(fobj, BundleFile) or sig in ("UnityFS", "UnityWeb", "UnityRaw", "UnityArchive"):
            bundle_candidates.append((fname, fobj))
        else:
            other.append((fname, fobj))

    last_err = None
    for fname, fobj in bundle_candidates + other:
        save = getattr(fobj, "save", None)
        if not callable(save):
            continue
        for packer in ("original", "lz4", "none"):
            try:
                data = save(packer=packer)
            except TypeError:
                try:
                    data = save()
                except Exception as e:
                    last_err = e
                    continue
            except Exception as e:
                last_err = e
                continue
            if isinstance(data, (bytes, bytearray)) and len(data) > 64:
                logger.info("Saved %s packer=%s size=%s", type(fobj).__name__, packer, len(data))
                return bytes(data)

    tmp_dir = tempfile.mkdtemp(prefix="utr_out_")
    try:
        try:
            env.save(pack="original", out_path=tmp_dir)
        except Exception:
            env.save(pack="lz4", out_path=tmp_dir)
        produced = sorted(
            [f for f in Path(tmp_dir).rglob("*") if f.is_file()],
            key=lambda f: f.stat().st_size,
            reverse=True,
        )
        if not produced:
            raise RuntimeError(f"env.save produced nothing ({last_err})")
        data = produced[0].read_bytes()
        if len(data) < 128:
            raise RuntimeError(f"Output too small ({len(data)} bytes)")
        return data
    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _load_env(data: bytes):
    _configure_unitypy()
    try:
        return UnityPy.load(io.BytesIO(data))
    except Exception as e1:
        logger.warning("load(BytesIO) failed: %s", e1)
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
    path_id = getattr(obj, "path_id", 0)
    name = f"pathid_{path_id}"
    width = height = 0
    try:
        tree = obj.read_typetree()
        if isinstance(tree, dict):
            name = tree.get("m_Name") or name
            width = int(tree.get("m_Width") or 0)
            height = int(tree.get("m_Height") or 0)
            return {"name": name, "width": width, "height": height, "path_id": str(path_id)}
    except Exception:
        pass
    try:
        tex = obj.read()
        name = getattr(tex, "m_Name", None) or getattr(tex, "name", None) or name
        width = int(getattr(tex, "m_Width", 0) or 0)
        height = int(getattr(tex, "m_Height", 0) or 0)
    except Exception:
        pass
    return {"name": name, "width": width, "height": height, "path_id": str(path_id)}


def list_textures(data: bytes) -> list[dict]:
    env = _load_env(data)
    objects = list(env.objects) if env.objects is not None else []
    if not objects:
        for f in getattr(env, "files", {}).values():
            objs = getattr(f, "objects", None)
            if isinstance(objs, dict):
                objects.extend(objs.values())
            elif objs:
                objects.extend(list(objs))
    out = []
    for obj in objects:
        try:
            if _type_name(obj) != "Texture2D":
                continue
            info = _read_texture_info(obj)
            if info:
                out.append(info)
        except Exception:
            logger.exception("Skip object")
    return out


def extract_texture_png(
    data: bytes, path_id: str | int | None = None, name: str | None = None
) -> tuple[bytes, str, int, int]:
    env = _load_env(data)
    objects = list(env.objects) if env.objects is not None else []
    target_name = (name or "").lower()
    pid = str(path_id) if path_id is not None else None

    for obj in objects:
        if _type_name(obj) != "Texture2D":
            continue
        obj_pid = str(getattr(obj, "path_id", ""))
        matched = False
        if pid is not None and pid == obj_pid:
            matched = True
        if not matched and target_name:
            info = _read_texture_info(obj)
            if info and str(info["name"]).lower() == target_name:
                matched = True
        if not matched:
            continue
        try:
            tex = obj.read()
        except Exception as e:
            raise RuntimeError(f"Could not read Texture2D: {e}") from e
        tex_name = getattr(tex, "m_Name", None) or getattr(tex, "name", None) or f"pathid_{obj_pid}"
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
    env = _load_env(bundle_bytes)
    replaced = []
    objects = list(env.objects) if env.objects is not None else []
    for obj in objects:
        if _type_name(obj) != "Texture2D":
            continue
        try:
            tex = obj.read()
        except Exception:
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
            try:
                reader = getattr(tex, "object_reader", None) or obj
                assets = getattr(reader, "assets_file", None)
                if assets is not None and hasattr(assets, "mark_changed"):
                    assets.mark_changed()
            except Exception:
                pass
            replaced.append(str(name))
            logger.info("Replaced: %s", name)
        except Exception:
            logger.exception("Failed to replace %s", name)

    if not replaced:
        raise ValueError(
            "No textures matched. PNG must match Texture2D m_Name (case-insensitive)."
        )
    out = _save_env_bytes(env)
    if len(out) < max(1024, int(len(bundle_bytes) * 0.05)):
        raise RuntimeError(
            f"Saved output looks truncated ({len(out)} vs original {len(bundle_bytes)} bytes)."
        )
    return out


def sanitize(name: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", name)[:120]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

async def index(request: web.Request) -> web.Response:
    return web.FileResponse(STATIC_DIR / "index.html")


async def health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "sessions": len(SESSIONS)})


async def api_upload(request: web.Request) -> web.Response:
    """Upload bundle → create session, return textures."""
    _purge_old_sessions()
    reader = await request.multipart()
    bundle_bytes = None
    filename = "bundle.bundle"
    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name == "bundle":
            filename = part.filename or filename
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
            {"error": f"Could not parse as Unity AssetBundle: {e}"}, status=400
        )

    sid = secrets.token_urlsafe(16)
    SESSIONS[sid] = {
        "bundle_bytes": bundle_bytes,
        "original_size": len(bundle_bytes),
        "filename": filename,
        "replaced": [],
        "created": time.time(),
    }
    return web.json_response(
        {
            "session_id": sid,
            "filename": filename,
            "size_mb": round(size_mb, 2),
            "textures": textures,
            "count": len(textures),
            "replaced": [],
        }
    )


async def api_extract(request: web.Request) -> web.Response:
    """Extract texture PNG from current session bundle."""
    reader = await request.multipart()
    sid = path_id = name = None
    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name == "session_id":
            sid = (await part.read(decode=False)).decode().strip()
        elif part.name == "path_id":
            path_id = (await part.read(decode=False)).decode().strip() or None
        elif part.name == "name":
            name = (await part.read(decode=False)).decode().strip() or None
    sess = SESSIONS.get(sid or "")
    if not sess:
        return web.json_response({"error": "Session expired — re-upload bundle"}, status=400)
    try:
        png, tex_name, w, h = extract_texture_png(
            sess["bundle_bytes"], path_id=path_id, name=name
        )
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


async def api_replace(request: web.Request) -> web.Response:
    """Replace texture in session (NO download). Updates session bundle in memory."""
    reader = await request.multipart()
    sid = None
    tex_name = None
    png_bytes = None
    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name == "session_id":
            sid = (await part.read(decode=False)).decode().strip()
        elif part.name == "name":
            tex_name = (await part.read(decode=False)).decode().strip()
        elif part.name in ("texture", "textures", "png", "file"):
            png_bytes = await part.read(decode=False)
            if not tex_name and part.filename:
                tex_name = Path(part.filename).stem

    sess = SESSIONS.get(sid or "")
    if not sess:
        return web.json_response({"error": "Session expired — re-upload bundle"}, status=400)
    if not png_bytes or not tex_name:
        return web.json_response({"error": "Need texture name + PNG"}, status=400)
    if len(png_bytes) / (1024 * 1024) > MAX_TEXTURE_MB:
        return web.json_response({"error": "PNG too large"}, status=400)

    try:
        new_bytes = replace_textures(
            sess["bundle_bytes"], {tex_name.lower(): png_bytes}
        )
    except Exception as e:
        logger.exception("Replace failed")
        return web.json_response({"error": str(e)}, status=400)

    sess["bundle_bytes"] = new_bytes
    if tex_name not in sess["replaced"]:
        sess["replaced"].append(tex_name)
    sess["created"] = time.time()

    # Return updated preview of that texture
    preview_b64 = None
    try:
        png, _, w, h = extract_texture_png(new_bytes, name=tex_name)
        import base64
        preview_b64 = base64.b64encode(png).decode("ascii")
    except Exception as e:
        logger.warning("Preview after replace failed: %s", e)
        w = h = 0

    return web.json_response(
        {
            "ok": True,
            "replaced_name": tex_name,
            "replaced": sess["replaced"],
            "bundle_size": len(new_bytes),
            "bundle_size_mb": round(len(new_bytes) / (1024 * 1024), 2),
            "preview_png_base64": preview_b64,
            "width": w,
            "height": h,
        }
    )


async def api_build(request: web.Request) -> web.Response:
    """Download the full current session bundle."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    sid = (body.get("session_id") or "").strip()
    sess = SESSIONS.get(sid)
    if not sess:
        return web.json_response({"error": "Session expired — re-upload bundle"}, status=400)

    data = sess["bundle_bytes"]
    out_name = f"replaced_{sanitize(sess['filename'])}"
    logger.info(
        "Build download: %s bytes (%.2f MB), replaced=%s",
        len(data),
        len(data) / (1024 * 1024),
        sess["replaced"],
    )
    return web.Response(
        body=data,
        headers={
            "Content-Type": "application/octet-stream",
            "Content-Disposition": f'attachment; filename="{out_name}"',
            "Content-Length": str(len(data)),
            "X-Original-Size": str(sess["original_size"]),
            "X-Output-Size": str(len(data)),
            "X-Replaced-Count": str(len(sess["replaced"])),
        },
    )


async def api_status(request: web.Request) -> web.Response:
    sid = request.rel_url.query.get("session_id", "")
    sess = SESSIONS.get(sid)
    if not sess:
        return web.json_response({"error": "no session"}, status=404)
    return web.json_response(
        {
            "filename": sess["filename"],
            "size": len(sess["bundle_bytes"]),
            "original_size": sess["original_size"],
            "replaced": sess["replaced"],
        }
    )


def create_app() -> web.Application:
    max_size = MAX_BUNDLE_MB * 1024 * 1024 + 30 * 1024 * 1024
    app = web.Application(client_max_size=max_size)
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_post("/api/upload", api_upload)
    app.router.add_post("/api/extract", api_extract)
    app.router.add_post("/api/replace", api_replace)
    app.router.add_post("/api/build", api_build)
    app.router.add_get("/api/status", api_status)
    return app


if __name__ == "__main__":
    logger.info("Starting on 0.0.0.0:%s (fallback Unity %s)", PORT, FALLBACK_UNITY)
    web.run_app(create_app(), host="0.0.0.0", port=PORT, print=None)
