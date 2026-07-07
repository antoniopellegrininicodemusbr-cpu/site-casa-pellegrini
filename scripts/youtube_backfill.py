#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Backfill Instagram -> YouTube Shorts (Casa Pellegrini)
- Le midias antigas do Instagram (Graph API, token permanente)
- Sobe como Shorts no canal via YouTube Data API v3 (projeto pellegrini-app, quota propria)
- SO processa posts ANTERIORES a CUTOFF (novos ficam com o Zap "Reels -> YouTube Shorts")
- Estado em data/youtube-backfill-state.json (nunca re-processa)
- MAX_UPLOADS por rodada (default 5 = ~8000/10000 pontos de quota)
Secrets/env: IG_ACCESS_TOKEN, YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN, MAX_UPLOADS (opcional)
"""
import json, os, re, sys, tempfile
import urllib.request, urllib.parse, urllib.error

IG_USER_ID = "17841404743438046"          # @casapellegrini (IG Business)
CUTOFF = "2026-07-01T00:00:00"            # NAO tocar em posts >= cutoff (territorio do Zap)
STATE_FILE = "data/youtube-backfill-state.json"
MAX_UPLOADS = int(os.environ.get("MAX_UPLOADS", "5"))
GRAPH = "https://graph.facebook.com/v21.0"

SITE_BLOCK = (
    "\n\n🍔🍺 Casa Pellegrini — o point do Centro Histórico de Petrópolis"
    "\n📍 Rua Treze de Maio, 184 — Centro, Petrópolis/RJ"
    "\n🌐 https://casapellegrini.com.br"
    "\n📸 Instagram: https://instagram.com/casapellegrini"
    "\n\n#Shorts #CasaPellegrini #Petropolis #Hamburguer #Chopp #HappyHour"
)
TAGS = ["casa pellegrini", "petropolis", "bar petropolis", "hamburgueria petropolis",
        "hamburguer artesanal", "chopp gelado", "happy hour petropolis", "shorts"]

def http(url, data=None, headers=None, method=None, raw=False):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=120) as r:
        b = r.read()
        return b if raw else json.loads(b.decode("utf-8"))

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"done": [], "failed": {}}

def save_state(st):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)

def ig_list_all():
    """Lista todas as midias (paginado), retorna so videos/reels antigos, mais antigo primeiro."""
    tok = os.environ["IG_ACCESS_TOKEN"]
    fields = "id,caption,media_type,media_product_type,media_url,permalink,timestamp"
    url = f"{GRAPH}/{IG_USER_ID}/media?fields={fields}&limit=100&access_token={tok}"
    items = []
    while url:
        d = http(url)
        items += d.get("data", [])
        url = d.get("paging", {}).get("next")
    vids = [m for m in items
            if m.get("media_type") == "VIDEO"
            and m.get("timestamp", "9999") < CUTOFF
            and m.get("media_url")]
    vids.sort(key=lambda m: m["timestamp"])
    return vids

def yt_access_token():
    body = urllib.parse.urlencode({
        "client_id": os.environ["YT_CLIENT_ID"],
        "client_secret": os.environ["YT_CLIENT_SECRET"],
        "refresh_token": os.environ["YT_REFRESH_TOKEN"],
        "grant_type": "refresh_token"}).encode()
    d = http("https://oauth2.googleapis.com/token", data=body,
             headers={"Content-Type": "application/x-www-form-urlencoded"})
    return d["access_token"]

def make_title(caption):
    if caption:
        line = caption.strip().splitlines()[0]
        line = re.sub(r"#\w+", "", line).strip(" -–·|.,!🔥")
        line = re.sub(r"\s{2,}", " ", line).strip()
        if len(line) >= 8:
            t = line[:88]
            return (t + " #Shorts")[:100]
    return "Casa Pellegrini — o point do Centro Histórico de Petrópolis #Shorts"

def yt_upload(token, video_bytes, title, description):
    meta = {
        "snippet": {"title": title, "description": description[:4900], "tags": TAGS,
                     "categoryId": "22", "defaultLanguage": "pt-BR", "defaultAudioLanguage": "pt-BR"},
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
    }
    init_url = ("https://www.googleapis.com/upload/youtube/v3/videos"
                "?uploadType=resumable&part=snippet,status")
    req = urllib.request.Request(init_url, data=json.dumps(meta).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8",
                 "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(len(video_bytes))})
    with urllib.request.urlopen(req, timeout=60) as r:
        upload_url = r.headers["Location"]
    d = http(upload_url, data=video_bytes, method="PUT",
             headers={"Authorization": f"Bearer {token}", "Content-Type": "video/mp4"})
    return d.get("id")

def main():
    st = load_state()
    done = set(st["done"])
    vids = ig_list_all()
    pending = [v for v in vids if v["id"] not in done and v["id"] not in st["failed"]]
    print(f"Videos antigos no IG: {len(vids)} | ja feitos: {len(done)} | pendentes: {len(pending)}")
    if not pending:
        print("Backfill completo — nada a fazer. Pode desativar o workflow.")
        return
    token = yt_access_token()
    uploaded = 0
    for v in pending[:MAX_UPLOADS]:
        vid_id = v["id"]
        try:
            print(f"Baixando {vid_id} ({v['timestamp']}) {v.get('permalink','')}")
            data = http(v["media_url"], raw=True)
            title = make_title(v.get("caption"))
            desc = (v.get("caption") or "").strip() + SITE_BLOCK
            yt_id = yt_upload(token, data, title, desc)
            print(f"  -> OK https://youtube.com/shorts/{yt_id} | {title}")
            st["done"].append(vid_id)
            uploaded += 1
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")[:400]
            if e.code == 403 and "quota" in body.lower():
                print("Quota do YouTube esgotada por hoje — parando (retoma amanha).")
                break
            print(f"  -> FALHA {vid_id}: HTTP {e.code} {body}")
            st["failed"][vid_id] = f"HTTP {e.code}"
        except Exception as e:
            print(f"  -> FALHA {vid_id}: {e}")
            st["failed"][vid_id] = str(e)[:200]
        finally:
            save_state(st)
    print(f"Rodada concluida: {uploaded} enviados. Restam {len(pending) - uploaded}.")

if __name__ == "__main__":
    main()
