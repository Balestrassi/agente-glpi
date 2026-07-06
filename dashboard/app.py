"""
Dashboard em tempo real — Suporte RM
Serve o HTML e um endpoint /api/stats com dados do GLPI (cache 5 min).
"""

import os
import sys
import json
import html
import time
import threading
import requests
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, jsonify, render_template, request

# Sob gunicorn, stdout nao vai para um terminal e o Python usa buffer cheio
# em vez de por linha — os print() de diagnostico (ex: "[historico] erro")
# ficavam presos no buffer e nunca apareciam no log do Render.
sys.stdout.reconfigure(line_buffering=True)

try:
    import gspread
    from google.oauth2.service_account import Credentials
    _GSPREAD_OK = True
except ImportError:
    _GSPREAD_OK = False

BRASILIA = timezone(timedelta(hours=-3))

app = Flask(__name__)

GLPI_URL         = os.environ.get("GLPI_URL",                "https://servicedesk.a7on.ai")
APP_TOKEN        = os.environ.get("GLPI_APP_TOKEN",          "")
USER_TOKEN       = os.environ.get("GLPI_USER_TOKEN",         "")
GLPI_PROFILE_ID  = os.environ.get("GLPI_PROFILE_ID",         "4")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN",          "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID",        "")
GOOGLE_JSON      = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
SHEET_ID         = os.environ.get("SPREADSHEET_ID",          "")
SHEET_NAME       = "Histórico Semanal"
CACHE_TTL        = 300  # 5 minutos

# Token de acesso ao dashboard (defina DASHBOARD_TOKEN no Render).
# Sem ele definido o dashboard fica aberto — compatibilidade retroativa.
DASHBOARD_TOKEN  = os.environ.get("DASHBOARD_TOKEN", "")

# Radar SLA: chamados abertos sem movimentação há mais de X horas
SLA_RADAR_HORAS  = 12

_hist_cache = {"ts": 0, "data": None}
_cache      = {"ts": 0, "data": None}
_tec_cache  = {"ts": 0, "data": None}
_hist_lock  = threading.Lock()
_cache_lock = threading.Lock()
_tec_lock   = threading.Lock()
_nomes_cache = {}
MAX_DIAS_PERIODO = 180  # limite para /api/periodo evitar consultas gigantes
PERIODOS_DIA = ["Madrugada", "Manhã", "Tarde", "Noite"]
DIAS_SEMANA  = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]


@app.before_request
def _exigir_token():
    """Protege as rotas de API com DASHBOARD_TOKEN.
    O webhook do Telegram tem validação própria (chat autorizado)."""
    if not DASHBOARD_TOKEN:
        return
    if not (request.path.startswith("/api/") or request.path == "/setup-webhook"):
        return
    enviado = (request.headers.get("X-Dashboard-Token")
               or request.args.get("token") or "")
    if enviado != DASHBOARD_TOKEN:
        return jsonify({"error": "unauthorized"}), 401


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
    tok = r.json()["session_token"]
    # Forca o perfil Super-Admin: a conta usada aqui tambem tem um perfil
    # restrito por entidade (Tegma-only). Sem isso, initSession pode herdar
    # o ultimo perfil ativo da conta e esconder chamados de outras entidades.
    requests.post(f"{GLPI_URL}/apirest.php/changeActiveProfile", headers=_h(tok),
                  json={"profiles_id": GLPI_PROFILE_ID}, timeout=15)
    return tok


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
    entidade = html.unescape((ticket.get("entities_id") or "").strip())
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


