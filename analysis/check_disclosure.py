"""Prova que os HTML renderizados de ``analysis/`` não publicam o que prometem não publicar.

Os dois ``.html`` são **commitados**, então o que entra neles é decisão, não
efeito colateral do que por acaso imprimiu. O ``analysis/README.md`` declara três
promessas — nenhum corpo de prompt (corpus, semente, lote ou pacote de
demonstração), nenhum alvo escondido de sintética ainda não revisada, e nenhum
rótulo do gabarito da campanha. Este script é o que transforma essas promessas em
verificação; antes dele elas eram conferidas à mão no dia do render, o que
significa que ninguém as conferia de novo.

**Por que não é um teste do pytest.** ``tests/conftest.py`` declara que nenhum
teste toca ``data/``, e as sondas daqui saem justamente do corpus, da semente e
do banco da Bancada. Um teste que dependesse desses arquivos seria pulado no CI e
em todo clone limpo — isto é, estaria verde exatamente onde não verifica nada.
Aqui ele é um passo do procedimento de re-render, documentado no README ao lado.

**Cobertura parcial é dita, nunca escondida.** Rodar sem o corpus no disco reduz
o conjunto de sondas, e um script que saísse 0 nesse caso estaria afirmando
"nada vazou" quando o certo é "não procurei". A saída separa as duas coisas, e é
a mesma distinção que o projeto faz entre ``None`` e ``0.0`` no agreement.

**O canário existe porque uma busca quebrada passa em tudo.** Se a normalização
tivesse um defeito — uma entidade HTML não desfeita, um espaço não colapsado —
todas as sondas negativas passariam e o resultado pareceria ótimo. Por isso cada
página carrega uma sonda POSITIVA: um trecho que tem de ser encontrado. Se o
canário some, o veredito é falha, não sucesso.

Uso::

    & "$env:USERPROFILE\\.local\\bin\\uv.exe" run python analysis/check_disclosure.py
"""

from __future__ import annotations

import html as html_mod
import json
import re
import sqlite3
import sys
from pathlib import Path

from prompt_factory import paths

# Trecho longo o bastante para ser distintivo (prosa comum não repete 60
# caracteres por acaso) e curto o bastante para sobreviver à quebra de linha que
# o navegador insere. Trechos são tirados do MEIO do texto: o começo de um prompt
# costuma ser genérico ("Escreva um texto sobre...") e daria falso positivo.
TRECHO = 60
POR_FONTE = 40

PAGINAS = {
    "01_annotation_qc.html": "reviewer calibration",
    "02_labeling_campaign_qc.html": "near-duplicate validation",
}


def normalizar(s: str) -> str:
    """Desfaz entidades HTML e colapsa espaço em branco.

    As duas coisas são obrigatórias e por motivos diferentes. Sem o unescape, um
    prompt contendo ``"`` aparece na página como ``&quot;`` e a sonda literal não
    o encontra — a verificação passaria por falso negativo, que é o pior desfecho
    possível aqui. Sem o colapso de espaço, a quebra de linha que o render insere
    no meio de uma frase parte a sonda em duas.
    """
    return re.sub(r"\s+", " ", html_mod.unescape(s))


