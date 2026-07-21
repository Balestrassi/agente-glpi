"""
Relatório Individual por Cliente — Recebe Mais
Gera um PDF separado para cada cliente (ALS, H2, DockTech, Tegma, …) com
as métricas do período. Controlado pela env var MODO:
  semanal (padrão) — semana anterior seg–dom, roda toda segunda-feira
  mensal           — mês anterior completo, roda todo dia 1º do mês
"""

import html, io, os, sys, requests
from datetime import datetime, timedelta, date as _date
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, PageBreak,
)

# ── Credenciais ──────────────────────────────────────────────────────────
GLPI_URL         = os.environ.get("GLPI_URL",         "https://servicedesk.a7on.ai")
APP_TOKEN        = os.environ.get("GLPI_APP_TOKEN",   "")
USER_TOKEN       = os.environ.get("GLPI_USER_TOKEN",  "")
GLPI_PROFILE_ID  = os.environ.get("GLPI_PROFILE_ID",  "4")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN",   "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ── Modo e período ───────────────────────────────────────────────────────
MODO = os.environ.get("MODO", sys.argv[1] if len(sys.argv) > 1 else "semanal").strip().lower()

_hoje = datetime.utcnow()

if MODO == "mensal":
    _prim_atual  = _date(_hoje.year, _hoje.month, 1)
    _ult_ant     = _prim_atual - timedelta(days=1)
    _prim_ant    = _date(_ult_ant.year, _ult_ant.month, 1)
    _ult_ret     = _prim_ant - timedelta(days=1)
    _prim_ret    = _date(_ult_ret.year, _ult_ret.month, 1)

    DATA_INI     = _prim_ant.strftime("%Y-%m-%d 00:00:00")
    DATA_FIM     = _ult_ant.strftime("%Y-%m-%d 23:59:59")
    DATA_INI_ANT = _prim_ret.strftime("%Y-%m-%d 00:00:00")
    DATA_FIM_ANT = _ult_ret.strftime("%Y-%m-%d 23:59:59")
    PERIODO      = f"{_prim_ant.strftime('%d/%m/%Y')} a {_ult_ant.strftime('%d/%m/%Y')}"
    PERIODO_ANT  = _prim_ret.strftime("%m/%Y")
    PERIODO_LABEL = _prim_ant.strftime("%m/%Y")
    TIPO_LABEL   = "Mensal"
    FILE_SUFIXO  = _prim_ant.strftime("%m_%Y")
else:
    _seg_desta   = _hoje - timedelta(days=_hoje.weekday())
    _seg_ant     = _seg_desta - timedelta(days=7)
    _dom_ant     = _seg_desta - timedelta(days=1)
    _seg_pen     = _seg_ant   - timedelta(days=7)
    _dom_pen     = _seg_ant   - timedelta(days=1)

    DATA_INI     = _seg_ant.strftime("%Y-%m-%d 00:00:00")
    DATA_FIM     = _dom_ant.strftime("%Y-%m-%d 23:59:59")
    DATA_INI_ANT = _seg_pen.strftime("%Y-%m-%d 00:00:00")
    DATA_FIM_ANT = _dom_pen.strftime("%Y-%m-%d 23:59:59")
    PERIODO      = f"{_seg_ant.strftime('%d/%m/%Y')} a {_dom_ant.strftime('%d/%m/%Y')}"
    PERIODO_ANT  = f"{_seg_pen.strftime('%d/%m')} a {_dom_pen.strftime('%d/%m/%Y')}"
    PERIODO_LABEL = f"{_seg_ant.strftime('%d/%m')}–{_dom_ant.strftime('%d/%m/%Y')}"
    TIPO_LABEL   = "Semanal"
    FILE_SUFIXO  = f"{_seg_ant.strftime('%d_%m')}_{_dom_ant.strftime('%d_%m_%Y')}"

HOJE_STR = _hoje.strftime("%d/%m/%Y %H:%M")

# ── Cores ────────────────────────────────────────────────────────────────
AZUL_ESC  = colors.HexColor("#1A3A5C")
AZUL_MED  = colors.HexColor("#2E6DA4")
AZUL_CLA  = colors.HexColor("#D6E8F7")
VERDE     = colors.HexColor("#27AE60")
AMARELO   = colors.HexColor("#F39C12")
VERMELHO  = colors.HexColor("#E74C3C")
CINZA_CLA = colors.HexColor("#F5F6FA")
CINZA_BRD = colors.HexColor("#DADDE3")
BRANCO    = colors.white
PRETO     = colors.HexColor("#2C3E50")

# ── Estilos ──────────────────────────────────────────────────────────────
def S(name, **kw): return ParagraphStyle(name, **kw)

