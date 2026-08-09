"""Peças comuns dos dois dedups: escolha do canônico, union-find e Jaccard.

O s04 (exato) e o s06 (próximo) agrupam linhas por critérios diferentes, mas
respondem à mesma pergunta depois: **qual das linhas do grupo sobrevive?**

A resposta é sempre a mesma ordem lexicográfica de desempate, e ela é
determinística de ponta a ponta (rodar de novo dá o mesmo canônico):

1. **Licença de política mais aberta primeiro** (``LICENSE_RANK``). Não é gosto:
   manter a cópia Apache-2.0 em vez da LMSYS faz a linha continuar exportável.
   Este é o único critério com consequência jurídica; os demais só desempatam.
   Dentro de uma mesma política a ordem é editorial — e é lá que mora a
   exceção do ``cc0-1.0`` da plataforma, explicada na própria ``LICENSE_RANK``.
2. **Ordem das seções em ``config/sources.toml``** — que é a ordem editorial das
   fontes (português primeiro, WildChat antes de aya, etc.).
3. Linha **com** ``source_id`` antes de linha sem (rastreabilidade).
4. ``source_id`` e depois ``uid``, lexicográficos — o desempate final, que
   garante determinismo mesmo entre duas linhas idênticas da mesma fonte.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from functools import lru_cache
from typing import Any

from . import config
from .schema import License
from .textnorm import norm_for_hash

#: Menor = preferida como canônica. O que o número ordena é a **política**
#: (``commercial_ok``/``redistributable``), não a fama da licença: as SEIS
#: primeiras são todas ``(True, True)`` e formam uma classe de equivalência —
#: entre elas o export não vê diferença nenhuma, e a ordem é editorial.
#:
#: É dentro dessa classe que o ``cc0-1.0`` da plataforma fica por ÚLTIMO,
#: apesar de ser a licença mais permissiva que existe. O motivo não é jurídico,
#: é de proveniência: quando uma criação da Bancada colapsa com um prompt real,
#: o caso dominante não é coincidência — é alguém ter COLADO o texto do corpus
#: no formulário (foi exatamente assim que o P4 foi verificado). Se o cc0
#: ganhasse o desempate, aprovar essa criação substituiria a linha do aya (com
#: autor, citação e atribuição publicada) por uma linha cuja proveniência é o
#: nosso próprio demo — em silêncio, sem nada para sinalizar. O banco inteiro
#: existe para provar de onde cada linha veio; essa propriedade não pode ser
#: sobrescrita por um Ctrl+V.
#:
#: E não é a ordem das seções do ``sources.toml`` que resolve isto: o critério
#: 2 só roda quando o 1 empata, e com o cc0 sozinho na sua posição ele nunca
#: empata contra outra fonte. Quem decide é esta tabela.
LICENSE_RANK: dict[str, int] = {
    License.APACHE_2_0.value: 0,
    License.MIT.value: 1,
    License.CC_BY_4_0.value: 2,
    License.ODC_BY_1_0.value: 3,
    License.CC_BY_SA_3_0.value: 4,
    License.CC0_1_0.value: 5,
    License.CC_BY_NC_4_0.value: 6,
    License.LMSYS_1M.value: 7,
    License.UNKNOWN.value: 8,
}

# Licença nova em schema.py sem entrada aqui viraria "pior que unknown" em
# silêncio, e o dedup passaria a preferir a cópia errada. Falha no import.
_faltando = sorted({lic.value for lic in License} - set(LICENSE_RANK))
if _faltando:  # pragma: no cover - só dispara se alguém editar schema.License
    raise RuntimeError(
        f"dedup.LICENSE_RANK não cobre schema.License: faltando {_faltando}"
    )
del _faltando

#: Rank de licença desconhecida (pior que qualquer conhecida).
RANK_LICENCA_DESCONHECIDA: int = max(LICENSE_RANK.values()) + 1
#: Rank de fonte fora do sources.toml (fica por último, sem quebrar).
RANK_FONTE_DESCONHECIDA: int = 1 << 20


@lru_cache(maxsize=1)
def source_rank() -> dict[str, int]:
    """``{fonte: posição da seção em config/sources.toml}``."""
    return {nome: i for i, nome in enumerate(config.sources())}


def canonical_key(
    license_id: str | None,
    source: str | None,
    source_id: str | None,
    uid: str,
) -> tuple[int, int, int, str, str]:
    """Chave de ordenação do canônico (menor vence). Ver docstring do módulo."""
    ranks = source_rank()
    return (
        LICENSE_RANK.get(str(license_id or ""), RANK_LICENCA_DESCONHECIDA),
        ranks.get(str(source or ""), RANK_FONTE_DESCONHECIDA),
        0 if source_id else 1,
        str(source_id or ""),
        str(uid),
    )


def choose_canonical(rows: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    """A linha que sobrevive ao grupo. ``rows`` traz ``license/source/source_id/uid``."""
    if not rows:
        raise ValueError("choose_canonical: grupo vazio")
    return min(
        rows,
        key=lambda r: canonical_key(
            r.get("license"), r.get("source"), r.get("source_id"), r["uid"]
        ),
    )


class UnionFind:
    """Union-find com compressão de caminho e união por tamanho.

    Índices posicionais (0..n-1), que é como o s06 enxerga as linhas dentro de
    uma partição de idioma.
    """

    __slots__ = ("_pai", "_tam")

    def __init__(self, n: int) -> None:
        self._pai = list(range(n))
        self._tam = [1] * n

    def find(self, x: int) -> int:
        pai = self._pai
        raiz = x
        while pai[raiz] != raiz:
            raiz = pai[raiz]
        while pai[x] != raiz:  # compressão de caminho, iterativa
            pai[x], x = raiz, pai[x]
        return raiz

    def union(self, a: int, b: int) -> bool:
        """Une os dois conjuntos; devolve ``False`` se já estavam juntos."""
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        if self._tam[ra] < self._tam[rb]:
            ra, rb = rb, ra
        self._pai[rb] = ra
        self._tam[ra] += self._tam[rb]
        return True

    def size(self, x: int) -> int:
        return self._tam[self.find(x)]

    def groups(self) -> dict[int, list[int]]:
        """``{raiz: [índices]}`` — só os grupos com 2 ou mais elementos."""
        agrupado: dict[int, list[int]] = {}
        for i in range(len(self._pai)):
            agrupado.setdefault(self.find(i), []).append(i)
        return {r: m for r, m in agrupado.items() if len(m) > 1}


def tokens(text: str) -> frozenset[str]:
    """Conjunto de tokens da chave de comparação (mesma normalização do hash)."""
    return frozenset(norm_for_hash(text).split())


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    """Jaccard entre dois conjuntos de tokens.

    Bordas explícitas: dois vazios dão **1.0** (dois prompts só de pontuação são
    de fato a mesma coisa e devem colapsar); vazio contra não-vazio dá 0.0.
    """
    sa = a if isinstance(a, frozenset | set) else frozenset(a)
    sb = b if isinstance(b, frozenset | set) else frozenset(b)
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    if not inter:
        return 0.0
    return inter / len(sa | sb)


def razao_tamanho(a: int, b: int) -> float:
    """Razão entre dois tamanhos (>= 1.0). Zero contra zero é 1.0."""
    maior, menor = (a, b) if a >= b else (b, a)
    if menor <= 0:
        return 1.0 if maior <= 0 else float("inf")
    return maior / menor


__all__ = [
    "LICENSE_RANK",
    "RANK_FONTE_DESCONHECIDA",
    "RANK_LICENCA_DESCONHECIDA",
    "UnionFind",
    "canonical_key",
    "choose_canonical",
    "jaccard",
    "razao_tamanho",
    "source_rank",
    "tokens",
]
