"""
Histórico de Métricas — Google Sheets
Registra uma linha semanal na planilha com os KPIs do período.
Roda todo domingo às 22h BRT (antes do relatório de segunda).

Colunas da planilha:
  Data | Período | Total | Resolvidos | Taxa% | T.Técnico(h) | T.Ciclo(h) | Em Aberto

Setup necessário (uma vez):
  1. Crie um projeto no Google Cloud Console
  2. Ative a Google Sheets API
  3. Crie uma Service Account e baixe o JSON de credenciais
  4. Compartilhe a planilha com o e-mail da Service Account (Editor)
  5. Adicione aos secrets do GitHub:
       GOOGLE_CREDENTIALS_JSON → conteúdo do arquivo JSON da Service Account
       SPREADSHEET_ID          → ID da planilha (da URL do Google Sheets)
"""

import os
import json
import requests
from datetime import datetime, timedelta, timezone

import gspread
from google.oauth2.service_account import Credentials

# ── Credenciais ───────────────────────────────────────────────────────
GLPI_URL    = os.environ.get("GLPI_URL",               "https://servicedesk.a7on.ai")
APP_TOKEN   = os.environ.get("GLPI_APP_TOKEN",         "")
USER_TOKEN  = os.environ.get("GLPI_USER_TOKEN",        "")
GOOGLE_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON","")
SHEET_ID    = os.environ.get("SPREADSHEET_ID",         "")

BRASILIA    = timezone(timedelta(hours=-3))
SHEET_NAME  = "Histórico Semanal"
CABECALHO   = ["Data", "Período", "Total", "Resolvidos", "Taxa%",
               "T.Técnico(h)", "T.Ciclo(h)", "Em Aberto"]

# ── Período: semana anterior (seg → dom) ─────────────────────────────
hoje         = datetime.utcnow()
seg_desta    = hoje - timedelta(days=hoje.weekday())
seg_anterior = seg_desta - timedelta(days=7)
dom_anterior = seg_desta - timedelta(days=1)
DATA_INI     = seg_anterior.strftime("%Y-%m-%d 00:00:00")
DATA_FIM     = dom_anterior.strftime("%Y-%m-%d 23:59:59")
PERIODO      = f"{seg_anterior.strftime('%d/%m')}–{dom_anterior.strftime('%d/%m/%Y')}"

# ── GLPI API ──────────────────────────────────────────────────────────
def _h(tok=None):
    h = {"App-Token": APP_TOKEN}
    if tok: h["Session-Token"] = tok
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
    r = requests.get(f"{GLPI_URL}/apirest.php/{path}",
                     headers=_h(tok), params=params, timeout=30)
    if r.status_code in (200, 206):
        return r.json()
    return []

# ── Filtro Recebe Mais ────────────────────────────────────────────────
def eh_recebemai(ticket):
    entidade  = (ticket.get("entities_id")       or "").lower()
    categoria = (ticket.get("itilcategories_id") or "").lower()
    return "recebe mais" in entidade or "recebemai" in categoria or "recebe mais" in categoria

# ── Busca tickets ─────────────────────────────────────────────────────
def buscar_tickets(tok):
    tickets = []
    offset  = 0
    while True:
        r = requests.get(
            f"{GLPI_URL}/apirest.php/Ticket",
            headers=_h(tok),
            params={"expand_dropdowns": True, "range": f"{offset}-{offset+99}",
                    "sort": "date_creation", "order": "DESC"},
            timeout=30,
        )
        if r.status_code not in (200, 206):
            break
        data = r.json()
        if not isinstance(data, list) or not data:
            break
        passou = False
        for t in data:
            dc = t.get("date_creation") or ""
            if dc > DATA_FIM:  continue
            if dc < DATA_INI:  passou = True; break
            if eh_recebemai(t): tickets.append(t)
        if passou or len(data) < 100:
            break
        offset += 100
    return tickets

# ── Cálculo de tempo útil ─────────────────────────────────────────────
FMT   = "%Y-%m-%d %H:%M:%S"
H_INI = 8
H_FIM = 18

