"""
Popular Base de Conhecimento — GLPI
Quando um técnico resolve um chamado Recebe Mais com solução registrada,
cria automaticamente um artigo na base de conhecimento do GLPI.
Roda a cada 2 horas via GitHub Actions.

Fontes de solução (em ordem de preferência):
  1. ITILSolution (solução formal registrada no chamado)
  2. Último followup do técnico (excluindo respostas automáticas)
"""

import os
import json
import re
import html as html_module
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path

GLPI_URL   = os.environ.get("GLPI_URL",        "https://servicedesk.a7on.ai")
APP_TOKEN  = os.environ.get("GLPI_APP_TOKEN",  "")
USER_TOKEN = os.environ.get("GLPI_USER_TOKEN", "")
GLPI_PROFILE_ID = os.environ.get("GLPI_PROFILE_ID", "4")

BRASILIA          = timezone(timedelta(hours=-3))
JANELA_HORAS      = 48    # busca tickets resolvidos nas últimas 48h
MIN_CHARS_SOLUCAO = 40    # solução muito curta não tem valor real

PROCESSADOS_FILE = Path(__file__).parent / "kb_artigos_criados.json"

# Conteúdos que indicam resposta automática — não devem virar artigo
CONTEUDO_EXCLUIR = [
    "Obrigado pelo seu contato",
    "encaminhado para análise",
    "Base de Conhecimento",
    "Sugestão Automática",
    "Equipe de Suporte Recebe Mais",
]

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

def api_post(path, tok, body):
    r = requests.post(
        f"{GLPI_URL}/apirest.php/{path}",
        headers={**_h(tok), "Content-Type": "application/json"},
        json=body, timeout=20,
    )
    r.raise_for_status()
    return r.json()

# ── Persistência ──────────────────────────────────────────────────────
def carregar_processados() -> set:
    if PROCESSADOS_FILE.exists():
        return set(json.loads(PROCESSADOS_FILE.read_text(encoding="utf-8")))
    return set()

def salvar_processados(ids: set):
    PROCESSADOS_FILE.write_text(
        json.dumps(sorted(ids), ensure_ascii=False, indent=2), encoding="utf-8"
    )

# ── Filtro Recebe Mais ────────────────────────────────────────────────
SOLICITANTES_EXCLUIDOS = frozenset({"marlon.james", "vinicius.ariston", "marlon james", "vinicius ariston", "vinícius ariston"})


def eh_recebemai(ticket):
    sol = (ticket.get("users_id_recipient") or "").lower()
    if any(s in sol for s in SOLICITANTES_EXCLUIDOS):
        return False
    entidade  = (ticket.get("entities_id")       or "").lower()
    categoria = (ticket.get("itilcategories_id") or "").lower()
    return "recebe mais" in entidade or "recebemai" in categoria or "recebe mais" in categoria

# ── Texto limpo ───────────────────────────────────────────────────────
def limpar_html(texto: str) -> str:
    texto = html_module.unescape(texto or "")
    texto = re.sub(r"<[^>]+>", " ", texto)
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto

def conteudo_automatico(texto: str) -> bool:
    """True se o texto é uma resposta automática sem valor para a KB."""
    tl = texto.lower()
    return any(ex.lower() in tl for ex in CONTEUDO_EXCLUIR)

def solucao_valida(texto: str) -> bool:
    if not texto:
        return False
    texto = texto.strip()
    if len(texto) < MIN_CHARS_SOLUCAO:
        return False
    if conteudo_automatico(texto):
        return False
    return True

# ── Busca da solução ──────────────────────────────────────────────────
def buscar_solucao(tok, tid: int, requester_id) -> tuple[str | None, str]:
    """
    Retorna (texto_solucao, fonte) onde fonte é 'ITILSolution' ou 'followup'.
    Retorna (None, '') se não houver solução válida.
    """
    # 1ª opção: solução formal registrada no chamado
    try:
        solucoes = api_get(f"Ticket/{tid}/ITILSolution", tok)
        if isinstance(solucoes, list) and solucoes:
            txt = limpar_html(solucoes[-1].get("content") or "")
            if solucao_valida(txt):
                return txt, "ITILSolution"
    except Exception as e:
        print(f"    [!] ITILSolution erro: {e}")

    # 2ª opção: último followup do técnico (não-solicitante, não-automático)
    try:
        followups = api_get(f"Ticket/{tid}/ITILFollowup", tok)
        if isinstance(followups, list):
            tecnicos = [
                f for f in followups
                if f.get("users_id") != requester_id
                and not conteudo_automatico(f.get("content") or "")
            ]
            if tecnicos:
                txt = limpar_html(tecnicos[-1].get("content") or "")
                if solucao_valida(txt):
                    return txt, "followup"
    except Exception as e:
        print(f"    [!] Followup erro: {e}")

    return None, ""

