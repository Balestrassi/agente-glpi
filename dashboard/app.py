"""
Dashboard em tempo real — Suporte RM
Serve o HTML e um endpoint /api/stats com dados do GLPI (cache 5 min).
"""

import os
import time
import requests
from datetime import datetime, timedelta
from flask import Flask, jsonify, render_template

app = Flask(__name__)

GLPI_URL   = os.environ.get("GLPI_URL",        "https://servicedesk.a7on.ai")
APP_TOKEN  = os.environ.get("GLPI_APP_TOKEN",  "")
USER_TOKEN = os.environ.get("GLPI_USER_TOKEN", "")
CACHE_TTL  = 300  # 5 minutos

_cache = {"ts": 0, "data": None}


def _h(tok=None):
    h = {"App-Token": APP_TOKEN}
    if tok:
        h["Session-Token"] = tok
    return h


def get_session():
    r = requests.get(
        f"{GLPI_URL}/apirest.php/initSession",
        headers={"Authorization": f"user_token {USER_TOKEN}", **_h()},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["session_token"]


def close_session(tok):
    requests.get(f"{GLPI_URL}/apirest.php/killSession", headers=_h(tok), timeout=10)


def api_get(path, tok, params=None):
    r = requests.get(
        f"{GLPI_URL}/apirest.php/{path}",
        headers=_h(tok),
        params=params,
        timeout=30,
    )
    if r.status_code in (200, 206):
        d = r.json()
        return d if isinstance(d, list) else []
    return []


def produto(titulo):
    tl = titulo.lower()
    if "messianica" in tl or "messiânica" in tl:
        return "Messiânica"
    if "manserv" in tl:
        return "Manserv"
    if "recebemai" in tl:
        return "RecebeMais"
    return "Outros"


def tipo(titulo):
    tl = titulo.lower()
    if "erro" in tl:
        return "Erro"
    if "servico" in tl or "serviço" in tl:
        return "Solicitação"
    if "wildlife" in tl:
        return "Wildlife"
    if "duvida" in tl or "dúvida" in tl:
        return "Dúvida"
    if "melhoria" in tl:
        return "Melhoria"
    return "Outros"


def dias_aberto(criacao_str):
    try:
        dt = datetime.strptime(criacao_str[:19], "%Y-%m-%d %H:%M:%S")
        return (datetime.utcnow() - dt).days
    except Exception:
        return 0


def calcular():
    tok = get_session()
    try:
        tickets = api_get("Ticket", tok, {
            "expand_dropdowns": True,
            "range": "0-499",
            "sort": "date_creation",
            "order": "DESC",
        })

        agora         = datetime.utcnow()
        hoje_str      = agora.strftime("%Y-%m-%d")
        seg           = agora - timedelta(days=agora.weekday())
        inicio_semana = seg.strftime("%Y-%m-%d 00:00:00")
        limite_crit   = (agora - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")

        abertos         = 0
        resolvidos_hoje = 0
        criticos        = []
        por_prod        = {}
        por_tipo        = {}
        sem_total       = 0
        sem_resol       = 0

        for t in tickets:
            nome     = t.get("name", "")
            status   = t.get("status")
            criacao  = t.get("date_creation") or t.get("date", "")
            data_mod = t.get("date_mod", "")
            tid      = t.get("id")

            prod = produto(nome)
            tp   = tipo(nome)

            if criacao >= inicio_semana:
                sem_total += 1
                if status in (5, 6):
                    sem_resol += 1

            if status in (1, 2, 4):
                abertos += 1
                por_prod[prod] = por_prod.get(prod, 0) + 1
                por_tipo[tp]   = por_tipo.get(tp, 0) + 1
                if criacao < limite_crit:
                    criticos.append({
                        "id":          tid,
                        "titulo":      (nome[:50] + "…") if len(nome) > 50 else nome,
                        "status_nome": {1: "Novo", 2: "Em andamento", 4: "Pendente"}.get(status, "?"),
                        "status":      status,
                        "dias":        dias_aberto(criacao),
                    })
            elif status in (5, 6):
                if data_mod.startswith(hoje_str):
                    resolvidos_hoje += 1

        criticos.sort(key=lambda x: x["dias"], reverse=True)
        total_abertos_prod = sum(por_prod.values()) or 1

        return {
            "abertos":          abertos,
            "resolvidos_hoje":  resolvidos_hoje,
            "criticos_count":   len(criticos),
            "criticos":         criticos[:8],
            "por_prod":         sorted(por_prod.items(), key=lambda x: x[1], reverse=True),
            "por_tipo":         sorted(por_tipo.items(), key=lambda x: x[1], reverse=True),
            "total_abertos_prod": total_abertos_prod,
            "sem_total":        sem_total,
            "sem_resol":        sem_resol,
            "taxa_semana":      round(sem_resol / sem_total * 100) if sem_total else 0,
            "atualizado":       agora.strftime("%d/%m/%Y %H:%M") + " UTC",
        }
    finally:
        close_session(tok)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/stats")
def stats():
    global _cache
    now = time.time()
    if _cache["data"] is None or now - _cache["ts"] > CACHE_TTL:
        try:
            _cache["data"] = calcular()
            _cache["ts"]   = now
        except Exception as e:
            if _cache["data"]:
                return jsonify(_cache["data"])
            return jsonify({"error": str(e)}), 500
    return jsonify(_cache["data"])


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
