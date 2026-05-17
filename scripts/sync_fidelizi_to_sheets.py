#!/usr/bin/env python3
"""
Sincroniza a base de clientes do Fidelizi com a planilha Google Sheets
"Base Fidelizi - Casa Pellegrini".

Roda diariamente via GitHub Actions. Faz:
  1. GET na API do Fidelizi (todos os clientes, paginado)
  2. Lê o estado atual da planilha (A2:P) ANTES de sobrescrever
  3. Calcula campos derivados (dias_inativo, premios_pendentes, email_sha256, phone_sha256)
  4. Detecta DELTAS: clientes novos + clientes cujo `compras` ou `receita` aumentou
  5. Loga os deltas (na Fase 2, esses deltas viram eventos Meta CAPI + TikTok Events)
  6. Limpa a aba (preservando os headers em A1:P1)
  7. Reescreve todos os clientes em A2:P

Variáveis de ambiente:
  FIDELIZI_APP_TOKEN     - app token do Fidelizi
  FIDELIZI_ACCESS_TOKEN  - access token do Fidelizi
  FIDELIZI_SHOP_ID       - ID da loja (4477 pra Casa Pellegrini)
  SPREADSHEET_ID         - ID da planilha Google Sheets
  GOOGLE_SHEETS_CREDS    - conteúdo (JSON) do arquivo da Service Account
"""

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone

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

# ---------- Helpers ---------------------------------------------------------

def parse_dt(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def normalize_phone_e164(phone: str) -> str:
    """Mantém só dígitos e prefixa com +. Fidelizi retorna +55 já incluso."""
    if not phone:
        return ""
    digits = re.sub(r"\D", "", phone)
    if not digits:
        return ""
    return "+" + digits


def hash_sha256(s: str) -> str:
    """SHA256 hex de string lowercase + trim. Vazio se string vazia."""
    if not s:
        return ""
    return hashlib.sha256(s.strip().lower().encode("utf-8")).hexdigest()


def sum_premios_pendentes(c: dict) -> int:
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

def fetch_fidelizi_clients(app_token: str, access_token: str, shop_id: str) -> list:
    """Pega todos os clientes da loja, lidando com paginação."""
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

def read_current_state(sheets, spreadsheet_id: str) -> dict:
    """Lê o estado atual da planilha A2:P. Retorna dict por id_cliente.

    Usa valueRenderOption=UNFORMATTED_VALUE pra ler números como float nativo
    (sem formatação locale BR que usa vírgula no separador decimal e quebra
    o float() do Python).
    """
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
        # Padding pra garantir 16 colunas
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


def ensure_headers(sheets, spreadsheet_id: str) -> None:
    """Garante que a linha 1 tem todos os 16 headers (idempotente)."""
    sheets.values().update(
        spreadsheetId=spreadsheet_id,
        range=HEADER_RANGE,
        valueInputOption="RAW",
        body={"values": [HEADERS]},
    ).execute()


# ---------- Transformação ---------------------------------------------------

def process_client(c: dict, now: datetime) -> list:
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


def is_test_client(c: dict) -> bool:
    """Identifica clientes de teste/treinamento (excluídos dos deltas)."""
    nome = (c.get("nome") or "").lower()
    return any(p in nome for p in TEST_NAME_PATTERNS)


def detect_deltas(previous_state: dict, clients: list) -> dict:
    """Compara estado anterior (do Sheets) com novo (do Fidelizi).

    Retorna:
      {
        'novos_clientes': [...],
        'compras_novas':  [...]  # eventos de Purchase pra Conversões Offline
      }

    Clientes de teste/treinamento (TESTADOR, etc) são silenciosamente excluídos.
    """
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
                "receita": c.get("receita", 0) or 0,
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


# ---------- Main ------------------------------------------------------------

def main() -> int:
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

    # Garante headers atualizados (incluindo email_sha256 e phone_sha256)
    ensure_headers(sheets, spreadsheet_id)
    print(f"[sheets] headers garantidos em {HEADER_RANGE}")

    # Lê estado anterior ANTES de buscar novos dados
    previous_state = read_current_state(sheets, spreadsheet_id)
    print(f"[sheets] estado anterior: {len(previous_state)} clientes")

    # Busca estado atual do Fidelizi
    clients = fetch_fidelizi_clients(app_token, access_token, shop_id)
    print(f"[fidelizi] {len(clients)} clientes encontrados")

    if not clients:
        print("[abort] nenhum cliente retornado - nada será escrito")
        return 0

    # Detecta deltas
    deltas = detect_deltas(previous_state, clients)
    novos = deltas["novos_clientes"]
    compras_novas = deltas["compras_novas"]

    print(f"[delta] {len(novos)} clientes NOVOS (primeira vez na base)")
    for n in novos[:10]:
        print(f"  [novo] {n['id_cliente']} {n['nome']!r} R$ {n['receita']}")
    if len(novos) > 10:
        print(f"  ... e mais {len(novos) - 10}")

    print(f"[delta] {len(compras_novas)} COMPRAS NOVAS detectadas (eventos Purchase pra Conversões Offline)")
    for e in compras_novas[:10]:
        print(
            f"  [purchase] {e['id_cliente']} {e['nome']!r} "
            f"+{e['delta_compras']} compra(s), +R$ {e['delta_receita']:.2f}, "
            f"ultima_compra={e['ultima_compra']}"
        )
    if len(compras_novas) > 10:
        print(f"  ... e mais {len(compras_novas) - 10}")

    # Monta as linhas
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rows = [process_client(c, now) for c in clients]

    # Limpa a aba (preservando o header) e reescreve
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
