#!/usr/bin/env python3
"""Sync Fidelizi -> Sheets + Meta CAPI + TikTok Events + Meta/TikTok Custom Audiences."""

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

FIDELIZI_BASE = "https://integracao.fidelizii.com.br/api/v4"
SHEET_NAME = "Página1"
HEADER_RANGE = f"{SHEET_NAME}!A1:P1"
DATA_RANGE_CLEAR = f"{SHEET_NAME}!A2:P"
DATA_RANGE_WRITE = f"{SHEET_NAME}!A2"
HEADERS = [
    "id_cliente", "nome", "email", "celular",
    "data_nascimento", "data_cadastro",
    "receita", "compras",
    "primeira_compra", "ultima_compra",
    "dias_inativo", "premios_pendentes",
    "saldo_pontos", "ultima_sincronizacao",
    "email_sha256", "phone_sha256",
]

META_GRAPH_VERSION = "v21.0"
TIKTOK_EVENTS_ENDPOINT = "https://business-api.tiktok.com/open_api/v1.3/event/track/"
TIKTOK_API_BASE = "https://business-api.tiktok.com/open_api/v1.3"

# Nomes canonicos das audiences na Meta
META_AUDIENCE_NAME = "Fidelizi - Compradores"
META_LOOKALIKE_RATIOS = [0.01, 0.03, 0.05]

# Nomes canonicos das audiences no TikTok
TIKTOK_AUDIENCE_NAME = "Fidelizi - Compradores (Email)"
# TikTok Lookalike: EXTENSIVE = ratio mais largo (~10%), BALANCE = medio (~3%), PRECISE = mais qualificado (~1%)
TIKTOK_LOOKALIKE_TYPES = [("PRECISE", "1pct"), ("BALANCE", "3pct"), ("EXTENSIVE", "5pct")]


