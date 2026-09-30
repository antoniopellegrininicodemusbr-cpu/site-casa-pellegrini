#!/usr/bin/env python3
"""
Robo de clima — Casa Pellegrini (criado 25/09/2026, decisao Antonio)
Roda diario 21h07 BRT (cron 00:07 UTC). Le a previsao do DIA SEGUINTE pra Petropolis e
liga/desliga anuncios sensiveis a clima (flags sazon:frio / sazon:calor na esteira-fila.json).

Racional (Antonio 25/09): em Petropolis o clima varia demais pra se guiar por estacao.
Anuncio de caldo pausado num dia quente nao acumula metrica ruim -> as reguas A/B nunca
o julgam pelo dia errado, e o numero de anuncios se mantem.

Veredito do dia:
  FRIO  = tmax <= 19C  OU  precipitacao provavel (>=70% ou >=8mm)
  CALOR = tmax >= 24C  E   sem chuva forte
  AMENO = resto -> tudo ativo
Acao: FRIO -> ativa sazon:frio, pausa sazon:calor. CALOR -> o inverso. AMENO -> ativa os dois.

Seguranca: o robo SO reativa anuncio que ele mesmo pausou (data/clima-state.json).
Nunca mexe em anuncio pausado por regua/humano. So toca ads listados na fila
(status PROMOVIDO ou EM_TESTE) com flag sazon:frio|calor.

Clima: usa Google Weather API se GOOGLE_WEATHER_KEY existir; senao MET Norway (gratis, sem chave).
Historico (28/09/2026): cada execucao grava em data/clima-state.json -> historico[] com
data, veredito, tmax, chuva_mm/prob, fonte e acoes do dia (ultimos 60 dias). E a fonte do
relatorio do robo na rodada de terca/sexta — antes so dava pra saber o veredito, nao o numero.
Dia alvo (30/09/2026, decisao Antonio): a decisao vale pra PROXIMA abertura dos conjuntos
(10h BRT), nao pro dia em que o run acontece. Rodando 21h07 BRT o alvo e amanha; se o
agendador do GitHub atrasar (mede-se 4-7h de atraso em TODOS os workflows deste repo) e o run
cair de madrugada ou de manha antes das 10h, o alvo passa a ser o proprio dia. Imune ao atraso.
Env: META_ADS_TOKEN (preferido) ou IG_ACCESS_TOKEN; GOOGLE_WEATHER_KEY (opcional); DRY_RUN.
"""
import json, os, re, sys, urllib.request
from datetime import datetime, timedelta, timezone

LAT, LON = -22.5054, -43.1786  # Centro Historico de Petropolis
FRIO_TMAX, CALOR_TMAX = 19.0, 24.0
CHUVA_PROB, CHUVA_MM = 70, 8.0
ABRE_HORA = 10  # hora BRT em que os conjuntos abrem
FILA = "data/esteira-fila.json"
STATE = "data/clima-state.json"
TOKEN = os.environ.get("META_ADS_TOKEN") or os.environ.get("IG_ACCESS_TOKEN") or ""
DRY = os.environ.get("DRY_RUN", "false").lower() == "true"
GRAPH = "https://graph.facebook.com/v21.0"

def http_json(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())

def dia_alvo():
    """Dia (BRT) pro qual a decisao vale = proxima abertura de 10h BRT ainda nao ocorrida."""
    agora = datetime.now(timezone.utc) - timedelta(hours=3)
    return agora.date() + timedelta(days=1) if agora.hour >= ABRE_HORA else agora.date()

def clima_google(key, alvo):
    u = (f"https://weather.googleapis.com/v1/forecast/days:lookup?key={key}"
         f"&location.latitude={LAT}&location.longitude={LON}&days=3")
    for d in http_json(u)["forecastDays"]:
        dd = d.get("displayDate") or {}
        if (dd.get("year"), dd.get("month"), dd.get("day")) != (alvo.year, alvo.month, alvo.day):
            continue
        tmax = d["maxTemperature"]["degrees"]
        day = d.get("daytimeForecast", {})
        prob = day.get("precipitation", {}).get("probability", {}).get("percent", 0)
        mm = day.get("precipitation", {}).get("qpf", {}).get("quantity", 0)
        return tmax, prob, mm, "google"
    raise RuntimeError(f"google weather sem previsao para {alvo}")

def clima_metno(alvo):
    u = f"https://api.met.no/weatherapi/locationforecast/2.0/compact?lat={LAT}&lon={LON}"
    d = http_json(u, headers={"User-Agent": "casa-pellegrini-clima-ads/1.0 github.com/antoniopellegrininicodemusbr-cpu"})
    temps, mm = [], 0.0
    for t in d["properties"]["timeseries"]:  # so as horas do dia ALVO, em BRT
        h = datetime.strptime(t["time"], "%Y-%m-%dT%H:%M:%SZ") - timedelta(hours=3)
        if h.date() != alvo: continue
        temps.append(t["data"]["instant"]["details"]["air_temperature"])
        # prefere next_1_hours; onde a serie vira 6-horaria (sem next_1_hours) usa next_6_hours.
        # As entradas 6-horarias sao espacadas de 6h, entao nao ha dupla contagem.
        h1 = t["data"].get("next_1_hours", {}).get("details", {}).get("precipitation_amount")
        h6 = t["data"].get("next_6_hours", {}).get("details", {}).get("precipitation_amount")
        mm += h1 if h1 is not None else (h6 or 0)
    if not temps:
        raise RuntimeError(f"met.no sem previsao para {alvo}")
    return max(temps), None, mm, "met.no"

