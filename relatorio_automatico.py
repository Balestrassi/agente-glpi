"""
Relatório Semanal Automático — Recebe Mais
Calcula a semana anterior (seg-dom), busca chamados da entidade Recebe Mais
via API, gera PDF por cliente e envia ao grupo Telegram.
Roda toda segunda-feira às 09h via GitHub Actions.
"""

import os
import io
import requests
from datetime import datetime, timedelta
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, PageBreak
)

# ── Credenciais ───────────────────────────────────────────────────────
GLPI_URL         = os.environ.get("GLPI_URL",        "https://servicedesk.a7on.ai")
APP_TOKEN        = os.environ.get("GLPI_APP_TOKEN",  "")
USER_TOKEN       = os.environ.get("GLPI_USER_TOKEN", "")
GLPI_PROFILE_ID  = os.environ.get("GLPI_PROFILE_ID", "4")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN",  "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID","")

# ── Períodos ──────────────────────────────────────────────────────────
hoje          = datetime.utcnow()
seg_desta     = hoje - timedelta(days=hoje.weekday())

# Semana anterior (objeto do relatório)
seg_anterior  = seg_desta  - timedelta(days=7)
dom_anterior  = seg_desta  - timedelta(days=1)

# Semana retrasada (para comparativo)
seg_penultima = seg_anterior - timedelta(days=7)
dom_penultima = seg_anterior - timedelta(days=1)

DATA_INI      = seg_anterior.strftime("%Y-%m-%d 00:00:00")
DATA_FIM      = dom_anterior.strftime("%Y-%m-%d 23:59:59")
DATA_INI_ANT  = seg_penultima.strftime("%Y-%m-%d 00:00:00")
DATA_FIM_ANT  = dom_penultima.strftime("%Y-%m-%d 23:59:59")

PERIODO       = f"{seg_anterior.strftime('%d/%m/%Y')} a {dom_anterior.strftime('%d/%m/%Y')}"
PERIODO_ANT   = f"{seg_penultima.strftime('%d/%m')} a {dom_penultima.strftime('%d/%m/%Y')}"
PERIODO_LABEL = f"{seg_anterior.strftime('%d/%m')}–{dom_anterior.strftime('%d/%m/%Y')}"
OUTPUT        = f"Relatorio_RecebeMais_{seg_anterior.strftime('%d_%m')}_{dom_anterior.strftime('%d_%m_%Y')}.pdf"
HOJE_STR      = datetime.utcnow().strftime("%d/%m/%Y %H:%M")

# ── Cores ─────────────────────────────────────────────────────────────
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

# ── Estilos ───────────────────────────────────────────────────────────
def S(name, **kw): return ParagraphStyle(name, **kw)

TITULO_C = S("TC", fontName="Helvetica-Bold", fontSize=26, textColor=BRANCO,   alignment=TA_CENTER, leading=32)
SUB_C    = S("SC", fontName="Helvetica",      fontSize=11, textColor=AZUL_CLA, alignment=TA_CENTER, leading=16)
SECAO    = S("SE", fontName="Helvetica-Bold", fontSize=12, textColor=AZUL_ESC, spaceAfter=4, spaceBefore=12)
BODY     = S("BO", fontName="Helvetica",      fontSize=9,  textColor=PRETO,    leading=13)
NUM_G    = S("NG", fontName="Helvetica-Bold", fontSize=24, textColor=AZUL_MED, alignment=TA_CENTER)
NUM_L    = S("NL", fontName="Helvetica",      fontSize=8,  textColor=colors.HexColor("#7F8C8D"), alignment=TA_CENTER, leading=10)

# ── GLPI API ─────────────────────────────────────────────────────────
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
    # Forca o perfil Super-Admin: a conta usada aqui tambem tem um perfil
    # restrito por entidade (Tegma-only). Sem isso, initSession pode herdar
    # o ultimo perfil ativo da conta e esconder chamados de outras entidades.
    requests.post(f"{GLPI_URL}/apirest.php/changeActiveProfile", headers=_headers(tok),
                  json={"profiles_id": GLPI_PROFILE_ID}, timeout=15)
    return tok

def close_session(tok):
    requests.get(f"{GLPI_URL}/apirest.php/killSession", headers=_headers(tok), timeout=10)

def api_get(path, tok, params=None):
    r = requests.get(f"{GLPI_URL}/apirest.php/{path}",
                     headers=_headers(tok), params=params, timeout=30)
    if r.status_code in (200, 206):
        return r.json()
    return []

# ── Filtro e classificação Recebe Mais ────────────────────────────────
def eh_recebemai(ticket):
    entidade  = (ticket.get("entities_id")       or "").lower()
    categoria = (ticket.get("itilcategories_id") or "").lower()
    return "recebe mais" in entidade or "recebemai" in categoria or "recebe mais" in categoria

def produto(ticket):
    import html as _html
    ent    = _html.unescape((ticket.get("entities_id") or "").strip())
    partes = [p.strip() for p in ent.split(">") if p.strip()]
    if len(partes) < 3:
        return "Recebe Mais (Geral)"
    return partes[2]

def tipo(titulo):
    tl = titulo.lower()
    if "erro"    in tl:                          return "Erro"
    if "servico" in tl or "serviço" in tl:       return "Solicitação de Serviço"
    if "wildlife" in tl:                          return "Wildlife"
    if "duvida"  in tl or "dúvida"  in tl:       return "Dúvida / Orientação"
    if "melhoria" in tl:                          return "Solicitação de Melhoria"
    return "Outros"

