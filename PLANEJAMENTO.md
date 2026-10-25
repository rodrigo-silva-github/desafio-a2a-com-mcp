# Planejamento — Desafio "A Ponte" (agente A2A com MCP por dentro)

Plano de implementação completo para entregar o desafio no fork
`rodrigo-silva-github/desafio-a2a-com-mcp`, branch `main`, passando nas 36
verificações do validador e no fluxo manual do avaliador.

Fonte da verdade do contrato: `exemplos/wire/` (11 pares request/response
capturados de uma execução real com o SDK Python) e `validador/validar.py`
(as 36 verificações, lidas linha a linha). `dados/`, `validador/` e
`exemplos/` são intocáveis.

---

## 1. Objetivo

Entregar dois processos separados e verificáveis:

1. **Servidor MCP** em Streamable HTTP na porta 7301, endpoint `/mcp`: 3 tools,
   1 resource, os dois tipos de erro e o ciclo completo de MRTR na reserva.
2. **Agente** na porta 7300: host MCP por dentro (cliente do próprio servidor)
   e servidor A2A por fora (`/.well-known/agent-card.json` + `/a2a` com
   `SendMessage`/`GetTask`), com a ponte: `input_required` do MCP vira
   `TASK_STATE_INPUT_REQUIRED`, o `requestState` fica guardado na Task e volta
   no retry com id JSON-RPC novo.

Validador final (processos recém-iniciados):

```
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

## 2. Stack escolhida

**Python + SDK oficial `mcp` (v2) nas duas pontas** — recomendado com base em
evidência dos wires:

- A chave do `inputRequests` nos wires 03/04/11 é `__main__:escolha_de_sala`
  e o erro `-32021` do wire 06 cita `resolver '__main__:escolha_de_sala'` —
  formato de nomes do SDK Python (FastMCP).
- O `requestState` dos wires começa com `v1.` + base64url — formato do
  utilitário de request state assinado do SDK Python.
- Python 3.14.4 e `uv` já disponíveis no ambiente; validador também é Python.

Regras da stack:

- Versões **travadas** em `pyproject.toml` de cada pasta (`mcp==<versão
  exata da v2>` + pins das dependências diretas). Nunca `>=` solto.
- O servidor usa o mecanismo **nativo de MRTR** do SDK (procurar
  `input_required` nas docstrings do pacote instalado — o enunciado garante
  que existe), nunca um loop/thread esperando resposta do cliente.
- O agente usa o **cliente MCP do SDK sem callback de elicitation** — tem que
  ver o `input_required` cru. Callback de elicitation fecha o ciclo sozinho e
  a Task nunca pausa (erro nº 1 do enunciado).
- Fallback documentado: se algo do SDK quebrar com Python 3.14, subir a
  stack Node (`@modelcontextprotocol/server`) — o contrato aceita as duas.

## 3. Estado atual e regras de versionamento

- Repo clonado do fork `git@github.com:rodrigo-silva-github/desafio-a2a-com-mcp.git`,
  1 commit (`initial commit`), `main` = `origin/main`. **Nunca** push para o
  upstream `devfullcycle` (nem existe remote — manter assim).
- Único untracked: `desafio` (enunciado local). **Adicionado ao `.gitignore`**
  — nunca entra no versionamento.
- `.kilo/` é worktree local da ferramenta Kilo (repo embutido): o git já não
  o rastreia; não commitar, não adicionar.
- `dados/`, `validador/`, `exemplos/` e arquivos do starter: **zero
  alterações** (conferir com `git diff` antes do push final). Os `.gitkeep`
  de `servidor-mcp/` e `agente/` são removidos quando as pastas ganham código.
- Toda a entrega vai na branch `main`, em commits atômicos por fase.

## 4. Restrições que norteiam tudo (não negociáveis)

- **Dois processos separados**: o agente fala com o MCP por HTTP, como cliente
  de verdade. Proibido importar a função da tool.
- **O agente não implementa regra de negócio**: conflito, política e
  alternativas são 100% do servidor MCP. O agente traduz protocolo.
- **Sem sessão**: nenhum dos lados infere versão/capabilities de request
  anterior; `_meta` completo em todo request. (Manter objeto de cliente vivo
  entre chamadas é normal e recomendado.)
- **`requestState` é opaco para o agente**: guardar, ecoar, nunca abrir.
- **Sem LLM no caminho de execução**: nenhuma dependência de SDK de provedor
  no `pyproject.toml`; mesmo pedido → mesmo resultado (verificação 36).
- **Mensagens de erro exatas** da seção "Regras de negócio" — o texto exato
  precisa aparecer no conteúdo do resultado (prefixo do SDK é aceito).

## 5. Estrutura final alvo

```
.
├── README.md                  (substituído — 4 seções obrigatórias)
├── PLANEJAMENTO.md            (este documento — apoio)
├── .gitignore                 (+ "desafio")
├── dados/  validador/  exemplos/      (intocados)
├── servidor-mcp/
│   ├── pyproject.toml         (deps travadas, ex.: mcp==2.x.y)
│   └── server.py             (FastMCP: 3 tools, resource, MRTR, log stderr)
└── agente/
    ├── pyproject.toml
    └── agente.py             (cliente MCP + servidor HTTP A2A + máquina de estados)
