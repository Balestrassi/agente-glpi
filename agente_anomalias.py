"""
Agente Detector de Anomalias de Volume — GLPI
Roda a cada 15 minutos via GitHub Actions.

Quando o mesmo cliente abre vários chamados em uma janela curta, quase sempre
é UM incidente sistêmico (integração fora, erro em lote) — não N problemas
isolados. Este agente detecta a rajada e envia UM alerta consolidado ao
Telegram, permitindo resposta de incidente em vez de triagem um a um.

Estado em anomalias_alertadas.json (persistido via actions/cache) evita
re-alertar a mesma rajada.
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

BRASILIA = timezone(timedelta(hours=-3))

JANELA_MINUTOS     = 60   # janela de detecção da rajada
LIMIAR_CHAMADOS    = 5    # >= N chamados do mesmo cliente na janela = anomalia
SUPRESSAO_HORAS    = 3    # não re-alertar o mesmo cliente por N horas
SOLICITANTES_EXCLUIDOS = frozenset({"marlon.james", "vinicius.ariston", "marlon james", "vinicius ariston", "vinícius ariston"})
TICKETS_EXCLUIDOS = {14975}  # chamados abertos por engano para outro time

ALERTADOS_FILE = Path("anomalias_alertadas.json")

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
    r = requests.get(f"{GLPI_URL}/apirest.php/{path}",
                     headers=_h(tok), params=params, timeout=30)
    if r.status_code in (200, 206):
        d = r.json()
        return d if isinstance(d, list) else []
    return []


# ── Classificação ─────────────────────────────────────────────────────
def cliente_de(ticket):
    entidade = html.unescape((ticket.get("entities_id") or "").strip())
    if not entidade:
        return "Sem entidade"
    return [p.strip() for p in entidade.split(">")][-1]


# ── Estado ────────────────────────────────────────────────────────────
def carregar_alertados() -> dict:
    if ALERTADOS_FILE.exists():
        try:
            return json.loads(ALERTADOS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def salvar_alertados(d: dict):
    # poda entradas com mais de 24h — irrelevantes para a supressão
    limite = (datetime.now(BRASILIA) - timedelta(hours=24)).isoformat()
    d = {k: v for k, v in d.items() if v >= limite}
    ALERTADOS_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2),
                              encoding="utf-8")


# ── Telegram ──────────────────────────────────────────────────────────
def enviar_telegram(texto: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("  [!] Telegram não configurado.")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": texto,
                  "parse_mode": "Markdown", "disable_web_page_preview": True},
            timeout=10,
        )
    except Exception as e:
        print(f"  [!] Erro ao enviar Telegram: {e}")


# ── Main ──────────────────────────────────────────────────────────────
def main():
    agora = datetime.now(BRASILIA).replace(tzinfo=None)
    print(f"[{agora.strftime('%H:%M:%S')}] Detector de anomalias — "
          f"janela {JANELA_MINUTOS} min, limiar {LIMIAR_CHAMADOS}")

    limite = (agora - timedelta(minutes=JANELA_MINUTOS)).strftime("%Y-%m-%d %H:%M:%S")
    alertados = carregar_alertados()

    tok = get_session()
    try:
        tickets = api_get("Ticket", tok, {
            "expand_dropdowns": True,
            "range": "0-99",
            "sort": "date_creation",
            "order": "DESC",
        })
    finally:
        close_session(tok)

    recentes = [
        t for t in tickets
        if (t.get("date_creation") or "") >= limite
        and t.get("id") not in TICKETS_EXCLUIDOS
        and not any(s in (t.get("users_id_recipient") or "").lower() for s in SOLICITANTES_EXCLUIDOS)
    ]
    print(f"  Chamados na janela: {len(recentes)}")

    por_cliente = {}
    for t in recentes:
        por_cliente.setdefault(cliente_de(t), []).append(t)

    for cliente, ts in sorted(por_cliente.items(), key=lambda x: -len(x[1])):
        if len(ts) < LIMIAR_CHAMADOS:
            continue

        # Supressão: já alertamos este cliente há menos de SUPRESSAO_HORAS?
        ultimo = alertados.get(cliente, "")
        corte  = (datetime.now(BRASILIA) - timedelta(hours=SUPRESSAO_HORAS)).isoformat()
        if ultimo >= corte:
            print(f"  {cliente}: {len(ts)} chamados, mas já alertado — suprimido")
            continue

        print(f"  ANOMALIA: {cliente} — {len(ts)} chamados em {JANELA_MINUTOS} min")
        linhas = [
            f"🚨 *Possível incidente em massa — {_esc_md(cliente)}*", "",
            f"*{len(ts)} chamados* abertos nos últimos {JANELA_MINUTOS} minutos.",
            "Provavelmente é UM incidente sistêmico — avalie antes de triar um a um.", "",
        ]
        for t in ts[:6]:
            titulo = _esc_md(html.unescape(t.get("name") or "")[:45])
            hora   = (t.get("date_creation") or "")[11:16]
            linhas.append(
                f"• [#{t['id']}]({GLPI_URL}/front/ticket.form.php?id={t['id']})"
                f" {hora} — {titulo}"
            )
        if len(ts) > 6:
            linhas.append(f"_... e mais {len(ts) - 6} chamado(s)_")
        enviar_telegram("\n".join(linhas))

        alertados[cliente] = datetime.now(BRASILIA).isoformat()

    salvar_alertados(alertados)
    print("  Concluído.")


if __name__ == "__main__":
    main()
