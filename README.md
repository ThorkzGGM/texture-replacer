# Unity Asset Bundle Texture Replacer — Telegram Bot

A Telegram bot that replaces **Texture2D** assets inside Unity AssetBundles using [UnityPy](https://github.com/K0lb3/UnityPy).

**Flow**

1. User sends an AssetBundle (`.bundle`, `.unity3d`, `.assets`, …)
2. Bot lists all Texture2D names + sizes
3. User sends one or more PNG images whose **filename (without extension)** matches a texture `m_Name`
4. User sends `/done`
5. Bot returns the modified AssetBundle

Matching is **case-insensitive**. The original texture format / compression is handled by UnityPy when the new PIL image is assigned.

---

## Features

- List all Texture2D assets in a bundle
- Replace any number of textures by name
- Works with compressed (LZ4, LZMA, …) and uncompressed bundles
- Temporary per-user sessions (files cleaned up after `/done` or `/cancel`)
- Configurable size limits via environment variables

---

## Local development

```bash
git clone <your-repo-url>
cd unity-texture-replacer-bot

python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

pip install -r requirements.txt

export BOT_TOKEN="123456:ABC-DEF..."   # from @BotFather
python bot.py
```

Talk to your bot on Telegram and follow the prompts.

---

## Deploy on Render (recommended)

Render free tier works well for a **Background Worker** (always-on polling bot).

### 1. Push to GitHub

Create a new repository and push this folder:

```bash
git init
git add .
git commit -m "Initial commit: Unity texture replacer Telegram bot"
git branch -M main
git remote add origin https://github.com/YOUR_USER/unity-texture-replacer-bot.git
git push -u origin main
```

### 2. Create the service on Render

1. Go to [https://dashboard.render.com](https://dashboard.render.com) → **New +** → **Background Worker**
2. Connect the GitHub repository
3. Settings:

| Field            | Value                          |
|------------------|--------------------------------|
| Name             | `unity-texture-replacer-bot`   |
| Region           | closest to you                 |
| Branch           | `main`                         |
| Runtime          | Python 3                       |
| Build Command    | `pip install -r requirements.txt` |
| Start Command    | `python bot.py`                |

4. **Environment Variables**

| Key            | Value                          |
|----------------|--------------------------------|
| `BOT_TOKEN`    | your token from @BotFather     |
| `MAX_BUNDLE_MB`| `80` (optional)                |
| `MAX_TEXTURE_MB`| `20` (optional)               |

5. Create the service. Render will build and start the worker.

### Alternative: Web Service + keep-alive

If you prefer a Web Service (or free tier changes), you can wrap the bot with a tiny HTTP server so Render’s health checks stay happy. One simple option is to add a second process or use a library such as `staypresent`. For most cases the **Background Worker** is cleaner.

---

## Bot commands

| Command   | Description                                      |
|-----------|--------------------------------------------------|
| `/start`  | Show help and begin a new session                |
| `/list`   | List Texture2D names in the current bundle       |
| `/done`   | Apply replacements and send the modified bundle  |
| `/cancel` | Abort current session and delete temp files      |

---

## How replacement works (technical)

```python
env = UnityPy.load("original.bundle")

for obj in env.objects:
    if obj.type.name == "Texture2D":
        data = obj.read()
        if data.m_Name.lower() == "my_texture":
            data.image = Image.open("my_texture.png").convert("RGBA")
            data.save()

with open("modified.bundle", "wb") as f:
    f.write(env.file.save(packer="lz4"))
```

UnityPy re-encodes the texture into a format the engine accepts. Complex platforms / special formats may need extra handling; test with your target game.

---

## Limitations & notes

- Free Render instances have limited disk & RAM — keep bundles under ~80 MB.
- Very large or highly compressed textures can be slow.
- Some games use custom asset bundle formats or encryption; UnityPy may not support them.
- CRC / signature checks in some anti-cheat systems can detect modified bundles.
- Always keep backups of original assets.

---

## License

MIT — do whatever you want. Use at your own risk when modifying game assets.