```

Sem ORM, sem banco, sem camada de serviços, sem compose, sem UI. Reservas em
memória no servidor (lista), Tasks e requestStates por Task em memória no
agente.

## 6. Mapa verificação × implementação (cobertura sem excesso)

### 6.1 Servidor MCP — verificações 01–20 do validador

| Verif. | O que o código precisa fazer |
|---|---|
| 01 | `tools/list` devolve as 3 tools: `listar_salas`, `consultar_disponibilidade`, `reservar_sala` |
| 02 | Toda tool com `inputSchema.type == "object"` (pydantic faz isso) |
| 03 | `listar_salas`: `structuredContent` conforme `outputSchema` declarado **e** o mesmo JSON serializado num bloco `text` (`json.loads(texto) == structuredContent`) |
| 04, 05 | Request sem `protocolVersion` ou sem `clientCapabilities` no `_meta` → `-32602` + HTTP 400. **Conferir se o transporte do SDK já rejeita; se não, middleware explícito** |
| 06 | Tool inexistente recusada (`-32602` ou `isError: true` — o que o SDK fizer) |
| 07 | `resources/read politica://uso` devolve o conteúdo de `dados/politica-de-uso.md`, `mimeType: text/markdown` |
| 08 | `resources/read` de URI inexistente → `-32602` (nunca `contents` vazio) |
| 09 | Sala inexistente → `isError: true` com **`Sala inexistente: sala-delorean`** |
| 10 | Janela violada → **`Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00`** |
| 11 | Duração > 2h → **`Duracao acima do limite: a politica permite no maximo 2 horas`** |
| 12 | Fim ≤ início → **`Intervalo invalido: fim deve ser posterior a inicio`** |
| 13 | Conflito em `reservar_sala` → `resultType: input_required`, 1 entrada em `inputRequests`, `requestState` presente |
| 14 | `mode: form` e `properties.sala.enum == ["sala-fusca", "sala-mirante"]` **nessa ordem** (regra de alternativas, §7.3) |
| 15 | Conflito sem capability `elicitation.form` → `-32021` + `data.requiredCapabilities` + HTTP 400 (nativo do SDK — confirmar) |
| 16 | Retry com `inputResponses` + `requestState` → `complete`, sem `isError`, reserva na sala **escolhida** (`sala-garagem`) |
| 17 | `requestState` adulterado → `-32602` |
| 18 | Retry com argumentos adulterados: **ou** erro, **ou** valores selados vencem (nunca os adulterados) |
| 19 | `action: decline` → `complete`, sem `isError`, `structuredContent.reservado == false` (+ `motivo`) |
| 20 | Conflito sem nenhuma sala alternativa → `isError: true` com **`Sem alternativas disponiveis no intervalo`** |

