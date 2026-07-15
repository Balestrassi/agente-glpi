import os
import json
import re
import requests
import html
from datetime import datetime, timedelta, timezone

BRASILIA = timezone(timedelta(hours=-3))

GLPI_URL = os.environ["GLPI_URL"].rstrip("/")
APP_TOKEN = os.environ["GLPI_APP_TOKEN"]
USER_TOKEN = os.environ["GLPI_USER_TOKEN"]
GLPI_PROFILE_ID = os.environ.get("GLPI_PROFILE_ID", "4")
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# IDs da equipe interna (respostas desses usuarios nao disparam notificacao)
EQUIPE_IDS = {431, 179, 449}  # victor.balestrassi, felipe.aggio, nas.pontes(jhonatan)

IDS_VISTOS_FILE = "ids_vistos.json"

PRIMEIRA_RESPOSTA = """Olá,
Tudo bem?


Obrigado pelo seu contato.
O chamado já foi encaminhado para análise da equipe responsável e, assim que tivermos novidades, retornaremos por aqui com uma atualização.
Ficamos à disposição para qualquer dúvida adicional.


Atenciosamente,
Equipe de Suporte Recebe Mais"""


def carregar_estado():
    if os.path.exists(IDS_VISTOS_FILE):
        with open(IDS_VISTOS_FILE, "r") as f:
            data = json.load(f)
        # Migrar formato antigo (lista simples) para novo formato
        if isinstance(data, list):
            return {"chamados": data, "followups": {}}
        return data
    return {"chamados": [], "followups": {}}


def salvar_estado(estado):
    with open(IDS_VISTOS_FILE, "w") as f:
        json.dump(estado, f)


def get_session():
    r = requests.get(
        f"{GLPI_URL}/apirest.php/initSession",
        headers={"Authorization": f"user_token {USER_TOKEN}", "App-Token": APP_TOKEN},
    )
    r.raise_for_status()
    tok = r.json()["session_token"]
    # Forca o perfil Super-Admin: a conta usada aqui tambem tem um perfil
    # restrito por entidade (Tegma-only). Sem isso, initSession pode herdar
    # o ultimo perfil ativo da conta e esconder chamados de outras entidades
    # (foi exatamente por isso que o chamado 14608, da entidade Messiânica,
    # nao recebeu primeiro atendimento).
    requests.post(
        f"{GLPI_URL}/apirest.php/changeActiveProfile",
        headers={"App-Token": APP_TOKEN, "Session-Token": tok},
        json={"profiles_id": GLPI_PROFILE_ID},
    )
    return tok


def close_session(session_token):
    requests.get(
        f"{GLPI_URL}/apirest.php/killSession",
        headers={"App-Token": APP_TOKEN, "Session-Token": session_token},
    )


def eh_recebemai(ticket):
    """Mesma regra usada em agente_sla.py, agente_reaberturas.py, relatorio_automatico.py
    e dashboard/app.py — mantida idêntica para que um chamado seja tratado de forma
    consistente por todos os agentes (evita chamados aparecerem em uns e não em outros)."""
    entidade  = (ticket.get("entities_id")       or "").lower()
    categoria = (ticket.get("itilcategories_id") or "").lower()
    return "recebe mais" in entidade or "recebemai" in categoria or "recebe mais" in categoria


def buscar_chamados_recebemais(session_token):
    """Busca chamados Recebe Mais em qualquer status aberto (Novo, Em andamento,
    Pendente) — necessário para continuar monitorando respostas do cliente mesmo
    depois que o técnico tira o chamado do status 'Novo'."""
    chamados = []
    for status in (1, 2, 4):
        r = requests.get(
            f"{GLPI_URL}/apirest.php/Ticket",
            headers={"App-Token": APP_TOKEN, "Session-Token": session_token},
            params={
                "searchText[status]": status,
                "expand_dropdowns": True,
                "range": "0-99",
                "order": "DESC",
                "sort": "date_creation",
            },
        )
        data = r.json()
        if isinstance(data, list):
            chamados.extend(data)
    return [c for c in chamados if eh_recebemai(c)]


