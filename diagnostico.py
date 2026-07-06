import os
import requests
from datetime import datetime, timedelta

GLPI_URL   = os.environ.get("GLPI_URL", "https://servicedesk.a7on.ai")
APP_TOKEN  = os.environ["GLPI_APP_TOKEN"]
USER_TOKEN = os.environ["GLPI_USER_TOKEN"]
GLPI_PROFILE_ID = os.environ.get("GLPI_PROFILE_ID", "4")

def get_session():
    r = requests.get(f"{GLPI_URL}/apirest.php/initSession",
        headers={"Authorization": f"user_token {USER_TOKEN}", "App-Token": APP_TOKEN}, timeout=15)
    tok = r.json()["session_token"]
    # Forca o perfil Super-Admin: a conta usada aqui tambem tem um perfil
    # restrito por entidade (Tegma-only). Sem isso, initSession pode herdar
    # o ultimo perfil ativo da conta e esconder chamados de outras entidades.
    requests.post(f"{GLPI_URL}/apirest.php/changeActiveProfile",
        headers={"App-Token": APP_TOKEN, "Session-Token": tok},
        json={"profiles_id": GLPI_PROFILE_ID}, timeout=15)
    return tok

def api_get(path, tok, params=None):
    r = requests.get(f"{GLPI_URL}/apirest.php/{path}",
        headers={"App-Token": APP_TOKEN, "Session-Token": tok},
        params=params, timeout=15)
    return r.json()

tok = get_session()

# 1. Detalhes do ticket 14417
t = api_get("Ticket/14417", tok, {"expand_dropdowns": True})
criacao = t.get("date_creation", "")
print("=== TICKET 14417 ===")
print(f"  Nome:      {t.get('name')}")
print(f"  Status:    {t.get('status')}")
print(f"  Entidade:  {t.get('entities_id')}")
print(f"  Criado em: {criacao}")

# 2. Análise da janela de tempo (GitHub Actions roda em UTC)
agora_utc = datetime.utcnow()
limite_utc = agora_utc - timedelta(minutes=120)
print()
print("=== JANELA DE TEMPO ===")
print(f"  Criacao GLPI:    {criacao}")
print(f"  Agora (UTC):     {agora_utc.strftime('%Y-%m-%d %H:%M:%S')}")
print(f"  Limite (UTC-2h): {limite_utc.strftime('%Y-%m-%d %H:%M:%S')}")
print(f"  Dentro da janela (comparacao string)? {criacao >= limite_utc.strftime('%Y-%m-%d %H:%M:%S')}")

# 3. O searchText[status]=1 realmente filtra por status?
tickets_s1 = api_get("Ticket", tok, {
    "expand_dropdowns": True, "range": "0-49",
    "sort": "date_creation", "order": "DESC",
    "searchText[status]": 1,
})
print()
print("=== searchText[status]=1 ===")
if isinstance(tickets_s1, list):
    ids = [x.get("id") for x in tickets_s1]
    statuses = set(x.get("status") for x in tickets_s1)
    print(f"  Total retornado: {len(tickets_s1)}")
    print(f"  Status presentes: {statuses}")
    print(f"  14417 aparece? {14417 in ids}")
else:
    print(f"  Resposta: {tickets_s1}")

# 4. Busca sem filtro de status
tickets_all = api_get("Ticket", tok, {
    "expand_dropdowns": True, "range": "0-49",
    "sort": "date_creation", "order": "DESC",
})
print()
print("=== SEM FILTRO DE STATUS ===")
if isinstance(tickets_all, list):
    ids2 = [x.get("id") for x in tickets_all]
    print(f"  Total retornado: {len(tickets_all)}")
    print(f"  14417 aparece? {14417 in ids2}")
    # Mostra os 5 mais recentes com status
    print("  5 mais recentes:")
    for x in tickets_all[:5]:
        print(f"    #{x.get('id')} status={x.get('status')} criado={x.get('date_creation','')[:16]}")

# 5. Followups do ticket 14417 (tem sugestão IA?)
followups = api_get(f"Ticket/14417/ITILFollowup", tok)
print()
print("=== FOLLOWUPS 14417 ===")
if isinstance(followups, list):
    for f in followups:
        privado = "(privado)" if f.get("is_private") else "(publico)"
        conteudo = (f.get("content") or "")[:60].replace("\n", " ")
        print(f"  {privado} user={f.get('users_id')} | {conteudo}")
else:
    print(f"  {followups}")

requests.get(f"{GLPI_URL}/apirest.php/killSession",
    headers={"App-Token": APP_TOKEN, "Session-Token": tok}, timeout=10)
