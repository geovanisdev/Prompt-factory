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

A LÍNGUA É UMA DIMENSÃO DO TEXTO, NÃO DA VERSÃO  (P3i)
=======================================================
``v1`` em inglês e ``v1`` em português são a **mesma regra dita em duas
línguas** — e ``anotacoes.versao_diretriz`` continua apontando para a versão,
nunca para o par (versão, língua). Por isso as duas línguas moram na **mesma
linha** da tabela, dentro do ``texto_json``, em vez de virarem uma coluna
``lang`` com ``UNIQUE (tipo, versao, lang)``:

* publicar ``v2`` só em inglês passa a ser **estruturalmente impossível**. Com
  uma linha por língua, ``versao_vigente`` (que é ``max(versao)``) começaria a
  devolver 2 e quem lê em português veria uma lista vazia — a tela ficaria muda
  exatamente onde ela mais precisa falar;
* ``anotacoes.versao_diretriz`` não ganha um segundo ponteiro para manter em dia;
* a tela recebe as **duas** línguas numa chamada só, e o botão de idioma troca o
  texto sem uma segunda ida ao servidor (trocar de língua não pode piscar).

O preço é o contrato do ``texto_json``, que subiu de ``diretrizes@1``
(``{"linhas": [...]}``) para ``diretrizes@2`` (``{"textos": {"en": [...],
"pt": [...]}}``). Ver ``_promovivel``: um banco que já tem a v1 monolíngue é
promovido **só** quando o português continua palavra por palavra o mesmo —
acrescentar tradução não é editar a regra; mudar a regra continua proibido.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from . import db as adb

#: O arquivo versionado, dentro do pacote.
ARQUIVO = Path(__file__).resolve().parent / "fixtures" / "diretrizes.json"

#: Contrato do ``texto_json`` da tabela. O ``@1`` guardava ``{"linhas": [...]}``
#: (uma língua só); o ``@2`` guarda ``{"textos": {"en": [...], "pt": [...]}}``.
SCHEMA_DIRETRIZ = "diretrizes@2"

#: O contrato monolíngue que o P3a gravou, e do qual ``_promovivel`` promove.
SCHEMA_DIRETRIZ_V1 = "diretrizes@1"

#: As línguas da interface. **O inglês é a fonte** (o portfólio é lido por
#: avaliadores estrangeiros); o português é a tradução que agrega valor. A ordem
#: é a de precedência, e ``IDIOMAS[0]`` é o default de quem não pediu nada.
IDIOMAS: tuple[str, ...] = ("en", "pt")

#: A língua em que o P3a escreveu as diretrizes — a que sobrevive palavra por
#: palavra na promoção do ``@1`` para o ``@2``.
IDIOMA_HERDADO = "pt"


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
    for tipo, textos in tipos.items():
        if not isinstance(textos, dict):
            raise ValueError(
                f"{alvo}: as diretrizes de {tipo!r} precisam ser um objeto por língua "
                f"({', '.join(IDIOMAS)}) — o formato de lista é o 'diretrizes@1'"
            )
        # PARIDADE, e não "pelo menos o inglês": uma língua a menos aqui vira uma
        # tela muda na hora de anotar, e o marco inteiro existe para que as duas
        # cubram as mesmas regras.
        if set(textos) != set(IDIOMAS):
            raise ValueError(
                f"{alvo}: {tipo!r} tem as línguas {sorted(textos)} — "
                f"as diretrizes precisam existir em {sorted(IDIOMAS)}, sem sobra nem falta"
            )
        for lang, linhas in textos.items():
            if not isinstance(linhas, list) or not linhas:
                raise ValueError(
                    f"{alvo}: as diretrizes de {tipo!r} em {lang!r} precisam ser "
                    "uma lista não vazia"
                )
            if not all(isinstance(x, str) and x.strip() for x in linhas):
                raise ValueError(f"{alvo}: linha vazia nas diretrizes de {tipo!r} em {lang!r}")
    return dados


def _texto_json(textos: dict[str, Any]) -> str:
    """O ``texto_json`` canônico de um tipo. Chaves ordenadas para o diff ser estável."""
    return json.dumps(
        {"schema": SCHEMA_DIRETRIZ, "textos": {lang: list(textos[lang]) for lang in IDIOMAS}},
        ensure_ascii=False,
    )


