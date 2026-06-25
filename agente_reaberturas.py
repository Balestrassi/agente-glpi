"""
Agente de Detecção de Reaberturas — GLPI
Monitora chamados Recebe Mais que foram resolvidos/fechados e voltaram a aberto.
Envia alerta no Telegram ao detectar reabertura.
Roda a cada 30 minutos via GitHub Actions.

Cache: reaberturas_cache.json → {tid_str: "data_em_que_foi_visto_resolvido"}
Quando reabre: remove do cache (evita re-alertar se fechar e abrir de novo).
"""

import os
import json
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path

GLPI_URL         = os.environ.get("GLPI_URL",        "https://servicedesk.a7on.ai")
APP_TOKEN        = os.environ.get("GLPI_APP_TOKEN",  "")
USER_TOKEN       = os.environ.get("GLPI_USER_TOKEN", "")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN",  "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID","")

BRASILIA   = timezone(timedelta(hours=-3))
CACHE_FILE = Path(__file__).parent / "reaberturas_cache.json"

STATUS_NOME = {1: "Novo", 2: "Em andamento", 4: "Pendente", 5: "Resolvido", 6: "Fechado"}

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
    return r.json()["session_token"]

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
def eh_recebemai(ticket):
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

# ── Cache ─────────────────────────────────────────────────────────────
def carregar_cache() -> dict:
    """Retorna {tid_str: "data_resolucao_iso"}."""
    if CACHE_FILE.exists():
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    return {}

def salvar_cache(cache: dict):
    CACHE_FILE.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
    )

# ── Telegram ──────────────────────────────────────────────────────────
def enviar_telegram(texto: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("  [!] Telegram não configurado.")
        return
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": texto,
              "parse_mode": "Markdown", "disable_web_page_preview": True},
        timeout=15,
    ).raise_for_status()

# ── Main ──────────────────────────────────────────────────────────────
def main():
    agora = datetime.now(BRASILIA).replace(tzinfo=None)
    print(f"[{agora.strftime('%H:%M')} BRT] Agente Reaberturas iniciado")

    cache = carregar_cache()

    tok = get_session()
    try:
        # Busca os 200 tickets Recebe Mais mais recentemente modificados
        # (qualquer status — resoluções e reaberturas alteram date_mod)
        tickets_rm = []
        for offset in (0, 100):
            lote = api_get("Ticket", tok, {
                "expand_dropdowns": True,
                "range": f"{offset}-{offset+99}",
                "sort": "date_mod",
                "order": "DESC",
            })
            if not isinstance(lote, list) or not lote:
                break
            tickets_rm.extend([t for t in lote if eh_recebemai(t)])
            if len(lote) < 100:
                break

        print(f"  Tickets RM nos 200 mais recentes: {len(tickets_rm)}")

        reabertos  = []
        cache_novo = dict(cache)

        for t in tickets_rm:
            tid     = str(t["id"])
            status  = t.get("status")
            titulo  = t.get("name", "")
            cli     = cliente(t)
            dm      = t.get("date_mod") or ""

            if status in (5, 6):
                # Está resolvido/fechado — registra/atualiza no cache
                cache_novo[tid] = dm
            elif status in (1, 2, 4):
                if tid in cache_novo:
                    # ERA resolvido/fechado, agora está aberto → reabertura!
                    data_resolucao = cache_novo.pop(tid)
                    try:
                        dr = datetime.strptime(data_resolucao[:10], "%Y-%m-%d")
                        data_fmt = dr.strftime("%d/%m/%Y")
                    except Exception:
                        data_fmt = data_resolucao[:10]

                    reabertos.append({
                        "id":     t["id"],
                        "titulo": titulo,
                        "cli":    cli,
                        "status": STATUS_NOME.get(status, str(status)),
                        "data_resolucao": data_fmt,
                    })

        salvar_cache(cache_novo)

        if not reabertos:
            print("  Nenhuma reabertura detectada.")
            return

        # Monta alerta Telegram
        linhas = [f"🔄 *{len(reabertos)} chamado(s) reaberto(s)\\!*\n"]
        for c in reabertos:
            link = f"{GLPI_URL}/front/ticket.form.php?id={c['id']}"
            titulo_esc = c['titulo'].replace('_', '\\_').replace('*', '\\*')
            linhas.append(f"• [\\#{c['id']}]({link}) — *{c['cli']}*")
            linhas.append(f"  {titulo_esc[:55]}")
            linhas.append(f"  _Resolvido em {c['data_resolucao']} → Status: {c['status']}_")

        linhas.append(f"\n_Verificado em {agora.strftime('%d/%m/%Y %H:%M')} BRT_")
        enviar_telegram("\n".join(linhas))
        print(f"  {len(reabertos)} reabertura(s) alertada(s).")

    finally:
        close_session(tok)

    print(f"[{datetime.now(BRASILIA).replace(tzinfo=None).strftime('%H:%M')}] Concluído")

if __name__ == "__main__":
    main()
