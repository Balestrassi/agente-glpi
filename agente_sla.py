"""
Agente de Alerta SLA — GLPI
Verifica chamados Recebe Mais sem resposta do técnico e envia alertas
escalonados no grupo Telegram:
  • Nível 1 — ⚠️  +24h sem resposta  (alerta único)
  • Nível 2 — 🚨  +48h sem resposta  (alerta único ao escalar)
  • Nível 3 — 🔴  +72h sem resposta  (re-alerta a cada 24h)
"""

import os
import json
import html
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path

GLPI_URL         = os.environ["GLPI_URL"].rstrip("/")
APP_TOKEN        = os.environ.get("GLPI_APP_TOKEN",  "")
USER_TOKEN       = os.environ.get("GLPI_USER_TOKEN", "")
GLPI_PROFILE_ID  = os.environ.get("GLPI_PROFILE_ID", "4")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN",  "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID","")

BRASILIA       = timezone(timedelta(hours=-3))
ALERTADOS_FILE = Path("sla_alertados.json")

NIVEL_HORAS  = {1: 24, 2: 48, 3: 72}
STATUS_NOME  = {1: "Novo", 2: "Em andamento", 4: "Pendente"}
NIVEL_EMOJI  = {1: "⚠️",  2: "🚨",  3: "🔴"}
NIVEL_LABEL  = {
    1: "Sem resposta há +24h",
    2: "URGENTE — Sem resposta há +48h",
    3: "CRÍTICO — Sem resposta há +72h",
}

def _esc_md(texto):
    """Escapa caracteres especiais do Markdown legado do Telegram (_ * ` [).
    Sem isso, um titulo/cliente com esses caracteres quebra a mensagem
    inteira com 400 Bad Request."""
    texto = texto or ""
    for ch in ("_", "*", "`", "["):
        texto = texto.replace(ch, "\\" + ch)
    return texto

# ── Persistência ──────────────────────────────────────────────────────
def carregar_alertados():
    if ALERTADOS_FILE.exists():
        dados = json.loads(ALERTADOS_FILE.read_text(encoding="utf-8"))
        # Migra formato antigo {tid: str} para {tid: {ts, nivel}}
        migrado = {}
        for tid, v in dados.items():
            if isinstance(v, str):
                migrado[tid] = {"ts": v, "nivel": 1}
            else:
                migrado[tid] = v
        return migrado
    return {}

def salvar_alertados(dados: dict):
    ALERTADOS_FILE.write_text(
        json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8"
    )

# ── GLPI API ─────────────────────────────────────────────────────────
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
        headers=_h(tok), params=params, timeout=30,
    )
    if r.status_code in (200, 206):
        d = r.json()
        return d if isinstance(d, list) else d
    return []

