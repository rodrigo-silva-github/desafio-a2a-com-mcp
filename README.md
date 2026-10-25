# A Ponte: um agente A2A com MCP por dentro

Entrega do desafio **"A Ponte"** (MBA Engenharia de Software com IA — MCP e A2A):
um servidor MCP em Streamable HTTP que expõe as salas de reunião da Hill Valley
Tech, e um agente que consome esse servidor **por dentro** (como host MCP) e se
oferece **por fora** (como servidor A2A), fazendo a ponte entre o ciclo de
`input_required`/`requestState` do MCP e a máquina de estados de Task do A2A.

Dois processos separados, sem LLM, sem banco, sem ORM: só protocolo.

```
.
├── servidor-mcp/            servidor MCP (Streamable HTTP, porta 7301, /mcp)
│   ├── server.py
│   └── pyproject.toml
├── agente/                  agente A2A (porta 7300: /a2a e /.well-known/agent-card.json)
│   ├── agente.py
│   └── pyproject.toml
├── dados/                   (starter, nao alterado)
├── validador/               (starter, nao alterado)
└── exemplos/                (starter, nao alterado)
```

## Como rodar

Pré-requisitos: Python 3.10+ (testado com 3.14) e o `pip`/`venv` do Python.

A partir de um clone limpo:

```bash
# 1. ambiente (uma unica vez) — versao travada do SDK MCP v2
python3 -m venv .venv
.venv/bin/pip install mcp==2.3.0

# 2. segredo de integridade do requestState (>= 32 bytes de aleatoriedade)
#    gere o seu e exporte no shell onde o servidor MCP vai rodar:
export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")

# 3. terminal 1 — servidor MCP na porta 7301 (endpoint /mcp); stderr fica visivel
.venv/bin/python servidor-mcp/server.py

# 4. terminal 2 — agente A2A na porta 7300 (RPC em /a2a, card em /.well-known/agent-card.json)
.venv/bin/python agente/agente.py

# 5. terminal 3 — validador (36 verificacoes)
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

O agente conecta no servidor MCP em `http://localhost:7301/mcp` no startup
(com retry), descobre as tools por `tools/list` e lê o resource
`politica://uso` — deixe o servidor MCP de pé antes (ou aguarde os retries).

Portas e endereços, quando precisar mudar (defaults são os do validador):

| Variavel     | Processo      | Default                          |
|--------------|---------------|----------------------------------|
| `MCP_HOST`/`MCP_PORT` | servidor-mcp | `127.0.0.1` / `7301` |
| `MCP_URL`    | agente (onde o MCP mora) | `http://127.0.0.1:7301/mcp` |
| `AGENTE_HOST`/`AGENTE_PORT`/`AGENTE_BASE` | agente | `127.0.0.1` / `7300` / `http://127.0.0.1:7300` |

O card A2A usa `AGENTE_BASE` (default `http://127.0.0.1:7300`) para montar a URL
da interface JSON-RPC.

O `REQUEST_STATE_SECRET` **nunca deve ser commitado**: o repositório é público e
o valor é a única coisa que selaria um requestState. Não há segredo hardcoded no
código — o servidor recusa chave curta ou ausente na subida.

## Onde a ponte acontece

A ponte tem dois lados, ambos em `agente/agente.py`:

1. **`input_required` do MCP vira `TASK_STATE_INPUT_REQUIRED`** — em
   `_executar_pedido` (Task nova) o agente chama o servidor MCP com o driver
   automático de elicitation **desligado**
   (`client.session.call_tool(..., allow_input_required=True)`), então o
   `InputRequiredResult` chega cru. Em `_pausar`, a Task vai para
   `TASK_STATE_INPUT_REQUIRED`, a mensagem vira exatamente
   `alternativas: <ids na ordem do enum>`, e o `requestState`, a chave do
   `inputRequests` e os argumentos originais ficam guardados **naquela Task**.

2. **O `requestState` volta para o servidor** — no `SendMessage` de continuação
   (`_continuar_task`), o agente refaz o `tools/call` **original** com um id de
   JSON-RPC novo (o SDK gera um id novo por request), ecoando o `requestState`
   sem abrir, com `inputResponses` usando a mesma chave que veio no
   `inputRequests` (`{chave: {"action": "accept"|"decline", ...}}`). O servidor
   desela o estado, reconstrói o pedido e conclui — ou cancela, se o cliente
   recusou.

O agente nunca decide sala, conflito, política ou alternativas: isso é do
servidor MCP (resolver `escolha_de_sala` em `servidor-mcp/server.py`). O agente
traduz protocolo; o `requestState` é opaco para ele.

## Decisões técnicas

