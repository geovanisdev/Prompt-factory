"""A REGRA sob a qual cada anotação foi feita — versionada, e gravada por linha.

Até o P3a as diretrizes eram texto literal dentro do ``index.html``. Isso quer
dizer que a anotação versionava o **formato** do que produziu
(``payload_schema``) e não versionava a **instrução** que a produziu.

O CENÁRIO QUE ISTO RESOLVE
==========================
Você ajusta a regra do A/B numa terça ("justificativa passa a exigir citação da
resposta"). Na quinta o cliente reclama que o lote está inconsistente. Sem
``anotacoes.versao_diretriz``, não existe consulta que separe o que foi feito
sob a regra velha do que foi feito sob a nova — e a única saída honesta é
refazer o lote inteiro. Com a coluna, é um ``WHERE``.

FONTE ÚNICA, VERSIONADA EM ARQUIVO, MATERIALIZADA NO BANCO
===========================================================
O texto mora em ``fixtures/diretrizes.json`` (dentro do pacote Python, como o
``demo_pack.json``, porque ``data/`` é gitignorado e uma diretriz precisa
sobreviver a um clone). O ``pf annotate seed`` a carrega para a tabela
``diretrizes``, e é do BANCO que a API a serve — assim uma diretriz já usada
para gravar trabalho continua legível mesmo que alguém edite o arquivo depois.

**Editar uma versão já usada é a única operação proibida aqui.** Mudou a regra,
sobe a versão: o seed insere a nova e a antiga permanece, apontada pelas
anotações que a seguiram. O ``semear_diretrizes`` recusa a sobrescrita em
silêncio justamente por isso.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from . import db as adb

#: O arquivo versionado, dentro do pacote.
ARQUIVO = Path(__file__).resolve().parent / "fixtures" / "diretrizes.json"

#: Contrato do ``texto_json`` da tabela.
SCHEMA_DIRETRIZ = "diretrizes@1"


def carregar(caminho: Path | None = None) -> dict[str, Any]:
    """Lê e confere o arquivo. Falha ALTO: diretriz quebrada é regra ausente."""
    alvo = Path(caminho) if caminho is not None else ARQUIVO
    dados = json.loads(alvo.read_text(encoding="utf-8"))
    versao = int(dados.get("versao", 0))
    if versao < 1:
        raise ValueError(f"{alvo}: 'versao' precisa ser >= 1")
    tipos = dados.get("tipos") or {}
    faltando = sorted(set(adb.TIPOS_TAREFA) - set(tipos))
    if faltando:
        # Um tipo sem diretriz abriria um workspace sem instrução nenhuma — a
        # tela ficaria muda justamente onde ela mais precisa falar.
        raise ValueError(f"{alvo}: sem diretriz para {', '.join(faltando)}")
    sobrando = sorted(set(tipos) - set(adb.TIPOS_TAREFA))
    if sobrando:
        raise ValueError(f"{alvo}: tipo desconhecido {', '.join(sobrando)}")
    for tipo, linhas in tipos.items():
        if not isinstance(linhas, list) or not linhas:
            raise ValueError(f"{alvo}: as diretrizes de {tipo!r} precisam ser uma lista não vazia")
        if not all(isinstance(x, str) and x.strip() for x in linhas):
            raise ValueError(f"{alvo}: linha vazia nas diretrizes de {tipo!r}")
    return dados


def semear(conn: sqlite3.Connection, dados: dict[str, Any] | None = None) -> int:
    """Insere a versão do arquivo. Devolve quantas linhas ENTRARAM.

    Idempotente por ``(tipo, versao)``. **Não sobrescreve**: se a versão já está
    no banco com outro texto, levanta — reescrever uma diretriz já usada para
    gravar trabalho humano transformaria em mentira todo ``versao_diretriz``
    que aponta para ela.
    """
    pacote = dados if dados is not None else carregar()
    versao = int(pacote["versao"])
    entraram = 0
    for tipo, linhas in pacote["tipos"].items():
        texto = json.dumps(
            {"schema": SCHEMA_DIRETRIZ, "linhas": list(linhas)}, ensure_ascii=False
        )
        atual = conn.execute(
            "SELECT texto_json FROM diretrizes WHERE tipo = ? AND versao = ?",
            (tipo, versao),
        ).fetchone()
        if atual is not None:
            if str(atual["texto_json"]) != texto:
                raise RuntimeError(
                    f"a diretriz {tipo}@v{versao} já está no banco com outro texto. "
                    "Diretriz usada para gravar trabalho não se edita — suba a "
                    "'versao' em fixtures/diretrizes.json e semeie de novo."
                )
            continue
        conn.execute(
            "INSERT INTO diretrizes (tipo, versao, texto_json) VALUES (?, ?, ?)",
            (tipo, versao, texto),
        )
        entraram += 1
    return entraram


def versao_vigente(conn: sqlite3.Connection, tipo: str) -> int | None:
    """A maior versão gravada para o tipo — a que vale para quem anota AGORA.

    ``None`` num banco sem diretrizes semeadas (clone limpo). Nesse caso a
    submissão grava ``versao_diretriz = NULL``, que é honesto: não havia regra
    publicada. Inventar 1 ali diria que a anotação seguiu uma diretriz que não
    existia.
    """
    linha = conn.execute(
        "SELECT max(versao) AS v FROM diretrizes WHERE tipo = ?", (tipo,)
    ).fetchone()
    return None if linha is None or linha["v"] is None else int(linha["v"])


def vigentes(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """``{tipo: {versao, linhas}}`` — o que a tela do anotador desenha.

    Uma consulta só para os quatro tipos: a tela pede isto uma vez por sessão e
    não deveria pagar quatro idas ao banco por causa disso.
    """
    linhas = conn.execute(
        "SELECT d.tipo, d.versao, d.texto_json FROM diretrizes d "
        "WHERE d.versao = (SELECT max(versao) FROM diretrizes WHERE tipo = d.tipo) "
        "ORDER BY d.tipo"
    ).fetchall()
    saida: dict[str, dict[str, Any]] = {}
    for linha in linhas:
        try:
            conteudo = json.loads(str(linha["texto_json"]))
        except (TypeError, ValueError):  # pragma: no cover - texto corrompido à mão
            conteudo = {}
        saida[str(linha["tipo"])] = {
            "versao": int(linha["versao"]),
            "linhas": conteudo.get("linhas", []) if isinstance(conteudo, dict) else [],
        }
    return saida


__all__ = [
    "ARQUIVO",
    "SCHEMA_DIRETRIZ",
    "carregar",
    "semear",
    "versao_vigente",
    "vigentes",
]
