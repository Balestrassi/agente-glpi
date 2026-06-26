"""
Dashboard em tempo real — Suporte RM
Serve o HTML e um endpoint /api/stats com dados do GLPI (cache 5 min).
"""

import os
import time
import threading
import requests
from datetime import datetime, timedelta, timezone
from flask import Flask, jsonify, render_template, request

BRASILIA = timezone(timedelta(hours=-3))

app = Flask(__name__)

GLPI_URL         = os.environ.get("GLPI_URL",        "https://servicedesk.a7on.ai")
APP_TOKEN        = os.environ.get("GLPI_APP_TOKEN",  "")
USER_TOKEN       = os.environ.get("GLPI_USER_TOKEN", "")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN",  "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID","")
CACHE_TTL        = 300  # 5 minutos

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


def eh_recebemai(ticket):
    entidade  = (ticket.get("entities_id")       or "").lower()
    categoria = (ticket.get("itilcategories_id") or "").lower()
    return "recebe mais" in entidade or "recebemai" in categoria or "recebe mais" in categoria


def produto(ticket):
    """Extrai o nome do cliente a partir do caminho da entidade."""
    entidade = (ticket.get("entities_id") or "").strip()
    if not entidade:
        return "Sem entidade"
    partes = [p.strip() for p in entidade.split(">")]
    cliente = partes[-1]
    if cliente.lower() == "recebe mais":
        return "Recebe Mais (Geral)"
    return cliente


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
        # GLPI retorna datas em BRT — comparar contra BRT
        return (datetime.now(BRASILIA).replace(tzinfo=None) - dt).days
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

        agora         = datetime.now(BRASILIA)
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
            if not eh_recebemai(t):
                continue

            nome     = t.get("name", "")
            status   = t.get("status")
            criacao  = t.get("date_creation") or t.get("date", "")
            data_mod = t.get("date_mod", "")
            tid      = t.get("id")

            prod = produto(t)
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
            "atualizado":       agora.strftime("%d/%m/%Y %H:%M") + " BRT",
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


# ── Telegram Bot ─────────────────────────────────────────────────────

def _reply(chat_id, text):
    if not TELEGRAM_TOKEN:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text,
                  "parse_mode": "Markdown", "disable_web_page_preview": True},
            timeout=10,
        )
    except Exception:
        pass


def _dados_cache():
    """Retorna dados do cache do dashboard, recalculando se necessário."""
    if _cache["data"] and time.time() - _cache["ts"] < CACHE_TTL:
        return _cache["data"]
    try:
        return calcular()
    except Exception:
        return {}


def _link(tid):
    return f"{GLPI_URL}/front/ticket.form.php?id={tid}"


def bot_resumo(chat_id):
    d = _dados_cache()
    linhas = [
        "📊 *Resumo Recebe Mais — agora*", "",
        f"📋 Em aberto: *{d.get('abertos', 0)}*",
        f"✅ Resolvidos hoje: *{d.get('resolvidos_hoje', 0)}*",
        f"🔴 Críticos \\(\\+3 dias\\): *{d.get('criticos_count', 0)}*",
        f"📈 Taxa semana: *{d.get('taxa_semana', 0)}%*",
        "",
        f"_Atualizado: {d.get('atualizado', '?')}_",
    ]
    _reply(chat_id, "\n".join(linhas))


def bot_criticos(chat_id):
    d        = _dados_cache()
    criticos = d.get("criticos", [])
    n        = d.get("criticos_count", len(criticos))
    if not criticos:
        _reply(chat_id, "✅ Nenhum chamado crítico no momento\\.")
        return
    linhas = [f"🔴 *{n} chamado(s) crítico(s) \\(\\+3 dias\\)*", ""]
    for c in criticos:
        titulo = (c.get("titulo") or "")[:50]
        linhas.append(f"• [\\#{c['id']}]({_link(c['id'])}) — {titulo}")
        linhas.append(f"  _{c.get('status_nome', '')} · {c.get('dias', 0)} dias_")
    _reply(chat_id, "\n".join(linhas))


def bot_abertos(chat_id):
    d       = _dados_cache()
    n       = d.get("abertos", 0)
    por_prod = d.get("por_prod", [])
    linhas  = [f"📋 *{n} chamados em aberto — Recebe Mais*", ""]
    if por_prod:
        linhas.append("*Por cliente:*")
        for nome, qtd in por_prod:
            linhas.append(f"  • {nome}: {qtd}")
    _reply(chat_id, "\n".join(linhas))


