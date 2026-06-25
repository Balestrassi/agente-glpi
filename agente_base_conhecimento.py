"""
Agente de Base de Conhecimento — GLPI
Roda a cada 5 minutos via Task Scheduler.
Para cada chamado novo (status=1), busca chamados similares já resolvidos
e posta uma sugestão privada visível apenas para técnicos.
"""

import os
import json
import re
import requests
import html
from datetime import datetime, timedelta, timezone
from pathlib import Path

BRASILIA = timezone(timedelta(hours=-3))

# ── Configuração ──────────────────────────────────────────────────────
GLPI_URL   = os.environ.get("GLPI_URL",        "https://servicedesk.a7on.ai")
APP_TOKEN  = os.environ.get("GLPI_APP_TOKEN",  "wXhcZYF5NcwtQWxLwWHlYC2UUlmCOMTxrJ0Q8Ryl")
USER_TOKEN = os.environ.get("GLPI_USER_TOKEN", "J2wAo6d8GXQ0WAVxeUXsmOJwBfMVty8qtXA8HNpj")

JANELA_MINUTOS  = 120   # janela ampla (2h) para cobrir delays do GitHub Actions
MAX_SIMILARES   = 3     # quantos chamados similares mostrar
CHARS_SOLUCAO   = 350   # tamanho máximo do texto de solução por chamado

PROCESSADOS_FILE = Path(__file__).parent / "base_conhecimento_processados.json"

STOPWORDS = {
    'de', 'do', 'da', 'em', 'no', 'na', 'para', 'por', 'com', 'um', 'uma',
    'que', 'se', 'ao', 'os', 'as', 'ou', 'mas', 'the', 'and', 'for', 'not',
    'issue', 'ticket', 'inc', 'solicitacao', 'solicitação',
    # Saudações e preenchimentos comuns
    'bom', 'boa', 'tarde', 'manha', 'manhã', 'noite', 'ola', 'olá',
    'tudo', 'bem', 'prezado', 'prezados', 'segue', 'seguem', 'conforme',
    'atenciosamente', 'cordialmente', 'favor', 'obrigado', 'obrigada',
    'gostaria', 'solicitar', 'informar', 'verificar', 'poderia',
}

# Rótulos de formulário a ignorar (são labels, não conteúdo relevante)
FORM_LABELS = {
    'selecione', 'urgencia', 'urgência', 'impacto', 'modulo', 'módulo',
    'opcoes', 'opções', 'servico', 'serviço',
    'media', 'média', 'médio', 'medio', 'médias', 'medias',
    'alta', 'baixa', 'dados', 'formulario', 'formulário', 'descreva',
    'forma', 'detalhada', 'coloque', 'evidencias', 'evidências',
    'acontecendo', 'numero', 'está', 'esta', 'como', 'qual', 'tipo',
    'valor', 'favor', 'mais', 'opcao', 'opção', 'campo', 'item',
    'nenhum', 'documento', 'anexado', 'anexo',
}