def veredito(alvo):
    key = os.environ.get("GOOGLE_WEATHER_KEY", "").strip()
    try:
        tmax, prob, mm, fonte = clima_google(key, alvo) if key else clima_metno(alvo)
    except Exception as e:
        print(f"clima: fonte primaria falhou ({e}); tentando met.no")
        tmax, prob, mm, fonte = clima_metno(alvo)
    chuva = (prob or 0) >= CHUVA_PROB or (mm or 0) >= CHUVA_MM
    if tmax <= FRIO_TMAX or chuva: v = "FRIO"
    elif tmax >= CALOR_TMAX: v = "CALOR"
    else: v = "AMENO"
    print(f"clima ({fonte}) para {alvo}: tmax={tmax:.1f}C prob={prob} mm={mm:.1f} chuva={chuva} -> {v}")
    return v, round(tmax, 1), prob, round(mm or 0, 1), fonte

def ads_por_sazon():
    regs = json.load(open(FILA))
    if not isinstance(regs, list): regs = regs.get("registros", regs)
    pool = {"frio": [], "calor": []}
    for r in regs:
        if r.get("status") not in ("PROMOVIDO", "EM_TESTE"): continue
        f = str(r.get("flags") or "")
        m = re.search(r"sazon:(frio|calor)", f)
        if not m: continue
        for aid in re.findall(r"(\d{15,})", str(r.get("ad_id_promovido") or "")):
            pool[m.group(1)].append(aid)
    return pool

def fb_status(aid):
    return http_json(f"{GRAPH}/{aid}?fields=status,effective_status&access_token={TOKEN}")

def fb_set(aid, status):
    if DRY: print(f"  DRY: {aid} -> {status}"); return True
    body = f"status={status}&access_token={TOKEN}".encode()
    r = http_json(f"{GRAPH}/{aid}", data=body)
    return r.get("success", False)

def main():
    if not TOKEN: sys.exit("sem token Meta (META_ADS_TOKEN/IG_ACCESS_TOKEN)")
    alvo = dia_alvo()
    alvo_s = alvo.strftime("%Y-%m-%d")
    agora_brt = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M")
    print(f"agora {agora_brt} BRT -> decidindo para o dia {alvo_s}")
    v, tmax, prob, mm, fonte = veredito(alvo)
    pool = ads_por_sazon()
    print(f"pool frio={pool['frio']} calor={pool['calor']}")
    state = {"pausados_por_clima": []}
    if os.path.exists(STATE):
        try: state = json.load(open(STATE))
        except Exception: pass
    meus = set(state.get("pausados_por_clima", []))
    pausar = pool["calor"] if v == "FRIO" else pool["frio"] if v == "CALOR" else []
    ativar = pool["frio"] if v == "FRIO" else pool["calor"] if v == "CALOR" else pool["frio"] + pool["calor"]
    acoes = []
    for aid in pausar:
        st = fb_status(aid)
        if st.get("status") == "ACTIVE":
            if fb_set(aid, "PAUSED"): meus.add(aid); acoes.append(f"pausou {aid}")
    for aid in ativar:
        if aid not in meus: continue  # so reativa o que o proprio robo pausou
        st = fb_status(aid)
        if st.get("status") == "PAUSED":
            if fb_set(aid, "ACTIVE"): meus.discard(aid); acoes.append(f"reativou {aid}")
    # historico e indexado pelo dia ALVO (o dia que a decisao vale), nao pelo dia do run
    hist = [h for h in state.get("historico", []) if h.get("data") != alvo_s]  # re-run do mesmo alvo sobrescreve
    hist.append({"data": alvo_s, "veredito": v, "tmax": tmax, "chuva_mm": mm, "chuva_prob": prob,
                 "fonte": fonte, "acoes": acoes or [], "pausados_apos": sorted(meus),
                 "executado_em": agora_brt, "run_id": os.environ.get("GITHUB_RUN_ID", "local")})
    hist = sorted(hist, key=lambda h: h["data"])[-60:]  # ~2 meses
    state = {"pausados_por_clima": sorted(meus), "ultimo_veredito": v,
             "ultima_execucao": os.environ.get("GITHUB_RUN_ID", "local"),
             "ultimo_clima": {"data": alvo_s, "tmax": tmax, "chuva_mm": mm, "chuva_prob": prob, "fonte": fonte},
             "historico": hist}
    if not DRY:
        json.dump(state, open(STATE, "w"), indent=1, ensure_ascii=False)
    print(f"veredito={v} acoes={acoes or 'nenhuma'} pausados_por_clima={sorted(meus)}")

if __name__ == "__main__":
    main()
