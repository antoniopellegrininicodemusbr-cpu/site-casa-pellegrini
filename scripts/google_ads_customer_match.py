#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Customer Match Google Ads (Casa Pellegrini) — sobe a base do Fidelizi (compradores) diariamente
- Cria/atualiza a user list "Fidelizi - Compradores" com email+telefone (SHA-256)
- Replace diario (removeAll + add) via OfflineUserDataJob, com consent GRANTED
- Auto-detecta a versao da Google Ads API (tenta v25..v22; pula versoes desativadas)
Secrets/env: GADS_CLIENT_ID, GADS_CLIENT_SECRET, GADS_REFRESH_TOKEN, GADS_DEVELOPER_TOKEN,
             FIDELIZI_APP_TOKEN, FIDELIZI_ACCESS_TOKEN, DRY_RUN
"""
import hashlib, json, os, urllib.request, urllib.parse

CUSTOMER = "5387211607"          # conta direta Casa Pellegrini
LOGIN_CUSTOMER = "8487848841"    # MCC
LIST_NAME = "Fidelizi - Compradores"
FIDELIZI_BASE = "https://integracao.fidelizii.com.br/api/v4"
SHOP_ID = "4477"
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

def http(url, data=None, headers=None, method=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode("utf-8") or "{}"), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode("utf-8", "ignore") or "{}"), e.code

def sha(s):
    s = (s or "").strip().lower()
    return hashlib.sha256(s.encode()).hexdigest() if s else ""

def phone_e164(p):
    d = "".join(ch for ch in (p or "") if ch.isdigit())
    if not d: return ""
    if not d.startswith("55"): d = "55" + d
    return "+" + d

def fidelizi_clients():
    app_t, acc_t = os.environ["FIDELIZI_APP_TOKEN"], os.environ["FIDELIZI_ACCESS_TOKEN"]
    out, page = [], 1
    while True:
        q = urllib.parse.urlencode({"itens_por_pagina": 200, "ordenado_por": "ultima_compra", "page": page})
        d, code = http(f"{FIDELIZI_BASE}/estabelecimentos/{SHOP_ID}/clientes?{q}",
                       headers={"app-token": app_t, "access-token": acc_t, "Accept": "application/json"})
        if code >= 400 or not d.get("success"):
            raise RuntimeError(f"Fidelizi {code}: {json.dumps(d)[:200]}")
        chunk = d.get("data", {}).get("data", [])
        if not chunk: break
        out += chunk
        if page >= d.get("data", {}).get("last_page", 1): break
        page += 1
    return out

def gads_token():
    body = urllib.parse.urlencode({
        "client_id": os.environ["GADS_CLIENT_ID"],
        "client_secret": os.environ["GADS_CLIENT_SECRET"],
        "refresh_token": os.environ["GADS_REFRESH_TOKEN"],
        "grant_type": "refresh_token"}).encode()
    d, code = http("https://oauth2.googleapis.com/token", data=body,
                   headers={"Content-Type": "application/x-www-form-urlencoded"})
    if code >= 400:
        raise RuntimeError(f"OAuth {code}: {json.dumps(d)[:200]}")
    return d["access_token"]

LOGIN_HEADER = {"value": LOGIN_CUSTOMER}   # mutavel: auto-detectado

def _headers(tok):
    h = {"Authorization": f"Bearer {tok}",
         "developer-token": os.environ["GADS_DEVELOPER_TOKEN"],
         "Content-Type": "application/json"}
    if LOGIN_HEADER["value"]:
        h["login-customer-id"] = LOGIN_HEADER["value"]
    return h

def gads_call(ver, tok, path, payload):
    return http(f"https://googleads.googleapis.com/{ver}/customers/{CUSTOMER}{path}",
                data=json.dumps(payload).encode(), method="POST", headers=_headers(tok))

def detect_version(tok):
    """Descobre versao valida E a combinacao certa de login-customer-id."""
    probe = {"query": "SELECT customer.id FROM customer LIMIT 1"}
    ver_ok = None
    for ver in ("v25", "v24", "v23", "v22"):
        d, code = gads_call(ver, tok, "/googleAds:searchStream", probe)
        if code == 404 or "UNSUPPORTED_VERSION" in json.dumps(d):
            print(f"[gads] versao {ver} indisponivel/desativada; tentando a proxima")
            continue
        ver_ok = ver
        if code < 400:
            print(f"[gads] versao {ver} + login-customer-id {LOGIN_HEADER['value'] or '(nenhum)'} OK")
            return ver
        break
    if not ver_ok:
        raise RuntimeError("nenhuma versao da Google Ads API respondeu")
    # 403: tenta descobrir a combinacao certa
    print(f"[gads] {ver_ok} deu {code} com login-customer-id={LOGIN_HEADER['value']}. Autodiagnostico...")
    d, c2 = http(f"https://googleads.googleapis.com/{ver_ok}/customers:listAccessibleCustomers",
                 headers={"Authorization": f"Bearer {tok}",
                          "developer-token": os.environ["GADS_DEVELOPER_TOKEN"]})
    acessiveis = [r.split("/")[-1] for r in d.get("resourceNames", [])] if c2 < 400 else []
    print(f"[gads] contas acessiveis pelo login: {acessiveis}")
    candidatos = [None] + acessiveis
    for cand in candidatos:
        LOGIN_HEADER["value"] = cand or ""
        d, code = gads_call(ver_ok, tok, "/googleAds:searchStream", probe)
        if code < 400:
            print(f"[gads] combinacao OK: login-customer-id={cand or '(nenhum)'}")
            return ver_ok
        print(f"[gads] login-customer-id={cand or '(nenhum)'} -> {code}")
    raise RuntimeError(f"nenhuma combinacao funcionou. Acessiveis: {acessiveis}. "
                       f"Confirmar se a conta Google autorizada tem acesso ao customer {CUSTOMER}.")

def get_or_create_list(ver, tok):
    d, code = gads_call(ver, tok, "/googleAds:searchStream",
                        {"query": f"SELECT user_list.resource_name, user_list.name, user_list.size_for_search "
                                  f"FROM user_list WHERE user_list.name = '{LIST_NAME}'"})
    if code == 200:
        for chunk in (d if isinstance(d, list) else [d]):
            for row in chunk.get("results", []):
                rn = row["userList"]["resourceName"]
                print(f"[gads] user list existente: {rn} (size busca: {row['userList'].get('sizeForSearch','?')})")
                return rn
    payload = {"operations": [{"create": {
        "name": LIST_NAME,
        "description": "Clientes com compras no Fidelizi. Sincronizado diariamente via GitHub Action.",
        "membershipLifeSpan": "540",
        "crmBasedUserList": {"uploadKeyType": "CONTACT_INFO"}}}]}
    d, code = gads_call(ver, tok, "/userLists:mutate", payload)
    if code >= 400:
        raise RuntimeError(f"criar user list falhou {code}: {json.dumps(d)[:300]}")
    rn = d["results"][0]["resourceName"]
    print(f"[gads] user list CRIADA: {rn}")
    return rn

SEASONAL_NEG_CAMPAIGN = "23747885498"
SEASONAL_NEG_TERMS = ("bauernfest", "bauerfest", "restaurante alemão")

def manage_seasonal_negatives(ver, tok):
    """Bauernfest: negativas ativas o ano todo, REMOVIDAS de 01/06 a 11/07 (janela do festival). Decisao Antonio 10/08/2026."""
    from datetime import date
    t = date.today()
    in_window = (t.month == 6) or (t.month == 7 and t.day <= 11)
    q = {"query": "SELECT campaign_criterion.criterion_id, campaign_criterion.keyword.text FROM campaign_criterion WHERE campaign_criterion.negative = TRUE AND campaign_criterion.type = 'KEYWORD' AND campaign.id = " + SEASONAL_NEG_CAMPAIGN}
    d, code = gads_call(ver, tok, "/googleAds:searchStream", q)
    rows = []
    if isinstance(d, list):
        for chunk in d:
            rows += chunk.get("results", [])
    elif isinstance(d, dict):
        rows = d.get("results", [])
    wanted = [s.lower() for s in SEASONAL_NEG_TERMS]
    present = {}
    for r in rows:
        cc = r.get("campaignCriterion", {})
        kw = ((cc.get("keyword", {}) or {}).get("text", "") or "").lower()
        if kw in wanted:
            present[kw] = cc.get("criterionId")
    ops = []
    if in_window:
        for kw, cid in present.items():
            ops.append({"remove": "customers/" + CUSTOMER + "/campaignCriteria/" + SEASONAL_NEG_CAMPAIGN + "~" + str(cid)})
        action = "REMOVIDAS (janela Bauernfest 01/06-11/07)"
    else:
        for s in SEASONAL_NEG_TERMS:
            if s.lower() not in present:
                ops.append({"create": {"campaign": "customers/" + CUSTOMER + "/campaigns/" + SEASONAL_NEG_CAMPAIGN, "negative": True, "keyword": {"text": s, "matchType": "PHRASE"}}})
        action = "REPOSTAS (fora da janela)"
    if ops:
        d2, c2 = gads_call(ver, tok, "/campaignCriteria:mutate", {"operations": ops})
        print("[seasonal-negatives] " + action + ": " + str(len(ops)) + " ops, code " + str(c2))
    else:
        print("[seasonal-negatives] estado correto (janela=" + str(in_window) + ")")


def main():
    clients = fidelizi_clients()
    users = []
    for c in clients:
        if (c.get("compras", 0) or 0) <= 0: continue
        ids = []
        em = sha(c.get("email"))
        ph = sha(phone_e164(c.get("celular") or c.get("telefone")))
        if em: ids.append({"hashedEmail": em})
        if ph: ids.append({"hashedPhoneNumber": ph})
        if ids: users.append({"create": {"userIdentifiers": ids}})
    print(f"[fidelizi] {len(clients)} clientes | {len(users)} compradores com email/telefone")
    if DRY_RUN:
        print(f"[DRY] subiria {len(users)} usuarios pra lista '{LIST_NAME}' (replace diario)")
        return
    tok = gads_token()
    ver = detect_version(tok)
    try:
        manage_seasonal_negatives(ver, tok)
    except Exception as e:
        print("[seasonal-negatives] erro:", e)
    lista = get_or_create_list(ver, tok)
    users_dm = []
    for u in users:
        ids = []
        for ident in u["create"]["userIdentifiers"]:
            if "hashedEmail" in ident:
                ids.append({"emailAddress": ident["hashedEmail"]})
            if "hashedPhoneNumber" in ident:
                ids.append({"phoneNumber": ident["hashedPhoneNumber"]})
        if ids:
            users_dm.append({"userData": {"userIdentifiers": ids}})
    list_id = lista.split("/")[-1]
    dest = {"operatingAccount": {"accountType": "GOOGLE_ADS", "accountId": CUSTOMER}, "productDestinationId": list_id}
    d0, code0 = http("https://datamanager.googleapis.com/v1/audienceMembers:removeAll",
                     data=json.dumps({"destinations": [dest]}).encode(), method="POST",
                     headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
    if code0 >= 400:
        print("[datamanager] removeAll falhou (segue sem replace):", json.dumps(d0)[:200])
    else:
        print("[datamanager] removeAll ok")
    total = 0
    for i in range(0, len(users_dm), 10000):
        payload = {"destinations": [dest],
                   "audienceMembers": users_dm[i:i+10000],
                   "consent": {"adUserData": "CONSENT_GRANTED", "adPersonalization": "CONSENT_GRANTED"},
                   "encoding": "HEX",
                   "termsOfService": {"customerMatchTermsOfServiceStatus": "ACCEPTED"}}
        d, code = http("https://datamanager.googleapis.com/v1/audienceMembers:ingest",
                       data=json.dumps(payload).encode(), method="POST",
                       headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
        if code >= 400:
            raise RuntimeError("datamanager ingest falhou: " + json.dumps(d)[:300])
        total += len(users_dm[i:i+10000])
        print("[datamanager] lote ok, requestId:", d.get("requestId"))
    print("[datamanager] enviados " + str(total) + " membros pra lista " + list_id + " (matching aparece em 24-48h)")


if __name__ == "__main__":
    main()
