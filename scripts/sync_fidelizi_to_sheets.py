#!/usr/bin/env python3
"""
Sincroniza a base de clientes do Fidelizi com a planilha Google Sheets
"Base Fidelizi - Casa Pellegrini" E envia eventos Purchase pras Conversoes
Offline do Meta + TikTok.

Roda diariamente via GitHub Actions. Faz:
  1. GET na API do Fidelizi (todos os clientes, paginado)
  2. Le o estado atual da planilha (A2:P) ANTES de sobrescrever
  3. Calcula campos derivados (dias_inativo, premios_pendentes, *_sha256)
  4. Detecta DELTAS: clientes novos (receita>0) + clientes cujo compras/receita aumentou
  5. Junta novos+compras_novas num unico pool de eventos Purchase
  6. Envia eventos pra Meta CAPI (unified dataset)
  7. Envia eventos pra TikTok Events API (offline event set)
  8. Limpa a aba (preservando o header) e reescreve

Variaveis de ambiente:
  FIDELIZI_APP_TOKEN     - app token do Fidelizi
  FIDELIZI_ACCESS_TOKEN  - access token do Fidelizi
  FIDELIZI_SHOP_ID       - ID da loja (4477 pra Casa Pellegrini)
  SPREADSHEET_ID         - ID da planilha Google Sheets
  GOOGLE_SHEETS_CREDS    - conteudo (JSON) do arquivo da Service Account

  META_CAPI_TOKEN        - (opcional) System User Token com permissao no dataset
  META_DATASET_ID        - (opcional) ID do dataset Meta (ex: 2602966053197429)
  TIKTOK_EVENTS_TOKEN    - (opcional) Access Token gerado dentro do Event Set
  TIKTOK_EVENT_SET_ID    - (opcional) ID do Event Set TikTok (ex: 7640893360621781010)

  Se META_* ou TIKTOK_* nao estiverem setados, o envio pra aquela plataforma e
  silenciosamente pulado (logs indicam). Isso permite rodar o sync sozinho.
"""

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

# ---------- Helpers ---------------------------------------------------------

def parse_dt(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def to_unix_ts(dt_str, fallback_now):
    """Converte 'YYYY-MM-DD HH:MM:SS' (assume BRT/UTC-3) em Unix timestamp UTC.

    Se a string for vazia ou invalida, usa o fallback_now (ja em UTC).
    """
    dt = parse_dt(dt_str)
    if dt is None:
        return int(fallback_now.replace(tzinfo=timezone.utc).timestamp())
    # BRT = UTC - 3h -> UTC = BRT + 3h
    dt_utc = dt + timedelta(hours=3)
    return int(dt_utc.replace(tzinfo=timezone.utc).timestamp())


def normalize_phone_e164(phone):
    """Mantem so digitos e prefixa com +. Fidelizi retorna +55 ja incluso."""
    if not phone:
        return ""
    digits = re.sub(r"\D", "", phone)
    if not digits:
        return ""
    return "+" + digits


def hash_sha256(s):
    """SHA256 hex de string lowercase + trim. Vazio se string vazia."""
    if not s:
        return ""
    return hashlib.sha256(s.strip().lower().encode("utf-8")).hexdigest()


def sum_premios_pendentes(c):
    return sum(
        c.get(f"pendente_resgate_{k}", 0) or 0
        for k in (
            "premio_fidelidade",
            "brinde_roleta",
            "premio_surpresa",
            "premio_campanha",
            "premio_game",
        )
    )


# ---------- Fidelizi --------------------------------------------------------

def fetch_fidelizi_clients(app_token, access_token, shop_id):
    """Pega todos os clientes da loja, lidando com paginacao."""
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


# ---------- Sheets ----------------------------------------------------------

def read_current_state(sheets, spreadsheet_id):
    """Le o estado atual da planilha A2:P. Retorna dict por id_cliente."""
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
    """Garante que a linha 1 tem todos os 16 headers (idempotente)."""
    sheets.values().update(
        spreadsheetId=spreadsheet_id,
        range=HEADER_RANGE,
        valueInputOption="RAW",
        body={"values": [HEADERS]},
    ).execute()


# ---------- Transformacao ---------------------------------------------------

def process_client(c, now):
    """Transforma um cliente Fidelizi numa linha (lista) pra escrever no Sheets."""
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


# ---------- Delta detection -------------------------------------------------

TEST_NAME_PATTERNS = ("testador", "teste fidelizi", "fidelizi teste")


def is_test_client(c):
    """Identifica clientes de teste/treinamento (excluidos dos deltas)."""
    nome = (c.get("nome") or "").lower()
    return any(p in nome for p in TEST_NAME_PATTERNS)


def detect_deltas(previous_state, clients):
    """Compara estado anterior (do Sheets) com novo (do Fidelizi)."""
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


# ---------- Eventos Purchase (Meta + TikTok) --------------------------------

def build_purchase_events(novos, compras_novas, now):
    """Junta novos clientes (com receita>0) + compras_novas num unico pool de
    eventos Purchase prontos pra mandar pras APIs."""
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
    """Envia eventos Purchase pra Meta Conversions API (unified dataset)."""
    token = os.environ.get("META_CAPI_TOKEN", "").strip()
    dataset_id = os.environ.get("META_DATASET_ID", "").strip()
    if not token or not dataset_id:
        print("[meta] META_CAPI_TOKEN/META_DATASET_ID nao configurados - pulando")
        return None
    if not events:
        print("[meta] nenhum evento pra enviar")
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
            "custom_data": {
                "currency": "BRL",
                "value": e["value"],
            },
        })
    payload = {"data": data, "access_token": token}

    r = requests.post(url, json=payload, timeout=30)
    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text}
    print(f"[meta] POST {url} -> {r.status_code}")
    print(f"[meta] response: {json.dumps(body, ensure_ascii=False)[:500]}")
    if r.status_code >= 400:
        print("[meta] ERRO no envio")
    return body