# ── Critério de "resolvido" estável no tempo ───────────────────────────
def foi_resolvido_no_periodo(solvedate, data_fim):
    """Usa solvedate (nao o status ao vivo) para que 'Resolvidos'/'Taxa'
    nao mudem dependendo de quando o relatorio roda em relacao ao
    historico_metricas.py — ambos passam a bater sempre."""
    return bool(solvedate) and solvedate <= data_fim

# ── Busca de tickets ──────────────────────────────────────────────────
def buscar_tickets_range(tok, data_ini, data_fim):
    tickets = []
    offset  = 0
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
        passou_inicio = False
        for t in data:
            dc = t.get("date_creation") or t.get("date", "")
            if dc > data_fim:   continue
            if dc < data_ini:   passou_inicio = True; break
            if eh_recebemai(t): tickets.append(t)
        if passou_inicio:
            break
        cr = r.headers.get("Content-Range", "")
        if cr:
            if offset + 100 >= int(cr.split("/")[-1]): break
        else:
            break
        offset += 100
    return tickets

def buscar_tickets_periodo(tok):
    tickets = buscar_tickets_range(tok, DATA_INI, DATA_FIM)
    print(f"  Tickets Recebe Mais na semana: {len(tickets)}")
    return tickets

# ── Followups e técnicos ──────────────────────────────────────────────
def requester_numeric_id(tok, tid):
    try:
        t = api_get(f"Ticket/{tid}", tok)
        return t.get("users_id_recipient")
    except Exception:
        return None

def ultimo_tecnico_completo(tok, tid, requester_id):
    """Retorna (ultima_data, user_id, primeira_data) dos followups do técnico.
    primeira_data alimenta a métrica FRT (tempo de primeira resposta)."""
    try:
        followups = api_get(f"Ticket/{tid}/ITILFollowup", tok)
        if not isinstance(followups, list):
            return None, None, None
        tecnico = [
            f for f in followups
            if f.get("users_id") != requester_id
            and "Base de Conhecimento" not in (f.get("content") or "")
            and "Obrigado pelo seu contato" not in (f.get("content") or "")
        ]
        if not tecnico:
            return None, None, None
        ult, prim = tecnico[-1], tecnico[0]
        return (ult.get("date") or ult.get("date_creation"),
                ult.get("users_id"),
                prim.get("date") or prim.get("date_creation"))
    except Exception:
        return None, None, None

_nomes_cache = {}
def buscar_nome_usuario(tok, user_id):
    if not user_id:
        return "Desconhecido"
    if user_id in _nomes_cache:
        return _nomes_cache[user_id]
    try:
        u = api_get(f"User/{user_id}", tok)
        if isinstance(u, dict):
            nome = f"{u.get('firstname', '')} {u.get('realname', '')}".strip()
            _nomes_cache[user_id] = nome or f"Usuário {user_id}"
        else:
            _nomes_cache[user_id] = f"Usuário {user_id}"
    except Exception:
        _nomes_cache[user_id] = f"Usuário {user_id}"
    return _nomes_cache[user_id]

# ── Métricas de tempo ─────────────────────────────────────────────────
FMT   = "%Y-%m-%d %H:%M:%S"
H_INI = 8
H_FIM = 18
H_DIA = H_FIM - H_INI

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

def fmt_hu(h):
    if h < 1:      return f"{int(h*60)} min úteis"
    if h < H_DIA:  return f"{h:.1f} h úteis"
    dias = h / H_DIA
    if dias < 1.05: return f"{h:.1f} h úteis"
    return f"{dias:.1f} dias úteis"

def media_l(valores):
    return sum(valores) / len(valores) if valores else 0.0

def percentil(valores, p):
    """Percentil p (0-100) por posto mais próximo. P50 = mediana."""
    if not valores:
        return 0.0
    s = sorted(valores)
    k = round(p / 100 * (len(s) - 1))
    return s[max(0, min(len(s) - 1, k))]

def delta_str(atual, anterior):
    """Retorna string de variação ex: '+12%' ou '-5%'."""
    if anterior == 0:
        return "—"
    pct = round((atual - anterior) / anterior * 100)
    sinal = "+" if pct >= 0 else ""
    return f"{sinal}{pct}%"

def delta_pp(atual, anterior):
    """Variação em pontos percentuais."""
    diff = round(atual - anterior)
    sinal = "+" if diff >= 0 else ""
    return f"{sinal}{diff} pp"

# ── Helpers visuais ───────────────────────────────────────────────────
def section(text):
    return [Spacer(1, 6), Paragraph(text, SECAO),
            HRFlowable(width="100%", thickness=1.5, color=AZUL_MED, spaceAfter=8)]

def th_para(text):
    return Paragraph(text, ParagraphStyle("th", fontName="Helvetica-Bold",
                     fontSize=8, textColor=BRANCO, alignment=TA_CENTER))

def styled_tbl(header, rows, cols, hbg=AZUL_ESC):
    data = [[th_para(h) for h in header]]
    for row in rows:
        data.append([Paragraph(str(c), BODY) if not isinstance(c, Paragraph) else c
                     for c in row])
    t = Table(data, colWidths=cols, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0),  hbg),
        ("ROWBACKGROUNDS",(0, 1), (-1,-1), [BRANCO, CINZA_CLA]),
        ("GRID",          (0, 0), (-1,-1), 0.3, CINZA_BRD),
        ("VALIGN",        (0, 0), (-1,-1), "MIDDLE"),
        ("TOPPADDING",    (0, 0), (-1,-1), 4),
        ("BOTTOMPADDING", (0, 0), (-1,-1), 4),
        ("LEFTPADDING",   (0, 0), (-1,-1), 6),
    ]))
    return t

URG_MAP = {1:"Muito Baixa", 2:"Baixa", 3:"Média", 4:"Alta", 5:"Muito Alta"}

