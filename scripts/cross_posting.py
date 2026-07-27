#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cross-posting Casa Pellegrini - Instagram -> TikTok (Zernio) + Bluesky + Discord.

Substitui o cenario Make 4971593, que parou em 05/07/2026 porque a conexao OAuth
do Facebook no Make expirou (token de USUARIO da Meta dura 60 dias e nao tem
refresh token). Aqui usamos o IG_ACCESS_TOKEN, que e um Page Token PERMANENTE.

Cobertura de formatos (v2):
  video           -> TikTok (video) + Bluesky (texto + thumb) + Discord (embed)
  foto            -> TikTok (photo post) + Bluesky (imagem) + Discord (embed)
  carrossel       -> TikTok (photo carousel) + Bluesky (ate 4 imgs) + Discord

Estado: state/crossposted.json no proprio repo, com granularidade por plataforma
(um post pode ja ter ido pro Discord e ainda faltar o TikTok, por exemplo).

Dependencias: stdlib. Pillow e OPCIONAL (se instalado, comprime imagem pro
limite de ~1MB do Bluesky; sem ele, imagens grandes sao puladas).
"""

import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

try:  # opcional
    from PIL import Image

    HAS_PIL = True
except Exception:
    HAS_PIL = False

# ---------------------------------------------------------------- config

def _clean(v):
    """Tira espaco/quebra de linha/aspas/crase que vem junto no copiar-colar."""
    v = (v or "").strip()
    for ch in ('"', "'", "`"):
        v = v.strip(ch)
    return "".join(v.split())


GRAPH_VERSION = "v21.0"
IG_USER_ID = os.environ.get("IG_USER_ID", "17841404743438046")
IG_ACCESS_TOKEN = _clean(os.environ.get("IG_ACCESS_TOKEN", ""))

ZERNIO_API_BASE = "https://zernio.com/api/v1"
ZERNIO_API_KEY = _clean(os.environ.get("ZERNIO_API_KEY", ""))
ZERNIO_TIKTOK_ACCOUNT_ID = _clean(os.environ.get("ZERNIO_TIKTOK_ACCOUNT_ID", ""))

BLUESKY_PDS = "https://bsky.social"
BLUESKY_HANDLE = _clean(os.environ.get("BLUESKY_HANDLE", "casapellegrini.bsky.social"))
BLUESKY_APP_PASSWORD = _clean(os.environ.get("BLUESKY_APP_PASSWORD", ""))
BLUESKY_MAX_BLOB = 950_000  # limite real ~1MB; margem de seguranca
BLUESKY_MAX_IMAGES = 4

def _clean_url(v):
    v = _clean(v)
    if v and not v.startswith(("http://", "https://")):
        v = "https://" + v.lstrip("/")
    return v


DISCORD_WEBHOOK_URL = _clean_url(os.environ.get("DISCORD_WEBHOOK_URL", ""))

STATE_FILE = os.environ.get("STATE_FILE", "state/crossposted.json")
EXPECTED_USERNAME = os.environ.get("EXPECTED_USERNAME", "casapellegrini")
FETCH_LIMIT = int(os.environ.get("FETCH_LIMIT", "10"))

# Zernio Free = 20 posts/mes. Teto por rodada evita queimar a cota de uma vez.
MAX_TIKTOK_PER_RUN = int(os.environ.get("MAX_TIKTOK_PER_RUN", "3"))

# 0 = sem limite de idade
MAX_AGE_DAYS = int(os.environ.get("MAX_AGE_DAYS", "0"))

DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")

TIKTOK_HASHTAGS = (
    "#fyp #foryou #petropolis #petropolisrj #gastronomia "
    "#casapellegrini #restaurantepetropolis"
)

PLATFORMS = ("discord", "bluesky", "tiktok")


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------- http


def http_json(url, method="GET", payload=None, headers=None, timeout=180):
    data = None
    hdrs = {"Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        hdrs["Content-Type"] = "application/json"

    try:
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        status = e.code
    except Exception as e:
        return 0, {"error": str(e)}

    try:
        return status, json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return status, {"raw": raw[:800]}


def http_bytes(url, timeout=180):
    """Baixa bytes (imagem). Devolve (bytes|None, content_type)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(), resp.headers.get("Content-Type", "image/jpeg")
    except Exception as e:
        log(f"   ! download falhou: {e}")
        return None, None


