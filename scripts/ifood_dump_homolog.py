# -*- coding: utf-8 -*-
"""Resumo da loja de TESTE do iFood (x-request-homologation) para responder o questionario
de homologacao financeira. Grava na aba H_resumo da planilha TESTE."""
import csv, gzip, io, json, os, re, sys, time
from collections import defaultdict
sys.path.insert(0, os.path.dirname(__file__))
from ifood_financeiro import IfoodClient, BASE, Sheet

cli = IfoodClient(os.environ["IFOOD_TEST_CLIENT_ID"].strip(), os.environ["IFOOD_TEST_CLIENT_SECRET"].strip(), True)
D0, D1, COMP = os.environ.get("D0", "2025-08-01"), os.environ.get("D1", "2025-08-03"), os.environ.get("COMP", "2025-08")
out = []
def add(*r): out.append([str(x) for x in r])

def raw(method, path, params=None, body=None):
    if not cli.token or time.time() >= cli.token_exp:
        cli._auth()
    h = {"Authorization": "Bearer " + cli.token, "Accept": "application/json", "x-request-homologation": "true"}
    r = cli.s.request(method, BASE + path, params=params, json=body, headers=h, timeout=60)
    try:
        return r.status_code, r.json(), r.text
    except ValueError:
        return r.status_code, None, r.text

st, mer, _ = raw("GET", "/merchant/v1.0/merchants")
mid = mer[0]["id"]
F = "/financial/v3.0/merchants/%s" % mid

# ---------- SALES
st, j, _ = raw("GET", F + "/sales", {"beginSalesDate": D0, "endSalesDate": D1})
for s in (j or {}).get("sales") or []:
    keep = {k: s.get(k) for k in ("shortId", "createdAt", "currentStatus", "saleGrossValue", "benefits", "payments", "billingSummary")}
    add("SALE", s.get("id"), json.dumps(keep, ensure_ascii=False)[:45000])
    for ev in s.get("orderEvents") or []:
        add("SALE_EVENT", ev.get("name") or ev.get("type"), json.dumps(ev, ensure_ascii=False)[:3000])

# ---------- EVENTS
evs, page = [], 1
while True:
    st, j, _ = raw("GET", F + "/financial-events", {"beginDate": D0, "endDate": D1, "page": page, "size": 100})
    e = (j or {}).get("financialEvents") or []
    evs += e
    if not (j or {}).get("hasNextPage") or not e or page > 20:
        break
    page += 1
add("EVENTS_N", len(evs))
if evs:
    add("EVENT_KEYS", json.dumps(sorted(evs[0].keys())), json.dumps(evs[-1], ensure_ascii=False)[:5000])
piv = defaultdict(lambda: [0, 0.0])
for e in evs:
    v = float((e.get("amount") or {}).get("value") or 0)
    pay = e.get("payment") or {}
    k = (e.get("name"), e.get("description"), e.get("trigger"), e.get("hasTransferImpact"), pay.get("liability"), pay.get("method"), (e.get("reference") or {}).get("id", "")[:8])
    piv[k][0] += 1; piv[k][1] += v
for k, (n, v) in sorted(piv.items(), key=lambda x: str(x[0])):
    add("EV", *k, n, round(v, 2))
tot = lambda f: round(sum(float((e.get("amount") or {}).get("value") or 0) for e in evs if f(e)), 2)
add("EV_TOTAL_ALL", tot(lambda e: True))
add("EV_TOTAL_IMPACT", tot(lambda e: e.get("hasTransferImpact")))
add("EV_TOTAL_NOIMPACT", tot(lambda e: not e.get("hasTransferImpact")))
add("EV_TOTAL_SINGLE_OCC", tot(lambda e: "OCCURRENCE" in str(e.get("trigger"))))
add("EV_LIAB_MERCHANT_ALL", tot(lambda e: (e.get("payment") or {}).get("liability") == "MERCHANT"))
add("EV_LIAB_MERCHANT_POS", tot(lambda e: (e.get("payment") or {}).get("liability") == "MERCHANT" and float((e.get("amount") or {}).get("value") or 0) > 0))
add("EV_LIAB_IFOOD_ALL", tot(lambda e: (e.get("payment") or {}).get("liability") == "IFOOD"))
add("EV_POS_ALL", tot(lambda e: float((e.get("amount") or {}).get("value") or 0) > 0))
add("EV_SALDOS", json.dumps(sorted({str((e.get("period") or {}).get("idSaldo")) for e in evs})),
    json.dumps({s: round(sum(float((e.get("amount") or {}).get("value") or 0) for e in evs if str((e.get("period") or {}).get("idSaldo")) == s and e.get("hasTransferImpact")), 2) for s in {str((e.get("period") or {}).get("idSaldo")) for e in evs}}))

# ---------- SETTLEMENTS / ANTICIPATIONS
for ep in ("settlements", "anticipations"):
    for p in ({"beginDate": D0, "endDate": D1}, {"beginDate": D0, "endDate": "2025-08-31"},
              {"beginPaymentDate": D0, "endPaymentDate": "2025-08-31"}, {"beginCalculationDate": D0, "endCalculationDate": D1}):
        st, j, t = raw("GET", F + "/" + ep, p)
        add("SET_" + ep, json.dumps(p), st, t[:40000])

