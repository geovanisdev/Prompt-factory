"""s10 — aplica o classificador ao universo inteiro, com limiares de confiança.

Entradas: ``models/classifier_*.npz`` + ``models/classifier_meta.json`` (s09) e
o par posicional ``emb/universe.f16.npy`` + ``emb/universe_uids.txt`` (s06).
Saída: ``final/labeled.parquet`` — uma linha por linha do universo, nas 8
colunas que o s11 consome.

**Só ``task_type`` e ``domain`` são aplicados.** São os dois eixos com meta
acordada (macro-F1 >= 0,65 / >= 0,55) e os dois que viram faceta na interface.
``quality`` é treinado e MEDIDO pelo s09, mas não gravado: um 1..3 plausível e
errado entraria no filtro ``quality_min`` da interface sem nada denunciando, e
não existe meta que diga quando ele está bom o bastante. ``nsfw`` idem (nem
treino tem: 18 positivos). Os dois saem NULL — o valor honesto de "ninguém
decidiu".

**As três faixas de confiança** (``[classifier]``): >= ``conf_accept`` grava o
rótulo limpo; entre ``conf_review`` e ``conf_accept`` grava com
``needs_review = 1``; abaixo de ``conf_review`` o eixo fica NULL — abstenção.
A linha abstida em ``task_type`` entra no teto de
``[loaddb] allow_unlabeled_pct`` do pré-voo do s11, e o funil daqui imprime
essa conta ANTES de o ``pf load-db`` recusar.

**``label_confidence`` é o MÍNIMO dos eixos.** É a coluna que ordena a fila de
revisão da interface: o elo mais fraco da linha é o que pede olho humano, e uma
média esconderia um ``domain`` de 0,36 atrás de um ``task_type`` de 0,95.

**Desalinhamento é fatal, e ``--force`` não destrava.** O ``.npz`` é posicional
contra o ``.npy``, que é posicional contra o universo: aplicado a embeddings de
outra build, o classificador devolve rótulos plausíveis da linha ERRADA — o
mesmo pior-defeito-possível da busca semântica, com as mesmas guardas (sha dos
insumos gravado no meta + uids conferidos contra o universo). ``--force`` só
destrava o caso legítimo: o ``seed_labels`` mudou DEPOIS do treino (rótulos
novos chegaram) e alguém decide aplicar o modelo antigo assim mesmo.
"""

from __future__ import annotations

import json
from collections import Counter

import numpy as np

from ..config import get
from ..export import sha256_arquivo
from ..schema import DOMAINS, TASK_TYPES, load_taxonomy
from . import (
    LABELED,
    MODELOS_META,
    MODELOS_NPZ,
    SEED_LABELS,
    UNIVERSE,
    UNIVERSE_EMB,
    UNIVERSE_UIDS,
    Cronometro,
    StageConfig,
    escrever_tabela,
    exigir,
    imprimir_funil,
    rel,
)

ESTAGIO = "s10"

#: Os eixos gravados no ``labeled.parquet`` (ver a docstring do módulo).
EIXOS_APLICADOS: tuple[str, ...] = ("task_type", "domain")

_VALIDOS: dict[str, tuple[str, ...]] = {"task_type": TASK_TYPES, "domain": DOMAINS}


def decidir(conf: float, aceita: float, revisa: float) -> tuple[bool, bool]:
    """(grava_o_rótulo, precisa_revisão) para UMA confiança.

    Função pura de propósito: é a definição única das três faixas, usada pelo
    laço do ``run`` e pelos testes — duas cópias da regra divergiriam em
    silêncio na fronteira (o ``>=`` importa: ``conf == conf_accept`` é aceite
    limpo, ``conf == conf_review`` ainda grava).
    """
    if conf >= aceita:
        return True, False
    if conf >= revisa:
        return True, True
    return False, True


def _softmax(logits: np.ndarray) -> np.ndarray:
    maior = logits.max(axis=1, keepdims=True)
    exp = np.exp(logits - maior)
    return exp / exp.sum(axis=1, keepdims=True)