def parse_dt(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def to_unix_ts(dt_str, fallback_now):
    dt = parse_dt(dt_str)
    if dt is None:
        return int(fallback_now.replace(tzinfo=timezone.utc).timestamp())
    dt_utc = dt + timedelta(hours=3)
    return int(dt_utc.replace(tzinfo=timezone.utc).timestamp())


def normalize_phone_e164(phone):
    if not phone:
        return ""
    digits = re.sub(r"\D", "", phone)
    if not digits:
        return ""
    return "+" + digits


def hash_sha256(s):
    if not s:
        return ""
    return hashlib.sha256(s.strip().lower().encode("utf-8")).hexdigest()


def sum_premios_pendentes(c):
    return sum(
        c.get(f"pendente_resgate_{k}", 0) or 0
        for k in ("premio_fidelidade", "brinde_roleta", "premio_surpresa",
                  "premio_campanha", "premio_game")
    )


def fetch_fidelizi_clients(app_token, access_token, shop_id):
    url = f"{FIDELIZI_BASE}/estabelecimentos/{shop_id}/clientes"
    headers = {
        "app-token": app_token,
        "access-token": access_token,
        "Accept": "application/json",
    }
    params = {"itens_por_pagina": 200, "ordenado_por": "ultima_compra"}
    out = []
    page = 1
    while True:
        params["page"] = page
        r = requests.get(url, headers=headers, params=params, timeout=30)
        r.raise_for_status()
        body = r.json()
        if not body.get("success"):
            raise RuntimeError(f"Fidelizi retornou success=false: {body}")
        chunk = body.get("data", {}).get("data", [])
        if not chunk:
            break
        out.extend(chunk)
        last_page = body.get("data", {}).get("last_page", 1)
        if page >= last_page:
            break
        page += 1
    return out


def read_current_state(sheets, spreadsheet_id):
    result = sheets.values().get(
        spreadsheetId=spreadsheet_id,
        range=DATA_RANGE_CLEAR,
        valueRenderOption="UNFORMATTED_VALUE",
    ).execute()
    rows = result.get("values", [])
    state = {}
    for row in rows:
        if not row or not row[0]:
            continue
        row = row + [""] * (16 - len(row))
        id_cliente = str(row[0])
        try:
            compras = int(row[7]) if row[7] not in ("", None) else 0
        except (ValueError, TypeError):
            compras = 0
        try:
            receita = float(row[6]) if row[6] not in ("", None) else 0.0
        except (ValueError, TypeError):
            receita = 0.0
        state[id_cliente] = {
            "compras": compras,
            "receita": receita,
            "ultima_compra": row[9],
            "email_sha256": row[14],
            "phone_sha256": row[15],
        }
    return state


def ensure_headers(sheets, spreadsheet_id):
    sheets.values().update(
        spreadsheetId=spreadsheet_id,
        range=HEADER_RANGE,
        valueInputOption="RAW",
        body={"values": [HEADERS]},
    ).execute()


def process_client(c, now):
    uc = parse_dt(c.get("ultima_compra"))
    dias_inativo = (now - uc).days if uc else ""
    pendentes = sum_premios_pendentes(c)
    carteira = c.get("carteira") or {}
    saldo = carteira.get("saldo", 0)
    email = (c.get("email") or "").strip().lower()
    phone_e164 = normalize_phone_e164(c.get("celular") or "")
    return [
        c.get("id_cliente", ""),
        c.get("nome") or "",
        c.get("email") or "",
        c.get("celular") or "",
        c.get("data_nascimento") or "",
        c.get("data_cadastro") or "",
        c.get("receita", 0) or 0,
        c.get("compras", 0) or 0,
        c.get("primeira_compra") or "",
        c.get("ultima_compra") or "",
        dias_inativo,
        pendentes,
        saldo,
        now.strftime("%Y-%m-%d %H:%M:%S"),
        hash_sha256(email),
        hash_sha256(phone_e164),
    ]


TEST_NAME_PATTERNS = ("testador", "teste fidelizi", "fidelizi teste")


def is_test_client(c):
    nome = (c.get("nome") or "").lower()
    return any(p in nome for p in TEST_NAME_PATTERNS)


def detect_deltas(previous_state, clients):
    novos = []
    compras_novas = []
    for c in clients:
        if is_test_client(c):
            continue
        id_cliente = str(c.get("id_cliente"))
        prev = previous_state.get(id_cliente)
        if prev is None:
            novos.append({
                "id_cliente": id_cliente,
                "nome": c.get("nome"),
                "email": c.get("email"),
                "celular": c.get("celular"),
                "compras": c.get("compras", 0) or 0,
                "receita": float(c.get("receita", 0) or 0),
                "ultima_compra": c.get("ultima_compra"),
                "primeira_compra": c.get("primeira_compra"),
            })
            continue
        compras_old = prev["compras"]
        compras_new = c.get("compras", 0) or 0
        receita_old = prev["receita"]
        receita_new = float(c.get("receita", 0) or 0)
        if compras_new > compras_old or receita_new > receita_old + 0.01:
            compras_novas.append({
                "id_cliente": id_cliente,
                "nome": c.get("nome"),
                "email": c.get("email"),
                "celular": c.get("celular"),
                "delta_compras": compras_new - compras_old,
                "delta_receita": round(receita_new - receita_old, 2),
                "compras_total": compras_new,
                "receita_total": round(receita_new, 2),
                "ultima_compra": c.get("ultima_compra"),
            })
    return {"novos_clientes": novos, "compras_novas": compras_novas}


def build_purchase_events(novos, compras_novas, now):
    events = []
    for n in novos:
        if (n.get("receita") or 0) <= 0:
            continue
        email = (n.get("email") or "").strip().lower()
        phone_e164 = normalize_phone_e164(n.get("celular") or "")
        ts = to_unix_ts(n.get("ultima_compra") or n.get("primeira_compra"), now)
        events.append({
            "event_id": f"fidelizi-novo-{n['id_cliente']}-{int(n['receita']*100)}",
            "event_time": ts,
            "value": float(n["receita"]),
            "email_sha256": hash_sha256(email),
            "phone_sha256": hash_sha256(phone_e164),
            "id_cliente": n["id_cliente"],
            "nome": n.get("nome"),
            "tipo": "primeira_compra",
        })
    for e in compras_novas:
        if (e.get("delta_receita") or 0) <= 0:
            continue
        email = (e.get("email") or "").strip().lower()
        phone_e164 = normalize_phone_e164(e.get("celular") or "")
        ts = to_unix_ts(e.get("ultima_compra"), now)
        events.append({
            "event_id": f"fidelizi-delta-{e['id_cliente']}-{e['ultima_compra']}-{int(e['receita_total']*100)}",
            "event_time": ts,
            "value": float(e["delta_receita"]),
            "email_sha256": hash_sha256(email),
            "phone_sha256": hash_sha256(phone_e164),
            "id_cliente": e["id_cliente"],
            "nome": e.get("nome"),
            "tipo": "recompra",
        })
    return events


def send_to_meta_capi(events):
    token = os.environ.get("META_CAPI_TOKEN", "").strip()
    dataset_id = os.environ.get("META_DATASET_ID", "").strip()
    if not token or not dataset_id:
        print("[meta-capi] tokens nao configurados - pulando")
        return None
    if not events:
        print("[meta-capi] nenhum evento")
        return None
    url = f"https://graph.facebook.com/{META_GRAPH_VERSION}/{dataset_id}/events"
    data = []
    for e in events:
        user_data = {}
        if e["email_sha256"]:
            user_data["em"] = [e["email_sha256"]]
        if e["phone_sha256"]:
            user_data["ph"] = [e["phone_sha256"]]
        data.append({
            "event_name": "Purchase",
            "event_time": e["event_time"],
            "event_id": e["event_id"],
            "action_source": "physical_store",
            "user_data": user_data,
            "custom_data": {"currency": "BRL", "value": e["value"]},
        })
    payload = {"data": data, "access_token": token}
    r = requests.post(url, json=payload, timeout=30)
    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text}
    print(f"[meta-capi] POST -> {r.status_code}")
    print(f"[meta-capi] response: {json.dumps(body, ensure_ascii=False)[:500]}")
    return body


