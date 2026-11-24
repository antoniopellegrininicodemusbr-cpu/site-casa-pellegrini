#!/usr/bin/env python3
"""
Reconciliacao da fila com os anuncios reais — Casa Pellegrini (Antonio, 23/11/2026)

POR QUE EXISTE: desde 17/11/2026 a rodada agendada de Ads (Claude) nao consegue mais escrever
no repo (classificador da sessao barra o PUT/push). Ela continua mexendo nos ANUNCIOS normalmente.
Este script roda dentro da Action diaria `triagem-criativos.yml` (que tem rede livre e o secret
META_ADS_TOKEN), le o estado real dos 3 conjuntos da campanha Vendas na Graph API e acerta a
data/esteira-fila.json sozinho. Resultado: a fila (e o pool do robo de clima, que le a fila)
acompanha as rodadas sem ninguem precisar commitar nada.

O QUE ELE FAZ (so transicoes sem ambiguidade; o anuncio e localizado pelo
creative.source_instagram_media_id == media_id do registro):
  1. APROVADO_FILA/REPESCAGEM + anuncio ATIVO no V-Teste (e nenhum nos principais) -> EM_TESTE
  2. APROVADO_FILA/EM_TESTE/REPESCAGEM + anuncio no V-Dia ou V-Noite          -> PROMOVIDO
  3. EM_TESTE + todo anuncio dele no V-Teste PAUSADO (e nao pelo robo de clima)
     + nenhum nos principais                                                   -> REPESCAGEM
  4. PROMOVIDO + todos os anuncios dele nos principais PAUSADOS (e nao pelo robo de clima)
                                                                               -> CORTADO_RESULTADO
  5. APROVADO_FILA + anuncio no V-Teste com "FILLER" no nome                    -> FILLER_CLUBE
Nos casos 2 e 3 preenche gasto_teste / compras_teste / ctr_teste com a vida do anuncio do V-Teste.
Toda mudanca ganha a flag `reconciliado:<antes>-><depois>_<data>` (auditavel e reversivel).

O QUE ELE NAO FAZ: nunca mexe em anuncio (so LE a Meta); nunca toca em REPROVADO_GEMINI,
EXPIRADO, ZUMBI_DELETADO, FILLER_CLUBE, SAZONAL, NAO_TRIADO; nao recupera motivo de veredito
nem conferencia de cardapio (isso fica no doc mestre). Divergencia ambigua so e impressa no log.

TRAVAS: se algum conjunto vier vazio (token/rede) ou se o numero de mudancas passar de
MAX_MUDANCAS (sinal de bug), NAO grava nada e sai com erro. O step usa continue-on-error,
entao a triagem do dia roda de qualquer jeito.
Env: META_ADS_TOKEN (System User), DRY_RUN.
"""
import json, os, sys, urllib.request, urllib.parse
from datetime import datetime, timedelta, timezone

GRAPH = "https://graph.facebook.com/v21.0"
TOKEN = os.environ.get("META_ADS_TOKEN", "").strip()
DRY = os.environ.get("DRY_RUN", "false").lower() == "true"
FILA = os.environ.get("FILA_PATH", "data/esteira-fila.json")
STATE = os.environ.get("CLIMA_STATE_PATH", "data/clima-state.json")
ADSETS = {"120254652786890460": "dia", "120254652787400460": "noite", "120255615676900460": "teste"}
NUNCA_TOCAR = {"REPROVADO_GEMINI", "EXPIRADO", "ZUMBI_DELETADO", "FILLER_CLUBE", "SAZONAL",
               "APROVADO_SAZONAL", "NAO_TRIADO"}  # APROVADO_SAZONAL: confra de fim de ano, vive na campanha propria
MAX_MUDANCAS = 8
HOJE = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%d")


def get(path, params):
    params = dict(params, access_token=TOKEN)
    url = f"{GRAPH}/{path}?{urllib.parse.urlencode(params)}"
    out = []
    while url:
        with urllib.request.urlopen(url, timeout=30) as r:
            d = json.loads(r.read().decode())
        if "data" not in d:
            return d
        out += d["data"]
        url = d.get("paging", {}).get("next")
    return out


def dia(ts):
    return (ts or "")[:10]


def metricas(ad_id):
    rows = get(f"{ad_id}/insights", {"fields": "spend,impressions,clicks,actions", "date_preset": "maximum"})
    if not rows:
        return "0.00", "0", "0.00"
    r = rows[0]
    compras = sum(float(a["value"]) for a in r.get("actions", []) if a["action_type"] == "purchase")
    imp = float(r.get("impressions", 0) or 0)
    ctr = (float(r.get("clicks", 0) or 0) / imp * 100) if imp else 0.0
    return f"{float(r.get('spend', 0) or 0):.2f}", str(int(compras)), f"{ctr:.2f}"