TITULO_C = S("TC", fontName="Helvetica-Bold", fontSize=22, textColor=BRANCO,   alignment=TA_CENTER, leading=28)
SUB_C    = S("SC", fontName="Helvetica",      fontSize=11, textColor=AZUL_CLA, alignment=TA_CENTER, leading=16)
SECAO    = S("SE", fontName="Helvetica-Bold", fontSize=12, textColor=AZUL_ESC, spaceAfter=4, spaceBefore=12)
BODY     = S("BO", fontName="Helvetica",      fontSize=9,  textColor=PRETO,    leading=13)
NUM_G    = S("NG", fontName="Helvetica-Bold", fontSize=22, textColor=AZUL_MED, alignment=TA_CENTER)
NUM_L    = S("NL", fontName="Helvetica",      fontSize=8,  textColor=colors.HexColor("#7F8C8D"), alignment=TA_CENTER, leading=10)

# ── GLPI API ─────────────────────────────────────────────────────────────
def _headers(tok):
    return {"App-Token": APP_TOKEN, "Session-Token": tok}

def get_session():
    r = requests.get(
        f"{GLPI_URL}/apirest.php/initSession",
        headers={"Authorization": f"user_token {USER_TOKEN}", "App-Token": APP_TOKEN},
        timeout=15,
    )
    r.raise_for_status()
    tok = r.json()["session_token"]
    requests.post(f"{GLPI_URL}/apirest.php/changeActiveProfile",
                  headers=_headers(tok), json={"profiles_id": GLPI_PROFILE_ID}, timeout=15)
    return tok

def close_session(tok):
    requests.get(f"{GLPI_URL}/apirest.php/killSession", headers=_headers(tok), timeout=10)

def api_get(path, tok, params=None):
    r = requests.get(f"{GLPI_URL}/apirest.php/{path}",
                     headers=_headers(tok), params=params, timeout=30)
    return r.json() if r.status_code in (200, 206) else []

def eh_recebemai(ticket):
    ent = (ticket.get("entities_id")       or "").lower()
    cat = (ticket.get("itilcategories_id") or "").lower()
    return "recebe mais" in ent or "recebemai" in cat or "recebe mais" in cat

# Profundidade do nível de cliente na hierarquia de entidades do GLPI.
# Definida em main() após inspecionar os tickets reais; usada em produto()
# para agrupar sub-entidades sob o mesmo cliente (ex: Messiânica > Messianica
# → ambas ficam como "Messiânica").
_PROF_CLI: int = 0

def produto(ticket):
    """Extrai o nome do cliente a partir do caminho de entidade do GLPI.
    O GLPI retorna '>' como '&#62;' quando expand_dropdowns=True, então
    é necessário decodificar HTML antes de separar os segmentos.
    Usa _PROF_CLI para fixar o nível de extração e mesclar sub-entidades."""
    ent    = html.unescape((ticket.get("entities_id") or "").strip())
    partes = [p.strip() for p in ent.split(">") if p.strip()]
    if not partes:
        return "Sem entidade"
    idx = min((_PROF_CLI or len(partes)) - 1, len(partes) - 1)
    cli = partes[idx]
    return "Recebe Mais (Geral)" if cli.lower() == "recebe mais" else cli

def tipo(titulo):
    tl = titulo.lower()
    if "erro"    in tl:                        return "Erro"
    if "servico" in tl or "serviço" in tl:     return "Solicitação de Serviço"
    if "wildlife" in tl:                        return "Wildlife"
    if "duvida"  in tl or "dúvida"  in tl:     return "Dúvida / Orientação"
    if "melhoria" in tl:                        return "Solicitação de Melhoria"
    return "Outros"

def foi_resolvido(solvedate, data_fim):
    return bool(solvedate) and solvedate <= data_fim