def periodo_do_dia(hora):
    if hora < 6:  return 0  # Madrugada
    if hora < 12: return 1  # Manhã
    if hora < 18: return 2  # Tarde
    return 3                 # Noite


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
        parados         = []   # radar SLA: abertos sem movimentação
        por_prod        = {}
        por_tipo        = {}
        sem_total       = 0
        sem_resol       = 0
        heatmap         = [[0] * 4 for _ in range(7)]  # [dia_semana][periodo]
        agora_naive     = agora.replace(tzinfo=None)

        for t in tickets:
            if not eh_recebemai(t):
                continue

            nome     = html.unescape(t.get("name", "") or "")
            status   = t.get("status")
            criacao  = t.get("date_creation") or t.get("date", "")
            data_mod = t.get("date_mod", "")
            tid      = t.get("id")

            prod = produto(t)
            tp   = tipo(nome)
            cli  = produto(t)

            if criacao >= inicio_semana:
                sem_total += 1
                if status in (5, 6):
                    sem_resol += 1

            try:
                dt_criacao = datetime.strptime(criacao[:19], "%Y-%m-%d %H:%M:%S")
                heatmap[dt_criacao.weekday()][periodo_do_dia(dt_criacao.hour)] += 1
            except Exception:
                pass

            if status in (1, 2, 4):
                abertos += 1
                por_prod[prod] = por_prod.get(prod, 0) + 1
                por_tipo[tp]   = por_tipo.get(tp, 0) + 1
                # Radar SLA: horas desde a última movimentação (qualquer update)
                try:
                    ref = data_mod or criacao
                    dt_ref = datetime.strptime(ref[:19], "%Y-%m-%d %H:%M:%S")
                    horas_parado = (agora_naive - dt_ref).total_seconds() / 3600
                except Exception:
                    horas_parado = 0
                if horas_parado >= SLA_RADAR_HORAS:
                    parados.append({
                        "id":          tid,
                        "titulo":      (nome[:50] + "…") if len(nome) > 50 else nome,
                        "cliente":     cli,
                        "status_nome": {1: "Novo", 2: "Em andamento", 4: "Pendente"}.get(status, "?"),
                        "status":      status,
                        "horas":       round(horas_parado),
                    })
                if criacao < limite_crit:
                    criticos.append({
                        "id":          tid,
                        "titulo":      (nome[:50] + "…") if len(nome) > 50 else nome,
                        "status_nome": {1: "Novo", 2: "Em andamento", 4: "Pendente"}.get(status, "?"),
                        "status":      status,
                        "dias":        dias_aberto(criacao),
                        "cliente":     cli,
                    })
            elif status in (5, 6):
                data_resol = t.get("solvedate") or data_mod
                if data_resol.startswith(hoje_str):
                    resolvidos_hoje += 1

        criticos.sort(key=lambda x: x["dias"], reverse=True)
        parados.sort(key=lambda x: x["horas"], reverse=True)
        total_abertos_prod = sum(por_prod.values()) or 1

        return {
            "abertos":          abertos,
            "resolvidos_hoje":  resolvidos_hoje,
            "criticos_count":   len(criticos),
            "criticos":         criticos[:50],
            "parados":          parados[:30],
            "parados_count":    len(parados),
            "por_prod":         sorted(por_prod.items(), key=lambda x: x[1], reverse=True),
            "por_tipo":         sorted(por_tipo.items(), key=lambda x: x[1], reverse=True),
            "total_abertos_prod": total_abertos_prod,
            "sem_total":        sem_total,
            "sem_resol":        sem_resol,
            "taxa_semana":      round(sem_resol / sem_total * 100) if sem_total else 0,
            "atualizado":       agora.strftime("%d/%m/%Y %H:%M") + " BRT",
            "heatmap":          heatmap,
            "heatmap_dias":     DIAS_SEMANA,
            "heatmap_periodos": PERIODOS_DIA,
        }
    finally:
        close_session(tok)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/ticket/<int:tid>")
def ticket_detail(tid):
    import re
    try:
        tok = get_session()
        try:
            t = requests.get(f"{GLPI_URL}/apirest.php/Ticket/{tid}",
                             headers=_h(tok), params={"expand_dropdowns": True}, timeout=15)
            if t.status_code != 200:
                return jsonify({"error": "Não encontrado"}), 404
            t = t.json()
            if not isinstance(t, dict) or "id" not in t:
                return jsonify({"error": "Não encontrado"}), 404

            fups_raw = api_get(f"Ticket/{tid}/ITILFollowup", tok,
                               {"range": "0-5", "order": "DESC", "sort": "date_creation"})
            status_map = {1:"Novo", 2:"Em andamento", 4:"Pendente", 5:"Resolvido", 6:"Fechado"}
            urg_map    = {1:"Muito Baixa", 2:"Baixa", 3:"Média", 4:"Alta", 5:"Muito Alta"}
            entidade   = html.unescape((t.get("entities_id") or "").strip())
            cliente    = [p.strip() for p in entidade.split(">")][-1] if entidade else "?"
            fups = []
            for f in (fups_raw if isinstance(fups_raw, list) else [])[:4]:
                bruto = html.unescape(f.get("content") or "")
                sem_tags = re.sub(r"<[^>]+>", " ", bruto)
                txt = re.sub(r"\s+", " ", sem_tags).strip()
                txt = (txt[:300] + "…") if len(txt) > 300 else txt
                fups.append({"data": (f.get("date_creation") or "")[:16], "conteudo": txt})
            return jsonify({
                "id":          t["id"],
                "titulo":      html.unescape(t.get("name", "") or ""),
                "cliente":     cliente,
                "status":      status_map.get(t.get("status"), "?"),
                "urgencia":    urg_map.get(t.get("urgency"), "?"),
                "criacao":     (t.get("date_creation") or "")[:16],
                "atualizacao": (t.get("date_mod") or "")[:16],
                "dias":        dias_aberto(t.get("date_creation", "")),
                "followups":   fups,
                "url":         _link(t["id"]),
            })
        finally:
            close_session(tok)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/periodo")