def _amostrar(seq: list, n: int) -> list:
    """Amostra com passo constante, nunca as ``n`` primeiras.

    A semente é gravada ordenada por faixa de tamanho e o universo é gravado
    agrupado por fonte, então um corte pelo começo devolve o canto mais curto e
    mais homogêneo do arquivo — foi assim que a primeira versão deste script
    sondou 40 linhas da semente com 16 a 36 caracteres, todas descartadas por
    serem curtas demais, e declarou sucesso sem ter olhado a semente. É a mesma
    lição que o pool da Bancada já tinha aprendido com ``ORDER BY id LIMIT``.
    """
    if not seq:
        return []
    passo = max(1, len(seq) // n)
    return seq[::passo][:n]


def _trechos(textos, rotulo: str) -> list[tuple[str, str]]:
    """Extrai sondas distintivas do meio de cada texto."""
    out = []
    for t in textos:
        t = normalizar(str(t or ""))
        if len(t) < TRECHO * 2:
            continue
        meio = len(t) // 2
        out.append((rotulo, t[meio : meio + TRECHO]))
        if len(out) >= POR_FONTE:
            break
    return out


def sondas_negativas() -> tuple[list[tuple[str, str]], list[str]]:
    """Monta o que NÃO pode aparecer, e relata o que não pôde ser lido."""
    sondas: list[tuple[str, str]] = []
    ausentes: list[str] = []

    # 1. Um arquivo de lote: é o que o anotador recebe, com os 80 prompts.
    lote = paths.BATCHES / "batch_0001.json"
    if lote.exists():
        dados = json.loads(lote.read_text(encoding="utf-8"))
        itens = dados.get("items") or dados.get("itens") or []
        sondas += _trechos((i.get("text") for i in itens), "batch_0001.json")
    else:
        ausentes.append(f"{lote} (lote)")

    # 2. A semente. A coluna `text` é justamente a que o notebook 02 promete
    #    nunca carregar, então ela é a sonda mais direta dessa promessa.
    seed = paths.SEED / "seed.parquet"
    if seed.exists():
        import pyarrow.parquet as pq

        t = pq.read_table(seed, columns=["text"])
        # Amostra ao longo do arquivo inteiro: a semente é estratificada por
        # faixa de tamanho e o começo dela é só o balde dos textos curtos.
        sondas += _trechos(_amostrar(t.column("text").to_pylist(), POR_FONTE * 3),
                           "seed.parquet")
    else:
        ausentes.append(f"{seed} (semente)")

    # 3. O corpus. O universo é gravado agrupado por fonte, então o passo é o que
    #    faz a sonda atravessar as nove fontes em vez de ficar na primeira.
    if paths.DB_FILE.exists():
        con = sqlite3.connect(f"file:{paths.DB_FILE.as_posix()}?mode=ro&immutable=1", uri=True)
        try:
            (n_linhas,) = con.execute("SELECT count(*) FROM prompts").fetchone()
            passo = max(1, n_linhas // (POR_FONTE * 3))
            linhas = con.execute(
                "SELECT text FROM prompts WHERE length(text) > ? AND id % ? = 0 LIMIT ?",
                (TRECHO * 3, passo, POR_FONTE * 3),
            ).fetchall()
        finally:
            con.close()
        sondas += _trechos((r[0] for r in linhas), "prompts.sqlite")
    else:
        ausentes.append(f"{paths.DB_FILE} (corpus)")

    # 4. O pacote de demonstração. Escrito à mão, e mesmo assim não entra: a
    #    regra da página é uniforme, senão vira "não publicamos texto, exceto...".
    pack = Path(__file__).resolve().parents[1] / (
        "src/prompt_factory/annotate/fixtures/demo_pack.json"
    )
    if pack.exists():
        pack_json = json.loads(pack.read_text(encoding="utf-8"))
        textos: list[str] = []

        def _colher(v):
            if isinstance(v, dict):
                for k, x in v.items():
                    if k in ("prompt", "text", "texto", "resposta", "conteudo"):
                        if isinstance(x, str):
                            textos.append(x)
                    _colher(x)
            elif isinstance(v, list):
                for x in v:
                    _colher(x)

        _colher(pack_json)
        sondas += _trechos(textos, "demo_pack.json")
    else:
        ausentes.append(f"{pack} (pacote de demonstração)")

    # 5. O gabarito da campanha: uid e rótulos revisados à mão. É contra ele que
    #    TODO lote é pontuado — publicá-lo deixaria acertar o agreement por
    #    eliminação. Aqui a sonda é o próprio uid, que é curto e literal.
    if paths.LABEL_MANIFEST.exists():
        man = json.loads(paths.LABEL_MANIFEST.read_text(encoding="utf-8"))
        for uid in list(man.get("gold") or {})[:POR_FONTE]:
            sondas.append(("manifest.gold (uid do gabarito)", uid))
    else:
        ausentes.append(f"{paths.LABEL_MANIFEST} (manifest)")

    # 6. O alvo escondido de sintética AINDA NÃO revisada. Depois de avaliada o
    #    alvo pode ser publicado (é o que a auditoria faz); antes, não — o
    #    revisor é quem lê este repositório.
    if paths.ANNOTATE_DB_FILE.exists():
        con = sqlite3.connect(
            f"file:{paths.ANNOTATE_DB_FILE.as_posix()}?mode=ro&immutable=1", uri=True
        )
        try:
            linhas = con.execute(
                "SELECT an.id, an.gabarito_avaliacao_json FROM anotacoes an "
                "LEFT JOIN avaliacoes av ON av.anotacao_id = an.id "
                "WHERE an.gabarito_avaliacao_json IS NOT NULL AND av.id IS NULL"
            ).fetchall()
        finally:
            con.close()
        familias = set()
        for _id, blob in linhas:
            try:
                g = json.loads(blob)
            except (TypeError, ValueError):
                continue
            fam = g.get("familia_defeito")
            # "nenhum" é o valor dos alvos bons e aparece em prosa legítima;
            # sondá-lo daria falso positivo garantido.
            if fam and fam != "nenhum":
                familias.add(fam)
        for fam in sorted(familias):
            sondas.append(("familia de defeito plantada (item não revisado)", fam))
    else:
        ausentes.append(f"{paths.ANNOTATE_DB_FILE} (Bancada)")

    return sondas, ausentes


# Fontes de TEXTO que, existindo no disco, TÊM de render sonda. Uma delas
# presente e estéril significa que a página não foi verificada contra ela — e a
# primeira versão deste script caiu exatamente nisso com a semente, imprimindo
# "ok" sem nunca ter olhado o corpo de um prompt dela. Gabarito e família de
# defeito ficam fora desta lista porque zero é resposta legítima ali (uma
# campanha sem ouro importado, uma fila de sintéticas toda revisada).
FONTES_DE_TEXTO = ("batch_0001.json", "seed.parquet", "prompts.sqlite", "demo_pack.json")


def main() -> int:
    raiz = Path(__file__).resolve().parent
    sondas, ausentes = sondas_negativas()

    print(f"sondas negativas montadas: {len(sondas)}")
    por_fonte: dict[str, int] = {}
    for rotulo, _ in sondas:
        por_fonte[rotulo] = por_fonte.get(rotulo, 0) + 1
    for rotulo, n in sorted(por_fonte.items()):
        print(f"    {rotulo:<45} {n:>4}")
    if ausentes:
        print("\nCOBERTURA PARCIAL — não foi possível sondar a partir de:")
        for a in ausentes:
            print(f"    {a}")
        print("  (rode a partir do checkout que tem os dados; ausência aqui não é aprovação)")

    falhou = False

    faltando = [f for f in FONTES_DE_TEXTO
                if not por_fonte.get(f) and not any(f in a for a in ausentes)]
    if faltando:
        falhou = True
        print("\nFONTE PRESENTE E ESTÉRIL — existe no disco e não rendeu sonda nenhuma:")
        for f in faltando:
            print(f"    {f}")
        print("  A página NÃO foi verificada contra ela. Isto é falha, não observação:")
        print("  um 'ok' aqui afirmaria uma verificação que não aconteceu.")
    for nome, canario in PAGINAS.items():
        caminho = raiz / nome
        if not caminho.exists():
            print(f"\n{nome}: ABSENT — nada a verificar")
            falhou = True
            continue

        pagina = normalizar(caminho.read_text(encoding="utf-8", errors="replace"))

        # Controle POSITIVO primeiro: se o canário não aparece, a busca está
        # quebrada e qualquer "nada vazou" abaixo seria vazio.
        if canario not in pagina:
            print(f"\n{nome}: CANÁRIO NÃO ENCONTRADO ({canario!r}) — a busca não está "
                  f"funcionando; o resultado das sondas negativas não vale nada")
            falhou = True
            continue

        vazados = [(rot, s) for rot, s in sondas if s in pagina]
        if vazados:
            falhou = True
            print(f"\n{nome}: VAZOU — {len(vazados)} sonda(s) encontrada(s) na página")
            for rot, s in vazados[:10]:
                print(f"    [{rot}] {s[:80]!r}")
            if len(vazados) > 10:
                print(f"    ... e mais {len(vazados) - 10}")
        else:
            print(f"\n{nome}: ok — canário encontrado, {len(sondas)} sonda(s) ausente(s)")

    print()
    if falhou:
        print("VEREDITO: FALHOU")
        return 1
    print("VEREDITO: as duas páginas cumprem a política de disclosure do analysis/README.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
