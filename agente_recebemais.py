import os
import json
import requests
from datetime import datetime

GLPI_URL = os.environ["GLPI_URL"].rstrip("/")
APP_TOKEN = os.environ["GLPI_APP_TOKEN"]
USER_TOKEN = os.environ["GLPI_USER_TOKEN"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

PALAVRA_CHAVE = "RecebeMais"
IDS_VISTOS_FILE = "ids_vistos.json"

PRIMEIRA_RESPOSTA = """Olá,
Tudo bem?


Obrigado pelo seu contato.
O chamado já foi encaminhado para análise da equipe responsável e, assim que tivermos novidades, retornaremos por aqui com uma atualização.
Ficamos à disposição para qualquer dúvida adicional.


Atenciosamente,
Equipe de Suporte Recebe Mais"""


def carregar_ids_vistos():
    if os.path.exists(IDS_VISTOS_FILE):
        with open(IDS_VISTOS_FILE, "r") as f:
            return set(json.load(f))
    return set()


def salvar_ids_vistos(ids):
    with open(IDS_VISTOS_FILE, "w") as f:
        json.dump(list(ids), f)


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


def processar_chamado(session_token, chamado):
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


def main():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Iniciando verificacao...")

    ids_vistos = carregar_ids_vistos()

    session = get_session()
    chamados = buscar_chamados_recebemais(session)

    if not ids_vistos and chamados:
        ids_vistos = {c["id"] for c in chamados}
        salvar_ids_vistos(ids_vistos)
        print(f"{len(ids_vistos)} chamados existentes registrados.")
        close_session(session)
        return

    novos = [c for c in chamados if c["id"] not in ids_vistos]

    for chamado in novos:
        processar_chamado(session, chamado)
        ids_vistos.add(chamado["id"])

    salvar_ids_vistos(ids_vistos)
    close_session(session)

    if not novos:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Nenhum chamado novo.")
    else:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {len(novos)} chamado(s) processado(s).")


if __name__ == "__main__":
    main()
