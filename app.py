"""
unity-assets-replacer-notuabe
Textures · text · mesh · animation · raw .dat · zip replace-all · storage cleaner
Subway Surfers ~2020 default: Unity 2019.4.11f1
"""

from __future__ import annotations

import base64
import gc
import io
import logging
import os
import re
import secrets
import shutil
import tempfile
import time
import zipfile
from pathlib import Path

import UnityPy
from aiohttp import web
from PIL import Image

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("notuabe")

MAX_BUNDLE_MB = int(os.environ.get("MAX_BUNDLE_MB", "35"))
MAX_FILE_MB = int(os.environ.get("MAX_FILE_MB", "20"))
PORT = int(os.environ.get("PORT", "8080"))
FALLBACK_UNITY = os.environ.get("FALLBACK_UNITY_VERSION", "2019.4.11f1")
SESSION_TTL = int(os.environ.get("SESSION_TTL_SEC", "1800"))
STATIC = Path(__file__).parent

SUPPORTED = {
    "Texture2D", "TextAsset", "Mesh", "Material", "Sprite",
    "AnimationClip", "AnimatorController", "Avatar",
}
RAW_REPLACE_TYPES = SUPPORTED | {
    "MonoBehaviour", "GameObject", "Transform", "SkinnedMeshRenderer",
    "MeshRenderer", "MeshFilter", "Animator",
}
SESSIONS: dict[str, dict] = {}


def purge():
    now = time.time()
    for k in [k for k, v in SESSIONS.items() if now - v.get("created", now) > SESSION_TTL]:
        SESSIONS.pop(k, None)
    gc.collect()


def storage_clean(clear_sessions: bool = True) -> dict:
    freed = 0
    removed = []
    tmp = Path(tempfile.gettempdir())
    for p in tmp.glob("notuabe_*"):
        try:
            if p.is_dir():
                size = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
                shutil.rmtree(p, ignore_errors=True)
            else:
                size = p.stat().st_size
                p.unlink(missing_ok=True)
            freed += size
            removed.append(str(p.name))
        except Exception:
            pass
    n_sess = 0
    if clear_sessions:
        n_sess = len(SESSIONS)
        SESSIONS.clear()
    gc.collect()
    free_mb = None
    try:
        usage = shutil.disk_usage(str(STATIC))
        free_mb = round(usage.free / (1024 * 1024), 1)
    except Exception:
        pass
    return {
        "freed_mb": round(freed / (1024 * 1024), 2),
        "removed": removed,
        "sessions_cleared": n_sess,
        "disk_free_mb": free_mb,
    }


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
            if tname == "AnimationClip":
                extra["size"] = int(tree.get("m_MuscleClipSize") or tree.get("m_Size") or 0)
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
        if hasattr(obj, "get_raw_data"):
            try:
                raw = obj.get_raw_data()
                if raw and not extra["size"]:
                    extra["size"] = len(raw)
            except Exception:
                pass
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
        if pid is not None and pid == str(getattr(obj, "path_id", "")):
            return obj, tname
        if target:
            info = asset_info(obj)
            if info and info["name"].lower() == target:
                return obj, tname
            if tname not in SUPPORTED:
                try:
                    tree = obj.read_typetree()
                    if isinstance(tree, dict) and str(tree.get("m_Name") or "").lower() == target:
                        return obj, tname
                except Exception:
                    pass
    return None, None


def get_raw(obj) -> bytes:
    if hasattr(obj, "get_raw_data"):
        return obj.get_raw_data()
    raise ValueError("No raw data API on this object")


def set_raw(obj, data: bytes):
    if hasattr(obj, "set_raw_data"):
        obj.set_raw_data(data)
        return
    raise ValueError("No set_raw_data on this object")


