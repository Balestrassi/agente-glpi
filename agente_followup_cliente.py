"""
Agente de Follow-up ao Cliente — GLPI
Quando um chamado Recebe Mais fica sem NENHUMA movimentação real da nossa
equipe (excluindo mensagens automáticas), posta uma mensagem de
acompanhamento diretamente no chamado, visível ao cliente, escalonando o
tom conforme o tempo passa:
  • Nível 1 — 2h sem movimentação da equipe
  • Nível 2 — 4h sem movimentação da equipe
  • Nível 3 — 8h sem movimentação da equipe

Cada nível é postado uma única vez. Se a equipe responder de verdade
(followup humano, sem a assinatura automática), o relógio zera e o
chamado sai do cache — voltará a escalar do nível 1 se ficar parado de novo.
"""

import os
import re
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

def ultimo_toque_real_da_equipe(tok, tid):
    """
    Retorna a data do último followup que representa uma resposta HUMANA
    da nossa equipe — excluindo qualquer mensagem automática (identificada
    pela assinatura padrão) e mensagens do próprio solicitante.
    """
    followups = api_get(f"Ticket/{tid}/ITILFollowup", tok)
    if not isinstance(followups, list) or not followups:
        return None
    for f in followups:
        conteudo = f.get("content", "") or ""
        if ASSINATURA_AUTOMATICA in conteudo:
            continue
        # followups já vêm ordenados; o primeiro não-automático é o mais recente
        data_str = f.get("date") or f.get("date_creation")
        try:
            return datetime.strptime(data_str[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
    return None

def horas_desde_brt(dt):
    if dt is None:
        return None
    return (datetime.now(BRASILIA).replace(tzinfo=None) - dt).total_seconds() / 3600

def nivel_escalada(horas):
    if horas >= NIVEL_HORAS[3]: return 3
    if horas >= NIVEL_HORAS[2]: return 2
    if horas >= NIVEL_HORAS[1]: return 1
    return 0

def postar_followup(tok, tid, mensagem):
    requests.post(
        f"{GLPI_URL}/apirest.php/ITILFollowup",
        headers={**_h(tok), "Content-Type": "application/json"},
        json={"input": {"items_id": tid, "itemtype": "Ticket", "content": mensagem}},
        timeout=15,
    )

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

            ultimo = ultimo_toque_real_da_equipe(tok, t["id"])
            if ultimo is not None:
                horas_ref = horas_desde_brt(ultimo)
            else:
                criacao = t.get("date_creation") or ""
                try:
                    dt_criacao = datetime.strptime(criacao[:19], "%Y-%m-%d %H:%M:%S")
                    horas_ref = horas_desde_brt(dt_criacao)
                except Exception:
                    continue

            nivel_atual = nivel_escalada(horas_ref)
            nivel_cache = cache.get(tid, {}).get("nivel", 0)

            if nivel_atual == 0:
                # Equipe respondeu de verdade — reseta o acompanhamento
                cache.pop(tid, None)
                continue

            if nivel_atual > nivel_cache:
                mensagem = MENSAGENS[nivel_atual]
                postar_followup(tok, t["id"], mensagem)
                cache[tid] = {"nivel": nivel_atual, "ts": agora_brt.isoformat()}
                postados.append({
                    "id": t["id"], "nivel": nivel_atual,
                    "titulo": html.unescape(t.get("name", "") or ""),
                    "horas": round(horas_ref, 1),
                })
                print(f"  Chamado #{t['id']}: follow-up nível {nivel_atual} postado ({horas_ref:.1f}h sem movimentação)")

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
                linhas.append(f"  _{p['horas']}h sem movimentação da equipe_")
            linhas.append(f"\n_Verificado em {agora_brt.strftime('%d/%m/%Y %H:%M')} BRT_")
            enviar_telegram("\n".join(linhas))
        else:
            print("  Nenhum follow-up necessário.")

    finally:
        close_session(tok)

    print(f"[{datetime.now(BRASILIA).replace(tzinfo=None).strftime('%H:%M')}] Concluído")

if __name__ == "__main__":
    main()
