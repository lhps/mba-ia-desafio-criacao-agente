"""Tools dos especialistas.

Regras que valem aqui, independentemente do que o modelo decidir:

- Nenhuma tool recebe apartamento como parâmetro. O apartamento vem sempre de
  `tool_context.state`, gravado uma única vez na criação da sessão pela API
  (Garantia 2). O modelo não tem como escolher outro.
- Ações que geram cobrança ou liberam acesso pedem confirmação via
  `tool_context.request_confirmation` e só gravam quando a confirmação chega
  pela rota de confirmações da API (Garantia 1). Texto do morador nunca vira
  `tool_context.tool_confirmation`.
- Respostas sobre a agenda dizem só "livre" ou "ocupada", sem dono nem código.
"""

from datetime import date

from google.adk.tools import ToolContext

from . import config, db, regulamento


def _apartamento(tool_context: ToolContext) -> str:
    apartamento = tool_context.state.get(config.STATE_APARTAMENTO)
    if not apartamento:
        raise RuntimeError("Sessão sem apartamento autenticado.")
    return str(apartamento)


def _validar_data(data: str) -> str | None:
    try:
        return date.fromisoformat(data.strip()).isoformat()
    except (ValueError, AttributeError):
        return None


def _erro_area(area: str) -> dict:
    return {
        "status": "erro",
        "mensagem": f"Área '{area}' não existe. Use um destes ids: "
        + ", ".join(a["id"] for a in db.listar_areas()),
    }


_ERRO_DATA = {"status": "erro", "mensagem": "Data inválida. Use o formato AAAA-MM-DD."}


# --- reservas ---------------------------------------------------------------


def listar_areas() -> dict:
    """Lista as áreas comuns que podem ser reservadas, com id, nome e taxa em reais.

    Taxa 0 significa que a área não gera cobrança.
    """
    return {"areas": db.listar_areas()}


def consultar_disponibilidade(area: str, data: str) -> dict:
    """Diz se uma área comum está livre ou ocupada em uma data.

    Args:
        area: id da área (ex.: "salao-de-festas", "churrasqueira", "quadra").
        data: data no formato AAAA-MM-DD.
    """
    if not db.buscar_area(area):
        return _erro_area(area)
    if not (data_ok := _validar_data(data)):
        return _ERRO_DATA
    livre = db.data_livre(area, data_ok)
    return {"area": area, "data": data_ok, "situacao": "livre" if livre else "ocupada"}


def listar_minhas_reservas(tool_context: ToolContext) -> dict:
    """Lista as reservas ativas do apartamento do morador desta conversa."""
    return {"reservas": db.reservas_do_apartamento(_apartamento(tool_context))}


def reservar_area(area: str, data: str, tool_context: ToolContext) -> dict:
    """Reserva uma área comum para o apartamento do morador desta conversa.

    Áreas com taxa geram cobrança e ficam pendentes até o morador aprovar pelo
    aplicativo. Áreas sem taxa são reservadas na hora.

    Args:
        area: id da área (ex.: "salao-de-festas", "churrasqueira", "quadra").
        data: data no formato AAAA-MM-DD.
    """
    apartamento = _apartamento(tool_context)
    info_area = db.buscar_area(area)
    if not info_area:
        return _erro_area(area)
    if not (data_ok := _validar_data(data)):
        return _ERRO_DATA

    indisponivel = {
        "status": "indisponivel",
        "mensagem": f"{info_area['nome']} já está ocupada em {data_ok}. Nenhuma reserva foi feita.",
    }
    if not db.data_livre(area, data_ok):
        return indisponivel

    if info_area["taxa"] > 0:
        confirmacao = tool_context.tool_confirmation
        if confirmacao is None:
            tool_context.request_confirmation(
                hint=(
                    f"Reservar {info_area['nome']} em {data_ok} gera cobrança de "
                    f"R$ {info_area['taxa']:.2f}. Aprove ou recuse pelo aplicativo."
                ),
                payload={
                    "area": area,
                    "nome_area": info_area["nome"],
                    "data": data_ok,
                    "taxa": info_area["taxa"],
                },
            )
            return {
                "status": "aguardando_confirmacao",
                "mensagem": "A reserva gera cobrança e aguarda a aprovação do morador pelo aplicativo.",
            }
        if not confirmacao.confirmed:
            return {"status": "recusada", "mensagem": "O morador recusou a cobrança. Nada foi reservado."}

    try:
        reserva = db.criar_reserva(apartamento, area, data_ok)
    except db.DataIndisponivel:
        return indisponivel
    return {
        "status": "reservada",
        "mensagem": "Reserva concluída e gravada. Nada mais está pendente.",
        "reserva": reserva,
        "taxa": info_area["taxa"],
    }