def buscar_tickets_range(tok, data_ini, data_fim):
    tickets, offset = [], 0
    while True:
        r = requests.get(
            f"{GLPI_URL}/apirest.php/Ticket",
            headers=_headers(tok),
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
            dc = t.get("date_creation") or t.get("date", "")
            if dc > data_fim: continue
            if dc < data_ini: passou = True; break
            if eh_recebemai(t): tickets.append(t)
        if passou: break
        cr = r.headers.get("Content-Range", "")
        if cr:
            if offset + 100 >= int(cr.split("/")[-1]): break
        else:
            break
        offset += 100
    return tickets

def parse_tickets(raw):
    return [(
        t.get("id"),
        t.get("name", ""),
        t.get("date_creation") or t.get("date", ""),
        t.get("date_mod", ""),
        t.get("urgency", 3),
        t.get("status", 1),
        produto(t),
        t.get("solvedate") or "",
    ) for t in raw]

# ── Followups e técnicos ──────────────────────────────────────────────────
_req_cache  = {}
_nomes_cache = {}

def requester_id(tok, tid):
    if tid not in _req_cache:
        try:
            t = api_get(f"Ticket/{tid}", tok)
            _req_cache[tid] = t.get("users_id_recipient") if isinstance(t, dict) else None
        except Exception:
            _req_cache[tid] = None
    return _req_cache[tid]

def ultimo_tecnico_completo(tok, tid, req_id):
    try:
        fps = api_get(f"Ticket/{tid}/ITILFollowup", tok)
        if not isinstance(fps, list):
            return None, None, None
        tec = [f for f in fps
               if f.get("users_id") != req_id
               and "Base de Conhecimento" not in (f.get("content") or "")
               and "Obrigado pelo seu contato" not in (f.get("content") or "")]
        if not tec:
            return None, None, None
        ult, prim = tec[-1], tec[0]
        return (ult.get("date")  or ult.get("date_creation"),
                ult.get("users_id"),
                prim.get("date") or prim.get("date_creation"))
    except Exception:
        return None, None, None

def buscar_nome_usuario(tok, uid):
    if not uid:
        return "Desconhecido"
    if uid not in _nomes_cache:
        try:
            u = api_get(f"User/{uid}", tok)
            if isinstance(u, dict):
                _nomes_cache[uid] = f"{u.get('firstname','').strip()} {u.get('realname','').strip()}".strip() or f"Usuário {uid}"
            else:
                _nomes_cache[uid] = f"Usuário {uid}"
        except Exception:
            _nomes_cache[uid] = f"Usuário {uid}"
    return _nomes_cache[uid]

# ── Métricas de tempo ─────────────────────────────────────────────────────
FMT    = "%Y-%m-%d %H:%M:%S"
H_INI, H_FIM = 8, 18
H_DIA  = H_FIM - H_INI

def horas_uteis(ini_str, fim_str):
    try:
        inicio = datetime.strptime(ini_str, FMT)
        fim    = datetime.strptime(fim_str,  FMT)
    except Exception:
        return 0.0
    if fim <= inicio:
        return 0.0
    total, atual = 0.0, inicio
    while atual < fim:
        if atual.weekday() >= 5:
            dias  = 7 - atual.weekday()
            atual = (datetime(atual.year, atual.month, atual.day) + timedelta(days=dias)).replace(hour=H_INI, minute=0, second=0, microsecond=0)
            continue
        t_ini = max(atual, atual.replace(hour=H_INI, minute=0, second=0, microsecond=0))
        t_fim = min(fim,   atual.replace(hour=H_FIM, minute=0, second=0, microsecond=0))
        if t_ini < t_fim:
            total += (t_fim - t_ini).total_seconds() / 3600
        atual = (datetime(atual.year, atual.month, atual.day) + timedelta(days=1)).replace(hour=H_INI, minute=0, second=0, microsecond=0)
    return total

def fmt_hu(h):
    if h < 1:      return f"{int(h*60)} min úteis"
    if h < H_DIA:  return f"{h:.1f} h úteis"
    dias = h / H_DIA
    return f"{h:.1f} h úteis" if dias < 1.05 else f"{dias:.1f} dias úteis"

def media_l(v):   return sum(v)/len(v) if v else 0.0
def pct50(v):
    if not v: return 0.0
    s = sorted(v); k = len(s)//2
    return s[k] if len(s)%2 else (s[k-1]+s[k])/2
def pct90(v):
    if not v: return 0.0
    s = sorted(v); k = round(0.9*(len(s)-1))
    return s[min(len(s)-1, k)]
def delta_str(a, b):
    if b == 0: return "—"
    p = round((a-b)/b*100)
    return f"+{p}%" if p >= 0 else f"{p}%"
def delta_pp(a, b):
    d = round(a-b)
    return f"+{d} pp" if d >= 0 else f"{d} pp"
def cor_delta(v):
    return "#27AE60" if "+" in v else ("#E74C3C" if "-" in v else "#7F8C8D")

# ── Helpers visuais ───────────────────────────────────────────────────────
def section(text):
    return [Spacer(1, 6), Paragraph(text, SECAO),
            HRFlowable(width="100%", thickness=1.5, color=AZUL_MED, spaceAfter=8)]

def th(text):
    return Paragraph(text, S("th", fontName="Helvetica-Bold", fontSize=8,
                              textColor=BRANCO, alignment=TA_CENTER))

def styled_tbl(header, rows, cols, hbg=AZUL_ESC):
    data = [[th(h) for h in header]]
    for row in rows:
        data.append([Paragraph(str(c), BODY) if not isinstance(c, Paragraph) else c for c in row])
    t = Table(data, colWidths=cols, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,0),  hbg),
        ("ROWBACKGROUNDS",(0,1), (-1,-1), [BRANCO, CINZA_CLA]),
        ("GRID",          (0,0), (-1,-1), 0.3, CINZA_BRD),
        ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING",    (0,0), (-1,-1), 4),
        ("BOTTOMPADDING", (0,0), (-1,-1), 4),
        ("LEFTPADDING",   (0,0), (-1,-1), 6),
    ]))
    return t