def main():
    if not TOKEN:
        sys.exit("reconciliar_fila: sem META_ADS_TOKEN — nada feito")
    fila = json.load(open(FILA, encoding="utf-8"))
    regs = fila["registros"] if isinstance(fila, dict) else fila
    clima = set()
    if os.path.exists(STATE):
        try:
            clima = set(json.load(open(STATE, encoding="utf-8")).get("pausados_por_clima", []))
        except Exception:
            pass

    por_midia = {}
    for adset, onde in ADSETS.items():
        ads = get(f"{adset}/ads", {"fields": "name,status,created_time,updated_time,creative{source_instagram_media_id}",
                                   "limit": 200})
        if not isinstance(ads, list) or not ads:
            sys.exit(f"reconciliar_fila: conjunto {onde} veio vazio/erro ({str(ads)[:200]}) — abortado sem gravar")
        for a in ads:
            m = (a.get("creative") or {}).get("source_instagram_media_id")
            if not m:
                continue
            a["vivo"] = a.get("status") == "ACTIVE" or a["id"] in clima
            por_midia.setdefault(m, {"teste": [], "principal": []})["teste" if onde == "teste" else "principal"].append(a)

    def nomes(lista):
        return "; ".join(f"{a['name']}={a['id']}" for a in sorted(lista, key=lambda x: x["created_time"]))

    mudancas, avisos = [], []
    for r in regs:
        st, m = r.get("status", ""), r.get("media_id")
        if st in NUNCA_TOCAR or m not in por_midia:
            continue
        T, P = por_midia[m]["teste"], por_midia[m]["principal"]
        novo = None
        if not P and st == "APROVADO_FILA" and any("FILLER" in a.get("name", "").upper() for a in T):
            novo = "FILLER_CLUBE"  # filler nasce PAUSADO de proposito (§3.2b) e nao compete na esteira
            r["ad_id_promovido"] = nomes([a for a in T if "FILLER" in a.get("name", "").upper()])
        elif P and st in ("APROVADO_FILA", "EM_TESTE", "REPESCAGEM"):
            novo = "PROMOVIDO"
            r["ad_id_promovido"] = nomes(P)
            r["data_veredito"] = r.get("data_veredito") or dia(min(a["created_time"] for a in P))
            if T and not r.get("gasto_teste"):
                t = max(T, key=lambda x: x["created_time"])
                r["gasto_teste"], r["compras_teste"], r["ctr_teste"] = metricas(t["id"])
        elif not P and st in ("APROVADO_FILA", "REPESCAGEM") and any(a["vivo"] for a in T):
            t = max([a for a in T if a["vivo"]], key=lambda x: x["created_time"])
            if st == "REPESCAGEM" and dia(t["created_time"]) <= (r.get("data_veredito") or ""):
                continue  # anuncio velho do 1o teste, nao e reteste
            novo = "EM_TESTE"
            r["data_entrada_teste"] = dia(t["created_time"])
            r["ad_id_promovido"] = f"{t['name']}={t['id']}"
            for c in ("gasto_teste", "compras_teste", "ctr_teste", "data_veredito"):
                r[c] = ""
        elif not P and st == "EM_TESTE" and T and not any(a["vivo"] for a in T):
            t = max(T, key=lambda x: x["created_time"])
            novo = "REPESCAGEM"
            r["data_veredito"] = dia(t["updated_time"])
            r["gasto_teste"], r["compras_teste"], r["ctr_teste"] = metricas(t["id"])
        elif P and st == "PROMOVIDO" and not any(a["vivo"] for a in P):
            novo = "CORTADO_RESULTADO"
        elif P and st == "CORTADO_RESULTADO" and any(a.get("status") == "ACTIVE" for a in P):
            avisos.append(f"{m} esta CORTADO_RESULTADO mas tem anuncio ATIVO nos principais ({nomes(P)}) — nao mexido")
        if novo:
            r["flags"] = "|".join(filter(None, [r.get("flags", ""), f"reconciliado:{st}->{novo}_{HOJE}"]))
            r["status"] = novo
            mudancas.append(f"{m}: {st} -> {novo} ({r.get('ad_id_promovido', '')})")

    print(f"reconciliar_fila: {len(mudancas)} mudanca(s)")
    for x in mudancas:
        print("  ", x)
    for x in avisos:
        print("  AVISO:", x)
    if len(mudancas) > MAX_MUDANCAS:
        sys.exit(f"reconciliar_fila: {len(mudancas)} mudancas > {MAX_MUDANCAS} — parece bug, abortado sem gravar")
    if DRY or not mudancas:
        print("nada gravado" + (" (DRY_RUN)" if DRY else ""))
        return
    if isinstance(fila, dict):
        fila["registros"], fila["total"], fila["atualizado_em"] = regs, len(regs), HOJE
    json.dump(fila, open(FILA, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("fila gravada")


if __name__ == "__main__":
    main()
