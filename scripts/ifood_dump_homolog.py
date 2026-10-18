# -*- coding: utf-8 -*-
"""Dump bruto das APIs financeiras do iFood (ambiente de TESTE, x-request-homologation)
para a aba RAW_homolog da planilha TESTE. Usado so para responder o questionario de homologacao."""
import gzip, json, os, re, sys, time
sys.path.insert(0, os.path.dirname(__file__))
from ifood_financeiro import IfoodClient, BASE, Sheet

cli = IfoodClient(os.environ["IFOOD_TEST_CLIENT_ID"].strip(), os.environ["IFOOD_TEST_CLIENT_SECRET"].strip(), True)
D0, D1 = os.environ.get("D0", "2025-08-01"), os.environ.get("D1", "2025-08-03")
COMP = os.environ.get("COMP", "2025-08")
out = []

def raw(label, method, path, params=None, body=None):
    if not cli.token or time.time() >= cli.token_exp:
        cli._auth()
    h = {"Authorization": "Bearer " + cli.token, "Accept": "application/json", "x-request-homologation": "true"}
    r = cli.s.request(method, BASE + path, params=params, json=body, headers=h, timeout=60)
    txt = r.text
    out.append([label, method + " " + re.sub(r"[0-9a-f-]{36}", "{id}", path) + " " + json.dumps(params or body or {}), r.status_code, txt])
    print(label, r.status_code, len(txt))
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, None

st, j = raw("merchants", "GET", "/merchant/v1.0/merchants")
mids = [m["id"] for m in (j or [])]
for mid in mids:
    F = "/financial/v3.0/merchants/%s" % mid
    for pg in (None, 1, 2):
        p = {"beginSalesDate": D0, "endSalesDate": D1}
        if pg: p["page"] = pg
        raw("sales p%s" % pg, "GET", F + "/sales", p)
    for pg in (1, 2, 3):
        raw("events %s..%s p%d" % (D0, D1, pg), "GET", F + "/financial-events", {"beginDate": D0, "endDate": D1, "page": pg, "size": 100})
    raw("settlements calc", "GET", F + "/settlements", {"beginCalculationDate": D0, "endCalculationDate": D1})
    raw("settlements pay", "GET", F + "/settlements", {"beginPaymentDate": D0, "endPaymentDate": "2025-08-31"})
    raw("anticipations calc", "GET", F + "/anticipations", {"beginCalculationDate": D0, "endCalculationDate": D1})
    raw("anticipations pay", "GET", F + "/anticipations", {"beginAnticipatedPaymentDate": D0, "endAnticipatedPaymentDate": "2025-08-31"})
    raw("reconciliation GET", "GET", F + "/reconciliation", {"competence": COMP})
    st, j = raw("recon on-demand POST", "POST", F + "/reconciliation/on-demand", body={"competence": COMP})
    m = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", json.dumps(j or {}))
    if m:
        url = None
        for i in range(12):
            time.sleep(min(5 * (i + 1), 30))
            st, j = raw("recon on-demand GET %d" % i, "GET", F + "/reconciliation/on-demand/" + m.group(0))
            s = json.dumps(j or {})
            u = re.search(r'"(https?://[^"]+)"', s)
            if u:
                url = u.group(1); break
            if any(k in s.lower() for k in ("error", "failed", "expired")):
                break
        if url:
            r = cli.s.get(url, timeout=120)
            data = r.content
            if data[:2] == b"\x1f\x8b":
                data = gzip.decompress(data)
            out.append(["recon CSV", "download", r.status_code, data.decode("utf-8-sig", "replace")])
    # tambem tenta a URL do GET simples, se houver
    for row in list(out):
        if row[0] == "reconciliation GET" and row[2] == 200:
            u = re.search(r'"(https?://[^"]+)"', row[3])
            if u:
                r = cli.s.get(u.group(1), timeout=120)
                data = r.content
                if data[:2] == b"\x1f\x8b":
                    data = gzip.decompress(data)
                out.append(["recon GET CSV", "download", r.status_code, data.decode("utf-8-sig", "replace")])

rows = []
for lab, req, st, txt in out:
    chunks = [txt[i:i + 45000] for i in range(0, len(txt), 45000)] or [""]
    for k, c in enumerate(chunks):
        rows.append([lab, req, st, k, c])
sh = Sheet(os.environ["GOOGLE_SHEETS_CREDS"], os.environ["SPREADSHEET_TEST"])
sh.ensure("RAW_homolog")
sh.api.values().clear(spreadsheetId=sh.id, range="'RAW_homolog'").execute()
sh.api.values().update(spreadsheetId=sh.id, range="'RAW_homolog'!A1", valueInputOption="RAW",
                       body={"values": [["label", "request", "status", "chunk", "body"]] + rows}).execute()
print("ok", len(rows))