URG_MAP    = {1:"Muito Baixa", 2:"Baixa", 3:"Média", 4:"Alta", 5:"Muito Alta"}
STATUS_MAP = {1:"Novo", 2:"Em Andamento", 4:"Pendente", 5:"Resolvido", 6:"Fechado"}

def urg_p(num):
    label = URG_MAP.get(num, "?")
    c = {"Muito Alta":"#E74C3C","Alta":"#F39C12","Média":"#2E6DA4","Baixa":"#27AE60"}.get(label,"#2C3E50")
    return Paragraph(f'<font color="{c}"><b>{label}</b></font>', BODY)

def nota_box(texto):
    t = Table([[Paragraph(texto, S("nb",fontName="Helvetica-Oblique",fontSize=8,
                textColor=colors.HexColor("#7F8C8D"),leading=12))]], colWidths=[17*cm])
    t.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,-1),CINZA_CLA),
                            ("BOX",(0,0),(-1,-1),0.5,CINZA_BRD),
                            ("TOPPADDING",(0,0),(-1,-1),6),
                            ("BOTTOMPADDING",(0,0),(-1,-1),6),
                            ("LEFTPADDING",(0,0),(-1,-1),10)]))
    return t

# ── Header / Footer ───────────────────────────────────────────────────────
def make_on_page(cliente):
    def on_page(canvas, doc):
        canvas.saveState()
        w, h = A4
        canvas.setFillColor(AZUL_ESC)
        canvas.rect(0, h-1.2*cm, w, 1.2*cm, fill=1, stroke=0)
        canvas.setFont("Helvetica-Bold", 9); canvas.setFillColor(BRANCO)
        canvas.drawString(1.5*cm, h-0.85*cm, f"Suporte RM — Relatório {TIPO_LABEL} {cliente} | {PERIODO}")
        canvas.drawRightString(w-1.5*cm, h-0.85*cm, f"Gerado em {HOJE_STR}")
        canvas.setFillColor(AZUL_ESC)
        canvas.rect(0, 0, w, 0.8*cm, fill=1, stroke=0)
        canvas.setFont("Helvetica", 8); canvas.setFillColor(BRANCO)
        canvas.drawString(1.5*cm, 0.27*cm, "Dados extraídos do GLPI | Filtro: entidade Recebe Mais + data de criação")
        canvas.drawRightString(w-1.5*cm, 0.27*cm, f"Página {doc.page}")
        canvas.restoreState()
    return on_page

def on_first(canvas, doc): pass

# ── Seções do PDF ─────────────────────────────────────────────────────────
def capa(story, cliente, total, taxa, comp):
    story.append(Spacer(1, 2.5*cm))
    t = Table(
        [[Paragraph(f"RELATÓRIO {TIPO_LABEL.upper()}<br/>{cliente.upper()}", TITULO_C)]],
        colWidths=[17*cm], rowHeights=[3.5*cm],
    )
    t.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,-1),AZUL_ESC),
                            ("ALIGN",(0,0),(-1,-1),"CENTER"),
                            ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
                            ("TOPPADDING",(0,0),(-1,-1),20),
                            ("BOTTOMPADDING",(0,0),(-1,-1),20)]))
    story.append(t)
    story.append(Spacer(1, 0.6*cm))
    sub = Table([[Paragraph(f"Período: {PERIODO}", SUB_C),
                  Paragraph(f"Gerado em: {HOJE_STR}", SUB_C)]],
                colWidths=[8.5*cm,8.5*cm], rowHeights=[1*cm])
    sub.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,-1),AZUL_MED),
                              ("ALIGN",(0,0),(-1,-1),"CENTER"),
                              ("VALIGN",(0,0),(-1,-1),"MIDDLE")]))
    story.append(sub)
    story.append(Spacer(1, 0.8*cm))

    kpi_vals = [
        [Paragraph(str(total), NUM_G),
         Paragraph(f"{taxa}%", S("pct",fontName="Helvetica-Bold",fontSize=22,
                                 textColor=VERDE,alignment=TA_CENTER))],
        [Paragraph("Chamados no Período",NUM_L), Paragraph("Taxa de Resolução",NUM_L)],
    ]
    if comp:
        dt = delta_str(total, comp["total"]); dta = delta_pp(taxa, comp["taxa"])
        kpi_vals.append([
            Paragraph(f'vs {PERIODO_ANT}: <b>{comp["total"]}</b> (<font color="{cor_delta(dt)}"><b>{dt}</b></font>)',
                      S("cv",fontName="Helvetica",fontSize=8.5,textColor=colors.HexColor("#7F8C8D"),alignment=TA_CENTER,leading=13)),
            Paragraph(f'vs {PERIODO_ANT}: <b>{comp["taxa"]}%</b> (<font color="{cor_delta(dta)}"><b>{dta}</b></font>)',
                      S("cv2",fontName="Helvetica",fontSize=8.5,textColor=colors.HexColor("#7F8C8D"),alignment=TA_CENTER,leading=13)),
        ])
    kpi = Table(kpi_vals, colWidths=[8.5*cm,8.5*cm])
    kpi.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,-1),CINZA_CLA),
                              ("ALIGN",(0,0),(-1,-1),"CENTER"),
                              ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
                              ("BOX",(0,0),(-1,-1),0.5,CINZA_BRD),
                              ("LINEAFTER",(0,0),(0,-1),0.5,CINZA_BRD),
                              ("TOPPADDING",(0,0),(-1,-1),8),
                              ("BOTTOMPADDING",(0,0),(-1,-1),8)]))
    story.append(kpi)
    story.append(PageBreak())


