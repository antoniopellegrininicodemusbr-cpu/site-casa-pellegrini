"""Homologacao do modulo MERCHANT do iFood - roda SO na LOJA DE TESTE com o app de TESTE.
Executa o checklist oficial (developer.ifood.com.br > Merchant > Criterios de homologacao)
e imprime um relatorio PASSOU/FALHOU por cenario. No fim, devolve os horarios originais
da loja de teste e remove as pausas criadas pelo proprio teste."""
import datetime as dt, json, os, sys, time, requests

BASE = "https://merchant-api.ifood.com.br/merchant/v1.0"
AUTH = "https://merchant-api.ifood.com.br/authentication/v1.0/oauth/token"
LOJA_TESTE = os.environ.get("IFOOD_TEST_MERCHANT_ID", "cc5b693f-4405-4d74-8359-ff70d2df3eb7")
LOJA_REAL = "953fe926-cd9d-4ee5-af17-dff66951839f"
if LOJA_TESTE == LOJA_REAL:
    sys.exit("ABORTADO: este teste nunca roda na loja real.")

CID, CSEC = os.environ["IFOOD_CLIENT_ID"], os.environ["IFOOD_CLIENT_SECRET"]
S = requests.Session()
res = []

def agora():
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=-3))).strftime("%d/%m/%Y %H:%M:%S")

def log(cenario, esperado, r, ok):
    try:
        corpo = json.dumps(r.json(), ensure_ascii=False) if r.content else "(sem conteudo)"
    except Exception:
        corpo = r.text
    res.append(ok)
    print("\n[%s] %s  %s\n  %s %s -> HTTP %s (esperado: %s)\n  resposta: %s" % (
        agora(), "PASSOU" if ok else "FALHOU", cenario, r.request.method,
        r.request.path_url, r.status_code, esperado, corpo[:700]))

def call(method, path, token=None, **kw):
    h = kw.pop("headers", {})
    if token is not None:
        h["Authorization"] = "Bearer " + token
    h.setdefault("Accept", "application/json")
    for i in range(4):
        r = S.request(method, BASE + path, headers=h, timeout=30, **kw)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", 2))); continue
        if r.status_code >= 500 and i < 3:
            time.sleep(2 ** i); continue
        return r
    return r

print("=" * 78)
print("HOMOLOGACAO MODULO MERCHANT - Casa Pellegrini")
print("Execucao: %s (horario de Brasilia)" % agora())
print("Aplicativo (client_id): %s" % CID)
print("Loja de teste (merchantId): %s" % LOJA_TESTE)
print("=" * 78)

# 1. Autenticacao
r = S.post(AUTH, data={"grantType": "client_credentials", "clientId": CID, "clientSecret": CSEC}, timeout=30)
r.raise_for_status()
TOKEN = r.json()["accessToken"]
print("\n[%s] token obtido (expira em %ss)" % (agora(), r.json().get("expiresIn")))
r = call("GET", "/merchants"); log("Autenticacao: requisicao SEM token", "401", r, r.status_code == 401)
r = call("GET", "/merchants", token="token-invalido-123"); log("Autenticacao: token INVALIDO", "401", r, r.status_code == 401)

# 2. Lojas
r = call("GET", "/merchants", TOKEN)
lojas = r.json() if r.status_code == 200 else []
log("Listar lojas: GET /merchants", "200 com >=1 loja (id, name, corporateName)", r,
    r.status_code == 200 and any(l.get("id") for l in lojas))
r = call("GET", "/merchants", TOKEN, params={"page": 1, "size": 10})
log("Listar lojas paginado: page=1&size=10", "200 com ate 10 lojas", r,
    r.status_code == 200 and len(r.json()) <= 10)
r = call("GET", "/merchants/%s" % LOJA_TESTE, TOKEN)
log("Detalhes da loja: GET /merchants/{id}", "200 com operations e address", r,
    r.status_code == 200 and "operations" in r.json() and "address" in r.json())

# 3. Status
r = call("GET", "/merchants/%s/status" % LOJA_TESTE, TOKEN)
ok = r.status_code == 200 and isinstance(r.json(), list) and all("state" in s and "validations" in s for s in r.json())
log("Status da loja: GET /merchants/{id}/status", "200 com state (OK/WARNING/CLOSED/ERROR) e validations", r, ok)
if r.status_code == 200:
    for s in r.json():
        print("  -> operacao %s: available=%s state=%s | %s" % (
            s.get("operation"), s.get("available"), s.get("state"),
            (s.get("message") or {}).get("title")))
        for v in s.get("validations", []):
            print("     validacao %-22s %s" % (v.get("code"), v.get("state")))
r = call("GET", "/merchants/%s/status/DELIVERY" % LOJA_TESTE, TOKEN)
log("Status por operacao: GET /status/DELIVERY", "200", r, r.status_code == 200)