def urg_p(num):
    label = URG_MAP.get(num, "?")
    cor   = {"Muito Alta":"#E74C3C","Alta":"#F39C12","Média":"#2E6DA4","Baixa":"#27AE60"}.get(label,"#2C3E50")
    return Paragraph(f'<font color="{cor}"><b>{label}</b></font>', BODY)

STATUS_NOME = {1:"Novo", 2:"Em Andamento", 4:"Pendente", 5:"Resolvido", 6:"Fechado"}

# ── Header/Footer ─────────────────────────────────────────────────────
def on_page(canvas, doc):
    canvas.saveState()
    w, h = A4
    canvas.setFillColor(AZUL_ESC)
    canvas.rect(0, h-1.2*cm, w, 1.2*cm, fill=1, stroke=0)
    canvas.setFont("Helvetica-Bold", 9)
    canvas.setFillColor(BRANCO)
    canvas.drawString(1.5*cm, h-0.85*cm, f"Suporte RM — Relatório Semanal Recebe Mais | {PERIODO}")
    canvas.drawRightString(w-1.5*cm, h-0.85*cm, f"Gerado em {HOJE_STR}")
    canvas.setFillColor(AZUL_ESC)
    canvas.rect(0, 0, w, 0.8*cm, fill=1, stroke=0)
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(BRANCO)
    canvas.drawString(1.5*cm, 0.27*cm, "Dados extraídos do GLPI | Filtro: entidade Recebe Mais + data de criação")
    canvas.drawRightString(w-1.5*cm, 0.27*cm, f"Página {doc.page}")
    canvas.restoreState()

def on_first(canvas, doc):
    canvas.saveState()
    w, h = A4
    canvas.setFillColor(AZUL_ESC)
    canvas.rect(0, 0, w, 0.8*cm, fill=1, stroke=0)
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(BRANCO)
    canvas.drawString(1.5*cm, 0.27*cm, "Suporte RM — Relatório Semanal Recebe Mais")
    canvas.restoreState()

# ── Seções do PDF ─────────────────────────────────────────────────────
def capa(story, total, taxa):
    story.append(Spacer(1, 3*cm))
    t = Table([[Paragraph("RELATÓRIO SEMANAL<br/>RECEBE MAIS", TITULO_C)]],
              colWidths=[17*cm], rowHeights=[3.5*cm])
    t.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,-1),AZUL_ESC),
        ("ALIGN",(0,0),(-1,-1),"CENTER"),
        ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
        ("TOPPADDING",(0,0),(-1,-1),20),
        ("BOTTOMPADDING",(0,0),(-1,-1),20),
    ]))
    story.append(t)
    story.append(Spacer(1, 0.6*cm))
    sub = Table([[Paragraph(f"Período: {PERIODO}", SUB_C),
                  Paragraph(f"Gerado em: {HOJE_STR}", SUB_C)]],
                colWidths=[8.5*cm,8.5*cm], rowHeights=[1*cm])
    sub.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,-1),AZUL_MED),
        ("ALIGN",(0,0),(-1,-1),"CENTER"),
        ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
    ]))
    story.append(sub)
    story.append(Spacer(1, 1*cm))
    story.append(Paragraph(
        f"Chamados filtrados por <b>entidade Recebe Mais</b> e "
        f"<b>data de criação</b> entre "
        f"{seg_anterior.strftime('%d/%m')} e {dom_anterior.strftime('%d/%m/%Y')}. "
        f"Total: <b>{total} chamados</b> | Taxa de resolução: <b>{taxa}%</b>",
        ParagraphStyle("tag", fontName="Helvetica", fontSize=10,
                       textColor=colors.HexColor("#7F8C8D"), alignment=TA_CENTER, leading=16)
    ))
    story.append(PageBreak())