def periodo_customizado():
    inicio = request.args.get("inicio", "")
    fim    = request.args.get("fim", "")
    try:
        dt_inicio = datetime.strptime(inicio, "%Y-%m-%d")
        dt_fim    = datetime.strptime(fim, "%Y-%m-%d")
    except Exception:
        return jsonify({"error": "Datas inválidas. Use formato YYYY-MM-DD"}), 400

    if dt_fim < dt_inicio:
        return jsonify({"error": "Data final anterior à data inicial"}), 400
    if (dt_fim - dt_inicio).days > MAX_DIAS_PERIODO:
        return jsonify({"error": f"Período máximo permitido: {MAX_DIAS_PERIODO} dias"}), 400

    data_ini = f"{inicio} 00:00:00"
    data_fim = f"{fim} 23:59:59"

    tok = get_session()
    try:
        total, resol, por_prod = 0, 0, {}
        offset = 0
        while True:
            lote = api_get("Ticket", tok, {
                "expand_dropdowns": True,
                "range": f"{offset}-{offset+99}",
                "sort": "date_creation", "order": "DESC",
            })
            if not lote:
                break
            parou = False
            for t in lote:
                dc = t.get("date_creation") or ""
                if dc > data_fim:
                    continue
                if dc < data_ini:
                    parou = True
                    break
                if not eh_recebemai(t):
                    continue
                total += 1
                if t.get("status") in (5, 6):
                    resol += 1
                prod = produto(t)
                por_prod[prod] = por_prod.get(prod, 0) + 1
            if parou or len(lote) < 100:
                break
            offset += 100

        return jsonify({
            "inicio": inicio, "fim": fim,
            "total": total, "resolvidos": resol,
            "taxa": round(resol / total * 100) if total else 0,
            "por_prod": sorted(por_prod.items(), key=lambda x: x[1], reverse=True),
        })
    finally:
        close_session(tok)


def _nome_usuario(tok, uid):
    if uid in _nomes_cache:
        return _nomes_cache[uid]
    try:
        u = requests.get(f"{GLPI_URL}/apirest.php/User/{uid}", headers=_h(tok), timeout=10).json()
        nome = f"{u.get('firstname','')} {u.get('realname','')}".strip() or u.get("name", f"Usuário {uid}")
    except Exception:
        nome = f"Usuário {uid}"
    _nomes_cache[uid] = nome
    return nome


def _tecnico_do_ticket(tok, tid):
    try:
        r = requests.get(f"{GLPI_URL}/apirest.php/Ticket/{tid}/Ticket_User",
                         headers=_h(tok), timeout=10)
        if r.status_code not in (200, 206):
            return None
        for assoc in r.json():
            if assoc.get("type") == 2:  # 2 = técnico atribuído
                return assoc.get("users_id")
        return None
    except Exception:
        return None


