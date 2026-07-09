#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Instagram -> Google Business Profile (Casa Pellegrini) — substitui o Zap "Posts do Instagram para o GMN" v10
Comportamento herdado do Zap:
- Video > 59s: descartado. Carrossel: 1a foto vira capa do post; TODAS as fotos vao pra galeria.
- Carrossel/video: legenda ganha "📱 Veja o post completo no Instagram: <permalink>". Cap 1490 chars.
Melhorias vs Zap:
- Botao CTA "Saiba mais" -> site com UTM em todo post
- Video tambem vai pra GALERIA (alem do post)
- Retry/backoff, estado idempotente, auto-seed na 1a rodada (nao re-posta nada antigo)
Secrets: IG_ACCESS_TOKEN, GBP_CLIENT_ID, GBP_CLIENT_SECRET, GBP_REFRESH_TOKEN, DRY_RUN
"""
import json, os, time, urllib.request, urllib.parse

IG_USER_ID = "17841404743438046"
ACCOUNT = "101169923071811421747"
LOCATION = "4247745759631447502"
GRAPH = "https://graph.facebook.com/v21.0"
GBP = f"https://mybusiness.googleapis.com/v4/accounts/{ACCOUNT}/locations/{LOCATION}"
STATE_FILE = "data/gbp-post-sync-state.json"
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
CTA_URL = "https://casapellegrini.com.br/?utm_source=google&utm_medium=gbp_post&utm_campaign=insta_sync"

def http(url, data=None, headers=None, method=None, tries=1):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")[:300]
            last = RuntimeError(f"HTTP {e.code}: {body}")
            print(f"  [http] tentativa {i+1}/{tries}: {last}")
            if i < tries - 1:
                time.sleep(15 * (i + 1)); continue
            raise last
        except Exception as e:
            last = e
            if i < tries - 1:
                time.sleep(15 * (i + 1)); continue
            raise
    raise last

def ig_list(all_pages=False):
    tok = os.environ["IG_ACCESS_TOKEN"]
    fields = "id,caption,media_type,media_product_type,media_url,thumbnail_url,permalink,timestamp,children{id,media_type,media_url}"
    url = f"{GRAPH}/{IG_USER_ID}/media?fields={fields}&limit=50&access_token={tok}"
    items = []
    while url:
        d = http(url, tries=3)
        items += d.get("data", [])
        url = d.get("paging", {}).get("next") if all_pages else None
    return items

def gbp_token():
    body = urllib.parse.urlencode({
        "client_id": os.environ["GBP_CLIENT_ID"],
        "client_secret": os.environ["GBP_CLIENT_SECRET"],
        "refresh_token": os.environ["GBP_REFRESH_TOKEN"],
        "grant_type": "refresh_token"}).encode()
    return http("https://oauth2.googleapis.com/token", data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"})["access_token"]

def video_duration(url):
    """Baixa o video e mede duracao com ffprobe (IG nao expoe duration na API)."""
    import subprocess, tempfile
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=120) as r:
            f.write(r.read())
        path = f.name
    r = subprocess.run(["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                        "-of", "csv=p=0", path], capture_output=True, text=True)
    os.unlink(path)
    try:
        return float(r.stdout.strip())
    except Exception:
        return 0.0

def caption_final(m):
    cap = (m.get("caption") or "").strip()
    permalink = m.get("permalink") or ""
    if m.get("media_type") in ("CAROUSEL_ALBUM", "VIDEO") and permalink and permalink not in cap:
        cap += f"\n\n📱 Veja o post completo no Instagram: {permalink}"
    if len(cap) > 1490:
        cap = cap[:1487] + "..."
    return cap

def gbp_create_post(tok, summary, media_format, source_url):
    body = {"languageCode": "pt-BR", "topicType": "STANDARD", "summary": summary,
            "callToAction": {"actionType": "LEARN_MORE", "url": CTA_URL}}
    if source_url:
        body["media"] = [{"mediaFormat": media_format, "sourceUrl": source_url}]
    return http(f"{GBP}/localPosts", data=json.dumps(body).encode(), method="POST",
                headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}, tries=2)

def gbp_gallery(tok, media_format, source_url):
    body = {"mediaFormat": media_format, "locationAssociation": {"category": "ADDITIONAL"},
            "sourceUrl": source_url}
    return http(f"{GBP}/media", data=json.dumps(body).encode(), method="POST",
                headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}, tries=2)

def main():
    st = {"done": {}, "seeded": False}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)

    def save():
        os.makedirs("data", exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)

    if not st.get("seeded"):
        todos = ig_list(all_pages=True)
        for m in todos:
            st["done"][m["id"]] = "seed (pre-existente, tratado pelo Zap)"
        st["seeded"] = True
        save()
        print(f"PRIMEIRA RODADA: {len(todos)} midias existentes semeadas como feitas. "
              "A partir de agora so posts NOVOS vao pro GBP. Pode desligar o Zap.")
        return

    itens = [m for m in ig_list() if m["id"] not in st["done"]]
    itens.sort(key=lambda m: m.get("timestamp", ""))
    print(f"novos: {len(itens)}")
    if not itens:
        return
    tok = gbp_token()
    for m in itens:
        mid, mtype = m["id"], m.get("media_type")
        cap = caption_final(m)
        try:
            if m.get("media_product_type") == "STORY":
                st["done"][mid] = "story ignorada"; save(); continue
            if mtype == "VIDEO":
                dur = video_duration(m.get("media_url") or "")
                if dur > 59:
                    print(f"[skip] video {mid} com {dur:.0f}s (>59)")
                    st["done"][mid] = f"video {dur:.0f}s descartado"; save(); continue
                if DRY_RUN:
                    print(f"[DRY] VIDEO {mid}: post + galeria | {cap[:80]}")
                else:
                    gbp_create_post(tok, cap, "VIDEO", m.get("media_url"))
                    gbp_gallery(tok, "VIDEO", m.get("media_url"))
                    print(f"[ok] video {mid} -> post + galeria")
            elif mtype == "CAROUSEL_ALBUM":
                fotos = [c.get("media_url") for c in (m.get("children") or {}).get("data", [])
                         if c.get("media_type") == "IMAGE" and c.get("media_url")]
                if not fotos:
                    st["done"][mid] = "carrossel sem fotos"; save(); continue
                if DRY_RUN:
                    print(f"[DRY] CARROSSEL {mid}: post capa + {len(fotos)} fotos na galeria | {cap[:80]}")
                else:
                    gbp_create_post(tok, cap, "PHOTO", fotos[0])
                    for u in fotos:
                        try: gbp_gallery(tok, "PHOTO", u)
                        except Exception as e: print(f"    galeria falhou p/ 1 foto: {str(e)[:120]}")
                    print(f"[ok] carrossel {mid} -> post + {len(fotos)} fotos na galeria")
            else:  # IMAGE
                if DRY_RUN:
                    print(f"[DRY] FOTO {mid}: post + galeria | {cap[:80]}")
                else:
                    gbp_create_post(tok, cap, "PHOTO", m.get("media_url"))
                    gbp_gallery(tok, "PHOTO", m.get("media_url"))
                    print(f"[ok] foto {mid} -> post + galeria")
            if not DRY_RUN:
                st["done"][mid] = m.get("timestamp", "ok")
                save()
        except Exception as e:
            print(f"[FALHA] {mid}: {str(e)[:200]} (tenta de novo na proxima rodada)")
    print("fim.")

if __name__ == "__main__":
    main()
