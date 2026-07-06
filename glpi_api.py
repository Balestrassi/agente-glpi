"""
Módulo compartilhado de acesso à API REST do GLPI.

Credenciais NUNCA ficam hardcoded aqui. São carregadas, nesta ordem:
  1. Variáveis de ambiente (GLPI_URL, GLPI_APP_TOKEN, GLPI_USER_TOKEN)
  2. Arquivo .env ao lado deste módulo (formato CHAVE=valor, uma por linha)

Se os tokens não forem encontrados, o import falha com mensagem clara.
"""

import os
import requests
from pathlib import Path


def _carregar_env():
    """Carrega variáveis do arquivo .env (se existir) sem sobrescrever o ambiente."""
    env_file = Path(__file__).parent / ".env"
    if not env_file.exists():
        return
    for linha in env_file.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, _, valor = linha.partition("=")
        os.environ.setdefault(chave.strip(), valor.strip())


def _obrigatoria(nome: str) -> str:
    valor = os.environ.get(nome)
    if not valor:
        raise RuntimeError(
            f"Variável {nome} não definida. Configure no ambiente (Task Scheduler / "
            f"GitHub Secrets) ou no arquivo .env ao lado de glpi_api.py."
        )
    return valor


_carregar_env()

GLPI_URL   = os.environ.get("GLPI_URL", "https://servicedesk.a7on.ai")
APP_TOKEN  = _obrigatoria("GLPI_APP_TOKEN")
USER_TOKEN = _obrigatoria("GLPI_USER_TOKEN")
GLPI_PROFILE_ID = os.environ.get("GLPI_PROFILE_ID", "4")


# ── Sessão GLPI ───────────────────────────────────────────────────────
def get_session() -> str:
    r = requests.get(
        f"{GLPI_URL}/apirest.php/initSession",
        headers={"Authorization": f"user_token {USER_TOKEN}", "App-Token": APP_TOKEN},
        timeout=15,
    )
    r.raise_for_status()
    tok = r.json()["session_token"]
    # Forca o perfil Super-Admin: a conta usada aqui tambem tem um perfil
    # restrito por entidade (Tegma-only). Sem isso, initSession pode herdar
    # o ultimo perfil ativo da conta e esconder chamados de outras entidades.
    requests.post(
        f"{GLPI_URL}/apirest.php/changeActiveProfile",
        headers={"App-Token": APP_TOKEN, "Session-Token": tok},
        json={"profiles_id": GLPI_PROFILE_ID},
        timeout=15,
    )
    return tok


def close_session(tok: str):
    requests.get(
        f"{GLPI_URL}/apirest.php/killSession",
        headers={"App-Token": APP_TOKEN, "Session-Token": tok},
        timeout=10,
    )


def api_get(path: str, tok: str, params: dict = None):
    r = requests.get(
        f"{GLPI_URL}/apirest.php/{path}",
        headers={"App-Token": APP_TOKEN, "Session-Token": tok},
        params=params,
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def api_post(path: str, tok: str, body: dict):
    r = requests.post(
        f"{GLPI_URL}/apirest.php/{path}",
        headers={"App-Token": APP_TOKEN, "Session-Token": tok, "Content-Type": "application/json"},
        json=body,
        timeout=15,
    )
    r.raise_for_status()
    return r.json()
