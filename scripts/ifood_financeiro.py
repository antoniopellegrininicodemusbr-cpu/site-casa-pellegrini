#!/usr/bin/env python3
"""iFood Financeiro -> Google Sheets (Casa Pellegrini).

Le as 5 APIs do modulo Financial do iFood (Sales, Financial Events, Settlements,
Anticipations e Reconciliation On Demand) e grava numa planilha do Google Sheets,
uma aba por tipo de dado. Somente leitura no iFood.

Variaveis de ambiente:
  IFOOD_CLIENT_ID / IFOOD_CLIENT_SECRET   credenciais do aplicativo (obrigatorias)
  GOOGLE_SHEETS_CREDS                     JSON da service account (obrigatoria)
  SPREADSHEETS                            JSON {"2026": "<id>", "2027": "<id>"} - uma planilha por ano (producao)
  SPREADSHEET_TEST                        id da planilha de teste (modo homologacao / demo)
  IFOOD_DEMO                              'true' = nao chama o iFood; usa ifood_demo_fixture.json (valida a planilha)
  IFOOD_MERCHANT_ID                       opcional; se vazio usa todas as lojas autorizadas
  IFOOD_HOMOLOGATION                      'true' envia o header x-request-homologation (ambiente de teste)
  DAYS_BACK                               dias para tras a reprocessar (padrao 45)
  DATE_FROM / DATE_TO                     opcional, AAAA-MM-DD, substitui DAYS_BACK
  COMPETENCES                             opcional, 'AAAA-MM,AAAA-MM' para o arquivo de conciliacao

Nunca imprime valores financeiros no log (o repositorio e publico) - so contagens.
"""
import csv
import gzip
import io
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone

BASE = "https://merchant-api.ifood.com.br"
BRT = timezone(timedelta(hours=-3))
MAX_TRIES = 6


