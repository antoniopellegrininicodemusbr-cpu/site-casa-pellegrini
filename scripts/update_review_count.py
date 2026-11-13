#!/usr/bin/env python3
"""Atualiza sozinho a nota e o numero de avaliacoes do Google nas paginas do site (Schema + texto visivel).

Pedido do Antonio (24/05/2026, aprovado de novo em 13/11/2026): o site mostrava "4,7 · 5.746 avaliacoes" fixo.
Fonte: API do Perfil da Empresa (mesmas credenciais do robo que responde avaliacoes).
Roda toda segunda pelo GitHub Actions (.github/workflows/review-count.yml). So grava se o numero mudou.

Travas: nota entre 3,5 e 5; total nao pode cair nem subir mais de 50% de uma vez; JSON-LD tem que continuar valido
e o arquivo tem que terminar em </html>. Se qualquer trava falhar, nao grava nada.
"""
import json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gbp_review_reply import ACCOUNT, LOCATION, http, gbp_token

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGES = ["index.html", "cardapio.html", "historia.html", "clube-pellegrini.html"]
DRY = os.environ.get("DRY_RUN", "false").lower() == "true"

def br(n):            # 5746 -> "5.746"
    return f"{n:,}".replace(",", ".")

def atual():
    s = open(os.path.join(ROOT, "index.html"), encoding="utf-8").read()
    c = re.search(r'"reviewCount":\s*"(\d+)"', s); r = re.search(r'"ratingValue":\s*"([\d.]+)"', s)
    if not c or not r: sys.exit("ERRO: nao achei reviewCount/ratingValue no index.html")
    return int(c.group(1)), r.group(1)

def aplicar(s, old_n, new_n, old_r, new_r):
    s = re.sub(r'("reviewCount":\s*")%d(")' % old_n, r"\g<1>%d\2" % new_n, s)
    s = s.replace(br(old_n), br(new_n))                      # "5.746 avaliacoes" visivel e nas respostas
    cem_old, cem_new = old_n // 100 * 100, new_n // 100 * 100  # "mais de 5.700 avaliacoes"
    if cem_new != cem_old:
        s = s.replace("mais de " + br(cem_old), "mais de " + br(cem_new))
    if new_r != old_r:
        s = re.sub(r'("ratingValue":\s*")%s(")' % re.escape(old_r), r"\g<1>%s\2" % new_r, s)
        a, b = old_r.replace(".", ","), new_r.replace(".", ",")
        s = re.sub(r"(?<![\d,.])%s(?![\d])" % re.escape(a), b, s)   # "4,7" (nao pega preco tipo 24,70)
    return s

def valido(s):
    if not s.rstrip().endswith("</html>"): return False
    try:
        for b in re.findall(r'<script type="application/ld\+json">(.*?)</script>', s, re.S): json.loads(b)
    except Exception:
        return False
    return True

def main():
    old_n, old_r = atual()
    tok = gbp_token()
    tok = tok["access_token"] if isinstance(tok, dict) else tok
    d = http(f"https://mybusiness.googleapis.com/v4/accounts/{ACCOUNT}/locations/{LOCATION}/reviews?pageSize=1",
             headers={"Authorization": "Bearer " + tok})
    new_n = int(d.get("totalReviewCount") or 0)
    new_r = "%.1f" % float(d.get("averageRating") or 0)
    print(f"Site hoje: {old_r} / {old_n}  |  Google: {new_r} / {new_n}")
    if not (3.5 <= float(new_r) <= 5.0) or new_n < old_n or new_n > old_n * 1.5:
        sys.exit("ERRO: numero do Google fora do esperado - nao gravei nada.")
    if new_n == old_n and new_r == old_r:
        print("Nada mudou."); return
    novos = {}
    for p in PAGES:
        f = os.path.join(ROOT, p); s = open(f, encoding="utf-8").read()
        t = aplicar(s, old_n, new_n, old_r, new_r)
        if not valido(t): sys.exit(f"ERRO: {p} ficaria invalido - nao gravei nada.")
        if t != s: novos[f] = t
    # llms.txt (resumo pra buscadores de IA): so a frase "mais de X avaliacoes"
    f = os.path.join(ROOT, "llms.txt")
    if os.path.exists(f):
        s = open(f, encoding="utf-8").read()
        t = s.replace("mais de " + br(old_n // 100 * 100), "mais de " + br(new_n // 100 * 100))
        if new_r != old_r: t = t.replace(old_r.replace(".", ",") + " estrelas", new_r.replace(".", ",") + " estrelas")
        if t != s: novos[f] = t
    print("Paginas alteradas:", [os.path.basename(f) for f in novos])
    if DRY:
        print("DRY_RUN: nao gravei."); return
    for f, t in novos.items():
        open(f, "w", encoding="utf-8").write(t)

if __name__ == "__main__":
    main()