def visao_geral(story, total, resol, taxa, por_status, por_prod, por_tipo_d,
                nao_resolvidos, comp):
    story += section("Visão Geral da Semana")

    kpi_data = [
        [Paragraph(str(total), NUM_G),
         Paragraph(str(resol), NUM_G),
         Paragraph(str(taxa)+"%", ParagraphStyle("pct",fontName="Helvetica-Bold",
                   fontSize=24,textColor=VERDE,alignment=TA_CENTER)),
         Paragraph(str(len(nao_resolvidos)), ParagraphStyle("ab",fontName="Helvetica-Bold",
                   fontSize=24,textColor=AMARELO,alignment=TA_CENTER))],
        [Paragraph("Total Abertos",NUM_L), Paragraph("Resolvidos / Fechados",NUM_L),
         Paragraph("Taxa de Resolução",NUM_L), Paragraph("Ainda em Aberto",NUM_L)],
    ]
    kpi = Table(kpi_data, colWidths=[4.25*cm]*4, rowHeights=[1.4*cm, 0.6*cm])
    kpi.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,-1),CINZA_CLA),
        ("ALIGN",(0,0),(-1,-1),"CENTER"),
        ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
        ("BOX",(0,0),(-1,-1),0.5,CINZA_BRD),
        ("LINEAFTER",(0,0),(-3,-1),0.5,CINZA_BRD),
    ]))
    story.append(kpi)
    story.append(Spacer(1, 0.4*cm))

    # Comparativo com semana anterior
    if comp:
        def cor_delta(v):
            if "+" in v: return "#27AE60"
            if "-" in v: return "#E74C3C"
            return "#7F8C8D"

        d_total = delta_str(total, comp["total"])
        d_taxa  = delta_pp(taxa, comp["taxa"])
        nota_comp = ParagraphStyle("nc", fontName="Helvetica", fontSize=8,
                                   textColor=colors.HexColor("#7F8C8D"),
                                   alignment=TA_CENTER, leading=12)

        comp_data = [[
            Paragraph(
                f'Semana anterior ({PERIODO_ANT}): '
                f'<b>{comp["total"]}</b> chamados · '
                f'<b>{comp["taxa"]}%</b> resolvidos · '
                f'Variação volume: <font color="{cor_delta(d_total)}"><b>{d_total}</b></font> · '
                f'Variação taxa: <font color="{cor_delta(d_taxa)}"><b>{d_taxa}</b></font>',
                nota_comp
            )
        ]]
        comp_tbl = Table(comp_data, colWidths=[17*cm])
        comp_tbl.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,-1),CINZA_CLA),
            ("BOX",(0,0),(-1,-1),0.5,CINZA_BRD),
            ("TOPPADDING",(0,0),(-1,-1),6),
            ("BOTTOMPADDING",(0,0),(-1,-1),6),
            ("LEFTPADDING",(0,0),(-1,-1),10),
        ]))
        story.append(comp_tbl)
        story.append(Spacer(1, 0.3*cm))

    story += section("Volume por Status")
    status_order = [(1,"Novo",VERMELHO),(2,"Em Andamento",AZUL_MED),
                    (4,"Pendente",AMARELO),(5,"Resolvido",VERDE),
                    (6,"Fechado",colors.HexColor("#7F8C8D"))]
    rows = []
    for st, nome, cor in status_order:
        qtd = por_status.get(st, 0)
        if qtd == 0: continue
        pct   = round(qtd/total*100)
        barra = "█"*int(pct/5) + "░"*(20-int(pct/5))
        c_hex = "#%02x%02x%02x" % (int(cor.red*255), int(cor.green*255), int(cor.blue*255))
        rows.append((Paragraph(f'<font color="{c_hex}"><b>{nome}</b></font>', BODY),
                     str(qtd), f"{pct}%", barra))
    story.append(styled_tbl(["Status","Qtd.","%","Proporção"], rows, [4*cm,2*cm,2*cm,9*cm]))
    story.append(Paragraph(
        "Nota: esta tabela reflete o status atual no GLPI no momento da geração do relatório. "
        "O KPI \"Resolvidos / Fechados\" acima considera a data de solução (solvedate) até o fim "
        "do período, por isso pode divergir levemente.",
        ParagraphStyle("statusnote", fontName="Helvetica-Oblique", fontSize=7.5,
                       textColor=colors.HexColor("#7F8C8D"), leading=11)
    ))

    story += section("Volume por Cliente")
    rows_p = []
    for p, qtd in sorted(por_prod.items(), key=lambda x: x[1], reverse=True):
        pct = round(qtd/total*100)
        rows_p.append((p, str(qtd), f"{pct}%", "█"*int(pct/5)+"░"*(20-int(pct/5))))
    story.append(styled_tbl(["Cliente","Qtd.","%","Proporção"], rows_p, [5.5*cm,2*cm,2*cm,7.5*cm]))

    story += section("Volume por Tipo de Chamado")
    tipo_order = ["Erro","Solicitação de Serviço","Wildlife","Dúvida / Orientação",
                  "Solicitação de Melhoria","Outros"]
    rows_t = []
    for tp in tipo_order:
        qtd = por_tipo_d.get(tp, 0)
        if qtd == 0: continue
        pct = round(qtd/total*100)
        rows_t.append((tp, str(qtd), f"{pct}%", "█"*int(pct/5)+"░"*(20-int(pct/5))))
    story.append(styled_tbl(["Tipo","Qtd.","%","Proporção"], rows_t, [5.5*cm,2*cm,2*cm,7.5*cm]))


def tempo_medio(story, tempos, tp_prod_ciclo, tp_prod_tecnico, tp_tipo_ciclo, tp_tipo_tecnico,
                TEMPO_GERAL_CICLO, TEMPO_GERAL_TECNICO, TEMPO_PROD_CICLO, TEMPO_PROD_TECNICO,
                TEMPO_TIPO_CICLO, TEMPO_TIPO_TECNICO):
    story.append(PageBreak())
    story += section("Tempo Médio de Resolução — Duas Métricas")

    nota = Table([[Paragraph(
        f"Calculado sobre {len(tempos)} chamados resolvidos/fechados criados na semana. "
        "<b>Tempo Técnico</b>: criação → último comentário do técnico. "
        "<b>Tempo Total</b>: criação → última atualização do chamado. "
        "Ambos em horas úteis: seg–sex, 08h–18h (10h/dia).",
        ParagraphStyle("n",fontName="Helvetica-Oblique",fontSize=8,
                       textColor=colors.HexColor("#7F8C8D"),leading=12)
    )]], colWidths=[17*cm])
    nota.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,-1),CINZA_CLA),
                               ("BOX",(0,0),(-1,-1),0.5,CINZA_BRD),
                               ("TOPPADDING",(0,0),(-1,-1),6),
                               ("BOTTOMPADDING",(0,0),(-1,-1),6),
                               ("LEFTPADDING",(0,0),(-1,-1),10)]))
    story.append(nota)
    story.append(Spacer(1, 0.4*cm))

    duo = Table([[
        Paragraph(fmt_hu(TEMPO_GERAL_TECNICO),
                  ParagraphStyle("tgt",fontName="Helvetica-Bold",fontSize=26,
                                 textColor=VERDE,alignment=TA_CENTER)),
        Paragraph(fmt_hu(TEMPO_GERAL_CICLO),
                  ParagraphStyle("tgc",fontName="Helvetica-Bold",fontSize=26,
                                 textColor=AZUL_MED,alignment=TA_CENTER)),
    ],[
        Paragraph("<b>Tempo Técnico Médio</b><br/>"
                  "<font color='#7F8C8D' size='8'>criação → último comentário do técnico</font>",
                  ParagraphStyle("lab1",fontName="Helvetica",fontSize=9,
                                 alignment=TA_CENTER,leading=13)),
        Paragraph("<b>Tempo Total Médio de Ciclo</b><br/>"
                  "<font color='#7F8C8D' size='8'>criação → última atualização do chamado</font>",
                  ParagraphStyle("lab2",fontName="Helvetica",fontSize=9,
                                 alignment=TA_CENTER,leading=13)),
    ]], colWidths=[8.5*cm, 8.5*cm], rowHeights=[2*cm, 1*cm])
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
    story.append(Spacer(1, 0.6*cm))

    story += section("Tempo Médio por Cliente")
    rows_p = []
    for prod in sorted(TEMPO_PROD_TECNICO, key=lambda p: len(tp_prod_tecnico[p]), reverse=True):
        ht = TEMPO_PROD_TECNICO[prod]
        hc = TEMPO_PROD_CICLO.get(prod, ht)
        n  = len(tp_prod_tecnico[prod])
        rows_p.append((prod, fmt_hu(ht), fmt_hu(hc), str(n)))
    story.append(styled_tbl(["Cliente","Tempo Técnico","Tempo Total","Qtd."],
                             rows_p, [6*cm,4*cm,4*cm,3*cm]))
    story.append(Spacer(1, 0.5*cm))

    story += section("Tempo Médio por Tipo de Chamado")
    tipo_ord = ["Solicitação de Serviço","Dúvida / Orientação","Wildlife",
                "Erro","Solicitação de Melhoria","Outros"]
    rows_t = []
    for tp in tipo_ord:
        if tp not in TEMPO_TIPO_TECNICO: continue
        ht, n = TEMPO_TIPO_TECNICO[tp]
        hc, _ = TEMPO_TIPO_CICLO.get(tp, (ht, n))
        obs   = " *(1)" if n == 1 else ""
        rows_t.append((tp, fmt_hu(ht)+obs, fmt_hu(hc)+obs, str(n)))
    story.append(styled_tbl(["Tipo","Tempo Técnico","Tempo Total","Qtd."],
                             rows_t, [6*cm,4*cm,4*cm,3*cm]))