# ----------------------------------------------------------------------------
# Cliente HTTP do iFood: token com renovacao automatica + backoff exponencial
# ----------------------------------------------------------------------------
class IfoodClient:
    def __init__(self, client_id, client_secret, homologation=False, session=None):
        import requests
        self.requests = requests
        self.s = session or requests.Session()
        self.client_id = client_id
        self.client_secret = client_secret
        self.homologation = homologation
        self.token = None
        self.token_exp = 0.0
        self.calls = 0

    def _auth(self):
        r = self.s.post(
            BASE + "/authentication/v1.0/oauth/token",
            data={"grantType": "client_credentials", "clientId": self.client_id,
                  "clientSecret": self.client_secret},
            headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
        if r.status_code != 200:
            raise RuntimeError("Falha na autenticacao iFood: HTTP %s" % r.status_code)
        j = r.json()
        self.token = j["accessToken"]
        # renova 5 min antes de vencer
        self.token_exp = time.time() + int(j.get("expiresIn", 3600)) - 300

    def request(self, method, path, params=None, body=None, ok=(200, 202, 204)):
        """Devolve (status, json|None). Trata 401 (renova token), 429 e 5xx (backoff)."""
        delay = 2.0
        last = None
        for attempt in range(1, MAX_TRIES + 1):
            if not self.token or time.time() >= self.token_exp:
                self._auth()
            headers = {"Authorization": "Bearer " + self.token, "Accept": "application/json"}
            if self.homologation:
                headers["x-request-homologation"] = "true"
            try:
                r = self.s.request(method, BASE + path, params=params, json=body,
                                   headers=headers, timeout=60)
            except self.requests.RequestException as e:  # timeout / rede
                last = "rede: %s" % type(e).__name__
                time.sleep(delay); delay = min(delay * 2, 60)
                continue
            self.calls += 1
            if r.status_code in ok or r.status_code == 409:
                try:
                    return r.status_code, (r.json() if r.content else None)
                except ValueError:
                    return r.status_code, None
            if r.status_code == 401:            # token vencido/invalido -> renova e repete
                self.token = None
                last = "401"
                continue
            if r.status_code == 429 or r.status_code >= 500:
                ra = r.headers.get("retry-after")
                wait = float(ra) if ra and ra.replace(".", "", 1).isdigit() else delay
                last = str(r.status_code)
                time.sleep(min(wait, 120)); delay = min(delay * 2, 60)
                continue
            # 400 / 403 / 404: erro definitivo, nao adianta repetir
            raise IfoodError(r.status_code, path, _short(r.text))
        raise IfoodError(0, path, "desistiu apos %d tentativas (ultimo: %s)" % (MAX_TRIES, last))

    def get(self, path, params=None):
        return self.request("GET", path, params=params)


class IfoodError(Exception):
    def __init__(self, status, path, msg):
        super().__init__("HTTP %s em %s: %s" % (status, re.sub(r"[0-9a-f-]{36}", "{id}", path), msg))
        self.status = status


def _short(t):
    return re.sub(r"\s+", " ", t or "")[:200]


# ----------------------------------------------------------------------------
# Datas
# ----------------------------------------------------------------------------
def windows(d0, d1, days):
    """Fatia [d0, d1] em janelas de ate `days` dias (inclusive)."""
    cur = d0
    while cur <= d1:
        end = min(cur + timedelta(days=days - 1), d1)
        yield cur, end
        cur = end + timedelta(days=1)


def to_brt(iso):
    """'2025-08-01T15:23:33.832Z' -> ('2025-08-01', '12:23') no horario de Brasilia."""
    if not iso:
        return "", ""
    s = iso.replace("Z", "+00:00")
    s = re.sub(r"\.(\d{6})\d+", r".\1", s)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return iso[:10], ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(BRT)
    return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")


def num(v):
    if v is None or v == "":
        return ""
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return v


# ----------------------------------------------------------------------------
# Coleta
# ----------------------------------------------------------------------------
def fetch_sales(cli, mid, d0, d1):
    out = {}
    for a, b in windows(d0, d1, 7):           # API aceita ate 90 dias; 7 mantem paginas pequenas
        page = None
        while True:
            p = {"beginSalesDate": a.isoformat(), "endSalesDate": b.isoformat()}
            if page is not None:
                p["page"] = page
            st, j = cli.get("/financial/v3.0/merchants/%s/sales" % mid, p)
            if st == 204 or not j:
                break
            sales = j.get("sales") or []
            for s in sales:
                out[s["id"]] = s
            cur = int(j.get("page", 0) or 0)
            pc = int(j.get("pageCount", 1) or 1)
            first = 0 if page is None and cur == 0 else 1   # API pode numerar a partir de 0 ou 1
            if not sales or (cur - first + 1) >= pc:
                break
            page = cur + 1
    return list(out.values())


def fetch_events(cli, mid, d0, d1):
    """Um dia por consulta: cada linha fica marcada com o dia consultado (chave de substituicao)."""
    rows = []
    day = d0
    while day <= d1:
        page = 1
        while True:
            st, j = cli.get("/financial/v3.0/merchants/%s/financial-events" % mid,
                            {"beginDate": day.isoformat(), "endDate": day.isoformat(),
                             "page": page, "size": 100})
            evs = (j or {}).get("financialEvents") or []
            for e in evs:
                rows.append((day.isoformat(), e))
            if not (j or {}).get("hasNextPage") or not evs:
                break
            page += 1
        day += timedelta(days=1)
    return rows


def _fetch_titles(cli, mid, endpoint, d0, d1, by):
    items = {}
    for a, b in windows(d0, d1, 30):
        try:
            st, j = cli.get("/financial/v3.0/merchants/%s/%s" % (mid, endpoint),
                            {"begin%sDate" % by: a.isoformat(), "end%sDate" % by: b.isoformat()})
        except IfoodError as e:
            print("  aviso: %s (%s) janela ignorada: %s" % (endpoint, by, e))
            continue
        for s in (j or {}).get("settlements") or []:
            for it in s.get("closingItems") or []:
                key = str(it.get("id") or json.dumps(it, sort_keys=True))
                items[key] = (s, it)
    return items


def fetch_settlements(cli, mid, d0, d1):
    items = _fetch_titles(cli, mid, "settlements", d0, d1, "Calculation")
    # repasses pagam ate ~30 dias depois da apuracao: consulta tambem por data de pagamento
    items.update(_fetch_titles(cli, mid, "settlements", d0, d1 + timedelta(days=35), "Payment"))
    return list(items.values())


def fetch_anticipations(cli, mid, d0, d1):
    items = _fetch_titles(cli, mid, "anticipations", d0, d1, "Calculation")
    return list(items.values())


DONE = {"done", "completed", "complete", "finished", "success", "succeeded", "ready", "processed"}
FAIL = {"error", "failed", "failure", "expired", "canceled", "cancelled"}


def find_url(j):
    for v in (j or {}).values():
        if isinstance(v, str) and v.startswith("http"):
            return v
        if isinstance(v, dict):
            u = find_url(v)
            if u:
                return u
    return None


def fetch_reconciliation(cli, mid, competence, max_wait=420):
    """Pede o arquivo de conciliacao do mes, faz polling com backoff e devolve (status, msg, header, rows)."""
    path = "/financial/v3.0/merchants/%s/reconciliation/on-demand" % mid
    st, j = cli.request("POST", path, body={"competence": competence})
    rid = (j or {}).get("requestId") or (j or {}).get("id")
    if st == 409 or not rid:
        # ja existe pedido recente e valido: reaproveita o requestId informado no erro
        m = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", json.dumps(j or {}))
        if not m:
            return "erro", "sem requestId (HTTP %s)" % st, [], []
        rid = m.group(0)
    delay, waited = 3.0, 0.0
    while True:
        st, j = cli.get(path + "/" + rid)
        status = str((j or {}).get("status", "")).lower()
        url = find_url(j)
        if url:
            break
        if status in FAIL:
            msg = str((j or {}).get("errorMessage") or status)
            if "no financial entries" in msg.lower():
                return "sem lancamentos", "Nenhum lancamento financeiro na competencia", [], []
            return "erro", _short(msg), [], []
        if waited >= max_wait:
            return "processando", "arquivo ainda em geracao (status: %s); sera baixado na proxima execucao" % status, [], []
        time.sleep(delay); waited += delay; delay = min(delay * 2, 60)
    r = cli.s.get(url, timeout=120)
    if r.status_code != 200:
        return "erro", "download HTTP %s" % r.status_code, [], []
    data = r.content
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    header, rows = parse_recon_csv(data.decode("utf-8-sig", errors="replace"))
    return "pronto", "", header, rows


NUMERIC_RECON = {"valor", "base_calculo", "percentual_taxa", "valor_transacao",
                 "valor_cesta_inicial", "valor_cesta_final"}


def parse_recon_csv(text):
    rd = csv.reader(io.StringIO(text), delimiter=";")
    rows = [r for r in rd if any(c.strip() for c in r)]
    if not rows:
        return [], []
    header = [h.strip() for h in rows[0]]
    out = []
    for r in rows[1:]:
        r = r + [""] * (len(header) - len(r))
        line = []
        for h, c in zip(header, r):
            c = c.strip()
            if h in NUMERIC_RECON and c:
                try:
                    c = round(float(c.replace(",", ".")), 4)
                except ValueError:
                    pass
            line.append(keep_text(c))
        out.append(line)
    return header, out


def keep_text(c):
    """Codigos so de digitos com zero a esquerda ou longos (pedido curto, CNPJ) ficam como texto."""
    if isinstance(c, str) and c.isdigit() and (c.startswith("0") or len(c) > 11):
        return "'" + c
    return c


# ----------------------------------------------------------------------------
# Transformacao em linhas de planilha
# ----------------------------------------------------------------------------
STATUS_PT = {"CONCLUDED": "Concluído", "CANCELLED": "Cancelado", "CONFIRMED": "Confirmado",
             "DISPATCHED": "Despachado", "PLACED": "Recebido", "CREATED": "Criado"}
ENTRY_PT = {
    "ORDER_PAYMENT": "Pagamento do pedido", "ORDER_COMMISSION": "Comissão iFood",
    "PAYMENT_TRANSACTION_FEE": "Taxa de pagamento online", "SERVICE_FEE": "Taxa de serviço",
    "REFUND_SERVICE_FEE": "Estorno taxa de serviço", "DELIVERY_FEE_IFOOD": "Entrega iFood",
    "DELIVERY_REQUEST": "Solicitação de entrega", "IFOOD_SUBSIDY": "Promoção paga pelo iFood",
    "STORE_SUBSIDY": "Promoção paga pela loja", "CHAIN_SUBSIDY": "Promoção paga pela rede",
}

VENDAS_HDR = ["Data", "Hora", "Pedido", "Status", "Canal", "Entrega", "Quem entrega",
              "Forma de pagamento", "Bandeira", "Quem recebeu do cliente",
              "Itens (R$)", "Taxa de entrega (R$)", "Taxa de serviço (R$)", "Valor bruto (R$)",
              "Promoção iFood (R$)", "Promoção loja (R$)", "Promoção outros (R$)",
              "Pago pelo cliente (R$)", "Comissão iFood (R$)", "Taxa pagamento (R$)",
              "Outros lançamentos (R$)", "Líquido a receber (R$)", "Total de taxas (%)",
              "Repasse previsto", "ID do pedido", "Loja", "Atualizado em"]


def sale_row(s, now):
    d, h = to_brt(s.get("createdAt"))
    g = s.get("saleGrossValue") or {}
    bag, dfee, sfee = num(g.get("bag")) or 0, num(g.get("deliveryFee")) or 0, num(g.get("serviceFee")) or 0
    promo = {"IFOOD": 0.0, "MERCHANT": 0.0}
    outros = 0.0
    for b in ((s.get("benefits") or {}).get("benefits") or []):
        for sp in b.get("sponsorships") or []:
            v = float(sp.get("value") or 0)
            if sp.get("name") in promo:
                promo[sp["name"]] += v
            else:
                outros += v
    methods = ((s.get("payments") or {}).get("methods") or [])
    forma = " + ".join(sorted({str(m.get("method", "")) for m in methods if m.get("method")}))
    band = " + ".join(sorted({str((m.get("card") or {}).get("brand", "")) for m in methods if (m.get("card") or {}).get("brand")}))
    liab = " + ".join(sorted({str(m.get("liability", "")) for m in methods if m.get("liability")}))
    bs = s.get("billingSummary") or {}
    ent = {}
    for e in bs.get("billingEntries") or []:
        ent[e.get("name")] = ent.get(e.get("name"), 0.0) + float(e.get("value") or 0)
    pago = ent.pop("ORDER_PAYMENT", 0.0)
    com = ent.pop("ORDER_COMMISSION", 0.0)
    txp = ent.pop("PAYMENT_TRANSACTION_FEE", 0.0)
    resto = sum(ent.values())
    liquido = num(bs.get("saleBalance"))
    taxas = com + txp + sum(v for k, v in ent.items() if "SUBSIDY" not in k and v < 0)
    pct = round(-taxas / bag * 100, 2) if bag else ""
    prev = ""
    for ev in s.get("orderEvents") or []:
        for en in ((ev.get("metadata") or {}).get("entries") or []) if isinstance(ev.get("metadata"), dict) else []:
            ep = en.get("expectedPaymentDate") or ""
            if ep and (not prev or ep < prev):
                prev = ep
    dl = s.get("delivery") or {}
    return [d, h, "'" + str(s.get("shortId", "")), STATUS_PT.get(s.get("currentStatus"), s.get("currentStatus", "")),
            s.get("salesChannel", ""), dl.get("type", ""),
            (dl.get("deliveryParameters") or {}).get("logisticProvider", ""),
            forma, band, liab, bag, dfee, sfee, round(bag + dfee + sfee, 2),
            round(promo["IFOOD"], 2), round(promo["MERCHANT"], 2), round(outros, 2),
            round(pago, 2), round(com, 2), round(txp, 2), round(resto, 2), liquido, pct,
            prev, s.get("id", ""), (s.get("merchant") or {}).get("name", ""), now]


EVENTOS_HDR = ["Dia consultado", "Competência", "Lançamento", "Descrição", "Gatilho",
               "Valor (R$)", "Impacta repasse", "Base de cálculo (R$)", "Taxa (%)",
               "Repasse previsto", "Período início", "Período fim", "ID do saldo",
               "Tipo de referência", "ID do pedido/transação", "Data da referência",
               "Parcela", "Forma de pagamento", "Bandeira", "Quem recebeu", "Produto", "Atualizado em"]


def event_row(day, e, now):
    per, ref, bil, pay = e.get("period") or {}, e.get("reference") or {}, e.get("billing") or {}, e.get("payment") or {}
    name = e.get("name", "")
    return [day, e.get("competence", ""), ENTRY_PT.get(name, name), e.get("description", ""),
            e.get("trigger", ""), num((e.get("amount") or {}).get("value")),
            "SIM" if e.get("hasTransferImpact") else "NÃO",
            num(bil.get("baseValue")), num(bil.get("feePercentage")),
            (e.get("settlement") or {}).get("expectedDate", ""),
            per.get("beginDate", ""), per.get("endDate", ""), "'" + str(per.get("idSaldo", "")) if per.get("idSaldo") else "",
            ref.get("type", ""), ref.get("id", ""), to_brt(ref.get("date"))[0],
            (bil.get("installments") or {}).get("reference", ""),
            pay.get("method", ""), pay.get("brand", ""), pay.get("liability", ""),
            e.get("product", ""), now]


TITULOS_HDR = ["Apuração início", "Apuração fim", "Data do pagamento", "Tipo", "Produto",
               "Valor (R$)", "Status", "Banco", "Agência", "Conta (final)", "ID do título",
               "ID da transferência", "Atualizado em"]
TIPO_PT = {"REPASSE": "Repasse", "BOLETO": "Boleto (saldo devedor)",
           "REGISTRO_RECEBIVEIS": "Registro de recebíveis", "RENEGOCIADA": "Renegociada"}
STATUS_TIT = {"SUCCEED": "Pago", "FAILED": "Falhou", "TRANSFER_RENEGOTIATED": "Renegociado",
              "SCHEDULED": "Agendado", "PENDING": "Pendente"}


def title_row(pair, now):
    s, it = pair
    acc = it.get("accountDetails") or {}
    conta = str(acc.get("accountNumber") or "")
    conta = ("…" + conta[-4:]) if conta else ""     # nunca grava o numero inteiro da conta
    return [(s.get("startDateCalculation") or "")[:10], (s.get("endDateCalculation") or "")[:10],
            (it.get("paymentDate") or "")[:10], TIPO_PT.get(it.get("type"), it.get("type", "")),
            it.get("product", ""), num(it.get("amount")),
            STATUS_TIT.get(it.get("status"), it.get("status", "")),
            acc.get("bankName", ""), ("'" + str(acc["branchCode"])) if acc.get("branchCode") else "", conta,
            "'" + str(it.get("id", "")), it.get("transactionId", ""), now]


# ----------------------------------------------------------------------------
# Google Sheets
# ----------------------------------------------------------------------------
class Sheet:
    def __init__(self, creds_json, spreadsheet_id):
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build
        creds = Credentials.from_service_account_info(
            json.loads(creds_json), scopes=["https://www.googleapis.com/auth/spreadsheets"])
        self.api = build("sheets", "v4", credentials=creds, cache_discovery=False).spreadsheets()
        self.id = spreadsheet_id
        self._load()

    def _load(self):
        meta = self.api.get(spreadsheetId=self.id, fields="sheets.properties").execute()
        self.tabs = {s["properties"]["title"]: s["properties"] for s in meta["sheets"]}

    def ensure(self, title, index=None):
        if title in self.tabs:
            return
        req = {"addSheet": {"properties": {"title": title}}}
        if index is not None:
            req["addSheet"]["properties"]["index"] = index
        self.api.batchUpdate(spreadsheetId=self.id, body={"requests": [req]}).execute()
        self._load()

    def drop_default(self):
        for t in ("Página1", "Sheet1", "Planilha1", "Página 1"):
            if t in self.tabs and len(self.tabs) > 1:
                self.api.batchUpdate(spreadsheetId=self.id, body={"requests": [
                    {"deleteSheet": {"sheetId": self.tabs[t]["sheetId"]}}]}).execute()
                self._load()

    def read(self, title):
        r = self.api.values().get(spreadsheetId=self.id, range="'%s'" % title,
                                  valueRenderOption="UNFORMATTED_VALUE",
                                  dateTimeRenderOption="FORMATTED_STRING").execute()
        return r.get("values", [])

    def write(self, title, header, rows, money_cols=(), freeze=1):
        self.ensure(title)
        self.api.values().clear(spreadsheetId=self.id, range="'%s'" % title).execute()
        values = [header] + rows
        for i in range(0, len(values), 5000):
            self.api.values().update(
                spreadsheetId=self.id, range="'%s'!A%d" % (title, i + 1),
                valueInputOption="USER_ENTERED", body={"values": values[i:i + 5000]}).execute()
        sid = self.tabs[title]["sheetId"]
        reqs = [
            {"updateSheetProperties": {"properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": freeze}},
                                       "fields": "gridProperties.frozenRowCount"}},
            {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
                            "cell": {"userEnteredFormat": {"textFormat": {"bold": True},
                                                           "backgroundColor": {"red": 0.93, "green": 0.93, "blue": 0.93},
                                                           "wrapStrategy": "WRAP"}},
                            "fields": "userEnteredFormat.textFormat.bold,userEnteredFormat.backgroundColor,userEnteredFormat.wrapStrategy"}},
        ]
        for c in money_cols:
            reqs.append({"repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": 1, "startColumnIndex": c, "endColumnIndex": c + 1},
                "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "#,##0.00"}}},
                "fields": "userEnteredFormat.numberFormat"}})
        self.api.batchUpdate(spreadsheetId=self.id, body={"requests": reqs}).execute()