def extract_asset(data: bytes, path_id=None, name=None, type_name_filter=None, as_raw=False):
    env = load_env(data)
    obj, tname = find_obj(env, path_id, name, type_name_filter)
    if obj is None:
        raise ValueError("Asset not found")
    aname = name or "asset"
    try:
        d = obj.read()
        aname = getattr(d, "m_Name", None) or getattr(d, "name", None) or aname
    except Exception:
        pass
    if as_raw or tname in ("AnimationClip", "AnimatorController", "Avatar"):
        raw = get_raw(obj)
        return {"kind": "raw", "name": str(aname), "type": tname, "raw_bytes": raw}
    if tname in ("Texture2D", "Sprite"):
        d = obj.read()
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
        d = obj.read()
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
        d = obj.read()
        obj_txt = d.export(format="obj")
        raw = obj_txt if isinstance(obj_txt, bytes) else str(obj_txt).encode("utf-8")
        return {"kind": "mesh", "name": str(aname), "obj_bytes": raw}
    raw = get_raw(obj)
    return {"kind": "raw", "name": str(aname), "type": tname, "raw_bytes": raw}


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
        mode = rep.get("mode") or "auto"
        aname = aname or rep.get("name") or obj_pid
        try:
            if mode == "raw" or (mode == "auto" and Path(rep.get("filename") or "").suffix.lower() == ".dat"):
                set_raw(obj, raw)
                replaced.append(f"{tname}:{aname}")
                continue
            if tname in ("Texture2D", "Sprite") and mode in ("auto", "texture"):
                data_obj = obj.read()
                img = Image.open(io.BytesIO(raw)).convert("RGBA")
                if hasattr(data_obj, "set_image"):
                    data_obj.set_image(img)
                else:
                    data_obj.image = img
                data_obj.save()
                try:
                    reader = getattr(data_obj, "object_reader", None) or obj
                    assets = getattr(reader, "assets_file", None)
                    if assets is not None and hasattr(assets, "mark_changed"):
                        assets.mark_changed()
                except Exception:
                    pass
                replaced.append(f"{tname}:{aname}")
            elif tname == "TextAsset" and mode in ("auto", "text"):
                data_obj = obj.read()
                try:
                    data_obj.m_Script = raw.decode("utf-8")
                except UnicodeDecodeError:
                    data_obj.m_Script = raw
                data_obj.save()
                replaced.append(f"{tname}:{aname}")
            elif tname in ("Mesh", "AnimationClip", "AnimatorController", "Avatar") or mode == "raw":
                set_raw(obj, raw)
                replaced.append(f"{tname}:{aname}")
            else:
                set_raw(obj, raw)
                replaced.append(f"{tname}:{aname}")
        except Exception:
            log.exception("replace failed %s", aname)

    if not replaced:
        raise ValueError("No matching assets. Check names/path_id/.dat")
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

        def put(key, payload):
            nonlocal count
            i = 1
            k = key
            while k in used:
                stem, ext = os.path.splitext(key)
                k = f"{stem}_{i}{ext}"
                i += 1
            used.add(k)
            zf.writestr(k, payload)
            count += 1

        for obj in all_objects(env):
            tname = type_name(obj)
            try:
                if tname == "Mesh":
                    d = obj.read()
                    name = getattr(d, "m_Name", None) or f"mesh_{obj.path_id}"
                    safe = re.sub(r"[^\w.\-]+", "_", str(name))[:100] or "mesh"
                    obj_data = d.export(format="obj")
                    put(f"meshes/{safe}.obj", obj_data if isinstance(obj_data, bytes) else str(obj_data).encode("utf-8"))
                    put(f"raw/{safe}.Mesh.dat", get_raw(obj))
                elif tname in ("Texture2D", "Sprite"):
                    d = obj.read()
                    name = getattr(d, "m_Name", None) or f"tex_{obj.path_id}"
                    safe = re.sub(r"[^\w.\-]+", "_", str(name))[:100] or "tex"
                    pbuf = io.BytesIO()
                    d.image.save(pbuf, format="PNG")
                    put(f"textures/{safe}.png", pbuf.getvalue())
                elif tname in ("AnimationClip", "AnimatorController", "Avatar"):
                    info = asset_info(obj)
                    name = (info or {}).get("name") or f"anim_{obj.path_id}"
                    safe = re.sub(r"[^\w.\-]+", "_", str(name))[:100] or "anim"
                    put(f"animations/{safe}.{tname}.dat", get_raw(obj))
            except Exception:
                log.exception("export skip %s", tname)
        zf.writestr(
            "README.txt",
            "meshes/*.obj = Mesh (preview)\n"
            "raw/*.Mesh.dat = Mesh raw (for replace)\n"
            "textures/*.png = Texture2D / Sprite\n"
            "animations/*.dat = AnimationClip / related raw\n"
            "To replace: put files in a zip and use Replace everything.\n"
            f"Exported {count} files.\n",
        )
    if count == 0:
        raise ValueError("Nothing to export")
    return buf.getvalue()


