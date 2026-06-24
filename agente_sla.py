"""
Agente de Alerta SLA — GLPI
Verifica chamados abertos sem resposta do técnico há mais de 24h
e envia alerta no grupo Telegram.
"""

import os
import json
import requests
from datetime import datetime, timedelta
from pathlib import Path

GLPI_URL         = os.environ.get("GLPI_URL",        "https://servicedesk.a7on.ai")
APP_TOKEN        = os.environ.get("GLPI_APP_TOKEN",  "")
USER_TOKEN       = os.environ.get("GLPI_USER_TOKEN", "")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN",  "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID","")

HORAS_SLA      = 24   # horas sem resposta para alertar
HORAS_RENOTIF  = 24   # re-alertar a cada X horas se ainda sem resposta
ALERTADOS_FILE = Path("sla_alertados.json")

STATUS_NOME = {1: "Novo", 2: "Em andamento", 4: "Pendente"}

# ── Persistência ──────────────────────────────────────────────────────
def carregar_alertados():
    if ALERTADOS_FILE.exists():
        return json.loads(ALERTADOS_FILE.read_text(encoding="utf-8"))
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
        return d if isinstance(d, list) else d
    return []

# ── Lógica de SLA ─────────────────────────────────────────────────────
def ultimo_comentario_tecnico(tok, tid, requester_id):
    """
    Retorna o datetime do último followup feito pelo técnico (não solicitante, não bot).
    Retorna None se não houver nenhum.
    """
    followups = api_get(f"Ticket/{tid}/ITILFollowup", tok)
    if not isinstance(followups, list) or not followups:
        return None

    tecnicos = [
        f for f in followups
        if f.get("users_id") != requester_id
        and "Base de Conhecimento" not in (f.get("content") or "")
        and "Obrigado pelo seu contato" not in (f.get("content") or "")
    ]
    if not tecnicos:
        return None

    ult = tecnicos[-1].get("date") or tecnicos[-1].get("date_creation")
    try:
        return datetime.strptime(ult[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None

def horas_desde(dt_str):
    try:
        dt = datetime.strptime(dt_str[:19], "%Y-%m-%d %H:%M:%S")
        return (datetime.utcnow() - dt).total_seconds() / 3600
    except Exception:
        return 0

def eh_recebemai(ticket):
    """Verifica se o chamado pertence ao produto Recebe Mais pela entidade ou categoria."""
    entidade  = (ticket.get("entities_id")       or "").lower()
    categoria = (ticket.get("itilcategories_id") or "").lower()
    return "recebe mais" in entidade or "recebemai" in categoria or "recebe mais" in categoria

def get_requester_id_numerico(tok, tid):
    """Busca o ID numérico do solicitante (sem expand_dropdowns)."""
    t = api_get(f"Ticket/{tid}", tok)
    return t.get("users_id_recipient") if isinstance(t, dict) else None

def buscar_abertos(tok):
    """Busca chamados abertos com campos expandidos para filtragem por entidade/categoria."""
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
    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": texto,
            "parse_mode": "Markdown",
        },
        timeout=15,
    )
    r.raise_for_status()

# ── Main ──────────────────────────────────────────────────────────────
def main():
    agora = datetime.utcnow()
    print(f"[{agora.strftime('%H:%M:%S')}] Agente SLA iniciado")

    alertados = carregar_alertados()
    limite_criacao = (agora - timedelta(hours=HORAS_SLA)).strftime("%Y-%m-%d %H:%M:%S")

    tok = get_session()
    try:
        tickets_abertos = buscar_abertos(tok)
        print(f"  Chamados abertos encontrados: {len(tickets_abertos)}")

        precisam_alerta = []
        ids_abertos_agora = set()

        for t in tickets_abertos:
            tid     = t.get("id")
            nome    = t.get("name", "")
            status  = t.get("status")
            criacao = t.get("date_creation") or t.get("date", "")

            ids_abertos_agora.add(str(tid))

            # Só monitora chamados Recebe Mais (por entidade ou categoria)
            if not eh_recebemai(t):
                continue

            # Só verifica tickets com mais de HORAS_SLA horas
            if criacao >= limite_criacao:
                continue

            horas_aberto = horas_desde(criacao)

            # Busca ID numérico do solicitante para comparar com followups
            req_id = get_requester_id_numerico(tok, tid)

            # Verifica último comentário do técnico
            ult_tec = ultimo_comentario_tecnico(tok, tid, req_id)

            sem_resposta = False
            if ult_tec is None:
                # Nunca teve resposta do técnico
                sem_resposta = True
            else:
                horas_sem = (agora - ult_tec).total_seconds() / 3600
                if horas_sem >= HORAS_SLA:
                    sem_resposta = True

            if not sem_resposta:
                # Tem resposta recente — remove do dict de alertados se estava lá
                alertados.pop(str(tid), None)
                continue

            # Verifica se já alertamos recentemente
            ultimo_alerta = alertados.get(str(tid))
            if ultimo_alerta:
                horas_desde_alerta = (agora - datetime.fromisoformat(ultimo_alerta)).total_seconds() / 3600
                if horas_desde_alerta < HORAS_RENOTIF:
                    continue  # já alertamos nas últimas 24h

            # Precisa alertar
            dias = int(horas_aberto // 24)
            horas_rest = int(horas_aberto % 24)
            tempo_str = f"{dias}d {horas_rest}h" if dias > 0 else f"{int(horas_aberto)}h"

            precisam_alerta.append({
                "id":     tid,
                "nome":   nome,
                "status": STATUS_NOME.get(status, str(status)),
                "tempo":  tempo_str,
            })
            alertados[str(tid)] = agora.isoformat()

        # Remove do dict tickets que já foram resolvidos/fechados
        for tid_str in list(alertados.keys()):
            if tid_str not in ids_abertos_agora:
                alertados.pop(tid_str, None)

        salvar_alertados(alertados)

        if not precisam_alerta:
            print("  Nenhum chamado sem resposta há +24h.")
            return

        # Monta mensagem Telegram
        linhas = [f"⚠️ *{len(precisam_alerta)} chamado(s) sem resposta há +{HORAS_SLA}h*\n"]
        for c in precisam_alerta:
            linhas.append(f"• *#{c['id']}* — {c['nome']}")
            linhas.append(f"  _{c['status']} · aberto há {c['tempo']}_")
        linhas.append(f"\n_Verificado em {agora.strftime('%d/%m/%Y %H:%M')} UTC_")

        msg = "\n".join(linhas)
        enviar_telegram(msg)
        print(f"  Alerta enviado: {len(precisam_alerta)} chamado(s)")
        for c in precisam_alerta:
            print(f"    #{c['id']} — {c['nome']} ({c['tempo']})")

    finally:
        close_session(tok)

    print(f"[{datetime.utcnow().strftime('%H:%M:%S')}] Concluído\n")

if __name__ == "__main__":
    main()