def norm_key(v):
    return str(v).lstrip("'").strip()


def merge_by_key(existing, fresh, key_idx, ncols):
    """Atualiza/insere por chave unica; preserva o historico que nao veio nesta execucao."""
    m = {}
    for r in existing:
        r = list(r) + [""] * (ncols - len(r))
        if norm_key(r[key_idx]):
            m[norm_key(r[key_idx])] = r[:ncols]
    for r in fresh:
        m[norm_key(r[key_idx])] = r
    return list(m.values())


def merge_by_window(existing, fresh, day_idx, d0, d1, ncols):
    """Substitui todas as linhas cujo dia esta dentro da janela reprocessada; preserva o resto."""
    keep = []
    a, b = d0.isoformat(), d1.isoformat()
    for r in existing:
        r = list(r) + [""] * (ncols - len(r))
        day = str(r[day_idx])[:10]
        if day and not (a <= day <= b):
            keep.append(r[:ncols])
    return keep + fresh


def fix_text_cols(rows, cols):
    """Ao reler da planilha, colunas de texto (ids) voltam sem o apostrofo: recoloca."""
    for r in rows:
        for c in cols:
            if c < len(r) and r[c] != "" and not str(r[c]).startswith("'"):
                r[c] = "'" + str(r[c])
    return rows


