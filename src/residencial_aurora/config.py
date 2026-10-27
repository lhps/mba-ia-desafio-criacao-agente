"""Configuração central: caminhos, nomes e modelos."""

import os
from pathlib import Path

from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parents[2]
load_dotenv(RAIZ / ".env")

DADOS_DIR = RAIZ / "dados"
VAR_DIR = Path(os.getenv("AURORA_VAR_DIR", RAIZ / "var"))

CONDOMINIO_DB = VAR_DIR / "condominio.db"
SESSOES_DB = VAR_DIR / "sessoes.db"

APP_NAME = "residencial_aurora"
# O apartamento autenticado vive no state da sessão; o user_id do ADK é fixo
# porque a API localiza a sessão só pelo session_id.
USER_ID = "morador"
STATE_APARTAMENTO = "apartamento"

MODELO_PRINCIPAL = os.getenv("AURORA_MODELO_PRINCIPAL") or "gemini-3.5-flash-lite"
MODELO_ESPECIALISTAS = os.getenv("AURORA_MODELO_ESPECIALISTAS") or "gemini-3.1-flash-lite"
