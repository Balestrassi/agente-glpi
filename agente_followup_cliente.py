"""
Agente de Follow-up ao Cliente — GLPI
Acompanha chamados Recebe Mais que têm APENAS o primeiro atendimento
automático — ou seja, ninguém (nem o cliente nem um técnico) interagiu
além da mensagem automática de acolhimento. Nesses casos posta uma
mensagem de acompanhamento visível ao cliente, escalonando o tom conforme
o tempo desde a ABERTURA do chamado:
  • Nível 1 — 2h desde a abertura, ainda só com o primeiro atendimento
  • Nível 2 — 4h desde a abertura, ainda só com o primeiro atendimento
  • Nível 3 — 8h desde a abertura, ainda só com o primeiro atendimento

Cada nível é postado uma única vez. Assim que QUALQUER followup não
automático aparece (resposta de técnico OU do próprio cliente), o chamado
deixa de ser elegível e sai do cache — nenhum follow-up adicional é postado.
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
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN",  "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID","")

BRASILIA    = timezone(timedelta(hours=-3))
CACHE_FILE  = Path(__file__).parent / "followup_cliente_cache.json"

# Trecho usado para identificar (e excluir) qualquer mensagem automática
# já postada por nós — inclusive o "primeiro atendimento" e os próprios
# follow-ups deste agente — para que não contem como "movimentação real".
ASSINATURA_AUTOMATICA = "Equipe de Suporte Recebe Mais"

NIVEL_HORAS = {1: 2, 2: 4, 3: 8}

# Trecho único de cada nível — usado para detectar no GLPI se o nível
# já foi postado mesmo quando o cache do GitHub Actions se perde.
FRASES_NIVEL = {
    1: "chamado continua em análise pela nossa equipe técnica",
    2: "Seu chamado segue em análise pela equipe responsável",
    3: "Pedimos desculpas pela demora no retorno",
}

MENSAGENS = {
    1: """Olá,

Passando para informar que seu chamado continua em análise pela nossa equipe técnica. Assim que houver uma atualização, retornaremos por aqui.

Agradecemos a paciência!


Atenciosamente,
Equipe de Suporte Recebe Mais""",

    2: """Olá,

Seu chamado segue em análise pela equipe responsável. Sabemos que já se passou algum tempo desde a abertura e queremos garantir que ele não foi esquecido — está sendo tratado com atenção.

Retornaremos assim que tivermos uma novidade concreta.


Atenciosamente,
Equipe de Suporte Recebe Mais""",

    3: """Olá,

Pedimos desculpas pela demora no retorno. Seu chamado ainda está em análise e estamos priorizando uma resposta o quanto antes.

Caso seja algo urgente, fique à vontade para responder por aqui que aceleramos o acompanhamento.