- **Integridade do requestState** — o servidor usa o utilitário nativo do SDK
  `mcp` (SDK v2, revisão 2026-07-28): `RequestStateSecurity(keys=[...], ttl=900)`
  instala a `RequestStateBoundary` no pipeline. O token é **AEAD** (AES-256-GCM,
  formato `v1.<base64url>`), carrega a expiração embutida e cobre o método, o
  nome da tool, o digest dos argumentos e a audiência (o nome do servidor).
  Qualquer adulteração, expiração ou dessincronia entre o requestState e o
  retry é rejeitada com `-32602` (HTTP 400). A chave (≥ 32 bytes) vem de
  `REQUEST_STATE_SECRET`; o estado viaja todo no token, então **um retry
  continua funcionando depois de reiniciar o processo do servidor** (verificado
  no fluxo do avaliador). Argumentos adulterados no retry não tomam efeito: o
  digest interno não bate e o SDK rejeita com `-32602` (caminho "rejeita o
  estado", aceito pelo enunciado). TTL: 15 minutos (janela exigida: 5–30 min).
- **Onde fica o estado das Tasks** — em memória no agente (um dict por Task:
  estado, mensagens, artifact, e os dados privados da ponte: `request_state`,
  chave da elicitation, argumentos e enum). Como cada Task guarda o seu próprio
  requestState, duas Tasks pausadas ao mesmo tempo terminam cada uma com a sua
  reserva, sem trocar estado. As reservas ficam em memória no servidor
  (carregadas de `dados/reservas.json` na subida; criadas durante a execução,
  visíveis para as consultas seguintes do mesmo processo).
- **Nada de sessão** — os dois lados são stateless por design: o servidor usa o
  transporte Streamable HTTP 2026-07-28 (`stateless_http=True`,
  `json_response=True`), que valida nativamente o `_meta` obrigatório
  (`protocolVersion`/`clientCapabilities` ausentes → `-32602` com HTTP 400) e os
  headers espelhados do corpo (`MCP-Protocol-Version`, `Mcp-Method`, `Mcp-Name`
  → `-32020` quando divergem). O `-32021` com `data.requiredCapabilities` é o
  comportamento nativo do resolver quando o cliente não declarou a capability
  `{"elicitation": {"form": {}}}`.
- **Capability de elicitation declarada, ciclo dirigido à mão** — o agente
  registra um callback de elicitation que **declara** a capability (o SDK só
  anuncia `elicitation.form` quando há callback), mas nunca responde por ele: o
  cliente usa `session.call_tool(allow_input_required=True)`, então o
  `input_required` chega cru e vira a pausa da Task, em vez de o ciclo fechar
  dentro do SDK.
- **traceparent** — o header `traceparent` da chamada A2A é lido de forma
  insensível a caixa (o case chega variando: `Traceparent` do urllib,
  `traceparent` do curl) e propagado no `_meta` de todo request MCP daquela Task
  com o **mesmo trace-id** e span novo; quando não há header, o agente gera um
  traceparent próprio. O servidor registra no **stderr** o método, o id e o
  traceparent de cada request (`request method=... id=... traceparent=...`).
- **Sem LLM** — o agente interpreta o pedido em formato fixo
  (`reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>`) por
  regra. Dependências: apenas `mcp==2.3.0` (versão travada nos dois
  `pyproject.toml`). Determinístico: o mesmo pedido produz o mesmo resultado.
- **Erros** — mensagens exatas de negócio via `ToolError` (vira `isError:
  true`); erros de protocolo via JSON-RPC; recusa (`decline`/`cancel`) conclui
  com `reservado: false` e `motivo`, sem `isError`.

## Saída do validador

Execução final, com os dois processos recém-iniciados
(`python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301`),
terminando com código de saída 0:

```
trace-id desta execucao: eb40fd79f80f2e0f3c897373923150fb
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```

Conferências manuais do fluxo do avaliador, todas feitas nesta entrega:

- O stderr do servidor MCP mostra `tools/list id=1` (e `resources/read id=2`)
  antes do primeiro `tools/call` do agente (id=3).
- O traceparent do validador (o trace-id da linha 1 da saída acima) aparece nos
  `tools/call` do agente com o mesmo trace-id e span novo a cada request.
- O `tools/call` que passa pela pausa aparece em par (ex.: ids 4 e 5 no log) com
  **ids diferentes** — o retry é um request novo.
- Um `SendMessage` de `sala-garagem` das 14h–15h do dia 03/11 pausa a Task em
  `TASK_STATE_INPUT_REQUIRED` com a linha `alternativas: sala-fusca, sala-mirante`;
  `escolha=sala-mirante` conclui em `TASK_STATE_COMPLETED` (artifact com
  `"politica": "2026-11-01"`); `escolha=recusar` termina em `TASK_STATE_CANCELED`;
  sala inexistente termina em `TASK_STATE_FAILED` com a mensagem da tool.
- Um `requestState` adulterado é rejeitado com `-32602` (HTTP 400); o retry com
  o requestState válido conclui a reserva **mesmo com o servidor MCP reiniciado
  entre a pausa e o retry**; o mesmo `tools/call` de conflito sem a capability de
  elicitation responde `-32021` com HTTP 400.