def send_to_tiktok_events(events):
    """Envia eventos Purchase pra TikTok Events API v1.3 (offline event set)."""
    token = os.environ.get("TIKTOK_EVENTS_TOKEN", "").strip()
    event_set_id = os.environ.get("TIKTOK_EVENT_SET_ID", "").strip()
    if not token or not event_set_id:
        print("[tiktok] TIKTOK_EVENTS_TOKEN/TIKTOK_EVENT_SET_ID nao configurados - pulando")
        return None
    if not events:
        print("[tiktok] nenhum evento pra enviar")
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
            "properties": {
                "currency": "BRL",
                "value": e["value"],
            },
        })

    payload = {
        "event_source": "offline",
        "event_source_id": event_set_id,
        "data": data,
    }
    headers = {
        "Access-Token": token,
        "Content-Type": "application/json",
    }
    r = requests.post(TIKTOK_EVENTS_ENDPOINT, headers=headers, json=payload, timeout=30)
    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text}
    print(f"[tiktok] POST {TIKTOK_EVENTS_ENDPOINT} -> {r.status_code}")
    print(f"[tiktok] response: {json.dumps(body, ensure_ascii=False)[:500]}")
    code = body.get("code") if isinstance(body, dict) else None
    if r.status_code >= 400 or (code not in (0, None)):
        print(f"[tiktok] ERRO no envio (code={code})")
    return body


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
        print("[abort] nenhum cliente retornado - nada sera escrito")
        return 0

    deltas = detect_deltas(previous_state, clients)
    novos = deltas["novos_clientes"]
    compras_novas = deltas["compras_novas"]

    print(f"[delta] {len(novos)} clientes NOVOS (primeira vez na base)")
    for n in novos[:10]:
        print(f"  [novo] {n['id_cliente']} {n['nome']!r} R$ {n['receita']}")
    if len(novos) > 10:
        print(f"  ... e mais {len(novos) - 10}")

    print(f"[delta] {len(compras_novas)} COMPRAS NOVAS detectadas")
    for e in compras_novas[:10]:
        print(
            f"  [purchase] {e['id_cliente']} {e['nome']!r} "
            f"+{e['delta_compras']} compra(s), +R$ {e['delta_receita']:.2f}, "
            f"ultima_compra={e['ultima_compra']}"
        )
    if len(compras_novas) > 10:
        print(f"  ... e mais {len(compras_novas) - 10}")

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    purchase_events = build_purchase_events(novos, compras_novas, now)
    print(f"[events] {len(purchase_events)} eventos Purchase a enviar pras APIs")
    for ev in purchase_events[:10]:
        print(f"  [ev] {ev['event_id']} {ev['tipo']} R$ {ev['value']:.2f} ts={ev['event_time']}")

    send_to_meta_capi(purchase_events)
    send_to_tiktok_events(purchase_events)

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
    print(f"[sheets] {len(rows)} linhas escritas em {DATA_RANGE_WRITE}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