def bot_chamado(chat_id, arg):
    if not arg.isdigit():
        _reply(chat_id, "⚠️ Use: `/chamado 14412`")
        return
    tid = int(arg)
    tok = get_session()
    try:
        t = api_get(f"Ticket/{tid}", tok, {"expand_dropdowns": True})
        if not isinstance(t, dict) or "id" not in t:
            _reply(chat_id, f"❌ Chamado \\#{tid} não encontrado\\.")
            return
        status_map = {1:"Novo",2:"Em andamento",4:"Pendente",5:"Resolvido",6:"Fechado"}
        urg_map    = {1:"Muito Baixa",2:"Baixa",3:"Média",4:"Alta",5:"Muito Alta"}
        entidade   = (t.get("entities_id") or "").strip()
        cli        = [p.strip() for p in entidade.split(">")][-1] if entidade else "?"
        titulo     = (t.get("name") or "Sem título").replace("_","\\_").replace("*","\\*")
        criacao    = (t.get("date_creation") or "")[:16]
        linhas = [
            f"📌 *Chamado \\#{tid}*",
            f"*Título:* {titulo}",
            f"*Cliente:* {cli}",
            f"*Status:* {status_map.get(t.get('status'), '?')}",
            f"*Urgência:* {urg_map.get(t.get('urgency'), '?')}",
            f"*Aberto em:* {criacao}",
            f"[🔗 Abrir no GLPI]({_link(tid)})",
        ]
        _reply(chat_id, "\n".join(linhas))
    finally:
        close_session(tok)


def bot_cliente(chat_id, arg):
    if not arg:
        _reply(chat_id, "⚠️ Use: `/cliente tegma`")
        return
    tok = get_session()
    try:
        tickets = api_get("Ticket", tok, {
            "expand_dropdowns": True, "range": "0-99",
            "sort": "date_creation", "order": "DESC",
        })
        abertos = [
            t for t in (tickets if isinstance(tickets, list) else [])
            if t.get("status") in (1, 2, 4)
            and arg.lower() in (t.get("entities_id") or "").lower()
        ]
        if not abertos:
            _reply(chat_id, f"✅ Nenhum chamado aberto para *{arg}*\\.")
            return
        linhas = [f"🏢 *{arg.title()} — {len(abertos)} chamado(s) aberto(s)*", ""]
        for t in abertos[:8]:
            titulo = (t.get("name") or "")[:50]
            dc     = (t.get("date_creation") or "")[:10]
            try:
                dias = (datetime.now(BRASILIA).replace(tzinfo=None) -
                        datetime.strptime(dc, "%Y-%m-%d")).days
            except Exception:
                dias = 0
            linhas.append(f"• [\\#{t['id']}]({_link(t['id'])}) — {titulo}")
            linhas.append(f"  _{dias} dia(s) aberto_")
        if len(abertos) > 8:
            linhas.append(f"\n_\\.\\.\\. e mais {len(abertos) - 8} chamado(s)_")
        _reply(chat_id, "\n".join(linhas))
    finally:
        close_session(tok)


def bot_ajuda(chat_id):
    linhas = [
        "🤖 *Bot Suporte Recebe Mais*", "",
        "Comandos disponíveis:",
        "`/resumo` — visão geral rápida",
        "`/abertos` — chamados em aberto por cliente",
        "`/criticos` — chamados críticos \\(\\+3 dias\\)",
        "`/chamado 14412` — detalhes de um chamado",
        "`/cliente tegma` — chamados abertos de um cliente",
        "`/ajuda` — esta mensagem",
    ]
    _reply(chat_id, "\n".join(linhas))


def _processar_comando(chat_id, text):
    parts = text.strip().split(maxsplit=1)
    cmd   = parts[0].lower().split("@")[0]
    arg   = parts[1].strip() if len(parts) > 1 else ""
    cmds  = {
        "/start":   bot_ajuda,   "/ajuda": bot_ajuda,   "/help": bot_ajuda,
        "/resumo":  bot_resumo,
        "/abertos": bot_abertos,
        "/criticos":bot_criticos,
    }
    if cmd in cmds:
        cmds[cmd](chat_id)
    elif cmd == "/chamado":
        bot_chamado(chat_id, arg)
    elif cmd == "/cliente":
        bot_cliente(chat_id, arg)
    else:
        _reply(chat_id, "⚠️ Comando não reconhecido\\. Use `/ajuda` para ver os disponíveis\\.")


@app.route("/telegram/webhook", methods=["POST"])
def telegram_webhook():
    data    = request.get_json(silent=True) or {}
    message = data.get("message") or data.get("edited_message")
    if not message:
        return "", 200
    chat_id = message.get("chat", {}).get("id")
    text    = (message.get("text") or "").strip()
    # Segurança: só responde ao chat autorizado
    if TELEGRAM_CHAT_ID and str(chat_id) != str(TELEGRAM_CHAT_ID):
        return "", 200
    if not text.startswith("/"):
        return "", 200
    # Processa em background — responde imediatamente ao Telegram
    threading.Thread(target=_processar_comando, args=(chat_id, text), daemon=True).start()
    return "", 200


@app.route("/setup-webhook")
def setup_webhook():
    """Acesse uma vez para registrar o webhook no Telegram."""
    if not TELEGRAM_TOKEN:
        return jsonify({"error": "TELEGRAM_TOKEN não configurado"}), 500
    url = request.url_root.rstrip("/") + "/telegram/webhook"
    r   = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook",
        json={"url": url}, timeout=10,
    )
    return jsonify({"webhook_url": url, "telegram_response": r.json()})


# ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
