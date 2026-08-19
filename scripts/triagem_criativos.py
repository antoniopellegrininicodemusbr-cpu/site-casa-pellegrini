#!/usr/bin/env python3
"""
Esteira de triagem de criativos — Casa Pellegrini.

Mantem uma fila na planilha "Esteira de Criativos" com TODO video do @casapellegrini
dentro da janela de 2 anos, triado pelo Gemini.

Fluxo:
  1. Le a planilha (estado permanente).
  2. Busca as midias do Instagram e insere as que faltam como NAO_TRIADO.
  3. Aplica a janela de 2 anos (marca EXPIRADO quem passou).
  4. Tria ate MAX_POR_RODADA videos NAO_TRIADO, dos mais NOVOS pros mais velhos,
     mandando o video (com audio) pro Gemini.
  5. Grava tudo de volta.

O julgamento de resultado (promover / repescagem) NAO acontece aqui — quem faz isso
e a tarefa agendada do Cowork, que le e escreve nesta mesma planilha.

Secrets: GEMINI_API_KEY, IG_ACCESS_TOKEN, GOOGLE_SHEETS_CREDS
Env:     SPREADSHEET_ID, MAX_POR_RODADA (default 100), DRY_RUN
"""

import json
import os
import sys
import time
from datetime import date, datetime, timedelta

import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

IG_USER_ID = "17841404743438046"
GRAPH = "https://graph.facebook.com/v21.0"
GEMINI_BASE = "https://generativelanguage.googleapis.com"
GEMINI_MODEL = "gemini-3.5-flash"  # 2.5-flash morre em 16/10/2026

SHEET_NAME = "fila"
HEADERS = [
    "media_id", "data_post", "media_type", "permalink", "caption",
    "status", "nota_gemini", "motivo_reprova", "flags", "conjunto_sugerido",
    "data_triagem", "data_entrada_teste", "gasto_teste", "compras_teste",
    "ctr_teste", "data_veredito", "ad_id_promovido",
]
COL = {name: i for i, name in enumerate(HEADERS)}

JANELA_ANOS_DIAS = 730
MAX_DURACAO_S = 120          # video mais longo que isso e caro e nao e criativo de ads
MAX_POR_RODADA = int(os.environ.get("MAX_POR_RODADA", "100"))
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

HOJE = date.today()


# ----------------------------------------------------------------- prompt v3

ESTACAO = "inverno" if HOJE.month in (6, 7, 8, 9) else (
    "verao" if HOJE.month in (12, 1, 2, 3) else "meia-estacao")

PROMPT = f"""Voce e o triador de criativos de anuncio da Casa Pellegrini, um restaurante/bar/hamburgueria
no Centro Historico de Petropolis (RJ). Assista ao video (imagem E audio) e decida se ele serve como
ANUNCIO PAGO EVERGREEN, ou seja, um video que pode rodar em qualquer semana do ano sem ficar estranho.

CONTEXTO DE HOJE: data {HOJE.isoformat()}, estacao no Brasil = {ESTACAO}.

REPROVE SEMPRE (criterio rigido — "na duvida, reprova, temos conteudo bom de sobra"):
- QUALQUER indicio de data ou contexto comemorativo: aniversario da casa, mesversario, Dia das Maes/Pais,
  Dia do Trabalhador, Natal, Ano Novo, Pascoa/Semana Santa, Dia dos Namorados, Dia do Garcom, festa junina,
  Bauernfest, Copa do Mundo, eleicao, eventos com data marcada, corridas, estreias de serie/filme.
- Promocao com prazo ("so hoje", "essa semana", "ate domingo") ou preco que pode mudar.
- CAFE DA MANHA em qualquer forma — a casa so serve cafe aos domingos e feriados das 7h45 as 11h, e as
  janelas dos conjuntos de anuncio comecam as 10h. O anuncio prometeria algo que quase nunca esta disponivel.
- DELIVERY / iFood — a campanha e para visita fisica na casa.
- Conteudo gerado por IA envolvendo celebridade ou pessoa publica (deepfake). Politica da Meta + risco
  juridico. Se aparece um famoso improvavel num restaurante de Petropolis, assuma que e IA.
- Meme, trend passageira, piada interna, homenagem a funcionario, conteudo pessoal dos socios.
- Sazonalidade FORA DE EPOCA: solucao para calor rodando no inverno, ou para frio rodando no verao.
  Sazonal DENTRO da epoca atual ({ESTACAO}) esta OK.
- Qualidade ruim: audio inaudivel, imagem tremida, video cortado no meio.

APROVE: comida/bebida apetitosa, ambiente cheio, happy hour, chopp, atendimento, fachada, prova social
espontanea de cliente real, porcoes, hamburguer, almoco executivo — desde que nada acima se aplique.

Se aprovado, escolha o CONJUNTO de destino:
- "dia"   -> almoco, executivo, feijoada, pratos de almoco, ambiente diurno (roda 10h-16h)
- "noite" -> happy hour, chopp, drink, porcao, hamburguer, ambiente noturno (roda 16h-23h)
- "qualquer" -> serve bem nos dois

Responda SOMENTE com JSON valido, sem markdown, neste formato exato:
{{"aprovado": true|false,
 "nota": 0-10,
 "motivo": "uma frase curta em portugues",
 "conjunto_sugerido": "dia"|"noite"|"qualquer",
 "momento_datado": "nenhum"|"aniversario"|"mesversario"|"promocao_com_prazo"|"evento_unico"|"cafe_da_manha",
 "sazonalidade": "nenhuma"|"calor"|"frio"|"data_especifica",
 "conteudo_ia_celebridade": true|false,
 "tema": "2-4 palavras descrevendo o video"}}"""


