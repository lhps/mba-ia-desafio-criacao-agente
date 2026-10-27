# Residencial Aurora — assistente virtual com Google ADK

Assistente do aplicativo dos moradores do Residencial Aurora: reserva e cancela
áreas comuns, autoriza visitantes e tira dúvidas sobre o regulamento. Construído
com Google ADK (`google-adk==2.13.0`) e exposto por uma API FastAPI em
`http://localhost:8000`.

A filosofia: **o modelo decide o caminho, o código decide o que é permitido.**
Toda regra crítica vive em código Python ou no banco, nunca só no prompt.

## Arquitetura

```
                       ┌──────────────────────────────┐
  POST /sessoes/...    │ API FastAPI (api.py)          │
  ───────────────────▶ │ Runner + App do ADK           │
                       │ SqliteSessionService          │
                       └──────────────┬───────────────┘
                                      ▼
                       ┌──────────────────────────────┐
                       │ assistente_aurora (principal) │
                       └──┬──────────────┬──────────┬─┘
            transferência │              │          │ AgentTool
                          ▼              ▼          ▼
            especialista_reservas  especialista_   especialista_regulamento
                          │        visitantes      (sessão própria em memória)
                          ▼              ▼          ▼
                    tools → var/condominio.db    tool → dados/regulamento.md
                                                 (1 capítulo por chamada)
```

| Agente | Responsabilidade | Como é acionado | Por quê |
|---|---|---|---|
| `assistente_aurora` (`agents.py`) | Conversa com o morador e encaminha cada pedido. Não tem tools de dados nem o regulamento nas instruções. | Raiz do `App`; recebe toda mensagem nova. | Mantém o contexto principal pequeno e só roteia. |
| `especialista_reservas` | Listar áreas, consultar disponibilidade, listar, reservar e cancelar reservas do próprio apartamento. | `sub_agents` do principal, por **transferência** (`transfer_to_agent`). | As tools dele pedem confirmação. A confirmação precisa ficar no histórico da sessão persistida para ser retomada depois (inclusive após reinício), e o Runner devolve a resposta ao agente que fez a chamada. Por isso ele roda na própria sessão do morador. |
| `especialista_visitantes` | Autorizar e listar visitantes do próprio apartamento. | `sub_agents` do principal, por **transferência**. | Mesmo motivo: a autorização de visitante sempre pede confirmação. |
| `especialista_regulamento` | Responder dúvidas sobre o regulamento lendo **um capítulo por vez**. | `AgentTool` do principal. | O `AgentTool` executa o agente num Runner filho com `InMemorySessionService`: o texto do capítulo lido fica na sessão descartável do filho, e só a resposta curta volta como `function_response` para a sessão do morador (Garantia 4). |

Os dois especialistas transacionais usam `disallow_transfer_to_parent=True` e
`disallow_transfer_to_peers=True`: eles não decidem para onde a conversa vai.
Com isso, toda mensagem nova do morador chega ao principal (o Runner só
continua num subagente "transferível"), e só a **resposta de confirmação** é
roteada pelo Runner direto para o especialista que fez a chamada pendente.
Testado com sessão em SQLite, inclusive com a API reiniciada entre o pedido e a
aprovação.

Armazenamento:

- `var/condominio.db` (SQLite, stdlib `sqlite3`): apartamentos, áreas, reservas,
  visitantes e códigos emitidos. Criado e carregado de `dados/` na primeira
  subida. Os arquivos de `dados/` nunca são alterados.
- `var/sessoes.db` (SQLite via `SqliteSessionService` do ADK): sessões e eventos.

Código em `src/residencial_aurora/`:

| Arquivo | Conteúdo |
|---|---|
| `api.py` | Rotas, Runner, sessão persistida, cálculo das confirmações pendentes e o 409. |
| `agents.py` | Os quatro agentes e o `App`. |
| `tools.py` | Tools de reservas, visitantes e regulamento. |
| `db.py` | Schema SQLite, carga/restauração e todas as leituras e gravações. |
| `regulamento.py` | Divide o regulamento em capítulos e gera o índice. |
| `cli.py` | Comandos `aurora-api` e `aurora-restaurar`. |
| `config.py` | Caminhos, nomes e modelos. |

