#!/usr/bin/env python3
"""
Esteira de triagem de criativos — Casa Pellegrini.

FONTE DA VERDADE = data/esteira-fila.json (no repo).
A planilha do Google e um ESPELHO somente-leitura, pro Antonio enxergar.

Motivo do desenho: a sandbox do Cowork nao alcanca sheets.googleapis.com (DNS bloqueado),
mas alcanca github.com por git. Com a fila no repo, o Claude consegue ler E escrever a
esteira nas rodadas de terca/sexta com o PC do Antonio desligado. O push dele dispara
esta Action pelo gatilho `on: push: paths`, que reespelha na planilha em segundos.

MODOS (env MODE):
  full   (padrao) - insere midias novas, expira, tria no Gemini, grava JSON e espelha
  mirror          - so espelha o JSON na planilha (usado no gatilho de push)

Secrets: GEMINI_API_KEY, IG_ACCESS_TOKEN, GOOGLE_SHEETS_CREDS
Env:     SPREADSHEET_ID, MODE, MAX_POR_RODADA (default 100), DRY_RUN
"""

import json
import os
import sys
import time
from datetime import date, timedelta

import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

IG_USER_ID = "17841404743438046"
GRAPH = "https://graph.facebook.com/v21.0"
GEMINI_BASE = "https://generativelanguage.googleapis.com"
GEMINI_MODEL = "gemini-3.5-flash"  # 2.5-flash morre em 16/10/2026

FILA_PATH = "data/esteira-fila.json"
SHEET_NAME = "fila"
CAMPOS = [
    "media_id", "data_post", "media_type", "permalink", "caption",
    "status", "nota_gemini", "motivo_reprova", "flags", "conjunto_sugerido",
    "data_triagem", "data_entrada_teste", "gasto_teste", "compras_teste",
    "ctr_teste", "data_veredito", "ad_id_promovido",
]

JANELA_DIAS = 730
MAX_POR_RODADA = int(os.environ.get("MAX_POR_RODADA", "100"))
MODE = os.environ.get("MODE", "full").strip().lower()
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

HOJE = date.today()
ESTACAO = "inverno" if HOJE.month in (6, 7, 8, 9) else (
    "verao" if HOJE.month in (12, 1, 2, 3) else "meia-estacao")


# ----------------------------------------------------------------- prompt v3