def replace_from_zip(bundle_bytes: bytes, zip_bytes: bytes) -> tuple:
    replacements = []
    extras = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/")
            base = Path(name).name
            if base.startswith(".") or base == "README.txt":
                continue
            data = zf.read(info)
            if not data:
                continue
            if len(data) / (1024 * 1024) > MAX_FILE_MB:
                extras.append({"name": base, "reason": "too_large"})
                continue
            stem = Path(base).stem
            ext = Path(base).suffix.lower()
            # animations/foo.AnimationClip.dat -> stem AnimationClip stripped
            lower = name.lower()
            if ext == ".dat":
                # foo.Mesh.dat or foo.AnimationClip.dat or foo.dat
                parts = stem.split(".")
                type_hint = None
                real_name = stem
                if len(parts) >= 2 and parts[-1] in (
                    "Mesh", "AnimationClip", "AnimatorController", "Avatar",
                    "Texture2D", "TextAsset", "MonoBehaviour",
                ):
                    type_hint = parts[-1]
                    real_name = ".".join(parts[:-1])
                mode = "raw"
                replacements.append({
                    "name": real_name,
                    "type": type_hint,
                    "data": data,
                    "mode": mode,
                    "filename": base,
                })
            elif ext in (".png", ".jpg", ".jpeg", ".webp"):
                replacements.append({
                    "name": stem,
                    "type": "Texture2D",
                    "data": data,
                    "mode": "texture",
                    "filename": base,
                })
            elif ext in (".txt", ".json", ".xml", ".csv", ".bytes"):
                replacements.append({
                    "name": stem,
                    "type": "TextAsset",
                    "data": data,
                    "mode": "text",
                    "filename": base,
                })
            elif ext == ".obj":
                extras.append({"name": base, "reason": "obj_not_importable_use_dat"})
            else:
                extras.append({"name": base, "reason": "unsupported"})
    if not replacements:
        raise ValueError("No usable files in zip (.png, .txt, .dat)")
    try:
        out, just = replace_assets(bundle_bytes, replacements)
    except ValueError:
        out = bundle_bytes
        just = []
        for r in replacements:
            try:
                out, j = replace_assets(out, [r])
                just.extend(j)
            except Exception as ex:
                extras.append({"name": r.get("filename") or r.get("name"), "reason": str(ex)})
    return out, just, extras