## Garantias

### 1. Cobrança ou acesso só com confirmação

- `src/residencial_aurora/tools.py`, `reservar_area` (bloco `if info_area["taxa"] > 0:`)
  e `autorizar_visitante`: sem `tool_context.tool_confirmation`, a tool chama
  `tool_context.request_confirmation(hint=..., payload=...)` e retorna **sem
  gravar**. O ADK emite um function call `adk_request_confirmation` e pausa.
  Se `confirmacao.confirmed` for falso, retorna "recusada" sem gravar. Reserva de
  área com taxa 0 grava direto.
- `src/residencial_aurora/api.py`, `confirmacoes_pendentes`: as pendências são
  calculadas a partir dos **eventos persistidos**: todo `adk_request_confirmation`
  sem `function_response` de mesmo id. É isso que aparece em
  `confirmacoes_pendentes` (com `payload` em `detalhes`).
- `src/residencial_aurora/api.py`, `responder_confirmacao`: se o id não está
  entre as pendentes daquela sessão, responde `409` sem chamar o Runner. Se está,
  monta o `FunctionResponse(name="adk_request_confirmation", id=..., response={"confirmed": ...})`
  e retoma a execução. Depois da resposta, o function response fica gravado na
  sessão, então reenviar o mesmo id dá 409 e a ação não executa de novo.

Por que não depende do modelo: `tool_confirmation` só é preenchido pelo ADK a
partir de um `FunctionResponse` vindo do cliente, e quem monta esse objeto é só a
rota de confirmações. Texto como "já estou confirmando aqui" chega como texto e
nunca vira confirmação. Nenhuma tool tem parâmetro de "confirmado".

### 2. Cada sessão pertence a um apartamento

- `src/residencial_aurora/api.py`, `criar_sessao`: grava `state={"apartamento": ...}`
  uma única vez, na criação da sessão. Nenhuma rota nem tool escreve essa chave
  depois.
- `src/residencial_aurora/tools.py`, `_apartamento`: toda tool de reservas e
  visitantes lê o apartamento de `tool_context.state`. **Nenhuma tool tem
  parâmetro `apartamento`**, então o modelo não tem como escolher outro.
- `src/residencial_aurora/db.py`, `reservas_do_apartamento`, `visitantes_do_apartamento`
  e `cancelar_reservas`: todo SQL filtra `WHERE apartamento = ?`. Cancelar a
  reserva de outro apartamento retorna "não encontrada", sem código nem dono.
- `src/residencial_aurora/db.py`, `data_livre`, e `tools.py`, `consultar_disponibilidade`
  / `reservar_area`: a agenda devolve só `livre`/`ocupada`; reserva recusada diz
  só que a data está ocupada.

Por que não depende do modelo: mesmo que o morador convença o modelo de que é
do 302, não existe tool capaz de ler ou alterar dados do 302 a partir de uma
sessão do 101.

### 3. Nada se perde no reinício

- `src/residencial_aurora/api.py`: `SqliteSessionService(str(config.SESSOES_DB))`
  e `Runner(app=adk_app, session_service=session_service)`: sessões e eventos em
  `var/sessoes.db`.
- `src/residencial_aurora/db.py`: reservas e visitantes em `var/condominio.db`.
  `inicializar()` só carrega `dados/` se o banco estiver vazio; reiniciar a API
  não restaura nada.
- As confirmações pendentes são derivadas dos eventos gravados (Garantia 1), então
  uma pendência criada antes do reinício pode ser aprovada depois dele.

Por que não depende do modelo: o estado está em disco, não em memória do processo
nem na "lembrança" do modelo.

### 4. O regulamento é consultado, não carregado

- `src/residencial_aurora/agents.py`, `assistente_aurora`: as instruções do
  principal não contêm o regulamento; ele só tem `AgentTool(agent=especialista_regulamento)`.
- `src/residencial_aurora/agents.py`, `especialista_regulamento`: recebe nas
  instruções só o **índice** (títulos dos capítulos, `regulamento.indice()`).
