#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Resposta automatica de reviews GBP (Casa Pellegrini) — substitui o Zap "Resposta review GBP"
- Le reviews da location via GBP API (v4 reviews) com OAuth proprio (projeto pellegrini-app)
- Responde reviews 4 e 5 estrelas SEM resposta, criadas apos START_DATE
- Delay aleatorio 20-180 min (deterministico por review: hash do id) — anti-cara-de-bot
- Gera resposta com Gemini (prompt herdado do Zap, adaptado pra 4-5 estrelas)
- DRY_RUN=true: so imprime o que responderia (nao publica)
Secrets/env: GBP_CLIENT_ID, GBP_CLIENT_SECRET, GBP_REFRESH_TOKEN, GEMINI_API_KEY, DRY_RUN
"""
import hashlib, json, os, urllib.request, urllib.parse
from datetime import datetime, timezone

ACCOUNT = "101169923071811421747"
LOCATION = "4247745759631447502"
START_DATE = "2026-07-09T00:00:00Z"   # nao responder reviews anteriores ao deploy
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
STATE_FILE = "data/gbp-review-replies.json"
STARS = {"FIVE": 5, "FOUR": 4, "THREE": 3, "TWO": 2, "ONE": 1}

PROMPT = """Você responde reviews de 4 e 5 estrelas da Casa Pellegrini, bar e restaurante no Centro Histórico de Petrópolis (RJ), conhecido pelo hambúrguer artesanal e pelo chopp gelado.

Tom: próximo e descontraído, como uma conversa entre amigos numa mesa do bar. Sem formalidade corporativa.

A resposta deve:
1. Agradecer o avaliador pelo primeiro nome
2. Abordar o feedback específico da review
3. QUANDO fizer sentido natural no contexto, mencione casualmente palavras como "bar", "restaurante", "hambúrguer", "Centro Histórico" ou "Petrópolis" — MAS só se encaixar organicamente. Forçar fica artificial e perde o efeito
4. Se a pessoa mencionou algum prato ou bebida, citá-lo na resposta
5. Se a review for de 4 estrelas e tiver alguma crítica, reconheça com leveza e mostre vontade de melhorar — sem pedido de desculpas exagerado e sem prometer nada específico
6. Manter no máximo 250 caracteres
7. Ser caloroso, genuíno e curto
8. NÃO inclua assinatura — o Google já identifica que é a Casa Pellegrini

Responda APENAS com o texto da resposta, nada mais.

Avaliação:
Classificação: {rating}
Nome do Avaliador: {name}
Comentário: {comment}"""

def http(url, data=None, headers=None, method=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read().decode("utf-8") or "{}")

def gbp_token():
    body = urllib.parse.urlencode({
        "client_id": os.environ["GBP_CLIENT_ID"],
        "client_secret": os.environ["GBP_CLIENT_SECRET"],
        "refresh_token": os.environ["GBP_REFRESH_TOKEN"],
        "grant_type": "refresh_token"}).encode()
    return http("https://oauth2.googleapis.com/token", data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"})["access_token"]

def gemini(prompt):
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}],
                       "generationConfig": {"temperature": 0.9, "maxOutputTokens": 400}}).encode()
    d = http(f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={os.environ['GEMINI_API_KEY']}",
             data=body, headers={"Content-Type": "application/json"})
    return d["candidates"][0]["content"]["parts"][0]["text"].strip().strip('"')

def delay_minutes(review_id):
    h = int(hashlib.sha256(review_id.encode()).hexdigest(), 16)
    return 20 + (h % 161)   # 20 a 180 min, estavel por review

def main():
    st = {"replied": {}}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    tok = gbp_token()
    H = {"Authorization": f"Bearer {tok}"}
    url = f"https://mybusiness.googleapis.com/v4/accounts/{ACCOUNT}/locations/{LOCATION}/reviews?pageSize=50&orderBy=updateTime%20desc"
    reviews = http(url, headers=H).get("reviews", [])
    now = datetime.now(timezone.utc)
    acted = 0
    for rv in reviews:
        rid = rv["reviewId"]
        stars = STARS.get(rv.get("starRating", ""), 0)
        created = rv.get("createTime", "")
        name = (rv.get("reviewer", {}) or {}).get("displayName", "Cliente")
        comment = (rv.get("comment") or "").split("(Translated by Google)")[0].strip()
        if rv.get("reviewReply"):            continue   # ja respondida (por nos, pelo Zap ou manual)
        if stars < 4:                        continue   # so 4 e 5 estrelas
        if created < START_DATE:             continue   # nada retroativo
        age_min = (now - datetime.fromisoformat(created.replace("Z", "+00:00"))).total_seconds() / 60
        wait = delay_minutes(rid)
        if age_min < wait:
            print(f"[aguardando] {name} ({stars} estrelas): responde em ~{int(wait - age_min)} min")
            continue
        reply = gemini(PROMPT.format(rating=stars, name=name, comment=comment or "(sem comentario, so a nota)"))
        if len(reply) > 350:
            reply = gemini("Encurte para no maximo 250 caracteres, mantendo o tom: " + reply)
        if DRY_RUN:
            print(f"[DRY RUN] {name} ({stars} estrelas) '{comment[:80]}'\n  RESPOSTA: {reply}\n")
        else:
            http(f"https://mybusiness.googleapis.com/v4/accounts/{ACCOUNT}/locations/{LOCATION}/reviews/{rid}/reply",
                 data=json.dumps({"comment": reply}).encode(), method="PUT",
                 headers={**H, "Content-Type": "application/json"})
            print(f"[respondida] {name} ({stars} estrelas) -> {reply}")
            st["replied"][rid] = {"at": now.isoformat(), "stars": stars, "reply": reply}
            os.makedirs("data", exist_ok=True)
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(st, f, ensure_ascii=False, indent=1)
        acted += 1
    print(f"Fim: {len(reviews)} reviews vistas, {acted} tratadas nesta rodada.")

if __name__ == "__main__":
    main()