def visao_geral(story, tickets, total, resol, taxa, nao_resolvidos, comp):
    story += section("Visão Geral do Período")

    kpi_data = [
        [Paragraph(str(total), NUM_G),
         Paragraph(str(resol), NUM_G),
         Paragraph(f"{taxa}%", S("pct2",fontName="Helvetica-Bold",fontSize=22,
                                 textColor=VERDE,alignment=TA_CENTER)),
         Paragraph(str(len(nao_resolvidos)), S("ab",fontName="Helvetica-Bold",
                   fontSize=22,textColor=AMARELO,alignment=TA_CENTER))],
        [Paragraph("Total Abertos",NUM_L), Paragraph("Resolvidos/Fechados",NUM_L),
         Paragraph("Taxa de Resolução",NUM_L), Paragraph("Ainda em Aberto",NUM_L)],
    ]
    kpi = Table(kpi_data, colWidths=[4.25*cm]*4, rowHeights=[1.4*cm,0.6*cm])
    kpi.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,-1),CINZA_CLA),
                              ("ALIGN",(0,0),(-1,-1),"CENTER"),
                              ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
                              ("BOX",(0,0),(-1,-1),0.5,CINZA_BRD),
                              ("LINEAFTER",(0,0),(-3,-1),0.5,CINZA_BRD)]))
    story.append(kpi)
    story.append(Spacer(1,0.4*cm))

    if comp:
        dt = delta_str(total, comp["total"]); dta = delta_pp(taxa, comp["taxa"])
        comp_data = [[Paragraph(
            f'Período anterior ({PERIODO_ANT}): <b>{comp["total"]}</b> chamados · <b>{comp["taxa"]}%</b> resolvidos · '
            f'Volume: <font color="{cor_delta(dt)}"><b>{dt}</b></font> · '
            f'Taxa: <font color="{cor_delta(dta)}"><b>{dta}</b></font>',
            S("nc",fontName="Helvetica",fontSize=8,textColor=colors.HexColor("#7F8C8D"),alignment=TA_CENTER,leading=12),
        )]]
        comp_tbl = Table(comp_data, colWidths=[17*cm])
        comp_tbl.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,-1),CINZA_CLA),
                                       ("BOX",(0,0),(-1,-1),0.5,CINZA_BRD),
                                       ("TOPPADDING",(0,0),(-1,-1),6),
                                       ("BOTTOMPADDING",(0,0),(-1,-1),6),
                                       ("LEFTPADDING",(0,0),(-1,-1),10)]))
        story.append(comp_tbl)
        story.append(Spacer(1,0.3*cm))

    # Volume por status
    por_status = {}
    for *_, st, _, _ in tickets:
        por_status[st] = por_status.get(st, 0) + 1
    story += section("Volume por Status")
    rows_s = []
    for st, nome, cor in [(1,"Novo",VERMELHO),(2,"Em Andamento",AZUL_MED),
                           (4,"Pendente",AMARELO),(5,"Resolvido",VERDE),
                           (6,"Fechado",colors.HexColor("#7F8C8D"))]:
        qtd = por_status.get(st, 0)
        if not qtd: continue
        pct   = round(qtd/total*100)
        c_hex = "#%02x%02x%02x" % (int(cor.red*255), int(cor.green*255), int(cor.blue*255))
        rows_s.append((Paragraph(f'<font color="{c_hex}"><b>{nome}</b></font>',BODY),
                       str(qtd), f"{pct}%", "█"*int(pct/5)+"░"*(20-int(pct/5))))
    story.append(styled_tbl(["Status","Qtd.","%","Proporção"], rows_s, [4*cm,2*cm,2*cm,9*cm]))

    # Volume por tipo
    por_tipo = {}
    for _, titulo, *_ in tickets:
        tp = tipo(titulo)
        por_tipo[tp] = por_tipo.get(tp, 0) + 1
    story += section("Volume por Tipo de Chamado")
    rows_t = []
    for tp in ["Erro","Solicitação de Serviço","Wildlife","Dúvida / Orientação","Solicitação de Melhoria","Outros"]:
        qtd = por_tipo.get(tp, 0)
        if not qtd: continue
        pct = round(qtd/total*100)
        rows_t.append((tp, str(qtd), f"{pct}%", "█"*int(pct/5)+"░"*(20-int(pct/5))))
    story.append(styled_tbl(["Tipo","Qtd.","%","Proporção"], rows_t, [5.5*cm,2*cm,2*cm,7.5*cm]))