def buscar_ranking_tecnicos():
    tok = get_session()
    try:
        tickets = api_get("Ticket", tok, {
            "expand_dropdowns": True, "range": "0-199",
            "sort": "date_creation", "order": "DESC",
        })
        abertos = [t for t in tickets if eh_recebemai(t) and t.get("status") in (1, 2, 4)]

        # Cada thread usa sua própria sessão do GLPI — evitar reuso concorrente
        # de um único Session-Token, que pode gerar respostas inconsistentes.
        N_WORKERS = min(6, len(abertos)) or 1
        sessoes = [get_session() for _ in range(N_WORKERS)]
        try:
            def buscar_com_sessao(idx_t):
                idx, t = idx_t
                sessao_thread = sessoes[idx % N_WORKERS]
                return _tecnico_do_ticket(sessao_thread, t["id"])

            with ThreadPoolExecutor(max_workers=N_WORKERS) as ex:
                uids = list(ex.map(buscar_com_sessao, enumerate(abertos)))
        finally:
            for s in sessoes:
                close_session(s)

        resultados = {}
        for t, uid in zip(abertos, uids):
            if not uid:
                chave = "_sem_tecnico"
            else:
                chave = uid
            if chave not in resultados:
                resultados[chave] = {"uid": uid, "tickets": []}
            resultados[chave]["tickets"].append({
                "id": t["id"], "titulo": (t.get("name") or "")[:60],
                "dias": dias_aberto(t.get("date_creation", "")),
            })

        ranking = []
        for chave, info in resultados.items():
            nome = "Sem técnico atribuído" if chave == "_sem_tecnico" else _nome_usuario(tok, info["uid"])
            ranking.append({
                "nome": nome,
                "count": len(info["tickets"]),
                "tickets": sorted(info["tickets"], key=lambda x: x["dias"], reverse=True),
            })
        ranking.sort(key=lambda x: x["count"], reverse=True)
        return ranking
    finally:
        close_session(tok)


@app.route("/api/tecnicos")
def tecnicos():
    global _tec_cache
    now = time.time()
    if _tec_cache["data"] is None or now - _tec_cache["ts"] > 600:  # cache 10 min
        with _tec_lock:
            # Reconfere dentro do lock: outra requisição pode ter recalculado enquanto esperávamos
            if _tec_cache["data"] is None or now - _tec_cache["ts"] > 600:
                try:
                    _tec_cache["data"] = buscar_ranking_tecnicos()
                    _tec_cache["ts"]   = time.time()
                except Exception as e:
                    if _tec_cache["data"] is None:
                        return jsonify({"error": str(e)}), 500
    return jsonify(_tec_cache["data"])


@app.route("/api/stats")
def stats():
    global _cache
    now = time.time()
    if _cache["data"] is None or now - _cache["ts"] > CACHE_TTL:
        with _cache_lock:
            if _cache["data"] is None or now - _cache["ts"] > CACHE_TTL:
                try:
                    _cache["data"] = calcular()
                    _cache["ts"]   = time.time()
                except Exception as e:
                    if not _cache["data"]:
                        return jsonify({"error": str(e)}), 500
    return jsonify(_cache["data"])


# ── Histórico Google Sheets ───────────────────────────────────────────

def buscar_historico():
    if not _GSPREAD_OK or not GOOGLE_JSON or not SHEET_ID:
        print(f"[historico] pre-condicao falhou: _GSPREAD_OK={_GSPREAD_OK} "
              f"GOOGLE_JSON_len={len(GOOGLE_JSON)} SHEET_ID={'set' if SHEET_ID else 'vazio'}")
        return None
    try:
        creds = Credentials.from_service_account_info(
            json.loads(GOOGLE_JSON),
            scopes=["https://www.googleapis.com/auth/spreadsheets",
                    "https://www.googleapis.com/auth/drive"],
        )
        gc   = gspread.authorize(creds)
        ws   = gc.open_by_key(SHEET_ID).worksheet(SHEET_NAME)
        rows = ws.get_all_values()
        if len(rows) < 2:
            return []
        # header: Data | Período | Total | Resolvidos | Taxa% | T.Técnico(h) | T.Ciclo(h) | Em Aberto
        result = []
        for row in rows[1:]:
            try:
                result.append({
                    "data":      row[0] if len(row) > 0 else "",
                    "periodo":   row[1] if len(row) > 1 else "",
                    "total":     int(float(row[2]))   if len(row) > 2 and row[2]  else 0,
                    "resolvidos":int(float(row[3]))   if len(row) > 3 and row[3]  else 0,
                    "taxa":      float(row[4])         if len(row) > 4 and row[4]  else 0,
                    "t_tecnico": float(row[5])         if len(row) > 5 and row[5]  else 0,
                    "t_ciclo":   float(row[6])         if len(row) > 6 and row[6]  else 0,
                    "em_aberto": int(float(row[7]))   if len(row) > 7 and row[7]  else 0,
                })
            except Exception:
                continue
        return result[-12:]  # últimas 12 semanas
    except Exception as e:
        print(f"[historico] erro: {e}")
        return None  # sinaliza falha — distinto de "sem linhas na planilha"