# ----------------------------------------------------------------------------
# Principal
# ----------------------------------------------------------------------------
TABS = ["Resumo", "Vendas", "Lançamentos", "Repasses", "Antecipações", "Conciliação"]


def collect_demo():
    """Modo DEMO: usa dados ficticios embutidos (sem chamar o iFood) para validar a planilha."""
    fx = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ifood_demo_fixture.json"), encoding="utf-8"))
    events = [((e.get("period") or {}).get("beginDate", "2025-08-01"), e) for e in fx["events"]]
    settl = [(s, it) for s in fx["settlements"]["settlements"] for it in s["closingItems"]]
    hdr, rows = parse_recon_csv(fx["recon_csv"])
    return fx["sales"], events, settl, [], {("demo", "2025-08"): ("pronto", "", hdr, rows)}, []


def collect_ifood(env, d0, d1, comps, homolog):
    cli = IfoodClient(env["IFOOD_CLIENT_ID"].strip(), env["IFOOD_CLIENT_SECRET"].strip(), homolog)
    mids = [m.strip() for m in env.get("IFOOD_MERCHANT_ID", "").split(",") if m.strip()]
    if not mids:
        st, j = cli.get("/merchant/v1.0/merchants")
        mids = [m["id"] for m in (j or [])]
    if not mids:
        raise SystemExit("Nenhuma loja autorizada para este aplicativo.")
    print("Lojas: %d" % len(mids))
    sales, events, settl, antic, recon, errors = [], [], [], [], {}, []
    for mid in mids:
        for label, fn, store in (("vendas", fetch_sales, sales), ("lancamentos", fetch_events, events),
                                 ("repasses", fetch_settlements, settl), ("antecipacoes", fetch_anticipations, antic)):
            try:
                got = fn(cli, mid, d0, d1)
                store.extend(got)
                print("  %s: %d" % (label, len(got)))
            except IfoodError as e:
                errors.append("%s: %s" % (label, e))
                print("  ERRO %s: %s" % (label, e))
        for comp in comps:
            try:
                recon[(mid, comp)] = fetch_reconciliation(cli, mid, comp)
            except IfoodError as e:
                recon[(mid, comp)] = ("erro", str(e), [], [])
            print("  conciliacao %s: %s (%d linhas)" % (comp, recon[(mid, comp)][0], len(recon[(mid, comp)][3])))
    print("Chamadas ao iFood: %d" % cli.calls)
    return sales, events, settl, antic, recon, errors