def tempo_medio(story, tempos, frt_list):
    if not tempos:
        return
    story.append(PageBreak())
    story += section("Tempo Médio de Resolução")
    story.append(nota_box(
        f"Calculado sobre {len(tempos)} chamados resolvidos/fechados criados no período. "
        "<b>Tempo Técnico</b>: criação → último comentário do técnico. "
        "<b>Tempo Total</b>: criação → última atualização do chamado. "
        "Ambos em horas úteis (seg–sex, 08h–18h)."
    ))
    story.append(Spacer(1,0.4*cm))

    ciclos   = [hc for hc, _ in tempos]
    tecnicos = [ht for _, ht in tempos]

    duo = Table([[
        Paragraph(fmt_hu(media_l(tecnicos)),
                  S("tgt",fontName="Helvetica-Bold",fontSize=26,textColor=VERDE,alignment=TA_CENTER)),
        Paragraph(fmt_hu(media_l(ciclos)),
                  S("tgc",fontName="Helvetica-Bold",fontSize=26,textColor=AZUL_MED,alignment=TA_CENTER)),
    ],[
        Paragraph("<b>Tempo Técnico Médio</b><br/><font color='#7F8C8D' size='8'>criação → último comentário do técnico</font>",
                  S("lb1",fontName="Helvetica",fontSize=9,alignment=TA_CENTER,leading=13)),
        Paragraph("<b>Tempo Total Médio de Ciclo</b><br/><font color='#7F8C8D' size='8'>criação → última atualização</font>",
                  S("lb2",fontName="Helvetica",fontSize=9,alignment=TA_CENTER,leading=13)),
    ]], colWidths=[8.5*cm,8.5*cm], rowHeights=[2*cm,1*cm])
    duo.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(0,-1),colors.HexColor("#EAFAF1")),
        ("BACKGROUND",(1,0),(1,-1),AZUL_CLA),
        ("ALIGN",(0,0),(-1,-1),"CENTER"),
        ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
        ("BOX",(0,0),(0,-1),1.5,VERDE),
        ("BOX",(1,0),(1,-1),1.5,AZUL_MED),
        ("TOPPADDING",(0,0),(-1,-1),8),
        ("BOTTOMPADDING",(0,0),(-1,-1),8),
    ]))
    story.append(duo)
    story.append(Spacer(1,0.6*cm))

    story += section("Distribuição (média × mediana × P90)")
    rows = [
        ("Tempo Técnico",  fmt_hu(media_l(tecnicos)), fmt_hu(pct50(tecnicos)), fmt_hu(pct90(tecnicos))),
        ("Tempo Total",    fmt_hu(media_l(ciclos)),   fmt_hu(pct50(ciclos)),   fmt_hu(pct90(ciclos))),
    ]
    if frt_list:
        rows.append(("Primeira Resposta (FRT)", fmt_hu(media_l(frt_list)),
                     fmt_hu(pct50(frt_list)), fmt_hu(pct90(frt_list))))
    story.append(styled_tbl(["Métrica","Média","Mediana (P50)","P90"],
                             rows, [5.5*cm,3.8*cm,3.8*cm,3.8*cm]))
    story.append(Spacer(1,4))
    story.append(Paragraph(
        "A <b>mediana</b> é o caso típico; o <b>P90</b> é o pior cenário usual (só 10% demoram mais). "
        "<b>FRT</b> = tempo até a primeira resposta do técnico.",
        S("leg",fontName="Helvetica-Oblique",fontSize=8,textColor=colors.HexColor("#7F8C8D"),leading=12),
    ))


def ranking_tecnicos(story, ranking):
    if not ranking:
        return
    story.append(PageBreak())
    story += section(f"Ranking de Técnicos — {PERIODO_LABEL}")
    story.append(nota_box(
        "Considera chamados resolvidos/fechados no período para este cliente. "
        "Técnico identificado pelo último comentário (excluindo respostas automáticas). "
        "Tempos em horas úteis: seg–sex, 08h–18h."
    ))
    story.append(Spacer(1,0.4*cm))
    medalhas = ["🥇","🥈","🥉"]
    rows = []
    for i, (uid, d) in enumerate(sorted(ranking.items(), key=lambda x: -x[1]["count"])):
        pos = medalhas[i] if i < 3 else f"{i+1}º"
        rows.append((
            Paragraph(f"<b>{pos}</b>", BODY),
            Paragraph(f"<b>{d['nome']}</b>", BODY),
            Paragraph(f"<b>{d['count']}</b>", S("bc",fontName="Helvetica-Bold",fontSize=9,
                      textColor=AZUL_MED,alignment=TA_CENTER)),
            fmt_hu(d["tempo_tecnico_avg"]),
            fmt_hu(d["tempo_ciclo_avg"]),
        ))
    story.append(styled_tbl(["#","Técnico","Resolvidos","T. Técnico Médio","T. Ciclo Médio"],
                             rows, [1.2*cm,6*cm,2.5*cm,3.65*cm,3.65*cm], hbg=VERDE))


