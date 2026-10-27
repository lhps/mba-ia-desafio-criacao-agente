"""Armazenamento do condomínio em SQLite.

Os arquivos de `dados/` são só o estado inicial e nunca são alterados: reservas
e visitantes vivem em `var/condominio.db`.

A exclusividade "uma reserva ativa por área e data" é garantida pelo índice
único parcial `uq_reserva_ativa`. É o SQLite que recusa a segunda gravação no
instante do INSERT, então duas aprovações simultâneas nunca geram duas reservas,
mesmo que as duas tenham visto a data livre antes.
"""

import json
import secrets
import sqlite3
import string
from contextlib import contextmanager
from typing import Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS apartamentos (
    numero  TEXT PRIMARY KEY,
    morador TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS areas (
    id   TEXT PRIMARY KEY,
    nome TEXT NOT NULL,
    taxa REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS reservas (
    codigo      TEXT PRIMARY KEY,
    apartamento TEXT NOT NULL REFERENCES apartamentos(numero),
    area        TEXT NOT NULL REFERENCES areas(id),
    data        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'ativa' CHECK (status IN ('ativa', 'cancelada'))
);

-- Garantia 5: no máximo uma reserva ATIVA por área e data, checado na gravação.
CREATE UNIQUE INDEX IF NOT EXISTS uq_reserva_ativa
    ON reservas(area, data) WHERE status = 'ativa';

-- Todo código já emitido, inclusive de reservas canceladas ou apagadas pela
-- restauração. Nunca é limpo, então um código nunca se repete.
CREATE TABLE IF NOT EXISTS codigos_emitidos (
    codigo TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS visitantes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    apartamento TEXT NOT NULL REFERENCES apartamentos(numero),
    nome        TEXT NOT NULL,
    data        TEXT NOT NULL
);
"""


class DataIndisponivel(Exception):
    """A área já tem reserva ativa na data pedida."""


@contextmanager
def conectar() -> Iterator[sqlite3.Connection]:
    config.VAR_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.CONDOMINIO_DB, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
    finally:
        conn.close()


def _ler_json(nome: str) -> list[dict]:
    return json.loads((config.DADOS_DIR / nome).read_text(encoding="utf-8"))


def inicializar(restaurar: bool = False) -> None:
    """Cria o schema e carrega os dados iniciais.

    Sem `restaurar`, só carrega se o banco ainda estiver vazio. Com `restaurar`,
    volta reservas e visitantes ao estado dos arquivos de `dados/`.
    """
    with conectar() as conn:
        conn.executescript(SCHEMA)
        conn.execute("BEGIN IMMEDIATE")
        try:
            vazio = conn.execute("SELECT COUNT(*) FROM apartamentos").fetchone()[0] == 0
            if restaurar or vazio:
                conn.execute("DELETE FROM reservas")
                conn.execute("DELETE FROM visitantes")
                conn.execute("DELETE FROM apartamentos")
                conn.execute("DELETE FROM areas")
                for a in _ler_json("apartamentos.json"):
                    conn.execute(
                        "INSERT INTO apartamentos (numero, morador) VALUES (?, ?)",
                        (a["numero"], a["morador"]),
                    )
                for a in _ler_json("areas.json"):
                    conn.execute(
                        "INSERT INTO areas (id, nome, taxa) VALUES (?, ?, ?)",
                        (a["id"], a["nome"], float(a["taxa"])),
                    )
                for r in _ler_json("reservas.json"):
                    conn.execute(
                        "INSERT INTO reservas (codigo, apartamento, area, data) VALUES (?, ?, ?, ?)",
                        (r["codigo"], r["apartamento"], r["area"], r["data"]),
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO codigos_emitidos (codigo) VALUES (?)",
                        (r["codigo"],),
                    )
                for v in _ler_json("visitantes.json"):
                    conn.execute(
                        "INSERT INTO visitantes (apartamento, nome, data) VALUES (?, ?, ?)",
                        (v["apartamento"], v["nome"], v["data"]),
                    )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


# --- leituras -------------------------------------------------------------


def listar_areas() -> list[dict]:
    with conectar() as conn:
        rows = conn.execute("SELECT id, nome, taxa FROM areas ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def buscar_area(area_id: str) -> dict | None:
    with conectar() as conn:
        row = conn.execute(
            "SELECT id, nome, taxa FROM areas WHERE id = ?", (area_id,)
        ).fetchone()
    return dict(row) if row else None


def apartamento_existe(numero: str) -> bool:
    with conectar() as conn:
        row = conn.execute(
            "SELECT 1 FROM apartamentos WHERE numero = ?", (numero,)
        ).fetchone()
    return row is not None


def data_livre(area_id: str, data: str) -> bool:
    """Diz só se a data está livre; nunca revela de quem é a reserva."""
    with conectar() as conn:
        row = conn.execute(
            "SELECT 1 FROM reservas WHERE area = ? AND data = ? AND status = 'ativa'",
            (area_id, data),
        ).fetchone()
    return row is None


def reservas_do_apartamento(apartamento: str) -> list[dict]:
    with conectar() as conn:
        rows = conn.execute(
            "SELECT codigo, area, data FROM reservas "
            "WHERE apartamento = ? AND status = 'ativa' ORDER BY data, codigo",
            (apartamento,),
        ).fetchall()
    return [dict(r) for r in rows]


def visitantes_do_apartamento(apartamento: str) -> list[dict]:
    with conectar() as conn:
        rows = conn.execute(
            "SELECT nome, data FROM visitantes WHERE apartamento = ? ORDER BY data, id",
            (apartamento,),
        ).fetchall()
    return [dict(r) for r in rows]


# --- gravações ------------------------------------------------------------

_ALFABETO = string.ascii_uppercase + string.digits


def _novo_codigo(conn: sqlite3.Connection) -> str:
    """Reserva um código nunca emitido. A PK de codigos_emitidos impede repetição."""
    while True:
        codigo = "RSV-" + "".join(secrets.choice(_ALFABETO) for _ in range(6))
        try:
            conn.execute("INSERT INTO codigos_emitidos (codigo) VALUES (?)", (codigo,))
            return codigo
        except sqlite3.IntegrityError:
            continue


def criar_reserva(apartamento: str, area_id: str, data: str) -> dict:
    """Grava a reserva. Levanta DataIndisponivel se o índice único recusar."""
    with conectar() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            codigo = _novo_codigo(conn)
            conn.execute(
                "INSERT INTO reservas (codigo, apartamento, area, data) VALUES (?, ?, ?, ?)",
                (codigo, apartamento, area_id, data),
            )
            conn.execute("COMMIT")
        except sqlite3.IntegrityError as e:
            conn.execute("ROLLBACK")
            raise DataIndisponivel(area_id, data) from e
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"codigo": codigo, "area": area_id, "data": data}


def cancelar_reservas(
    apartamento: str, *, codigo: str | None = None, area_id: str | None = None, data: str | None = None
) -> list[dict]:
    """Cancela reservas ativas do apartamento que casem com os filtros.

    O filtro por apartamento é sempre aplicado: reservas de outros apartamentos
    nunca são tocadas nem retornadas.
    """
    filtros = ["apartamento = ?", "status = 'ativa'"]
    params: list[str] = [apartamento]
    if codigo:
        filtros.append("codigo = ?")
        params.append(codigo.strip().upper())
    if area_id:
        filtros.append("area = ?")
        params.append(area_id)
    if data:
        filtros.append("data = ?")
        params.append(data)
    where = " AND ".join(filtros)
    with conectar() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(
                f"SELECT codigo, area, data FROM reservas WHERE {where}", params
            ).fetchall()
            conn.executemany(
                "UPDATE reservas SET status = 'cancelada' WHERE codigo = ?",
                [(r["codigo"],) for r in rows],
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return [dict(r) for r in rows]


def autorizar_visitante(apartamento: str, nome: str, data: str) -> dict:
    with conectar() as conn:
        conn.execute(
            "INSERT INTO visitantes (apartamento, nome, data) VALUES (?, ?, ?)",
            (apartamento, nome, data),
        )
    return {"nome": nome, "data": data}