def send_to_tiktok_events(events):
    token = os.environ.get("TIKTOK_EVENTS_TOKEN", "").strip()
    event_set_id = os.environ.get("TIKTOK_EVENT_SET_ID", "").strip()
    if not token or not event_set_id:
        print("[tiktok-events] tokens nao configurados - pulando")
        return None
    if not events:
        print("[tiktok-events] nenhum evento")
        return None
    data = []
    for e in events:
        user = {}
        if e["email_sha256"]:
            user["email"] = e["email_sha256"]
        if e["phone_sha256"]:
            user["phone"] = e["phone_sha256"]
        data.append({
            "event": "Purchase",
            "event_time": e["event_time"],
            "event_id": e["event_id"],
            "user": user,
            "properties": {"currency": "BRL", "value": e["value"]},
        })
    payload = {
        "event_source": "offline",
        "event_source_id": event_set_id,
        "data": data,
    }
    headers = {"Access-Token": token, "Content-Type": "application/json"}
    r = requests.post(TIKTOK_EVENTS_ENDPOINT, headers=headers, json=payload, timeout=30)
    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text}
    print(f"[tiktok-events] POST -> {r.status_code}")
    print(f"[tiktok-events] response: {json.dumps(body, ensure_ascii=False)[:500]}")
    return body


# ---------- Fase 2A: Meta Custom Audience + Lookalikes -----------------------

def get_compradores_for_audience(clients):
    users = []
    for c in clients:
        if is_test_client(c):
            continue
        if (c.get("compras", 0) or 0) <= 0:
            continue
        email = (c.get("email") or "").strip().lower()
        phone_e164 = normalize_phone_e164(c.get("celular") or "")
        em = hash_sha256(email)
        ph = hash_sha256(phone_e164)
        if em or ph:
            users.append([em, ph])
    return users