# ----------------------------------------------------------------- instagram

def ig_midias(token):
    """Todas as midias do perfil, mais novas primeiro."""
    out = []
    url = (f"{GRAPH}/{IG_USER_ID}/media"
           f"?fields=id,media_type,media_product_type,timestamp,permalink,caption,media_url"
           f"&limit=100&access_token={token}")
    while url and len(out) < 1000:
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        d = r.json()
        out.extend(d.get("data", []))
        url = d.get("paging", {}).get("next")
    return out


# ------------------------------------------------------------------- gemini

def gemini_upload(api_key, video_bytes, mime="video/mp4"):
    """Upload resumable pro Files API. Devolve o file_uri."""
    start = requests.post(
        f"{GEMINI_BASE}/upload/v1beta/files?key={api_key}",
        headers={
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(len(video_bytes)),
            "X-Goog-Upload-Header-Content-Type": mime,
            "Content-Type": "application/json",
        },
        json={"file": {"display_name": "criativo"}},
        timeout=60,
    )
    start.raise_for_status()
    upload_url = start.headers["X-Goog-Upload-URL"]

    up = requests.post(
        upload_url,
        headers={
            "Content-Length": str(len(video_bytes)),
            "X-Goog-Upload-Offset": "0",
            "X-Goog-Upload-Command": "upload, finalize",
        },
        data=video_bytes,
        timeout=300,
    )
    up.raise_for_status()
    info = up.json()["file"]

    # esperar o processamento sair de PROCESSING
    name, uri = info["name"], info["uri"]
    for _ in range(60):
        st = requests.get(f"{GEMINI_BASE}/v1beta/{name}?key={api_key}", timeout=30).json()
        if st.get("state") == "ACTIVE":
            return uri
        if st.get("state") == "FAILED":
            raise RuntimeError("Gemini falhou ao processar o video")
        time.sleep(3)
    raise TimeoutError("video ficou preso em PROCESSING")


