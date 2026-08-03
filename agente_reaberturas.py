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
import html
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path

GLPI_URL         = os.environ.get("GLPI_URL",        "https://servicedesk.a7on.ai")
APP_TOKEN        = os.environ.get("GLPI_APP_TOKEN",  "")
USER_TOKEN       = os.environ.get("GLPI_USER_TOKEN", "")
GLPI_PROFILE_ID  = os.environ.get("GLPI_PROFILE_ID", "4")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN",  "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID","")
# Opcional: registra reaberturas no Google Sheets (vira métrica dos relatórios)
GOOGLE_JSON      = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
SHEET_ID         = os.environ.get("SPREADSHEET_ID", "")

BRASILIA   = timezone(timedelta(hours=-3))
CACHE_FILE = Path(__file__).parent / "reaberturas_cache.json"

STATUS_NOME = {1: "Novo", 2: "Em andamento", 4: "Pendente", 5: "Resolvido", 6: "Fechado"}

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
    entidade = html.unescape((ticket.get("entities_id") or "").strip())
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
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": texto,
                  "parse_mode": "Markdown", "disable_web_page_preview": True},
            timeout=15,
        ).raise_for_status()
    except Exception as e:
        print(f"  [!] Erro ao enviar Telegram: {e}")

# ── Registro em planilha (métrica de reabertura) ──────────────────────
def registrar_planilha(reabertos, agora):
    """Grava as reaberturas na aba 'Reaberturas' do Google Sheets, para que
    a taxa de reabertura vire métrica nos relatórios. Sem credenciais ou
    sem gspread instalado, apenas pula — o alerta Telegram não depende disso."""
    if not GOOGLE_JSON or not SHEET_ID:
        return
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        print("  [!] gspread não instalado — registro em planilha pulado.")
        return
    try:
        creds = Credentials.from_service_account_info(
            json.loads(GOOGLE_JSON),
            scopes=["https://www.googleapis.com/auth/spreadsheets",
                    "https://www.googleapis.com/auth/drive"],
        )
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(SHEET_ID)
        try:
            ws = sh.worksheet("Reaberturas")
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title="Reaberturas", rows=1000, cols=6)
            ws.append_row(["Data", "Ticket", "Cliente", "Título", "Resolvido em"])
        for c in reabertos:
            ws.append_row([
                agora.strftime("%Y-%m-%d %H:%M"), c["id"], c["cli"],
                c["titulo"][:80], c["data_resolucao"],
            ])
        print(f"  {len(reabertos)} reabertura(s) registrada(s) na planilha.")
    except Exception as e:
        print(f"  [!] Falha ao registrar na planilha: {e}")

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
            titulo  = html.unescape(t.get("name", "") or "")
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
        linhas = [f"🔄 *{len(reabertos)} chamado(s) reaberto(s)!*\n"]
        for c in reabertos:
            link = f"{GLPI_URL}/front/ticket.form.php?id={c['id']}"
            titulo_esc = _esc_md(c['titulo'])
            linhas.append(f"• [#{c['id']}]({link}) — *{_esc_md(c['cli'])}*")
            linhas.append(f"  {titulo_esc[:55]}")
            linhas.append(f"  _Resolvido em {c['data_resolucao']} → Status: {c['status']}_")

        linhas.append(f"\n_Verificado em {agora.strftime('%d/%m/%Y %H:%M')} BRT_")
        enviar_telegram("\n".join(linhas))
        print(f"  {len(reabertos)} reabertura(s) alertada(s).")
        registrar_planilha(reabertos, agora)

    finally:
        close_session(tok)

    print(f"[{datetime.now(BRASILIA).replace(tzinfo=None).strftime('%H:%M')}] Concluído")

if __name__ == "__main__":
    main()