def cancelar_reserva(
    tool_context: ToolContext,
    codigo: str | None = None,
    area: str | None = None,
    data: str | None = None,
) -> dict:
    """Cancela reservas ativas do apartamento do morador desta conversa.

    Informe o código da reserva ou a área e a data. Reservas de outros
    apartamentos nunca são canceladas.

    Args:
        codigo: código da reserva (ex.: "RSV-1234"), se o morador souber.
        area: id da área (ex.: "salao-de-festas", "churrasqueira", "quadra").
        data: data da reserva no formato AAAA-MM-DD.
    """
    apartamento = _apartamento(tool_context)
    if not codigo and not (area and data):
        return {"status": "erro", "mensagem": "Informe o código da reserva ou a área e a data."}
    data_ok = None
    if data and not (data_ok := _validar_data(data)):
        return _ERRO_DATA
    canceladas = db.cancelar_reservas(apartamento, codigo=codigo, area_id=area, data=data_ok)
    if not canceladas:
        return {
            "status": "nao_encontrada",
            "mensagem": "Não há reserva ativa do seu apartamento com esses dados. Nada foi cancelado.",
        }
    return {"status": "cancelada", "reservas_canceladas": canceladas}


# --- visitantes -------------------------------------------------------------


def listar_meus_visitantes(tool_context: ToolContext) -> dict:
    """Lista as autorizações de visita do apartamento do morador desta conversa."""
    return {"visitantes": db.visitantes_do_apartamento(_apartamento(tool_context))}


def autorizar_visitante(nome: str, data: str, tool_context: ToolContext) -> dict:
    """Autoriza a entrada de um visitante no prédio em uma data.

    Toda autorização fica pendente até o morador aprovar pelo aplicativo, mesmo
    que ele diga na conversa que já confirmou.

    Args:
        nome: nome completo do visitante.
        data: data da visita no formato AAAA-MM-DD.
    """
    apartamento = _apartamento(tool_context)
    nome = " ".join(nome.split())
    if not nome:
        return {"status": "erro", "mensagem": "Informe o nome do visitante."}
    if not (data_ok := _validar_data(data)):
        return _ERRO_DATA

    confirmacao = tool_context.tool_confirmation
    if confirmacao is None:
        tool_context.request_confirmation(
            hint=f"Liberar a entrada de {nome} em {data_ok}. Aprove ou recuse pelo aplicativo.",
            payload={"nome": nome, "data": data_ok},
        )
        return {
            "status": "aguardando_confirmacao",
            "mensagem": "A autorização aguarda a aprovação do morador pelo aplicativo.",
        }
    if not confirmacao.confirmed:
        return {"status": "recusada", "mensagem": "O morador recusou a autorização. Nada foi gravado."}

    return {
        "status": "autorizado",
        "mensagem": "Entrada autorizada e gravada. Nada mais está pendente.",
        "visitante": db.autorizar_visitante(apartamento, nome, data_ok),
    }


# --- regulamento ------------------------------------------------------------


def ler_capitulo_regulamento(capitulo: str) -> dict:
    """Lê um único capítulo do regulamento interno.

    Args:
        capitulo: número romano do capítulo (ex.: "IV").
    """
    cap = regulamento.ler(capitulo)
    if not cap:
        return {"status": "erro", "mensagem": "Capítulo inexistente.", "indice": regulamento.indice()}
    return cap