- `src/residencial_aurora/tools.py`, `ler_capitulo_regulamento`, e
  `regulamento.py`, `ler`: devolvem **um único capítulo** por chamada.
- O `AgentTool` roda o especialista num Runner filho com `InMemorySessionService`
  (ADK, `google/adk/tools/agent_tool.py`). A leitura do capítulo acontece nessa
  sessão descartável; na sessão do morador entram só o function call
  `especialista_regulamento` e a resposta curta dele.

Por que não depende do modelo: mesmo que o especialista leia um capítulo errado,
esse texto não vai para os eventos da sessão do morador, e o principal nunca
recebe o regulamento.

### 5. Dois moradores, uma reserva

- `src/residencial_aurora/db.py`, schema:
  `CREATE UNIQUE INDEX uq_reserva_ativa ON reservas(area, data) WHERE status = 'ativa'`.
- `src/residencial_aurora/db.py`, `criar_reserva`: o `INSERT` acontece dentro de
  `BEGIN IMMEDIATE`; se o índice recusar, `sqlite3.IntegrityError` vira
  `DataIndisponivel`.
- `src/residencial_aurora/tools.py`, `reservar_area`: `except db.DataIndisponivel`
  devolve a resposta normal de "data ocupada", e a API responde 200.

Por que não depende do modelo (nem da conferência anterior): a conferência
`data_livre` é só uma cortesia para não pedir confirmação de uma data ocupada. A
exclusividade é verificada pelo SQLite **no instante do INSERT**: das duas
aprovações simultâneas, a segunda gravação falha no índice.

Códigos de reserva: `db.py`, `_novo_codigo`, gera `RSV-XXXXXX` aleatório e o
grava na tabela `codigos_emitidos` (PK). Essa tabela nunca é limpa, nem por
cancelamento nem pela restauração, então um código nunca se repete.

## Como rodar

### Pré-requisitos

- Python 3.12+ e [uv](https://docs.astral.sh/uv/).
- Uma chave do Google AI Studio (https://aistudio.google.com/apikey).
- Nenhum serviço externo: o armazenamento é SQLite em `var/`.

### Variáveis do `.env`

```bash
cp .env.example .env
```

| Variável | Obrigatória | Descrição |
|---|---|---|
| `GOOGLE_API_KEY` | sim | Chave do Google AI Studio. |
| `GOOGLE_GENAI_USE_VERTEXAI` | não | Deixe `FALSE` para usar o AI Studio. |
| `AURORA_MODELO_PRINCIPAL` | não | Modelo do agente principal. Padrão: `gemini-3.5-flash-lite`. |
| `AURORA_MODELO_ESPECIALISTAS` | não | Modelo dos especialistas. Padrão: `gemini-3.1-flash-lite`. |

Os padrões usam dois modelos diferentes porque a cota gratuita do AI Studio é
por modelo e por dia. Ajuste conforme os limites do seu projeto.

### Comandos

```bash
uv sync                 # instala as dependências
uv run aurora-restaurar # restaura reservas e visitantes a partir de dados/ e apaga as sessões
uv run aurora-api       # sobe a API em http://localhost:8000
```

A restauração também apaga as sessões, porque conversas antigas citariam
reservas que deixaram de existir. A primeira subida da API cria o banco
sozinha se ele ainda não existir.

### Exemplo

```bash
SID=$(curl -s -X POST localhost:8000/sessoes -H 'content-type: application/json' \
  -d '{"apartamento":"101"}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["session_id"])')

curl -s -X POST localhost:8000/sessoes/$SID/mensagens -H 'content-type: application/json' \
  -d '{"texto":"Reserve o salão de festas para 2030-04-20"}'
# -> confirmacoes_pendentes: [{"id": "adk-...", "acao": "reservar_area", "detalhes": {...}}]

curl -s -X POST localhost:8000/sessoes/$SID/confirmacoes -H 'content-type: application/json' \
  -d '{"id":"adk-...","confirmado":true}'

curl -s localhost:8000/apartamentos/101/reservas
```