def buscar_followups(session_token, chamado_id):
    r = requests.get(
        f"{GLPI_URL}/apirest.php/Ticket/{chamado_id}/ITILFollowup/",
        headers={"App-Token": APP_TOKEN, "Session-Token": session_token},
        params={"range": "0-50", "order": "DESC", "sort": "date_creation"},
    )
    data = r.json()
    return data if isinstance(data, list) else []


def postar_primeiro_atendimento(session_token, chamado_id):
    requests.post(
        f"{GLPI_URL}/apirest.php/ITILFollowup",
        headers={
            "App-Token": APP_TOKEN,
            "Session-Token": session_token,
            "Content-Type": "application/json",
        },
        json={
            "input": {
                "items_id": chamado_id,
                "itemtype": "Ticket",
                "content": PRIMEIRA_RESPOSTA,
            }
        },
    )


def enviar_telegram(mensagem):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": mensagem},
            timeout=10,
        )
        return r.status_code == 200
    except Exception as e:
        print(f"Erro ao enviar Telegram: {e}")
        return False


def followup_eh_recente(followup, minutos=10):
    """Retorna True se o followup foi criado nos últimos N minutos (BRT)."""
    data_str = followup.get("date_creation") or followup.get("date", "")
    if not data_str:
        return True
    try:
        data = datetime.strptime(data_str[:19], "%Y-%m-%d %H:%M:%S")
        agora_brt = datetime.now(BRASILIA).replace(tzinfo=None)
        return (agora_brt - data).total_seconds() < minutos * 60
    except Exception:
        return True


def ja_tem_primeiro_atendimento(session_token, chamado_id):
    """Checa nos followups do GLPI se a mensagem já foi enviada — evita duplicatas mesmo com falha de cache."""
    followups = buscar_followups(session_token, chamado_id)
    for f in followups:
        conteudo = f.get("content", "") or ""
        if "Equipe de Suporte Recebe Mais" in conteudo:
            return True
    return False


def dentro_horario_comercial():
    agora = datetime.now(BRASILIA)
    # Segunda=0 ... Sexta=4; 8h <= hora < 18h
    return agora.weekday() < 5 and 8 <= agora.hour < 18


def chamado_e_recente(chamado, horas=2):
    """
    Retorna True só se o chamado foi CRIADO há poucas horas.
    Evita tratar como "novo" um chamado antigo que apenas voltou ao
    status "Novo" (ex: cliente respondeu e o GLPI reabriu automaticamente).
    """
    criado_em = chamado.get("date_creation", "")
    if not criado_em:
        return True
    try:
        criado = datetime.strptime(criado_em[:19], "%Y-%m-%d %H:%M:%S")
        agora_brt = datetime.now(BRASILIA).replace(tzinfo=None)
        return (agora_brt - criado).total_seconds() < horas * 3600
    except Exception:
        return True


def processar_novo_chamado(session_token, chamado):
    chamado_id = chamado["id"]
    titulo = html.unescape(chamado.get("name", "Sem titulo") or "Sem titulo")
    criado_em = chamado.get("date_creation", "")
    solicitante = chamado.get("users_id_recipient", "Desconhecido")

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Novo chamado: #{chamado_id} - {titulo}")

    postar_primeiro_atendimento(session_token, chamado_id)
    print(f"  -> Primeiro atendimento postado no chamado #{chamado_id}")

    mensagem = (
        f"Novo chamado RecebeMais!\n\n"
        f"ID: #{chamado_id}\n"
        f"Titulo: {titulo}\n"
        f"Solicitante: {solicitante}\n"
        f"Aberto em: {criado_em}\n\n"
        f"Primeiro atendimento ja postado automaticamente.\n"
        f"Acesse: {GLPI_URL}/front/ticket.form.php?id={chamado_id}"
    )
    enviar_telegram(mensagem)
    print(f"  -> Notificacao Telegram enviada!")