def by_year(rows, idxs):
    """Agrupa linhas pelo ano da primeira coluna de data preenchida entre `idxs`."""
    out = {}
    for r in rows:
        y = next((str(r[i])[:4] for i in idxs if str(r[i])[:4].isdigit()), "")
        out.setdefault(y, []).append(r)
    return out


def write_book(sh, label, ambiente, now, d0, d1, v_rows, e_rows, r_rows, a_rows, recon, errors):
    for i, t in enumerate(TABS):
        sh.ensure(t, i)
    sh.drop_default()

    # Vendas (chave = ID do pedido)
    kid = VENDAS_HDR.index("ID do pedido")
    old = fix_text_cols(sh.read("Vendas")[1:], [2])
    rows = merge_by_key(old, v_rows, kid, len(VENDAS_HDR))
    rows.sort(key=lambda r: (str(r[0]), str(r[1])), reverse=True)
    sh.write("Vendas", VENDAS_HDR, rows, money_cols=range(10, 22))
    n_vendas = len(rows)

    # Lancamentos (substitui os dias reprocessados)
    old = fix_text_cols(sh.read("Lançamentos")[1:], [12])
    rows = merge_by_window(old, e_rows, 0, d0, d1, len(EVENTOS_HDR))
    rows.sort(key=lambda r: (str(r[0]), str(r[14])), reverse=True)
    sh.write("Lançamentos", EVENTOS_HDR, rows, money_cols=[5, 7])
    n_ev = len(rows)

    # Repasses e Antecipacoes (chave = ID do titulo)
    n_tit = {}
    for tab, data in (("Repasses", r_rows), ("Antecipações", a_rows)):
        old = fix_text_cols(sh.read(tab)[1:], [8, 10])
        rows = merge_by_key(old, data, 10, len(TITULOS_HDR))
        rows.sort(key=lambda r: str(r[2]), reverse=True)
        sh.write(tab, TITULOS_HDR, rows, money_cols=[5])
        n_tit[tab] = len(rows)

    # Conciliacao (substitui as competencias baixadas; preserva as demais)
    old = sh.read("Conciliação")
    old_hdr, old_rows = (old[0], old[1:]) if old else ([], [])
    new_hdr = next((r[2] for r in recon.values() if r[2]), old_hdr if "competencia" in old_hdr else [])
    got = {comp for (_, comp), r in recon.items() if r[0] == "pronto"}
    if new_hdr:
        ci = new_hdr.index("competencia") if "competencia" in new_hdr else 0
        keep = [[keep_text(c) for c in r] for r in old_rows
                if old_hdr == new_hdr and str(r[ci] if ci < len(r) else "")[:7] not in got]
        fresh = [row for r in recon.values() if r[0] == "pronto" for row in r[3]]
        money = [i for i, h in enumerate(new_hdr) if h in NUMERIC_RECON]
        sh.write("Conciliação", new_hdr, keep + fresh, money_cols=money)
    else:
        sh.write("Conciliação", ["Arquivo de conciliação mensal do iFood (CSV oficial)"],
                 [["Ainda sem arquivo disponível — veja o status na aba Resumo."]])

    res = [
        ["iFood Financeiro %s — Casa Pellegrini" % label, ""],
        ["Última atualização", now],
        ["Ambiente", ambiente],
        ["Período reprocessado nesta execução", "%s a %s" % (d0, d1)],
        ["Situação", "OK" if not errors else "COM ERRO — " + " | ".join(errors)[:400]],
        ["", ""],
        ["Pedidos na aba Vendas", n_vendas],
        ["Lançamentos financeiros", n_ev],
        ["Títulos de repasse", n_tit["Repasses"]],
        ["Antecipações", n_tit["Antecipações"]],
        ["", ""],
        ["Vendido em itens (R$)", "=SUM(Vendas!K2:K)"],
        ["Líquido a receber pelas vendas (R$)", "=SUM(Vendas!V2:V)"],
        ["Líquido pelos lançamentos que impactam o repasse (R$)",
         "=SUMIFS('Lançamentos'!F2:F;'Lançamentos'!G2:G;\"SIM\")"],
        ["Repasses pagos (R$)", "=SUMIFS(Repasses!F2:F;Repasses!G2:G;\"Pago\")"],
        ["", ""],
        ["Arquivo de conciliação", "Status"],
    ]
    for (mid, comp), r in sorted(recon.items(), key=lambda x: x[0][1]):
        res.append(["Competência " + comp, {"pronto": "Pronto — %d linhas (%s)" % (len(r[3]), now),
                                               "sem lancamentos": "Sem lançamentos na competência",
                                               "processando": "Em geração — " + r[1],
                                               "erro": "Erro — " + r[1]}.get(r[0], r[0])])
    res += [["", ""],
            ["Como ler", "Vendas = 1 linha por pedido. Lançamentos = cada débito/crédito. "
                         "Só os lançamentos com 'Impacta repasse = SIM' entram no valor que o iFood deposita. "
                         "Repasses = depósitos na conta (bate com o extrato do banco). "
                         "Conciliação = arquivo oficial do iFood (use impacto_no_repasse = SIM). "
                         "Para exportar: Arquivo > Fazer download > CSV."]]
    sh.write("Resumo", res[0], res[1:], money_cols=[], freeze=1)
    print("  planilha %s: vendas=%d lancamentos=%d repasses=%d" % (label, n_vendas, n_ev, n_tit["Repasses"]))