def ranking_tecnicos(story, ranking):
    """Ranking de técnicos por chamados resolvidos na semana."""
    story.append(PageBreak())
    story += section(f"Ranking de Técnicos — Semana {PERIODO_LABEL}")

    nota = Table([[Paragraph(
        "Considera chamados Recebe Mais resolvidos/fechados criados na semana. "
        "Técnico identificado pelo último comentário no chamado (excluindo respostas automáticas). "
        "Tempos em horas úteis: seg–sex, 08h–18h.",
        ParagraphStyle("rn",fontName="Helvetica-Oblique",fontSize=8,
                       textColor=colors.HexColor("#7F8C8D"),leading=12)
    )]], colWidths=[17*cm])
    nota.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,-1),CINZA_CLA),
                               ("BOX",(0,0),(-1,-1),0.5,CINZA_BRD),
                               ("TOPPADDING",(0,0),(-1,-1),6),
                               ("BOTTOMPADDING",(0,0),(-1,-1),6),
                               ("LEFTPADDING",(0,0),(-1,-1),10)]))
    story.append(nota)
    story.append(Spacer(1, 0.4*cm))

    # Ordena por chamados resolvidos desc
    ordenado = sorted(ranking.items(), key=lambda x: x[1]["count"], reverse=True)

    medalhas = ["🥇", "🥈", "🥉"]
    rows = []
    for i, (uid, dados) in enumerate(ordenado):
        pos   = medalhas[i] if i < 3 else f"{i+1}º"
        nome  = dados["nome"]
        count = dados["count"]
        ht    = dados["tempo_tecnico_avg"]
        hc    = dados["tempo_ciclo_avg"]
        rows.append((
            Paragraph(f"<b>{pos}</b>", BODY),
            Paragraph(f"<b>{nome}</b>", BODY),
            Paragraph(f"<b>{count}</b>", ParagraphStyle("bold_c", fontName="Helvetica-Bold",
                      fontSize=9, textColor=AZUL_MED, alignment=TA_CENTER)),
            fmt_hu(ht),
            fmt_hu(hc),
        ))

    story.append(styled_tbl(
        ["#", "Técnico", "Resolvidos", "T. Técnico Médio", "T. Ciclo Médio"],
        rows, [1.2*cm, 6*cm, 2.5*cm, 3.65*cm, 3.65*cm],
        hbg=VERDE,
    ))


def em_aberto(story, nao_resolvidos):
    story.append(PageBreak())
    story += section(f"Chamados da Semana Ainda em Aberto ({len(nao_resolvidos)})")

    aviso = Table([[Paragraph(
        f"Os {len(nao_resolvidos)} chamados abaixo foram criados na semana {PERIODO_LABEL} "
        "e não tinham data de solução registrada até o fim do período (podem já ter sido "
        "resolvidos após a geração deste relatório).",
        ParagraphStyle("av",fontName="Helvetica",fontSize=9,textColor=AMARELO,leading=13)
    )]], colWidths=[17*cm])
    aviso.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,-1),colors.HexColor("#FEF9E7")),
                                ("BOX",(0,0),(-1,-1),1,AMARELO),
                                ("TOPPADDING",(0,0),(-1,-1),8),
                                ("BOTTOMPADDING",(0,0),(-1,-1),8),
                                ("LEFTPADDING",(0,0),(-1,-1),10)]))
    story.append(aviso)
    story.append(Spacer(1, 0.4*cm))

    rows = []
    for tid, titulo, criacao, urg, st in sorted(nao_resolvidos, key=lambda x: x[2]):
        dt = datetime.strptime(criacao, FMT)
        rows.append((str(tid),
                     titulo[:52]+("…" if len(titulo)>52 else ""),
                     dt.strftime("%d/%m %H:%M"),
                     urg_p(urg),
                     Paragraph(st, BODY)))
    story.append(styled_tbl(
        ["ID","Título","Criação","Urgência","Status"],
        rows, [1.5*cm,7.5*cm,2.5*cm,3*cm,2.5*cm],
        hbg=AMARELO))