def verificar_resposta_cliente(session_token, chamado, ultimo_followup_id):
    chamado_id = chamado["id"]
    titulo = html.unescape(chamado.get("name", "Sem titulo") or "Sem titulo")

    followups = buscar_followups(session_token, chamado_id)
    if not followups:
        return ultimo_followup_id

    followup_mais_recente = followups[0]
    novo_id = followup_mais_recente["id"]

    # Se nao ha registro anterior, apenas salva o mais recente sem notificar
    if ultimo_followup_id == 0:
        return novo_id

    # Se o followup mais recente ja foi visto, nada a fazer
    if novo_id <= ultimo_followup_id:
        return ultimo_followup_id

    # Ha followups novos — verifica se algum e do cliente
    for followup in followups:
        if followup["id"] <= ultimo_followup_id:
            break

        autor_id = followup.get("users_id", 0)
        if autor_id not in EQUIPE_IDS and autor_id != 0:
            if not followup_eh_recente(followup, minutos=10):
                print(f"  Followup #{followup['id']} ignorado — criado há mais de 10 min (anti-duplicata)")
                continue
            conteudo_html = html.unescape(followup.get("content", "") or "")
            conteudo = re.sub(r"<[^>]+>", " ", conteudo_html)
            conteudo = re.sub(r"\s+", " ", conteudo).strip()
            conteudo = conteudo[:200] + "..." if len(conteudo) > 200 else conteudo

            mensagem = (
                f"Cliente respondeu no chamado RecebeMais!\n\n"
                f"ID: #{chamado_id}\n"
                f"Titulo: {titulo}\n"
                f"Mensagem: {conteudo}\n\n"
                f"Acesse: {GLPI_URL}/front/ticket.form.php?id={chamado_id}"
            )
            enviar_telegram(mensagem)
            print(f"  -> Cliente respondeu no #{chamado_id}, notificacao enviada!")
            break

    return novo_id


def main():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Iniciando verificacao...")

    estado = carregar_estado()
    chamados_vistos = set(estado["chamados"])
    followups_vistos = estado["followups"]

    session = get_session()
    chamados = buscar_chamados_recebemais(session)

    # Primeira execucao: registra tudo como visto sem notificar (independe do horario)
    if not chamados_vistos and chamados:
        chamados_vistos = {c["id"] for c in chamados}
        for c in chamados:
            followups = buscar_followups(session, c["id"])
            followups_vistos[str(c["id"])] = followups[0]["id"] if followups else 0
        salvar_estado({"chamados": list(chamados_vistos), "followups": followups_vistos})
        print(f"{len(chamados_vistos)} chamados existentes registrados.")
        close_session(session)
        return

    if not dentro_horario_comercial():
        agora = datetime.now(BRASILIA)
        print(f"[{agora.strftime('%H:%M')}] Fora do horario comercial (08h-18h, seg-sex). Nenhuma acao.")
        close_session(session)
        return

    novos = [c for c in chamados if c["id"] not in chamados_vistos]

    # Processa chamados novos
    for chamado in novos:
        cid = chamado["id"]
        status = chamado.get("status")
        if status != 1:
            print(f"  Chamado #{cid} visto pela primeira vez já em status {status} — registrado sem primeiro atendimento.")
        elif ja_tem_primeiro_atendimento(session, cid):
            print(f"  Chamado #{cid} já tem primeiro atendimento — adicionando ao cache sem reenviar")
        else:
            processar_novo_chamado(session, chamado)
        chamados_vistos.add(cid)
        followups = buscar_followups(session, cid)
        followups_vistos[str(cid)] = followups[0]["id"] if followups else 0
        # Salva a cada chamado: se uma falha de rede interromper o loop no
        # meio, notificacoes ja enviadas nao sao reenviadas no proximo run.
        salvar_estado({"chamados": list(chamados_vistos), "followups": followups_vistos})

    # Verifica respostas de clientes em chamados ja conhecidos
    for chamado in chamados:
        if chamado["id"] in chamados_vistos and chamado["id"] not in {c["id"] for c in novos}:
            chave = str(chamado["id"])
            ultimo_id = followups_vistos.get(chave, 0)
            novo_ultimo_id = verificar_resposta_cliente(session, chamado, ultimo_id)
            followups_vistos[chave] = novo_ultimo_id
            salvar_estado({"chamados": list(chamados_vistos), "followups": followups_vistos})

    close_session(session)

    if not novos:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Nenhum chamado novo.")
    else:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {len(novos)} chamado(s) processado(s).")


if __name__ == "__main__":
    main()
