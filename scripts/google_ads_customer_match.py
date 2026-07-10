#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Customer Match Google Ads (Casa Pellegrini) — sobe a base do Fidelizi (compradores) diariamente
- Cria/atualiza a user list "Fidelizi - Compradores" com email+telefone (SHA-256)
- Replace diario (removeAll + add) via OfflineUserDataJob, com consent GRANTED
- Auto-detecta a versao da Google Ads API (tenta v21..v18)
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
    for ver in ("v21", "v20", "v19", "v18"):
        d, code = gads_call(ver, tok, "/googleAds:searchStream", probe)
        if code == 404:
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
    lista = get_or_create_list(ver, tok)
    d, code = gads_call(ver, tok, "/offlineUserDataJobs:create",
                        {"job": {"type": "CUSTOMER_MATCH_USER_LIST",
                                 "customerMatchUserListMetadata": {
                                     "userList": lista,
                                     "consent": {"adUserData": "GRANTED", "adPersonalization": "GRANTED"}}}})
    if code >= 400:
        raise RuntimeError(f"criar job falhou {code}: {json.dumps(d)[:300]}")
    job = d["resourceName"]
    print(f"[gads] job: {job}")
    ops = [{"removeAll": True}] + users
    for i in range(0, len(ops), 10000):
        d, code = http(f"https://googleads.googleapis.com/{ver}/{job}:addOperations",
                       data=json.dumps({"operations": ops[i:i+10000], "enablePartialFailure": True}).encode(),
                       method="POST",
                       headers={"Authorization": f"Bearer {tok}",
                                "developer-token": os.environ["GADS_DEVELOPER_TOKEN"],
                                "login-customer-id": LOGIN_CUSTOMER,
                                "Content-Type": "application/json"})
        if code >= 400:
            raise RuntimeError(f"addOperations falhou {code}: {json.dumps(d)[:300]}")
    d, code = http(f"https://googleads.googleapis.com/{ver}/{job}:run", data=b"{}", method="POST",
                   headers={"Authorization": f"Bearer {tok}",
                            "developer-token": os.environ["GADS_DEVELOPER_TOKEN"],
                            "login-customer-id": LOGIN_CUSTOMER,
                            "Content-Type": "application/json"})
    if code >= 400:
        raise RuntimeError(f"run falhou {code}: {json.dumps(d)[:300]}")
    print(f"[gads] job rodando — {len(users)} usuarios (replace). Matching leva ate 24h; ver em Publicos-alvo no painel.")

if __name__ == "__main__":
    main()