def em_aberto(story, nao_resolvidos):
    if not nao_resolvidos:
        return
    story.append(PageBreak())
    story += section(f"Chamados do Período Ainda em Aberto ({len(nao_resolvidos)})")
    aviso = Table([[Paragraph(
        f"Os {len(nao_resolvidos)} chamados abaixo foram criados no período {PERIODO_LABEL} "
        "e não tinham data de solução registrada até o fim do período.",
        S("av",fontName="Helvetica",fontSize=9,textColor=AMARELO,leading=13),
    )]], colWidths=[17*cm])
    aviso.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,-1),colors.HexColor("#FEF9E7")),
                                ("BOX",(0,0),(-1,-1),1,AMARELO),
                                ("TOPPADDING",(0,0),(-1,-1),8),
                                ("BOTTOMPADDING",(0,0),(-1,-1),8),
                                ("LEFTPADDING",(0,0),(-1,-1),10)]))
    story.append(aviso)
    story.append(Spacer(1,0.4*cm))
    rows = []
    for tid, titulo, criacao, urg, st in sorted(nao_resolvidos, key=lambda x: x[2]):
        dt = datetime.strptime(criacao, FMT)
        rows.append((str(tid), titulo[:52]+("…" if len(titulo)>52 else ""),
                     dt.strftime("%d/%m %H:%M"), urg_p(urg), Paragraph(st,BODY)))
    story.append(styled_tbl(["ID","Título","Criação","Urgência","Status"],
                             rows, [1.5*cm,7.5*cm,2.5*cm,3*cm,2.5*cm], hbg=AMARELO))


# ── Gera PDF de um cliente ────────────────────────────────────────────────
def gerar_pdf_cliente(cliente, tickets_cli, dados_followup, comp):
    total = len(tickets_cli)
    resol = sum(1 for *_, s in tickets_cli if foi_resolvido(s, DATA_FIM))
    taxa  = round(resol/total*100) if total else 0

    tempos, frt_list, nao_resolvidos, ranking = [], [], [], {}

    for tid, titulo, criacao, atualizacao, urg, st, _, solve in tickets_cli:
        if foi_resolvido(solve, DATA_FIM):
            d         = dados_followup.get(tid, {})
            criacao_d = d.get("criacao") or criacao
            h_ciclo   = horas_uteis(criacao_d, atualizacao)
            ult_tec   = d.get("ultimo_tecnico")
            h_tecnico = horas_uteis(criacao_d, ult_tec) if ult_tec else h_ciclo
            tempos.append((h_ciclo, h_tecnico))
            prim_tec = d.get("primeiro_tecnico")
            if prim_tec:
                frt_list.append(horas_uteis(criacao_d, prim_tec))
            uid = d.get("tecnico_id")
            if uid:
                if uid not in ranking:
                    ranking[uid] = {"nome": _nomes_cache.get(uid, f"Usuário {uid}"),
                                    "count": 0, "tt": [], "tc": []}
                ranking[uid]["count"] += 1
                ranking[uid]["tt"].append(h_tecnico)
                ranking[uid]["tc"].append(h_ciclo)
        else:
            nao_resolvidos.append((tid, titulo, criacao, urg, STATUS_MAP.get(st, str(st))))

    ranking_final = {
        uid: {
            "nome":               d["nome"],
            "count":              d["count"],
            "tempo_tecnico_avg":  media_l(d["tt"]),
            "tempo_ciclo_avg":    media_l(d["tc"]),
        }
        for uid, d in ranking.items()
    }

    buf  = io.BytesIO()
    nome = f"Relatorio_{TIPO_LABEL}_{cliente.replace(' ','_')}_{FILE_SUFIXO}.pdf"
    doc  = SimpleDocTemplate(buf, pagesize=A4,
                             leftMargin=1.5*cm, rightMargin=1.5*cm,
                             topMargin=1.8*cm,  bottomMargin=1.2*cm)
    story = []
    capa(story, cliente, total, taxa, comp)
    visao_geral(story, tickets_cli, total, resol, taxa, nao_resolvidos, comp)
    tempo_medio(story, tempos, frt_list)
    ranking_tecnicos(story, ranking_final)
    em_aberto(story, nao_resolvidos)
    doc.build(story, onFirstPage=on_first, onLaterPages=make_on_page(cliente))

    return buf.getvalue(), nome, total, taxa