def meta_list_audiences(token, ad_account):
    audiences = []
    url = (f"https://graph.facebook.com/{META_GRAPH_VERSION}/{ad_account}/customaudiences"
           f"?fields=id,name,subtype,approximate_count_lower_bound&limit=200&access_token={token}")
    while url:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        body = r.json()
        audiences.extend(body.get("data", []))
        url = body.get("paging", {}).get("next")
    return audiences


def meta_get_or_create_custom_audience(token, ad_account, name, description):
    existing = meta_list_audiences(token, ad_account)
    for a in existing:
        if a.get("name") == name and a.get("subtype") == "CUSTOM":
            return a["id"]
    create_url = f"https://graph.facebook.com/{META_GRAPH_VERSION}/{ad_account}/customaudiences"
    body = {
        "name": name,
        "description": description,
        "subtype": "CUSTOM",
        "customer_file_source": "USER_PROVIDED_ONLY",
        "access_token": token,
    }
    r = requests.post(create_url, data=body, timeout=30)
    resp = r.json()
    print(f"[meta-audience] CREATE {name} -> {r.status_code} {json.dumps(resp)[:300]}")
    if r.status_code >= 400:
        return None
    return resp.get("id")


def meta_replace_audience_users(token, audience_id, users):
    if not users:
        print("[meta-audience] sem usuarios pra enviar")
        return None
    url = f"https://graph.facebook.com/{META_GRAPH_VERSION}/{audience_id}/users"
    payload_inner = {
        "schema": ["EMAIL_SHA256", "PHONE_SHA256"],
        "data": users,
    }
    body = {
        "payload": json.dumps(payload_inner),
        "access_token": token,
    }
    r = requests.post(url, data=body, timeout=60)
    try:
        resp = r.json()
    except Exception:
        resp = {"raw": r.text}
    print(f"[meta-audience] REPLACE users ({len(users)}) -> {r.status_code} {json.dumps(resp, ensure_ascii=False)[:300]}")
    return resp


def meta_get_or_create_lookalike(token, ad_account, source_id, ratio, name):
    existing = meta_list_audiences(token, ad_account)
    for a in existing:
        if a.get("name") == name and a.get("subtype") == "LOOKALIKE":
            return a["id"]
    create_url = f"https://graph.facebook.com/{META_GRAPH_VERSION}/{ad_account}/customaudiences"
    lookalike_spec = {
        "origin_audience_id": source_id,
        "ratio": ratio,
        "country": "BR",
    }
    body = {
        "name": name,
        "subtype": "LOOKALIKE",
        "lookalike_spec": json.dumps(lookalike_spec),
        "origin_audience_id": source_id,
        "access_token": token,
    }
    r = requests.post(create_url, data=body, timeout=30)
    try:
        resp = r.json()
    except Exception:
        resp = {"raw": r.text}
    print(f"[meta-lookalike] CREATE {name} (ratio={ratio}) -> {r.status_code} {json.dumps(resp, ensure_ascii=False)[:300]}")
    if r.status_code >= 400:
        return None
    return resp.get("id")


def sync_meta_audiences(clients):
    token = os.environ.get("META_USER_TOKEN", "").strip()
    ad_account = os.environ.get("META_AD_ACCOUNT_ID", "").strip()
    if not token or not ad_account:
        print("[meta-audience] tokens nao configurados - pulando Fase 2A")
        return
    users = get_compradores_for_audience(clients)
    print(f"[meta-audience] {len(users)} compradores filtrados (compras>0)")
    if len(users) < 100:
        print(f"[meta-audience] AVISO: menos de 100 compradores - Lookalike pode falhar")
    desc = "Casa Pellegrini - clientes que ja compraram (compras>0), sincronizado diariamente do Fidelizi"
    audience_id = meta_get_or_create_custom_audience(token, ad_account, META_AUDIENCE_NAME, desc)
    if not audience_id:
        print("[meta-audience] falha ao criar/obter Custom Audience - pulando Lookalikes")
        return
    print(f"[meta-audience] Custom Audience id={audience_id}")
    meta_replace_audience_users(token, audience_id, users)
    for ratio in META_LOOKALIKE_RATIOS:
        ratio_pct = int(ratio * 100)
        lal_name = f"Fidelizi - Compradores - Lookalike {ratio_pct}% BR"
        meta_get_or_create_lookalike(token, ad_account, audience_id, ratio, lal_name)


