"""
Unity Asset Bundle Replacer — Web Service
=========================================
Session-based replace for:
  - Texture2D  (PNG / images)
  - TextAsset  (txt / any text / raw bytes)
  - AudioClip  (mp3 / wav / ogg raw data when supported)

Flow: upload → list → open asset → replace in session → Build & download
"""

from __future__ import annotations

import base64
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
MAX_FILE_MB = int(os.environ.get("MAX_TEXTURE_MB", os.environ.get("MAX_FILE_MB", "30")))
PORT = int(os.environ.get("PORT", "8080"))
FALLBACK_UNITY = os.environ.get("FALLBACK_UNITY_VERSION", "2018.4.36f1")
SESSION_TTL_SEC = int(os.environ.get("SESSION_TTL_SEC", "3600"))
STATIC_DIR = Path(__file__).parent

SUPPORTED_TYPES = {"Texture2D", "TextAsset", "AudioClip"}

SESSIONS: dict[str, dict] = {}


def _purge_old_sessions() -> None:
    now = time.time()
    for k in [k for k, v in SESSIONS.items() if now - v.get("created", now) > SESSION_TTL_SEC]:
        SESSIONS.pop(k, None)


def _configure_unitypy() -> None:
    try:
        UnityPy.config.FALLBACK_UNITY_VERSION = FALLBACK_UNITY
    except Exception:
        pass


def _save_env_bytes(env) -> bytes:
    from UnityPy.files import BundleFile

    files = list(getattr(env, "files", {}).items())
    bundles, other = [], []
    for fname, fobj in files:
        sig = getattr(fobj, "signature", None)
        if isinstance(fobj, BundleFile) or sig in ("UnityFS", "UnityWeb", "UnityRaw", "UnityArchive"):
            bundles.append((fname, fobj))
        else:
            other.append((fname, fobj))

    last_err = None
    for fname, fobj in bundles + other:
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


def _all_objects(env):
    objects = list(env.objects) if env.objects is not None else []
    if not objects:
        for f in getattr(env, "files", {}).values():
            objs = getattr(f, "objects", None)
            if isinstance(objs, dict):
                objects.extend(objs.values())
            elif objs:
                objects.extend(list(objs))
    return objects


def _asset_info(obj) -> dict | None:
    tname = _type_name(obj)
    if tname not in SUPPORTED_TYPES:
        return None
    path_id = str(getattr(obj, "path_id", 0))
    name = f"pathid_{path_id}"
    extra: dict = {"type": tname, "path_id": path_id, "width": 0, "height": 0, "size": 0}

    try:
        tree = obj.read_typetree()
        if isinstance(tree, dict):
            name = tree.get("m_Name") or name
            if tname == "Texture2D":
                extra["width"] = int(tree.get("m_Width") or 0)
                extra["height"] = int(tree.get("m_Height") or 0)
            if tname == "TextAsset":
                script = tree.get("m_Script")
                if isinstance(script, str):
                    extra["size"] = len(script.encode("utf-8", errors="replace"))
                elif isinstance(script, (bytes, bytearray)):
                    extra["size"] = len(script)
            if tname == "AudioClip":
                extra["size"] = int(tree.get("m_Resource", {}).get("m_Size", 0) or tree.get("m_Size", 0) or 0)
    except Exception:
        pass

    try:
        data = obj.read()
        name = getattr(data, "m_Name", None) or getattr(data, "name", None) or name
        if tname == "Texture2D":
            extra["width"] = int(getattr(data, "m_Width", 0) or 0)
            extra["height"] = int(getattr(data, "m_Height", 0) or 0)
        if tname == "TextAsset":
            script = getattr(data, "m_Script", None)
            if isinstance(script, str):
                extra["size"] = len(script.encode("utf-8", errors="replace"))
            elif isinstance(script, (bytes, bytearray)):
                extra["size"] = len(script)
        if tname == "AudioClip":
            samples = getattr(data, "samples", None)
            if isinstance(samples, dict) and samples:
                extra["size"] = sum(len(v) for v in samples.values() if isinstance(v, (bytes, bytearray)))
    except Exception:
        pass

    extra["name"] = str(name)
    return extra