# ── Distribuição dos tempos (média × mediana × P90) ──────────────────
def distribuicao(story, tempos, frt_list):
    """Média sozinha esconde outliers — mediana é o caso típico e P90 o
    pior cenário usual. FRT = tempo até a primeira resposta do técnico."""
    story += section("Distribuição dos Tempos (média × mediana × P90)")
    ciclos   = [hc for hc, _, _, _ in tempos]
    tecnicos = [ht for _, ht, _, _ in tempos]
    rows = [
        ("Tempo Técnico", fmt_hu(media_l(tecnicos)),
         fmt_hu(percentil(tecnicos, 50)), fmt_hu(percentil(tecnicos, 90))),
        ("Tempo Total",   fmt_hu(media_l(ciclos)),
         fmt_hu(percentil(ciclos, 50)),   fmt_hu(percentil(ciclos, 90))),
    ]
    if frt_list:
        rows.append(("Primeira Resposta (FRT)", fmt_hu(media_l(frt_list)),
                     fmt_hu(percentil(frt_list, 50)), fmt_hu(percentil(frt_list, 90))))
    story.append(styled_tbl(["Métrica", "Média", "Mediana (P50)", "P90"],
                             rows, [5.5*cm, 3.8*cm, 3.8*cm, 3.8*cm]))
    story.append(Spacer(1, 4))
    story.append(Paragraph(
        "Leitura: a <b>mediana</b> é o caso típico (metade resolve mais rápido); "
        "o <b>P90</b> é o pior cenário usual (só 10% demoram mais). "
        "<b>FRT</b> = criação até o primeiro comentário do técnico.",
        S("leg", fontName="Helvetica-Oblique", fontSize=8,
          textColor=colors.HexColor("#7F8C8D"), leading=12)))

# ── Reaberturas do período (métrica opcional, via Google Sheets) ──────
def contar_reaberturas_periodo():
    """Conta as reaberturas registradas pelo agente_reaberturas na aba
    'Reaberturas' dentro do período do relatório. Retorna None se a
    planilha não estiver configurada — a seção simplesmente não aparece."""
    gj  = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
    sid = os.environ.get("SPREADSHEET_ID", "")
    if not gj or not sid:
        return None
    try:
        import json as _json
        import gspread
        from google.oauth2.service_account import Credentials
        creds = Credentials.from_service_account_info(
            _json.loads(gj),
            scopes=["https://www.googleapis.com/auth/spreadsheets",
                    "https://www.googleapis.com/auth/drive"],
        )
        gc   = gspread.authorize(creds)
        ws   = gc.open_by_key(sid).worksheet("Reaberturas")
        rows = ws.get_all_values()[1:]  # pula cabeçalho
        ini, fim = DATA_INI[:10], DATA_FIM[:10]
        return sum(1 for r in rows if r and r[0] and ini <= r[0][:10] <= fim)
    except Exception as e:
        print(f"  [!] Métrica de reaberturas indisponível: {e}")
        return None

