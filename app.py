"""
unity-assets-replacer-notuabe
Local tool for Termux / PC. Replace textures and text in Unity .bundle files.
Export meshes and textures as a zip. No audio. No cloud required.
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
import zipfile
from pathlib import Path

import UnityPy
from aiohttp import web
from PIL import Image

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("notuabe")

MAX_BUNDLE_MB = int(os.environ.get("MAX_BUNDLE_MB", "80"))
MAX_FILE_MB = int(os.environ.get("MAX_FILE_MB", "30"))
PORT = int(os.environ.get("PORT", "8080"))
FALLBACK_UNITY = os.environ.get("FALLBACK_UNITY_VERSION", "2018.4.36f1")
SESSION_TTL = int(os.environ.get("SESSION_TTL_SEC", "3600"))
STATIC = Path(__file__).parent

SUPPORTED = {"Texture2D", "TextAsset", "Mesh", "Material", "Sprite"}
SESSIONS: dict[str, dict] = {}


def purge():
    now = time.time()
    for k in [k for k, v in SESSIONS.items() if now - v.get("created", now) > SESSION_TTL]:
        SESSIONS.pop(k, None)


def cfg_unity():
    try:
        UnityPy.config.FALLBACK_UNITY_VERSION = FALLBACK_UNITY
    except Exception:
        pass


def load_env(data: bytes):
    cfg_unity()
    try:
        return UnityPy.load(io.BytesIO(data))
    except Exception:
        return UnityPy.load(data)


def type_name(obj) -> str:
    t = getattr(obj, "type", None)
    if t is None:
        return ""
    n = getattr(t, "name", None)
    if callable(n):
        try:
            return str(n()) or ""
        except Exception:
            return ""
    return str(n) if n is not None else str(t)


def all_objects(env):
    objs = list(env.objects) if env.objects is not None else []
    if not objs:
        for f in getattr(env, "files", {}).values():
            o = getattr(f, "objects", None)
            if isinstance(o, dict):
                objs.extend(o.values())
            elif o:
                objs.extend(list(o))
    return objs


def save_env_bytes(env) -> bytes:
    from UnityPy.files import BundleFile

    files = list(getattr(env, "files", {}).items())
    bundles, other = [], []
    for fname, fobj in files:
        sig = getattr(fobj, "signature", None)
        if isinstance(fobj, BundleFile) or sig in ("UnityFS", "UnityWeb", "UnityRaw", "UnityArchive"):
            bundles.append((fname, fobj))
        else:
            other.append((fname, fobj))
    last = None
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
                    last = e
                    continue
            except Exception as e:
                last = e
                continue
            if isinstance(data, (bytes, bytearray)) and len(data) > 64:
                return bytes(data)
    tmp = tempfile.mkdtemp(prefix="notuabe_")
    try:
        try:
            env.save(pack="original", out_path=tmp)
        except Exception:
            env.save(pack="lz4", out_path=tmp)
        produced = sorted(
            [f for f in Path(tmp).rglob("*") if f.is_file()],
            key=lambda f: f.stat().st_size,
            reverse=True,
        )
        if not produced:
            raise RuntimeError(f"save failed ({last})")
        data = produced[0].read_bytes()
        if len(data) < 128:
            raise RuntimeError("output too small")
        return data
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def asset_info(obj):
    tname = type_name(obj)
    if tname not in SUPPORTED:
        return None
    path_id = str(getattr(obj, "path_id", 0))
    name = f"pathid_{path_id}"
    extra = {"type": tname, "path_id": path_id, "width": 0, "height": 0, "size": 0}
    try:
        tree = obj.read_typetree()
        if isinstance(tree, dict):
            name = tree.get("m_Name") or name
            if tname in ("Texture2D", "Sprite"):
                extra["width"] = int(tree.get("m_Width") or 0)
                extra["height"] = int(tree.get("m_Height") or 0)
            if tname == "TextAsset":
                s = tree.get("m_Script")
                if isinstance(s, str):
                    extra["size"] = len(s.encode("utf-8", errors="replace"))
                elif isinstance(s, (bytes, bytearray)):
                    extra["size"] = len(s)
            if tname == "Mesh":
                extra["size"] = int(tree.get("m_VertexCount") or 0)
    except Exception:
        pass
    try:
        d = obj.read()
        name = getattr(d, "m_Name", None) or getattr(d, "name", None) or name
        if tname in ("Texture2D", "Sprite"):
            extra["width"] = int(getattr(d, "m_Width", 0) or 0)
            extra["height"] = int(getattr(d, "m_Height", 0) or 0)
        if tname == "TextAsset":
            s = getattr(d, "m_Script", None)
            if isinstance(s, str):
                extra["size"] = len(s.encode("utf-8", errors="replace"))
            elif isinstance(s, (bytes, bytearray)):
                extra["size"] = len(s)
    except Exception:
        pass
    extra["name"] = str(name)
    return extra


def list_assets(data: bytes):
    env = load_env(data)
    out = []
    for obj in all_objects(env):
        try:
            info = asset_info(obj)
            if info:
                out.append(info)
        except Exception:
            pass
    out.sort(key=lambda x: (x["type"], x["name"].lower()))
    return out


def find_obj(env, path_id=None, name=None, type_name_filter=None):
    target = (name or "").lower()
    pid = str(path_id) if path_id is not None else None
    for obj in all_objects(env):
        tname = type_name(obj)
        if type_name_filter and tname != type_name_filter:
            continue
        if tname not in SUPPORTED:
            continue
        if pid is not None and pid == str(getattr(obj, "path_id", "")):
            return obj, tname
        if target:
            info = asset_info(obj)
            if info and info["name"].lower() == target:
                return obj, tname
    return None, None


def extract_asset(data: bytes, path_id=None, name=None, type_name_filter=None):
    env = load_env(data)
    obj, tname = find_obj(env, path_id, name, type_name_filter)
    if obj is None:
        raise ValueError("Asset not found")
    d = obj.read()
    aname = getattr(d, "m_Name", None) or getattr(d, "name", None) or name or "asset"
    if tname in ("Texture2D", "Sprite"):
        img = d.image
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return {
            "kind": "texture",
            "name": str(aname),
            "width": int(getattr(d, "m_Width", 0) or 0),
            "height": int(getattr(d, "m_Height", 0) or 0),
            "png_bytes": buf.getvalue(),
        }
    if tname == "TextAsset":
        script = getattr(d, "m_Script", b"")
        if isinstance(script, str):
            text, raw = script, script.encode("utf-8")
        else:
            raw = bytes(script or b"")
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = None
        return {"kind": "text", "name": str(aname), "text": text, "raw_bytes": raw, "is_binary": text is None}
    if tname == "Mesh":
        obj_txt = d.export(format="obj")
        raw = obj_txt if isinstance(obj_txt, bytes) else str(obj_txt).encode("utf-8")
        return {"kind": "mesh", "name": str(aname), "obj_bytes": raw}
    raise ValueError(f"Cannot extract type: {tname}")


def replace_assets(bundle_bytes: bytes, replacements: list) -> tuple:
    env = load_env(bundle_bytes)
    replaced = []
    by_pid, by_tn, by_n = {}, {}, {}
    for r in replacements:
        if r.get("path_id"):
            by_pid[str(r["path_id"])] = r
        n = str(r.get("name") or "").lower()
        if n:
            by_n[n] = r
            by_tn[(str(r.get("type") or ""), n)] = r

    for obj in all_objects(env):
        tname = type_name(obj)
        if tname not in SUPPORTED:
            continue
        obj_pid = str(getattr(obj, "path_id", ""))
        rep = by_pid.get(obj_pid)
        aname = None
        if rep is None:
            try:
                tree = obj.read_typetree()
                if isinstance(tree, dict):
                    aname = tree.get("m_Name")
            except Exception:
                pass
            if not aname:
                try:
                    tmp = obj.read()
                    aname = getattr(tmp, "m_Name", None) or getattr(tmp, "name", None)
                except Exception:
                    continue
            key = str(aname).lower()
            rep = by_tn.get((tname, key)) or by_n.get(key)
        if not rep:
            continue
        raw = rep["data"]
        try:
            data_obj = obj.read()
        except Exception:
            continue
        aname = getattr(data_obj, "m_Name", None) or getattr(data_obj, "name", None) or aname or rep.get("name")
        try:
            if tname in ("Texture2D", "Sprite"):
                img = Image.open(io.BytesIO(raw)).convert("RGBA")
                if hasattr(data_obj, "set_image"):
                    data_obj.set_image(img)
                else:
                    data_obj.image = img
            elif tname == "TextAsset":
                try:
                    data_obj.m_Script = raw.decode("utf-8")
                except UnicodeDecodeError:
                    data_obj.m_Script = raw
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
            log.info("Replaced %s %s", tname, aname)
        except Exception:
            log.exception("replace failed %s", aname)

    if not replaced:
        raise ValueError("No matching assets. Check names or path_id.")
    out = save_env_bytes(env)
    if len(out) < max(1024, int(len(bundle_bytes) * 0.05)):
        raise RuntimeError(f"Save too small ({len(out)} vs {len(bundle_bytes)})")
    return out, replaced


def export_models_zip(data: bytes) -> bytes:
    env = load_env(data)
    buf = io.BytesIO()
    count = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        used = set()
        for obj in all_objects(env):
            tname = type_name(obj)
            try:
                if tname == "Mesh":
                    d = obj.read()
                    name = getattr(d, "m_Name", None) or f"mesh_{obj.path_id}"
                    safe = re.sub(r"[^\w.\-]+", "_", str(name))[:100] or "mesh"
                    key = f"models/{safe}.obj"
                    i = 1
                    while key in used:
                        key = f"models/{safe}_{i}.obj"
                        i += 1
                    used.add(key)
                    obj_data = d.export(format="obj")
                    zf.writestr(key, obj_data if isinstance(obj_data, bytes) else str(obj_data).encode("utf-8"))
                    count += 1
                elif tname in ("Texture2D", "Sprite"):
                    d = obj.read()
                    name = getattr(d, "m_Name", None) or f"tex_{obj.path_id}"
                    safe = re.sub(r"[^\w.\-]+", "_", str(name))[:100] or "tex"
                    key = f"textures/{safe}.png"
                    i = 1
                    while key in used:
                        key = f"textures/{safe}_{i}.png"
                        i += 1
                    used.add(key)
                    pbuf = io.BytesIO()
                    d.image.save(pbuf, format="PNG")
                    zf.writestr(key, pbuf.getvalue())
                    count += 1
            except Exception:
                log.exception("export skip %s", tname)
        zf.writestr(
            "README.txt",
            f"Exported {count} items.\nmodels/*.obj = Mesh\ntextures/*.png = Texture2D / Sprite\n",
        )
    if count == 0:
        raise ValueError("Nothing to export")
    return buf.getvalue()


def sanitize(name: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", name)[:120]


# routes

async def index(request):
    return web.FileResponse(STATIC / "index.html")


async def health(request):
    return web.json_response({"status": "ok", "name": "unity-assets-replacer-notuabe", "sessions": len(SESSIONS)})


async def api_upload(request):
    purge()
    reader = await request.multipart()
    bundle_bytes = None
    filename = "bundle.bundle"
    async for part in reader:
        if part.name == "bundle":
            filename = part.filename or filename
            bundle_bytes = await part.read(decode=False)
    if not bundle_bytes:
        return web.json_response({"error": "No bundle"}, status=400)
    if len(bundle_bytes) / (1024 * 1024) > MAX_BUNDLE_MB:
        return web.json_response({"error": "Bundle too large"}, status=400)
    try:
        assets = list_assets(bundle_bytes)
    except Exception as e:
        log.exception("parse")
        return web.json_response({"error": f"Parse failed: {e}"}, status=400)
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
    return web.json_response({
        "session_id": sid,
        "filename": filename,
        "size_mb": round(len(bundle_bytes) / (1024 * 1024), 2),
        "assets": assets,
        "count": len(assets),
        "counts": counts,
        "replaced": [],
    })


async def api_extract(request):
    reader = await request.multipart()
    sid = path_id = name = type_name_filter = None
    async for part in reader:
        val = (await part.read(decode=False)).decode("utf-8", errors="ignore").strip()
        if part.name == "session_id":
            sid = val
        elif part.name == "path_id":
            path_id = val or None
        elif part.name == "name":
            name = val or None
        elif part.name == "type":
            type_name_filter = val or None
    sess = SESSIONS.get(sid or "")
    if not sess:
        return web.json_response({"error": "Session expired. Upload again."}, status=400)
    try:
        result = extract_asset(sess["bundle_bytes"], path_id, name, type_name_filter)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)
    if result["kind"] == "texture":
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
    if result["kind"] == "mesh":
        return web.Response(
            body=result["obj_bytes"],
            headers={
                "Content-Type": "text/plain",
                "Content-Disposition": f'attachment; filename="{sanitize(result["name"])}.obj"',
                "X-Asset-Kind": "mesh",
                "X-Asset-Name": result["name"],
            },
        )
    return web.json_response({
        "kind": "text",
        "name": result["name"],
        "is_binary": result["is_binary"],
        "text": result["text"],
        "size": len(result["raw_bytes"]),
        "raw_base64": base64.b64encode(result["raw_bytes"]).decode("ascii") if result["is_binary"] else None,
    })


async def api_replace(request):
    reader = await request.multipart()
    sid = name = type_name_filter = path_id = None
    file_bytes = None
    async for part in reader:
        if part.name == "session_id":
            sid = (await part.read(decode=False)).decode().strip()
        elif part.name == "name":
            name = (await part.read(decode=False)).decode().strip()
        elif part.name == "type":
            type_name_filter = (await part.read(decode=False)).decode().strip() or None
        elif part.name == "path_id":
            path_id = (await part.read(decode=False)).decode().strip() or None
        elif part.name in ("file", "texture", "textures", "png", "text"):
            file_bytes = await part.read(decode=False)
            if not name and part.filename:
                name = Path(part.filename).stem
    sess = SESSIONS.get(sid or "")
    if not sess:
        return web.json_response({"error": "Session expired. Upload again."}, status=400)
    if not file_bytes or not (name or path_id):
        return web.json_response({"error": "Need name or path_id and a file"}, status=400)
    try:
        new_bytes, just = replace_assets(
            sess["bundle_bytes"],
            [{"name": name or "", "type": type_name_filter, "path_id": path_id, "data": file_bytes}],
        )
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)
    sess["bundle_bytes"] = new_bytes
    for item in just:
        label = item.split(":", 1)[-1]
        if label not in sess["replaced"]:
            sess["replaced"].append(label)
    sess["created"] = time.time()
    preview = None
    try:
        result = extract_asset(new_bytes, name=name, type_name_filter=type_name_filter, path_id=path_id)
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
    except Exception:
        pass
    return web.json_response({
        "ok": True,
        "replaced": sess["replaced"],
        "bundle_size_mb": round(len(new_bytes) / (1024 * 1024), 2),
        "preview": preview,
    })


async def api_bulk_replace(request):
    reader = await request.multipart()
    sid = None
    files = []
    async for part in reader:
        if part.name == "session_id":
            sid = (await part.read(decode=False)).decode().strip()
        elif part.name in ("files", "file"):
            fname = part.filename or "file"
            data = await part.read(decode=False)
            if data:
                files.append((fname, data))
    sess = SESSIONS.get(sid or "")
    if not sess:
        return web.json_response({"error": "Session expired. Upload again."}, status=400)
    if not files:
        return web.json_response({"error": "No files"}, status=400)
    IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".tga", ".bmp"}
    TEXT_EXT = {".txt", ".json", ".xml", ".csv", ".bytes", ".cfg", ".ini"}
    replacements, extras, planned = [], [], []
    for fname, data in files:
        if len(data) / (1024 * 1024) > MAX_FILE_MB:
            extras.append({"name": fname, "reason": "too_large"})
            continue
        stem = Path(fname).stem
        ext = Path(fname).suffix.lower()
        if ext in IMAGE_EXT:
            replacements.append({"name": stem, "type": "Texture2D", "data": data})
            planned.append({"file": fname, "m_Name": stem})
        elif ext in TEXT_EXT or ext == "":
            replacements.append({"name": stem, "type": "TextAsset", "data": data})
            planned.append({"file": fname, "m_Name": stem})
        else:
            extras.append({"name": fname, "m_Name": stem, "reason": "unsupported"})
    just = []
    if replacements:
        try:
            new_bytes, just = replace_assets(sess["bundle_bytes"], replacements)
            sess["bundle_bytes"] = new_bytes
        except ValueError:
            cur = sess["bundle_bytes"]
            for r in replacements:
                try:
                    cur, j = replace_assets(cur, [r])
                    just.extend(j)
                except Exception as ex:
                    extras.append({"name": r["name"], "reason": str(ex)})
            sess["bundle_bytes"] = cur
    for item in just:
        label = item.split(":", 1)[-1]
        if label not in sess["replaced"]:
            sess["replaced"].append(label)
    replaced_names = {x.split(":", 1)[-1].lower() for x in just}
    for p in planned:
        if p["m_Name"].lower() not in replaced_names:
            if not any((e.get("m_Name") == p["m_Name"] or e.get("name") == p["file"]) for e in extras):
                extras.append({"name": p["file"], "m_Name": p["m_Name"], "reason": "no_match"})
    return web.json_response({
        "ok": True,
        "replaced": sess["replaced"],
        "just_replaced": just,
        "extras": extras,
        "bundle_size_mb": round(len(sess["bundle_bytes"]) / (1024 * 1024), 2),
        "message": f"Replaced {len(just)}. Extras: {len(extras)}.",
    })


async def api_export_models(request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    sid = (body.get("session_id") or "").strip()
    sess = SESSIONS.get(sid)
    if not sess:
        return web.json_response({"error": "Session expired. Upload again."}, status=400)
    try:
        zdata = export_models_zip(sess["bundle_bytes"])
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)
    name = f"models_{sanitize(sess['filename'])}.zip"
    return web.Response(
        body=zdata,
        headers={
            "Content-Type": "application/zip",
            "Content-Disposition": f'attachment; filename="{name}"',
            "Content-Length": str(len(zdata)),
        },
    )


async def api_build(request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    sid = (body.get("session_id") or "").strip()
    sess = SESSIONS.get(sid)
    if not sess:
        return web.json_response({"error": "Session expired. Upload again."}, status=400)
    data = sess["bundle_bytes"]
    out_name = f"replaced_{sanitize(sess['filename'])}"
    return web.Response(
        body=data,
        headers={
            "Content-Type": "application/octet-stream",
            "Content-Disposition": f'attachment; filename="{out_name}"',
            "Content-Length": str(len(data)),
            "X-Original-Size": str(sess["original_size"]),
            "X-Output-Size": str(len(data)),
        },
    )


def create_app():
    app = web.Application(client_max_size=MAX_BUNDLE_MB * 1024 * 1024 + 40 * 1024 * 1024)
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_post("/api/upload", api_upload)
    app.router.add_post("/api/extract", api_extract)
    app.router.add_post("/api/replace", api_replace)
    app.router.add_post("/api/bulk_replace", api_bulk_replace)
    app.router.add_post("/api/export_models", api_export_models)
    app.router.add_post("/api/build", api_build)
    return app


if __name__ == "__main__":
    log.info("unity-assets-replacer-notuabe on http://127.0.0.1:%s", PORT)
    web.run_app(create_app(), host="0.0.0.0", port=PORT, print=None)