# ---------- Fase 2B: TikTok Custom Audience + Lookalikes ---------------------

def get_compradores_emails_sha256(clients):
    """Lista de email_sha256 dos compradores (compras>0). Pra upload no TikTok."""
    emails = []
    for c in clients:
        if is_test_client(c):
            continue
        if (c.get("compras", 0) or 0) <= 0:
            continue
        email = (c.get("email") or "").strip().lower()
        em = hash_sha256(email)
        if em:
            emails.append(em)
    return emails


def tiktok_upload_audience_file(token, advertiser_id, csv_content, calculate_type="EMAIL_SHA256"):
    """Upload de arquivo CSV via multipart. Retorna file_path."""
    url = f"{TIKTOK_API_BASE}/dmp/custom_audience/file/upload/"
    file_signature = hashlib.md5(csv_content.encode()).hexdigest()
    files = {
        "file": ("fidelizi_compradores.csv", csv_content, "text/csv"),
    }
    data = {
        "advertiser_id": advertiser_id,
        "calculate_type": calculate_type,
        "file_name": "fidelizi_compradores.csv",
        "file_signature": file_signature,
    }
    headers = {"Access-Token": token}
    r = requests.post(url, headers=headers, data=data, files=files, timeout=60)
    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text}
    print(f"[tiktok-audience] FILE UPLOAD -> {r.status_code} code={body.get('code')} msg={body.get('message')}")
    if body.get("code") != 0:
        return None
    return body.get("data", {}).get("file_path")


def tiktok_list_audiences(token, advertiser_id):
    """Lista todas Custom Audiences/Lookalikes da conta."""
    audiences = []
    page = 1
    while True:
        r = requests.get(
            f"{TIKTOK_API_BASE}/dmp/custom_audience/list/",
            headers={"Access-Token": token},
            params={"advertiser_id": advertiser_id, "page": page, "page_size": 100},
            timeout=30,
        )
        try:
            body = r.json()
        except Exception:
            return audiences
        if body.get("code") != 0:
            print(f"[tiktok-audience] LIST erro: {body.get('message')}")
            return audiences
        items = body.get("data", {}).get("list", [])
        audiences.extend(items)
        page_info = body.get("data", {}).get("page_info", {})
        if page >= page_info.get("total_page", 1):
            break
        page += 1
    return audiences


def tiktok_get_or_create_audience(token, advertiser_id, name, file_path, calculate_type="EMAIL_SHA256"):
    """Cria nova Custom Audience ou retorna id existente."""
    existing = tiktok_list_audiences(token, advertiser_id)
    for a in existing:
        if a.get("custom_audience_name") == name:
            return a.get("custom_audience_id")
    url = f"{TIKTOK_API_BASE}/dmp/custom_audience/create/"
    body = {
        "advertiser_id": advertiser_id,
        "custom_audience_name": name,
        "file_paths": [file_path],
        "calculate_type": calculate_type,
    }
    headers = {"Access-Token": token, "Content-Type": "application/json"}
    r = requests.post(url, headers=headers, json=body, timeout=30)
    try:
        resp = r.json()
    except Exception:
        resp = {"raw": r.text}
    print(f"[tiktok-audience] CREATE {name} -> {r.status_code} code={resp.get('code')} msg={resp.get('message')}")
    if resp.get("code") != 0:
        return None
    return resp.get("data", {}).get("custom_audience_id")


