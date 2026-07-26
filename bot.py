"""
Unity Asset Bundle Texture Replacer Telegram Bot
================================================
Send an asset bundle + replacement PNG(s). The bot replaces matching
Texture2D assets (by name) and returns the modified bundle.

Requires: BOT_TOKEN environment variable.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import UnityPy
from PIL import Image
from telegram import InputFile, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Config & logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
MAX_BUNDLE_MB = int(os.environ.get("MAX_BUNDLE_MB", "80"))  # Render free tier limit-ish
MAX_TEXTURE_MB = int(os.environ.get("MAX_TEXTURE_MB", "20"))

# Conversation states
WAITING_BUNDLE, WAITING_TEXTURES = range(2)

# Per-user temporary session data
# chat_id -> {"bundle_path": Path, "textures": {name: Path}, "tmp_dir": Path}
sessions: Dict[int, dict] = {}


# ---------------------------------------------------------------------------
# UnityPy helpers
# ---------------------------------------------------------------------------

def list_textures(bundle_path: Path) -> List[Tuple[str, int, int]]:
    """Return list of (name, width, height) for all Texture2D objects."""
    env = UnityPy.load(str(bundle_path))
    results = []
    for obj in env.objects:
        if obj.type.name == "Texture2D":
            data = obj.read()
            name = getattr(data, "m_Name", None) or f"pathid_{obj.path_id}"
            w = getattr(data, "m_Width", 0)
            h = getattr(data, "m_Height", 0)
            results.append((name, w, h))
    return results


def replace_textures(
    bundle_path: Path,
    replacements: Dict[str, Path],
    output_path: Path,
    compression: str = "lz4",
) -> List[str]:
    """
    Replace Texture2D assets whose m_Name matches a key in `replacements`.
    Keys are matched case-insensitively and without extension.
    Returns list of successfully replaced texture names.
    """
    env = UnityPy.load(str(bundle_path))
    replaced: List[str] = []

    # Normalize lookup: lower-case name without extension
    lookup = {
        Path(k).stem.lower(): v for k, v in replacements.items()
    }

    for obj in env.objects:
        if obj.type.name != "Texture2D":
            continue
        data = obj.read()
        name = getattr(data, "m_Name", None) or ""
        key = name.lower()

        if key not in lookup:
            continue

        img_path = lookup[key]
        try:
            pil_img = Image.open(img_path).convert("RGBA")
            # Preferred modern API (UnityPy 1.20+)
            if hasattr(data, "set_image"):
                data.set_image(pil_img)
            else:
                data.image = pil_img
            data.save()
            replaced.append(name)
            logger.info("Replaced texture: %s", name)
        except Exception as e:
            logger.exception("Failed to replace %s: %s", name, e)

    # UnityPy 1.25+ : Environment.save(pack="lz4"|"none", out_path=...)
    pack = compression if compression in ("lz4", "none") else "lz4"
    env.save(pack=pack, out_path=str(output_path))
    return replaced


def sanitize_filename(name: str) -> str:
    return re.sub(r'[^\w.\-]+', "_", name)[:120]


# ---------------------------------------------------------------------------
# Bot handlers
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "🎨 *Unity Texture Replacer Bot*\n\n"
        "I replace textures inside Unity AssetBundles.\n\n"
        "*How to use:*\n"
        "1. Send me an AssetBundle file (`.bundle`, `.unity3d`, `.assets`, …)\n"
        "2. Then send one or more PNG images.\n"
        "   • Filename (without extension) must match the texture `m_Name`\n"
        "3. When finished, send /done\n\n"
        "Commands:\n"
        "/start – show this help\n"
        "/list – list Texture2D names in the current bundle\n"
        "/done – process replacements and return the new bundle\n"
        "/cancel – abort current session\n\n"
        f"Limits: bundle ≤ {MAX_BUNDLE_MB} MB, each PNG ≤ {MAX_TEXTURE_MB} MB",
        parse_mode=ParseMode.MARKDOWN,
    )
    return WAITING_BUNDLE


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    _cleanup_session(chat_id)
    await update.message.reply_text("Session cancelled. Send a new AssetBundle to start over.")
    return ConversationHandler.END


async def receive_bundle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    doc = update.message.document

    if not doc:
        await update.message.reply_text("Please send an AssetBundle as a *file* (document).")
        return WAITING_BUNDLE

    size_mb = (doc.file_size or 0) / (1024 * 1024)
    if size_mb > MAX_BUNDLE_MB:
        await update.message.reply_text(
            f"File too large ({size_mb:.1f} MB). Max is {MAX_BUNDLE_MB} MB."
        )
        return WAITING_BUNDLE

    await update.message.chat.send_action(ChatAction.TYPING)

    # Create isolated temp dir for this user
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"utr_{chat_id}_"))
    bundle_path = tmp_dir / sanitize_filename(doc.file_name or "bundle")

    try:
        tg_file = await doc.get_file()
        await tg_file.download_to_drive(str(bundle_path))
    except Exception as e:
        logger.exception("Download failed")
        await update.message.reply_text(f"Download failed: {e}")
        return WAITING_BUNDLE

    # Quick validation
    try:
        textures = list_textures(bundle_path)
    except Exception as e:
        logger.exception("Invalid bundle")
        await update.message.reply_text(
            f"Could not parse as Unity AssetBundle:\n`{e}`\n\n"
            "Make sure the file is a valid (possibly compressed) AssetBundle.",
            parse_mode=ParseMode.MARKDOWN,
        )
        _cleanup_session(chat_id)
        return WAITING_BUNDLE

    sessions[chat_id] = {
        "bundle_path": bundle_path,
        "textures": {},
        "tmp_dir": tmp_dir,
        "original_name": doc.file_name or "bundle",
    }

    names_preview = "\n".join(f"• `{n}` ({w}×{h})" for n, w, h in textures[:30])
    more = f"\n… and {len(textures) - 30} more" if len(textures) > 30 else ""

    await update.message.reply_text(
        f"✅ Bundle loaded ({size_mb:.1f} MB)\n"
        f"Found *{len(textures)}* Texture2D assets:\n\n"
        f"{names_preview}{more}\n\n"
        "Now send PNG image(s). Filename (without .png) must match a texture name.\n"
        "When ready, send /done",
        parse_mode=ParseMode.MARKDOWN,
    )
    return WAITING_TEXTURES


async def receive_texture(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    session = sessions.get(chat_id)
    if not session:
        await update.message.reply_text("No active session. Send an AssetBundle first (/start).")
        return WAITING_BUNDLE

    doc = update.message.document
    photo = update.message.photo

    # Prefer document (higher quality), fall back to largest photo
    if doc:
        size_mb = (doc.file_size or 0) / (1024 * 1024)
        if size_mb > MAX_TEXTURE_MB:
            await update.message.reply_text(f"Texture too large ({size_mb:.1f} MB).")
            return WAITING_TEXTURES
        file_name = doc.file_name or "texture.png"
        tg_file = await doc.get_file()
    elif photo:
        # Telegram compresses photos; still accept
        largest = photo[-1]
        size_mb = (largest.file_size or 0) / (1024 * 1024)
        if size_mb > MAX_TEXTURE_MB:
            await update.message.reply_text(f"Texture too large ({size_mb:.1f} MB).")
            return WAITING_TEXTURES
        file_name = "photo.png"
        tg_file = await largest.get_file()
    else:
        await update.message.reply_text("Please send a PNG file or photo.")
        return WAITING_TEXTURES

    stem = Path(file_name).stem
    dest = session["tmp_dir"] / f"{sanitize_filename(stem)}.png"

    try:
        await tg_file.download_to_drive(str(dest))
        # Validate image
        with Image.open(dest) as im:
            im.verify()
    except Exception as e:
        await update.message.reply_text(f"Invalid image: {e}")
        return WAITING_TEXTURES

    session["textures"][stem] = dest
    count = len(session["textures"])
    await update.message.reply_text(
        f"📥 Added replacement for `{stem}` (total {count}).\n"
        "Send more PNGs or /done when finished.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return WAITING_TEXTURES


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    session = sessions.get(chat_id)
    if not session:
        await update.message.reply_text("No active bundle. Send one first.")
        return WAITING_BUNDLE

    try:
        textures = list_textures(session["bundle_path"])
    except Exception as e:
        await update.message.reply_text(f"Error reading bundle: {e}")
        return WAITING_TEXTURES

    if not textures:
        await update.message.reply_text("No Texture2D found.")
        return WAITING_TEXTURES

    lines = [f"`{n}` — {w}×{h}" for n, w, h in textures]
    # Split into chunks if very long
    text = "*Textures in current bundle:*\n\n" + "\n".join(lines)
    if len(text) > 4000:
        text = text[:3900] + "\n… (truncated)"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    return WAITING_TEXTURES


async def done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    session = sessions.get(chat_id)
    if not session:
        await update.message.reply_text("No active session.")
        return ConversationHandler.END

    if not session["textures"]:
        await update.message.reply_text("No replacement textures uploaded yet. Send some PNGs first.")
        return WAITING_TEXTURES

    await update.message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)
    await update.message.reply_text("⏳ Processing… this may take a moment.")

    out_name = f"replaced_{sanitize_filename(session['original_name'])}"
    out_path = session["tmp_dir"] / out_name

    try:
        replaced = await asyncio.to_thread(
            replace_textures,
            session["bundle_path"],
            session["textures"],
            out_path,
        )
    except Exception as e:
        logger.exception("Replacement failed")
        await update.message.reply_text(f"❌ Replacement failed:\n`{e}`", parse_mode=ParseMode.MARKDOWN)
        _cleanup_session(chat_id)
        return ConversationHandler.END

    if not replaced:
        await update.message.reply_text(
            "No textures were replaced.\n"
            "Check that PNG filenames (without extension) match the Texture2D `m_Name` exactly "
            "(case-insensitive)."
        )
        _cleanup_session(chat_id)
        return ConversationHandler.END

    # Send result
    try:
        with open(out_path, "rb") as f:
            await update.message.reply_document(
                document=InputFile(f, filename=out_name),
                caption=(
                    f"✅ Replaced {len(replaced)} texture(s):\n"
                    + "\n".join(f"• `{n}`" for n in replaced)
                ),
                parse_mode=ParseMode.MARKDOWN,
            )
    except Exception as e:
        logger.exception("Upload failed")
        await update.message.reply_text(f"Could not send result file: {e}")

    _cleanup_session(chat_id)
    await update.message.reply_text("Session finished. Send a new AssetBundle to start again.")
    return ConversationHandler.END


def _cleanup_session(chat_id: int) -> None:
    session = sessions.pop(chat_id, None)
    if session and "tmp_dir" in session:
        import shutil
        try:
            shutil.rmtree(session["tmp_dir"], ignore_errors=True)
        except Exception:
            pass


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text(
            "An internal error occurred. Try /cancel and start over."
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN environment variable is required")

    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            WAITING_BUNDLE: [
                MessageHandler(filters.Document.ALL, receive_bundle),
                CommandHandler("cancel", cancel),
            ],
            WAITING_TEXTURES: [
                MessageHandler(filters.Document.ALL | filters.PHOTO, receive_texture),
                CommandHandler("list", list_cmd),
                CommandHandler("done", done),
                CommandHandler("cancel", cancel),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("start", start))  # also outside conv
    app.add_error_handler(error_handler)

    logger.info("Bot starting (polling)…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