# ── Resumo executivo com IA (opcional) ────────────────────────────────
def gerar_resumo_ia(contexto: str) -> str:
    """Gera parágrafo de análise executiva via API do Claude.
    Opcional: requer o secret ANTHROPIC_API_KEY e o pacote 'anthropic'.
    Sem a chave, retorna vazio e o relatório sai sem a seção."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return ""
    try:
        import anthropic
    except ImportError:
        print("  [!] Pacote 'anthropic' não instalado — resumo IA pulado.")
        return ""
    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=600,
            system=(
                "Você é um analista de operações de suporte técnico. A partir das "
                "métricas semanais fornecidas, escreva um resumo executivo de 3 a 5 "
                "frases, em português, voltado a gerentes. Destaque a variação vs a "
                "semana anterior, onde o volume se concentrou e pontos de atenção. "
                "Tom objetivo e factual. Use apenas os números fornecidos — não "
                "invente dados. Responda com um único parágrafo corrido, sem "
                "markdown, sem listas e sem título."
            ),
            messages=[{"role": "user", "content": contexto}],
        )
        texto = "".join(b.text for b in resp.content if b.type == "text").strip()
        if texto:
            print("  Resumo executivo IA gerado.")
        return texto
    except Exception as e:
        print(f"  [!] Resumo IA indisponível: {e}")
        return ""

def resumo_executivo(story, texto):
    story += section("Resumo Executivo")
    box = Table([[Paragraph(texto, S("re", fontName="Helvetica", fontSize=9.5,
                                     textColor=PRETO, leading=14))]],
                colWidths=[17*cm])
    box.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), colors.HexColor("#EBF5FB")),
        ("BOX",           (0, 0), (-1, -1), 1, AZUL_MED),
        ("TOPPADDING",    (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("LEFTPADDING",   (0, 0), (-1, -1), 12),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 12),
    ]))
    story.append(box)
    story.append(Paragraph(
        "Análise gerada por IA (Claude) a partir das métricas do período.",
        S("ia", fontName="Helvetica-Oblique", fontSize=7.5,
          textColor=colors.HexColor("#7F8C8D"))))
    story.append(Spacer(1, 8))

# ── Telegram ─────────────────────────────────────────────────────────
def enviar_telegram(pdf_bytes):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("  [!] Credenciais Telegram não configuradas, pulando envio.")
        return
    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument",
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "caption": (
                f"📊 *Relatório Semanal — Recebe Mais*\n"
                f"Período: {PERIODO}\n"
                f"Gerado automaticamente via GLPI"
            ),
            "parse_mode": "Markdown",
        },
        files={"document": (OUTPUT, pdf_bytes, "application/pdf")},
        timeout=60,
    )
    r.raise_for_status()
    print(f"  PDF enviado ao Telegram com sucesso.")

# ── Main ──────────────────────────────────────────────────────────────
def main():
    print(f"[{HOJE_STR}] Relatório Automático Recebe Mais — período: {PERIODO}")

    # Busca tickets da semana atual (relatório)
    tok = get_session()
    try:
        tickets_raw = buscar_tickets_periodo(tok)
    finally:
        close_session(tok)

    if not tickets_raw:
        print("  Nenhum ticket Recebe Mais encontrado. Encerrando.")
        return

    # Busca métricas da semana anterior para comparativo
    print(f"  Buscando comparativo ({PERIODO_ANT})...")
    tok_ant = get_session()
    try:
        tickets_ant = buscar_tickets_range(tok_ant, DATA_INI_ANT, DATA_FIM_ANT)
        total_ant   = len(tickets_ant)
        resol_ant   = sum(1 for t in tickets_ant
                          if foi_resolvido_no_periodo(t.get("solvedate") or "", DATA_FIM_ANT))
        taxa_ant    = round(resol_ant / total_ant * 100) if total_ant else 0
        comp        = {"total": total_ant, "resol": resol_ant, "taxa": taxa_ant}
        print(f"  Semana anterior: {total_ant} tickets, {taxa_ant}% resolvidos")
    finally:
        close_session(tok_ant)

    # Monta lista de tickets
    TICKETS = []
    for t in tickets_raw:
        TICKETS.append((
            t.get("id"),
            t.get("name", ""),
            t.get("date_creation") or t.get("date", ""),
            t.get("date_mod", ""),
            t.get("urgency", 3),
            t.get("status", 1),
            produto(t),
            t.get("solvedate") or "",
        ))

    # Busca followups para todos os resolvidos (técnico + ID do técnico)
    resolvidos = [(tid, criacao, prod) for tid, _, criacao, _, _, st, prod, solve in TICKETS
                  if foi_resolvido_no_periodo(solve, DATA_FIM)]
    print(f"  Buscando followups de {len(resolvidos)} tickets resolvidos...")
    tok2 = get_session()
    try:
        DADOS    = {}
        RANKING  = {}   # {user_id: {nome, count, tempos}}
        for i, (tid, criacao, prod) in enumerate(resolvidos):
            req_id                   = requester_numeric_id(tok2, tid)
            ult_data, uid, prim_data = ultimo_tecnico_completo(tok2, tid, req_id)
            DADOS[tid] = {"criacao": criacao, "ultimo_tecnico": ult_data,
                          "tecnico_id": uid, "primeiro_tecnico": prim_data}
            if uid:
                if uid not in RANKING:
                    RANKING[uid] = {"nome": "", "count": 0,
                                    "tempos_tecnico": [], "tempos_ciclo": []}
                RANKING[uid]["count"] += 1
            if (i+1) % 10 == 0:
                print(f"    {i+1}/{len(resolvidos)} processados...")

        # Resolve nomes dos técnicos
        for uid in RANKING:
            RANKING[uid]["nome"] = buscar_nome_usuario(tok2, uid)
    finally:
        close_session(tok2)

    # Calcula métricas
    por_status = {}
    por_prod   = {}
    por_tipo_d = {}
    tempos     = []
    frt_list   = []   # tempo até a primeira resposta do técnico (horas úteis)
    nao_resolvidos = []

    for tid, titulo, criacao, atualizacao, urg, st, prod, solve in TICKETS:
        por_status[st] = por_status.get(st, 0) + 1
        tp = tipo(titulo)
        por_prod[prod]   = por_prod.get(prod, 0) + 1
        por_tipo_d[tp]   = por_tipo_d.get(tp, 0) + 1
        resolvido = foi_resolvido_no_periodo(solve, DATA_FIM)
        if resolvido:
            d          = DADOS.get(tid, {})
            dt_criacao = d.get("criacao") or criacao
            h_ciclo    = horas_uteis(dt_criacao, atualizacao)
            ult_tec    = d.get("ultimo_tecnico")
            h_tecnico  = horas_uteis(dt_criacao, ult_tec) if ult_tec else h_ciclo
            tempos.append((h_ciclo, h_tecnico, prod, tp))
            prim_tec = d.get("primeiro_tecnico")
            if prim_tec:
                frt_list.append(horas_uteis(dt_criacao, prim_tec))
            uid = d.get("tecnico_id")
            if uid and uid in RANKING:
                RANKING[uid]["tempos_tecnico"].append(h_tecnico)
                RANKING[uid]["tempos_ciclo"].append(h_ciclo)
        else:
            nao_resolvidos.append((tid, titulo, criacao, urg, STATUS_NOME.get(st, str(st))))

    TOTAL      = len(TICKETS)
    RESOL      = sum(1 for *_, solve in TICKETS if foi_resolvido_no_periodo(solve, DATA_FIM))
    TAXA_RESOL = round(RESOL / TOTAL * 100) if TOTAL else 0

    TEMPO_GERAL_CICLO   = sum(hc for hc, *_ in tempos) / len(tempos) if tempos else 0
    TEMPO_GERAL_TECNICO = sum(ht for _, ht, *_ in tempos) / len(tempos) if tempos else 0

    tp_prod_ciclo = {}; tp_prod_tecnico = {}
    for hc, ht, prod, tp in tempos:
        tp_prod_ciclo.setdefault(prod,   []).append(hc)
        tp_prod_tecnico.setdefault(prod, []).append(ht)
    TEMPO_PROD_CICLO   = {p: sum(v)/len(v) for p, v in tp_prod_ciclo.items()}
    TEMPO_PROD_TECNICO = {p: sum(v)/len(v) for p, v in tp_prod_tecnico.items()}

    tp_tipo_ciclo = {}; tp_tipo_tecnico = {}
    for hc, ht, prod, tp in tempos:
        tp_tipo_ciclo.setdefault(tp,   []).append(hc)
        tp_tipo_tecnico.setdefault(tp, []).append(ht)
    TEMPO_TIPO_CICLO   = {t: (sum(v)/len(v), len(v)) for t, v in tp_tipo_ciclo.items()}
    TEMPO_TIPO_TECNICO = {t: (sum(v)/len(v), len(v)) for t, v in tp_tipo_tecnico.items()}

    # Prepara ranking final com médias de tempo
    ranking_final = {}
    for uid, dados in RANKING.items():
        if dados["count"] == 0:
            continue
        tt = dados["tempos_tecnico"]
        tc = dados["tempos_ciclo"]
        ranking_final[uid] = {
            "nome":               dados["nome"],
            "count":              dados["count"],
            "tempo_tecnico_avg":  sum(tt)/len(tt) if tt else 0,
            "tempo_ciclo_avg":    sum(tc)/len(tc) if tc else 0,
        }

    print(f"  Total: {TOTAL} | Resolvidos: {RESOL} ({TAXA_RESOL}%)")
    print(f"  Técnicos identificados: {len(ranking_final)}")
    print(f"  Tempo técnico médio: {fmt_hu(TEMPO_GERAL_TECNICO)}")
    print(f"  Tempo ciclo médio:   {fmt_hu(TEMPO_GERAL_CICLO)}")

    # Reaberturas da semana (opcional — depende da planilha Google)
    n_reab = contar_reaberturas_periodo()
    if n_reab is not None:
        print(f"  Reaberturas no período: {n_reab}")

    # Resumo executivo com IA (opcional — só roda se ANTHROPIC_API_KEY existir)
    top_prod = sorted(por_prod.items(),   key=lambda x: -x[1])[:4]
    top_tipo = sorted(por_tipo_d.items(), key=lambda x: -x[1])[:4]
    contexto_ia = (
        f"Período: {PERIODO}. Total de chamados: {TOTAL}. "
        f"Resolvidos/fechados: {RESOL} (taxa {TAXA_RESOL}%). "
        f"Ainda em aberto: {len(nao_resolvidos)}. "
        f"Semana anterior ({PERIODO_ANT}): {comp['total']} chamados, taxa {comp['taxa']}%. "
        f"Volume por cliente: {', '.join(f'{n}: {q}' for n, q in top_prod)}. "
        f"Volume por tipo: {', '.join(f'{n}: {q}' for n, q in top_tipo)}. "
        f"Tempo técnico: média {fmt_hu(TEMPO_GERAL_TECNICO)}, "
        f"mediana {fmt_hu(percentil([ht for _, ht, _, _ in tempos], 50)) if tempos else 'n/d'}, "
        f"P90 {fmt_hu(percentil([ht for _, ht, _, _ in tempos], 90)) if tempos else 'n/d'}. "
        f"Tempo total de ciclo: média {fmt_hu(TEMPO_GERAL_CICLO)}. "
        + (f"Primeira resposta (FRT): média {fmt_hu(media_l(frt_list))}, "
           f"mediana {fmt_hu(percentil(frt_list, 50))}. " if frt_list else "")
        + (f"Reaberturas na semana: {n_reab} "
           f"(taxa {round(n_reab / RESOL * 100)}% dos resolvidos). "
           if n_reab is not None and RESOL else "")
    )
    resumo_ia = gerar_resumo_ia(contexto_ia)

    # Gera PDF
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=1.5*cm, rightMargin=1.5*cm,
                            topMargin=1.8*cm, bottomMargin=1.2*cm)
    story = []
    capa(story, TOTAL, TAXA_RESOL)
    if resumo_ia:
        resumo_executivo(story, resumo_ia)
    visao_geral(story, TOTAL, RESOL, TAXA_RESOL, por_status, por_prod,
                por_tipo_d, nao_resolvidos, comp)
    if n_reab is not None:
        taxa_reab = f" ({round(n_reab / RESOL * 100)}% dos resolvidos)" if RESOL else ""
        story.append(Spacer(1, 4))
        story.append(Paragraph(
            f"<b>Reaberturas na semana: {n_reab}</b>{taxa_reab} — chamados que "
            "voltaram a aberto após serem resolvidos. Reabertura é o principal "
            "termômetro de qualidade da solução.",
            S("reab", fontName="Helvetica", fontSize=9, textColor=PRETO, leading=13)))
    if tempos:
        tempo_medio(story, tempos, tp_prod_ciclo, tp_prod_tecnico,
                    tp_tipo_ciclo, tp_tipo_tecnico,
                    TEMPO_GERAL_CICLO, TEMPO_GERAL_TECNICO,
                    TEMPO_PROD_CICLO, TEMPO_PROD_TECNICO,
                    TEMPO_TIPO_CICLO, TEMPO_TIPO_TECNICO)
        distribuicao(story, tempos, frt_list)
    if ranking_final:
        ranking_tecnicos(story, ranking_final)
    if nao_resolvidos:
        em_aberto(story, nao_resolvidos)
    doc.build(story, onFirstPage=on_first, onLaterPages=on_page)

    pdf_bytes = buf.getvalue()
    print(f"  PDF gerado: {len(pdf_bytes)//1024} KB")
    enviar_telegram(pdf_bytes)

if __name__ == "__main__":
    main()