def http_upload_raw(url, blob, content_type, headers=None, timeout=180):
    hdrs = {"Content-Type": content_type}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=blob, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return e.code, {"raw": e.read().decode("utf-8", "replace")[:400]}
    except Exception as e:
        return 0, {"error": str(e)}


# ---------------------------------------------------------------- estado


def load_state(path):
    if not os.path.exists(path):
        log(f"state: {path} nao existe ainda, comecando vazio")
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "posts" in data:
            return data["posts"]
        return data if isinstance(data, dict) else {}
    except Exception as e:
        log(f"AVISO: falha lendo state ({e}); comecando vazio")
        return {}


def save_state(path, posts):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {"updated_at": datetime.now(timezone.utc).isoformat(), "posts": posts},
            f,
            ensure_ascii=False,
            indent=1,
            sort_keys=True,
        )
    log(f"state: salvo ({len(posts)} posts) em {path}")


# ---------------------------------------------------------------- instagram


def fetch_instagram_media():
    fields = (
        "id,caption,media_type,media_url,permalink,timestamp,username,"
        "thumbnail_url,children{media_url,media_type,thumbnail_url}"
    )
    qs = urllib.parse.urlencode(
        {"fields": fields, "limit": FETCH_LIMIT, "access_token": IG_ACCESS_TOKEN}
    )
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{IG_USER_ID}/media?{qs}"
    status, body = http_json(url)
    if status != 200:
        raise RuntimeError(
            f"Instagram Graph API falhou (HTTP {status}): {(body or {}).get('error', body)}"
        )
    items = body.get("data", [])
    log(f"instagram: {len(items)} midias retornadas")
    return items


def child_items(item):
    return ((item.get("children") or {}).get("data")) or []


def image_urls(item, limit=None):
    """URLs de imagem do post (foto unica, filhos do carrossel, ou thumb do video)."""
    mtype = item.get("media_type")
    urls = []
    if mtype == "IMAGE":
        if item.get("media_url"):
            urls = [item["media_url"]]
    elif mtype == "CAROUSEL_ALBUM":
        for c in child_items(item):
            if c.get("media_type") == "IMAGE" and c.get("media_url"):
                urls.append(c["media_url"])
            elif c.get("thumbnail_url"):  # filho video -> usa a thumb
                urls.append(c["thumbnail_url"])
        if not urls and item.get("media_url"):
            urls = [item["media_url"]]
    elif mtype == "VIDEO":
        if item.get("thumbnail_url"):
            urls = [item["thumbnail_url"]]
    return urls[:limit] if limit else urls


def post_age_days(item):
    ts = item.get("timestamp")
    if not ts:
        return 0
    try:
        dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return 0
    return (datetime.now(timezone.utc) - dt).days


# ---------------------------------------------------------------- textos


def clean_caption(caption):
    return (caption or "").strip()


def strip_tags_and_urls(text):
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"#\S+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def build_tiktok_caption(item):
    base = clean_caption(item.get("caption"))
    return (f"{base}\n\n{TIKTOK_HASHTAGS}" if base else TIKTOK_HASHTAGS)[:2200]


def build_tiktok_photo_title(item):
    base = strip_tags_and_urls(clean_caption(item.get("caption")))
    return (base or "Casa Pellegrini")[:90]


