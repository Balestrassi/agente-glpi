"""
TESTE — Relatório Semanal Recebe Mais
Mesma lógica do relatorio_automatico.py, mas:
  - Filtra apenas chamados da entidade Recebe Mais
  - Salva PDF localmente (não envia ao Telegram)
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

# ── Credenciais (via ambiente/Secrets — nunca hardcoded) ──────────────
GLPI_URL   = os.environ.get("GLPI_URL", "https://servicedesk.a7on.ai")
APP_TOKEN  = os.environ["GLPI_APP_TOKEN"]
USER_TOKEN = os.environ["GLPI_USER_TOKEN"]

# ── Período: semana anterior seg-dom ─────────────────────────────────
hoje         = datetime.utcnow()
seg_desta    = hoje - timedelta(days=hoje.weekday())
seg_anterior = seg_desta - timedelta(days=7)
dom_anterior = seg_desta - timedelta(days=1)

DATA_INI = seg_anterior.strftime("%Y-%m-%d 00:00:00")
DATA_FIM = dom_anterior.strftime("%Y-%m-%d 23:59:59")
PERIODO  = f"{seg_anterior.strftime('%d/%m/%Y')} a {dom_anterior.strftime('%d/%m/%Y')}"
PERIODO_LABEL = f"{seg_anterior.strftime('%d/%m')}–{dom_anterior.strftime('%d/%m/%Y')}"
OUTPUT   = f"Relatorio_RecebeMais_{seg_anterior.strftime('%d_%m')}_{dom_anterior.strftime('%d_%m_%Y')}.pdf"
HOJE_STR = datetime.utcnow().strftime("%d/%m/%Y %H:%M")

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
    return r.json()["session_token"]

def close_session(tok):
    requests.get(f"{GLPI_URL}/apirest.php/killSession", headers=_headers(tok), timeout=10)

def api_get(path, tok, params=None):
    r = requests.get(f"{GLPI_URL}/apirest.php/{path}",
                     headers=_headers(tok), params=params, timeout=30)
    if r.status_code in (200, 206):
        return r.json()
    return []

# ── Filtro Recebe Mais (por entidade ou categoria) ────────────────────
def eh_recebemai(ticket):
    entidade  = (ticket.get("entities_id")       or "").lower()
    categoria = (ticket.get("itilcategories_id") or "").lower()
    return "recebe mais" in entidade or "recebemai" in categoria or "recebe mais" in categoria

# ── Busca de tickets ──────────────────────────────────────────────────
def buscar_tickets_periodo(tok):
    """Busca todos os tickets Recebe Mais criados na semana anterior."""
    tickets = []
    offset  = 0
    total_bruto = 0
    while True:
        r = requests.get(
            f"{GLPI_URL}/apirest.php/Ticket",
            headers=_headers(tok),
            params={
                "expand_dropdowns": True,
                "range": f"{offset}-{offset+99}",
                "sort": "date_creation",
                "order": "DESC",
            },
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
            if dc > DATA_FIM:
                continue
            if dc < DATA_INI:
                passou_inicio = True
                break
            total_bruto += 1
            if eh_recebemai(t):
                tickets.append(t)

        if passou_inicio:
            break

        content_range = r.headers.get("Content-Range", "")
        if content_range:
            total = int(content_range.split("/")[-1])
            if offset + 100 >= total:
                break
        else:
            break

        offset += 100

    print(f"  Tickets no período (total): {total_bruto}")
    print(f"  Tickets Recebe Mais filtrados: {len(tickets)}")
    return tickets

def ultimo_tecnico(tok, tid, requester_id):
    try:
        followups = api_get(f"Ticket/{tid}/ITILFollowup", tok)
        if not isinstance(followups, list):
            return None
        tecnico = [
            f for f in followups
            if f.get("users_id") != requester_id
            and "Base de Conhecimento" not in (f.get("content") or "")
            and "Obrigado pelo seu contato" not in (f.get("content") or "")
        ]
        if not tecnico:
            return None
        return tecnico[-1].get("date") or tecnico[-1].get("date_creation")
    except Exception:
        return None

def requester_numeric_id(tok, tid):
    try:
        t = api_get(f"Ticket/{tid}", tok)
        return t.get("users_id_recipient")
    except Exception:
        return None

# ── Classificação por entidade — extrai o cliente folha do caminho ────
def produto(ticket):
    """Extrai o nome do cliente a partir do caminho da entidade.
    Ex: 'Luzcon > Recebe Mais > Tegma' → 'Tegma'
        'Luzcon > Recebe Mais' → 'Recebe Mais (Geral)'
    """
    entidade = (ticket.get("entities_id") or "").strip()
    if not entidade:
        return "Sem entidade"
    partes = [p.strip() for p in entidade.split(">")]
    # O cliente é o último segmento do caminho
    cliente = partes[-1]
    # Se o último segmento for "Recebe Mais" (sem sub-entidade), agrupa
    if cliente.lower() == "recebe mais":
        return "Recebe Mais (Geral)"
    return cliente

def tipo(titulo):
    tl = titulo.lower()
    if "erro"    in tl:                          return "Erro"
    if "servico" in tl or "serviço" in tl:       return "Solicitação de Serviço"
    if "wildlife" in tl:                          return "Wildlife"
    if "duvida"  in tl or "dúvida"  in tl:       return "Dúvida / Orientação"
    if "melhoria" in tl:                          return "Solicitação de Melhoria"
    return "Outros"

# ── Métricas ──────────────────────────────────────────────────────────
FMT   = "%Y-%m-%d %H:%M:%S"
H_INI = 8
H_FIM = 18
H_DIA = H_FIM - H_INI

def horas_uteis(ini_str, fim_str):
    inicio = datetime.strptime(ini_str, FMT)
    fim    = datetime.strptime(fim_str,  FMT)
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

# ── Helpers visuais ───────────────────────────────────────────────────
def hr():
    return HRFlowable(width="100%", thickness=0.5, color=CINZA_BRD, spaceAfter=5, spaceBefore=5)

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

def visao_geral(story, total, resol, taxa, por_status, por_prod, por_tipo_d, nao_resolvidos):
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
    story.append(Spacer(1, 0.5*cm))

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
    story.append(styled_tbl(["Status","Qtd.","%","Proporção"], rows,
                             [4*cm,2*cm,2*cm,9*cm]))

    story += section("Volume por Cliente")
    rows_p = []
    for p, qtd in sorted(por_prod.items(), key=lambda x: x[1], reverse=True):
        pct = round(qtd/total*100)
        rows_p.append((p, str(qtd), f"{pct}%", "█"*int(pct/5)+"░"*(20-int(pct/5))))
    story.append(styled_tbl(["Cliente","Qtd.","%","Proporção"], rows_p,
                             [5.5*cm,2*cm,2*cm,7.5*cm]))

    story += section("Volume por Tipo de Chamado")
    tipo_order = ["Erro","Solicitação de Serviço","Wildlife","Dúvida / Orientação",
                  "Solicitação de Melhoria","Outros"]
    rows_t = []
    for tp in tipo_order:
        qtd = por_tipo_d.get(tp, 0)
        if qtd == 0: continue
        pct = round(qtd/total*100)
        rows_t.append((tp, str(qtd), f"{pct}%", "█"*int(pct/5)+"░"*(20-int(pct/5))))
    story.append(styled_tbl(["Tipo","Qtd.","%","Proporção"], rows_t,
                             [5.5*cm,2*cm,2*cm,7.5*cm]))

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
    story.append(styled_tbl(
        ["Cliente","Tempo Técnico","Tempo Total","Qtd."],
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
    story.append(styled_tbl(
        ["Tipo","Tempo Técnico","Tempo Total","Qtd."],
        rows_t, [6*cm,4*cm,4*cm,3*cm]))

def em_aberto(story, nao_resolvidos):
    story.append(PageBreak())
    story += section(f"Chamados da Semana Ainda em Aberto ({len(nao_resolvidos)})")

    aviso = Table([[Paragraph(
        f"Os {len(nao_resolvidos)} chamados abaixo foram criados na semana {PERIODO_LABEL} "
        "e ainda não foram resolvidos ou fechados.",
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

# ── Main ──────────────────────────────────────────────────────────────
def main():
    print(f"[{HOJE_STR}] TESTE — Relatório Recebe Mais | período: {PERIODO}")

    tok = get_session()
    try:
        tickets_raw = buscar_tickets_periodo(tok)
    finally:
        close_session(tok)

    if not tickets_raw:
        print("  Nenhum ticket Recebe Mais encontrado no período. Encerrando.")
        return

    TICKETS = []
    for t in tickets_raw:
        tid         = t.get("id")
        titulo      = t.get("name", "")
        criacao     = t.get("date_creation") or t.get("date", "")
        atualizacao = t.get("date_mod", criacao)
        urg         = t.get("urgency", 3)
        st          = t.get("status", 1)
        prod        = produto(t)
        TICKETS.append((tid, titulo, criacao, atualizacao, urg, st, prod))

    print(f"  Buscando último comentário técnico para tickets resolvidos...")
    tok2 = get_session()
    try:
        DADOS = {}
        resolvidos = [(tid, criacao) for (tid, _, criacao, _, _, st, _) in TICKETS if st in (5, 6)]
        for i, (tid, criacao) in enumerate(resolvidos):
            req_id = requester_numeric_id(tok2, tid)
            ult    = ultimo_tecnico(tok2, tid, req_id)
            DADOS[tid] = {"criacao": criacao, "ultimo_tecnico": ult}
            if (i+1) % 10 == 0:
                print(f"    {i+1}/{len(resolvidos)} processados...")
    finally:
        close_session(tok2)

    por_status = {}
    por_prod   = {}
    por_tipo_d = {}
    tempos     = []
    nao_resolvidos = []

    for tid, titulo, criacao, atualizacao, urg, st, prod in TICKETS:
        por_status[st] = por_status.get(st, 0) + 1
        tp = tipo(titulo)
        por_prod[prod]   = por_prod.get(prod, 0) + 1
        por_tipo_d[tp]   = por_tipo_d.get(tp, 0) + 1
        if st in (5, 6):
            d          = DADOS.get(tid, {})
            dt_criacao = d.get("criacao") or criacao
            h_ciclo    = horas_uteis(dt_criacao, atualizacao)
            ult_tec    = d.get("ultimo_tecnico")
            h_tecnico  = horas_uteis(dt_criacao, ult_tec) if ult_tec else h_ciclo
            tempos.append((h_ciclo, h_tecnico, prod, tp))
        if st in (1, 2, 4):
            nao_resolvidos.append((tid, titulo, criacao, urg, STATUS_NOME[st]))

    TOTAL      = len(TICKETS)
    RESOL      = por_status.get(5, 0) + por_status.get(6, 0)
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

    print(f"  Total Recebe Mais: {TOTAL} | Resolvidos: {RESOL} ({TAXA_RESOL}%)")
    print(f"  Tempo técnico médio: {fmt_hu(TEMPO_GERAL_TECNICO)}")
    print(f"  Tempo ciclo médio:   {fmt_hu(TEMPO_GERAL_CICLO)}")

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=1.5*cm, rightMargin=1.5*cm,
                            topMargin=1.8*cm, bottomMargin=1.2*cm)
    story = []
    capa(story, TOTAL, TAXA_RESOL)
    visao_geral(story, TOTAL, RESOL, TAXA_RESOL, por_status, por_prod, por_tipo_d, nao_resolvidos)
    if tempos:
        tempo_medio(story, tempos, tp_prod_ciclo, tp_prod_tecnico,
                    tp_tipo_ciclo, tp_tipo_tecnico,
                    TEMPO_GERAL_CICLO, TEMPO_GERAL_TECNICO,
                    TEMPO_PROD_CICLO, TEMPO_PROD_TECNICO,
                    TEMPO_TIPO_CICLO, TEMPO_TIPO_TECNICO)
    if nao_resolvidos:
        em_aberto(story, nao_resolvidos)
    doc.build(story, onFirstPage=on_first, onLaterPages=on_page)

    pdf_bytes = buf.getvalue()
    with open(OUTPUT, "wb") as f:
        f.write(pdf_bytes)
    print(f"  PDF salvo: {OUTPUT} ({len(pdf_bytes)//1024} KB)")
    print(f"  Abra o arquivo: {os.path.abspath(OUTPUT)}")

if __name__ == "__main__":
    main()
