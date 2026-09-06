"""O número de testes citado nos READMEs tem de ser o número de testes que existe.

Este arquivo existe por causa de um defeito concreto: o README anunciava **1.287
testes** num ponto e **1.273** em outro, o `CLAUDE.md` dizia 1.466, e a contagem
real era 1.467. Quatro números para o mesmo fato, todos escritos à mão em
momentos diferentes, nenhum errado no dia em que foi escrito.

Corrigir os números sem corrigir o processo os teria defasado de novo na semana
seguinte — foi o que aconteceu três vezes (e uma quarta, pega por este teste no
próprio sprint que o criou). Por isso o conserto é um teste que **falha** quando
o README diverge da coleta, e não uma edição.

Desde a Camada 1 são DOIS arquivos, em duas línguas: ``README.md`` (inglês, o
primário) e ``README.pt-BR.md``. Cada um escreve o número na sua convenção —
``1,470 tests`` e ``1.470 testes`` — e os dois têm de concordar entre si e com a
coleta. Um README traduzido que envelhece sozinho é o mesmo defeito com sotaque.

Três armadilhas moldaram o desenho:

* **São dois números legítimos.** A suíte inteira e a suíte sem os testes
  marcados ``slow`` diferem, e o CI roda justamente com o filtro. Um check que
  comparasse o README com ``len(session.items)`` passaria na máquina do dono e
  falharia no CI — ou pior, passaria nos dois medindo coisas diferentes. Daí o
  hook ``pytest_deselected`` do ``conftest``: o total é o que foi selecionado
  mais o que o marcador tirou.
* **Rodar um arquivo só não pode reprovar o README.** ``pytest
  tests/test_readme.py`` coleta um punhado de itens, e cobrar 1.470 deles seria
  um falso vermelho que ensina a ignorar o teste. Quando a coleta é parcial o
  teste **pula dizendo isso**, em vez de passar calado — pular por coleta parcial
  e passar por concordância são fatos diferentes.
* **O separador de milhar é da língua.** ``1,470`` e ``1.470`` são o mesmo
  número; o padrão aceita os dois e os descarta antes de comparar.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.conftest import DESELECIONADOS

RAIZ = Path(__file__).resolve().parents[1]
READMES = ("README.md", "README.pt-BR.md")

# "N tests" (en) ou "N testes" (pt), com ponto ou vírgula de milhar. Qualquer
# ocorrência é uma afirmação sobre o tamanho da suíte, e todas têm de concordar.
# Espaço HORIZONTAL entre o número e a palavra, nunca `\s`: `\s` atravessa a
# quebra de linha, e na árvore do repositório a linha `run_pipeline.ps1` termina
# em dígito e a seguinte começa com `tests/` — o padrão lia "1 tests" e reprovava
# os dois READMEs por uma citação que não existe.
PADRAO = re.compile(r"(\d[\d.,]*)[ \t]+test(?:es|s)\b")


def _citacoes() -> list[tuple[str, str, int]]:
    """(arquivo, texto citado, valor) para toda citação nos dois READMEs."""
    saida = []
    for nome in READMES:
        texto = (RAIZ / nome).read_text(encoding="utf-8")
        for m in PADRAO.finditer(texto):
            saida.append((nome, m.group(1), int(re.sub(r"[.,]", "", m.group(1)))))
    return saida


def test_os_dois_readmes_existem() -> None:
    """O inglês é o primário e o português é mantido em paralelo (R9)."""
    faltando = [n for n in READMES if not (RAIZ / n).is_file()]
    assert not faltando, f"faltam {faltando}: o README em inglês e o pt-BR andam juntos"


def test_o_readme_cita_o_tamanho_da_suite(request: pytest.FixtureRequest) -> None:
    selecionados = list(request.session.items)
    total = len(selecionados) + len(DESELECIONADOS)

    # Coleta parcial: comparar contra o total da suíte não faria sentido. A
    # medida é por ARQUIVO, não por item — um `-k` que sobrasse dois testes de
    # cada arquivo ainda é uma coleta que enxergou a suíte toda.
    no_disco = {p.name for p in (RAIZ / "tests").glob("test_*.py")}
    vistos = {Path(str(item.path)).name for item in selecionados + DESELECIONADOS}
    if not no_disco <= vistos:
        pytest.skip(
            f"coleta parcial ({len(vistos)} de {len(no_disco)} arquivos de teste) — "
            "o número do README só é conferível sobre a suíte inteira"
        )

    citados = _citacoes()
    for nome in READMES:
        assert any(n == nome for n, _, _ in citados), (
            f"{nome} não cita o tamanho da suíte. O número é uma promessa ao leitor e "
            "este teste é quem a sustenta — escreva 'N tests'/'N testes' na seção de "
            "instalação e na árvore do repositório."
        )

    divergentes = sorted({f"{n}: {t}" for n, t, v in citados if v != total})
    assert not divergentes, (
        f"os READMEs dizem {', '.join(divergentes)} onde a coleta encontra {total} testes. "
        "Atualize os DOIS arquivos (e a data da medição ao lado do número). Este teste "
        "existe porque corrigir o número sem corrigir o processo já defasou o documento "
        "quatro vezes."
    )


def test_todas_as_citacoes_dos_readmes_concordam_entre_si() -> None:
    """Duas afirmações diferentes sobre o mesmo fato, no mesmo par de documentos.

    Separado do teste acima de propósito: este vale mesmo numa coleta parcial,
    porque não depende da suíte — só de os documentos não se contradizerem, nem
    um ao outro. Foi exatamente esta a forma do defeito original (1.287 na seção
    de instalação, 1.273 na árvore do repositório).
    """
    citados = _citacoes()
    distintos = {v for _, _, v in citados}
    assert len(distintos) <= 1, (
        f"os READMEs citam {len(distintos)} números diferentes de testes: "
        f"{sorted(distintos)}. Um documento que se contradiz sobre um número "
        "verificável faz o leitor duvidar dos números que ele não pode verificar."
    )