def list_assets(data: bytes) -> list[dict]:
    env = _load_env(data)
    out = []
    for obj in _all_objects(env):
        try:
            info = _asset_info(obj)
            if info:
                out.append(info)
        except Exception:
            logger.exception("Skip object")
    # stable sort: type then name
    out.sort(key=lambda x: (x["type"], x["name"].lower()))
    return out


def _find_obj(env, path_id: str | None, name: str | None, type_name: str | None = None):
    target = (name or "").lower()
    pid = str(path_id) if path_id is not None else None
    for obj in _all_objects(env):
        tname = _type_name(obj)
        if type_name and tname != type_name:
            continue
        if tname not in SUPPORTED_TYPES:
            continue
        obj_pid = str(getattr(obj, "path_id", ""))
        if pid is not None and pid == obj_pid:
            return obj, tname
        if target:
            info = _asset_info(obj)
            if info and info["name"].lower() == target:
                if type_name is None or info["type"] == type_name:
                    return obj, tname
    return None, None


def extract_asset(
    data: bytes, path_id: str | None = None, name: str | None = None, type_name: str | None = None
) -> dict:
    """
    Returns dict with keys:
      kind: texture|text|audio
      name, width, height
      png_bytes | text | audio_bytes, audio_ext
    """
    env = _load_env(data)
    obj, tname = _find_obj(env, path_id, name, type_name)
    if obj is None:
        raise ValueError("Asset not found in bundle")

    data_obj = obj.read()
    aname = getattr(data_obj, "m_Name", None) or getattr(data_obj, "name", None) or name or "asset"

    if tname == "Texture2D":
        img = data_obj.image
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return {
            "kind": "texture",
            "name": str(aname),
            "width": int(getattr(data_obj, "m_Width", 0) or 0),
            "height": int(getattr(data_obj, "m_Height", 0) or 0),
            "png_bytes": buf.getvalue(),
        }

    if tname == "TextAsset":
        script = getattr(data_obj, "m_Script", b"")
        if isinstance(script, str):
            text = script
            raw = script.encode("utf-8")
        else:
            raw = bytes(script or b"")
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = None
        return {
            "kind": "text",
            "name": str(aname),
            "text": text,
            "raw_bytes": raw,
            "is_binary": text is None,
        }

    if tname == "AudioClip":
        samples = getattr(data_obj, "samples", None) or {}
        # samples: dict name -> bytes
        if not samples:
            # fallback raw m_AudioData
            audio_data = getattr(data_obj, "m_AudioData", None)
            if audio_data:
                raw = bytes(audio_data) if not isinstance(audio_data, (bytes, bytearray)) else bytes(audio_data)
                return {
                    "kind": "audio",
                    "name": str(aname),
                    "audio_bytes": raw,
                    "audio_ext": "bin",
                }
            raise RuntimeError("AudioClip has no extractable samples")
        # pick first sample
        sname, sdata = next(iter(samples.items()))
        ext = Path(str(sname)).suffix.lstrip(".") or getattr(data_obj, "extension", None) or "wav"
        return {
            "kind": "audio",
            "name": str(aname),
            "audio_bytes": bytes(sdata),
            "audio_ext": str(ext).lstrip("."),
            "sample_name": str(sname),
        }

    raise ValueError(f"Unsupported type: {tname}")