# 4. Pausas
r = call("GET", "/merchants/%s/interruptions" % LOJA_TESTE, TOKEN)
log("Listar pausas: GET /interruptions", "200 com array", r, r.status_code == 200 and isinstance(r.json(), list))
ini = dt.datetime.now(dt.timezone.utc).replace(microsecond=0) + dt.timedelta(minutes=10)
fim = ini + dt.timedelta(minutes=30)
iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")
corpo = {"description": "Teste de homologacao Casa Pellegrini", "start": iso(ini), "end": iso(fim)}
r = call("POST", "/merchants/%s/interruptions" % LOJA_TESTE, TOKEN, json=corpo)
pid = r.json().get("id") if r.status_code == 201 else None
log("Criar pausa: POST /interruptions", "201 com id, start, end", r, r.status_code == 201 and bool(pid))
r = call("GET", "/merchants/%s/interruptions" % LOJA_TESTE, TOKEN)
log("Pausa criada aparece na listagem", "200 contendo o id criado", r,
    r.status_code == 200 and any(p.get("id") == pid for p in r.json()))
sob = {"description": "Pausa sobreposta (deve falhar)", "start": iso(ini + dt.timedelta(minutes=5)),
       "end": iso(fim + dt.timedelta(minutes=5))}
r = call("POST", "/merchants/%s/interruptions" % LOJA_TESTE, TOKEN, json=sob)
log("Criar pausa SOBREPOSTA", "409 InterruptionOverlap", r, r.status_code == 409)
if r.status_code == 201:  # limpa se a API aceitou
    call("DELETE", "/merchants/%s/interruptions/%s" % (LOJA_TESTE, r.json().get("id")), TOKEN)
inv = {"description": "Fim antes do inicio (deve falhar)", "start": iso(fim), "end": iso(ini)}
r = call("POST", "/merchants/%s/interruptions" % LOJA_TESTE, TOKEN, json=inv)
log("Parametros invalidos (fim antes do inicio)", "400 com code e message", r, r.status_code == 400)
if pid:
    r = call("DELETE", "/merchants/%s/interruptions/%s" % (LOJA_TESTE, pid), TOKEN)
    log("Remover pausa: DELETE /interruptions/{id}", "204 sem conteudo", r, r.status_code == 204)
    r = call("GET", "/merchants/%s/interruptions" % LOJA_TESTE, TOKEN)
    log("Pausa removida NAO aparece mais", "200 sem o id removido", r,
        r.status_code == 200 and not any(p.get("id") == pid for p in r.json()))

# 5. Horarios
r = call("GET", "/merchants/%s/opening-hours" % LOJA_TESTE, TOKEN)
orig = []
if r.status_code == 200:
    for bloco in r.json():
        orig += bloco.get("shifts", [])
log("Consultar horarios: GET /opening-hours", "200 com turnos (dayOfWeek, start, duration)", r,
    r.status_code == 200 and all({"dayOfWeek", "start", "duration"} <= set(t) for t in orig))
dias = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]
novos = [{"dayOfWeek": d, "start": "11:00:00", "duration": 240} for d in dias] + \
        [{"dayOfWeek": d, "start": "18:00:00", "duration": 300} for d in dias]
r = call("PUT", "/merchants/%s/opening-hours" % LOJA_TESTE, TOKEN, json={"storeId": LOJA_TESTE, "shifts": novos})
log("Atualizar horarios com 2 turnos por dia: PUT /opening-hours", "201 com turnos criados", r, r.status_code == 201)
r = call("GET", "/merchants/%s/opening-hours" % LOJA_TESTE, TOKEN)
lidos = [t for b in (r.json() if r.status_code == 200 else []) for t in b.get("shifts", [])]
log("GET /opening-hours retorna os novos horarios", "14 turnos (11h e 18h)", r,
    r.status_code == 200 and len(lidos) == 14 and {t["start"][:5] for t in lidos} == {"11:00", "18:00"})
sobre = [{"dayOfWeek": "MONDAY", "start": "11:00:00", "duration": 240},
         {"dayOfWeek": "MONDAY", "start": "12:00:00", "duration": 120}]
r = call("PUT", "/merchants/%s/opening-hours" % LOJA_TESTE, TOKEN, json={"storeId": LOJA_TESTE, "shifts": sobre})
log("Turnos SOBREPOSTOS no mesmo dia", "400 BadRequest", r, r.status_code == 400)

# 6. Restaura a loja de teste
if orig:
    volta = [{"dayOfWeek": t["dayOfWeek"], "start": t["start"], "duration": t["duration"]} for t in orig]
    r = call("PUT", "/merchants/%s/opening-hours" % LOJA_TESTE, TOKEN, json={"storeId": LOJA_TESTE, "shifts": volta})
    print("\n[%s] horarios originais da loja de teste restaurados -> HTTP %s" % (agora(), r.status_code))

print("\n" + "=" * 78)
print("RESULTADO: %d de %d cenarios PASSARAM" % (sum(res), len(res)))
print("Fim: %s" % agora())
print("=" * 78)