# ---------- RECONCILIATION
st, j, t = raw("GET", F + "/reconciliation", {"competence": COMP})
u = re.search(r'"(https?://[^"]+)"', t)
if u:
    r = cli.s.get(u.group(1), timeout=120)
    data = r.content
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    txt = data.decode("utf-8-sig", "replace")
    rows = list(csv.DictReader(io.StringIO(txt), delimiter=";"))
    hdr = list(rows[0].keys()) if rows else []
    add("RC_HEADER", len(rows), json.dumps(hdr))
    if rows:
        add("RC_ROW0", json.dumps(rows[0], ensure_ascii=False))
    f = lambda x: float((x or "0").replace(",", ".") or 0)
    for col in hdr:
        vals = sorted({r[col] for r in rows})
        if len(vals) <= 25:
            add("RC_DISTINCT", col, len(vals), json.dumps(vals, ensure_ascii=False)[:3000])
        else:
            add("RC_DISTINCT", col, len(vals), "(muitos)")
    keys = [c for c in ("fato_gerador", "tipo_lancamento", "descricao_lancamento", "responsavel_transacao", "impacto_no_repasse", "canal_vendas", "motivo_cancelamento") if c in hdr]
    piv = defaultdict(lambda: [0, 0.0, set()])
    for r in rows:
        k = tuple(r[c] for c in keys)
        piv[k][0] += 1; piv[k][1] += f(r.get("valor")); piv[k][2].add(r.get("pedido_associado_ifood"))
    add("RC_PIVOT_KEYS", json.dumps(keys))
    for k, (n, v, ps) in sorted(piv.items()):
        add("RC", *k, n, round(v, 2), len([p for p in ps if p]))
    orders = {r["pedido_associado_ifood"] for r in rows if r.get("pedido_associado_ifood")}
    add("RC_UNIQUE_ORDERS", len(orders))
    add("RC_UNIQUE_ORDERS_SHORT", len({r["pedido_associado_ifood_curto"] for r in rows if r.get("pedido_associado_ifood_curto")}))
    add("RC_SUM_ALL", round(sum(f(r["valor"]) for r in rows), 2))
    add("RC_SUM_IMPACT_SIM", round(sum(f(r["valor"]) for r in rows if r.get("impacto_no_repasse") == "SIM"), 2))
    for rv in sorted({r.get("responsavel_transacao", "") for r in rows}):
        add("RC_SUM_RESP", rv, round(sum(f(r["valor"]) for r in rows if r.get("responsavel_transacao", "") == rv), 2),
            round(sum(f(r["valor"]) for r in rows if r.get("responsavel_transacao", "") == rv and f(r["valor"]) > 0), 2),
            round(sum(f(r["valor"]) for r in rows if r.get("responsavel_transacao", "") == rv and r.get("impacto_no_repasse") == "SIM"), 2))
    # pedidos com algum cancelamento
    canc = defaultdict(list)
    for r in rows:
        blob = " ".join(str(v) for v in r.values()).lower()
        if "cancel" in blob or "estorno" in blob or "reembolso" in blob:
            canc[r.get("pedido_associado_ifood")].append(r)
    add("RC_CANCEL_ORDERS", len(canc))
    for pid, rs in canc.items():
        allr = [r for r in rows if r.get("pedido_associado_ifood") == pid]
        add("RC_CANCEL", pid, rs[0].get("pedido_associado_ifood_curto"), rs[0].get("motivo_cancelamento"),
            round(sum(f(r["valor"]) for r in allr), 2), round(sum(f(r["valor"]) for r in allr if r.get("impacto_no_repasse") == "SIM"), 2),
            json.dumps([[r.get("fato_gerador"), r.get("tipo_lancamento"), r.get("descricao_lancamento"), r.get("valor"), r.get("impacto_no_repasse"), r.get("valor_transacao"), r.get("valor_cesta_inicial"), r.get("valor_cesta_final")] for r in allr], ensure_ascii=False)[:20000])
else:
    add("RC_ERR", st, t[:2000])

sh = Sheet(os.environ["GOOGLE_SHEETS_CREDS"], os.environ["SPREADSHEET_TEST"])
for t in ("RAW_homolog",):
    if t in sh.tabs:
        sh.api.batchUpdate(spreadsheetId=sh.id, body={"requests": [{"deleteSheet": {"sheetId": sh.tabs[t]["sheetId"]}}]}).execute()
        sh._load()
sh.ensure("H_resumo", index=0)
sh.api.batchUpdate(spreadsheetId=sh.id, body={"requests": [{"updateSheetProperties": {"properties": {"sheetId": sh.tabs["H_resumo"]["sheetId"], "index": 0}, "fields": "index"}}]}).execute()
sh.api.values().clear(spreadsheetId=sh.id, range="'H_resumo'").execute()
sh.api.values().update(spreadsheetId=sh.id, range="'H_resumo'!A1", valueInputOption="RAW", body={"values": out}).execute()
print("ok", len(out))
