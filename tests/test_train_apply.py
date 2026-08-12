"""Testes do s09/s10 (M7): treino do classificador e aplicação ao universo.

O mundo em miniatura tem embeddings FABRICADOS, separáveis por construção (cada
classe mora numa direção própria do espaço), porque o que se testa aqui é o
CONTRATO dos estágios — artefatos, guardas de alinhamento, limiares e o parquet
que o s11 consome — e não a capacidade do modelo. Um fixture "realista"
provaria menos: métrica mediana passa com o modelo certo e com o modelo
trocado; num mundo separável, qualquer erro grande é defeito de código.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import numpy as np
import pyarrow as pa
import pytest

from prompt_factory.schema import COLUMN_NAMES, arrow_schema, text_stats
from prompt_factory.stages import (
    LABELED,
    MODELOS_META,
    MODELOS_NPZ,
    SEED_LABELS,
    UNIVERSE,
    UNIVERSE_EMB,
    UNIVERSE_UIDS,
    StageConfig,
)
from prompt_factory.stages import s09_train as s09
from prompt_factory.stages import s10_apply as s10

from .conftest import DEFAULTS, escrever

DIM = 8

#: Direção do espaço em que cada classe mora. `traducao` é o singleton (1
#: exemplo rotulado): exercita o caminho "fica no treino, sem teste" do split e
#: o pulo da validação cruzada.
_DIRECAO_TAREFA = {"qa-aberta": 0, "codigo": 1, "resumo": 2, "traducao": 6}
_DIRECAO_DOMINIO = {"tecnologia": 3, "geral": 4}

N = 48
N_ROTULADOS = 40  # 4 manual + 36 agent; os 8 restantes viram nativos (só task)


def _tarefa(i: int) -> str:
    if i == 39:
        return "traducao"
    return ("qa-aberta", "codigo", "resumo")[i % 3]


def _dominio(i: int) -> str:
    return ("tecnologia", "geral")[i % 2]


def _mundo(cfg: StageConfig) -> None:
    """Grava universo + embeddings + uids + seed_labels dentro do tmp."""
    rng = np.random.default_rng(7)
    uids = [f"{i:016x}" for i in range(N)]

    X = rng.normal(0.0, 0.05, size=(N, DIM))
    for i in range(N):
        X[i, _DIRECAO_TAREFA[_tarefa(i)]] += 1.0
        X[i, _DIRECAO_DOMINIO[_dominio(i)]] += 1.0
        X[i, 5] += ((i % 3) + 1) * 0.5  # quality 1..3 numa dimensão própria
    X /= np.linalg.norm(X, axis=1, keepdims=True)

    caminho_emb = cfg.caminho(UNIVERSE_EMB)
    caminho_emb.parent.mkdir(parents=True, exist_ok=True)
    np.save(caminho_emb, X.astype(np.float16))
    cfg.caminho(UNIVERSE_UIDS).write_text("\n".join(uids) + "\n", encoding="utf-8")

    linhas: list[dict[str, Any]] = []
    for i, uid in enumerate(uids):
        texto = f"prompt de teste numero {i} com conteudo bastante variado para o estagio"
        n_chars, n_words = text_stats(texto)
        linhas.append(
            {
                **DEFAULTS,
                "uid": uid,
                "text": texto,
                "text_raw": texto,
                "lang": "pt" if i % 2 == 0 else "en",
                "source_id": str(i),
                "hash_norm": f"{i:064x}",
                "n_chars": n_chars,
                "n_words": n_words,
            }
        )
    escrever(
        pa.table({c: [linha[c] for linha in linhas] for c in COLUMN_NAMES}, schema=arrow_schema()),
        cfg.caminho(UNIVERSE),
    )

    rotulos: list[dict[str, Any]] = []
    for i in range(N_ROTULADOS):
        rotulos.append(
            {
                "uid": uids[i],
                "task_type": _tarefa(i),
                "domain": _dominio(i),
                "quality": (i % 3) + 1,
                "nsfw": False,
                "label_method": "manual" if i < 4 else "agent",
                "label_weight": 1.0,
                "batch_id": "batch_0000" if i < 4 else "batch_0001",
            }
        )
    for i in range(N_ROTULADOS, N):
        rotulos.append(
            {
                "uid": uids[i],
                "task_type": _tarefa(i),
                "domain": None,
                "quality": None,
                "nsfw": None,
                "label_method": "native",
                "label_weight": 0.5,
                "batch_id": None,
            }
        )
    schema = pa.schema(
        [
            pa.field("uid", pa.string(), nullable=False),
            pa.field("task_type", pa.string(), nullable=False),
            pa.field("domain", pa.string(), nullable=True),
            pa.field("quality", pa.int8(), nullable=True),
            pa.field("nsfw", pa.bool_(), nullable=True),
            pa.field("label_method", pa.string(), nullable=False),
            pa.field("label_weight", pa.float32(), nullable=False),
            pa.field("batch_id", pa.string(), nullable=True),
        ]
    )
    escrever(
        pa.table({c: [r[c] for r in rotulos] for c in schema.names}, schema=schema),
        cfg.caminho(SEED_LABELS),
    )


@pytest.fixture
def mundo(stage_dirs: StageConfig) -> StageConfig:
    _mundo(stage_dirs)
    return replace(stage_dirs, folds=3)


def _meta(cfg: StageConfig) -> dict[str, Any]:
    return json.loads(cfg.caminho(MODELOS_META).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# s09 — treino
# ---------------------------------------------------------------------------


def test_treina_grava_artefatos_e_acerta_o_separavel(mundo: StageConfig) -> None:
    assert s09.run(mundo) == 0
    meta = _meta(mundo)
    assert set(meta["eixos"]) == {"task_type", "domain", "quality"}
    for eixo in meta["eixos"]:
        assert mundo.caminho(MODELOS_NPZ.format(eixo=eixo)).is_file()
    # dados separáveis por construção: erro grande aqui é defeito de código
    assert meta["eixos"]["task_type"]["f1_macro_teste"] >= 0.9
    assert meta["eixos"]["domain"]["f1_macro_teste"] >= 0.9
    assert meta["eixos"]["quality"]["f1_macro_teste"] >= 0.6
    # as metas do settings entram na resposta, com o veredito
    assert meta["eixos"]["task_type"]["meta_atingida"] is True
    assert meta["eixos"]["quality"]["meta_f1"] is None


def test_singleton_fica_no_treino_e_fora_da_media(mundo: StageConfig) -> None:
    assert s09.run(mundo) == 0
    entrada = _meta(mundo)["eixos"]["task_type"]
    assert "traducao" in entrada["classes"]
    assert "traducao" in entrada["classes_sem_teste"]
    suporte = {linha["classe"]: linha["suporte"] for linha in entrada["por_classe"]}
    assert suporte["traducao"] == 0


def test_variantes_com_e_sem_nativos_sao_medidas(mundo: StageConfig) -> None:
    assert s09.run(mundo) == 0
    entrada = _meta(mundo)["eixos"]["task_type"]
    assert set(entrada["variantes"]) == {"sem_nativos", "com_nativos"}
    assert entrada["variante_escolhida"] in entrada["variantes"]
    # domain não tem nativos (o s08 só mapeia task_type)
    assert set(_meta(mundo)["eixos"]["domain"]["variantes"]) == {"sem_nativos"}


def test_treino_e_deterministico(mundo: StageConfig) -> None:
    assert s09.run(mundo) == 0
    w1 = np.load(mundo.caminho(MODELOS_NPZ.format(eixo="task_type")))["W"].copy()
    assert s09.run(mundo) == 0
    w2 = np.load(mundo.caminho(MODELOS_NPZ.format(eixo="task_type")))["W"]
    assert np.array_equal(w1, w2)


def test_treino_parcial_por_eixo(mundo: StageConfig) -> None:
    assert s09.run(replace(mundo, axis="domain")) == 0
    meta = _meta(mundo)
    assert set(meta["eixos"]) == {"domain"}
    assert not mundo.caminho(MODELOS_NPZ.format(eixo="task_type")).is_file()
    # aplicar sem os dois eixos aplicáveis treinados é recusado com instrução
    with pytest.raises(SystemExit, match="pf train"):
        s10.run(mundo)


def test_valor_fora_da_taxonomia_e_fatal(mundo: StageConfig) -> None:
    import pyarrow.parquet as pq

    caminho = mundo.caminho(SEED_LABELS)
    tabela = pq.read_table(caminho).to_pydict()
    tabela["task_type"][0] = "categora-inventada"
    escrever(pa.table(tabela), caminho)
    with pytest.raises(SystemExit, match="taxonomia"):
        s09.run(mundo)


# ---------------------------------------------------------------------------
# s10 — aplicação
# ---------------------------------------------------------------------------


def test_aplica_no_contrato_que_o_s11_le(mundo: StageConfig) -> None:
    import pyarrow.parquet as pq

    assert s09.run(mundo) == 0
    assert s10.run(mundo) == 0
    t = pq.read_table(mundo.caminho(LABELED))
    assert t.column_names == [
        "uid",
        "task_type",
        "domain",
        "quality",
        "nsfw",
        "label_method",
        "label_confidence",
        "needs_review",
    ]
    assert t.num_rows == N
    assert t.column("uid").to_pylist() == [f"{i:016x}" for i in range(N)]
    tipos = {campo.name: str(campo.type) for campo in t.schema}
    assert tipos["quality"] == "int8"
    assert tipos["needs_review"] == "int8"
    assert tipos["label_confidence"] == "float"  # float32 do pyarrow
    assert set(t.column("label_method").to_pylist()) == {"classifier"}
    # quality e nsfw NÃO são aplicados: NULL em 100% das linhas
    assert t.column("quality").null_count == N
    assert t.column("nsfw").null_count == N
    # mundo separável: a maioria sai aceita
    aceitos = [v for v in t.column("task_type").to_pylist() if v]
    assert len(aceitos) >= int(0.8 * N)


def test_faixas_de_confianca_na_fronteira() -> None:
    # (grava_o_rótulo, precisa_revisão); o >= das fronteiras é contrato
    assert s10.decidir(0.50, 0.50, 0.35) == (True, False)
    assert s10.decidir(0.499, 0.50, 0.35) == (True, True)
    assert s10.decidir(0.35, 0.50, 0.35) == (True, True)
    assert s10.decidir(0.349, 0.50, 0.35) == (False, True)


def test_embeddings_de_outra_build_sao_fatais_mesmo_com_force(mundo: StageConfig) -> None:
    assert s09.run(mundo) == 0
    emb = np.load(mundo.caminho(UNIVERSE_EMB))
    np.save(mundo.caminho(UNIVERSE_EMB), emb[::-1].copy())  # mesma forma, outro conteúdo
    with pytest.raises(SystemExit, match="outra build"):
        s10.run(mundo)
    with pytest.raises(SystemExit, match="outra build"):
        s10.run(replace(mundo, force=True))


def test_seed_labels_novo_exige_force(mundo: StageConfig) -> None:
    import pyarrow.parquet as pq

    assert s09.run(mundo) == 0
    caminho = mundo.caminho(SEED_LABELS)
    tabela = pq.read_table(caminho)
    escrever(tabela.slice(0, tabela.num_rows - 1), caminho)
    with pytest.raises(SystemExit, match="--force"):
        s10.run(mundo)
    assert s10.run(replace(mundo, force=True)) == 0


def test_uids_fora_de_ordem_sao_fatais(mundo: StageConfig) -> None:
    assert s09.run(mundo) == 0
    caminho = mundo.caminho(UNIVERSE_UIDS)
    linhas = caminho.read_text(encoding="utf-8").splitlines()
    linhas[0], linhas[1] = linhas[1], linhas[0]
    caminho.write_text("\n".join(linhas) + "\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="uid"):
        s10.run(mundo)


def test_max_rows_e_ensaio_parcial(mundo: StageConfig) -> None:
    import pyarrow.parquet as pq

    assert s09.run(mundo) == 0
    assert s10.run(replace(mundo, max_rows=10)) == 0
    t = pq.read_table(mundo.caminho(LABELED))
    assert t.num_rows == 10
    assert t.column("uid").to_pylist() == [f"{i:016x}" for i in range(10)]


def test_o_labeled_alimenta_o_s11_com_a_precedencia_certa(mundo: StageConfig) -> None:
    """O s11 aplica «observado vence inferido»: as 40 linhas com rótulo
    manual/agent no seed_labels saem com ESSE método, e só as 8 restantes
    (nativas, que não vencem predição) ficam com o do classificador."""
    from prompt_factory import db as dbmod
    from prompt_factory.stages import DB_SQLITE
    from prompt_factory.stages import s11_load_db as s11

    assert s09.run(mundo) == 0
    assert s10.run(mundo) == 0
    assert s11.run(replace(mundo, allow_unlabeled_pct=100.0)) == 0
    conn = dbmod.connect(mundo.caminho(DB_SQLITE))
    try:
        metodos = dict(
            conn.execute(
                "SELECT label_method, count(*) FROM prompts WHERE task_type IS NOT NULL "
                "GROUP BY label_method"
            ).fetchall()
        )
    finally:
        conn.close()
    assert metodos.get("manual", 0) == 4
    assert metodos.get("agent", 0) == 36
    # os 8 uids sem rótulo observado ficam com a predição — mundo separável e
    # confiante, nenhum deles deveria abster
    assert metodos.get("classifier", 0) == N - N_ROTULADOS
