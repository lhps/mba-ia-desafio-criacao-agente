"""Comandos `aurora-api` (sobe a API) e `aurora-restaurar` (restaura os dados)."""

import os

from . import config, db


def api() -> None:
    import uvicorn

    uvicorn.run(
        "residencial_aurora.api:api",
        host=os.getenv("AURORA_HOST", "0.0.0.0"),
        port=int(os.getenv("AURORA_PORT", "8000")),
    )


def restaurar() -> None:
    db.inicializar(restaurar=True)
    # As sessões também são apagadas: conversas antigas citariam reservas que
    # deixaram de existir.
    for sufixo in ("", "-wal", "-shm"):
        caminho = config.SESSOES_DB.with_name(config.SESSOES_DB.name + sufixo)
        caminho.unlink(missing_ok=True)
    print(f"Dados restaurados a partir de {config.DADOS_DIR} e sessões apagadas.")
