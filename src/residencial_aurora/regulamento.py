"""Leitura do regulamento por capítulo.

O regulamento nunca é carregado inteiro em lugar nenhum: o especialista recebe
só o índice (títulos) nas instruções e lê um capítulo por vez via tool.
"""

import re
from functools import cache

from . import config

_CABECALHO = re.compile(r"^## Capítulo ([IVXLC]+): (.+)$", re.MULTILINE)


@cache
def capitulos() -> dict[str, dict]:
    texto = (config.DADOS_DIR / "regulamento.md").read_text(encoding="utf-8")
    marcas = list(_CABECALHO.finditer(texto))
    resultado: dict[str, dict] = {}
    for i, m in enumerate(marcas):
        fim = marcas[i + 1].start() if i + 1 < len(marcas) else len(texto)
        resultado[m.group(1)] = {
            "capitulo": m.group(1),
            "titulo": m.group(2).strip(),
            "texto": texto[m.start() : fim].strip(),
        }
    return resultado


def indice() -> str:
    return "\n".join(f"- Capítulo {c['capitulo']}: {c['titulo']}" for c in capitulos().values())


def ler(capitulo: str) -> dict | None:
    chave = capitulo.strip().upper().removeprefix("CAPÍTULO").strip()
    return capitulos().get(chave)