### 6.2 Agente/A2A — verificações 21–36

| Verif. | O que o código precisa fazer |
|---|---|
| 21 | `GET /.well-known/agent-card.json` → 200, JSON (forma do wire 07) |
| 22 | Card v1.0: `supportedInterfaces[]` com `protocolBinding: JSONRPC`, `url` do endpoint, `protocolVersion: 1.0` |
| 23 | Card declara skill `reservar-sala` |
| 24 | SendMessage com sala livre (`sala-porao` 09–10h) → Task `TASK_STATE_COMPLETED` na mesma resposta |
| 25 | Artifact `name: "reserva"`, `parts[0].text` = JSON com `politica == "2026-11-01"` (lida do resource) e `sala == "sala-porao"` |
| 26 | `GetTask` devolve `id`, `contextId`, `status.state` corrente e artifact |
| 27 | SendMessage com sala ocupada (`sala-garagem` 14–15h) → Task `TASK_STATE_INPUT_REQUIRED` |
| 28 | Mensagem de status exatamente `alternativas: sala-fusca, sala-mirante` (ordem do enum) |
| 29 | `escolha=sala-aquario` (fora do enum) → mantém `TASK_STATE_INPUT_REQUIRED`, repete a lista |
| 30 | `escolha=sala-fusca` → `TASK_STATE_COMPLETED`, artifact com `sala == "sala-fusca"` |
| 31 | SendMessage em Task terminal → **erro JSON-RPC** |
| 32 | `escolha=recusar` → `TASK_STATE_CANCELED` |
| 33 | Duas Tasks pausadas ao mesmo tempo concluem cada uma com **sua própria** reserva (requestState nunca trocado) |
| 34 | `requestState` **não aparece em nenhuma resposta A2A** (nem card, nem mensagem, nem artifact) |
| 35 | Sala inexistente → Task `TASK_STATE_FAILED`, com a mensagem exata da tool no status (histórico) |
| 36 | Determinismo: mesmo pedido duas vezes → mesma mensagem de pausa |

### 6.3 Avaliado manualmente (fora do validador, exigido pelo enunciado)

- stderr do servidor registra de **cada request**: método, id e `traceparent`
  quando vier no `_meta` → middleware de request.
- Log mostra um `tools/list` **do agente** antes do primeiro `tools/call` →
  agente faz descoberta no startup (com retry) e antes da primeira chamada.
- `traceparent` propagado: mesmo trace-id do header A2A, span-id novo, em todo
  request MCP daquela Task (verificável no stderr do MCP).
- Retry da ponte com **id JSON-RPC diferente** do request inicial.
- `-32020` quando header espelhado (Mcp-Method/Mcp-Name/MCP-Protocol-Version)
  diverge do corpo — comportamento nativo do transporte; validar com curl.
- Retry de reserva funciona **após restart do servidor MCP** (requestState
  autocontido, chave do env).
- README com os comandos que funcionam a partir de clone limpo.

## 7. Fases de implementação (ordem do enunciado)

### Fase 0 — Setup e spike de SDK

- [ ] Criar `servidor-mcp/` e `agente/` com `pyproject.toml` (deps travadas).
- [ ] Instalar o SDK `mcp` (v2) num venv (com `uv` ou `python3 -m venv`) e
      **confirmar compatibilidade com Python 3.14** (pydantic/wheels). Se
      quebrar, pino 3.13 no `requires-python` apenas do runtime (validador
      continua 3.10+ do sistema).
- [ ] **Spike**: ler docstrings do pacote instalado (grep `input_required`,
      `request_state`, `elicitation`, `traceparent`) para fixar as 3 APIs que
      governam a arquitetura:
      1. como a tool devolve `input_required` (MRTR nativo);
      2. como o `requestState` é assinado/verificado (chave + TTL) e como o
         transporte rejeita estado inválido com `-32602`;
      3. como injetar `traceparent`/capabilities no `_meta` dos requests do
         cliente.