# ── Sessão GLPI ───────────────────────────────────────────────────────
def get_session():
    r = requests.get(
        f"{GLPI_URL}/apirest.php/initSession",
        headers={"Authorization": f"user_token {USER_TOKEN}", "App-Token": APP_TOKEN},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["session_token"]

def close_session(tok):
    requests.get(
        f"{GLPI_URL}/apirest.php/killSession",
        headers={"App-Token": APP_TOKEN, "Session-Token": tok},
        timeout=10,
    )

def api_get(path, tok, params=None):
    r = requests.get(
        f"{GLPI_URL}/apirest.php/{path}",
        headers={"App-Token": APP_TOKEN, "Session-Token": tok},
        params=params,
        timeout=15,
    )
    r.raise_for_status()
    return r.json()

def api_post(path, tok, body):
    r = requests.post(
        f"{GLPI_URL}/apirest.php/{path}",
        headers={"App-Token": APP_TOKEN, "Session-Token": tok, "Content-Type": "application/json"},
        json=body,
        timeout=15,
    )
    r.raise_for_status()
    return r.json()

# ── Utilitários ───────────────────────────────────────────────────────
def carregar_processados():
    if PROCESSADOS_FILE.exists():
        return set(json.loads(PROCESSADOS_FILE.read_text(encoding="utf-8")))
    return set()

def salvar_processados(ids: set):
    PROCESSADOS_FILE.write_text(
        json.dumps(sorted(ids), ensure_ascii=False, indent=2), encoding="utf-8"
    )

def limpar_html(texto: str) -> str:
    """Remove tags HTML e decodifica entidades."""
    texto = html.unescape(texto or "")
    texto = re.sub(r"<[^>]+>", " ", texto)
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto

def extrair_keywords(titulo: str) -> list[str]:
    """Extrai palavras-chave relevantes do título do chamado."""
    titulo = re.sub(r'\bINC\d+\b', '', titulo, flags=re.IGNORECASE)
    palavras = re.findall(r'[A-Za-zÀ-ÿ]{4,}', titulo)
    return [p for p in palavras if p.lower() not in STOPWORDS][:4]

# Palavras que identificam o campo descritivo livre do formulário
DESCRICAO_LABELS = {
    'acontecendo', 'aconteceu', 'melhorar', 'descreva', 'descricao',
    'descrição', 'problema', 'gostaria', 'informe', 'solicita',
    'solicitar', 'ocorreu', 'relatar', 'evidencia', 'evidências',
    'pedido', 'detalhe', 'necessita', 'necessidade', 'precisa',
    'reportar', 'impacto', 'reportando', 'solicitando',
}

def extrair_texto_descritivo(content_html: str) -> str:
    """Extrai o conteúdo do campo de descrição livre (ex: campo 6) do formulário GLPI.
    Divide pelo padrão N) e procura o campo cujo rótulo tem palavras descritivas.
    Fallback: campo com valor mais longo (excluindo Anexo)."""
    texto = limpar_html(content_html)

    # Divide nos campos numerados: "1) label : valor"
    campos = re.split(r'\b\d+\)', texto)

    melhor       = ""
    melhor_score = -1

    for campo in campos:
        campo = campo.strip()
        if ':' not in campo or len(campo) < 10:
            continue

        label, _, valor = campo.partition(':')
        label_lower = label.lower()
        valor = valor.strip()

        # Ignorar campo de Anexo e respostas muito curtas
        if 'nexo' in label_lower or len(valor) < 8:
            continue

        score = sum(1 for kw in DESCRICAO_LABELS if kw in label_lower)

        if score > melhor_score:
            melhor_score = score
            melhor = valor

    # Fallback: maior valor em texto livre (excluindo Anexo)
    if not melhor:
        candidatos = []
        for campo in campos:
            if 'nexo' in campo.lower() or ':' not in campo:
                continue
            _, _, v = campo.partition(':')
            candidatos.append(v.strip())
        if candidatos:
            melhor = max(candidatos, key=len)

    return melhor

def extrair_keywords_descricao(content_html: str) -> list[str]:
    """Extrai keywords do campo descritivo livre do formulário."""
    texto_desc = extrair_texto_descritivo(content_html)
    if not texto_desc:
        return []

    texto_desc = re.sub(r'[()\/|•\-]', ' ', texto_desc)
    palavras = re.findall(r'[A-Za-zÀ-ÿ]{4,}', texto_desc)

    vistas, resultado = set(), []
    for p in palavras:
        pl = p.lower()
        if pl not in STOPWORDS and pl not in FORM_LABELS and pl not in vistas:
            vistas.add(pl)
            resultado.append(p)

    return resultado[:8]

def ja_tem_sugestao_ia(tok, chamado_id: int) -> bool:
    """Verifica se o chamado já tem um acompanhamento privado da IA."""
    try:
        acomps = api_get(f"Ticket/{chamado_id}/ITILFollowup", tok)
        if not isinstance(acomps, list):
            return False
        return any(
            "Base de Conhecimento" in (a.get("content") or "")
            for a in acomps
        )
    except Exception:
        return False

# ── Lógica principal ──────────────────────────────────────────────────
def buscar_chamados_novos(tok) -> list:
    """Retorna chamados criados na janela de tempo (qualquer status aberto).
    Janela ampla (2h) para cobrir delays do GitHub Actions (cron pode atrasar até 30 min).
    Não filtra por status pois o primeiro atendimento automático pode mudar o status de
    Novo (1) para Em andamento (2) antes deste agente rodar.
    Deduplicação feita pelo processados.json — sem risco de sugestão duplicada."""
    # GLPI armazena datas em BRT (UTC-3) — usar BRT aqui para a comparação ser correta
    limite = (datetime.now(BRASILIA).replace(tzinfo=None) - timedelta(minutes=JANELA_MINUTOS)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        tickets = api_get("Ticket", tok, {
            "expand_dropdowns": True,
            "range": "0-99",
            "sort": "date_creation",
            "order": "DESC",
        })
        if not isinstance(tickets, list):
            return []
        # Inclui qualquer status aberto (1=Novo, 2=Em andamento, 4=Pendente)
        # Exclui resolvidos/fechados (5, 6) — não faz sentido sugerir para chamados encerrados
        return [
            t for t in tickets
            if (t.get("date_creation") or "") >= limite
            and t.get("status") not in (5, 6)
        ]
    except Exception as e:
        print(f"  [!] Erro ao buscar chamados novos: {e}")
        return []

def _eh_recebemai_entidade(entidade_str: str) -> bool:
    """Verifica se a entidade (string expandida) pertence ao universo Recebe Mais."""
    return "recebe mais" in (entidade_str or "").lower()

def buscar_similares_resolvidos(tok, titulo: str, kw_descricao: list = None, entity: str = None) -> list:
    """Busca chamados similares por título E descrição.
    Aceita qualquer cliente Recebe Mais (não exige entidade idêntica),
    mas dá bônus de score para o mesmo cliente."""
    kw_titulo = extrair_keywords(titulo)
    kw_desc   = kw_descricao or []

    if not kw_titulo and not kw_desc:
        return []

    limite_data = (datetime.now(BRASILIA).replace(tzinfo=None) - timedelta(days=90)).strftime("%Y-%m-%d")
    encontrados = {}

    def registrar(t, score):
        if t.get("status") not in (5, 6):
            return
        if (t.get("date_creation") or "") < limite_data:
            return
        # Aceita qualquer entidade Recebe Mais (não exige match exato)
        entidade_similar = t.get("entities_id") or ""
        if not _eh_recebemai_entidade(entidade_similar):
            return
        # Bônus: mesmo cliente tem peso maior
        score_final = score
        if entity and entidade_similar == entity:
            score_final += 1
        tid = t["id"]
        if tid in encontrados:
            encontrados[tid]["_score"] += score_final
        else:
            t["_score"] = score_final
            encontrados[tid] = t

    # Busca por título (peso 2)
    for kw in kw_titulo:
        try:
            results = api_get("Ticket", tok, {
                "expand_dropdowns": True,
                "range": "0-29",
                "sort": "date_mod",
                "order": "DESC",
                "searchText[name]": kw,
            })
            if isinstance(results, list):
                for t in results:
                    registrar(t, 2)
        except Exception:
            continue

    # Busca por descrição/formulário (peso 1)
    for kw in kw_desc[:5]:
        try:
            results = api_get("Ticket", tok, {
                "expand_dropdowns": True,
                "range": "0-19",
                "sort": "date_mod",
                "order": "DESC",
                "searchText[content]": kw,
            })
            if isinstance(results, list):
                for t in results:
                    registrar(t, 1)
        except Exception:
            continue

    similares = sorted(
        encontrados.values(),
        key=lambda x: (x.get("_score", 0), x.get("date_mod", "")),
        reverse=True,
    )
    return similares[:MAX_SIMILARES]

def obter_ultima_solucao(tok, chamado_id: int, solicitante_id) -> str:
    """Extrai o último comentário do técnico (não do solicitante)."""
    try:
        acomps = api_get(f"Ticket/{chamado_id}/ITILFollowup", tok)
        if not isinstance(acomps, list) or not acomps:
            return ""
        tecnicos = [a for a in acomps if str(a.get("users_id")) != str(solicitante_id)]
        fonte = tecnicos[-1] if tecnicos else acomps[-1]
        texto = limpar_html(fonte.get("content", ""))
        return texto[:CHARS_SOLUCAO] + ("..." if len(texto) > CHARS_SOLUCAO else "")
    except Exception:
        return ""

def formatar_sugestao_html(similares_com_solucao: list) -> str:
    """Monta o HTML da sugestão privada."""
    linhas = [
        "<p><b>&#x1F916; Sugest&atilde;o Autom&aacute;tica — Base de Conhecimento</b></p>",
        f"<p>Encontrei <b>{len(similares_com_solucao)} chamado(s) similar(es)</b> "
        "j&aacute; resolvido(s). Verifique antes de aplicar:</p>",
        "<hr/>",
    ]
    for s in similares_com_solucao:
        tid     = s["id"]
        titulo  = limpar_html(s.get("titulo", s.get("name", "")))
        solucao = s.get("_solucao", "Sem solução registrada.")
        data    = (s.get("date_mod") or "")[:10]
        linhas += [
            f"<p><b>#{tid}</b> — {titulo}<br/>",
            f"<i>Resolvido em {data}</i><br/>",
            f"<b>Solu&ccedil;&atilde;o:</b> {solucao}</p>",
            "<hr/>",
        ]
    linhas.append(
        "<p><i>&#x26A0; Sugest&atilde;o gerada automaticamente. "
        "Confirme no chamado original antes de aplicar.</i></p>"
    )
    return "\n".join(linhas)

def postar_sugestao_privada(tok, chamado_id: int, conteudo_html: str):
    api_post("ITILFollowup", tok, {
        "input": {
            "items_id": chamado_id,
            "itemtype": "Ticket",
            "content": conteudo_html,
            "is_private": 1,
        }
    })

# ── Entry point ───────────────────────────────────────────────────────
def main():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Agente Base de Conhecimento iniciado")
    processados = carregar_processados()

    tok = get_session()
    try:
        novos = buscar_chamados_novos(tok)
        print(f"  Chamados novos na janela: {len(novos)}")

        for ticket in novos:
            tid    = ticket["id"]
            titulo = ticket.get("name", "")

            if tid in processados:
                print(f"  #{tid} — já processado, pulando")
                continue

            if ja_tem_sugestao_ia(tok, tid):
                print(f"  #{tid} — já tem sugestão IA, pulando")
                processados.add(tid)
                continue

            print(f"  #{tid} — {titulo}")

            # Buscar conteúdo/descrição e entidade do chamado
            kw_desc = []
            entity  = ticket.get("entities_id", "")
            try:
                detalhes = api_get(f"Ticket/{tid}", tok, {"expand_dropdowns": True})
                content  = detalhes.get("content", "")
                entity   = detalhes.get("entities_id") or entity
                kw_desc  = extrair_keywords_descricao(content)
                print(f"    Entidade:           {entity}")
                print(f"    Keywords título:    {extrair_keywords(titulo)}")
                print(f"    Keywords descrição: {kw_desc}")
            except Exception as e:
                print(f"    [!] Não foi possível obter descrição: {e}")

            similares = buscar_similares_resolvidos(tok, titulo, kw_desc, entity=entity)
            if not similares:
                print(f"    Nenhum similar encontrado — será tentado novamente na próxima execução")
                # Não marca como processado: tenta de novo na próxima rodada.
                # Evita marcar permanentemente quando a busca retorna vazia por falta de histórico.
                continue

            # Busca a solução de cada similar
            solicitante = ticket.get("users_id_recipient") or ""
            for s in similares:
                solucao = obter_ultima_solucao(tok, s["id"], solicitante)
                s["_solucao"] = solucao or "Sem solução registrada."
                print(f"    Similar #{s['id']} — solução: {solucao[:60]}...")

            html_sugestao = formatar_sugestao_html(similares)
            postar_sugestao_privada(tok, tid, html_sugestao)
            print(f"    ✓ Sugestão privada postada no chamado #{tid}")

            processados.add(tid)

        salvar_processados(processados)

    finally:
        close_session(tok)

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Concluído\n")

if __name__ == "__main__":
    main()
