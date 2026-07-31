"""Wrapper de embeddings — testes LENTOS, que só rodam com o modelo em cache.

Numa máquina limpa estes testes **pulam**: baixar 450 MB no meio de um
`pytest -q` seria hostil. Depois do primeiro `pf run s05` o modelo está em
cache e eles passam a rodar de verdade; para forçar o download numa máquina
limpa, ``PF_DOWNLOAD_MODELS=1``.

O que se prova aqui é a *receita* (prefixo ``query: ``, normalização L2, fp32,
dimensão), não a qualidade do modelo.
"""

from __future__ import annotations

import os

import numpy as np
import pytest

from prompt_factory import embedder

pytestmark = pytest.mark.slow

BAIXAR = os.environ.get("PF_DOWNLOAD_MODELS") == "1"

requer_modelo = pytest.mark.skipif(
    not (BAIXAR or embedder.modelo_em_cache()),
    reason=(
        "modelo de embeddings fora do cache do HuggingFace "
        "(rode `pf run s05` ou exporte PF_DOWNLOAD_MODELS=1)"
    ),
)


def test_configuracao_vem_do_settings() -> None:
    assert embedder.model_name() == "intfloat/multilingual-e5-small"
    assert embedder.dim() == 384
    assert embedder.batch_size() >= 1
    assert embedder.truncate_chars() >= 512


@requer_modelo
def test_forma_e_normas() -> None:
    emb = embedder.embed_texts(["olá mundo", "hello world", ""])
    assert emb.shape == (3, embedder.dim())
    assert emb.dtype == np.float32
    assert np.allclose(np.linalg.norm(emb, axis=1), 1.0, atol=1e-3)


@requer_modelo
def test_lista_vazia_devolve_matriz_vazia() -> None:
    emb = embedder.embed_texts([])
    assert emb.shape == (0, embedder.dim())


@requer_modelo
def test_prefixo_query_e_aplicado() -> None:
    """``embed_texts("oi")`` tem de ser ``encode("query: oi")``, não ``encode("oi")``.

    É o teste que pega a regressão mais cara do projeto: sem o prefixo os
    vetores continuam saindo, os cossenos continuam plausíveis, e o índice
    inteiro fica num espaço diferente do das consultas.
    """
    modelo = embedder.get_model()
    nosso = embedder.embed_texts(["oi, tudo bem?"])[0]
    com_prefixo = modelo.encode(
        ["query: oi, tudo bem?"], normalize_embeddings=True, convert_to_numpy=True
    )[0]
    sem_prefixo = modelo.encode(
        ["oi, tudo bem?"], normalize_embeddings=True, convert_to_numpy=True
    )[0]
    assert float(np.dot(nosso, com_prefixo)) == pytest.approx(1.0, abs=1e-4)
    assert float(np.dot(nosso, sem_prefixo)) < 0.9999


@requer_modelo
def test_corte_de_texto_e_aplicado_antes_de_tokenizar() -> None:
    # O corte em truncate_chars vem ANTES da tokenização: o que passa do corte
    # não pode influenciar o vetor. O texto-base já é maior que o corte, então
    # o apêndice cai inteiro fora da janela.
    base = "explique a fotossíntese de forma simples e curta. " * 100
    assert len(base) > embedder.truncate_chars()
    curto = embedder.embed_texts([base])[0]
    longo = embedder.embed_texts([base + " GATO CACHORRO ELEFANTE" * 500])[0]
    assert float(np.dot(curto, longo)) == pytest.approx(1.0, abs=1e-4)
