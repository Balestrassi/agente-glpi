# Agente GLPI — Recebe Mais

Suite de agentes Python que monitora chamados do GLPI em tempo real para o time de suporte **Recebe Mais**: dispara notificações no Telegram, envia relatórios automáticos e serve um dashboard web.

---

## Arquitetura

```
GLPI (REST API)
      │
      ├── agentes Python (GitHub Actions)
      │         │
      │         ├── Telegram (alertas em tempo real)
      │         └── Google Sheets (histórico de métricas)
      │
      └── Dashboard Flask (Render)
```

---

## Agentes

| Arquivo | Frequência | O que faz |
|---|---|---|
| `agente_recebemais.py` | a cada 5 min¹ | Detecta novos chamados e posta o primeiro atendimento automático |
| `agente_followup_cliente.py` | a cada 15 min¹ | Envia follow-ups progressivos (+2h / +4h / +8h) enquanto o cliente aguarda resposta |
| `agente_sla.py` | a cada 30 min | Alerta sobre chamados próximos ou em violação de SLA (24h / 48h / 72h) |
| `agente_anomalias.py` | a cada 15 min | Detecta bursts (múltiplos chamados do mesmo cliente em curto período) |
| `agente_reaberturas.py` | a cada 30 min | Notifica quando um chamado resolvido volta a aberto |
| `resumo_diario.py` | 08h BRT (seg–sex) | Resumo matinal com total em aberto e chamados críticos |
| `relatorio_automatico.py` | segunda 09h07 BRT | Relatório semanal em PDF enviado pelo Telegram |
| `relatorio_mensal.py` | dia 1 de cada mês | Relatório mensal em PDF com comparativo semanal |
| `historico_metricas.py` | domingo 22h BRT | Registra métricas semanais no Google Sheets |

> ¹ Disparado externamente via [cron-job.org](https://cron-job.org) usando `workflow_dispatch` — o `schedule` nativo do GitHub foi removido por imprecisão em repositórios com pouca atividade.

---

## Dashboard

Aplicação Flask servida no [Render](https://render.com) (`dashboard/`). Exibe métricas em tempo real, histórico semanal, desempenho por técnico e permite drill-down por período.

---

## Secrets necessários (GitHub Actions)

| Secret | Descrição |
|---|---|
| `GLPI_URL` | URL base do GLPI (ex: `https://servicedesk.exemplo.com`) |
| `GLPI_APP_TOKEN` | App Token da API REST do GLPI |
| `GLPI_USER_TOKEN` | User Token da conta de serviço |
| `GLPI_PROFILE_ID` | ID do perfil Super-Admin (padrão: `4`) |
| `TELEGRAM_TOKEN` | Token do bot do Telegram |
| `TELEGRAM_CHAT_ID` | ID do grupo/canal de destino |
| `GOOGLE_CREDENTIALS` | JSON da Service Account (base64) |
| `SPREADSHEET_ID` | ID da planilha do Google Sheets |
| `DASHBOARD_TOKEN` | Token de acesso ao dashboard web |

---

## Rodar localmente

```bash
pip install requests
cp .env.example .env  # preencha com suas credenciais
python agente_recebemais.py
```

> O `.env` nunca deve ser commitado. Adicione ao `.gitignore`.
