"""TESTE SO LEITURA: o app de producao do iFood consegue ler a situacao da loja?
Faz apenas GETs no modulo Merchant e imprime o codigo HTTP + resposta. Nao grava nada."""
import json, os, requests

BASE = "https://merchant-api.ifood.com.br"
MID = os.environ.get("IFOOD_MERCHANT_ID", "953fe926-cd9d-4ee5-af17-dff66951839f")

r = requests.post(BASE + "/authentication/v1.0/oauth/token", timeout=30,
                  data={"grantType": "client_credentials",
                        "clientId": os.environ["IFOOD_CLIENT_ID"],
                        "clientSecret": os.environ["IFOOD_CLIENT_SECRET"]})
print("token:", r.status_code)
r.raise_for_status()
h = {"Authorization": "Bearer " + r.json()["accessToken"], "Accept": "application/json"}

for nome, path in [
    ("lista de lojas", "/merchant/v1.0/merchants"),
    ("dados da loja", "/merchant/v1.0/merchants/%s" % MID),
    ("SITUACAO (aberta/fechada)", "/merchant/v1.0/merchants/%s/status" % MID),
    ("pausas programadas", "/merchant/v1.0/merchants/%s/interruptions" % MID),
    ("horarios", "/merchant/v1.0/merchants/%s/opening-hours" % MID),
]:
    x = requests.get(BASE + path, headers=h, timeout=30)
    try:
        body = json.dumps(x.json(), ensure_ascii=False)
    except Exception:
        body = x.text
    print("\n=== %s -> HTTP %s\n%s" % (nome, x.status_code, body[:1500]))
