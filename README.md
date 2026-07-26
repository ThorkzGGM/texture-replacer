# Unity Texture Replacer (Web)

Python + HTML on **Render**. No Telegram.

Upload a Unity `.bundle`, list Texture2D assets, replace textures with PNGs, download the modified bundle.

## Deploy on Render

1. Push this folder to GitHub
2. **New → Web Service** → connect repo
3. Settings:

| Field | Value |
|-------|--------|
| Build Command | `pip install -r requirements.txt` |
| Start Command | `python app.py` |
| Health Check Path | `/health` |

Optional env: `MAX_BUNDLE_MB=80`, `MAX_TEXTURE_MB=20`

## Local

```bash
pip install -r requirements.txt
python app.py
# open http://localhost:8080
```

## How to use

1. Drop a `.bundle` → **List textures**
2. Drop PNG(s) — filename without extension = texture `m_Name`
3. **Build & download .bundle**

## Stack

- Backend: `aiohttp` + `UnityPy` + `Pillow`
- Frontend: single `index.html` (served by the same app)


## If listing fails (`NoneType` / parse error)

Some games strip Unity version / TypeTree from bundles.

On Render, add env var:

| Key | Example value |
|-----|----------------|
| `FALLBACK_UNITY_VERSION` | `2021.3.0f1` or `2022.3.0f1` or your game's Unity version |

Redeploy after setting it. You can find the Unity version in the game's `globalgamemanagers` or APK libs.