# ── Envio Telegram ────────────────────────────────────────────────────────
def enviar_telegram(pdf_bytes, nome_arquivo, cliente, total, taxa):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    caption = (
        f"📊 *Relatório {TIPO_LABEL} — {cliente}*\n"
        f"Período: {PERIODO}\n"
        f"{total} chamado(s) | {taxa}% resolvidos"
    )
    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument",
        data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "Markdown"},
        files={"document": (nome_arquivo, pdf_bytes, "application/pdf")},
        timeout=60,
    )
    if r.ok:
        print(f"  ✓ Telegram: {cliente} ({len(pdf_bytes)//1024} KB)")
    else:
        print(f"  [!] Falha Telegram {cliente}: {r.status_code} {r.text[:200]}")


# ── Main ──────────────────────────────────────────────────────────────────
def main():
    print(f"Relatório Individual por Cliente | Modo: {TIPO_LABEL} | Período: {PERIODO}")

    # Busca tickets do período e do anterior (comparativo)
    tok = get_session()
    try:
        raw      = buscar_tickets_range(tok, DATA_INI,     DATA_FIM)
        raw_ant  = buscar_tickets_range(tok, DATA_INI_ANT, DATA_FIM_ANT)
    finally:
        close_session(tok)

    print(f"  Período atual: {len(raw)} tickets | Anterior: {len(raw_ant)} tickets")

    # Determina a profundidade do nível de cliente na hierarquia de entidades.
    # GLPI retorna '>' como '&#62;'; decodificamos antes de contar segmentos.
    # Usamos o mínimo (ignorando depth 1 = raiz) para que sub-entidades mais
    # profundas sejam agrupadas sob o mesmo cliente-pai.
    global _PROF_CLI
    depths = []
    for t in raw:
        ent    = html.unescape((t.get("entities_id") or "").strip())
        partes = [p.strip() for p in ent.split(">") if p.strip()]
        if len(partes) >= 2:
            depths.append(len(partes))
    _PROF_CLI = min(depths) if depths else 3
    print(f"  Profundidade de entidade cliente detectada: {_PROF_CLI}")

    TICKETS     = parse_tickets(raw)
    TICKETS_ANT = parse_tickets(raw_ant)

    # Agrupa por cliente
    por_cli     = {}
    for t in TICKETS:     por_cli.setdefault(t[6], []).append(t)
    por_cli_ant = {}
    for t in TICKETS_ANT: por_cli_ant.setdefault(t[6], []).append(t)

    print(f"  Clientes: {', '.join(sorted(por_cli))}")

    # Busca followups de todos os tickets resolvidos de uma vez
    resolvidos = [(tid, criacao) for tid, _, criacao, _, _, _, _, solve in TICKETS
                  if foi_resolvido(solve, DATA_FIM)]
    print(f"  Buscando followups de {len(resolvidos)} tickets resolvidos...")

    tok2 = get_session()
    try:
        DADOS = {}
        for i, (tid, criacao) in enumerate(resolvidos):
            req_id        = requester_id(tok2, tid)
            ult_d, uid, prim_d = ultimo_tecnico_completo(tok2, tid, req_id)
            DADOS[tid] = {"criacao": criacao, "ultimo_tecnico": ult_d,
                          "primeiro_tecnico": prim_d, "tecnico_id": uid}
            if (i+1) % 10 == 0:
                print(f"    {i+1}/{len(resolvidos)} processados...")
        for uid in {d["tecnico_id"] for d in DADOS.values() if d.get("tecnico_id")}:
            buscar_nome_usuario(tok2, uid)
    finally:
        close_session(tok2)

    # Gera e envia um PDF por cliente
    print(f"\n  Gerando {len(por_cli)} relatório(s)...")
    for cliente in sorted(por_cli):
        tickets_cli = por_cli[cliente]
        ant_cli     = por_cli_ant.get(cliente, [])
        if ant_cli:
            total_ant = len(ant_cli)
            resol_ant = sum(1 for *_, s in ant_cli if foi_resolvido(s, DATA_FIM_ANT))
            comp      = {"total": total_ant, "taxa": round(resol_ant/total_ant*100) if total_ant else 0}
        else:
            comp = None

        print(f"  [{cliente}] {len(tickets_cli)} tickets...", end=" ", flush=True)
        pdf_bytes, nome, total_cli, taxa_cli = gerar_pdf_cliente(
            cliente, tickets_cli, DADOS, comp
        )
        print(f"{len(pdf_bytes)//1024} KB")
        enviar_telegram(pdf_bytes, nome, cliente, total_cli, taxa_cli)

    print("\n  Concluído.")


if __name__ == "__main__":
    main()
