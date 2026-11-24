#!/usr/bin/env python3
"""
Guarda da campanha sazonal de fim de ano — Casa Pellegrini (decisao Antonio 24/11/2026)

Por que existe: a campanha "Reservas Final de Ano" (conjunto "Trafego Reservas", destino
WhatsApp) precisa estar no ar na temporada de confraternizacao e sair do ar quando ja nao
adianta mais vender confra. Ate 2026 isso dependia do Antonio lembrar — e nao deu certo: o
conjunto ficou com end_time 14/12/2025, a campanha ficou pausada e a temporada de 2026
comecou sem nada no ar.

Regra:
  01/11 a 14/12 -> garante campanha ACTIVE + conjunto ACTIVE + end_time = 14/12 do ano corrente
  15/12 a 31/12 -> garante campanha PAUSED
  01/01 a 31/10 -> NAO MEXE EM NADA (se o Antonio ligar fora de epoca, o robo nao atrapalha)

Roda TODO DIA de proposito. O agendamento do GitHub atrasa 4-7h neste repo e as vezes pula
o run inteiro; como cada execucao so "garante que esta assim" (idempotente), um dia perdido
se conserta sozinho no dia seguinte. Dois crons anuais de tiro unico nao teriam segunda chance.

Seguranca: so mexe nesta campanha, e so dentro das duas janelas acima.

Env: META_ADS_TOKEN (preferido) ou IG_ACCESS_TOKEN; DRY_RUN=true pra so mostrar.
"""
import json, os, sys, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

CAMPANHA = "120204216444050460"   # Reservas Final de Ano
CONJUNTO = "120204216444130460"   # Trafego Reservas (WhatsApp)
STATE    = "data/campanha-sazonal-state.json"
GRAPH    = "https://graph.facebook.com/v21.0"
TOKEN    = os.environ.get("META_ADS_TOKEN") or os.environ.get("IG_ACCESS_TOKEN") or ""
DRY      = os.environ.get("DRY_RUN", "false").lower() == "true"

ABRE_MES, ABRE_DIA   = 11, 1    # liga em 01/11
FECHA_MES, FECHA_DIA = 12, 15   # pausa em 15/12 (ultimo dia no ar: 14/12)


def http(url, data=None):
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def ler(obj_id, fields):
    return http(f"{GRAPH}/{obj_id}?" + urllib.parse.urlencode({"fields": fields, "access_token": TOKEN}))


def escrever(obj_id, campos):
    if DRY:
        print(f"  DRY: {obj_id} <- {campos}")
        return True
    campos = dict(campos, access_token=TOKEN)
    r = http(f"{GRAPH}/{obj_id}", data=urllib.parse.urlencode(campos).encode())
    return bool(r.get("success", True))


def fase(hoje):
    """ligar | pausar | fora_de_epoca"""
    abre  = hoje.replace(month=ABRE_MES,  day=ABRE_DIA)
    fecha = hoje.replace(month=FECHA_MES, day=FECHA_DIA)
    if abre <= hoje < fecha:
        return "ligar"
    if fecha <= hoje <= hoje.replace(month=12, day=31):
        return "pausar"
    return "fora_de_epoca"


def main():
    if not TOKEN:
        sys.exit("sem token Meta (META_ADS_TOKEN/IG_ACCESS_TOKEN)")
    hoje = (datetime.now(timezone.utc) - timedelta(hours=3)).date()  # dia em BRT
    f = fase(hoje)
    acoes = []

    cam = ler(CAMPANHA, "name,status")
    print(f"{hoje} fase={f} campanha={cam.get('name')} status={cam.get('status')}")

    if f == "ligar":
        fim = f"{hoje.year}-12-{FECHA_DIA - 1:02d}T23:59:00-0300"
        cj = ler(CONJUNTO, "name,status,end_time")
        if (cj.get("end_time") or "")[:10] != fim[:10]:
            if escrever(CONJUNTO, {"end_time": fim}):
                acoes.append(f"end_time do conjunto -> {fim[:10]}")
        if cj.get("status") != "ACTIVE" and escrever(CONJUNTO, {"status": "ACTIVE"}):
            acoes.append("conjunto -> ACTIVE")
        if cam.get("status") != "ACTIVE" and escrever(CAMPANHA, {"status": "ACTIVE"}):
            acoes.append("campanha -> ACTIVE")

    elif f == "pausar":
        if cam.get("status") != "PAUSED" and escrever(CAMPANHA, {"status": "PAUSED"}):
            acoes.append("campanha -> PAUSED")

    # historico: uma linha por dia, 400 dias (cobre mais de uma temporada inteira)
    state = {}
    if os.path.exists(STATE):
        try:
            state = json.load(open(STATE))
        except Exception:
            pass
    hist = [h for h in state.get("historico", []) if h.get("data") != hoje.isoformat()]
    hist.append({"data": hoje.isoformat(), "fase": f,
                 "status_campanha_antes": cam.get("status"), "acoes": acoes,
                 "run_id": os.environ.get("GITHUB_RUN_ID", "local")})
    hist = sorted(hist, key=lambda h: h["data"])[-400:]
    if not DRY:
        json.dump({"ultima_fase": f, "ultima_execucao": hoje.isoformat(), "historico": hist},
                  open(STATE, "w"), indent=1, ensure_ascii=False)
    print(f"fase={f} acoes={acoes or 'nenhuma (ja estava como devia)'}")


if __name__ == "__main__":
    main()