def replace_assets(bundle_bytes: bytes, replacements: list[dict]) -> tuple[bytes, list[str]]:
    """
    replacements: list of {name, type?, data: bytes}
    name matched case-insensitive to m_Name.
    """
    env = _load_env(bundle_bytes)
    replaced: list[str] = []
    lookup = {}
    for r in replacements:
        key = (r.get("type") or "", str(r["name"]).lower())
        lookup[key] = r
        # also name-only key
        lookup[("", str(r["name"]).lower())] = r

    for obj in _all_objects(env):
        tname = _type_name(obj)
        if tname not in SUPPORTED_TYPES:
            continue
        try:
            data_obj = obj.read()
        except Exception:
            continue
        aname = getattr(data_obj, "m_Name", None) or getattr(data_obj, "name", None) or ""
        key_typed = (tname, str(aname).lower())
        key_any = ("", str(aname).lower())
        rep = lookup.get(key_typed) or lookup.get(key_any)
        if not rep:
            continue
        raw = rep["data"]
        try:
            if tname == "Texture2D":
                img = Image.open(io.BytesIO(raw)).convert("RGBA")
                if hasattr(data_obj, "set_image"):
                    data_obj.set_image(img)
                else:
                    data_obj.image = img
            elif tname == "TextAsset":
                # Prefer string if UTF-8 text; else bytes
                try:
                    text = raw.decode("utf-8")
                    data_obj.m_Script = text
                except UnicodeDecodeError:
                    data_obj.m_Script = raw
            elif tname == "AudioClip":
                # Best-effort: set samples dict if present
                samples = getattr(data_obj, "samples", None)
                if isinstance(samples, dict) and samples:
                    first_key = next(iter(samples.keys()))
                    samples[first_key] = raw
                    data_obj.samples = samples
                else:
                    data_obj.m_AudioData = list(raw) if not isinstance(raw, list) else raw
            else:
                continue

            data_obj.save()
            try:
                reader = getattr(data_obj, "object_reader", None) or obj
                assets = getattr(reader, "assets_file", None)
                if assets is not None and hasattr(assets, "mark_changed"):
                    assets.mark_changed()
            except Exception:
                pass
            replaced.append(f"{tname}:{aname}")
            logger.info("Replaced %s %s", tname, aname)
        except Exception:
            logger.exception("Failed replace %s %s", tname, aname)

    if not replaced:
        raise ValueError("No matching assets replaced. Check names/types.")
    out = _save_env_bytes(env)
    if len(out) < max(1024, int(len(bundle_bytes) * 0.05)):
        raise RuntimeError(
            f"Saved output looks truncated ({len(out)} vs original {len(bundle_bytes)} bytes)."
        )
    return out, replaced


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
        return web.json_response({"error": f"Bundle too large ({size_mb:.1f} MB)"}, status=400)
    try:
        assets = list_assets(bundle_bytes)
    except Exception as e:
        logger.exception("Parse failed")
        return web.json_response({"error": f"Could not parse AssetBundle: {e}"}, status=400)

    sid = secrets.token_urlsafe(16)
    SESSIONS[sid] = {
        "bundle_bytes": bundle_bytes,
        "original_size": len(bundle_bytes),
        "filename": filename,
        "replaced": [],
        "created": time.time(),
    }
    counts = {}
    for a in assets:
        counts[a["type"]] = counts.get(a["type"], 0) + 1
    return web.json_response(
        {
            "session_id": sid,
            "filename": filename,
            "size_mb": round(size_mb, 2),
            "assets": assets,
            "count": len(assets),
            "counts": counts,
            "replaced": [],
        }
    )


async def api_extract(request: web.Request) -> web.Response:
    reader = await request.multipart()
    sid = path_id = name = type_name = None
    while True:
        part = await reader.next()
        if part is None:
            break
        val = (await part.read(decode=False)).decode("utf-8", errors="ignore").strip()
        if part.name == "session_id":
            sid = val
        elif part.name == "path_id":
            path_id = val or None
        elif part.name == "name":
            name = val or None
        elif part.name == "type":
            type_name = val or None
    sess = SESSIONS.get(sid or "")
    if not sess:
        return web.json_response({"error": "Session expired — re-upload bundle"}, status=400)
    try:
        result = extract_asset(sess["bundle_bytes"], path_id=path_id, name=name, type_name=type_name)
    except Exception as e:
        logger.exception("Extract failed")
        return web.json_response({"error": str(e)}, status=400)

    kind = result["kind"]
    if kind == "texture":
        return web.Response(
            body=result["png_bytes"],
            headers={
                "Content-Type": "image/png",
                "Content-Disposition": f'inline; filename="{sanitize(result["name"])}.png"',
                "X-Asset-Kind": "texture",
                "X-Asset-Name": result["name"],
                "X-Texture-Width": str(result["width"]),
                "X-Texture-Height": str(result["height"]),
            },
        )
    if kind == "text":
        payload = {
            "kind": "text",
            "name": result["name"],
            "is_binary": result["is_binary"],
            "text": result["text"],
            "size": len(result["raw_bytes"]),
            "raw_base64": base64.b64encode(result["raw_bytes"]).decode("ascii")
            if result["is_binary"]
            else None,
        }
        return web.json_response(payload)
    if kind == "audio":
        ext = result.get("audio_ext") or "bin"
        mime = {
            "mp3": "audio/mpeg",
            "wav": "audio/wav",
            "ogg": "audio/ogg",
            "fsb": "application/octet-stream",
        }.get(ext.lower(), "application/octet-stream")
        return web.Response(
            body=result["audio_bytes"],
            headers={
                "Content-Type": mime,
                "Content-Disposition": f'inline; filename="{sanitize(result["name"])}.{ext}"',
                "X-Asset-Kind": "audio",
                "X-Asset-Name": result["name"],
                "X-Audio-Ext": ext,
            },
        )
    return web.json_response({"error": "unknown kind"}, status=500)


