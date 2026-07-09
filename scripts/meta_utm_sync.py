#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
UTM automatico nos anuncios da Meta (Casa Pellegrini)
- Varre anuncios ATIVOS sem url_tags e aplica o template dinamico (a Meta preenche campanha/adset/anuncio no clique)
- O url_tags mora no CRIATIVO (imutavel) -> cria copia do criativo com utm e troca no anuncio
  (anuncio volta pra revisao rapida da Meta -> por isso MAX_PER_RUN=5 por rodada, gradual)
- Cada anuncio e tocado UMA vez na vida (estado); falhas nao re-tentam sem limpar o estado
Secrets/env: META_USER_TOKEN, META_AD_ACCOUNT_ID, DRY_RUN, MAX_PER_RUN
"""
import json, os, time, urllib.request, urllib.parse

TOK = os.environ["META_USER_TOKEN"]
ACT = os.environ.get("META_AD_ACCOUNT_ID", "667580495472069").replace("act_", "")
GRAPH = "https://graph.facebook.com/v21.0"
STATE_FILE = "data/meta-utm-state.json"
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "5"))
URL_TAGS = ("utm_source=facebook&utm_medium=paid"
            "&utm_campaign={{campaign.name}}&utm_term={{adset.name}}&utm_content={{ad.name}}")

def http(url, data=None, method=None, tries=2):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, method=method)
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            last = RuntimeError(f"HTTP {e.code}: {e.read().decode('utf-8','ignore')[:250]}")
            if i < tries - 1: time.sleep(10); continue
            raise last
    raise last

def main():
    st = {"done": {}, "failed": {}}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    def save():
        os.makedirs("data", exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)

    url = (f"{GRAPH}/act_{ACT}/ads?fields=id,name,effective_status,"
           f"creative{{id,url_tags,effective_object_story_id}},campaign{{name}}&limit=100&access_token={TOK}")
    ads = []
    while url:
        d = http(url)
        ads += d.get("data", [])
        url = d.get("paging", {}).get("next")
    pendentes = [a for a in ads
                 if a.get("effective_status") == "ACTIVE"
                 and not (a.get("creative") or {}).get("url_tags")
                 and a["id"] not in st["done"] and a["id"] not in st["failed"]]
    print(f"ativos: {sum(1 for a in ads if a.get('effective_status')=='ACTIVE')} | "
          f"sem utm pendentes: {len(pendentes)} | nesta rodada: {min(len(pendentes), MAX_PER_RUN)}")
    for a in pendentes[:MAX_PER_RUN]:
        aid, nome = a["id"], a["name"]
        cr = a.get("creative") or {}
        story = cr.get("effective_object_story_id")
        try:
            if not story:
                raise RuntimeError("criativo sem object_story_id (formato nao suportado — tratar manual)")
            if DRY_RUN:
                print(f"[DRY] {nome}: criaria copia do criativo {cr.get('id')} com utm e trocaria no anuncio")
                continue
            novo = http(f"{GRAPH}/act_{ACT}/adcreatives", method="POST",
                        data=urllib.parse.urlencode({
                            "object_story_id": story,
                            "url_tags": URL_TAGS,
                            "name": f"UTM - {nome}"[:99],
                            "access_token": TOK}).encode())
            http(f"{GRAPH}/{aid}", method="POST",
                 data=urllib.parse.urlencode({
                     "creative": json.dumps({"creative_id": novo["id"]}),
                     "access_token": TOK}).encode())
            print(f"[ok] {nome} -> criativo {novo['id']} com utm")
            st["done"][aid] = nome
            save()
        except Exception as e:
            print(f"[FALHA] {nome}: {str(e)[:200]}")
            st["failed"][aid] = str(e)[:200]
            save()
    print(f"fim. total com utm aplicado: {len(st['done'])} | falhas registradas: {len(st['failed'])}")

if __name__ == "__main__":
    main()