- [ ] Confirmar: transporte do SDK rejeita `_meta` sem
      `protocolVersion`/`clientCapabilities` com `-32602` + 400 (verif. 04/05)
      e rejeita divergência de header com `-32020`; se não, anotar onde entra
      o middleware.
- [ ] Confirmar formato do requestState do SDK: `v1.` + base64url, e que a
      verificação de adulteração (trocar os últimos 6 chars) cai em `-32602`.

**Definição de pronto**: servidor mínimo respondendo `tools/list` com 1 tool no
endpoint `/mcp` porta 7301, e as 3 APIs do spike documentadas (no README ou
nesta seção, para não redescobrir).

### Fase 1 — Servidor MCP base

- [ ] FastMCP em Streamable HTTP, porta 7301 (env `PORT`/`HOST` com defaults),
      endpoint `/mcp`, `serverInfo` `central-de-salas`/`1.0.0` (igual aos
      wires).
- [ ] Declarar capabilities de `tools` e `resources`.
- [ ] `listar_salas`: sem parâmetros, `outputSchema` declarado (model pydantic
      com `salas`: id, nome, capacidade, recursos), `structuredContent` + bloco
      de texto com o mesmo JSON (verif. 03).
- [ ] Resource `politica://uso` → conteúdo literal de `dados/politica-de-uso.md`,
      `mimeType: text/markdown` (verif. 07/08).
- [ ] Log de request no stderr (método, id, traceparent quando houver) via
      middleware/hook do SDK.
- [ ] Cobertura de `_meta` obrigatório (verif. 04/05) e `-32020`.
- [ ] Validar contra os wires 01 e 05 com curl (mesmo shape de resposta,
      incluindo `_meta.serverInfo`, `cacheScope`, `resultType`).

### Fase 2 — Regras de negócio (consultar_disponibilidade + caminho feliz)

- [ ] Carga em memória de `dados/salas.json` e `dados/reservas.json` (uma vez,
      no startup). Reserva nova = `res-000X` (próximo id sequencial, 4 dígitos).
- [ ] Helpers de domínio (no servidor, nunca no agente):
      - parse ISO8601 com offset (aware, `ZoneInfo("America/Sao_Paulo")`);
      - janela: início e fim convertidos para SP entre 08:00 e 20:00;
      - duração ≤ 2h; fim > início;
      - sobreposição: `inicio < r.fim and fim > r.inicio`.
- [ ] **Ordem de validação** (determina qual mensagem sai): sala existe →
      janela → duração → intervalo invertido → (só então) conflito. Conferir
      contra verif. 09–12 e 20.
- [ ] `consultar_disponibilidade` (sala, inicio, fim): mesmas validações/mensagens;
      devolve `sala`, `livre`, `conflitos` (id, inicio, fim, responsavel) com
      `outputSchema` declarado. **Nunca** pede input.
- [ ] `reservar_sala` caminho feliz: intervalo livre e válido → cria reserva;
      `structuredContent` com `reserva, reservado=true, sala, inicio, fim,
      responsavel, politica (versão lida do arquivo), motivo=null`, mais o
      bloco de texto com o mesmo JSON (shape do wire 02).

**Definição de pronto**: verif. 01–12 e 20 passando, mais curl dos wires 02.

### Fase 3 — MRTR na reserva (o núcleo do desafio)

Usar o mecanismo nativo do SDK (spike da Fase 0). Pontos do contrato:

- [ ] Conflito → `input_required` com 1 entrada em `inputRequests`
      (`elicitation/create`, `mode: form`), `requestedSchema` plano com
      `sala` restrito às alternativas, e `requestState` presente.
