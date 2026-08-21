#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Instagram -> YouTube (Casa Pellegrini) — backfill do acervo + Reels NOVOS (substitui o Zap do YouTube)
- Reels novos (>= CUTOFF) tem PRIORIDADE e sobem na proxima rodada (cron 6h)
- Acervo antigo (< CUTOFF) completa o teto diario (DAILY_CAP=5, quota ~6/dia)
- Listagem SEM media_url (evita 400 por midia com copyright); media_url buscada por item no upload
- Retry com backoff na listagem; erro de 1 video nao derruba a rodada
- Estado em data/youtube-backfill-state.json (done/failed/day_count)
Secrets/env: IG_ACCESS_TOKEN, YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN, MAX_UPLOADS(=cap diario)
"""
import json, os, time, urllib.request, urllib.parse
from datetime import datetime, timezone

IG_USER_ID = "17841404743438046"
CUTOFF = "2026-07-01T00:00:00"      # < cutoff = acervo antigo; >= cutoff = "novos" (antes eram do Zap)
STATE_FILE = "data/youtube-backfill-state.json"
DAILY_CAP = int(os.environ.get("MAX_UPLOADS", "5"))
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

def http(url, data=None, headers=None, method=None, raw=False, tries=1):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
            with urllib.request.urlopen(req, timeout=120) as r:
                b = r.read()
                return b if raw else json.loads(b.decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")[:300]
            last = RuntimeError(f"HTTP {e.code}: {body}")
            print(f"  [http] tentativa {i+1}/{tries} falhou: {last}")
            if e.code in (400, 500, 502, 503, 429) and i < tries - 1:
                time.sleep(15 * (i + 1)); continue
            raise last
        except Exception as e:
            last = e
            print(f"  [http] tentativa {i+1}/{tries} falhou: {e}")
            if i < tries - 1:
                time.sleep(15 * (i + 1)); continue
            raise
    raise last

def ig_list_all():
    """Lista videos SEM media_url (id/caption/tipo/timestamp), mais antigo primeiro."""
    tok = os.environ["IG_ACCESS_TOKEN"]
    fields = "id,caption,media_type,media_product_type,permalink,timestamp,thumbnail_url,children{id,media_type}"
    url = f"{GRAPH}/{IG_USER_ID}/media?fields={fields}&limit=100&access_token={tok}"
    items = []
    while url:
        d = http(url, tries=3)
        items += d.get("data", [])
        url = d.get("paging", {}).get("next")
    def eh_elegivel(m):
        if m.get("media_type") == "VIDEO":
            return True
        if m.get("media_type") == "CAROUSEL_ALBUM":
            filhos = (m.get("children") or {}).get("data", [])
            return sum(1 for c in filhos if c.get("media_type") == "IMAGE") >= 2
        return False
    vids = [m for m in items if eh_elegivel(m)]
    vids.sort(key=lambda m: m["timestamp"])
    return vids

def ig_media_url(media_id):
    tok = os.environ["IG_ACCESS_TOKEN"]
    return http(f"{GRAPH}/{media_id}?fields=media_url&access_token={tok}", tries=2).get("media_url")

def yt_access_token():
    body = urllib.parse.urlencode({
        "client_id": os.environ["YT_CLIENT_ID"],
        "client_secret": os.environ["YT_CLIENT_SECRET"],
        "refresh_token": os.environ["YT_REFRESH_TOKEN"],
        "grant_type": "refresh_token"}).encode()
    return http("https://oauth2.googleapis.com/token", data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"})["access_token"]

def make_title(caption):
    import re
    if caption:
        line = caption.strip().splitlines()[0]
        line = re.sub(r"#\w+", "", line).strip(" -–·|.,!🔥")
        line = re.sub(r"\s{2,}", " ", line).strip()
        if len(line) >= 8:
            return (line[:88] + " #Shorts")[:100]
    return None

def gemini_title(thumb_url):
    """Sem legenda: Gemini olha o thumbnail e inventa um titulo curto. Retorna None se nao der."""
    import base64
    key = os.environ.get("GEMINI_API_KEY")
    if not (key and thumb_url):
        return None
    try:
        img = http(thumb_url, raw=True)
        body = json.dumps({
            "contents": [{"parts": [
                {"text": "Esta imagem é um frame de um vídeo antigo do Instagram da Casa Pellegrini, "
                         "bar e restaurante no Centro Histórico de Petrópolis (hambúrguer artesanal, chopp gelado, jogos na TV). "
                         "Escreva UM título curto de YouTube Shorts em português, natural e chamativo, máx 70 caracteres, "
                         "sem hashtags e sem aspas. Responda SÓ o título."},
                {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(img).decode()}}]}],
            "generationConfig": {"temperature": 0.8, "maxOutputTokens": 200,
                                  "thinkingConfig": {"thinkingBudget": 0}}}).encode()
        d = http(f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={key}",
                 data=body, headers={"Content-Type": "application/json"}, tries=2)
        t = d["candidates"][0]["content"]["parts"][0]["text"].strip().strip('"').splitlines()[0][:70]
        return (t + " #Shorts") if len(t) >= 8 else None
    except Exception as e:
        print(f"  gemini_title falhou: {str(e)[:100]}")
        return None

MESES = ["", "jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]

def fallback_title(ts):
    try:
        ano, mes = ts[:4], MESES[int(ts[5:7])]
        return f"Casa Pellegrini em {mes}/{ano} 🍺 #Shorts"
    except Exception:
        return "Casa Pellegrini — o point do Centro Histórico de Petrópolis #Shorts"

def build_slideshow(image_blobs, workdir="/tmp/slides"):
    """Monta Short vertical 1080x1920: foto centrada sobre fundo desfocado, 2.5s/foto + musica (se houver em assets/audio)."""
    import glob, hashlib, subprocess, shutil
    if not shutil.which("ffmpeg"):
        print("  instalando ffmpeg no runner...")
        r = subprocess.run(["sudo", "apt-get", "install", "-y", "-qq", "ffmpeg"], capture_output=True)
        if r.returncode != 0:
            subprocess.run(["sudo", "apt-get", "update", "-qq"], capture_output=True)
            subprocess.run(["sudo", "apt-get", "install", "-y", "-qq", "ffmpeg"], check=True, capture_output=True)
    shutil.rmtree(workdir, ignore_errors=True)
    os.makedirs(workdir, exist_ok=True)
    paths = []
    for i, blob in enumerate(image_blobs):
        p = f"{workdir}/img{i:02d}.jpg"
        open(p, "wb").write(blob)
        paths.append(p)
    cmd = ["ffmpeg", "-y"]
    for p in paths:
        cmd += ["-loop", "1", "-t", "2.5", "-i", p]
    tracks = sorted(glob.glob("assets/audio/*.mp3")) + sorted(glob.glob("assets/audio/*.m4a"))
    audio_idx = None
    if tracks:
        h = int(hashlib.sha256("".join(paths).encode()).hexdigest(), 16)
        cmd += ["-i", tracks[h % len(tracks)]]
        audio_idx = len(paths)
    fc = []
    for i in range(len(paths)):
        fc.append(f"[{i}:v]split[a{i}][b{i}];"
                  f"[a{i}]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20:2[bg{i}];"
                  f"[b{i}]scale=1000:1700:force_original_aspect_ratio=decrease[fg{i}];"
                  f"[bg{i}][fg{i}]overlay=(W-w)/2:(H-h)/2,setsar=1,fps=30[v{i}]")
    fc.append("".join(f"[v{i}]" for i in range(len(paths))) + f"concat=n={len(paths)}:v=1:a=0[vout]")
    dur = 2.5 * len(paths)
    maps = ["-map", "[vout]"]
    if audio_idx is not None:
        fc.append(f"[{audio_idx}:a]atrim=0:{dur},afade=t=out:st={dur-1.5}:d=1.5[aout]")
        maps += ["-map", "[aout]"]
    out = f"{workdir}/short.mp4"
    cmd += ["-filter_complex", ";".join(fc)] + maps + ["-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-t", str(dur), out]
    subprocess.run(cmd, check=True, capture_output=True)
    return open(out, "rb").read()

SHORTS_MAX_SEG = 180          # limite do YouTube Shorts (3 min)

def _garante_ffmpeg():
    import shutil, subprocess
    if shutil.which("ffmpeg"):
        return
    print("  instalando ffmpeg no runner...")
    r = subprocess.run(["sudo", "apt-get", "install", "-y", "-qq", "ffmpeg"], capture_output=True)
    if r.returncode != 0:
        subprocess.run(["sudo", "apt-get", "update", "-qq"], capture_output=True)
        subprocess.run(["sudo", "apt-get", "install", "-y", "-qq", "ffmpeg"], check=True, capture_output=True)

def sondar_video(caminho):
    """Devolve (duracao_seg, largura, altura) via ffprobe."""
    import subprocess
    _garante_ffmpeg()
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height:format=duration",
         "-of", "json", caminho],
        capture_output=True, text=True, check=True)
    d = json.loads(r.stdout or "{}")
    fluxo = (d.get("streams") or [{}])[0]
    dur = float((d.get("format") or {}).get("duration") or 0)
    return dur, int(fluxo.get("width") or 0), int(fluxo.get("height") or 0)

def para_vertical(video_bytes, workdir="/tmp/vert"):
    """
    Deixa o video elegivel pra Shorts. O YouTube classifica como Short por
    duracao (<=3 min) E formato (vertical/quadrado) — a hashtag #Shorts no
    titulo nao forca nada. Video horizontal virava video comum e nao contava
    view de Short (descoberto em 21/08/2026).
    Horizontal -> 1080x1920 com fundo desfocado, mesma tecnica do build_slideshow.
    Vertical passa direto, sem reencodar. Devolve (bytes, nota).
    """
    import shutil, subprocess
    _garante_ffmpeg()
    shutil.rmtree(workdir, ignore_errors=True)
    os.makedirs(workdir, exist_ok=True)
    entrada = f"{workdir}/in.mp4"
    open(entrada, "wb").write(video_bytes)

    dur, w, h = sondar_video(entrada)
    if dur > SHORTS_MAX_SEG:
        raise RuntimeError(
            f"video de {dur:.0f}s passa do limite de Shorts ({SHORTS_MAX_SEG}s) — pulado")
    if w == 0 or h == 0:
        return video_bytes, "sem stream de video legivel; enviado como veio"
    if h > w:
        return video_bytes, f"ja vertical ({w}x{h}, {dur:.0f}s)"

    saida = f"{workdir}/out.mp4"
    fc = ("[0:v]split[a][b];"
          "[a]scale=1080:1920:force_original_aspect_ratio=increase,"
          "crop=1080:1920,boxblur=20:2[bg];"
          "[b]scale=1000:1700:force_original_aspect_ratio=decrease[fg];"
          "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,fps=30[vout]")
    cmd = ["ffmpeg", "-y", "-i", entrada, "-filter_complex", fc,
           "-map", "[vout]", "-c:v", "libx264", "-pix_fmt", "yuv420p",
           "-preset", "veryfast"]
    # so mapeia audio se existir, senao o ffmpeg aborta
    tem_audio = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=codec_type", "-of", "csv=p=0", entrada],
        capture_output=True, text=True).stdout.strip()
    if tem_audio:
        cmd += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "128k"]
    cmd += [saida]
    subprocess.run(cmd, check=True, capture_output=True)
    return open(saida, "rb").read(), f"convertido {w}x{h} -> 1080x1920 (fundo desfocado), {dur:.0f}s"


def yt_upload(token, video_bytes, title, description):
    meta = {"snippet": {"title": title, "description": description[:4900], "tags": TAGS,
                        "categoryId": "22", "defaultLanguage": "pt-BR", "defaultAudioLanguage": "pt-BR"},
            "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False}}
    req = urllib.request.Request(
        "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",
        data=json.dumps(meta).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8",
                 "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(len(video_bytes))})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            upload_url = r.headers["Location"]
    except urllib.error.HTTPError as e:
        # sem isso o erro chega como "HTTP Error 400: Bad Request", sem motivo
        corpo = e.read().decode("utf-8", "ignore")[:300]
        raise RuntimeError(f"HTTP {e.code} ao abrir a sessao de upload: {corpo}") from None
    d = http(upload_url, data=video_bytes, method="PUT",
             headers={"Authorization": f"Bearer {token}", "Content-Type": "video/mp4"})
    return d.get("id")

def main():
    st = {"done": [], "failed": {}, "day": "", "day_count": 0}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            st.update(json.load(f))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if st.get("day") != today:
        st["day"], st["day_count"] = today, 0

    vids = ig_list_all()
    skip = set(st["done"]) | set(st["failed"])
    novos    = [v for v in vids if v["id"] not in skip and v["timestamp"] >= CUTOFF]
    backlog  = [v for v in vids if v["id"] not in skip and v["timestamp"] < CUTOFF]
    # Reels NOVOS sempre sobem (furam o teto — sao raros); o teto diario vale so pro ACERVO
    budget_backlog = max(0, DAILY_CAP - st["day_count"])
    fila = novos + backlog[:budget_backlog]
    print(f"videos: {len(vids)} | novos pendentes: {len(novos)} (sobem sempre) | backlog: {len(backlog)} | "
          f"acervo hoje: {st['day_count']}/{DAILY_CAP} | vai subir agora: {len(fila)}")
    if not fila:
        return
    token = yt_access_token()
    for v in fila:
        vid_id, ts = v["id"], v["timestamp"]
        rotulo = "NOVO" if ts >= CUTOFF else "acervo"
        try:
            if v.get("media_type") == "CAROUSEL_ALBUM":
                filhos = [c["id"] for c in (v.get("children") or {}).get("data", []) if c.get("media_type") == "IMAGE"]
                print(f"[{rotulo}] carrossel {vid_id} ({ts}) — {len(filhos)} fotos -> slideshow")
                blobs = []
                for cid in filhos[:10]:
                    cu = ig_media_url(cid)
                    if cu:
                        blobs.append(http(cu, raw=True))
                if len(blobs) < 2:
                    raise RuntimeError("carrossel sem fotos acessiveis")
                data = build_slideshow(blobs)
            else:
                murl = ig_media_url(vid_id)
                if not murl:
                    raise RuntimeError("sem media_url (provavel copyright/indisponivel)")
                print(f"[{rotulo}] baixando {vid_id} ({ts})")
                data = http(murl, raw=True)
                data, nota = para_vertical(data)
                print(f"  [formato] {nota}")
            title = (make_title(v.get("caption"))
                     or gemini_title(v.get("thumbnail_url"))
                     or fallback_title(ts))
            desc = (v.get("caption") or "").strip() + SITE_BLOCK
            yt_id = yt_upload(token, data, title, desc)
            print(f"  -> OK https://youtube.com/shorts/{yt_id} | {title}")
            st["done"].append(vid_id)
            if rotulo == "acervo":
                st["day_count"] += 1
        except Exception as e:
            msg = str(e)[:200]
            if "quota" in msg.lower():
                print("Quota do YouTube esgotada — parando (retoma na proxima rodada)."); break
            print(f"  -> FALHA {vid_id}: {msg}")
            st["failed"][vid_id] = msg
        finally:
            os.makedirs("data", exist_ok=True)
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(st, f, ensure_ascii=False, indent=1)
    print(f"Rodada concluida. Hoje: {st['day_count']}/{DAILY_CAP}.")

if __name__ == "__main__":
    main()
