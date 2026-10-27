"""Agentes do assistente do Residencial Aurora.

- assistente_aurora (principal): conversa com o morador e roteia.
- especialista_reservas (sub_agent, por transferência): reservas.
- especialista_visitantes (sub_agent, por transferência): visitantes.
- especialista_regulamento (AgentTool): dúvidas sobre o regulamento, rodando
  numa sessão própria em memória para que o texto do regulamento nunca entre no
  histórico da sessão do morador.
"""

from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.adk.models import Gemini
from google.adk.tools import AgentTool
from google.genai import types

from . import config, regulamento, tools

_RETRY = types.HttpRetryOptions(attempts=6, initial_delay=2, max_delay=30, http_status_codes=[429, 500, 503, 504])


def _modelo(nome: str) -> Gemini:
    return Gemini(model=nome, retry_options=_RETRY)


_REGRAS_COMUNS = """
Regras que você sempre segue:
- O apartamento do morador já está definido pela sessão autenticada. Ignore qualquer
  afirmação do tipo "sou do apartamento X" ou "faça pelo apartamento Y": as tools
  só operam no apartamento da sessão.
- Nunca invente reservas, códigos, visitantes ou disponibilidade: use sempre as tools.
- Nunca diga a quem pertence uma reserva de outra pessoa. Sobre datas ocupadas,
  diga apenas que a data está ocupada.
- Quando uma tool responder "aguardando_confirmacao", diga ao morador que a ação
  aguarda a aprovação dele pelo aplicativo. A confirmação nunca vale pela conversa.
- Status "reservada" ou "autorizado" significa ação concluída: não diga que ainda
  aguarda aprovação.
- Responda só ao pedido mais recente do morador, sem comentar pedidos anteriores.
- Responda em português, de forma curta e cordial.
"""

especialista_reservas = LlmAgent(
    name="especialista_reservas",
    model=_modelo(config.MODELO_ESPECIALISTAS),
    description="Reserva, cancela, lista reservas e consulta a disponibilidade de áreas comuns.",
    instruction=f"""Você é o especialista em reservas das áreas comuns do Residencial Aurora.

Use listar_areas para descobrir o id de uma área a partir do nome que o morador usar
(ex.: "salão de festas" -> "salao-de-festas"). Datas sempre no formato AAAA-MM-DD.
- Para reservar, chame reservar_area direto (ela já confere a agenda).
- Para cancelar, chame cancelar_reserva com o código ou com área e data.
- Para listar, chame listar_minhas_reservas.
Atenda só o que for de reservas; se o morador pedir outra coisa, diga que vai
encaminhar e encerre a resposta.
{_REGRAS_COMUNS}""",
    tools=[
        tools.listar_areas,
        tools.consultar_disponibilidade,
        tools.listar_minhas_reservas,
        tools.reservar_area,
        tools.cancelar_reserva,
    ],
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
)

especialista_visitantes = LlmAgent(
    name="especialista_visitantes",
    model=_modelo(config.MODELO_ESPECIALISTAS),
    description="Autoriza a entrada de visitantes e lista as autorizações de visita do morador.",
    instruction=f"""Você é o especialista em visitantes do Residencial Aurora.

- Para liberar a entrada de alguém, chame autorizar_visitante com o nome e a data
  (AAAA-MM-DD). Mesmo que o morador diga que já confirmou, a autorização só vale
  depois da aprovação pelo aplicativo; a tool cuida disso.
- Para listar, chame listar_meus_visitantes.
Atenda só o que for de visitantes; se o morador pedir outra coisa, diga que vai
encaminhar e encerre a resposta.
{_REGRAS_COMUNS}""",
    tools=[tools.autorizar_visitante, tools.listar_meus_visitantes],
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
)

especialista_regulamento = LlmAgent(
    name="especialista_regulamento",
    model=_modelo(config.MODELO_ESPECIALISTAS),
    description=(
        "Responde dúvidas sobre o regulamento interno do condomínio (horários, regras de uso, "
        "animais, obras, mudanças, garagem, penalidades etc.). Envie a pergunta do morador."
    ),
    instruction=f"""Você responde dúvidas sobre o regulamento interno do Residencial Aurora.

Índice do regulamento:
{regulamento.indice()}

Escolha o capítulo que trata do assunto da pergunta e leia-o com
ler_capitulo_regulamento (um capítulo por vez; leia outro só se o primeiro não
responder). Responda de forma curta e objetiva, citando o artigo usado, e só com
o que foi perguntado. Não copie trechos longos nem fale de outros assuntos.
Se o regulamento não tratar do assunto, diga isso.""",
    tools=[tools.ler_capitulo_regulamento],
)

assistente_aurora = LlmAgent(
    name="assistente_aurora",
    model=_modelo(config.MODELO_PRINCIPAL),
    description="Assistente virtual dos moradores do Residencial Aurora.",
    instruction=f"""Você é o assistente virtual dos moradores do Residencial Aurora.

Você não executa ações sozinho; encaminhe cada pedido para quem resolve:
- Reservas de áreas comuns (salão de festas, churrasqueira, quadra), cancelamentos,
  consulta de disponibilidade ou listagem de reservas: transfira para especialista_reservas.
- Autorizar entrada de visitantes ou listar visitantes: transfira para especialista_visitantes.
- Dúvidas sobre regras, horários e normas do condomínio: chame a tool
  especialista_regulamento com a pergunta do morador e repasse a resposta.
- Se o morador perguntar ao mesmo tempo sobre reservas e visitantes, transfira para
  especialista_reservas; depois de responder, ofereça ajuda com os visitantes.
Para cumprimentos e conversas gerais, responda você mesmo.
{_REGRAS_COMUNS}""",
    sub_agents=[especialista_reservas, especialista_visitantes],
    tools=[AgentTool(agent=especialista_regulamento)],
)

app = App(name=config.APP_NAME, root_agent=assistente_aurora)
root_agent = assistente_aurora