def tiktok_update_audience(token, advertiser_id, custom_audience_id, file_path):
    """Substitui arquivo de uma audience existente (sincroniza com base atual)."""
    url = f"{TIKTOK_API_BASE}/dmp/custom_audience/update/"
    body = {
        "advertiser_id": advertiser_id,
        "custom_audience_id": custom_audience_id,
        "file_paths": [file_path],
    }
    headers = {"Access-Token": token, "Content-Type": "application/json"}
    r = requests.post(url, headers=headers, json=body, timeout=30)
    try:
        resp = r.json()
    except Exception:
        resp = {"raw": r.text}
    print(f"[tiktok-audience] UPDATE {custom_audience_id} -> {r.status_code} code={resp.get('code')} msg={resp.get('message')}")
    return resp


def tiktok_get_or_create_lookalike(token, advertiser_id, source_id, expand_type, name, country="BR"):
    """Cria Lookalike TikTok. expand_type: PRECISE (mais qualificado), BALANCE (medio), EXTENSIVE (mais alcance)."""
    existing = tiktok_list_audiences(token, advertiser_id)
    for a in existing:
        if a.get("custom_audience_name") == name:
            return a.get("custom_audience_id")
    url = f"{TIKTOK_API_BASE}/dmp/custom_audience/lookalike/create/"
    body = {
        "advertiser_id": advertiser_id,
        "custom_audience_name": name,
        "lookalike_spec": {
            "source_audience_id": source_id,
            "country": country,
            "expand_type": expand_type,
            "include_source": False,
            "mobile_os": "ALL",
            "placements": ["TikTok"],
            "location_ids": ["6252001"],
            "audience_size": {"PRECISE": "NARROW", "BALANCE": "BALANCED", "EXTENSIVE": "BROAD"}.get(expand_type, "BALANCED"),
        },
    }
    headers = {"Access-Token": token, "Content-Type": "application/json"}
    r = requests.post(url, headers=headers, json=body, timeout=30)
    try:
        resp = r.json()
    except Exception:
        resp = {"raw": r.text}
    print(f"[tiktok-lookalike] CREATE {name} ({expand_type}) -> {r.status_code} code={resp.get('code')} msg={resp.get('message')}")
    if resp.get("code") != 0:
        return None
    return resp.get("data", {}).get("audience_id")


def sync_tiktok_audiences(clients):
    """Sincroniza Custom Audience + Lookalikes no TikTok via Marketing API."""
    token = os.environ.get("TIKTOK_BUSINESS_TOKEN", "").strip()
    advertiser_id = os.environ.get("TIKTOK_ADVERTISER_ID", "").strip()
    if not token or not advertiser_id:
        print("[tiktok-audience] TIKTOK_BUSINESS_TOKEN/TIKTOK_ADVERTISER_ID nao configurados - pulando Fase 2B")
        return
    emails = get_compradores_emails_sha256(clients)
    print(f"[tiktok-audience] {len(emails)} emails de compradores filtrados")
    if len(emails) < 1000:
        print(f"[tiktok-audience] AVISO: TikTok exige minimo 1000 source users pra Lookalike (atual: {len(emails)}). Lookalike sera pulado.")
    # Monta CSV (1 coluna, 1 email_sha256 por linha)
    csv_content = "\n".join(emails) + "\n"
    file_path = tiktok_upload_audience_file(token, advertiser_id, csv_content, "EMAIL_SHA256")
    if not file_path:
        print("[tiktok-audience] falha no upload do arquivo - pulando")
        return
    print(f"[tiktok-audience] file uploaded, file_path={file_path}")
    audience_id = tiktok_get_or_create_audience(
        token, advertiser_id, TIKTOK_AUDIENCE_NAME, file_path, "EMAIL_SHA256"
    )
    if not audience_id:
        print("[tiktok-audience] falha ao criar/obter Custom Audience - pulando Lookalikes")
        return
    print(f"[tiktok-audience] Custom Audience id={audience_id}")
    # Atualiza arquivo da audience (idempotente em re-runs - mantem base sincronizada)
    tiktok_update_audience(token, advertiser_id, audience_id, file_path)
    # Lookalikes - so se source >= 1000 (limite TikTok)
    if len(emails) < 1000:
        print(f"[tiktok-lookalike] SKIP: source size {len(emails)} < 1000 (limite TikTok). Refazer quando base crescer.")
    else:
        for expand_type, label in TIKTOK_LOOKALIKE_TYPES:
            lal_name = f"Fidelizi - Compradores - Lookalike {label} BR"
            tiktok_get_or_create_lookalike(token, advertiser_id, audience_id, expand_type, lal_name)