- [ ] **Regra de alternativas** (fechada no enunciado): salas livres no
      intervalo, capacidade ≥ da sala pedida, no máximo 3, ordenadas por
      capacidade crescente e, em empate, por id alfabético. A sala pedida está
      ocupada (é o conflito) e sai naturalmente. Conferir enum
      `["sala-fusca", "sala-mirante"]` para garagem 14–15h do 03/11 (verif. 14).
- [ ] Sem alternativa → sem elicitation: `isError: true` com
      `Sem alternativas disponiveis no intervalo` (verif. 20).
- [ ] **requestState**:
      - assinado (HMAC/AEAD) — utilitário do SDK; formato compatível com o
        wire (`v1.` + base64url);
      - expiração entre 5 e 30 min (escolher TTL fixo, ex.: 15 min —
        documentar no README);
      - carrega **tudo** para reconstruir o pedido (argumentos originais);
        o servidor não guarda nada em memória → retry sobrevive a restart;
      - chave = env `REQUEST_STATE_SECRET` (≥ 32 bytes; validar no startup e
        abortar com mensagem clara se ausente/curta). **Nunca** hardcoded.
- [ ] Capability: cliente sem `elicitation.form` → `-32021` +
      `data.requiredCapabilities` + HTTP 400 (verif. 15; nativo do SDK).
- [ ] Retry: lê `inputResponses` + `requestState` de `params`, reconstrói o
      pedido do estado selado e conclui com a **sala escolhida** (verif. 16 e
      wire 04: reserva criada com a sala do `content`, demais campos selados).
- [ ] **Argumentos adulterados do retry não tomam efeito**: usar valores
      selados (ou rejeitar) — verif. 18. Confirmar no spike quem ganha: se o
      SDK re-executa o handler com os args do estado, está resolvido; senão,
      validar explicitamente args × estado.
- [ ] `action: decline`/`cancel` → `complete`, sem `isError`,
      `reservado: false`, `motivo: "recusado"` (verif. 19, wire 11).
- [ ] `requestState` adulterado ou expirado → `-32602` (verif. 17).

**Definição de pronto**: verif. 13–19 passando, e um teste manual de retry
com o processo do servidor morto e reiniciado entre a pausa e o retry.

### Fase 4 — Agente como host MCP (sem A2A ainda)

- [ ] `StreamableHttpClientSession` apontando para `http://127.0.0.1:7301/mcp`.
- [ ] Descoberta: `tools/list` **antes** da primeira chamada (no startup, com
      retry) — nunca lista fixa no código; visível no stderr do servidor.
- [ ] Ler `resources/read politica://uso` e extrair a versão da primeira linha
      (`versao: 2026-11-01`) — usada no artifact.
- [ ] Todo request MCP com `_meta` completo: `protocolVersion`,
      `clientCapabilities` com `{"elicitation": {"form": {}}}` e headers
      espelhados corretos (MCP-Protocol-Version, Mcp-Method, Mcp-Name).
- [ ] **traceparent**: função `extrair_trace_id(traceparent)` +
      `gerar_traceparent(trace_id)` (span-id novo). Injeta no `_meta` de todo
      request da Task (mecanismo conforme spike — nativo do SDK ou extensão do
      meta). Sem campos OTel além disso.
- [ ] Chamada crua: `call_tool` devolve o resultado com `resultType:
      input_required` à mostra — **sem callback de elicitation**.
- [ ] Tradução do resultado para o "núcleo" do agente (a ser consumido na
      Fase 6): `complete` com `structuredContent`; `input_required` com chave,
      enum de alternativas (na ordem) e `requestState`; `isError` com a
      mensagem.
- [ ] Teste de script: um pedido fixo → fluxo completo respondendo a
      elicitation "na mão" (ciclo fechado sem A2A).

### Fase 5 — Agente como servidor A2A

