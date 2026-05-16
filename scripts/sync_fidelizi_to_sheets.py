#!/usr/bin/env python3
"""
Sincroniza a base de clientes do Fidelizi com a planilha Google Sheets
"Base Fidelizi - Casa Pellegrini".

Roda diariamente via GitHub Actions. Faz:
  1. GET na API do Fidelizi (todos os clientes, paginado)
  2. Calcula campos derivados (dias_inativo, premios_pendentes)
  3. Limpa a aba (preservando os headers em A1:N1)
  4. Reescreve todos os clientes em A2:N

Variáveis de ambiente:
  FIDELIZI_APP_TOKEN     - app token do Fidelizi
  FIDELIZI_ACCESS_TOKEN  - access token do Fidelizi
  FIDELIZI_SHOP_ID       - ID da loja (4477 pra Casa Pellegrini)
  SPREADSHEET_ID         - ID da planilha Google Sheets
  GOOGLE_SHEETS_CREDS    - conteúdo (JSON) do arquivo da Service Account
"""

import json
import os
import sys
from datetime import datetime, timezone

import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

FIDELIZI_BASE = "https://integracao.fidelizii.com.br/api/v4"
SHEET_NAME = "Página1"
HEADER_RANGE = f"{SHEET_NAME}!A1:N1"
DATA_RANGE_CLEAR = f"{SHEET_NAME}!A2:N"
DATA_RANGE_WRITE = f"{SHEET_NAME}!A2"


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


def parse_dt(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def process_client(c: dict, now: datetime) -> list:
    """Transforma um cliente Fidelizi numa linha (lista) pra escrever no Sheets."""
    uc = parse_dt(c.get("ultima_compra"))
    dias_inativo = (now - uc).days if uc else ""

    pendentes = sum(
        c.get(f"pendente_resgate_{k}", 0) or 0
        for k in (
            "premio_fidelidade",
            "brinde_roleta",
            "premio_surpresa",
            "premio_campanha",
            "premio_game",
        )
    )

    carteira = c.get("carteira") or {}
    saldo = carteira.get("saldo", 0)

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
    ]


def main() -> int:
    # Lê env vars (falha cedo se faltar)
    app_token = os.environ["FIDELIZI_APP_TOKEN"]
    access_token = os.environ["FIDELIZI_ACCESS_TOKEN"]
    shop_id = os.environ.get("FIDELIZI_SHOP_ID", "4477")
    spreadsheet_id = os.environ["SPREADSHEET_ID"]
    creds_json = os.environ["GOOGLE_SHEETS_CREDS"]

    # Autentica no Google via Service Account
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False).spreadsheets()

    # Busca clientes do Fidelizi
    clients = fetch_fidelizi_clients(app_token, access_token, shop_id)
    print(f"[fidelizi] {len(clients)} clientes encontrados")

    if not clients:
        print("[abort] nenhum cliente retornado - nada será escrito")
        return 0

    # Monta as linhas
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rows = [process_client(c, now) for c in clients]

    # Limpa a aba (preservando o header)
    sheets.values().clear(
        spreadsheetId=spreadsheet_id,
        range=DATA_RANGE_CLEAR,
    ).execute()
    print(f"[sheets] range {DATA_RANGE_CLEAR} limpo")

    # Escreve as linhas novas
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