PROMPT = f"""Voce e o triador de criativos de anuncio da Casa Pellegrini, um restaurante/bar/hamburgueria
no Centro Historico de Petropolis (RJ). Assista ao video (imagem E audio) e decida se ele serve como
ANUNCIO PAGO EVERGREEN, ou seja, um video que pode rodar em qualquer semana do ano sem ficar estranho.

CONTEXTO DE HOJE: data {HOJE.isoformat()}, estacao no Brasil = {ESTACAO}.

REPROVE SEMPRE (criterio rigido — "na duvida, reprova, temos conteudo bom de sobra"):
- QUALQUER indicio de data ou contexto comemorativo: aniversario da casa, mesversario, Dia das Maes/Pais,
  Dia do Trabalhador, Natal, Ano Novo, Pascoa/Semana Santa, Dia dos Namorados, Dia do Garcom, festa junina,
  Bauernfest, Copa do Mundo, eleicao, eventos com data marcada, corridas, estreias de serie/filme.
- Texto na tela ou fala citando dia da semana / horario especifico ("quarta as 12h", "so hoje", "amanha").
- Promocao com prazo ou preco que pode mudar.
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
    out, url = [], (
        f"{GRAPH}/{IG_USER_ID}/media"
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
    start = requests.post(
        f"{GEMINI_BASE}/upload/v1beta/files?key={api_key}",
        headers={
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(len(video_bytes)),
            "X-Goog-Upload-Header-Content-Type": mime,
            "Content-Type": "application/json",
        },
        json={"file": {"display_name": "criativo"}}, timeout=60)
    start.raise_for_status()
    up = requests.post(
        start.headers["X-Goog-Upload-URL"],
        headers={
            "Content-Length": str(len(video_bytes)),
            "X-Goog-Upload-Offset": "0",
            "X-Goog-Upload-Command": "upload, finalize",
        },
        data=video_bytes, timeout=300)
    up.raise_for_status()
    info = up.json()["file"]
    for _ in range(60):
        st = requests.get(f"{GEMINI_BASE}/v1beta/{info['name']}?key={api_key}", timeout=30).json()
        if st.get("state") == "ACTIVE":
            return info["uri"]
        if st.get("state") == "FAILED":
            raise RuntimeError("Gemini falhou ao processar o video")
        time.sleep(3)
    raise TimeoutError("video preso em PROCESSING")


def gemini_triar(api_key, file_uri):
    r = requests.post(
        f"{GEMINI_BASE}/v1beta/models/{GEMINI_MODEL}:generateContent?key={api_key}",
        json={
            "contents": [{"parts": [
                {"file_data": {"mime_type": "video/mp4", "file_uri": file_uri}},
                {"text": PROMPT},
            ]}],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }, timeout=180)
    r.raise_for_status()
    return json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])


# ---------------------------------------------------------- fila (fonte json)

def carregar_fila():
    if os.path.exists(FILA_PATH):
        with open(FILA_PATH, encoding="utf-8") as f:
            return json.load(f)["registros"]
    return None


def salvar_fila(registros):
    os.makedirs(os.path.dirname(FILA_PATH), exist_ok=True)
    registros.sort(key=lambda r: r["data_post"], reverse=True)
    with open(FILA_PATH, "w", encoding="utf-8") as f:
        json.dump({
            "atualizado_em": HOJE.isoformat(),
            "total": len(registros),
            "registros": registros,
        }, f, ensure_ascii=False, indent=1)


def bootstrap_do_sheet(sheets, sid):
    """Primeira migracao: a planilha ja tinha dados antes do JSON existir."""
    try:
        vals = sheets.values().get(
            spreadsheetId=sid, range=f"{SHEET_NAME}!A1:Q").execute().get("values", [])
    except Exception:
        return []
    if len(vals) < 2 or vals[0] != CAMPOS:
        return []
    regs = []
    for linha in vals[1:]:
        linha = linha + [""] * (len(CAMPOS) - len(linha))
        r = dict(zip(CAMPOS, linha))
        if r["media_id"]:
            regs.append(r)
    print(f"bootstrap: {len(regs)} registros recuperados da planilha")
    return regs


# ------------------------------------------------------------ espelho sheets

def espelhar(sheets, sid, registros):
    meta = sheets.get(spreadsheetId=sid).execute()
    if not any(s["properties"]["title"] == SHEET_NAME for s in meta["sheets"]):
        sheets.batchUpdate(spreadsheetId=sid, body={
            "requests": [{"addSheet": {"properties": {"title": SHEET_NAME}}}]}).execute()
    linhas = [[str(r.get(c, "")) for c in CAMPOS] for r in registros]
    sheets.values().update(
        spreadsheetId=sid, range=f"{SHEET_NAME}!A1",
        valueInputOption="RAW", body={"values": [CAMPOS]}).execute()
    sheets.values().clear(spreadsheetId=sid, range=f"{SHEET_NAME}!A2:Q").execute()
    if linhas:
        sheets.values().update(
            spreadsheetId=sid, range=f"{SHEET_NAME}!A2",
            valueInputOption="RAW", body={"values": linhas}).execute()
    print(f"espelhado na planilha: {len(linhas)} linhas")


# --------------------------------------------------------------------- main

def main():
    sid = os.environ["SPREADSHEET_ID"]
    creds = Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_SHEETS_CREDS"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets"])
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False).spreadsheets()

    registros = carregar_fila()
    if registros is None:
        print(f"{FILA_PATH} nao existe — tentando bootstrap da planilha")
        registros = bootstrap_do_sheet(sheets, sid)
    print(f"fila carregada: {len(registros)} registros | MODE={MODE}")

    if MODE == "mirror":
        # o Claude deu push no JSON — so reespelha e sai (nao gasta cota do Gemini)
        espelhar(sheets, sid, registros)
        return

    ig_token = os.environ["IG_ACCESS_TOKEN"]
    gem_key = os.environ["GEMINI_API_KEY"]
    por_id = {r["media_id"]: r for r in registros}
    limite = (HOJE - timedelta(days=JANELA_DIAS)).isoformat()

    # 1. inserir midias novas
    midias = ig_midias(ig_token)
    novas = 0
    for m in midias:
        if m.get("media_type") != "VIDEO":
            continue
        dia = m["timestamp"][:10]
        if dia < limite or m["id"] in por_id:
            continue
        r = {c: "" for c in CAMPOS}
        r.update({
            "media_id": m["id"], "data_post": dia,
            "media_type": m.get("media_product_type", "VIDEO"),
            "permalink": m.get("permalink", ""),
            "caption": (m.get("caption") or "").replace("\n", " ")[:300],
            "status": "NAO_TRIADO",
        })
        registros.append(r)
        por_id[m["id"]] = r
        novas += 1
    print(f"midias novas inseridas: {novas}")

    # 2. expirar
    expirados = 0
    for r in registros:
        if r["status"] in ("APROVADO_FILA", "REPESCAGEM", "NAO_TRIADO") and r["data_post"] < limite:
            r["status"] = "EXPIRADO"
            expirados += 1
    if expirados:
        print(f"⚠️  {expirados} criativos expiraram (2 anos) sem chegar a ser testados")

    # 3. triar, dos mais NOVOS pros mais velhos
    pendentes = sorted([r for r in registros if r["status"] == "NAO_TRIADO"],
                       key=lambda r: r["data_post"], reverse=True)
    print(f"pendentes: {len(pendentes)} | teto desta rodada: {MAX_POR_RODADA}")
    midia_por_id = {m["id"]: m for m in midias}
    triados = aprovados = 0

    for r in pendentes[:MAX_POR_RODADA]:
        url = midia_por_id.get(r["media_id"], {}).get("media_url")
        if not url:
            r.update({"status": "REPROVADO_GEMINI",
                      "motivo_reprova": "sem media_url (video indisponivel na API)",
                      "data_triagem": HOJE.isoformat()})
            continue
        try:
            vid = requests.get(url, timeout=180).content
            if len(vid) > 90 * 1024 * 1024:
                raise ValueError("video acima de 90MB")
            g = gemini_triar(gem_key, gemini_upload(gem_key, vid))

            reprova_hard = (g.get("conteudo_ia_celebridade") is True
                            or g.get("momento_datado", "nenhum") != "nenhum")
            ok = bool(g.get("aprovado")) and not reprova_hard

            r["nota_gemini"] = str(g.get("nota", ""))
            r["flags"] = "|".join(filter(None, [
                f"datado:{g.get('momento_datado')}" if g.get("momento_datado", "nenhum") != "nenhum" else "",
                f"sazon:{g.get('sazonalidade')}" if g.get("sazonalidade", "nenhuma") != "nenhuma" else "",
                "IA_CELEBRIDADE" if g.get("conteudo_ia_celebridade") else "",
                f"tema:{g.get('tema', '')}",
            ]))
            r["data_triagem"] = HOJE.isoformat()
            if ok:
                r["status"] = "APROVADO_FILA"
                r["conjunto_sugerido"] = g.get("conjunto_sugerido", "qualquer")
                aprovados += 1
            else:
                r["status"] = "REPROVADO_GEMINI"
                r["motivo_reprova"] = str(g.get("motivo", ""))[:200]
            triados += 1
            print(f"  {r['data_post']} {r['media_id']} -> {r['status']} ({g.get('tema', '')})")
        except Exception as e:
            print(f"  {r['media_id']} ERRO: {str(e)[:160]}", file=sys.stderr)
        time.sleep(4)  # free tier: 15 RPM

    resumo = {}
    for r in registros:
        resumo[r["status"]] = resumo.get(r["status"], 0) + 1
    print(f"\ntriados: {triados} | aprovados: {aprovados}")
    print("estado da fila:", json.dumps(resumo, ensure_ascii=False))

    if DRY_RUN:
        print("DRY_RUN — nada gravado")
        return
    salvar_fila(registros)
    espelhar(sheets, sid, registros)


if __name__ == "__main__":
    main()