# ── Criação do artigo na KB do GLPI ──────────────────────────────────
def criar_artigo_kb(tok, titulo: str, solucao: str, entities_id_num,
                    ticket_id: int) -> int | None:
    """
    Cria artigo na base de conhecimento do GLPI.
    Retorna o ID do artigo criado ou None em caso de falha.
    """
    resposta_html = (
        f"<p>{solucao}</p>"
        f"<hr/>"
        f"<p><em>Artigo gerado automaticamente a partir do chamado "
        f"<strong>#{ticket_id}</strong>. Revise antes de publicar.</em></p>"
    )
    corpo = {
        "input": {
            "name":   titulo,
            "answer": resposta_html,
            "is_faq": 0,
        }
    }
    if entities_id_num:
        corpo["input"]["entities_id"] = entities_id_num

    try:
        resultado = api_post("KnowledgebaseItem", tok, corpo)
        if isinstance(resultado, dict):
            return resultado.get("id")
    except Exception as e:
        print(f"    [!] Erro ao criar artigo: {e}")
    return None

# ── Busca tickets resolvidos ──────────────────────────────────────────
def buscar_resolvidos(tok, limite_str: str) -> list:
    """Busca tickets Recebe Mais resolvidos/fechados na janela de tempo."""
    tickets = []
    for status in (5, 6):
        offset = 0
        while True:
            r = requests.get(
                f"{GLPI_URL}/apirest.php/Ticket",
                headers=_h(tok),
                params={
                    "expand_dropdowns": True,
                    "range": f"{offset}-{offset+99}",
                    "sort": "date_mod",
                    "order": "DESC",
                    "searchText[status]": status,
                },
                timeout=30,
            )
            if r.status_code not in (200, 206):
                break
            data = r.json()
            if not isinstance(data, list) or not data:
                break

            passou_janela = False
            for t in data:
                dm = t.get("date_mod") or ""
                if dm < limite_str:
                    passou_janela = True
                    break
                if eh_recebemai(t):
                    tickets.append(t)

            if passou_janela or len(data) < 100:
                break
            offset += 100

    # Deduplicar por ID (ticket pode aparecer nos dois status loops)
    vistos = set()
    unicos = []
    for t in tickets:
        if t["id"] not in vistos:
            vistos.add(t["id"])
            unicos.append(t)
    return unicos

# ── Main ──────────────────────────────────────────────────────────────
def main():
    agora     = datetime.now(BRASILIA).replace(tzinfo=None)
    limite    = (agora - timedelta(hours=JANELA_HORAS)).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{agora.strftime('%H:%M')} BRT] Popular Base de Conhecimento")
    print(f"  Janela: últimas {JANELA_HORAS}h (desde {limite})")

    processados = carregar_processados()

    tok = get_session()
    try:
        tickets = buscar_resolvidos(tok, limite)
        novos   = [t for t in tickets if t["id"] not in processados]
        print(f"  Tickets RM resolvidos na janela: {len(tickets)} | Não processados: {len(novos)}")

        criados  = 0
        pulados  = 0

        for t in novos:
            tid    = t["id"]
            titulo = (t.get("name") or f"Chamado #{tid}").strip()

            print(f"\n  #{tid} — {titulo[:60]}")

            # Busca o ticket sem expand_dropdowns: entities_id e
            # users_id_recipient precisam vir como IDs numéricos aqui —
            # com expand_dropdowns (usado em buscar_resolvidos) esses campos
            # viram nome/texto, o que quebra a comparação
            # "f.get('users_id') != requester_id" dentro de buscar_solucao.
            t_raw           = api_get(f"Ticket/{tid}", tok)
            entities_id_num = t_raw.get("entities_id") if isinstance(t_raw, dict) else None
            req_id          = t_raw.get("users_id_recipient") if isinstance(t_raw, dict) else None

            solucao, fonte = buscar_solucao(tok, tid, req_id)

            if not solucao:
                print(f"    → Sem solução válida, pulando")
                processados.add(tid)
                pulados += 1
                salvar_processados(processados)
                continue

            print(f"    → Solução via {fonte}: {solucao[:80]}...")

            kb_id = criar_artigo_kb(tok, titulo, solucao, entities_id_num, tid)
            if kb_id:
                print(f"    ✓ Artigo #{kb_id} criado na base de conhecimento")
                criados += 1
            else:
                print(f"    ✗ Falha ao criar artigo (permissão ou API indisponível)")

            processados.add(tid)
            # Salva a cada ticket: se uma falha de rede interromper o loop no
            # meio, os artigos já criados não voltam a ser reprocessados
            # (evita artigo de KB duplicado no próximo run).
            salvar_processados(processados)
        print(f"\n  Resumo: {criados} artigo(s) criado(s), {pulados} sem solução registrada")

    finally:
        close_session(tok)

    print(f"[{datetime.now(BRASILIA).replace(tzinfo=None).strftime('%H:%M')}] Concluído")

if __name__ == "__main__":
    main()