def horas_uteis(ini_str, fim_str):
    try:
        inicio = datetime.strptime(ini_str, FMT)
        fim    = datetime.strptime(fim_str,  FMT)
    except Exception:
        return 0.0
    if fim <= inicio:
        return 0.0
    total = 0.0
    atual = inicio
    while atual < fim:
        if atual.weekday() >= 5:
            dias  = 7 - atual.weekday()
            atual = datetime(atual.year, atual.month, atual.day) + timedelta(days=dias)
            atual = atual.replace(hour=H_INI, minute=0, second=0, microsecond=0)
            continue
        ini_dia = atual.replace(hour=H_INI, minute=0, second=0, microsecond=0)
        fim_dia = atual.replace(hour=H_FIM, minute=0, second=0, microsecond=0)
        t_ini   = max(atual, ini_dia)
        t_fim   = min(fim,   fim_dia)
        if t_ini < t_fim:
            total += (t_fim - t_ini).total_seconds() / 3600
        proximo = datetime(atual.year, atual.month, atual.day) + timedelta(days=1)
        atual   = proximo.replace(hour=H_INI, minute=0, second=0, microsecond=0)
    return total

def ultimo_tecnico_data(tok, tid, req_id):
    try:
        followups = api_get(f"Ticket/{tid}/ITILFollowup", tok)
        if not isinstance(followups, list):
            return None
        tecnicos = [
            f for f in followups
            if f.get("users_id") != req_id
            and "Obrigado pelo seu contato" not in (f.get("content") or "")
            and "Base de Conhecimento" not in (f.get("content") or "")
        ]
        if not tecnicos:
            return None
        return tecnicos[-1].get("date") or tecnicos[-1].get("date_creation")
    except Exception:
        return None

# ── Google Sheets ─────────────────────────────────────────────────────
def get_worksheet():
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_JSON),
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    gc          = gspread.authorize(creds)
    spreadsheet = gc.open_by_key(SHEET_ID)
    try:
        ws = spreadsheet.worksheet(SHEET_NAME)
        # Cria cabeçalho se a aba estiver vazia
        if not ws.get_all_values():
            ws.append_row(CABECALHO)
        return ws
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=SHEET_NAME, rows=500, cols=len(CABECALHO))
        ws.append_row(CABECALHO)
        return ws

# ── Main ──────────────────────────────────────────────────────────────
def main():
    agora = datetime.now(BRASILIA).replace(tzinfo=None)
    print(f"[{agora.strftime('%H:%M')} BRT] Histórico de Métricas — {PERIODO}")

    if not GOOGLE_JSON or not SHEET_ID:
        print("  [!] GOOGLE_CREDENTIALS_JSON ou SPREADSHEET_ID não configurados.")
        return

    tok = get_session()
    try:
        tickets = buscar_tickets(tok)
        print(f"  Tickets Recebe Mais na semana: {len(tickets)}")
    finally:
        close_session(tok)

    if not tickets:
        print("  Nenhum ticket encontrado.")
        return

    total    = len(tickets)
    resol    = sum(1 for t in tickets if t.get("status") in (5, 6))
    em_aberto = sum(1 for t in tickets if t.get("status") in (1, 2, 4))
    taxa     = round(resol / total * 100) if total else 0

    # Tempo médio: busca followups dos resolvidos
    print(f"  Calculando tempos de {resol} tickets resolvidos...")
    tempos_tec  = []
    tempos_ciclo= []
    tok2 = get_session()
    try:
        for t in tickets:
            if t.get("status") not in (5, 6):
                continue
            tid      = t["id"]
            criacao  = t.get("date_creation") or ""
            atualizacao = t.get("date_mod") or ""
            req_id   = t.get("users_id_recipient")

            h_ciclo  = horas_uteis(criacao, atualizacao)
            ult      = ultimo_tecnico_data(tok2, tid, req_id)
            h_tec    = horas_uteis(criacao, ult) if ult else h_ciclo

            tempos_tec.append(h_tec)
            tempos_ciclo.append(h_ciclo)
    finally:
        close_session(tok2)

    t_tec_med  = round(sum(tempos_tec)   / len(tempos_tec),   2) if tempos_tec   else 0
    t_ciclo_med= round(sum(tempos_ciclo) / len(tempos_ciclo), 2) if tempos_ciclo else 0

    linha = [
        seg_anterior.strftime("%d/%m/%Y"),  # Data (segunda-feira da semana)
        PERIODO,
        total,
        resol,
        taxa,
        t_tec_med,
        t_ciclo_med,
        em_aberto,
    ]

    print(f"  Registrando na planilha: {linha}")
    ws = get_worksheet()
    ws.append_row(linha)
    print(f"  ✓ Linha registrada na aba '{SHEET_NAME}'")
    print(f"  Resumo: {total} tickets | {resol} resolvidos ({taxa}%) | "
          f"T.Técnico: {t_tec_med}h | T.Ciclo: {t_ciclo_med}h")

if __name__ == "__main__":
    main()
