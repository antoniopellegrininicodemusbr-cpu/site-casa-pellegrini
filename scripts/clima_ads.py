#!/usr/bin/env python3
"""
Robo de clima — Casa Pellegrini (criado 25/09/2026, decisao Antonio)
Roda diario 06h BRT. Le a previsao do dia pra Petropolis e liga/desliga anuncios
sensiveis a clima (flags sazon:frio / sazon:calor na esteira-fila.json).

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
Env: META_ADS_TOKEN (preferido) ou IG_ACCESS_TOKEN; GOOGLE_WEATHER_KEY (opcional); DRY_RUN.
"""
import json, os, re, sys, urllib.request

LAT, LON = -22.5054, -43.1786  # Centro Historico de Petropolis
FRIO_TMAX, CALOR_TMAX = 19.0, 24.0
CHUVA_PROB, CHUVA_MM = 70, 8.0
FILA = "data/esteira-fila.json"
STATE = "data/clima-state.json"
TOKEN = os.environ.get("META_ADS_TOKEN") or os.environ.get("IG_ACCESS_TOKEN") or ""
DRY = os.environ.get("DRY_RUN", "false").lower() == "true"
GRAPH = "https://graph.facebook.com/v21.0"

def http_json(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())

def clima_google(key):
    u = (f"https://weather.googleapis.com/v1/forecast/days:lookup?key={key}"
         f"&location.latitude={LAT}&location.longitude={LON}&days=1")
    d = http_json(u)["forecastDays"][0]
    tmax = d["maxTemperature"]["degrees"]
    day = d.get("daytimeForecast", {})
    prob = day.get("precipitation", {}).get("probability", {}).get("percent", 0)
    mm = day.get("precipitation", {}).get("qpf", {}).get("quantity", 0)
    return tmax, prob, mm, "google"

def clima_metno():
    u = f"https://api.met.no/weatherapi/locationforecast/2.0/compact?lat={LAT}&lon={LON}"
    d = http_json(u, headers={"User-Agent": "casa-pellegrini-clima-ads/1.0 github.com/antoniopellegrininicodemusbr-cpu"})
    ts = d["properties"]["timeseries"][:18]  # ~proximas 18h
    temps = [t["data"]["instant"]["details"]["air_temperature"] for t in ts]
    mm = sum(t["data"].get("next_1_hours", {}).get("details", {}).get("precipitation_amount", 0) for t in ts)
    return max(temps), None, mm, "met.no"

def veredito():
    key = os.environ.get("GOOGLE_WEATHER_KEY", "").strip()
    try:
        tmax, prob, mm, fonte = clima_google(key) if key else clima_metno()
    except Exception as e:
        print(f"clima: fonte primaria falhou ({e}); tentando met.no")
        tmax, prob, mm, fonte = clima_metno()
    chuva = (prob or 0) >= CHUVA_PROB or (mm or 0) >= CHUVA_MM
    if tmax <= FRIO_TMAX or chuva: v = "FRIO"
    elif tmax >= CALOR_TMAX: v = "CALOR"
    else: v = "AMENO"
    print(f"clima ({fonte}): tmax={tmax:.1f}C prob={prob} mm={mm:.1f} chuva={chuva} -> {v}")
    return v

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
    v = veredito()
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
    state = {"pausados_por_clima": sorted(meus), "ultimo_veredito": v, "ultima_execucao": os.environ.get("GITHUB_RUN_ID", "local")}
    if not DRY:
        json.dump(state, open(STATE, "w"), indent=1)
    print(f"veredito={v} acoes={acoes or 'nenhuma'} pausados_por_clima={sorted(meus)}")

if __name__ == "__main__":
    main()
