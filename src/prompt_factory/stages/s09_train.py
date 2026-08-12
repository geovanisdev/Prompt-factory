"""s09 — treina o classificador de rótulos (regressão logística sobre embeddings).

Entradas: ``final/seed_labels.parquet`` (s08), o par posicional
``emb/universe.f16.npy`` + ``emb/universe_uids.txt`` (s06) e
``final/universe.parquet`` (só a coluna ``lang``, para o relatório por idioma).
Saídas: ``models/classifier_<eixo>.npz`` (pesos em forma de softmax) e
``models/classifier_meta.json`` (métricas, insumos e a escolha de C) — tudo
dentro de ``data/``, regenerável.

**Por que regressão logística sobre os embeddings, e não um modelo novo.** Os
vetores do e5 já existem para cada linha do universo (foram as horas do s05),
saem L2-normalizados e são a mesma representação que o dedup próximo validou
par a par; treinar em cima deles custa minutos de CPU e herda o multilíngue do
e5 — um classificador só para pt e en juntos. A alternativa (fine-tuning de um
encoder) custaria horas de GPU por um ganho que ninguém mediu: se o barato não
atingir a meta, ele vira o baseline do caro — nunca o contrário.

**O teste sai só de ``manual``/``agent``.** O rótulo ``native`` é um mapeamento
de categoria genérica que ninguém leu o texto para dar, e o próprio s08 mede
mapeamentos com até 78% de discordância contra o agente (dolly
``summarization``, "MAPEAMENTO SUSPEITO"). Medir o modelo contra isso mediria o
mapeamento, não o modelo. O nativo entra (com o peso 0.5 que o s08 carimbou)
apenas como AUMENTO de treino de ``task_type`` — e em duas variantes,
``sem_nativos`` e ``com_nativos``, medidas contra o MESMO teste retido. Quem
decide qual vira artefato é o número; na validação cruzada da variante
``com_nativos`` os nativos entram em todo fold de ajuste e em NENHUM de
validação, pela mesma razão.

**A métrica descreve a receita; o artefato usa 100% dos rótulos.** O macro-F1
publicado é medido num teste retido de ``[classifier] test_frac``; o ``.npz``
final é a mesma receita (mesmo C, mesma composição) reajustada sobre todos os
rótulos. Publicar o modelo do treino parcial jogaria fora um quinto do sinal
para manter um número que continuaria sendo estimativa do mesmo jeito.

**``nsfw`` fica fora.** São 18 positivos em 3.180: qualquer modelo sai dizendo
"False" com 99,4% de acurácia e utilidade zero — um eixo treinado nessas
condições seria um número decorativo no relatório.

**Macro-F1 é calculado sobre as classes presentes no teste.** Classe sem item
de teste (o split preserva singletons no treino em vez de perdê-los) aparece na
tabela com suporte 0 e fora da média: um F1 = 0 imposto a uma classe que
ninguém mediu derrubaria a média sem informação nenhuma — o ``meta.json``
lista essas classes em ``classes_sem_teste`` para o leitor saber o que NÃO foi
medido.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from ..config import get
from ..export import sha256_arquivo
from ..schema import DOMAINS, QUALITY_VALUES, TASK_TYPES, load_taxonomy
from . import (
    MODELOS_META,
    MODELOS_NPZ,
    SEED_LABELS,
    UNIVERSE,
    UNIVERSE_EMB,
    UNIVERSE_UIDS,
    Cronometro,
    StageConfig,
    exigir,
    imprimir_funil,
    rel,
    substituir,
)

ESTAGIO = "s09"

#: Eixos treináveis, na ordem do relatório (``nsfw`` fora — ver a docstring).
EIXOS: tuple[str, ...] = ("task_type", "domain", "quality")

#: Origens em que alguém LEU o texto para rotular: as únicas que entram no
#: teste. ``native`` só aumenta o treino de ``task_type``.
ORIGENS_CONFIAVEIS: tuple[str, ...] = ("manual", "agent")

#: Valores válidos por eixo — um valor fora disto no ``seed_labels`` é erro
#: fatal (parquet corrompido ou de outra taxonomia), nunca "classe nova".
_VALIDOS: dict[str, tuple[Any, ...]] = {
    "task_type": TASK_TYPES,
    "domain": DOMAINS,
    "quality": QUALITY_VALUES,
}

#: Chave do ``[classifier]`` com a meta de macro-F1 de cada eixo com portão
#: humano. ``quality`` não tem meta: é eixo diagnóstico, medido e não aplicado.
_META_POR_EIXO: dict[str, str] = {"task_type": "meta_f1_task", "domain": "meta_f1_domain"}


def _ajustar(X: np.ndarray, y: np.ndarray, pesos: np.ndarray, c: float, seed: int) -> Any:
    """Uma regressão logística multinomial, sempre com os mesmos botões.

    ``class_weight="balanced"`` porque a meta é macro-F1: sem ele, as 7.549
    linhas de ``qa-aberta`` afogam as 40 de ``planejamento`` e a média por
    classe paga a conta. ``sample_weight`` carrega o ``label_weight`` do s08
    (1.0 humano/agente, 0.5 nativo) por cima.
    """
    from sklearn.linear_model import LogisticRegression

    clf = LogisticRegression(C=c, class_weight="balanced", max_iter=2000, random_state=seed)
    clf.fit(X, y, sample_weight=pesos)
    return clf


def _pesos_softmax(clf: Any) -> tuple[np.ndarray, np.ndarray]:
    """Pesos em forma N_classes x dim, expandindo o caso binário.

    O sklearn guarda o binário como UMA linha (decisão sigmoide);
    ``softmax([0, z]) == sigmoide(z)``, então ``[zeros; w]`` reproduz o
    ``predict_proba`` exato sem um caminho especial no s10.
    """
    W = np.asarray(clf.coef_, dtype=np.float32)
    b = np.asarray(clf.intercept_, dtype=np.float32)
    if len(clf.classes_) == 2 and W.shape[0] == 1:
        W = np.vstack([np.zeros_like(W), W])
        b = np.concatenate([np.zeros_like(b), b])
    return W, b


def _f1_macro(y_real: np.ndarray, y_pred: np.ndarray) -> float:
    """Macro-F1 sobre as classes PRESENTES em ``y_real`` (ver a docstring)."""
    from sklearn.metrics import f1_score

    presentes = sorted(set(y_real.tolist()), key=str)
    return float(f1_score(y_real, y_pred, labels=presentes, average="macro", zero_division=0))


def _dividir(y: np.ndarray, frac: float, rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
    """Índices (treino, teste) estratificados por classe, determinísticos.

    Feito à mão em vez de ``train_test_split(stratify=y)`` porque o sklearn
    RECUSA classe com um exemplo só — e jogar a classe fora em silêncio seria
    pior que não medi-la: aqui o singleton fica inteiro no treino e a tabela
    final o mostra com suporte 0.
    """
    por_classe: dict[Any, list[int]] = {}
    for i, valor in enumerate(y.tolist()):
        por_classe.setdefault(valor, []).append(i)
    treino: list[int] = []
    teste: list[int] = []
    for valor in sorted(por_classe, key=str):
        indices = np.array(por_classe[valor])
        rng.shuffle(indices)
        n_teste = round(len(indices) * frac)
        n_teste = 0 if len(indices) < 2 else min(n_teste, len(indices) - 1)
        teste.extend(indices[:n_teste].tolist())
        treino.extend(indices[n_teste:].tolist())
    return np.array(sorted(treino), dtype=np.int64), np.array(sorted(teste), dtype=np.int64)


def _buscar_c(
    X: np.ndarray,
    y: np.ndarray,
    pesos: np.ndarray,
    extras: tuple[np.ndarray, np.ndarray, np.ndarray] | None,
    grade: list[float],
    folds: int,
    seed: int,
    rotulo: str,
) -> tuple[float, dict[str, float]]:
    """C por validação cruzada estratificada; ``extras`` (os nativos) entram em
    todo fold de AJUSTE e em nenhum de validação.

    Empate escolhe o menor C (a grade é crescente e o ``argmax`` fica com a
    primeira ocorrência): entre dois modelos indistinguíveis, o mais
    regularizado.
    """
    from sklearn.metrics import f1_score
    from sklearn.model_selection import StratifiedKFold

    minimo = min(Counter(y.tolist()).values())
    n_splits = min(folds, minimo)
    if n_splits < 2:
        c = grade[len(grade) // 2]
        print(
            f"[{ESTAGIO}] {rotulo}: classe com {minimo} exemplo impede a validação "
            f"cruzada — C = {c:g} (meio da grade)"
        )
        return c, {}
    if n_splits < folds:
        print(f"[{ESTAGIO}] {rotulo}: folds reduzidos {folds} -> {n_splits} (classe mais rara tem {minimo})")

    dobras = list(StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=seed).split(X, y))
    medias: dict[str, float] = {}
    for c in grade:
        notas: list[float] = []
        for idx_fit, idx_val in dobras:
            X_fit, y_fit, w_fit = X[idx_fit], y[idx_fit], pesos[idx_fit]
            if extras is not None:
                X_fit = np.vstack([X_fit, extras[0]])
                y_fit = np.concatenate([y_fit, extras[1]])
                w_fit = np.concatenate([w_fit, extras[2]])
            clf = _ajustar(X_fit, y_fit, w_fit, c, seed)
            notas.append(
                float(f1_score(y[idx_val], clf.predict(X[idx_val]), average="macro", zero_division=0))
            )
        medias[f"{c:g}"] = round(float(np.mean(notas)), 4)
    melhor = grade[int(np.argmax([medias[f"{c:g}"] for c in grade]))]
    return melhor, medias


def _gravar_npz(destino: Path, W: np.ndarray, b: np.ndarray, classes: list[Any]) -> None:
    """``.npz`` atômico (``.tmp`` + replace), como todo artefato da pipeline."""
    tmp = destino.parent / f"{destino.name}.tmp"
    destino.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp, "wb") as fh:
        np.savez(fh, W=W.astype(np.float32), b=b.astype(np.float32), classes=np.asarray(classes))
    substituir(tmp, destino)


def _tabela_por_classe(
    y_real: np.ndarray, y_pred: np.ndarray, classes: list[Any]
) -> list[dict[str, Any]]:
    """Precisão/revocação/F1/suporte de TODAS as classes treinadas, na ordem."""
    from sklearn.metrics import precision_recall_fscore_support

    if len(y_real) == 0:
        return [{"classe": c, "precisao": None, "revocacao": None, "f1": None, "suporte": 0} for c in classes]
    p, r, f, s = precision_recall_fscore_support(y_real, y_pred, labels=classes, zero_division=0)
    linhas = []
    for i, classe in enumerate(classes):
        suporte = int(s[i])
        linhas.append(
            {
                "classe": classe,
                "precisao": round(float(p[i]), 4) if suporte else None,
                "revocacao": round(float(r[i]), 4) if suporte else None,
                "f1": round(float(f[i]), 4) if suporte else None,
                "suporte": suporte,
            }
        )
    return linhas


def treinar_eixo(
    eixo: str,
    X_conf: np.ndarray,
    y_conf: np.ndarray,
    w_conf: np.ndarray,
    lang_conf: np.ndarray,
    nativos: tuple[np.ndarray, np.ndarray, np.ndarray] | None,
    destino_npz: Path,
    grade: list[float],
    folds: int,
    test_frac: float,
    seed: int,
) -> dict[str, Any]:
    """Treina um eixo, grava o ``.npz`` e devolve a entrada do meta."""
    rng = np.random.default_rng(seed)
    idx_treino, idx_teste = _dividir(y_conf, test_frac, rng)
    X_tr, y_tr, w_tr = X_conf[idx_treino], y_conf[idx_treino], w_conf[idx_treino]
    X_te, y_te = X_conf[idx_teste], y_conf[idx_teste]

    variantes: dict[str, dict[str, Any]] = {}
    predicoes: dict[str, np.ndarray] = {}
    planos = [("sem_nativos", None)]
    if nativos is not None and len(nativos[1]):
        planos.append(("com_nativos", nativos))
    for nome, extras in planos:
        c, cv = _buscar_c(X_tr, y_tr, w_tr, extras, grade, folds, seed, f"{eixo} {nome}")
        if extras is not None:
            clf = _ajustar(
                np.vstack([X_tr, extras[0]]),
                np.concatenate([y_tr, extras[1]]),
                np.concatenate([w_tr, extras[2]]),
                c,
                seed,
            )
        else:
            clf = _ajustar(X_tr, y_tr, w_tr, c, seed)
        pred = clf.predict(X_te) if len(idx_teste) else np.array([], dtype=y_conf.dtype)
        f1 = round(_f1_macro(y_te, pred), 4) if len(idx_teste) else None
        variantes[nome] = {"c": c, "cv_por_c": cv, "f1_macro_teste": f1}
        predicoes[nome] = pred

    # Empate fica com sem_nativos (dado mais simples): só um f1 estritamente
    # maior justifica carregar 19 mil rótulos que ninguém leu para dar.
    escolhida = "sem_nativos"
    if "com_nativos" in variantes:
        f1_sem = variantes["sem_nativos"]["f1_macro_teste"] or 0.0
        f1_com = variantes["com_nativos"]["f1_macro_teste"] or 0.0
        if f1_com > f1_sem:
            escolhida = "com_nativos"

    # O artefato: a receita vencedora reajustada sobre 100% dos rótulos.
    if escolhida == "com_nativos" and nativos is not None:
        X_full = np.vstack([X_conf, nativos[0]])
        y_full = np.concatenate([y_conf, nativos[1]])
        w_full = np.concatenate([w_conf, nativos[2]])
    else:
        X_full, y_full, w_full = X_conf, y_conf, w_conf
    clf_final = _ajustar(X_full, y_full, w_full, variantes[escolhida]["c"], seed)
    W, b = _pesos_softmax(clf_final)
    classes = clf_final.classes_.tolist()
    _gravar_npz(destino_npz, W, b, classes)

    pred_teste = predicoes[escolhida]
    por_classe = _tabela_por_classe(y_te, pred_teste, classes)
    por_idioma: dict[str, dict[str, Any]] = {}
    for idioma in ("pt", "en"):
        mascara = lang_conf[idx_teste] == idioma
        if int(mascara.sum()):
            por_idioma[idioma] = {
                "f1_macro": round(_f1_macro(y_te[mascara], pred_teste[mascara]), 4),
                "n": int(mascara.sum()),
            }

    return {
        "arquivo": destino_npz.name,
        "classes": classes,
        "n_confiaveis": len(y_conf),
        "n_treino": len(idx_treino),
        "n_teste": len(idx_teste),
        "n_nativos": len(nativos[1]) if nativos is not None else 0,
        "variante_escolhida": escolhida,
        "variantes": variantes,
        "c": variantes[escolhida]["c"],
        "f1_macro_teste": variantes[escolhida]["f1_macro_teste"],
        "por_classe": por_classe,
        "por_idioma": por_idioma,
        "classes_sem_teste": [linha["classe"] for linha in por_classe if not linha["suporte"]],
    }


def _dados_do_eixo(
    eixo: str,
    colunas: dict[str, list[Any]],
    posicoes: dict[str, int],
    emb: np.ndarray,
    lang_por_uid: dict[str, str],
) -> tuple[tuple[np.ndarray, ...], tuple[np.ndarray, np.ndarray, np.ndarray] | None]:
    """Separa os rótulos do eixo em (confiáveis, nativos) já como matrizes."""
    conf: list[tuple[int, Any, float, str]] = []
    nat: list[tuple[int, Any, float]] = []
    validos = set(_VALIDOS[eixo])
    fora: list[Any] = []
    for uid, valor, metodo, peso in zip(
        colunas["uid"], colunas[eixo], colunas["label_method"], colunas["label_weight"], strict=True
    ):
        if valor is None:
            continue
        if valor not in validos:
            fora.append(valor)
            continue
        if metodo in ORIGENS_CONFIAVEIS:
            conf.append((posicoes[uid], valor, float(peso), lang_por_uid.get(uid, "?")))
        else:
            nat.append((posicoes[uid], valor, float(peso)))
    if fora:
        raise SystemExit(
            f"[{ESTAGIO}] {eixo}: {len(fora)} valor(es) fora da taxonomia no seed_labels "
            f"(ex.: {fora[0]!r}) — o parquet está corrompido ou é de outra taxonomia"
        )
    if not conf:
        raise SystemExit(f"[{ESTAGIO}] {eixo}: nenhum rótulo manual/agent — rode a campanha antes")

    X_conf = np.asarray(emb[np.array([c[0] for c in conf])], dtype=np.float32)
    y_conf = np.asarray([c[1] for c in conf])
    w_conf = np.asarray([c[2] for c in conf], dtype=np.float64)
    lang_conf = np.asarray([c[3] for c in conf])
    nativos = None
    if nat:
        nativos = (
            np.asarray(emb[np.array([n[0] for n in nat])], dtype=np.float32),
            np.asarray([n[1] for n in nat]),
            np.asarray([n[2] for n in nat], dtype=np.float64),
        )
    return (X_conf, y_conf, w_conf, lang_conf), nativos


def run(cfg: StageConfig) -> int:
    """Treina os eixos, grava os artefatos e imprime as métricas. 0 = sucesso."""
    import pyarrow.parquet as pq

    relogio = Cronometro(ESTAGIO)
    load_taxonomy()
    cfg.preparar_dirs()

    caminho_rotulos = cfg.caminho(SEED_LABELS)
    caminho_emb = cfg.caminho(UNIVERSE_EMB)
    caminho_uids = cfg.caminho(UNIVERSE_UIDS)
    universo = cfg.caminho(UNIVERSE)
    exigir(caminho_rotulos, "pf merge-labels")
    exigir(caminho_emb, "pf run s06")
    exigir(caminho_uids, "pf run s06")
    exigir(universo, "pf run s01-s06")

    if cfg.axis is not None and cfg.axis not in EIXOS:
        raise SystemExit(f"[{ESTAGIO}] eixo desconhecido: {cfg.axis!r} (use {', '.join(EIXOS)})")

    grade = sorted(float(c) for c in get("classifier", "c_grid", default=[0.25, 1.0, 4.0, 16.0]))
    test_frac = float(get("classifier", "test_frac", default=0.2))
    seed = int(get("general", "seed", default=42))
    folds = int(cfg.folds)

    tabela = pq.read_table(caminho_rotulos)
    colunas = {
        nome: tabela.column(nome).to_pylist()
        for nome in ("uid", "task_type", "domain", "quality", "label_method", "label_weight")
    }

    posicoes = {u: i for i, u in enumerate(caminho_uids.read_text(encoding="utf-8").splitlines())}
    ausentes = [u for u in colunas["uid"] if u not in posicoes]
    if ausentes:
        raise SystemExit(
            f"[{ESTAGIO}] {len(ausentes)} uid(s) do seed_labels não estão em {rel(caminho_uids)} "
            f"(ex.: {ausentes[0]}) — o par .npy/uids é de outra build do universo"
        )

    emb = np.load(caminho_emb, mmap_mode="r")
    if emb.shape[0] != len(posicoes):
        raise SystemExit(
            f"[{ESTAGIO}] {rel(caminho_emb)} tem {emb.shape[0]} linhas e {rel(caminho_uids)} "
            f"tem {len(posicoes)} — par de outra build"
        )

    tabela_lang = pq.read_table(universo, columns=["uid", "lang"])
    lang_por_uid = dict(
        zip(
            tabela_lang.column("uid").to_pylist(),
            tabela_lang.column("lang").to_pylist(),
            strict=True,
        )
    )

    # --- meta anterior (treino parcial com --axis) ---------------------------
    sha_rotulos = sha256_arquivo(caminho_rotulos)
    sha_emb = sha256_arquivo(caminho_emb)
    destino_meta = cfg.caminho(MODELOS_META)
    eixos_anteriores: dict[str, Any] = {}
    if cfg.axis is not None and destino_meta.is_file():
        anterior = json.loads(destino_meta.read_text(encoding="utf-8"))
        insumos = anterior.get("insumos", {})
        if (insumos.get("seed_labels_sha256"), insumos.get("emb_sha256")) != (sha_rotulos, sha_emb):
            raise SystemExit(
                f"[{ESTAGIO}] o meta existente foi treinado sobre OUTROS insumos — atualizar só "
                f"`--axis {cfg.axis}` deixaria os demais eixos descrevendo dados que não existem "
                "mais; rode `pf train` completo"
            )
        eixos_anteriores = dict(anterior.get("eixos", {}))

    eixos = (cfg.axis,) if cfg.axis is not None else EIXOS
    novos: dict[str, Any] = {}
    resumo: list[list[Any]] = []
    for eixo in eixos:
        (X_conf, y_conf, w_conf, lang_conf), nativos = _dados_do_eixo(
            eixo, colunas, posicoes, emb, lang_por_uid
        )
        entrada = treinar_eixo(
            eixo,
            X_conf,
            y_conf,
            w_conf,
            lang_conf,
            nativos,
            cfg.caminho(MODELOS_NPZ.format(eixo=eixo)),
            grade,
            folds,
            test_frac,
            seed,
        )
        alvo = get("classifier", _META_POR_EIXO[eixo], default=None) if eixo in _META_POR_EIXO else None
        entrada["meta_f1"] = float(alvo) if alvo is not None else None
        f1 = entrada["f1_macro_teste"]
        entrada["meta_atingida"] = (
            None if f1 is None or alvo is None else bool(f1 >= float(alvo))
        )
        novos[eixo] = entrada

        situacao = "-"
        if entrada["meta_atingida"] is True:
            situacao = f"ATINGIDA (meta {float(alvo):.2f})"
        elif entrada["meta_atingida"] is False:
            situacao = f"NAO atingida (meta {float(alvo):.2f})"
        resumo.append(
            [
                eixo,
                entrada["n_treino"],
                entrada["n_teste"],
                entrada["n_nativos"] if entrada["variante_escolhida"] == "com_nativos" else 0,
                f"{entrada['c']:g}",
                f"{f1:.4f}" if f1 is not None else "-",
                situacao,
            ]
        )
        imprimir_funil(
            ESTAGIO,
            (f"{eixo} (variante {entrada['variante_escolhida']})", "precisão", "revocação", "f1", "suporte"),
            [
                [
                    linha["classe"],
                    f"{linha['precisao']:.2f}" if linha["precisao"] is not None else "-",
                    f"{linha['revocacao']:.2f}" if linha["revocacao"] is not None else "-",
                    f"{linha['f1']:.2f}" if linha["f1"] is not None else "-",
                    linha["suporte"],
                ]
                for linha in entrada["por_classe"]
            ],
        )
        if entrada["por_idioma"]:
            imprimir_funil(
                ESTAGIO,
                (f"{eixo} por idioma", "f1 macro", "n teste"),
                [[k, f"{v['f1_macro']:.4f}", v["n"]] for k, v in entrada["por_idioma"].items()],
            )
        if "com_nativos" in entrada["variantes"]:
            imprimir_funil(
                ESTAGIO,
                (f"{eixo} variantes", "C", "f1 macro teste"),
                [
                    [nome, f"{v['c']:g}", f"{v['f1_macro_teste']:.4f}" if v["f1_macro_teste"] is not None else "-"]
                    for nome, v in entrada["variantes"].items()
                ],
            )

    meta = {
        "model_id": hashlib.sha256(f"{sha_rotulos}{sha_emb}".encode()).hexdigest()[:16],
        "insumos": {
            "seed_labels_sha256": sha_rotulos,
            "emb_sha256": sha_emb,
            "n_universo": int(emb.shape[0]),
            "dim": int(emb.shape[1]),
        },
        "config": {"c_grid": grade, "test_frac": test_frac, "folds": folds, "seed": seed},
        "eixos": {**eixos_anteriores, **novos},
        "treinado_em": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    tmp = destino_meta.parent / f"{destino_meta.name}.tmp"
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    substituir(tmp, destino_meta)

    imprimir_funil(
        ESTAGIO,
        ("eixo", "treino", "teste", "nativos usados", "C", "f1 macro", "meta"),
        resumo,
    )
    print(f"[{ESTAGIO}] artefatos -> {rel(destino_meta)} (model_id {meta['model_id']})")
    relogio.fim()
    return 0


__all__ = [
    "EIXOS",
    "ESTAGIO",
    "ORIGENS_CONFIAVEIS",
    "run",
    "treinar_eixo",
]