def _promovivel(atual: str, textos: dict[str, Any]) -> bool:
    """``True`` quando o que está no banco é a MESMA regra, só que monolíngue.

    É a única sobrescrita permitida aqui, e ela não é uma exceção à regra "não
    se edita diretriz usada": acrescentar a tradução de uma regra **não muda a
    regra**. A prova é literal — as linhas em português têm de continuar
    idênticas, uma a uma. Qualquer outra diferença cai no ``RuntimeError``.
    """
    try:
        antigo = json.loads(atual)
    except (TypeError, ValueError):  # pragma: no cover - texto corrompido à mão
        return False
    if not isinstance(antigo, dict) or antigo.get("schema") != SCHEMA_DIRETRIZ_V1:
        return False
    return list(antigo.get("linhas") or []) == list(textos[IDIOMA_HERDADO])


def semear(conn: sqlite3.Connection, dados: dict[str, Any] | None = None) -> int:
    """Insere a versão do arquivo. Devolve quantas linhas ENTRARAM ou foram promovidas.

    Idempotente por ``(tipo, versao)``. **Não sobrescreve**: se a versão já está
    no banco com outro texto, levanta — reescrever uma diretriz já usada para
    gravar trabalho humano transformaria em mentira todo ``versao_diretriz``
    que aponta para ela. A ÚNICA exceção é a promoção ``@1 -> @2`` descrita em
    ``_promovivel``, em que a regra não muda: ela só passa a existir também em
    inglês.
    """
    pacote = dados if dados is not None else carregar()
    versao = int(pacote["versao"])
    entraram = 0
    for tipo, textos in pacote["tipos"].items():
        texto = _texto_json(textos)
        atual = conn.execute(
            "SELECT texto_json FROM diretrizes WHERE tipo = ? AND versao = ?",
            (tipo, versao),
        ).fetchone()
        if atual is not None:
            gravado = str(atual["texto_json"])
            if gravado == texto:
                continue
            if not _promovivel(gravado, textos):
                raise RuntimeError(
                    f"a diretriz {tipo}@v{versao} já está no banco com outro texto. "
                    "Diretriz usada para gravar trabalho não se edita — suba a "
                    "'versao' em fixtures/diretrizes.json e semeie de novo."
                )
            conn.execute(
                "UPDATE diretrizes SET texto_json = ? WHERE tipo = ? AND versao = ?",
                (texto, tipo, versao),
            )
            entraram += 1
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


def _textos_do_json(bruto: Any) -> dict[str, list[str]]:
    """As duas línguas de um ``texto_json``, seja ele ``@1`` ou ``@2``.

    Um banco que ainda não passou pelo seed do P3i guarda o ``@1`` monolíngue.
    Lê-lo como português nas duas línguas é o que impede a tela de abrir vazia em
    inglês — texto na língua errada é ruim; **nenhum** texto é pior, e o seed já
    conserta na primeira execução.
    """
    try:
        conteudo = json.loads(str(bruto))
    except (TypeError, ValueError):  # pragma: no cover - texto corrompido à mão
        conteudo = {}
    if not isinstance(conteudo, dict):
        return {lang: [] for lang in IDIOMAS}
    textos = conteudo.get("textos")
    if isinstance(textos, dict):
        return {lang: list(textos.get(lang) or []) for lang in IDIOMAS}
    herdado = list(conteudo.get("linhas") or [])
    return {lang: list(herdado) for lang in IDIOMAS}


def vigentes(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """``{tipo: {versao, textos: {en, pt}}}`` — o que a tela do anotador desenha.

    As DUAS línguas de uma vez, de propósito: o botão de idioma troca o texto na
    tela sem uma segunda ida ao servidor, e uma tela que pisca ao trocar de
    língua faria a troca parecer um recarregamento.

    Uma consulta só para os quatro tipos: a tela pede isto uma vez por sessão e
    não deveria pagar quatro idas ao banco por causa disso.
    """
    linhas = conn.execute(
        "SELECT d.tipo, d.versao, d.texto_json FROM diretrizes d "
        "WHERE d.versao = (SELECT max(versao) FROM diretrizes WHERE tipo = d.tipo) "
        "ORDER BY d.tipo"
    ).fetchall()
    return {
        str(linha["tipo"]): {
            "versao": int(linha["versao"]),
            "textos": _textos_do_json(linha["texto_json"]),
        }
        for linha in linhas
    }


__all__ = [
    "ARQUIVO",
    "IDIOMAS",
    "IDIOMA_HERDADO",
    "SCHEMA_DIRETRIZ",
    "SCHEMA_DIRETRIZ_V1",
    "carregar",
    "semear",
    "versao_vigente",
    "vigentes",
]