# ── Lógica de SLA ─────────────────────────────────────────────────────
def analisar_followups(tok, tid, requester_id, dt_criacao_str):
    """
    Retorna (ult_cli, ult_tec):
    - ult_cli: datetime da última mensagem do CLIENTE (criação do chamado ou
      último followup do solicitante) — é o marco a partir do qual medimos
      se o técnico já respondeu.
    - ult_tec: datetime do último followup de um técnico (ou None se nenhum).
      Mensagens automáticas ("Obrigado pelo seu contato", "Base de Conhecimento")
      são ignoradas — não contam como resposta real do técnico.

    Só devemos alertar sobre SLA quando o cliente deixou uma mensagem sem
    resposta do técnico (ult_cli > ult_tec, ou ult_tec is None).
    Se o técnico já respondeu à última mensagem do cliente (ult_tec >= ult_cli),
    a bola está no cliente — não é uma pendência do técnico.
    """
    try:
        dt_base = datetime.strptime(dt_criacao_str[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        dt_base = datetime.min

    followups = api_get(f"Ticket/{tid}/ITILFollowup", tok)
    if not isinstance(followups, list):
        return dt_base, None

    ult_cli = dt_base
    ult_tec = None

    for f in followups:
        autor   = f.get("users_id")
        content = f.get("content") or ""
        date_str = f.get("date") or f.get("date_creation") or ""
        try:
            dt = datetime.strptime(date_str[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue

        if autor == requester_id:
            if dt > ult_cli:
                ult_cli = dt
        elif (
            "Base de Conhecimento" not in content
            and "Obrigado pelo seu contato" not in content
        ):
            if ult_tec is None or dt > ult_tec:
                ult_tec = dt

    return ult_cli, ult_tec

def horas_desde_brt(dt_str):
    """Calcula horas desde dt_str (data BRT do GLPI) até agora BRT."""
    try:
        dt = datetime.strptime(dt_str[:19], "%Y-%m-%d %H:%M:%S")
        return (datetime.now(BRASILIA).replace(tzinfo=None) - dt).total_seconds() / 3600
    except Exception:
        return 0

def nivel_escalada(horas):
    if horas >= NIVEL_HORAS[3]: return 3
    if horas >= NIVEL_HORAS[2]: return 2
    if horas >= NIVEL_HORAS[1]: return 1
    return 0

def deve_alertar(tid_str, nivel_atual, alertados, agora_utc):
    """
    Alerta se:
    - Nunca alertado antes
    - Nível aumentou (escalada imediata)
    - Nível 3 e já passaram 24h desde o último alerta (re-alerta crítico)
    """
    if tid_str not in alertados:
        return True
    reg = alertados[tid_str]
    nivel_anterior = reg.get("nivel", 0)
    if nivel_atual > nivel_anterior:
        return True
    if nivel_atual == 3:
        ultimo = datetime.fromisoformat(reg["ts"])
        return (agora_utc - ultimo).total_seconds() / 3600 >= 24
    return False

SOLICITANTES_EXCLUIDOS = frozenset({"marlon.james", "vinicius.ariston", "marlon james", "vinicius ariston", "vinícius ariston"})
TICKETS_EXCLUIDOS = {14975}  # chamados abertos por engano para outro time


def eh_recebemai(ticket):
    if ticket.get("id") in TICKETS_EXCLUIDOS:
        return False
    sol = (ticket.get("users_id_recipient") or "").lower()
    if any(s in sol for s in SOLICITANTES_EXCLUIDOS):
        return False
    entidade  = (ticket.get("entities_id")       or "").lower()
    categoria = (ticket.get("itilcategories_id") or "").lower()
    return "recebe mais" in entidade or "recebemai" in categoria or "recebe mais" in categoria

def get_requester_id_numerico(tok, tid):
    t = api_get(f"Ticket/{tid}", tok)
    return t.get("users_id_recipient") if isinstance(t, dict) else None

def buscar_abertos(tok):
    tickets = []
    for status in (1, 2, 4):
        resultado = api_get("Ticket", tok, {
            "expand_dropdowns": True,
            "range": "0-199",
            "sort": "date_creation",
            "order": "ASC",
            "searchText[status]": status,
        })
        if isinstance(resultado, list):
            tickets.extend(resultado)
    return tickets

# ── Telegram ─────────────────────────────────────────────────────────
def enviar_telegram(texto):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("  [!] Telegram não configurado.")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": texto, "parse_mode": "Markdown",
                  "disable_web_page_preview": True},
            timeout=15,
        ).raise_for_status()
    except Exception as e:
        print(f"  [!] Erro ao enviar Telegram: {e}")

# ── Main ──────────────────────────────────────────────────────────────
def main():
    agora_brt = datetime.now(BRASILIA).replace(tzinfo=None)
    agora_utc = datetime.utcnow()
    print(f"[{agora_brt.strftime('%H:%M')} BRT] Agente SLA iniciado")

    alertados = carregar_alertados()

    tok = get_session()
    try:
        tickets_abertos = buscar_abertos(tok)
        print(f"  Chamados abertos encontrados: {len(tickets_abertos)}")

        por_nivel      = {1: [], 2: [], 3: []}
        ids_abertos    = set()

        for t in tickets_abertos:
            tid    = t.get("id")
            nome   = html.unescape(t.get("name", "") or "")
            status = t.get("status")
            criacao = t.get("date_creation") or t.get("date", "")

            ids_abertos.add(str(tid))

            if not eh_recebemai(t):
                continue

            horas_aberto = horas_desde_brt(criacao)
            if horas_aberto < NIVEL_HORAS[1]:
                continue

            req_id = get_requester_id_numerico(tok, tid)
            ult_cli, ult_tec = analisar_followups(tok, tid, req_id, criacao)

            if ult_tec is not None and ult_tec >= ult_cli:
                # Técnico já respondeu à última mensagem do cliente → sem pendência de SLA
                alertados.pop(str(tid), None)
                continue

            # Horas desde a última mensagem do cliente sem resposta do técnico
            horas_ref = (agora_brt - ult_cli).total_seconds() / 3600

            nivel = nivel_escalada(horas_ref)
            if nivel == 0 or not deve_alertar(str(tid), nivel, alertados, agora_utc):
                continue

            dias  = int(horas_ref // 24)
            horas = int(horas_ref % 24)
            tempo_str = f"{dias}d {horas}h" if dias > 0 else f"{int(horas_ref)}h"

            por_nivel[nivel].append({
                "id":     tid,
                "nome":   nome,
                "status": STATUS_NOME.get(status, str(status)),
                "tempo":  tempo_str,
            })
            alertados[str(tid)] = {"ts": agora_utc.isoformat(), "nivel": nivel}

        # Remove tickets resolvidos/fechados do controle
        for tid_str in list(alertados.keys()):
            if tid_str not in ids_abertos:
                alertados.pop(tid_str, None)

        salvar_alertados(alertados)

        total = sum(len(v) for v in por_nivel.values())
        if total == 0:
            print("  Nenhum chamado requer alerta.")
            return

        # Envia do nível mais grave para o menos grave
        for nivel in (3, 2, 1):
            chamados = por_nivel[nivel]
            if not chamados:
                continue

            linhas = [f"{NIVEL_EMOJI[nivel]} *{len(chamados)} chamado(s) — {NIVEL_LABEL[nivel]}*\n"]
            for c in chamados:
                link = f"{GLPI_URL}/front/ticket.form.php?id={c['id']}"
                linhas.append(f"• [#{c['id']}]({link}) — {_esc_md(c['nome'])}")
                linhas.append(f"  _{c['status']} · sem resposta há {c['tempo']}_")

            linhas.append(f"\n_Verificado em {agora_brt.strftime('%d/%m/%Y %H:%M')} BRT_")
            enviar_telegram("\n".join(linhas))
            print(f"  Nível {nivel} ({NIVEL_EMOJI[nivel]}): {len(chamados)} chamado(s)")

    finally:
        close_session(tok)

    print(f"[{datetime.now(BRASILIA).replace(tzinfo=None).strftime('%H:%M')}] Concluído\n")

if __name__ == "__main__":
    main()
