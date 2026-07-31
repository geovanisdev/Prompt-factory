"""Rotas de meta da Bancada: saúde dos dois bancos e as personas.

São as únicas rotas do P1, e as duas primeiras que a tela chama. O ``/api/health``
daqui é maior que o da curadoria de propósito: ele é o **painel de estado do
admin**, e o que ele diz precisa bastar para responder três perguntas sem abrir
um terminal:

1. os dois bancos abriram, e quais arquivos são;
2. **de onde vem o pool agora** (coleção curada ou fallback) e quantos itens tem;
3. quanto trabalho existe no banco da plataforma.

Tudo lido AO VIVO. Nada do corpus é congelado no lifespan: ele é trocado por
swap de arquivo embaixo desta app, e um número guardado na subida seria uma
mentira com data de validade.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, HTTPException

from .. import __version__
from ..config import get as _cfg
from . import db as adb
from . import pool as poolmod
from .deps import ConAnotacao, ConCorpus, Estado
from .models import PerfilIn, perfil

router = APIRouter(tags=["bancada"])

#: Os limites de ``[annotate]`` que a INTERFACE precisa conhecer para dizer o
#: que falta **antes** do clique. Lidos por request (o ``config`` é cacheado por
#: arquivo, então isto é um acesso a dict), e não congelados no lifespan: quem
#: mexe no ``settings.toml`` espera ver o efeito ao recarregar a página.
LIMITES: tuple[tuple[str, int], ...] = (
    ("min_chars_justificativa", 30),
    ("min_chars_sft", 40),
    ("min_criterios_rubrica", 3),
    ("max_criterios_rubrica", 8),
    ("min_chars_criacao", 15),
    ("claim_ttl_min", 120),
    ("page_size", 20),
)


def limites() -> dict[str, int]:
    """``{chave: valor}`` de ``[annotate]``, para a tela validar igual ao servidor."""
    return {chave: int(_cfg("annotate", chave, default=padrao)) for chave, padrao in LIMITES}


@router.get("/api/health", summary="Os dois bancos abriram, e de onde vem o pool")
def health(anot: ConAnotacao, corpus: ConCorpus, meta: Estado) -> dict[str, Any]:
    """O primeiro request da interface. Barato: ~0,2 ms de pool + 10 contagens.

    Note que **as duas conexões são pedidas na assinatura**: se o corpus tiver
    sumido entre a subida e agora, o erro aparece aqui, na primeira chamada, em
    vez de numa rota qualquer no meio de uma anotação.
    """
    p = poolmod.resolver(corpus)
    linha = corpus.execute("SELECT count(*) AS n FROM prompts").fetchone()
    corpus_meta = {
        r["key"]: r["value"] for r in corpus.execute("SELECT key, value FROM app_meta")
    }
    return {
        "status": "ok",
        "app": "Bancada",
        "pf_version": __version__,
        "sqlite_version": sqlite3.sqlite_version,
        "anotacao": {
            "arquivo": meta["db_anotacao"],
            "schema_version": int(
                adb.get_meta(anot, adb.CHAVE_VERSAO, str(adb.SCHEMA_VERSION_ANOTACAO))
                or adb.SCHEMA_VERSION_ANOTACAO
            ),
            "contagens": adb.contagens(anot),
        },
        "corpus": {
            "arquivo": meta["db_corpus"],
            # Dito explicitamente, e não deduzido pela interface: esta app NUNCA
            # escreve no corpus, e a tela mostra isso porque é uma promessa do
            # produto, não um detalhe de implementação.
            "somente_leitura": True,
            "n_prompts": int(linha["n"]),
            "db_build_id": corpus_meta.get("db_build_id"),
            "built_at": corpus_meta.get("built_at"),
            "schema_version": corpus_meta.get("schema_version"),
        },
        "pool": {
            "origem": p.origem,
            "n_pool": p.n_pool,
            "motivo": p.motivo,
            "colecao": p.colecao,
            "filtro_fallback": poolmod.descrever_fallback(),
        },
        # OS LIMITES SAEM DAQUI, e a tela os LÊ. É o que faz "o botão diz o que
        # falta" concordar com o 422 do servidor sem que os números existam em
        # dois lugares — eles moram em `config/settings.toml`, e uma cópia no
        # JavaScript divergiria na primeira vez que alguém ajustasse o arquivo.
        "limites": limites(),
    }


@router.get("/api/perfis", summary="As personas cadastradas")
def listar_perfis(anot: ConAnotacao) -> dict[str, Any]:
    """Ordenadas por papel e nome — é a ordem em que a tela de entrada agrupa."""
    linhas = anot.execute(
        "SELECT id, nome, papel, ativo, criado_em FROM anotadores "
        "ORDER BY papel, nome"
    ).fetchall()
    return {"items": [perfil(linha) for linha in linhas]}


@router.post("/api/perfis", status_code=201, summary="Cria uma persona")
def criar_perfil(corpo: PerfilIn, anot: ConAnotacao) -> dict[str, Any]:
    """409 em nome repetido — quem decide é o ``UNIQUE`` do DDL.

    Conferir com um SELECT antes seria uma corrida (e, com duas abas abertas na
    demonstração, uma corrida que acontece). Deixar o banco recusar e traduzir o
    erro é o mesmo caminho de ``app/routes_collections.criar``.
    """
    try:
        cur = anot.execute(
            "INSERT INTO anotadores (nome, papel) VALUES (?, ?)",
            (corpo.nome, corpo.papel),
        )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"já existe alguém chamado {corpo.nome!r} — escolha outro nome",
        ) from exc
    linha = anot.execute(
        "SELECT id, nome, papel, ativo, criado_em FROM anotadores WHERE id = ?",
        (int(cur.lastrowid or 0),),
    ).fetchone()
    return perfil(linha)


__all__ = ["router"]