def sanitize(name: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", name)[:120]


# routes

async def index(request):
    return web.FileResponse(STATIC / "index.html")


async def health(request):
    return web.json_response({
        "status": "ok",
        "name": "unity-assets-replacer-notuabe",
        "sessions": len(SESSIONS),
        "fallback_unity": FALLBACK_UNITY,
    })


async def api_clean(request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    clear = body.get("clear_sessions", True)
    result = storage_clean(clear_sessions=bool(clear))
    return web.json_response({"ok": True, **result})


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
        return web.json_response({"error": f"Bundle too large (max {MAX_BUNDLE_MB} MB)"}, status=400)
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
    as_raw = False
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
        elif part.name == "as_raw":
            as_raw = val in ("1", "true", "True", "yes")
    sess = SESSIONS.get(sid or "")
    if not sess:
        return web.json_response({"error": "Session expired. Upload again."}, status=400)
    try:
        result = extract_asset(sess["bundle_bytes"], path_id, name, type_name_filter, as_raw=as_raw)
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
    if result["kind"] == "raw":
        fname = f"{sanitize(result['name'])}.{result.get('type') or 'raw'}.dat"
        return web.Response(
            body=result["raw_bytes"],
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Disposition": f'attachment; filename="{fname}"',
                "X-Asset-Kind": "raw",
                "X-Asset-Name": result["name"],
                "X-Asset-Type": result.get("type") or "",
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
    sid = name = type_name_filter = path_id = mode = None
    file_bytes = None
    filename = ""
    async for part in reader:
        if part.name == "session_id":
            sid = (await part.read(decode=False)).decode().strip()
        elif part.name == "name":
            name = (await part.read(decode=False)).decode().strip()
        elif part.name == "type":
            type_name_filter = (await part.read(decode=False)).decode().strip() or None
        elif part.name == "path_id":
            path_id = (await part.read(decode=False)).decode().strip() or None
        elif part.name == "mode":
            mode = (await part.read(decode=False)).decode().strip() or None
        elif part.name in ("file", "texture", "textures", "png", "text", "raw", "dat"):
            filename = part.filename or ""
            file_bytes = await part.read(decode=False)
            if not name and part.filename:
                name = Path(part.filename).stem.split(".")[0]
    sess = SESSIONS.get(sid or "")
    if not sess:
        return web.json_response({"error": "Session expired. Upload again."}, status=400)
    if not file_bytes or not (name or path_id):
        return web.json_response({"error": "Need name or path_id and a file"}, status=400)
    if not mode:
        mode = "raw" if (filename or "").lower().endswith(".dat") else "auto"
    try:
        new_bytes, just = replace_assets(
            sess["bundle_bytes"],
            [{
                "name": name or "",
                "type": type_name_filter,
                "path_id": path_id,
                "data": file_bytes,
                "mode": mode,
                "filename": filename,
            }],
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
    except Exception:
        pass
    return web.json_response({
        "ok": True,
        "replaced": sess["replaced"],
        "just_replaced": just,
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
    replacements, extras = [], []
    for fname, data in files:
        if len(data) / (1024 * 1024) > MAX_FILE_MB:
            extras.append({"name": fname, "reason": "too_large"})
            continue
        stem = Path(fname).stem
        ext = Path(fname).suffix.lower()
        if ext == ".dat":
            parts = stem.split(".")
            type_hint = parts[-1] if len(parts) >= 2 else None
            real = ".".join(parts[:-1]) if type_hint else stem
            replacements.append({"name": real, "type": type_hint, "data": data, "mode": "raw", "filename": fname})
        elif ext in IMAGE_EXT:
            replacements.append({"name": stem, "type": "Texture2D", "data": data, "mode": "texture", "filename": fname})
        elif ext in TEXT_EXT or ext == "":
            replacements.append({"name": stem, "type": "TextAsset", "data": data, "mode": "text", "filename": fname})
        else:
            extras.append({"name": fname, "reason": "unsupported"})
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
                    extras.append({"name": r.get("filename"), "reason": str(ex)})
            sess["bundle_bytes"] = cur
    for item in just:
        label = item.split(":", 1)[-1]
        if label not in sess["replaced"]:
            sess["replaced"].append(label)
    return web.json_response({
        "ok": True,
        "replaced": sess["replaced"],
        "just_replaced": just,
        "extras": extras,
        "bundle_size_mb": round(len(sess["bundle_bytes"]) / (1024 * 1024), 2),
        "message": f"Replaced {len(just)}. Extras: {len(extras)}.",
    })


async def api_replace_zip(request):
    reader = await request.multipart()
    sid = None
    zip_bytes = None
    async for part in reader:
        if part.name == "session_id":
            sid = (await part.read(decode=False)).decode().strip()
        elif part.name in ("zip", "file"):
            zip_bytes = await part.read(decode=False)
    sess = SESSIONS.get(sid or "")
    if not sess:
        return web.json_response({"error": "Session expired. Upload again."}, status=400)
    if not zip_bytes:
        return web.json_response({"error": "No zip"}, status=400)
    try:
        new_bytes, just, extras = replace_from_zip(sess["bundle_bytes"], zip_bytes)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)
    sess["bundle_bytes"] = new_bytes
    for item in just:
        label = item.split(":", 1)[-1]
        if label not in sess["replaced"]:
            sess["replaced"].append(label)
    sess["created"] = time.time()
    return web.json_response({
        "ok": True,
        "replaced": sess["replaced"],
        "just_replaced": just,
        "extras": extras,
        "bundle_size_mb": round(len(new_bytes) / (1024 * 1024), 2),
        "message": f"Zip replace: {len(just)} ok, {len(extras)} skipped.",
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
    name = f"assets_{sanitize(sess['filename'])}.zip"
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
    app.router.add_post("/api/clean", api_clean)
    app.router.add_post("/api/upload", api_upload)
    app.router.add_post("/api/extract", api_extract)
    app.router.add_post("/api/replace", api_replace)
    app.router.add_post("/api/bulk_replace", api_bulk_replace)
    app.router.add_post("/api/replace_zip", api_replace_zip)
    app.router.add_post("/api/export_models", api_export_models)
    app.router.add_post("/api/build", api_build)
    return app


if __name__ == "__main__":
    log.info("notuabe on http://127.0.0.1:%s (Unity %s)", PORT, FALLBACK_UNITY)
    web.run_app(create_app(), host="0.0.0.0", port=PORT, print=None)