@app.route("/api/historico")
def historico():
    global _hist_cache
    now = time.time()
    if _hist_cache["data"] is None or now - _hist_cache["ts"] > 3600:  # cache 1h
        with _hist_lock:
            if _hist_cache["data"] is None or now - _hist_cache["ts"] > 3600:
                novo = buscar_historico()
                if novo is not None:
                    _hist_cache["data"] = novo
                    _hist_cache["ts"]   = time.time()
                elif _hist_cache["data"] is None:
                    # nunca teve sucesso: nao trava por 1h, tenta de novo no proximo request
                    _hist_cache["data"] = []
    return jsonify(_hist_cache["data"])


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
        entidade   = html.unescape((t.get("entities_id") or "").strip())
        cli        = [p.strip() for p in entidade.split(">")][-1] if entidade else "?"
        titulo     = html.unescape(t.get("name") or "Sem título").replace("_","\\_").replace("*","\\*")
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
            titulo = html.unescape(t.get("name") or "")[:50]
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


def bot_tendencias(chat_id):
    dados = buscar_historico()
    if not dados:
        _reply(chat_id, "📊 Ainda não há histórico suficiente\\. O registro começa todo domingo às 22h\\.")
        return
    n = len(dados)
    ultima = dados[-1]
    linhas = [f"📈 *Tendências — últimas {n} semana(s)*", ""]

    # Tabela resumo
    for d in dados[-4:]:
        seta_taxa = ""
        if dados.index(d) > 0:
            ant = dados[dados.index(d) - 1]["taxa"]
            diff = d["taxa"] - ant
            seta_taxa = f" {'↑' if diff > 0 else '↓'}{abs(diff):.0f}pp" if diff != 0 else " ↔"
        linhas.append(f"*{d['periodo']}*")
        linhas.append(f"  Total: {d['total']} | Resolvidos: {d['resolvidos']} | Taxa: {d['taxa']:.0f}%{seta_taxa}")

    # Análise de tendência
    if n >= 2:
        linhas.append("")
        taxas = [d["taxa"] for d in dados]
        media = sum(taxas) / len(taxas)
        tendencia_taxa = taxas[-1] - taxas[0]
        linhas.append(f"*Análise:*")
        linhas.append(f"  Média de resolução: {media:.0f}%")
        if tendencia_taxa > 5:
            linhas.append(f"  Tendência: ↑ Melhorando \\({tendencia_taxa:+.0f}pp desde a 1ª semana\\)")
        elif tendencia_taxa < -5:
            linhas.append(f"  Tendência: ↓ Atenção \\({tendencia_taxa:+.0f}pp desde a 1ª semana\\)")
        else:
            linhas.append(f"  Tendência: ↔ Estável")

        melhor = max(dados, key=lambda x: x["taxa"])
        linhas.append(f"  Melhor semana: {melhor['periodo']} \\({melhor['taxa']:.0f}%\\)")

    linhas.append("")
    linhas.append("_Ver gráficos: suporte\\-rm\\-dashboard\\.onrender\\.com_")
    _reply(chat_id, "\n".join(linhas))


