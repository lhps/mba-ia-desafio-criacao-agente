"""API HTTP do assistente (FastAPI + Runner do ADK com sessão em SQLite)."""

import asyncio
import logging
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from google.adk.events import Event
from google.adk.runners import Runner
from google.adk.sessions import Session
from google.adk.sessions.sqlite_session_service import SqliteSessionService
from google.genai import types
from pydantic import BaseModel

from . import config, db
from .agents import app as adk_app

logger = logging.getLogger(__name__)

# Nome da function call que o ADK emite quando uma tool pede confirmação.
CONFIRMACAO = "adk_request_confirmation"

session_service = SqliteSessionService(str(config.SESSOES_DB))
runner = Runner(app=adk_app, session_service=session_service)

# Serializa execuções dentro de uma mesma sessão (sessões diferentes rodam em paralelo).
_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


@asynccontextmanager
async def lifespan(_: FastAPI):
    config.VAR_DIR.mkdir(parents=True, exist_ok=True)
    db.inicializar()
    yield
    await runner.close()


api = FastAPI(title="Residencial Aurora", lifespan=lifespan)


class NovaSessao(BaseModel):
    apartamento: str


class Mensagem(BaseModel):
    texto: str


class RespostaConfirmacao(BaseModel):
    id: str
    confirmado: bool


# --- helpers ----------------------------------------------------------------


async def _sessao(session_id: str) -> Session:
    session = await session_service.get_session(
        app_name=config.APP_NAME, user_id=config.USER_ID, session_id=session_id
    )
    if session is None:
        raise HTTPException(status_code=404, detail="Sessão não encontrada.")
    return session


def confirmacoes_pendentes(session: Session) -> list[dict[str, Any]]:
    """Confirmações pedidas pelas tools e ainda sem resposta, lidas dos eventos.

    Uma confirmação é um function call `adk_request_confirmation`; ela deixa de
    estar pendente quando aparece na sessão um function response com o mesmo id,
    o que só a rota de confirmações produz.
    """
    pedidas: dict[str, dict[str, Any]] = {}
    respondidas: set[str] = set()
    for event in session.events:
        for fc in event.get_function_calls():
            if fc.name == CONFIRMACAO and fc.id:
                pedidas[fc.id] = dict(fc.args or {})
        for fr in event.get_function_responses():
            if fr.name == CONFIRMACAO and fr.id:
                respondidas.add(fr.id)

    pendentes = []
    for fc_id, args in pedidas.items():
        if fc_id in respondidas:
            continue
        original = args.get("originalFunctionCall") or {}
        confirmacao = args.get("toolConfirmation") or {}
        detalhes = confirmacao.get("payload") or original.get("args") or {}
        pendentes.append(
            {
                "id": fc_id,
                "acao": original.get("name", ""),
                "detalhes": detalhes,
                "mensagem": confirmacao.get("hint", ""),
            }
        )
    return pendentes


def _texto(events: list[Event]) -> str:
    partes = []
    for event in events:
        if event.author == "user" or event.partial or not event.content:
            continue
        for part in event.content.parts or []:
            if part.text and not part.thought:
                partes.append(part.text.strip())
    return "\n\n".join(p for p in partes if p)


async def _executar(session_id: str, mensagem: types.Content) -> dict[str, Any]:
    eventos = []
    async for event in runner.run_async(
        user_id=config.USER_ID, session_id=session_id, new_message=mensagem
    ):
        eventos.append(event)
    session = await _sessao(session_id)
    return {"resposta": _texto(eventos), "confirmacoes_pendentes": confirmacoes_pendentes(session)}


# --- rotas de conversa ------------------------------------------------------


@api.post("/sessoes", status_code=201)
async def criar_sessao(body: NovaSessao) -> dict[str, str]:
    apartamento = body.apartamento.strip()
    if not db.apartamento_existe(apartamento):
        raise HTTPException(status_code=422, detail="Apartamento inexistente.")
    # Garantia 2: o apartamento entra no state uma única vez, aqui. Nenhuma rota
    # nem tool grava essa chave depois.
    session = await session_service.create_session(
        app_name=config.APP_NAME,
        user_id=config.USER_ID,
        state={config.STATE_APARTAMENTO: apartamento},
    )
    return {"session_id": session.id}


@api.post("/sessoes/{session_id}/mensagens")
async def enviar_mensagem(session_id: str, body: Mensagem) -> dict[str, Any]:
    await _sessao(session_id)
    async with _locks[session_id]:
        conteudo = types.Content(role="user", parts=[types.Part(text=body.texto)])
        return await _executar(session_id, conteudo)


@api.post("/sessoes/{session_id}/confirmacoes")
async def responder_confirmacao(session_id: str, body: RespostaConfirmacao):
    await _sessao(session_id)
    async with _locks[session_id]:
        # Garantia 1: só aceita id que está pendente nesta sessão agora.
        session = await _sessao(session_id)
        if body.id not in {c["id"] for c in confirmacoes_pendentes(session)}:
            return JSONResponse(
                status_code=409,
                content={"detail": "Não existe confirmação pendente com esse id nesta sessão."},
            )
        resposta = types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        id=body.id,
                        name=CONFIRMACAO,
                        response={"confirmed": body.confirmado},
                    )
                )
            ],
        )
        return await _executar(session_id, resposta)


@api.get("/sessoes/{session_id}/eventos")
async def listar_eventos(session_id: str) -> list[dict[str, Any]]:
    session = await _sessao(session_id)
    return [e.model_dump(mode="json", exclude_none=True, by_alias=True) for e in session.events]


# --- rotas de verificação (leem o banco direto, sem modelo) -----------------


@api.get("/apartamentos/{numero}/reservas")
async def reservas_do_apartamento(numero: str) -> list[dict[str, Any]]:
    return db.reservas_do_apartamento(numero)


@api.get("/apartamentos/{numero}/visitantes")
async def visitantes_do_apartamento(numero: str) -> list[dict[str, Any]]:
    return db.visitantes_do_apartamento(numero)
