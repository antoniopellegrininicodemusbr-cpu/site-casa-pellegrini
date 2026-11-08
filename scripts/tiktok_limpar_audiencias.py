"""Limpa as copias repetidas de publicos do TikTok (08/11/2026).

Bug antigo do robo do pixel: ele nunca achava o publico que ja existia e criava uma copia
nova todo dia - a conta lotou o limite de 400 publicos. Este script deixa 1 de cada nome:
  - se alguma copia esta num conjunto de anuncios (incluida OU excluida), fica essa;
  - se alguma copia e a origem de um lookalike que fica, fica essa;
  - senao, fica a mais recente.
So mexe em publicos com nome "Fidelizi - ..." ou "Clube Pellegrini - ..." (os do robo).
MODO=simular (padrao) so mostra; MODO=apagar apaga de verdade.
"""
import os, sys, json, requests

API = "https://business-api.tiktok.com/open_api/v1.3"
TOKEN = os.environ["TIKTOK_BUSINESS_TOKEN"].strip()
ADV = os.environ["TIKTOK_ADVERTISER_ID"].strip()
MODO = os.environ.get("MODO", "simular").strip().lower()
H = {"Access-Token": TOKEN}
PREFIXOS = ("Fidelizi - ", "Clube Pellegrini - ")

nome = lambda a: a.get("custom_audience_name") or a.get("name") or a.get("audience_name") or ""
aid = lambda a: str(a.get("custom_audience_id") or a.get("audience_id") or "")
quando = lambda a: str(a.get("create_time") or a.get("created_time") or "")


def listar():
    out, page = [], 1
    while True:
        b = requests.get(f"{API}/dmp/custom_audience/list/", headers=H,
                         params={"advertiser_id": ADV, "page": page, "page_size": 100}, timeout=30).json()
        if b.get("code") != 0:
            sys.exit(f"LIST erro: {b.get('message')}")
        out += b["data"].get("list", [])
        if page >= b["data"].get("page_info", {}).get("total_page", 1):
            return out
        page += 1


def usados_em_anuncios():
    usados, page = set(), 1
    while True:
        b = requests.get(f"{API}/adgroup/get/", headers=H,
                         params={"advertiser_id": ADV, "page": page, "page_size": 1000}, timeout=30).json()
        if b.get("code") != 0:
            sys.exit(f"ADGROUP erro: {b.get('message')} (nao apago nada sem saber o que os anuncios usam)")
        for g in b["data"].get("list", []):
            for k in ("audience_ids", "excluded_audience_ids"):
                usados |= {str(x) for x in (g.get(k) or [])}
        if page >= b["data"].get("page_info", {}).get("total_page", 1):
            return usados
        page += 1


def origens(ids):
    """Origem dos lookalikes (pelo /get, que traz lookalike_spec)."""
    src = set()
    ids = list(ids)
    for i in range(0, len(ids), 100):
        b = requests.get(f"{API}/dmp/custom_audience/get/", headers=H,
                         params={"advertiser_id": ADV, "custom_audience_ids": json.dumps(ids[i:i + 100])}, timeout=30).json()
        if b.get("code") != 0:
            print(f"[aviso] GET lookalike: {b.get('message')}")
            continue
        for x in b["data"].get("list", []):
            info = x.get("audience_details") or x
            spec = info.get("lookalike_spec") or {}
            s = spec.get("source_audience_id") or info.get("source_audience_id")
            if s:
                src.add(str(s))
    return src


todos = listar()
print(f"[tiktok] {len(todos)} publicos na conta")
if todos:
    print("[tiktok] campos da lista:", sorted(todos[0].keys()))
usados = usados_em_anuncios()
print(f"[tiktok] {len(usados)} publicos usados em conjuntos de anuncios")

grupos = {}
for a in todos:
    n = nome(a)
    if n.startswith(PREFIXOS) and aid(a):
        grupos.setdefault(n, []).append(a)


def escolher(lista, protegidos):
    fica = [a for a in lista if aid(a) in protegidos]
    if not fica:
        fica = [sorted(lista, key=quando, reverse=True)[0]]
    return {aid(a) for a in fica}


# 1o lookalikes (pra saber de qual origem eles dependem), depois o resto
manter = set()
for n, lista in grupos.items():
    if "Lookalike" in n:
        manter |= escolher(lista, usados)
src = origens([i for i in manter])
for n, lista in grupos.items():
    if "Lookalike" not in n:
        manter |= escolher(lista, usados | src)

apagar = []
for n, lista in sorted(grupos.items()):
    fica = [aid(a) for a in lista if aid(a) in manter]
    sai = [aid(a) for a in lista if aid(a) not in manter]
    apagar += sai
    print(f"  {n}: {len(lista)} copias -> fica {fica} | apaga {len(sai)}")
print(f"[tiktok] TOTAL a apagar: {len(apagar)} (fica {len(manter)})")

if MODO != "apagar":
    print("[simular] nada apagado. Rode com modo=apagar.")
    sys.exit(0)
ok = 0
for i in range(0, len(apagar), 100):
    lote = apagar[i:i + 100]
    b = requests.post(f"{API}/dmp/custom_audience/delete/", headers={**H, "Content-Type": "application/json"},
                      json={"advertiser_id": ADV, "custom_audience_ids": lote}, timeout=60).json()
    print(f"[delete] lote {i // 100 + 1} ({len(lote)}) -> code={b.get('code')} msg={b.get('message')}")
    if b.get("code") == 0:
        ok += len(lote)
print(f"[tiktok] apagados: {ok} de {len(apagar)}")
