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
  full   (padrao) - insere midias novas, expira, tria no Gemini, grava JSON e espelha.
                    No fim roda tambem o re-passe de LEGENDA nos aprovados que ainda nao passaram por ele.
  mirror          - so espelha o JSON na planilha (usado no gatilho de push)
  legendas        - SO o re-passe de legenda nos APROVADO_FILA/REPESCAGEM (barato, texto puro, sem video)

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

# Itens/linhas que SAIRAM do cardapio. Um criativo que anuncia qualquer um deles nao pode ir pro ar,
# por melhor que seja o video. Manter sincronizado com o PDF do Drive (memoria cardapio_sync_pdf_07-08-2026).
ITENS_FORA_DO_CARDAPIO = [
    "drinks do Zodiaco (linha inteira: aries, touro, gemeos, cancer, leao, virgem, libra, escorpiao, "
    "sagitario, capricornio, aquario, peixes) — viraram os '5 Autorais'",
    "Drinks Polemicos — mesma extincao dos Zodiacos",
    "Provolone Burger",
    "Gnocchi",
    "qualquer prato anunciado como 'a vontade' / rodizio",
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
no Centro Historico de Petropolis (RJ). Assista ao video (imagem E audio) E leia a LEGENDA do post, e decida
se ele serve como ANUNCIO PAGO EVERGREEN, ou seja, algo que pode rodar em qualquer semana do ano sem ficar
estranho.

⚠️ A LEGENDA CONTA TANTO QUANTO O VIDEO. Isto vira um anuncio impulsionado a partir do post original, entao
a legenda vai junto pro ar exatamente como esta. Um video impecavel com legenda vencida ("ate dia 31 de
Outubro", "em breve lancaremos", "so essa semana", preco antigo) REPROVA. Julgue os dois.

ITENS QUE SAIRAM DO CARDAPIO — se o video OU a legenda anuncia qualquer um destes, REPROVE
(motivo: "item fora do cardapio"):
{chr(10).join("- " + i for i in ITENS_FORA_DO_CARDAPIO)}

CONTEXTO DE HOJE: data {HOJE.isoformat()}, estacao no Brasil = {ESTACAO}.

REPROVE SEMPRE (criterio rigido — "na duvida, reprova, temos conteudo bom de sobra"):
- QUALQUER indicio de data ou contexto comemorativo: aniversario da casa, mesversario, Dia das Maes/Pais,
  Dia do Trabalhador, Natal, Ano Novo, Pascoa/Semana Santa, Dia dos Namorados, Dia do Garcom, festa junina,
  Bauernfest, Copa do Mundo, eleicao, eventos com data marcada, corridas, estreias de serie/filme.
- Texto na tela, fala OU LEGENDA citando dia da semana / horario especifico / data ("quarta as 12h",
  "so hoje", "amanha", "ate dia 31 de outubro").
- Promocao com prazo ou preco que pode mudar — inclusive preco escrito na LEGENDA.
- Promessa que ja venceu: "em breve", "vem ai", "lancaremos", "novidade chegando". O post e antigo; o
  "em breve" dele ja aconteceu ha muito tempo e hoje soa velho.
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
 "momento_datado": "nenhum"|"aniversario"|"mesversario"|"promocao_com_prazo"|"evento_unico"|"cafe_da_manha"|"promessa_vencida"|"item_fora_do_cardapio",
 "sazonalidade": "nenhuma"|"calor"|"frio"|"data_especifica",
 "conteudo_ia_celebridade": true|false,
 "tema": "2-4 palavras descrevendo o video"}}"""


# ------------------------------------------------- re-passe de legenda (texto)
#
# Por que existe: ate 01/09/2026 a caption NAO ia no prompt do fluxo de video — o Gemini julgava so a
# imagem/audio. Isso deixou passar o "Em breve, lancaremos um novo cardapio ... ate o dia 31 de Outubro"
# com nota 9 (post de out/2024), pego a mao pelo Antonio. Como o anuncio e um post impulsionado, a legenda
# vai pro ar junto. Este passe reavalia SO O TEXTO dos que ja estao aprovados. E barato (sem upload de
# video) e idempotente: quem passa ganha a flag legenda_v2:ok e nunca mais e reavaliado.

PROMPT_LEGENDA = f"""Voce revisa LEGENDAS de posts do Instagram da Casa Pellegrini (restaurante/bar em
Petropolis) que serao impulsionados como anuncio HOJE, {HOJE.isoformat()}. O post e antigo; a legenda vai
pro ar exatamente como esta escrita.

REPROVE a legenda se ela tiver QUALQUER um destes:
- data, prazo ou janela ("ate dia 31", "so hoje", "essa semana", "quarta as 12h", mes especifico);
- preco em reais (preco velho no ar gera reclamacao no balcao);
- promessa ja vencida ("em breve", "vem ai", "lancaremos", "novidade chegando", "aguardem");
- ocasiao comemorativa datada (dia das maes, natal, festa junina, Bauernfest, copa, eleicao, corrida);
- cafe da manha (a casa so serve domingo/feriado 7h45-11h e o anuncio roda a partir das 10h);
- delivery ou iFood (a campanha e pra visita fisica);
- qualquer ITEM QUE SAIU DO CARDAPIO:
{chr(10).join("- " + i for i in ITENS_FORA_DO_CARDAPIO)}

APROVE qualquer legenda atemporal — descricao de comida/bebida, convite generico, hashtags, endereco,
telefone. Na duvida entre "atemporal" e "so uma frase solta", APROVE: o video ja foi aprovado antes.