- [ ] Servidor HTTP na porta 7300 (env com defaults):
      `GET /.well-known/agent-card.json` (card exato do wire 07: nome,
      description, provider, `supportedInterfaces` com JSONRPC + url +
      `protocolVersion: "1.0"`, capabilities `streaming/pushNotifications/
      extendedAgentCard: false`, input/output modes, skill `reservar-sala`
      com exemplo) e `POST /a2a` (JSON-RPC 2.0).
- [ ] Handler JSON-RPC: `SendMessage` e `GetTask`; método desconhecido/params
      inválidos → `error` JSON-RPC.
- [ ] Modelo de Task (em memória, dict por id): id (`task-…`), `contextId`
      (`ctx-…`), status (estado + mensagem), `history`, `artifacts`.
- [ ] Máquina de estados: `TASK_STATE_SUBMITTED → TASK_STATE_WORKING →`
      terminal (`COMPLETED` | `FAILED` | `CANCELED`) ou `INPUT_REQUIRED`.
      **Terminal é definitivo**: SendMessage em task terminal → erro (verif.
      31).
- [ ] SendMessage sem `taskId` → Task nova; com `taskId` (e não terminal) →
      continuação.
- [ ] Sucesso: artifact `name: "reserva"`, `parts[0].text` = JSON
      `{reserva, sala, inicio, fim, responsavel, politica}` (shape do wire 10);
      mensagem de status `Reserva <id> confirmada na sala-<sala>.`.
- [ ] `GetTask` devolve id, contextId, status corrente, history e artifacts
      (wire 09).

### Fase 6 — A ponte (costura das duas pontas)

- [ ] Parse do pedido fixo: `reservar sala=<id> inicio=<iso> fim=<iso>
      responsavel=<nome>`; continuação: `escolha=<valor>`.
- [ ] **Pausa**: `input_required` do MCP → Task `TASK_STATE_INPUT_REQUIRED`,
      mensagem **exatamente** `alternativas: <ids na ordem do enum, separados
      por ", ">` — sem prefixo/saudação (verif. 28; byte a byte).
- [ ] Guardar na Task: `requestState`, chave do `inputRequests`, enum, args
      originais — **nunca** expostos em card/mensagem/artifact (verif. 34).
- [ ] Continuação:
      - fora do enum → mantém `INPUT_REQUIRED`, repete a lista (verif. 29);
      - `recusar` → retry com `{"action": "decline"}` → `CANCELED` (verif. 32);
      - sala válida → retry `tools/call` **com id novo** (cada `call_tool` do
        SDK gera id novo — garantir que o request é novo, não reuso),
        `inputResponses` com a mesma chave + `{"action": "accept", "content":
        {"sala": ...}}`, `requestState` ecoado sem modificação, `_meta` com
        traceparent da Task → `COMPLETED` (verif. 30).
- [ ] `isError: true` do MCP → Task `FAILED`, mensagem exata da tool em
      `status.message` (verif. 35).
- [ ] Resposta final do SendMessage = a Task inteira (status + history +
      artifacts), como nos wires 08/09/10.
- [ ] Estado pausado **por Task** (dict task_id → contexto) — duas pausas
      simultâneas não trocam requestState (verif. 33).

### Fase 7 — README (substitui o do starter)

Quatro seções obrigatórias:

1. **Como rodar** — do clone limpo: gerar/exportar
   `REQUEST_STATE_SECRET` (comando do enunciado,
   `python3 -c "import secrets; print(secrets.token_hex(32))"`), subir o
   servidor MCP (termo 1, stderr visível), subir o agente (termo 2), rodar o
   validador. **Nunca** colar o valor do segredo.
2. **Onde a ponte acontece** — parágrafo apontando: (a) onde
   `input_required` vira `TASK_STATE_INPUT_REQUIRED` (handler do SendMessage /
   tradução do resultado MCP) e (b) onde o `requestState` volta (retry na
   continuação), com referências de arquivo:linha.
3. **Decisões técnicas** — proteção do requestState (assinatura, TTL de 15
   min, chave do env, valores selados), onde ficam as Tasks (memória, por id),
   e as APIs de SDK usadas.