Atenciosamente,
Equipe de Suporte Recebe Mais""",
}

NIVEL_EMOJI = {1: "🕑", 2: "🕓", 3: "🕗"}

# ── Persistência ──────────────────────────────────────────────────────
def carregar_cache() -> dict:
    if CACHE_FILE.exists():
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    return {}

def salvar_cache(cache: dict):
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")

# ── GLPI API ─────────────────────────────────────────────────────────
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
    r = requests.get(f"{GLPI_URL}/apirest.php/{path}", headers=_h(tok), params=params, timeout=30)
    if r.status_code in (200, 206):
        d = r.json()
        return d if isinstance(d, list) else d
    return []

def eh_recebemai(ticket):
    entidade  = (ticket.get("entities_id")       or "").lower()
    categoria = (ticket.get("itilcategories_id") or "").lower()
    return "recebe mais" in entidade or "recebemai" in categoria or "recebe mais" in categoria

def buscar_abertos(tok):
    tickets = []
    for status in (1, 2, 4):
        resultado = api_get("Ticket", tok, {
            "expand_dropdowns": True, "range": "0-199",
            "sort": "date_creation", "order": "ASC",
            "searchText[status]": status,
        })
        if isinstance(resultado, list):
            tickets.extend(resultado)
    return [t for t in tickets if eh_recebemai(t)]

def _fetch_followups(tok, tid):
    """Busca os followups do chamado. Retorna a lista, ou None se a busca
    falhar por qualquer motivo (rede, status != 200, JSON inválido).
    None é tratado como 'não sei' e leva ao fail-safe (não postar)."""
    try:
        r = requests.get(
            f"{GLPI_URL}/apirest.php/Ticket/{tid}/ITILFollowup",
            headers=_h(tok),
            params={"range": "0-100", "sort": "date_creation", "order": "ASC"},
            timeout=30,
        )
    except Exception:
        return None
    if r.status_code not in (200, 206):
        return None
    try:
        d = r.json()
    except Exception:
        return None
    return d if isinstance(d, list) else None


def _requester_id(tok, tid):
    """ID numérico do solicitante do chamado (sem expand_dropdowns, para vir
    o id e não o nome). Retorna None se não conseguir determinar."""
    t = api_get(f"Ticket/{tid}", tok)
    if isinstance(t, dict):
        return t.get("users_id_recipient")
    return None


def ticket_apenas_primeiro_atendimento(tok, tid, requester_id):
    """
    True somente se o chamado tiver APENAS mensagens automáticas nossas
    (o "primeiro atendimento" e/ou follow-ups já postados por este agente,
    todos com a assinatura padrão) — ou seja, ninguém, nem o cliente nem
    um técnico, adicionou qualquer outro followup.

    Fail-safe: se a busca dos followups falhar (None), retorna False — na
    dúvida, NÃO posta. Também retorna False se não houver nenhum followup
    (nem o primeiro atendimento ainda postado).

    Um followup desqualifica o chamado se:
      - for do próprio solicitante (mesmo que cite nossa assinatura numa
        resposta por e-mail que reproduz a mensagem anterior), ou
      - não contiver a assinatura automática (resposta real de um técnico).
    """
    followups = _fetch_followups(tok, tid)
    if not followups:  # None (erro) ou lista vazia
        return False
    for f in followups:
        conteudo = f.get("content", "") or ""
        autor    = f.get("users_id")
        if requester_id is not None and autor == requester_id:
            return False
        if ASSINATURA_AUTOMATICA not in conteudo:
            return False
    return True

def horas_desde_brt(dt):
    if dt is None:
        return None
    return (datetime.now(BRASILIA).replace(tzinfo=None) - dt).total_seconds() / 3600

def em_horario_comercial(agora_brt: datetime) -> bool:
    """Retorna True somente entre 08h–18h BRT de segunda a sexta."""
    if agora_brt.weekday() >= 5:   # sábado=5, domingo=6
        return False
    return 8 <= agora_brt.hour < 18

def niveis_ja_enviados_glpi(tok, tid) -> set:
    """Detecta quais níveis de follow-up já foram postados no chamado,
    lendo o conteúdo dos followups do GLPI — fonte da verdade quando o
    cache do GitHub Actions se perde entre execuções."""
    followups = _fetch_followups(tok, tid)
    if not followups:
        return set()
    encontrados = set()
    for f in followups:
        conteudo = f.get("content", "") or ""
        for nivel, frase in FRASES_NIVEL.items():
            if frase in conteudo:
                encontrados.add(nivel)
    return encontrados

def nivel_escalada(horas):
    if horas >= NIVEL_HORAS[3]: return 3
    if horas >= NIVEL_HORAS[2]: return 2
    if horas >= NIVEL_HORAS[1]: return 1
    return 0

def postar_followup(tok, tid, mensagem):
    """Posta o followup e retorna True só se o GLPI confirmar (200/201)."""
    try:
        r = requests.post(
            f"{GLPI_URL}/apirest.php/ITILFollowup",
            headers={**_h(tok), "Content-Type": "application/json"},
            json={"input": {"items_id": tid, "itemtype": "Ticket", "content": mensagem}},
            timeout=15,
        )
        return r.status_code in (200, 201)
    except Exception:
        return False

def enviar_telegram(texto):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": texto, "parse_mode": "Markdown",
                  "disable_web_page_preview": True},
            timeout=15,
        )
    except Exception:
        pass

# ── Main ──────────────────────────────────────────────────────────────
def main():
    agora_brt = datetime.now(BRASILIA).replace(tzinfo=None)
    print(f"[{agora_brt.strftime('%H:%M')} BRT] Agente Follow-up Cliente iniciado")

    if not em_horario_comercial(agora_brt):
        print("  Fora do horário comercial (08h–18h seg–sex) — nenhum follow-up enviado.")
        return

    cache = carregar_cache()
    tok = get_session()
    try:
        tickets = buscar_abertos(tok)
        print(f"  Chamados abertos Recebe Mais: {len(tickets)}")

        ids_abertos = set()
        postados = []

        for t in tickets:
            tid = str(t["id"])
            ids_abertos.add(tid)

            # Só acompanha chamados que têm APENAS o primeiro atendimento
            # automático. Qualquer interação (do cliente ou de um técnico)
            # tira o chamado do acompanhamento.
            requester_id = _requester_id(tok, t["id"])
            if not ticket_apenas_primeiro_atendimento(tok, t["id"], requester_id):
                cache.pop(tid, None)
                continue

            # Escalonamento medido a partir da criação do chamado
            criacao = t.get("date_creation") or ""
            try:
                dt_criacao = datetime.strptime(criacao[:19], "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
            horas_ref = horas_desde_brt(dt_criacao)

            nivel_atual = nivel_escalada(horas_ref)
            nivel_cache = cache.get(tid, {}).get("nivel", 0)

            # Detecta do próprio GLPI quais níveis já foram enviados —
            # evita re-postagem quando o cache do GitHub Actions se perde.
            ja_glpi = niveis_ja_enviados_glpi(tok, t["id"])
            nivel_max_glpi = max(ja_glpi) if ja_glpi else 0
            nivel_referencia = max(nivel_cache, nivel_max_glpi)

            if nivel_atual == 0 or nivel_atual <= nivel_referencia:
                continue

            # Só grava no cache e reporta se o GLPI confirmar a postagem.
            # Salva o cache logo após postar (incremental) para não repostar
            # o mesmo nível caso o processo caia antes do salvamento final.
            if postar_followup(tok, t["id"], MENSAGENS[nivel_atual]):
                cache[tid] = {"nivel": max(nivel_atual, nivel_max_glpi), "ts": agora_brt.isoformat()}
                salvar_cache(cache)
                postados.append({
                    "id": t["id"], "nivel": nivel_atual,
                    "titulo": html.unescape(t.get("name", "") or ""),
                    "horas": round(horas_ref, 1),
                })
                print(f"  Chamado #{t['id']}: follow-up nível {nivel_atual} postado ({horas_ref:.1f}h desde a abertura, só com primeiro atendimento)")
            else:
                print(f"  Chamado #{t['id']}: FALHA ao postar follow-up nível {nivel_atual} — será tentado na próxima execução")

        # Remove do cache chamados que não estão mais abertos
        for tid in list(cache.keys()):
            if tid not in ids_abertos:
                cache.pop(tid, None)

        salvar_cache(cache)

        if postados:
            linhas = [f"📨 *{len(postados)} follow-up(s) automático(s) enviado(s) ao cliente*\n"]
            for p in postados:
                link = f"{GLPI_URL}/front/ticket.form.php?id={p['id']}"
                titulo_esc = p['titulo'].replace('_', '\\_').replace('*', '\\*')[:60]
                linhas.append(f"• [\\#{p['id']}]({link}) {NIVEL_EMOJI[p['nivel']]} nível {p['nivel']} — {titulo_esc}")
                linhas.append(f"  _{p['horas']}h desde a abertura, ainda só com o primeiro atendimento_")
            linhas.append(f"\n_Verificado em {agora_brt.strftime('%d/%m/%Y %H:%M')} BRT_")
            enviar_telegram("\n".join(linhas))
        else:
            print("  Nenhum follow-up necessário.")

    finally:
        close_session(tok)

    print(f"[{datetime.now(BRASILIA).replace(tzinfo=None).strftime('%H:%M')}] Concluído")

if __name__ == "__main__":
    main()
