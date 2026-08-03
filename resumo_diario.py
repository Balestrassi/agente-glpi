"""
Resumo Diário — GLPI Recebe Mais
Envia mensagem resumida no Telegram toda manhã (seg–sex, 8h BRT).
Filtra apenas chamados da entidade Recebe Mais.
"""

import os
import requests
from datetime import datetime, timedelta, timezone

GLPI_URL         = os.environ.get("GLPI_URL",        "https://servicedesk.a7on.ai")
APP_TOKEN        = os.environ.get("GLPI_APP_TOKEN",  "")
USER_TOKEN       = os.environ.get("GLPI_USER_TOKEN", "")
GLPI_PROFILE_ID  = os.environ.get("GLPI_PROFILE_ID", "4")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN",  "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID","")

BRASILIA      = timezone(timedelta(hours=-3))
CRITICO_DIAS  = 3   # chamados abertos há mais de X dias são críticos

DIAS_SEMANA = {0:"Segunda",1:"Terça",2:"Quarta",3:"Quinta",4:"Sexta",5:"Sábado",6:"Domingo"}

def _esc_md(texto):
    """Escapa caracteres especiais do Markdown legado do Telegram (_ * ` [).
    Sem isso, um titulo/cliente com esses caracteres quebra a mensagem
    inteira com 400 Bad Request."""
    texto = texto or ""
    for ch in ("_", "*", "`", "["):
        texto = texto.replace(ch, "\\" + ch)
    return texto

# ── GLPI API ──────────────────────────────────────────────────────────
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
        return r.json()
    return []

# ── Filtro Recebe Mais ────────────────────────────────────────────────
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

def cliente(ticket):
    entidade = (ticket.get("entities_id") or "").strip()
    if not entidade:
        return "?"
    partes = [p.strip() for p in entidade.split(">")]
    ultimo = partes[-1]
    return "Recebe Mais" if ultimo.lower() == "recebe mais" else ultimo

# ── Busca tickets ─────────────────────────────────────────────────────
def buscar_tickets(tok):
    tickets = []
    offset  = 0
    while True:
        data = api_get("Ticket", tok, {
            "expand_dropdowns": True,
            "range": f"{offset}-{offset+199}",
            "sort": "date_creation",
            "order": "DESC",
        })
        if not isinstance(data, list) or not data:
            break
        tickets.extend(data)
        if len(data) < 200:
            break
        offset += 200
    return [t for t in tickets if eh_recebemai(t)]

# ── Telegram ──────────────────────────────────────────────────────────
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
    agora     = datetime.now(BRASILIA)
    hoje_str  = agora.strftime("%Y-%m-%d")
    ontem_str = (agora - timedelta(days=1)).strftime("%Y-%m-%d")
    limite_critico = (agora - timedelta(days=CRITICO_DIAS)).strftime("%Y-%m-%d %H:%M:%S")

    print(f"[{agora.strftime('%H:%M')} BRT] Resumo Diário iniciado")

    tok = get_session()
    try:
        tickets = buscar_tickets(tok)
    finally:
        close_session(tok)

    print(f"  Tickets Recebe Mais encontrados: {len(tickets)}")

    abertos         = []
    abertos_ontem   = []
    criticos        = []

    for t in tickets:
        status  = t.get("status")
        criacao = t.get("date_creation") or ""
        tid     = t.get("id")
        nome    = t.get("name", "Sem título")
        cli     = cliente(t)

        if status in (1, 2, 4):
            abertos.append(t)
            if criacao < limite_critico:
                dias = (agora.replace(tzinfo=None) - datetime.strptime(criacao[:19], "%Y-%m-%d %H:%M:%S")).days
                criticos.append({"id": tid, "nome": nome, "cliente": cli, "dias": dias, "status": status})

        if criacao.startswith(ontem_str):
            abertos_ontem.append(t)

    criticos.sort(key=lambda x: x["dias"], reverse=True)

    # ── Monta mensagem ────────────────────────────────────────────────
    dia_semana = DIAS_SEMANA[agora.weekday()]
    data_fmt   = agora.strftime("%d/%m/%Y")

    linhas = [
        f"🌅 *Bom dia! Resumo — Recebe Mais*",
        f"📅 {dia_semana}, {data_fmt}",
        "",
        f"📋 *Em aberto agora:* {len(abertos)}",
        f"🆕 *Abertos ontem:* {len(abertos_ontem)}",
        f"🔴 *Críticos (+{CRITICO_DIAS} dias):* {len(criticos)}",
    ]

    if criticos:
        linhas.append("")
        linhas.append(f"*Chamados críticos:*")
        STATUS_LABEL = {1: "Novo", 2: "Em andamento", 4: "Pendente"}
        for c in criticos[:8]:
            status_label = STATUS_LABEL.get(c["status"], "?")
            titulo = _esc_md(c["nome"][:45] + ("…" if len(c["nome"]) > 45 else ""))
            link = f"{GLPI_URL}/front/ticket.form.php?id={c['id']}"
            linhas.append(f"• [#{c['id']}]({link}) — {_esc_md(c['cliente'])} | {titulo}")
            linhas.append(f"  _{status_label} · aberto há {c['dias']} dias_")

    linhas.append("")
    linhas.append(f"_Dados via GLPI · {agora.strftime('%H:%M')} BRT_")

    msg = "\n".join(linhas)
    enviar_telegram(msg)
    print(f"  Resumo enviado: {len(abertos)} abertos, {len(abertos_ontem)} ontem, {len(criticos)} críticos")

if __name__ == "__main__":
    main()