def build_bluesky_text(item):
    base = clean_caption(item.get("caption")) or "Novidade da Casa Pellegrini"
    link = item.get("permalink") or ""
    budget = 300 - (len(link) + 2 if link else 0)
    snippet = base[: max(budget, 0)].rstrip()
    return f"{snippet}\n\n{link}".strip() if link else snippet


def build_discord_content(item):
    base = re.sub(r"[\r\n]+", " ", clean_caption(item.get("caption")))[:1400]
    link = item.get("permalink") or ""
    parts = ["**Novidade da Casa Pellegrini!**"]
    if base:
        parts.append(base)
    if link:
        parts.append(f"Veja no Instagram: {link}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------- discord


def publish_discord(item):
    if not DISCORD_WEBHOOK_URL:
        return False, "DISCORD_WEBHOOK_URL ausente"
    payload = {"content": build_discord_content(item)}

    imgs = image_urls(item, limit=4)
    if imgs:
        link = item.get("permalink") or ""
        # Varios embeds com a MESMA url viram galeria no Discord
        payload["embeds"] = [{"url": link or None, "image": {"url": u}} for u in imgs]
        payload["embeds"] = [
            {k: v for k, v in e.items() if v is not None} for e in payload["embeds"]
        ]

    if DRY_RUN:
        return True, f"DRY_RUN ({len(imgs)} imgs)"
    status, body = http_json(DISCORD_WEBHOOK_URL, "POST", payload)
    if status in (200, 204):
        return True, f"ok ({len(imgs)} imgs)"
    # se o embed falhar, tenta so texto
    if imgs:
        status2, body2 = http_json(
            DISCORD_WEBHOOK_URL, "POST", {"content": payload["content"]}
        )
        if status2 in (200, 204):
            return True, f"ok (texto; embed falhou HTTP {status})"
    return False, f"HTTP {status}: {str(body)[:300]}"


# ---------------------------------------------------------------- bluesky


def bluesky_session():
    status, body = http_json(
        f"{BLUESKY_PDS}/xrpc/com.atproto.server.createSession",
        "POST",
        {"identifier": BLUESKY_HANDLE, "password": BLUESKY_APP_PASSWORD},
    )
    if status != 200 or "accessJwt" not in body:
        raise RuntimeError(f"Bluesky login falhou (HTTP {status}): {str(body)[:300]}")
    return body["accessJwt"], body["did"]


def shrink_image(blob, limit=BLUESKY_MAX_BLOB):
    """Reduz a imagem ate caber no limite. Sem Pillow, devolve None se estourar."""
    if len(blob) <= limit:
        return blob, "image/jpeg"
    if not HAS_PIL:
        return None, None
    try:
        img = Image.open(io.BytesIO(blob))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        for quality in (85, 70, 55, 40):
            for scale in (1.0, 0.75, 0.5):
                buf = io.BytesIO()
                w, h = img.size
                resized = (
                    img
                    if scale == 1.0
                    else img.resize((int(w * scale), int(h * scale)))
                )
                resized.save(buf, format="JPEG", quality=quality, optimize=True)
                if buf.tell() <= limit:
                    return buf.getvalue(), "image/jpeg"
    except Exception as e:
        log(f"   ! compressao falhou: {e}")
    return None, None


def bluesky_upload_images(jwt, urls):
    blobs = []
    for u in urls[:BLUESKY_MAX_IMAGES]:
        raw, ctype = http_bytes(u)
        if not raw:
            continue
        raw, ctype2 = shrink_image(raw)
        if not raw:
            log("   ! imagem grande demais pro Bluesky (sem Pillow), pulando")
            continue
        status, body = http_upload_raw(
            f"{BLUESKY_PDS}/xrpc/com.atproto.repo.uploadBlob",
            raw,
            ctype2 or ctype or "image/jpeg",
            headers={"Authorization": f"Bearer {jwt}"},
        )
        if status == 200 and "blob" in body:
            blobs.append(body["blob"])
        else:
            log(f"   ! uploadBlob HTTP {status}: {str(body)[:200]}")
    return blobs


def publish_bluesky(item, session_cache):
    if not BLUESKY_APP_PASSWORD:
        return False, "BLUESKY_APP_PASSWORD ausente"
    if DRY_RUN:
        return True, "DRY_RUN"
    try:
        if not session_cache:
            session_cache.extend(bluesky_session())
        jwt, did = session_cache[0], session_cache[1]
    except Exception as e:
        return False, str(e)

    record = {
        "$type": "app.bsky.feed.post",
        "text": build_bluesky_text(item),
        "langs": ["pt-BR"],
        "createdAt": datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
    }

    imgs = image_urls(item, limit=BLUESKY_MAX_IMAGES)
    n_img = 0
    if imgs:
        blobs = bluesky_upload_images(jwt, imgs)
        if blobs:
            n_img = len(blobs)
            alt = strip_tags_and_urls(clean_caption(item.get("caption")))[:280]
            record["embed"] = {
                "$type": "app.bsky.embed.images",
                "images": [
                    {"alt": alt or "Casa Pellegrini", "image": b} for b in blobs
                ],
            }

    status, body = http_json(
        f"{BLUESKY_PDS}/xrpc/com.atproto.repo.createRecord",
        "POST",
        {"repo": did, "collection": "app.bsky.feed.post", "record": record},
        headers={"Authorization": f"Bearer {jwt}"},
    )
    if status == 200:
        return True, f"{body.get('uri', 'ok')} ({n_img} imgs)"
    return False, f"HTTP {status}: {str(body)[:300]}"


# ---------------------------------------------------------------- tiktok


def publish_tiktok(item):
    """Video -> post de video. Foto/carrossel -> photo post (ate 35 imagens)."""
    if not ZERNIO_API_KEY or not ZERNIO_TIKTOK_ACCOUNT_ID:
        return False, "credenciais Zernio ausentes"

    mtype = item.get("media_type")
    settings = {
        "privacy_level": "PUBLIC_TO_EVERYONE",
        "allow_comment": True,
        # Obrigatorios por exigencia legal do TikTok
        "content_preview_confirmed": True,
        "express_consent_given": True,
    }

    if mtype == "VIDEO":
        if not item.get("media_url"):
            return False, "sem media_url"
        content = build_tiktok_caption(item)
        media = [{"type": "video", "url": item["media_url"]}]
        settings.update({"allow_duet": True, "allow_stitch": True})
    else:
        # IMAGE ou CAROUSEL_ALBUM -> photo post
        imgs = image_urls(item, limit=35)
        if not imgs:
            return False, "sem imagem utilizavel"
        content = build_tiktok_photo_title(item)  # titulo: 90 chars, sem hashtag
        media = [{"type": "image", "url": u} for u in imgs]
        settings.update(
            {
                "media_type": "photo",
                "photo_cover_index": 0,
                "description": build_tiktok_caption(item)[:4000],
                "auto_add_music": True,
            }
        )

    if DRY_RUN:
        return True, f"DRY_RUN ({mtype}, {len(media)} midias)"

    payload = {
        "content": content,
        "mediaItems": media,
        "platforms": [{"platform": "tiktok", "accountId": ZERNIO_TIKTOK_ACCOUNT_ID}],
        "tiktokSettings": settings,
        "publishNow": True,
    }
    status, body = http_json(
        f"{ZERNIO_API_BASE}/posts",
        "POST",
        payload,
        headers={"Authorization": f"Bearer {ZERNIO_API_KEY}"},
    )
    if status in (200, 201):
        post = (body or {}).get("post") or {}
        return True, (
            f"id={post.get('_id', '?')} status={post.get('status', '?')} "
            f"({mtype}, {len(media)} midias)"
        )
    return False, f"HTTP {status}: {str(body)[:400]}"


# ---------------------------------------------------------------- main


def safe_call(fn, *args):
    """Isola cada rede: excecao numa nao derruba o resto da rodada."""
    try:
        return fn(*args)
    except Exception as e:
        return False, f"EXCECAO: {e}"


def main():
    if not IG_ACCESS_TOKEN:
        log("ERRO: IG_ACCESS_TOKEN nao definido")
        return 1

    log(f"=== cross-posting Casa Pellegrini (DRY_RUN={DRY_RUN}, Pillow={HAS_PIL}) ===")
    log(
        "secrets (tamanho esperado): "
        f"IG={len(IG_ACCESS_TOKEN)} | ZERNIO_KEY={len(ZERNIO_API_KEY)}/67 "
        f"| ZERNIO_ACC={len(ZERNIO_TIKTOK_ACCOUNT_ID)}/24 "
        f"| BSKY_HANDLE={len(BLUESKY_HANDLE)}/26 "
        f"| BSKY_PASS={len(BLUESKY_APP_PASSWORD)}/19 "
        f"| DISCORD={len(DISCORD_WEBHOOK_URL)}/121"
    )

    state = load_state(STATE_FILE)
    try:
        media = fetch_instagram_media()
    except Exception as e:
        log(f"ERRO fatal: {e}")
        return 1

    media = sorted(media, key=lambda m: m.get("timestamp") or "")

    session_cache = []
    tiktok_done = 0
    changed = False
    resumo = []

    for item in media:
        pid = item.get("id")
        if not pid:
            continue

        username = (item.get("username") or "").lower()
        if EXPECTED_USERNAME and username != EXPECTED_USERNAME.lower():
            log(f"- {pid}: pulando (username={username!r}, provavel collab)")
            continue

        if MAX_AGE_DAYS and post_age_days(item) > MAX_AGE_DAYS:
            log(f"- {pid}: pulando (mais velho que {MAX_AGE_DAYS} dias)")
            continue

        entry = state.get(pid) or {}
        if not isinstance(entry, dict):
            entry = {p: True for p in PLATFORMS}

        pendentes = [p for p in PLATFORMS if not entry.get(p)]
        if not pendentes:
            continue

        mtype = item.get("media_type")
        log(f"> {pid} ({mtype}, {item.get('timestamp')}) pendentes={pendentes}")

        if "discord" in pendentes:
            ok, detail = safe_call(publish_discord, item)
            log(f"   discord: {'OK' if ok else 'FALHOU'} -> {detail}")
            if ok:
                entry["discord"] = True
                changed = True
                resumo.append(f"discord:{pid}")

        if "bluesky" in pendentes:
            ok, detail = safe_call(publish_bluesky, item, session_cache)
            log(f"   bluesky: {'OK' if ok else 'FALHOU'} -> {detail}")
            if ok:
                entry["bluesky"] = True
                changed = True
                resumo.append(f"bluesky:{pid}")

        if "tiktok" in pendentes:
            if tiktok_done >= MAX_TIKTOK_PER_RUN:
                log(
                    f"   tiktok: adiado (teto de {MAX_TIKTOK_PER_RUN}/rodada; "
                    "sai na proxima)"
                )
            else:
                ok, detail = safe_call(publish_tiktok, item)
                log(f"   tiktok: {'OK' if ok else 'FALHOU'} -> {detail}")
                if ok:
                    entry["tiktok"] = True
                    tiktok_done += 1
                    changed = True
                    resumo.append(f"tiktok:{pid}")
                    time.sleep(2)

        entry["permalink"] = item.get("permalink")
        entry["timestamp"] = item.get("timestamp")
        entry["media_type"] = mtype
        state[pid] = entry

    if DRY_RUN:
        log("DRY_RUN: estado NAO salvo (nada foi marcado como publicado)")
    elif changed:
        save_state(STATE_FILE, state)
    else:
        log("nada novo para publicar")

    log(f"=== fim: {len(resumo)} publicacoes -> {', '.join(resumo) or 'nenhuma'} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
