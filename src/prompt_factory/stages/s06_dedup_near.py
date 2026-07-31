"""s06 — dedup próximo (cosseno + Jaccard), recheck de idioma e nascimento do ``universe.parquet``.

Entradas: ``interim/dedup1.parquet`` + ``emb/embeddings.f16.npy`` +
``emb/uids.txt``. Saídas: ``final/universe.parquet``,
``final/dedup_near_map.parquet`` e o par de embeddings alinhado ao universo
(``emb/universe.f16.npy`` + ``emb/universe_uids.txt``).

**Dois critérios, não um.** Cosseno alto sozinho junta prompts que apenas
*falam do mesmo assunto* — e este corpus tem um robô que gerou dezenas de
milhares de linhas com o mesmo cabeçalho e conteúdos diferentes. Só vira
duplicata quem passa nos três testes:

1. cosseno ≥ ``[dedup] near_cosine``;
2. Jaccard dos tokens (mesma normalização do hash) ≥ ``[dedup] near_jaccard``;
3. razão de ``n_chars`` ≤ ``[dedup] near_len_ratio`` — um resumo não é o texto.

**Por que a validação par a par (``[dedup] near_pairwise``).** Union-find é
transitivo por construção: confirmado A~B e B~C, o A e o C caem no mesmo
componente **sem nunca terem sido comparados**. Medido na primeira execução
deste estágio: dos 44.333 descartes, **27% (11.964) tinham cosseno < 0,985
contra o canônico do próprio grupo** e 13,4% tinham Jaccard < 0,65. Agrava o
problema o corte de 512 tokens do e5 (~1.879 caracteres em pt): dois textos que
compartilham um cabeçalho longo dão cosseno 1,0000 mesmo divergindo por
completo depois — houve um cluster de 2.088 linhas com 8.180 caracteres de
prefixo comum onde uma pedia previsão sobre o Surface RT e outra um roteiro de
Kyoto. Com a chave ligada, cada membro é reconferido **contra o canônico** nos
mesmos três critérios; quem falha sai do grupo e **permanece no universo**.
``near_pairwise = false`` reproduz o comportamento antigo (encadeamento aceito),
para poder replayar o estágio.

Duas decisões de projeto da validação, que valem registrar:

* **O canônico não muda.** ``dedup.choose_canonical`` é um ``min`` sobre o
  grupo, e a validação só remove membros que **não** são o canônico: o mínimo de
  um subconjunto que ainda contém o mínimo é o mesmo mínimo. Não é sorte, é
  invariante — e é o que permite validar num passe só.
* **Não há recomputação em cascata.** Dois membros poupados podem ser
  duplicatas *entre si*; reagrupá-los exigiria refazer o union-find até um ponto
  fixo, com o canônico mudando no meio, e trocaria um estágio explicável por um
  laço difícil de auditar. Preferimos deixar passar alguns pares próximos a
  descartar linha por transitividade.

**Recheck de idioma (``[dedup] lang_recheck``).** 976 linhas do template francês
``Goal / Corriger les erreurs de formatage...`` chegaram ao universo — 839 como
``pt`` (318 delas classificadas pt-BR e 212 pt-PT!) e 137 como ``en``. Os dois
detectores do s02 estavam **certos**: são documentos bilíngues, com ~200
caracteres de instrução em francês seguidos de milhares de caracteres de payload
JSON em português ou inglês. Na janela de 1.000 caracteres do s02 o payload
ganha por volume, e fastText e lingua **concordam** que o documento é pt/en — não
houve discordância para o árbitro pegar. O idioma do PROMPT, porém, é o idioma
da INSTRUÇÃO, não o do material colado embaixo dela.

Daí o recheck olhar o **cabeçalho** (``[dedup] lang_recheck_head_chars``, 250 —
uma janela *menor* que a do s02, não maior) e só das linhas em que cabeçalho e
documento podem divergir (``n_chars`` maior que a janela; nas demais o s02 já viu
exatamente o mesmo texto). Descarta quem tem as **duas** camadas de acordo num
idioma fora de {pt, en}, com o árbitro acima de
``[dedup] lang_recheck_arbiter_conf``. O árbitro já traz FRANCÊS, ESPANHOL,
ITALIANO e CATALÃO (``langdetect.IDIOMAS_ARBITRO``; lingua 2.2.0 não tem GALEGO).
Medido no corpus real: 2.215 linhas fora, 2.177 francesas e 38 espanholas, com a
família ``Goal/Corriger`` inteira (2.061) dentro — e nenhum falso positivo nas
154 restantes, todas prompts genuínos em francês ou espanhol.

Ele mora **aqui e não no s02** de propósito: refazer o s02 obrigaria a refazer o
s05, que são horas de embedding; no s06 os embeddings continuam válidos porque
remover linha não move as outras — a máscara é posicional.

**Por que blocos duplos.** A conta é ``X @ X.T`` dentro de cada idioma; com 145
mil linhas isso seria uma matriz de 84 GB. O passe percorre ``block_rows`` linhas
x ``col_chunk`` colunas por vez (2048 x 32768 f32 = 256 MB) e só olha o triângulo
superior. A multiplicação é **sempre em float32**: o `.npy` é f16 para caber no
disco, mas numpy não tem BLAS para f16 e a conta cairia para um laço em Python.
Os vetores são renormalizados em f32 depois do round-trip.

**Invariante posicional.** ``embeddings.f16.npy`` está alinhado ao
``dedup1.parquet``; ``universe.f16.npy`` ao ``universe.parquet``. O estágio
confere o primeiro par com assert (forma e uids, posição a posição) antes de
qualquer conta, e grava o segundo par junto do universo — sem isso a busca
semântica do app devolveria uids que não existem mais.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from .. import config, dedup
from ..schema import arrow_schema
from . import (
    BATCH_LEITURA,
    DEDUP1,
    EMB_UIDS,
    EMBEDDINGS,
    MAPA_PROXIMO,
    UNIVERSE,
    UNIVERSE_EMB,
    UNIVERSE_UIDS,
    Cronometro,
    EscritorParquet,
    StageConfig,
    checar_colunas,
    escrever_tabela,
    exigir,
    imprimir_funil,
    rel,
    substituir,
)

ESTAGIO = "s06"

#: Colunas do mapa de near-duplicatas.
COLUNAS_MAPA: tuple[str, ...] = ("uid", "canonical_uid", "cosine", "jaccard", "lang")

#: Os únicos idiomas que o corpus aceita. Cabeçalho fora daqui, com as duas
#: camadas de acordo, é linha de outro idioma que o s02 não enxergou.
IDIOMAS_DO_CORPUS: frozenset[str] = frozenset({"pt", "en"})


def _confirmar_pares(
    pares: Any,
    idx: Any,
    textos: Any,
    n_chars: Any,
    uf: dedup.UnionFind,
    limiar_jaccard: float,
    limiar_razao: float,
    cache: dict[int, frozenset[str]],
) -> int:
    """Aplica Jaccard nos candidatos e une o que passar. Devolve quantos passaram."""
    confirmados = 0
    for local_i, local_j in pares:
        if uf.find(local_i) == uf.find(local_j):
            # Já estão no mesmo componente: confirmar o par não mudaria nada e
            # o Jaccard é a parte cara do estágio.
            continue
        gi, gj = int(idx[local_i]), int(idx[local_j])
        ti = cache.get(gi)
        if ti is None:
            ti = cache[gi] = dedup.tokens(textos[gi].as_py() or "")
        tj = cache.get(gj)
        if tj is None:
            tj = cache[gj] = dedup.tokens(textos[gj].as_py() or "")
        if dedup.jaccard(ti, tj) < limiar_jaccard:
            continue
        if dedup.razao_tamanho(int(n_chars[gi]), int(n_chars[gj])) > limiar_razao:
            continue
        uf.union(local_i, local_j)
        confirmados += 1
    return confirmados


def _recheck_idioma(
    textos: Any,
    n_chars: Any,
    langs: Any,
    cabeca: int,
    conf_min: float,
) -> tuple[Any, Counter[str], Counter[tuple[str, str]]]:
    """Máscara de linhas cujo **cabeçalho** está num terceiro idioma.

    Devolve ``(máscara, {idioma detectado: quantas}, {(idioma, lang atual): n})``.
    Ver a docstring do módulo para o porquê da janela pequena.
    """
    import numpy as np
    import pyarrow.compute as pc

    from .. import langdetect

    n = len(n_chars)
    remover = np.zeros(n, dtype=bool)
    detectado: Counter[str] = Counter()
    cruzado: Counter[tuple[str, str]] = Counter()

    # Fatiar em pyarrow, não em Python: materializar 189 mil textos inteiros
    # (alguns de 1 MB) só para olhar 250 caracteres custaria minutos e ~1 GB.
    # utf8_slice_codeunits corta por CARACTERE, igual ao `len()` que gerou n_chars.
    cabecalhos = pc.utf8_slice_codeunits(textos, 0, cabeca).to_pylist()

    posicoes: list[int] = []
    rotulos: list[str] = []
    amostras: list[str] = []
    for i, cab in enumerate(cabecalhos):
        # Texto que cabe inteiro na janela não tem cabeçalho *distinto* do
        # documento: é exatamente o que o s02 já leu, e reprocessar só
        # devolveria o mesmo veredito com outro nome.
        if not cab or int(n_chars[i]) <= cabeca:
            continue
        rotulo, _score = langdetect.detect1(cab)
        if rotulo in IDIOMAS_DO_CORPUS or rotulo == langdetect.UNKNOWN:
            continue
        posicoes.append(i)
        rotulos.append(rotulo)
        amostras.append(cab)

    print(
        f"[{ESTAGIO}] recheck de idioma: {len(cabecalhos)} cabeçalhos, "
        f"{len(posicoes)} suspeitos (camada 1 fora de pt/en)",
        flush=True,
    )
    if not posicoes:
        return remover, detectado, cruzado

    for pos, rotulo, (arbitro, conf) in zip(
        posicoes, rotulos, langdetect.arbitrar(amostras), strict=True
    ):
        # As duas camadas têm de concordar, como no s02: sozinho, o árbitro
        # devolve CATALÃO para russo (não está no conjunto) e derrubaria linhas
        # boas com um rótulo inventado.
        if arbitro != rotulo or arbitro in IDIOMAS_DO_CORPUS or conf < conf_min:
            continue
        remover[pos] = True
        detectado[rotulo] += 1
        cruzado[(rotulo, str(langs[pos]))] += 1
    return remover, detectado, cruzado


def run(cfg: StageConfig) -> int:
    """Colapsa near-duplicatas e grava o universo. 0 = sucesso."""
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq

    relogio = Cronometro(ESTAGIO)
    cfg.preparar_dirs()
    origem = cfg.caminho(DEDUP1)
    exigir(origem, "pf run s04")
    origem_emb = cfg.caminho(EMBEDDINGS)
    exigir(origem_emb, "pf run s05")
    origem_uids = cfg.caminho(EMB_UIDS)
    exigir(origem_uids, "pf run s05")

    near_cos = float(config.get("dedup", "near_cosine", default=0.985))
    near_jac = float(config.get("dedup", "near_jaccard", default=0.65))
    near_ratio = float(config.get("dedup", "near_len_ratio", default=2.0))
    par_a_par = bool(config.get("dedup", "near_pairwise", default=True))
    block_rows = int(config.get("dedup", "block_rows", default=2048))
    col_chunk = int(config.get("dedup", "col_chunk", default=32768))
    recheck = bool(config.get("dedup", "lang_recheck", default=True))
    cabeca = int(config.get("dedup", "lang_recheck_head_chars", default=250))
    conf_arbitro = float(config.get("dedup", "lang_recheck_arbiter_conf", default=0.85))

    tabela_chaves = pq.read_table(
        origem, columns=["uid", "lang", "n_chars", "license", "source", "source_id", "text"]
    )
    n = tabela_chaves.num_rows
    uids = tabela_chaves.column("uid").to_pylist()
    langs = np.array(tabela_chaves.column("lang").to_pylist(), dtype=object)
    n_chars = np.array(tabela_chaves.column("n_chars").to_pylist(), dtype=np.int64)
    licencas = tabela_chaves.column("license").to_pylist()
    fontes = tabela_chaves.column("source").to_pylist()
    sids = tabela_chaves.column("source_id").to_pylist()
    # combine_chunks dá acesso posicional O(1) ao texto (o Jaccard consulta linhas
    # esparsas); soltar a tabela logo depois evita segurar a coluna duas vezes -
    # são ~1 GB de texto cada.
    textos = tabela_chaves.column("text").combine_chunks()
    del tabela_chaves

    emb = np.load(origem_emb, mmap_mode="r")
    if emb.shape[0] != n:
        raise SystemExit(
            f"[{ESTAGIO}] invariante quebrado: {rel(origem_emb)} tem {emb.shape[0]} linhas "
            f"e {rel(origem)} tem {n} — rode `pf run s05` de novo"
        )
    uids_npy = origem_uids.read_text(encoding="utf-8").splitlines()
    if uids_npy != uids:
        divergencia = next(
            (i for i, (a, b) in enumerate(zip(uids_npy, uids, strict=False)) if a != b),
            min(len(uids_npy), len(uids)),
        )
        raise SystemExit(
            f"[{ESTAGIO}] invariante quebrado: {rel(origem_uids)} diverge do parquet na "
            f"posição {divergencia} — rode `pf run s05` de novo"
        )
    print(f"[{ESTAGIO}] invariante posicional OK: {n} linhas x {emb.shape[1]} dims")
    print(
        f"[{ESTAGIO}] limiares: cosseno>={near_cos} jaccard>={near_jac} "
        f"razão<={near_ratio}; blocos {block_rows}x{col_chunk}; "
        f"par_a_par={par_a_par}; lang_recheck={recheck}"
    )

    # --- recheck de idioma ------------------------------------------------
    # Vem ANTES do agrupamento de propósito: assim uma linha em francês nunca é
    # escolhida canônica de um grupo (o que manteria justamente a errada e
    # descartaria as boas), e a partição de cosseno já sai menor.
    if recheck:
        remover_idioma, idiomas_fora, cruzado_idioma = _recheck_idioma(
            textos, n_chars, langs, cabeca, conf_arbitro
        )
    else:
        remover_idioma = np.zeros(n, dtype=bool)
        idiomas_fora = Counter()
        cruzado_idioma = Counter()
    n_idioma = int(np.count_nonzero(remover_idioma))
    if recheck:
        print(f"[{ESTAGIO}] recheck de idioma: {n_idioma} linhas descartadas", flush=True)

    remover = remover_idioma.copy()
    n_near = np.zeros(n, dtype=np.int32)
    mapa: list[dict[str, Any]] = []
    tamanhos: Counter[int] = Counter()
    removidas_por_fonte: Counter[str] = Counter()
    poupados_por_lang: Counter[str] = Counter()
    poupados_por_motivo: Counter[str] = Counter()
    maiores: list[tuple[int, str, str]] = []
    resumo_particoes: list[list[Any]] = []
    cache_tokens: dict[int, frozenset[str]] = {}

    for lang in sorted({str(x) for x in langs}):
        idx = np.flatnonzero((langs == lang) & ~remover_idioma)
        m = int(idx.size)
        if m < 2:
            resumo_particoes.append([lang, m, 0, 0, 0, 0, 0])
            continue
        X = np.asarray(emb[idx], dtype=np.float32)
        normas = np.linalg.norm(X, axis=1, keepdims=True)
        np.maximum(normas, 1e-12, out=normas)
        X /= normas

        uf = dedup.UnionFind(m)
        candidatos = confirmados = 0
        for i0 in range(0, m, block_rows):
            i1 = min(i0 + block_rows, m)
            A = X[i0:i1]
            for j0 in range(i0, m, col_chunk):
                j1 = min(j0 + col_chunk, m)
                S = A @ X[j0:j1].T
                linhas, colunas = np.nonzero(S >= near_cos)
                if linhas.size == 0:
                    continue
                gi = linhas + i0
                gj = colunas + j0
                acima = gj > gi
                gi, gj = gi[acima], gj[acima]
                if gi.size == 0:
                    continue
                # Filtro de tamanho vetorizado ANTES do Jaccard (que é Python).
                ci = n_chars[idx[gi]]
                cj = n_chars[idx[gj]]
                maior = np.maximum(ci, cj).astype(np.float64)
                menor = np.maximum(np.minimum(ci, cj), 1).astype(np.float64)
                passa = (maior / menor) <= near_ratio
                gi, gj = gi[passa], gj[passa]
                candidatos += int(gi.size)
                if gi.size:
                    confirmados += _confirmar_pares(
                        zip(gi.tolist(), gj.tolist(), strict=True),
                        idx,
                        textos,
                        n_chars,
                        uf,
                        near_jac,
                        near_ratio,
                        cache_tokens,
                    )
            print(
                f"[{ESTAGIO}] {lang}: {min(i1, m)}/{m} linhas varridas, "
                f"{candidatos} candidatos, {confirmados} confirmados",
                flush=True,
            )

        grupos = uf.groups()
        poupados_lang = 0
        for membros in grupos.values():
            globais = [int(idx[k]) for k in membros]
            canonico = int(
                dedup.choose_canonical(
                    [
                        {
                            "uid": uids[g],
                            "license": licencas[g],
                            "source": fontes[g],
                            "source_id": sids[g],
                            "_pos": g,
                        }
                        for g in globais
                    ]
                )["_pos"]
            )
            vetor_c = X[np.searchsorted(idx, canonico)]
            tok_c = cache_tokens.get(canonico)
            if tok_c is None:
                tok_c = cache_tokens[canonico] = dedup.tokens(textos[canonico].as_py() or "")
            removidos = 0
            for g in globais:
                if g == canonico:
                    continue
                tok_g = cache_tokens.get(g)
                if tok_g is None:
                    tok_g = cache_tokens[g] = dedup.tokens(textos[g].as_py() or "")
                cosseno = float(np.dot(vetor_c, X[np.searchsorted(idx, g)]))
                jac = float(dedup.jaccard(tok_c, tok_g))
                if par_a_par:
                    # Os MESMOS três critérios, agora contra o canônico. O motivo
                    # é o PRIMEIRO que falha, nesta ordem, para os três somarem o
                    # total de poupados no relatório.
                    razao = dedup.razao_tamanho(int(n_chars[canonico]), int(n_chars[g]))
                    motivo = (
                        "cosseno"
                        if cosseno < near_cos
                        else "jaccard"
                        if jac < near_jac
                        else "razão"
                        if razao > near_ratio
                        else ""
                    )
                    if motivo:
                        poupados_lang += 1
                        poupados_por_motivo[motivo] += 1
                        continue
                remover[g] = True
                removidos += 1
                removidas_por_fonte[str(fontes[g])] += 1
                mapa.append(
                    {
                        "uid": uids[g],
                        "canonical_uid": uids[canonico],
                        "cosine": cosseno,
                        "jaccard": jac,
                        "lang": lang,
                    }
                )
            # Contagens do canônico e do histograma refletem o cluster EFETIVO —
            # o que de fato colapsou —, não o componente inflado do union-find.
            n_near[canonico] = removidos
            if removidos:
                tamanhos[removidos + 1] += 1
                maiores.append((removidos + 1, uids[canonico], str(fontes[canonico])))
        poupados_por_lang[lang] = poupados_lang
        resumo_particoes.append(
            [
                lang,
                m,
                candidatos,
                confirmados,
                len(grupos),
                poupados_lang,
                int(np.count_nonzero(remover[idx] & ~remover_idioma[idx])),
            ]
        )
        del X

    # --- gravação --------------------------------------------------------
    destino = cfg.caminho(UNIVERSE)
    schema = arrow_schema()
    i_near = schema.get_field_index("n_near_dups")
    arquivo = pq.ParquetFile(origem)
    checar_colunas(arquivo.schema_arrow, rel(origem))
    offset = 0
    with EscritorParquet(destino, schema=schema) as escritor:
        for lote in arquivo.iter_batches(batch_size=BATCH_LEITURA):
            b = lote.num_rows
            manter = np.flatnonzero(~remover[offset : offset + b])
            if manter.size:
                tabela = pa.Table.from_batches([lote]).take(pa.array(manter, type=pa.int64()))
                perto = n_near[offset : offset + b][manter]
                tabela = tabela.set_column(
                    i_near, schema.field(i_near), pa.array(perto, type=pa.int32())
                )
                escritor.escrever(tabela.cast(schema))
            offset += b

    mantidas = np.flatnonzero(~remover)
    destino_emb = cfg.caminho(UNIVERSE_EMB)
    tmp_emb = destino_emb.parent / f"{destino_emb.name}.tmp"
    # np.save gruda ".npy" no NOME que recebe; com file handle ele respeita o .tmp.
    with tmp_emb.open("wb") as fh:
        np.save(fh, np.asarray(emb[mantidas], dtype=np.float16))
    substituir(tmp_emb, destino_emb)
    destino_uids = cfg.caminho(UNIVERSE_UIDS)
    tmp_uids = destino_uids.parent / f"{destino_uids.name}.tmp"
    with tmp_uids.open("w", encoding="utf-8", newline="\n") as fh:
        for p in mantidas.tolist():
            fh.write(f"{uids[p]}\n")
    substituir(tmp_uids, destino_uids)

    destino_mapa = cfg.caminho(MAPA_PROXIMO)
    escrever_tabela(
        pa.table(
            {
                "uid": [linha["uid"] for linha in mapa],
                "canonical_uid": [linha["canonical_uid"] for linha in mapa],
                "cosine": pa.array([linha["cosine"] for linha in mapa], type=pa.float32()),
                "jaccard": pa.array([linha["jaccard"] for linha in mapa], type=pa.float32()),
                "lang": [linha["lang"] for linha in mapa],
            },
            schema=pa.schema(
                [
                    pa.field("uid", pa.string(), nullable=False),
                    pa.field("canonical_uid", pa.string(), nullable=False),
                    pa.field("cosine", pa.float32(), nullable=False),
                    pa.field("jaccard", pa.float32(), nullable=False),
                    pa.field("lang", pa.string(), nullable=False),
                ]
            ),
        ),
        destino_mapa,
    )

    saida = int(mantidas.size)
    if pq.ParquetFile(destino).metadata.num_rows != saida:  # pragma: no cover - defensivo
        raise SystemExit(f"[{ESTAGIO}] universo gravado com contagem inesperada")
    conferir = np.load(destino_emb, mmap_mode="r")
    if conferir.shape[0] != saida:  # pragma: no cover - defensivo
        raise SystemExit(f"[{ESTAGIO}] universe.f16.npy com {conferir.shape[0]} != {saida}")
    del conferir

    imprimir_funil(
        ESTAGIO,
        ("lang", "linhas", "candidatos", "confirmados", "grupos", "poupados", "removidas"),
        resumo_particoes,
    )
    imprimir_funil(
        ESTAGIO,
        ("etapa", "linhas"),
        [
            ["lidas", n],
            ["descartadas por idioma", n_idioma],
            ["poupadas par a par", sum(poupados_por_lang.values())],
            ["removidas near-dup", n - saida - n_idioma],
            ["clusters", sum(tamanhos.values())],
        ],
        ["SAÍDA", saida],
    )
    if idiomas_fora:
        imprimir_funil(
            ESTAGIO,
            ("idioma detectado", "linhas", "vinha como pt", "vinha como en"),
            [
                [idioma, quantas, cruzado_idioma[(idioma, "pt")], cruzado_idioma[(idioma, "en")]]
                for idioma, quantas in idiomas_fora.most_common()
            ],
        )
    if par_a_par:
        imprimir_funil(
            ESTAGIO,
            ("lang", "poupados par a par"),
            sorted(poupados_por_lang.items()),
            ["TOTAL", sum(poupados_por_lang.values())],
        )
        if poupados_por_motivo:
            imprimir_funil(
                ESTAGIO,
                ("1º critério que falhou", "poupados"),
                sorted(poupados_por_motivo.items(), key=lambda kv: -kv[1]),
            )
    if removidas_por_fonte:
        imprimir_funil(
            ESTAGIO,
            ("fonte", "removidas"),
            sorted(removidas_por_fonte.items(), key=lambda kv: -kv[1]),
        )
    if tamanhos:
        imprimir_funil(
            ESTAGIO,
            ("tamanho do cluster", "quantos"),
            sorted(tamanhos.items()),
        )
        maiores.sort(key=lambda t: -t[0])
        imprimir_funil(
            ESTAGIO,
            ("tam", "uid canônico", "fonte"),
            [list(x) for x in maiores[:5]],
        )
    print(f"[{ESTAGIO}] {saida} linhas -> {rel(destino)}")
    print(f"[{ESTAGIO}] {len(mapa)} descartes -> {rel(destino_mapa)}")
    print(f"[{ESTAGIO}] embeddings do universo -> {rel(destino_emb)} + {rel(destino_uids)}")
    relogio.fim()
    return 0


__all__ = ["COLUNAS_MAPA", "ESTAGIO", "IDIOMAS_DO_CORPUS", "run"]
