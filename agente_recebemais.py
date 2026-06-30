import os
import json
import requests
import html
from datetime import datetime

GLPI_URL = os.environ["GLPI_URL"].rstrip("/")
APP_TOKEN = os.environ["GLPI_APP_TOKEN"]
USER_TOKEN = os.environ["GLPI_USER_TOKEN"]
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
    return r.json()["session_token"]


def close_session(session_token):
    requests.get(
        f"{GLPI_URL}/apirest.php/killSession",
        headers={"App-Token": APP_TOKEN, "Session-Token": session_token},
    )


def buscar_chamados_recebemais(session_token):
    r = requests.get(
        f"{GLPI_URL}/apirest.php/Ticket",
        headers={"App-Token": APP_TOKEN, "Session-Token": session_token},
        params={
            "searchText[status]": 1,
            "expand_dropdowns": True,
            "range": "0-99",
            "order": "DESC",
            "sort": "date_creation",
        },
    )
    data = r.json()
    if not isinstance(data, list):
        return []
    return [
        c for c in data
        if "recebe" in str(c.get("entities_id", "")).lower()
        or "recebemais" in str(c.get("name", "")).lower()
        or "recebemais" in str(c.get("itilcategories_id", "")).lower()
    ]


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


def ja_tem_primeiro_atendimento(session_token, chamado_id):
    """Checa nos followups do GLPI se a mensagem já foi enviada — evita duplicatas mesmo com falha de cache."""
    followups = buscar_followups(session_token, chamado_id)
    for f in followups:
        conteudo = f.get("content", "") or ""
        if "Equipe de Suporte Recebe Mais" in conteudo:
            return True
    return False


def processar_novo_chamado(session_token, chamado):
    chamado_id = chamado["id"]
    titulo = chamado.get("name", "Sem titulo")
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
    titulo = chamado.get("name", "Sem titulo")

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
            conteudo_html = followup.get("content", "")
            conteudo = html.unescape(conteudo_html).replace("<p>", "").replace("</p>", " ").replace("<br>", " ").strip()
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

    # Primeira execucao: registra tudo como visto sem notificar
    if not chamados_vistos and chamados:
        chamados_vistos = {c["id"] for c in chamados}
        for c in chamados:
            followups = buscar_followups(session, c["id"])
            followups_vistos[str(c["id"])] = followups[0]["id"] if followups else 0
        salvar_estado({"chamados": list(chamados_vistos), "followups": followups_vistos})
        print(f"{len(chamados_vistos)} chamados existentes registrados.")
        close_session(session)
        return

    novos = [c for c in chamados if c["id"] not in chamados_vistos]

    # Processa chamados novos
    for chamado in novos:
        cid = chamado["id"]
        if ja_tem_primeiro_atendimento(session, cid):
            print(f"  Chamado #{cid} já tem primeiro atendimento — adicionando ao cache sem reenviar")
        else:
            processar_novo_chamado(session, chamado)
        chamados_vistos.add(cid)
        followups = buscar_followups(session, cid)
        followups_vistos[str(cid)] = followups[0]["id"] if followups else 0

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
