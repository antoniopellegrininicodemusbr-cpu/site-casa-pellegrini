#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Geo-grid de ranking local da Casa Pellegrini — alternativa caseira ao Local Falcon.

O QUE FAZ:
  Pra cada ponto de uma grade (grid) ao redor da casa e pra cada termo de busca,
  consulta a Google Places API (Text Search) "como se" o usuário estivesse naquele
  ponto, acha a posição da Casa Pellegrini no resultado, e monta um mapa de calor de
  ranking + posição média. Roda TUDO num processo só e cospe um resumo compacto ->
  pouquíssimos tokens pro Claude (ele só lê o resumo final).

USO:
  export PLACES_API_KEY="AIza..."        # ou passe --key
  python3 geo_grid.py                    # usa os defaults abaixo
  python3 geo_grid.py --rows 7 --cols 7 --spacing-km 0.8 --topn 20
  python3 geo_grid.py --out resultado.json

LIMITAÇÃO DE REDE (IMPORTANTE):
  Este script chama maps.googleapis.com. O sandbox bash do Cowork NÃO alcança esse
  domínio (egress allowlist). Logo, NÃO roda direto do sandbox. Rode por:
    (a) GitHub Action (runner tem internet aberta) — padrão recomendado, igual sync Fidelizi; OU
    (b) adicionar maps.googleapis.com ao allowlist em Configurações -> Capacidades; OU
    (c) na máquina do Antonio (Windows) com Python instalado.