async def api_replace(request: web.Request) -> web.Response:
    reader = await request.multipart()
    sid = name = type_name = None
    file_bytes = None
    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name == "session_id":
            sid = (await part.read(decode=False)).decode().strip()
        elif part.name == "name":
            name = (await part.read(decode=False)).decode().strip()
        elif part.name == "type":
            type_name = (await part.read(decode=False)).decode().strip() or None
        elif part.name in ("file", "texture", "textures", "png", "text", "audio"):
            file_bytes = await part.read(decode=False)
            if not name and part.filename:
                name = Path(part.filename).stem

    sess = SESSIONS.get(sid or "")
    if not sess:
        return web.json_response({"error": "Session expired — re-upload bundle"}, status=400)
    if not file_bytes or not name:
        return web.json_response({"error": "Need asset name + file"}, status=400)
    if len(file_bytes) / (1024 * 1024) > MAX_FILE_MB:
        return web.json_response({"error": "File too large"}, status=400)

    try:
        new_bytes, just = replace_assets(
            sess["bundle_bytes"],
            [{"name": name, "type": type_name, "data": file_bytes}],
        )
    except Exception as e:
        logger.exception("Replace failed")
        return web.json_response({"error": str(e)}, status=400)

    sess["bundle_bytes"] = new_bytes
    for item in just:
        # item is "Type:name"
        label = item.split(":", 1)[-1]
        if label not in sess["replaced"]:
            sess["replaced"].append(label)
    sess["created"] = time.time()

    preview = None
    try:
        result = extract_asset(new_bytes, name=name, type_name=type_name)
        if result["kind"] == "texture":
            preview = {
                "kind": "texture",
                "png_base64": base64.b64encode(result["png_bytes"]).decode("ascii"),
                "width": result["width"],
                "height": result["height"],
            }
        elif result["kind"] == "text":
            preview = {
                "kind": "text",
                "text": result["text"],
                "is_binary": result["is_binary"],
                "size": len(result["raw_bytes"]),
            }
        elif result["kind"] == "audio":
            preview = {
                "kind": "audio",
                "audio_base64": base64.b64encode(result["audio_bytes"]).decode("ascii"),
                "audio_ext": result.get("audio_ext"),
                "size": len(result["audio_bytes"]),
            }
    except Exception as e:
        logger.warning("Preview after replace failed: %s", e)

    return web.json_response(
        {
            "ok": True,
            "replaced_name": name,
            "replaced": sess["replaced"],
            "bundle_size": len(new_bytes),
            "bundle_size_mb": round(len(new_bytes) / (1024 * 1024), 2),
            "preview": preview,
        }
    )


async def api_build(request: web.Request) -> web.Response:
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
    logger.info("Build: %s bytes, replaced=%s", len(data), sess["replaced"])
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


def create_app() -> web.Application:
    max_size = MAX_BUNDLE_MB * 1024 * 1024 + 40 * 1024 * 1024
    app = web.Application(client_max_size=max_size)
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_post("/api/upload", api_upload)
    app.router.add_post("/api/extract", api_extract)
    app.router.add_post("/api/replace", api_replace)
    app.router.add_post("/api/build", api_build)
    return app


if __name__ == "__main__":
    logger.info("Starting on 0.0.0.0:%s", PORT)
    web.run_app(create_app(), host="0.0.0.0", port=PORT, print=None)
