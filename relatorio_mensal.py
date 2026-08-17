"""
Relatório Mensal Automático — Recebe Mais
Calcula o mês anterior completo, gera PDF consolidado e envia ao Telegram.
Roda todo dia 1º do mês às 09h via GitHub Actions.

Seções do PDF:
  1. Capa
  2. Visão Geral (KPIs + comparativo mês anterior)
  3. Volume por Semana dentro do mês
  4. Volume por Cliente
  5. Volume por Tipo
  6. Tempo Médio de Resolução
  7. Ranking de Técnicos
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

# Importa utilitários compartilhados do relatório semanal
from relatorio_automatico import (
    get_session, close_session, buscar_tickets_range,
    eh_recebemai, produto, tipo, foi_resolvido_no_periodo,
    horas_uteis, fmt_hu,
    ultimo_tecnico_completo, requester_numeric_id, buscar_nome_usuario,
    AZUL_ESC, AZUL_MED, AZUL_CLA, VERDE, AMARELO, VERMELHO,
    CINZA_CLA, CINZA_BRD, BRANCO, PRETO,
    S, th_para, styled_tbl, section,
    FMT, STATUS_NOME,
)

# ── Credenciais ───────────────────────────────────────────────────────
GLPI_URL         = os.environ["GLPI_URL"].rstrip("/")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN",  "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID","")

# ── Período: mês anterior ─────────────────────────────────────────────
hoje = datetime.utcnow()
_primeiro_dia_atual  = hoje.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
_ultimo_dia_mes_ant  = _primeiro_dia_atual - timedelta(days=1)
_primeiro_dia_mes    = _ultimo_dia_mes_ant.replace(day=1)

# Mês retrasado para comparativo
_ultimo_dia_2meses   = _primeiro_dia_mes - timedelta(days=1)
_primeiro_dia_2meses = _ultimo_dia_2meses.replace(day=1)

MESES_PT = {
    1: "Janeiro",  2: "Fevereiro", 3: "Março",    4: "Abril",
    5: "Maio",     6: "Junho",     7: "Julho",     8: "Agosto",
    9: "Setembro", 10: "Outubro",  11: "Novembro", 12: "Dezembro",
}

DATA_INI     = _primeiro_dia_mes.strftime("%Y-%m-%d 00:00:00")
DATA_FIM     = _ultimo_dia_mes_ant.strftime("%Y-%m-%d 23:59:59")
DATA_INI_ANT = _primeiro_dia_2meses.strftime("%Y-%m-%d 00:00:00")
DATA_FIM_ANT = _ultimo_dia_2meses.strftime("%Y-%m-%d 23:59:59")

MES_NOME     = MESES_PT[_ultimo_dia_mes_ant.month]
MES_ANT_NOME = MESES_PT[_ultimo_dia_2meses.month]
ANO          = _ultimo_dia_mes_ant.year
ANO_ANT      = _ultimo_dia_2meses.year
PERIODO      = f"{MES_NOME} {ANO}"
PERIODO_ANT  = f"{MES_ANT_NOME}/{ANO_ANT}"
HOJE_STR     = datetime.utcnow().strftime("%d/%m/%Y %H:%M")
OUTPUT       = f"Relatorio_Mensal_RecebeMais_{_ultimo_dia_mes_ant.strftime('%m_%Y')}.pdf"

# ── Estilos ───────────────────────────────────────────────────────────
TITULO_C = S("TC", fontName="Helvetica-Bold", fontSize=26, textColor=BRANCO,   alignment=TA_CENTER, leading=32)
SUB_C    = S("SC", fontName="Helvetica",      fontSize=11, textColor=AZUL_CLA, alignment=TA_CENTER, leading=16)
SECAO    = S("SE", fontName="Helvetica-Bold", fontSize=12, textColor=AZUL_ESC, spaceAfter=4, spaceBefore=12)
BODY     = S("BO", fontName="Helvetica",      fontSize=9,  textColor=PRETO,    leading=13)
NUM_G    = S("NG", fontName="Helvetica-Bold", fontSize=24, textColor=AZUL_MED, alignment=TA_CENTER)
NUM_L    = S("NL", fontName="Helvetica",      fontSize=8,  textColor=colors.HexColor("#7F8C8D"), alignment=TA_CENTER, leading=10)

# ── Header / Footer ───────────────────────────────────────────────────
def on_page(canvas, doc):
    canvas.saveState()
    w, h = A4
    canvas.setFillColor(AZUL_ESC)
    canvas.rect(0, h - 1.2 * cm, w, 1.2 * cm, fill=1, stroke=0)
    canvas.setFont("Helvetica-Bold", 9)
    canvas.setFillColor(BRANCO)
    canvas.drawString(1.5 * cm, h - 0.85 * cm,
                      f"Suporte RM — Relatório Mensal Recebe Mais | {PERIODO}")
    canvas.drawRightString(w - 1.5 * cm, h - 0.85 * cm, f"Gerado em {HOJE_STR}")
    canvas.setFillColor(AZUL_ESC)
    canvas.rect(0, 0, w, 0.8 * cm, fill=1, stroke=0)
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(BRANCO)
    canvas.drawString(1.5 * cm, 0.27 * cm,
                      "Dados extraídos do GLPI | Filtro: entidade Recebe Mais")
    canvas.drawRightString(w - 1.5 * cm, 0.27 * cm, f"Página {doc.page}")
    canvas.restoreState()

def on_first(canvas, doc):
    canvas.saveState()
    w, h = A4
    canvas.setFillColor(AZUL_ESC)
    canvas.rect(0, 0, w, 0.8 * cm, fill=1, stroke=0)
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(BRANCO)
    canvas.drawString(1.5 * cm, 0.27 * cm, "Suporte RM — Relatório Mensal Recebe Mais")
    canvas.restoreState()

# ── Helpers ───────────────────────────────────────────────────────────
def delta_str(atual, anterior):
    if anterior == 0:
        return "—"
    pct   = round((atual - anterior) / anterior * 100)
    sinal = "+" if pct >= 0 else ""
    return f"{sinal}{pct}%"

def delta_pp(atual, anterior):
    diff  = round(atual - anterior)
    sinal = "+" if diff >= 0 else ""
    return f"{sinal}{diff} pp"

def cor_delta_hex(v):
    if "+" in v: return "#27AE60"
    if "-" in v: return "#E74C3C"
    return "#7F8C8D"

# ── Seções do PDF ─────────────────────────────────────────────────────
def capa(story, total, taxa):
    story.append(Spacer(1, 3 * cm))
    t = Table(
        [[Paragraph(f"RELATÓRIO MENSAL<br/>RECEBE MAIS", TITULO_C)]],
        colWidths=[17 * cm], rowHeights=[3.5 * cm],
    )
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), AZUL_ESC),
        ("ALIGN",      (0, 0), (-1, -1), "CENTER"),
        ("VALIGN",     (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",    (0, 0), (-1, -1), 20),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 20),
    ]))
    story.append(t)
    story.append(Spacer(1, 0.6 * cm))
    sub = Table(
        [[Paragraph(f"Período: {PERIODO}", SUB_C),
          Paragraph(f"Gerado em: {HOJE_STR}", SUB_C)]],
        colWidths=[8.5 * cm, 8.5 * cm], rowHeights=[1 * cm],
    )
    sub.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), AZUL_MED),
        ("ALIGN",      (0, 0), (-1, -1), "CENTER"),
        ("VALIGN",     (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(sub)
    story.append(Spacer(1, 1 * cm))
    story.append(Paragraph(
        f"Chamados filtrados por <b>entidade Recebe Mais</b> — mês de <b>{PERIODO}</b>. "
        f"Total: <b>{total} chamados</b> | Taxa de resolução: <b>{taxa}%</b>",
        ParagraphStyle("tag", fontName="Helvetica", fontSize=10,
                       textColor=colors.HexColor("#7F8C8D"),
                       alignment=TA_CENTER, leading=16),
    ))
    story.append(PageBreak())


def visao_geral(story, total, resol, taxa, por_status, comp):
    story += section("Visão Geral do Mês")

    kpi_data = [
        [Paragraph(str(total), NUM_G),
         Paragraph(str(resol), NUM_G),
         Paragraph(f"{taxa}%",
                   ParagraphStyle("pct", fontName="Helvetica-Bold", fontSize=24,
                                  textColor=VERDE, alignment=TA_CENTER)),
         Paragraph(str(total - resol),
                   ParagraphStyle("ab", fontName="Helvetica-Bold", fontSize=24,
                                  textColor=AMARELO, alignment=TA_CENTER))],
        [Paragraph("Total Abertos", NUM_L),
         Paragraph("Resolvidos / Fechados", NUM_L),
         Paragraph("Taxa de Resolução", NUM_L),
         Paragraph("Ainda em Aberto", NUM_L)],
    ]
    kpi = Table(kpi_data, colWidths=[4.25 * cm] * 4,
                rowHeights=[1.4 * cm, 0.6 * cm])
    kpi.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), CINZA_CLA),
        ("ALIGN",      (0, 0), (-1, -1), "CENTER"),
        ("VALIGN",     (0, 0), (-1, -1), "MIDDLE"),
        ("BOX",        (0, 0), (-1, -1), 0.5, CINZA_BRD),
        ("LINEAFTER",  (0, 0), (-3, -1), 0.5, CINZA_BRD),
    ]))
    story.append(kpi)
    story.append(Spacer(1, 0.4 * cm))

    # Comparativo com mês anterior
    if comp:
        d_total = delta_str(total, comp["total"])
        d_taxa  = delta_pp(taxa, comp["taxa"])
        nota_st = ParagraphStyle("nc", fontName="Helvetica", fontSize=8,
                                 textColor=colors.HexColor("#7F8C8D"),
                                 alignment=TA_CENTER, leading=12)
        comp_tbl = Table([[Paragraph(
            f'Mês anterior ({PERIODO_ANT}): '
            f'<b>{comp["total"]}</b> chamados · '
            f'<b>{comp["taxa"]}%</b> resolvidos · '
            f'Variação volume: <font color="{cor_delta_hex(d_total)}"><b>{d_total}</b></font> · '
            f'Variação taxa: <font color="{cor_delta_hex(d_taxa)}"><b>{d_taxa}</b></font>',
            nota_st,
        )]], colWidths=[17 * cm])
        comp_tbl.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), CINZA_CLA),
            ("BOX",           (0, 0), (-1, -1), 0.5, CINZA_BRD),
            ("TOPPADDING",    (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING",   (0, 0), (-1, -1), 10),
        ]))
        story.append(comp_tbl)
        story.append(Spacer(1, 0.4 * cm))

    # Volume por Status
    story += section("Volume por Status")
    status_order = [
        (1, "Novo",          VERMELHO),
        (2, "Em Andamento",  AZUL_MED),
        (4, "Pendente",      AMARELO),
        (5, "Resolvido",     VERDE),
        (6, "Fechado",       colors.HexColor("#7F8C8D")),
    ]
    rows = []
    for st, nome, cor in status_order:
        qtd = por_status.get(st, 0)
        if qtd == 0:
            continue
        pct   = round(qtd / total * 100)
        barra = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
        c_hex = "#%02x%02x%02x" % (int(cor.red * 255), int(cor.green * 255), int(cor.blue * 255))
        rows.append((
            Paragraph(f'<font color="{c_hex}"><b>{nome}</b></font>', BODY),
            str(qtd), f"{pct}%", barra,
        ))
    story.append(styled_tbl(
        ["Status", "Qtd.", "%", "Proporção"],
        rows, [4 * cm, 2 * cm, 2 * cm, 9 * cm],
    ))


def volume_por_semana(story, tickets_periodo):
    """Breakdown do volume por semana dentro do mês."""
    story += section("Volume por Semana no Mês")

    # Identifica semanas: agrupa por número de semana ISO dentro do mês
    semanas = {}
    for t in tickets_periodo:
        dc = t.get("date_creation") or ""
        try:
            dt = datetime.strptime(dc[:10], "%Y-%m-%d")
        except Exception:
            continue
        # Número da semana dentro do mês (1 a 5)
        semana_num = (dt.day - 1) // 7 + 1
        semana_label = f"Semana {semana_num} ({_primeiro_dia_mes.strftime('%d/%m')} + {(semana_num-1)*7}d)"
        # Calcula início/fim da semana dentro do mês
        ini = _primeiro_dia_mes.replace(day=1 + (semana_num - 1) * 7)
        fim_day = min(ini.day + 6, _ultimo_dia_mes_ant.day)
        try:
            fim = _primeiro_dia_mes.replace(day=fim_day)
        except Exception:
            fim = _ultimo_dia_mes_ant
        semana_label = f"Semana {semana_num}  ({ini.strftime('%d/%m')}–{fim.strftime('%d/%m')})"
        semanas.setdefault(semana_num, {"label": semana_label, "total": 0, "resol": 0})
        semanas[semana_num]["total"] += 1
        if t.get("status") in (5, 6):
            semanas[semana_num]["resol"] += 1

    total_mes = len(tickets_periodo)
    rows = []
    for num in sorted(semanas):
        s    = semanas[num]
        qtd  = s["total"]
        resol = s["resol"]
        taxa = round(resol / qtd * 100) if qtd else 0
        pct  = round(qtd / total_mes * 100) if total_mes else 0
        barra = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
        rows.append((s["label"], str(qtd), str(resol), f"{taxa}%", barra))

    story.append(styled_tbl(
        ["Semana", "Chamados", "Resolvidos", "Taxa", "Proporção"],
        rows, [5 * cm, 2.5 * cm, 2.5 * cm, 2 * cm, 5 * cm],
    ))


def volume_clientes_tipos(story, tickets_periodo, por_prod, por_tipo_d):
    total = len(tickets_periodo)

    story += section("Volume por Cliente")
    rows_p = []
    for p, qtd in sorted(por_prod.items(), key=lambda x: x[1], reverse=True):
        pct = round(qtd / total * 100)
        rows_p.append((p, str(qtd), f"{pct}%", "█" * int(pct / 5) + "░" * (20 - int(pct / 5))))
    story.append(styled_tbl(
        ["Cliente", "Qtd.", "%", "Proporção"],
        rows_p, [5.5 * cm, 2 * cm, 2 * cm, 7.5 * cm],
    ))

    story += section("Volume por Tipo de Chamado")
    tipo_order = ["Erro", "Solicitação de Serviço", "Wildlife",
                  "Dúvida / Orientação", "Solicitação de Melhoria", "Outros"]
    rows_t = []
    for tp in tipo_order:
        qtd = por_tipo_d.get(tp, 0)
        if qtd == 0:
            continue
        pct = round(qtd / total * 100)
        rows_t.append((tp, str(qtd), f"{pct}%", "█" * int(pct / 5) + "░" * (20 - int(pct / 5))))
    story.append(styled_tbl(
        ["Tipo", "Qtd.", "%", "Proporção"],
        rows_t, [5.5 * cm, 2 * cm, 2 * cm, 7.5 * cm],
    ))


def tempo_medio(story, tempos, tp_prod_tecnico, tp_prod_ciclo,
                TEMPO_GERAL_TECNICO, TEMPO_GERAL_CICLO, TEMPO_PROD_TECNICO, TEMPO_PROD_CICLO):
    story.append(PageBreak())
    story += section("Tempo Médio de Resolução")

    nota = Table([[Paragraph(
        f"Calculado sobre {len(tempos)} chamados resolvidos/fechados criados em {PERIODO}. "
        "<b>Tempo Técnico</b>: criação → último comentário do técnico. "
        "<b>Tempo Total</b>: criação → última atualização. "
        "Horas úteis: seg–sex, 08h–18h.",
        ParagraphStyle("n", fontName="Helvetica-Oblique", fontSize=8,
                       textColor=colors.HexColor("#7F8C8D"), leading=12),
    )]], colWidths=[17 * cm])
    nota.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), CINZA_CLA),
        ("BOX",           (0, 0), (-1, -1), 0.5, CINZA_BRD),
        ("TOPPADDING",    (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING",   (0, 0), (-1, -1), 10),
    ]))
    story.append(nota)
    story.append(Spacer(1, 0.4 * cm))

    duo = Table([
        [Paragraph(fmt_hu(TEMPO_GERAL_TECNICO),
                   ParagraphStyle("tgt", fontName="Helvetica-Bold", fontSize=26,
                                  textColor=VERDE, alignment=TA_CENTER)),
         Paragraph(fmt_hu(TEMPO_GERAL_CICLO),
                   ParagraphStyle("tgc", fontName="Helvetica-Bold", fontSize=26,
                                  textColor=AZUL_MED, alignment=TA_CENTER))],
        [Paragraph("<b>Tempo Técnico Médio</b><br/>"
                   "<font color='#7F8C8D' size='8'>criação → último comentário do técnico</font>",
                   ParagraphStyle("l1", fontName="Helvetica", fontSize=9,
                                  alignment=TA_CENTER, leading=13)),
         Paragraph("<b>Tempo Total Médio de Ciclo</b><br/>"
                   "<font color='#7F8C8D' size='8'>criação → última atualização</font>",
                   ParagraphStyle("l2", fontName="Helvetica", fontSize=9,
                                  alignment=TA_CENTER, leading=13))],
    ], colWidths=[8.5 * cm, 8.5 * cm], rowHeights=[2 * cm, 1 * cm])
    duo.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#EAFAF1")),
        ("BACKGROUND", (1, 0), (1, -1), AZUL_CLA),
        ("ALIGN",      (0, 0), (-1, -1), "CENTER"),
        ("VALIGN",     (0, 0), (-1, -1), "MIDDLE"),
        ("BOX",        (0, 0), (0, -1), 1.5, VERDE),
        ("BOX",        (1, 0), (1, -1), 1.5, AZUL_MED),
        ("TOPPADDING",    (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(duo)
    story.append(Spacer(1, 0.6 * cm))

    story += section("Tempo Médio por Cliente")
    rows_p = []
    for prod in sorted(TEMPO_PROD_TECNICO, key=lambda p: len(tp_prod_tecnico[p]), reverse=True):
        ht = TEMPO_PROD_TECNICO[prod]
        hc = TEMPO_PROD_CICLO.get(prod, ht)
        n  = len(tp_prod_tecnico[prod])
        rows_p.append((prod, fmt_hu(ht), fmt_hu(hc), str(n)))
    story.append(styled_tbl(
        ["Cliente", "Tempo Técnico", "Tempo Total", "Qtd."],
        rows_p, [6 * cm, 4 * cm, 4 * cm, 3 * cm],
    ))


def ranking_tecnicos(story, ranking):
    story.append(PageBreak())
    story += section(f"Ranking de Técnicos — {PERIODO}")

    nota = Table([[Paragraph(
        "Considera chamados Recebe Mais resolvidos/fechados criados no mês. "
        "Técnico identificado pelo último comentário no chamado (excluindo respostas automáticas). "
        "Tempos em horas úteis: seg–sex, 08h–18h.",
        ParagraphStyle("rn", fontName="Helvetica-Oblique", fontSize=8,
                       textColor=colors.HexColor("#7F8C8D"), leading=12),
    )]], colWidths=[17 * cm])
    nota.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), CINZA_CLA),
        ("BOX",           (0, 0), (-1, -1), 0.5, CINZA_BRD),
        ("TOPPADDING",    (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING",   (0, 0), (-1, -1), 10),
    ]))
    story.append(nota)
    story.append(Spacer(1, 0.4 * cm))

    medalhas  = ["🥇", "🥈", "🥉"]
    ordenado  = sorted(ranking.items(), key=lambda x: x[1]["count"], reverse=True)
    rows = []
    for i, (uid, dados) in enumerate(ordenado):
        pos  = medalhas[i] if i < 3 else f"{i + 1}º"
        rows.append((
            Paragraph(f"<b>{pos}</b>", BODY),
            Paragraph(f"<b>{dados['nome']}</b>", BODY),
            Paragraph(f"<b>{dados['count']}</b>",
                      ParagraphStyle("bc", fontName="Helvetica-Bold", fontSize=9,
                                     textColor=AZUL_MED, alignment=TA_CENTER)),
            fmt_hu(dados["tempo_tecnico_avg"]),
            fmt_hu(dados["tempo_ciclo_avg"]),
        ))
    story.append(styled_tbl(
        ["#", "Técnico", "Resolvidos", "T. Técnico Médio", "T. Ciclo Médio"],
        rows, [1.2 * cm, 6 * cm, 2.5 * cm, 3.65 * cm, 3.65 * cm],
        hbg=VERDE,
    ))

# ── Telegram ──────────────────────────────────────────────────────────
def enviar_telegram(pdf_bytes):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("  [!] Credenciais Telegram não configuradas.")
        return
    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument",
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "caption": (
                f"📅 *Relatório Mensal — Recebe Mais*\n"
                f"Período: {PERIODO}\n"
                f"Gerado automaticamente via GLPI"
            ),
            "parse_mode": "Markdown",
        },
        files={"document": (OUTPUT, pdf_bytes, "application/pdf")},
        timeout=60,
    )
    r.raise_for_status()
    print("  PDF enviado ao Telegram.")

# ── Main ──────────────────────────────────────────────────────────────
def main():
    print(f"[{HOJE_STR}] Relatório Mensal Recebe Mais — {PERIODO}")

    tok = get_session()
    try:
        tickets_raw = buscar_tickets_range(tok, DATA_INI, DATA_FIM)
        print(f"  Tickets do mês: {len(tickets_raw)}")
    finally:
        close_session(tok)

    if not tickets_raw:
        print("  Nenhum ticket encontrado. Encerrando.")
        return

    # Mês anterior para comparativo
    tok_ant = get_session()
    try:
        tickets_ant = buscar_tickets_range(tok_ant, DATA_INI_ANT, DATA_FIM_ANT)
        total_ant   = len(tickets_ant)
        resol_ant   = sum(1 for t in tickets_ant
                          if foi_resolvido_no_periodo(t.get("solvedate") or "", DATA_FIM_ANT))
        taxa_ant    = round(resol_ant / total_ant * 100) if total_ant else 0
        comp        = {"total": total_ant, "resol": resol_ant, "taxa": taxa_ant}
        print(f"  Comparativo ({PERIODO_ANT}): {total_ant} tickets, {taxa_ant}% resolvidos")
    finally:
        close_session(tok_ant)

    # Métricas
    por_status = {}
    por_prod   = {}
    por_tipo_d = {}
    tempos     = []

    TICKETS = [
        (t.get("id"), t.get("name", ""),
         t.get("date_creation") or t.get("date", ""),
         t.get("date_mod", ""),
         t.get("status", 1),
         produto(t),
         t.get("solvedate") or "")
        for t in tickets_raw
    ]

    for _, titulo, _, _, st, prod, _solve in TICKETS:
        tp = tipo(titulo)
        por_status[st]   = por_status.get(st, 0) + 1
        por_prod[prod]   = por_prod.get(prod, 0) + 1
        por_tipo_d[tp]   = por_tipo_d.get(tp, 0) + 1

    # Followups para tickets resolvidos
    print(f"  Buscando followups de tickets resolvidos...")
    tok2 = get_session()
    try:
        DADOS   = {}
        RANKING = {}
        resolvidos = [(tid, criacao, prod) for tid, _, criacao, _, st, prod, solve in TICKETS
                      if foi_resolvido_no_periodo(solve, DATA_FIM)]
        for i, (tid, criacao, prod) in enumerate(resolvidos):
            req_id        = requester_numeric_id(tok2, tid)
            ult_data, uid, _ = ultimo_tecnico_completo(tok2, tid, req_id)
            DADOS[tid]    = {"criacao": criacao, "ultimo_tecnico": ult_data, "tecnico_id": uid}
            if uid:
                if uid not in RANKING:
                    RANKING[uid] = {"nome": "", "count": 0,
                                    "tempos_tecnico": [], "tempos_ciclo": []}
                RANKING[uid]["count"] += 1
            if (i + 1) % 10 == 0:
                print(f"    {i + 1}/{len(resolvidos)} processados...")
        for uid in RANKING:
            RANKING[uid]["nome"] = buscar_nome_usuario(tok2, uid)
    finally:
        close_session(tok2)

    # Calcula tempos
    tp_prod_tecnico = {}
    tp_prod_ciclo   = {}
    for tid, titulo, criacao, atualizacao, st, prod, solve in TICKETS:
        if not foi_resolvido_no_periodo(solve, DATA_FIM):
            continue
        d          = DADOS.get(tid, {})
        dt_criacao = d.get("criacao") or criacao
        h_ciclo    = horas_uteis(dt_criacao, atualizacao)
        ult_tec    = d.get("ultimo_tecnico")
        h_tecnico  = horas_uteis(dt_criacao, ult_tec) if ult_tec else h_ciclo
        tempos.append((h_ciclo, h_tecnico, prod))
        tp_prod_ciclo.setdefault(prod,   []).append(h_ciclo)
        tp_prod_tecnico.setdefault(prod, []).append(h_tecnico)
        uid = d.get("tecnico_id")
        if uid and uid in RANKING:
            RANKING[uid]["tempos_tecnico"].append(h_tecnico)
            RANKING[uid]["tempos_ciclo"].append(h_ciclo)

    TOTAL      = len(TICKETS)
    RESOL      = sum(1 for *_, solve in TICKETS if foi_resolvido_no_periodo(solve, DATA_FIM))
    TAXA_RESOL = round(RESOL / TOTAL * 100) if TOTAL else 0

    TEMPO_GERAL_CICLO   = sum(hc for hc, *_ in tempos) / len(tempos) if tempos else 0
    TEMPO_GERAL_TECNICO = sum(ht for _, ht, *_ in tempos) / len(tempos) if tempos else 0

    TEMPO_PROD_CICLO   = {p: sum(v) / len(v) for p, v in tp_prod_ciclo.items()}
    TEMPO_PROD_TECNICO = {p: sum(v) / len(v) for p, v in tp_prod_tecnico.items()}

    ranking_final = {
        uid: {
            "nome":              d["nome"],
            "count":             d["count"],
            "tempo_tecnico_avg": sum(d["tempos_tecnico"]) / len(d["tempos_tecnico"]) if d["tempos_tecnico"] else 0,
            "tempo_ciclo_avg":   sum(d["tempos_ciclo"])   / len(d["tempos_ciclo"])   if d["tempos_ciclo"]   else 0,
        }
        for uid, d in RANKING.items() if d["count"] > 0
    }

    print(f"  Total: {TOTAL} | Resolvidos: {RESOL} ({TAXA_RESOL}%)")
    print(f"  Técnicos: {len(ranking_final)} | T. técnico médio: {fmt_hu(TEMPO_GERAL_TECNICO)}")

    # Gera PDF
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=1.5 * cm, rightMargin=1.5 * cm,
                            topMargin=1.8 * cm,  bottomMargin=1.2 * cm)
    story = []
    capa(story, TOTAL, TAXA_RESOL)
    visao_geral(story, TOTAL, RESOL, TAXA_RESOL, por_status, comp)
    volume_por_semana(story, tickets_raw)
    volume_clientes_tipos(story, tickets_raw, por_prod, por_tipo_d)
    if tempos:
        tempo_medio(story, tempos, tp_prod_tecnico, tp_prod_ciclo,
                    TEMPO_GERAL_TECNICO, TEMPO_GERAL_CICLO,
                    TEMPO_PROD_TECNICO, TEMPO_PROD_CICLO)
    if ranking_final:
        ranking_tecnicos(story, ranking_final)
    doc.build(story, onFirstPage=on_first, onLaterPages=on_page)

    pdf_bytes = buf.getvalue()
    print(f"  PDF gerado: {len(pdf_bytes) // 1024} KB")
    enviar_telegram(pdf_bytes)

if __name__ == "__main__":
    main()