DEPENDÊNCIAS: só a stdlib (urllib). Sem pip.
"""

import argparse
import json
import math
import os
import sys
import time
import unicodedata
import urllib.parse
import urllib.request

# ------------------------------------------------------------------ CONFIG DEFAULT
# Centro aproximado: Casa Pellegrini, Rua Treze de Maio, 184, Centro, Petropolis-RJ.
# TODO: refinar com a lat/lng exata do Google Business Profile (hoje e aproximada).
CENTER_LAT = -22.50468
CENTER_LNG = -43.181446

# Termos prioritarios (Tier 1). Manter em sincronia com marketing/palavras-chave-alvo.md.
DEFAULT_KEYWORDS = [
    "restaurante centro petropolis",
    "hamburguer petropolis",
    "happy hour petropolis",
    "bar petropolis centro",
    "almoco petropolis centro",
]

# Como reconhecer a Casa Pellegrini no resultado (match por substring, sem acento).
TARGET_MATCH = "pellegrini"

PLACES_TEXTSEARCH = "https://maps.googleapis.com/maps/api/place/textsearch/json"


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn").lower()


def km_to_deg(dlat_km, dlng_km, lat):
    dlat = dlat_km / 111.32
    dlng = dlng_km / (111.32 * math.cos(math.radians(lat)))
    return dlat, dlng


def build_grid(center_lat, center_lng, rows, cols, spacing_km):
    pts = []
    # centraliza a grade: offsets de -(n-1)/2 ate +(n-1)/2
    for r in range(rows):
        row = []
        for c in range(cols):
            off_r = (r - (rows - 1) / 2) * spacing_km   # norte/sul
            off_c = (c - (cols - 1) / 2) * spacing_km   # leste/oeste
            dlat, dlng = km_to_deg(off_r, off_c, center_lat)
            # off_r positivo = mais ao norte (lat maior); linha 0 = mais ao norte -> inverte
            lat = center_lat - dlat
            lng = center_lng + dlng
            row.append((lat, lng))
        pts.append(row)
    return pts


def textsearch(query, lat, lng, radius_m, key, timeout=20):
    params = {
        "query": query,
        "location": f"{lat},{lng}",
        "radius": str(radius_m),
        "key": key,
        "language": "pt-BR",
        "region": "br",
    }
    url = PLACES_TEXTSEARCH + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data


def rank_of_target(results, target_norm, topn):
    for i, place in enumerate(results[:topn], start=1):
        name = strip_accents(place.get("name", ""))
        if target_norm in name:
            return i
    return None  # nao apareceu no topN


def run(key, center_lat, center_lng, rows, cols, spacing_km, topn, keywords, sleep_s):
    radius_m = int(spacing_km * 1000)  # raio ~ espacamento, vies local por ponto
    grid = build_grid(center_lat, center_lng, rows, cols, spacing_km)
    out = {
        "center": [center_lat, center_lng],
        "grid": {"rows": rows, "cols": cols, "spacing_km": spacing_km},
        "topn": topn,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "keywords": {},
        "errors": [],
    }

    for kw in keywords:
        target_norm = strip_accents(TARGET_MATCH)
        ranks_grid = []      # matriz de ranks (None = fora do topN)
        found_ranks = []     # so os encontrados, pra media
        top3 = 0
        total_pts = rows * cols
        for r in range(rows):
            row_ranks = []
            for c in range(cols):
                lat, lng = grid[r][c]
                try:
                    data = textsearch(kw, lat, lng, radius_m, key)
                    status = data.get("status")
                    if status not in ("OK", "ZERO_RESULTS"):
                        out["errors"].append({"kw": kw, "pt": [r, c], "status": status,
                                               "msg": data.get("error_message", "")})
                        row_ranks.append(None)
                        continue
                    rank = rank_of_target(data.get("results", []), target_norm, topn)
                    row_ranks.append(rank)
                    if rank is not None:
                        found_ranks.append(rank)
                        if rank <= 3:
                            top3 += 1
                except Exception as e:  # noqa
                    out["errors"].append({"kw": kw, "pt": [r, c], "status": "EXCEPTION", "msg": str(e)})
                    row_ranks.append(None)
                if sleep_s:
                    time.sleep(sleep_s)
            ranks_grid.append(row_ranks)

        avg = round(sum(found_ranks) / len(found_ranks), 2) if found_ranks else None
        out["keywords"][kw] = {
            "grid_ranks": ranks_grid,
            "avg_rank": avg,
            "found_pts": len(found_ranks),
            "total_pts": total_pts,
            "top3_pts": top3,
            "top3_share": round(top3 / total_pts, 2),
        }
    return out


def render_heatmap(out):
    """Resumo legivel pro log (e pro Claude ler com poucos tokens)."""
    lines = []
    lines.append(f"GEO-GRID Casa Pellegrini | centro {out['center']} | "
                 f"{out['grid']['rows']}x{out['grid']['cols']} @ {out['grid']['spacing_km']}km | top{out['topn']}")
    lines.append("(numero = posicao da Casa Pellegrini naquele ponto; '·' = fora do top%d)" % out["topn"])
    for kw, d in out["keywords"].items():
        lines.append("")
        lines.append(f"== {kw} ==")
        for row in d["grid_ranks"]:
            cells = []
            for v in row:
                cells.append("·· " if v is None else f"{v:>2} ")
            lines.append("  " + "".join(cells))
        lines.append(f"  -> posicao media: {d['avg_rank']} | aparece em {d['found_pts']}/{d['total_pts']} pontos | "
                     f"top3 em {d['top3_pts']} ({int(d['top3_share']*100)}%)")
    if out["errors"]:
        lines.append("")
        lines.append(f"ERROS: {len(out['errors'])} (1o: {out['errors'][0]})")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", default=os.environ.get("PLACES_API_KEY"))
    ap.add_argument("--lat", type=float, default=CENTER_LAT)
    ap.add_argument("--lng", type=float, default=CENTER_LNG)
    ap.add_argument("--rows", type=int, default=7)
    ap.add_argument("--cols", type=int, default=7)
    ap.add_argument("--spacing-km", type=float, default=0.8)
    ap.add_argument("--topn", type=int, default=20)
    ap.add_argument("--sleep", type=float, default=0.1, help="pausa entre chamadas (s)")
    ap.add_argument("--keywords", nargs="*", default=DEFAULT_KEYWORDS)
    ap.add_argument("--out", default=None, help="caminho pra salvar JSON cru")
    args = ap.parse_args()

    if not args.key:
        print("ERRO: defina PLACES_API_KEY no ambiente ou passe --key", file=sys.stderr)
        sys.exit(2)

    out = run(args.key, args.lat, args.lng, args.rows, args.cols, args.spacing_km,
              args.topn, args.keywords, args.sleep)

    print(render_heatmap(out))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"\nJSON salvo em {args.out}")


if __name__ == "__main__":
    main()