def gemini_triar(api_key, file_uri):
    body = {
        "contents": [{"parts": [
            {"file_data": {"mime_type": "video/mp4", "file_uri": file_uri}},
            {"text": PROMPT},
        ]}],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    r = requests.post(
        f"{GEMINI_BASE}/v1beta/models/{GEMINI_MODEL}:generateContent?key={api_key}",
        json=body, timeout=180,
    )
    r.raise_for_status()
    txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(txt)


# ------------------------------------------------------------------- sheets

def ler_planilha(sheets, sid):
    try:
        r = sheets.values().get(spreadsheetId=sid, range=f"{SHEET_NAME}!A1:Q").execute()
    except Exception:
        return {}, []
    vals = r.get("values", [])
    if not vals or vals[0] != HEADERS:
        return {}, []
    linhas = [v + [""] * (len(HEADERS) - len(v)) for v in vals[1:]]
    return {l[COL["media_id"]]: l for l in linhas if l[COL["media_id"]]}, linhas


def gravar_planilha(sheets, sid, linhas):
    sheets.values().update(
        spreadsheetId=sid, range=f"{SHEET_NAME}!A1",
        valueInputOption="RAW", body={"values": [HEADERS]},
    ).execute()
    sheets.values().clear(spreadsheetId=sid, range=f"{SHEET_NAME}!A2:Q").execute()
    if linhas:
        sheets.values().update(
            spreadsheetId=sid, range=f"{SHEET_NAME}!A2",
            valueInputOption="RAW", body={"values": linhas},
        ).execute()


def garantir_aba(sheets, sid):
    meta = sheets.get(spreadsheetId=sid).execute()
    if any(s["properties"]["title"] == SHEET_NAME for s in meta["sheets"]):
        return
    sheets.batchUpdate(
        spreadsheetId=sid,
        body={"requests": [{"addSheet": {"properties": {"title": SHEET_NAME}}}]},
    ).execute()
    print(f"aba '{SHEET_NAME}' criada")


# --------------------------------------------------------------------- main

def main():
    sid = os.environ["SPREADSHEET_ID"]
    ig_token = os.environ["IG_ACCESS_TOKEN"]
    gem_key = os.environ["GEMINI_API_KEY"]

    creds = Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_SHEETS_CREDS"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False).spreadsheets()
    garantir_aba(sheets, sid)

    conhecidos, linhas = ler_planilha(sheets, sid)
    print(f"planilha: {len(linhas)} linhas ja existentes")

    # ---- 1. inserir midias novas
    midias = ig_midias(ig_token)
    limite = (HOJE - timedelta(days=JANELA_ANOS_DIAS)).isoformat()
    novas = 0
    for m in midias:
        if m.get("media_type") != "VIDEO":
            continue
        dia = m["timestamp"][:10]
        if dia < limite or m["id"] in conhecidos:
            continue
        linha = [""] * len(HEADERS)
        linha[COL["media_id"]] = m["id"]
        linha[COL["data_post"]] = dia
        linha[COL["media_type"]] = m.get("media_product_type", "VIDEO")
        linha[COL["permalink"]] = m.get("permalink", "")
        linha[COL["caption"]] = (m.get("caption") or "").replace("\n", " ")[:300]
        linha[COL["status"]] = "NAO_TRIADO"
        linhas.append(linha)
        conhecidos[m["id"]] = linha
        novas += 1
    print(f"midias novas inseridas: {novas}")

    # ---- 2. expirar quem passou dos 2 anos e ainda nao foi promovido
    expirados = 0
    for l in linhas:
        if l[COL["status"]] in ("APROVADO_FILA", "REPESCAGEM", "NAO_TRIADO") \
                and l[COL["data_post"]] < limite:
            l[COL["status"]] = "EXPIRADO"
            expirados += 1
    if expirados:
        print(f"⚠️  {expirados} criativos expiraram (2 anos) sem chegar a ser testados")

    # ---- 3. triar, dos mais NOVOS pros mais velhos
    pendentes = sorted(
        [l for l in linhas if l[COL["status"]] == "NAO_TRIADO"],
        key=lambda l: l[COL["data_post"]], reverse=True,
    )
    print(f"pendentes de triagem: {len(pendentes)} | teto desta rodada: {MAX_POR_RODADA}")

    por_id = {m["id"]: m for m in midias}
    triados = aprovados = 0
    for l in pendentes[:MAX_POR_RODADA]:
        mid = l[COL["media_id"]]
        m = por_id.get(mid, {})
        url = m.get("media_url")
        if not url:
            l[COL["status"]] = "REPROVADO_GEMINI"
            l[COL["motivo_reprova"]] = "sem media_url (video indisponivel na API)"
            l[COL["data_triagem"]] = HOJE.isoformat()
            continue
        try:
            vid = requests.get(url, timeout=180).content
            if len(vid) > 90 * 1024 * 1024:
                raise ValueError("video acima de 90MB")
            r = gemini_triar(gem_key, gemini_upload(gem_key, vid))

            reprova_hard = (
                r.get("conteudo_ia_celebridade") is True
                or r.get("momento_datado", "nenhum") != "nenhum"
            )
            aprovado = bool(r.get("aprovado")) and not reprova_hard

            l[COL["nota_gemini"]] = str(r.get("nota", ""))
            l[COL["flags"]] = "|".join(filter(None, [
                f"datado:{r.get('momento_datado')}" if r.get("momento_datado", "nenhum") != "nenhum" else "",
                f"sazon:{r.get('sazonalidade')}" if r.get("sazonalidade", "nenhuma") != "nenhuma" else "",
                "IA_CELEBRIDADE" if r.get("conteudo_ia_celebridade") else "",
                f"tema:{r.get('tema', '')}",
            ]))
            l[COL["data_triagem"]] = HOJE.isoformat()
            if aprovado:
                l[COL["status"]] = "APROVADO_FILA"
                l[COL["conjunto_sugerido"]] = r.get("conjunto_sugerido", "qualquer")
                aprovados += 1
            else:
                l[COL["status"]] = "REPROVADO_GEMINI"
                l[COL["motivo_reprova"]] = str(r.get("motivo", ""))[:200]
            triados += 1
            print(f"  {l[COL['data_post']]} {mid} -> {l[COL['status']]} ({r.get('tema', '')})")
        except Exception as e:
            print(f"  {mid} ERRO: {str(e)[:160]}", file=sys.stderr)
        time.sleep(4)  # free tier: 15 RPM

    print(f"\ntriados nesta rodada: {triados} | aprovados: {aprovados}")
    resumo = {}
    for l in linhas:
        resumo[l[COL["status"]]] = resumo.get(l[COL["status"]], 0) + 1
    print("estado da fila:", json.dumps(resumo, ensure_ascii=False))

    if DRY_RUN:
        print("DRY_RUN — nada gravado na planilha")
        return
    linhas.sort(key=lambda l: l[COL["data_post"]], reverse=True)
    gravar_planilha(sheets, sid, linhas)
    print("planilha atualizada")


if __name__ == "__main__":
    main()