Responda SOMENTE JSON valido:
{{"ok": true|false, "motivo": "uma frase curta em portugues"}}"""


def gemini_legenda(api_key, caption):
    r = requests.post(
        f"{GEMINI_BASE}/v1beta/models/{GEMINI_MODEL}:generateContent?key={api_key}",
        json={
            "contents": [{"parts": [
                {"text": PROMPT_LEGENDA + "\n\nLEGENDA:\n" + (caption or "").strip()},
            ]}],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }, timeout=90)
    r.raise_for_status()
    return json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])


def repassar_legendas(registros, gem_key):
    """Reavalia a legenda dos aprovados que ainda nao passaram pelo passe v2. Retorna (vistos, reprovados)."""
    alvo = [r for r in registros
            if r["status"] in ("APROVADO_FILA", "REPESCAGEM")
            and "legenda_v2:ok" not in (r.get("flags") or "")]
    if not alvo:
        print("re-passe de legenda: nada pendente")
        return 0, 0
    print(f"re-passe de legenda: {len(alvo)} aprovados a revisar")
    vistos = reprovados = 0
    for r in alvo:
        cap = (r.get("caption") or "").strip()
        try:
            if not cap:
                g = {"ok": True, "motivo": "sem legenda"}
            else:
                g = gemini_legenda(gem_key, cap)
                time.sleep(4)  # free tier: 15 RPM
        except Exception as e:
            print(f"  {r['media_id']} ERRO no re-passe: {str(e)[:140]}", file=sys.stderr)
            continue
        vistos += 1
        fl = [x for x in (r.get("flags") or "").split("|") if x]
        if g.get("ok"):
            fl.append("legenda_v2:ok")
            r["flags"] = "|".join(fl)
        else:
            fl.append("RECHECK_LEGENDA_REPROVOU")
            r["flags"] = "|".join(fl)
            r["status"] = "REPROVADO_GEMINI"
            r["motivo_reprova"] = ("legenda: " + str(g.get("motivo", ""))[:180])
            r["data_triagem"] = HOJE.isoformat()
            reprovados += 1
            print(f"  ✂️  {r['data_post']} {r['media_id']} REPROVADO — {g.get('motivo')}")
    print(f"re-passe de legenda: {vistos} revisados, {reprovados} reprovados")
    return vistos, reprovados


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


def gemini_triar(api_key, file_uri, caption=""):
    legenda = (caption or "").strip() or "(o post nao tem legenda)"
    r = requests.post(
        f"{GEMINI_BASE}/v1beta/models/{GEMINI_MODEL}:generateContent?key={api_key}",
        json={
            "contents": [{"parts": [
                {"file_data": {"mime_type": "video/mp4", "file_uri": file_uri}},
                {"text": PROMPT + "\n\nLEGENDA DO POST (vai pro ar junto do video):\n" + legenda},
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

    if MODE == "legendas":
        # so o re-passe de legenda, sem tocar no Instagram nem baixar video nenhum
        repassar_legendas(registros, os.environ["GEMINI_API_KEY"])
        if DRY_RUN:
            print("DRY_RUN — nada gravado")
            return
        salvar_fila(registros)
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
            g = gemini_triar(gem_key, gemini_upload(gem_key, vid), r.get("caption", ""))

            reprova_hard = (g.get("conteudo_ia_celebridade") is True
                            or g.get("momento_datado", "nenhum") != "nenhum")
            gemini_disse = bool(g.get("aprovado"))
            ok = gemini_disse and not reprova_hard

            r["nota_gemini"] = str(g.get("nota", ""))
            r["flags"] = "|".join(filter(None, [
                # AUDITORIA: separa o que o Gemini reprovou do que a MINHA trava dura derrubou.
                # Sem isso nao da pra saber quantos criativos bons a trava esta matando.
                f"gemini_aprovou:{'sim' if gemini_disse else 'nao'}",
                "TRAVA_DURA_DERRUBOU" if (gemini_disse and reprova_hard) else "",
                f"datado:{g.get('momento_datado')}" if g.get("momento_datado", "nenhum") != "nenhum" else "",
                f"sazon:{g.get('sazonalidade')}" if g.get("sazonalidade", "nenhuma") != "nenhuma" else "",
                "IA_CELEBRIDADE" if g.get("conteudo_ia_celebridade") else "",
                f"tema:{g.get('tema', '')}",
            ]))
            r["data_triagem"] = HOJE.isoformat()
            if ok:
                r["status"] = "APROVADO_FILA"
                r["conjunto_sugerido"] = g.get("conjunto_sugerido", "qualquer")
                # o prompt v4 ja julgou a legenda junto do video -> nao precisa do re-passe
                r["flags"] += "|legenda_v2:ok"
                aprovados += 1
            else:
                r["status"] = "REPROVADO_GEMINI"
                r["motivo_reprova"] = str(g.get("motivo", ""))[:200]
            triados += 1
            print(f"  {r['data_post']} {r['media_id']} -> {r['status']} ({g.get('tema', '')})")
        except Exception as e:
            print(f"  {r['media_id']} ERRO: {str(e)[:160]}", file=sys.stderr)
        time.sleep(4)  # free tier: 15 RPM

    # 4. re-passe de legenda no passivo (aprovados antes do prompt v4). Idempotente: roda ate zerar.
    repassar_legendas(registros, gem_key)

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