def bot_relatorio(chat_id, arg):
    """Resumo do período sob demanda: /relatorio [dd/mm] [dd/mm].
    Sem argumentos: últimos 7 dias."""
    import re as _re
    agora = datetime.now(BRASILIA).replace(tzinfo=None)
    datas = _re.findall(r"(\d{1,2})/(\d{1,2})(?:/(\d{4}))?", arg or "")
    try:
        if len(datas) >= 2:
            def _dt(d):
                dia, mes, ano = d
                return datetime(int(ano) if ano else agora.year, int(mes), int(dia))
            dt_ini, dt_fim = _dt(datas[0]), _dt(datas[1])
        else:
            dt_fim = agora
            dt_ini = agora - timedelta(days=7)
    except ValueError:
        _reply(chat_id, "⚠️ Datas inválidas\\. Use: `/relatorio 01/07 05/07`")
        return
    if dt_fim < dt_ini or (dt_fim - dt_ini).days > MAX_DIAS_PERIODO:
        _reply(chat_id, f"⚠️ Período inválido \\(máx {MAX_DIAS_PERIODO} dias\\)\\.")
        return

    data_ini = dt_ini.strftime("%Y-%m-%d 00:00:00")
    data_fim = dt_fim.strftime("%Y-%m-%d 23:59:59")

    tok = get_session()
    try:
        total, resol, por_prod, tempos_h = 0, 0, {}, []
        offset = 0
        while True:
            lote = api_get("Ticket", tok, {
                "expand_dropdowns": True,
                "range": f"{offset}-{offset+99}",
                "sort": "date_creation", "order": "DESC",
            })
            if not lote:
                break
            parou = False
            for t in lote:
                dc = t.get("date_creation") or ""
                if dc > data_fim:
                    continue
                if dc < data_ini:
                    parou = True
                    break
                if not eh_recebemai(t):
                    continue
                total += 1
                prod = produto(t)
                por_prod[prod] = por_prod.get(prod, 0) + 1
                if t.get("status") in (5, 6):
                    resol += 1
                    solve = t.get("solvedate") or t.get("date_mod") or ""
                    try:
                        d1 = datetime.strptime(dc[:19],    "%Y-%m-%d %H:%M:%S")
                        d2 = datetime.strptime(solve[:19], "%Y-%m-%d %H:%M:%S")
                        if d2 > d1:
                            tempos_h.append((d2 - d1).total_seconds() / 3600)
                    except Exception:
                        pass
            if parou or len(lote) < 100:
                break
            offset += 100

        periodo_txt = f"{dt_ini.strftime('%d/%m')} a {dt_fim.strftime('%d/%m/%Y')}"
        if not total:
            _reply(chat_id, f"📭 Nenhum chamado Recebe Mais entre {periodo_txt}\\.")
            return
        taxa = round(resol / total * 100)
        linhas = [
            f"📊 *Relatório — {periodo_txt}*".replace("/", "\\/"), "",
            f"📋 Total de chamados: *{total}*",
            f"✅ Resolvidos/Fechados: *{resol}* \\(*{taxa}%*\\)",
            f"🕐 Ainda em aberto: *{total - resol}*",
        ]
        if tempos_h:
            tempos_h.sort()
            media   = sum(tempos_h) / len(tempos_h)
            mediana = tempos_h[len(tempos_h) // 2]
            def _fmt(h):
                return f"{int(h*60)} min" if h < 1 else (f"{h:.1f} h" if h < 48 else f"{h/24:.1f} dias")
            linhas.append(f"⏱ Tempo até solução \\(corrido\\): média *{_fmt(media)}* · mediana *{_fmt(mediana)}*")
        if por_prod:
            linhas.append("")
            linhas.append("*Por cliente:*")
            for nome_p, qtd in sorted(por_prod.items(), key=lambda x: -x[1])[:6]:
                linhas.append(f"  • {nome_p}: {qtd}")
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
        "`/tendencias` — análise de tendência das últimas semanas",
        "`/relatorio 01/07 05/07` — resumo do período \\(sem datas: últimos 7 dias\\)",
        "`/ajuda` — esta mensagem",
    ]
    _reply(chat_id, "\n".join(linhas))


def _processar_comando(chat_id, text):
    parts = text.strip().split(maxsplit=1)
    cmd   = parts[0].lower().split("@")[0]
    arg   = parts[1].strip() if len(parts) > 1 else ""
    cmds  = {
        "/start":      bot_ajuda,      "/ajuda": bot_ajuda, "/help": bot_ajuda,
        "/resumo":     bot_resumo,
        "/abertos":    bot_abertos,
        "/criticos":   bot_criticos,
        "/tendencias": bot_tendencias,
    }
    if cmd in cmds:
        cmds[cmd](chat_id)
    elif cmd == "/chamado":
        bot_chamado(chat_id, arg)
    elif cmd == "/cliente":
        bot_cliente(chat_id, arg)
    elif cmd == "/relatorio":
        bot_relatorio(chat_id, arg)
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
