# Auto Assumir Ticket — Railway

Painel web pra rodar o auto-ticket do HIT-Suporte hospedado na Railway.

> AVISO: usa token de usuário (selfbot) — viola ToS do Discord. Rodando 24/7 em
> cloud o risco de detecção/banimento é maior que rodando localmente.

## Deploy na Railway

1. **Suba esse diretório como repositório GitHub** (ou use `railway up`).
2. **Crie um projeto na Railway** apontando pro repo.
3. **Adicione um Volume persistente** no service:
   - Settings → Volumes → New Volume → mount path: `/data`
4. **Configure as Variables** do service:
   | Variable          | Valor                                                    |
   |-------------------|----------------------------------------------------------|
   | `PANEL_PASSWORD`  | senha forte pra entrar no painel                         |
   | `SESSION_SECRET`  | string longa aleatória (ex: `openssl rand -hex 32`)      |
   | `CONFIG_PATH`     | `/data/config.json` (default já é esse)                  |
5. **Habilite domínio público**: Settings → Networking → Generate Domain.
6. Após o deploy, abra a URL gerada, faça login com `PANEL_PASSWORD`,
   preencha token + IDs e clique em **Iniciar**.

## Rodar local pra testar

```bash
pip install -r requirements.txt
$env:PANEL_PASSWORD = "teste123"
$env:CONFIG_PATH = "./config.json"
python app.py
```

Abre em `http://localhost:8000`.

## Arquitetura

- `app.py` — FastAPI + WebSocket pra log ao vivo
- `bot.py` — cliente Discord rodando como task asyncio no mesmo loop
- `/data/config.json` — config persistida (token + IDs)
- Login via senha + cookie de sessão assinado

## Botões do painel

- **Iniciar** — salva a config e starta o bot
- **Parar** — fecha a conexão com o Discord
- **Salvar config** — persiste sem iniciar
- **limpar log** — só visual, não apaga server-side