4. **Saída do validador** — bloco de código com a saída completa da última
   execução (36/36 + código de saída 0).

### Fase 8 — Validação final (do zero, como o avaliador)

- [ ] `git status` limpo de alterações em `dados/`, `validador/`, `exemplos/`;
      `desafio` ignorado.
- [ ] Clone limpo do fork em pasta temporária; seguir **só** o README.
- [ ] Rodar o validador com os dois processos recém-iniciados → 36 PASS,
      exit 0.
- [ ] Percorrer o **fluxo manual do avaliador** (passos 3–13 do enunciado):
      curl do card; trace-id da execução localizado no stderr do MCP;
      `tools/list` do agente antes do primeiro `tools/call`; par de
      `tools/call` com ids diferentes na pausa; SendMessage garagem 14–15h →
      `alternativas: sala-fusca, sala-mirante`; continuação `sala-mirante` →
      COMPLETED com artifact; `escolha=recusar` → CANCELED; sala inexistente →
      FAILED; requestState adulterado → `-32602`; restart do servidor entre
      pausa e retry → conclui mesmo assim; conflito sem capability →
      `-32021` + 400.
- [ ] Varredura de segredos: nenhum valor de `REQUEST_STATE_SECRET` no repo.
- [ ] Push final na branch `main` do fork `rodrigo-silva-github`.

## 8. Riscos e mitigações

| Risco | Mitigação |
|---|---|
| SDK `mcp` com Python 3.14 (wheels de pydantic) | Testar na Fase 0; fallback: venv com 3.13 só para os processos, ou stack Node |
| API exata do MRTR/requestState/traceparent do SDK | Spike da Fase 0 lendo docstrings do pacote instalado; enunciado garante que os utilitários existem ("MRTR de primeira classe") |
| `-32602`/HTTP 400 para `_meta` faltante e `-32021` não serem nativos | Middleware no servidor para validação explícita (verif. 04/05/15) |
| Verif. 18 (args adulterados) | Valores selados vencem no retry — decidir no spike quem reconstrói o pedido |
| Retry após restart | requestState autocontido (payload + assinatura + TTL); nada em memória no servidor |
| Ordem das alternativas | Regra implementada com sort estável (capacidade, depois id) e testada contra verif. 14/28/36 |
| Leak de requestState em respostas A2A | requestState só existe no contexto interno da Task; nunca serializado nas respostas |

## 9. Fora de escopo (não implementar)

- Autenticação/autorização (sem OAuth, sem token, `securitySchemes` no card
  sem conteúdo real).
- Streaming: nada de `SendStreamingMessage`, SSE ou push.
- Refinamento de Task, `referenceTaskIds`, continuidade entre Tasks.
- Card estendido autenticado / assinatura JWS.
- Prompts, subscriptions e progress do MCP.
- Container, compose, deploy, gateway, UI.
- Spans/atributos OTel — só propagar o `traceparent` e logar.
- Concorrência de escrita/transação/race entre reservas no mesmo
  milissegundo.
- Persistência em disco das reservas (memória basta; restart só tem que
  sobreviver via requestState).
- ORM, banco, camada de serviços, LLM de qualquer provedor.

## 10. Checklist final de entrega (antes do push)

- [ ] 36 verificações PASS com exit 0, processos recém-iniciados.
- [ ] Fluxo manual do avaliador (passos 3–13) reproduzido por completo.
- [ ] `dados/`, `validador/`, `exemplos/` intactos (`git diff --stat` vazio
      nesses caminhos).
- [ ] README com as 4 seções, comandos verificados de clone limpo, sem
      segredos.
- [ ] `desafio` fora do versionamento (`.gitignore`).
- [ ] Determínismo: nenhuma dependência de LLM no `pyproject.toml`.
- [ ] Tudo na `main` do fork `rodrigo-silva-github` (nunca upstream).