# ---------- Main ------------------------------------------------------------

def main():
    app_token = os.environ["FIDELIZI_APP_TOKEN"]
    access_token = os.environ["FIDELIZI_ACCESS_TOKEN"]
    shop_id = os.environ.get("FIDELIZI_SHOP_ID", "4477")
    spreadsheet_id = os.environ["SPREADSHEET_ID"]
    creds_json = os.environ["GOOGLE_SHEETS_CREDS"]

    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False).spreadsheets()

    ensure_headers(sheets, spreadsheet_id)
    print(f"[sheets] headers garantidos em {HEADER_RANGE}")

    previous_state = read_current_state(sheets, spreadsheet_id)
    print(f"[sheets] estado anterior: {len(previous_state)} clientes")

    clients = fetch_fidelizi_clients(app_token, access_token, shop_id)
    print(f"[fidelizi] {len(clients)} clientes encontrados")

    if not clients:
        print("[abort] nenhum cliente retornado")
        return 0

    deltas = detect_deltas(previous_state, clients)
    novos = deltas["novos_clientes"]
    compras_novas = deltas["compras_novas"]
    print(f"[delta] {len(novos)} clientes NOVOS")
    for n in novos[:10]:
        print(f"  [novo] {n['id_cliente']} {n['nome']!r} R$ {n['receita']}")
    if len(novos) > 10:
        print(f"  ... e mais {len(novos) - 10}")
    print(f"[delta] {len(compras_novas)} COMPRAS NOVAS")
    for e in compras_novas[:10]:
        print(f"  [purchase] {e['id_cliente']} {e['nome']!r} +R$ {e['delta_receita']:.2f}")
    if len(compras_novas) > 10:
        print(f"  ... e mais {len(compras_novas) - 10}")

    now = datetime.now(timezone.utc).replace(tzinfo=None)

    # Fase 1: Conversoes Offline
    purchase_events = build_purchase_events(novos, compras_novas, now)
    print(f"[events] {len(purchase_events)} eventos Purchase pra Meta CAPI + TikTok Events")
    for ev in purchase_events[:10]:
        print(f"  [ev] {ev['event_id']} {ev['tipo']} R$ {ev['value']:.2f}")
    send_to_meta_capi(purchase_events)
    send_to_tiktok_events(purchase_events)

    # Fase 2A: Meta Custom Audience + Lookalikes
    sync_meta_audiences(clients)

    # Fase 2B: TikTok Custom Audience + Lookalikes
    sync_tiktok_audiences(clients)

    # Sheets update
    rows = [process_client(c, now) for c in clients]
    sheets.values().clear(
        spreadsheetId=spreadsheet_id,
        range=DATA_RANGE_CLEAR,
    ).execute()
    print(f"[sheets] range {DATA_RANGE_CLEAR} limpo")
    sheets.values().update(
        spreadsheetId=spreadsheet_id,
        range=DATA_RANGE_WRITE,
        valueInputOption="RAW",
        body={"values": rows},
    ).execute()
    print(f"[sheets] {len(rows)} linhas escritas")
    return 0


if __name__ == "__main__":
    sys.exit(main())