def _carregar_modelo(cfg: StageConfig, eixo: str, dim: int) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Pesos + classes de um eixo, validados contra a taxonomia e a dimensão."""
    caminho = cfg.caminho(MODELOS_NPZ.format(eixo=eixo))
    exigir(caminho, "pf train")
    dados = np.load(caminho)
    W = np.asarray(dados["W"], dtype=np.float32)
    b = np.asarray(dados["b"], dtype=np.float32)
    classes = [str(c) for c in dados["classes"].tolist()]
    if W.shape != (len(classes), dim):
        raise SystemExit(
            f"[{ESTAGIO}] {rel(caminho)}: pesos {W.shape} não casam com {len(classes)} classes "
            f"x {dim} dims — artefato de outra build"
        )
    fora = sorted(set(classes) - set(_VALIDOS[eixo]))
    if fora:
        raise SystemExit(f"[{ESTAGIO}] {rel(caminho)}: classes fora da taxonomia: {', '.join(fora)}")
    return W, b, classes


def run(cfg: StageConfig) -> int:
    """Pontua o universo, aplica os limiares e grava o parquet. 0 = sucesso."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    relogio = Cronometro(ESTAGIO)
    load_taxonomy()
    cfg.preparar_dirs()

    caminho_meta = cfg.caminho(MODELOS_META)
    caminho_emb = cfg.caminho(UNIVERSE_EMB)
    caminho_uids = cfg.caminho(UNIVERSE_UIDS)
    caminho_rotulos = cfg.caminho(SEED_LABELS)
    universo = cfg.caminho(UNIVERSE)
    exigir(caminho_meta, "pf train")
    exigir(caminho_emb, "pf run s06")
    exigir(caminho_uids, "pf run s06")
    exigir(caminho_rotulos, "pf merge-labels")
    exigir(universo, "pf run s01-s06")

    meta = json.loads(caminho_meta.read_text(encoding="utf-8"))
    faltando = [e for e in EIXOS_APLICADOS if e not in meta.get("eixos", {})]
    if faltando:
        raise SystemExit(
            f"[{ESTAGIO}] eixo(s) sem treino no meta: {', '.join(faltando)} — rode `pf train`"
        )

    insumos = meta.get("insumos", {})
    if sha256_arquivo(caminho_emb) != insumos.get("emb_sha256"):
        raise SystemExit(
            f"[{ESTAGIO}] {rel(caminho_emb)} NÃO é o arquivo sobre o qual o modelo foi treinado — "
            "embeddings de outra build produzem rótulos plausíveis da linha errada; rode "
            "`pf train` de novo sobre os atuais (--force não destrava isto de propósito)"
        )
    if sha256_arquivo(caminho_rotulos) != insumos.get("seed_labels_sha256"):
        if not cfg.force:
            raise SystemExit(
                f"[{ESTAGIO}] {rel(caminho_rotulos)} mudou depois do treino — rode `pf train` "
                "para o modelo ver os rótulos novos, ou repita com --force para aplicar o "
                "modelo antigo assim mesmo"
            )
        print(f"[{ESTAGIO}] --force: aplicando um modelo treinado sobre OUTRO seed_labels")

    uids = caminho_uids.read_text(encoding="utf-8").splitlines()
    uids_universo = pq.read_table(universo, columns=["uid"]).column("uid").to_pylist()
    if uids != uids_universo:
        raise SystemExit(
            f"[{ESTAGIO}] {rel(caminho_uids)} não bate com a coluna uid de {rel(universo)} — "
            "par de outra build do universo"
        )
    emb = np.load(caminho_emb, mmap_mode="r")
    dim = int(insumos.get("dim", 0))
    if emb.shape != (len(uids), dim):
        raise SystemExit(
            f"[{ESTAGIO}] {rel(caminho_emb)} tem forma {emb.shape}; o meta espera "
            f"({len(uids)}, {dim}) — par de outra build"
        )

    modelos = {eixo: _carregar_modelo(cfg, eixo, dim) for eixo in EIXOS_APLICADOS}

    n_total = len(uids)
    n = n_total if cfg.max_rows is None else max(0, min(int(cfg.max_rows), n_total))
    if n < n_total:
        print(
            f"[{ESTAGIO}] --max-rows {n}: ENSAIO — o parquet sai PARCIAL e o pré-voo do "
            "`pf load-db` vai recusá-lo (esperado)"
        )

    aceita = float(get("classifier", "conf_accept", default=0.50))
    revisa = float(get("classifier", "conf_review", default=0.35))
    bloco = int(get("classifier", "apply_block_rows", default=16384))

    rotulos: dict[str, list[str | None]] = {eixo: [] for eixo in EIXOS_APLICADOS}
    faixas: dict[str, Counter[str]] = {eixo: Counter() for eixo in EIXOS_APLICADOS}
    conf_min = np.empty(n, dtype=np.float32)
    revisar = np.zeros(n, dtype=np.int8)

    for inicio in range(0, n, bloco):
        fim = min(inicio + bloco, n)
        X = np.asarray(emb[inicio:fim], dtype=np.float32)
        confs: dict[str, np.ndarray] = {}
        indices: dict[str, np.ndarray] = {}
        for eixo, (W, b, _classes) in modelos.items():
            probs = _softmax(X @ W.T + b)
            confs[eixo] = probs.max(axis=1)
            indices[eixo] = probs.argmax(axis=1)
        for j in range(fim - inicio):
            menor = 1.0
            precisa = False
            for eixo, (_W, _b, classes) in modelos.items():
                conf = float(confs[eixo][j])
                menor = min(menor, conf)
                grava, rever = decidir(conf, aceita, revisa)
                rotulos[eixo].append(classes[int(indices[eixo][j])] if grava else None)
                precisa = precisa or rever
                if not grava:
                    faixas[eixo]["abstido (NULL)"] += 1
                elif rever:
                    faixas[eixo]["aceito com needs_review"] += 1
                else:
                    faixas[eixo]["aceito limpo"] += 1
            conf_min[inicio + j] = menor
            revisar[inicio + j] = int(precisa)

    schema = pa.schema(
        [
            pa.field("uid", pa.string(), nullable=False),
            pa.field("task_type", pa.string(), nullable=True),
            pa.field("domain", pa.string(), nullable=True),
            pa.field("quality", pa.int8(), nullable=True),
            pa.field("nsfw", pa.bool_(), nullable=True),
            pa.field("label_method", pa.string(), nullable=True),
            pa.field("label_confidence", pa.float32(), nullable=True),
            pa.field("needs_review", pa.int8(), nullable=True),
        ]
    )
    destino = cfg.caminho(LABELED)
    escrever_tabela(
        pa.table(
            {
                "uid": uids[:n],
                "task_type": rotulos["task_type"],
                "domain": rotulos["domain"],
                "quality": pa.array([None] * n, type=pa.int8()),
                "nsfw": pa.array([None] * n, type=pa.bool_()),
                "label_method": ["classifier"] * n,
                "label_confidence": pa.array(conf_min.tolist(), type=pa.float32()),
                "needs_review": pa.array(revisar.tolist(), type=pa.int8()),
            },
            schema=schema,
        ),
        destino,
        schema=schema,
    )

    # --- relatório ----------------------------------------------------------
    def _fmt(valor: int) -> str:
        return f"{valor} ({valor / n:.1%})" if n else "0"

    imprimir_funil(
        ESTAGIO,
        ("eixo", "aceito limpo", "aceito com needs_review", "abstido (NULL)"),
        [
            [
                eixo,
                _fmt(faixas[eixo].get("aceito limpo", 0)),
                _fmt(faixas[eixo].get("aceito com needs_review", 0)),
                _fmt(faixas[eixo].get("abstido (NULL)", 0)),
            ]
            for eixo in EIXOS_APLICADOS
        ],
    )
    for eixo, ordem in (("task_type", TASK_TYPES), ("domain", DOMAINS)):
        contagem = Counter(v for v in rotulos[eixo] if v)
        if contagem:
            imprimir_funil(
                ESTAGIO,
                (f"{eixo} previsto", "n", "%"),
                [[c, contagem[c], f"{contagem[c] / n:.1%}"] for c in ordem if contagem.get(c)],
            )

    sem_task = faixas["task_type"].get("abstido (NULL)", 0)
    teto = float(get("loaddb", "allow_unlabeled_pct", default=1.0))
    pct_sem = 100.0 * sem_task / n if n else 0.0
    aviso = "" if pct_sem <= teto else (
        f" — ACIMA do teto: o load-db vai recusar; decida entre `pf train` melhor ou "
        f"`pf load-db --allow-unlabeled-pct {min(100.0, pct_sem + 1):.0f}`"
    )
    print(
        f"[{ESTAGIO}] linhas sem task_type: {sem_task} ({pct_sem:.2f}%) — o pré-voo do "
        f"`pf load-db` compara com [loaddb] allow_unlabeled_pct = {teto:g}{aviso}"
    )
    print(
        f"[{ESTAGIO}] needs_review = 1 em {int(revisar.sum())} linha(s); "
        f"label_confidence mediana {float(np.median(conf_min)):.3f}"
        if n
        else f"[{ESTAGIO}] 0 linhas"
    )
    print(f"[{ESTAGIO}] {n} linhas -> {rel(destino)} (model_id {meta.get('model_id', '?')})")
    relogio.fim()
    return 0


__all__ = [
    "EIXOS_APLICADOS",
    "ESTAGIO",
    "decidir",
    "run",
]