def main():
    env = os.environ
    today = datetime.now(BRT).date()
    if env.get("DATE_FROM", "").strip():
        d0 = date.fromisoformat(env["DATE_FROM"].strip())
        d1 = date.fromisoformat(env["DATE_TO"].strip()) if env.get("DATE_TO", "").strip() else today
    else:
        d0, d1 = today - timedelta(days=int(env.get("DAYS_BACK") or 45)), today
    demo = env.get("IFOOD_DEMO", "").strip().lower() == "true"
    homolog = env.get("IFOOD_HOMOLOGATION", "").strip().lower() == "true"
    now = datetime.now(BRT).strftime("%Y-%m-%d %H:%M")
    comps = [c.strip() for c in env.get("COMPETENCES", "").split(",") if c.strip()]
    if not comps:
        first = today.replace(day=1)
        comps = sorted({(first - timedelta(days=1)).strftime("%Y-%m"), today.strftime("%Y-%m")})
    print("Periodo %s a %s | demo=%s homologacao=%s" % (d0, d1, demo, homolog))

    if demo:
        sales, events, settl, antic, recon, errors = collect_demo()
        d0, d1 = date(2025, 8, 1), date(2025, 8, 31)
    else:
        sales, events, settl, antic, recon, errors = collect_ifood(env, d0, d1, comps, homolog)

    v = [sale_row(s, now) for s in sales]
    e = [event_row(d, x, now) for d, x in events]
    r = [title_row(p, now) for p in settl]
    a = [title_row(p, now) for p in antic]
    creds = env["GOOGLE_SHEETS_CREDS"]

    if demo or homolog:
        # ambiente de teste: tudo numa planilha separada, para nao misturar dado ficticio com o real
        amb = "DEMONSTRAÇÃO (dados fictícios embutidos)" if demo else "TESTE (dados fictícios do iFood — x-request-homologation)"
        sh = Sheet(creds, env["SPREADSHEET_TEST"].strip())
        write_book(sh, "TESTE", amb, now, d0, d1, v, e, r, a, recon, errors)
    else:
        # producao: uma planilha por ano
        books = json.loads(env.get("SPREADSHEETS") or "{}")
        vy, ey = by_year(v, [0]), by_year(e, [0])
        ry, ay = by_year(r, [2, 1]), by_year(a, [2, 1])
        years = set(vy) | set(ey) | set(ry) | set(ay) | {c[:4] for (_, c) in recon} | {str(y) for y in range(d0.year, d1.year + 1)}
        for y in sorted(x for x in years if x):
            has_data = bool(vy.get(y) or ey.get(y) or ry.get(y) or ay.get(y))
            if y not in books:
                if has_data:
                    errors.append("sem planilha cadastrada para o ano %s" % y)
                    print("  ERRO: sem planilha para %s (criar e incluir em SPREADSHEETS)" % y)
                continue
            rec_y = {k: val for k, val in recon.items() if k[1][:4] == y}
            a0, a1 = max(d0, date(int(y), 1, 1)), min(d1, date(int(y), 12, 31))
            write_book(Sheet(creds, books[y]), y, "PRODUÇÃO", now, a0, a1,
                       vy.get(y, []), ey.get(y, []), ry.get(y, []), ay.get(y, []), rec_y, errors)
    print("Concluido. Erros: %d" % len(errors))
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
