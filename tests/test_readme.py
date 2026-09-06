"""O número de testes citado no README tem de ser o número de testes que existe.

Este arquivo existe por causa de um defeito concreto: o README anunciava **1.287
testes** num ponto e **1.273** em outro, o `CLAUDE.md` dizia 1.466, e a contagem
real era 1.467. Quatro números para o mesmo fato, todos escritos à mão em
momentos diferentes, nenhum errado no dia em que foi escrito.

Corrigir os números sem corrigir o processo os teria defasado de novo na semana
seguinte — foi o que aconteceu três vezes. Por isso o conserto é um teste que
**falha** quando o README diverge da coleta, e não uma edição.

Duas armadilhas moldaram o desenho:

* **São dois números legítimos.** A suíte inteira e a suíte sem os testes
  marcados ``slow`` diferem, e o CI roda justamente com o filtro. Um check que
  comparasse o README com ``len(session.items)`` passaria na máquina do dono e
  falharia no CI — ou pior, passaria nos dois medindo coisas diferentes. Daí o
  hook ``pytest_deselected`` do ``conftest``: o total é o que foi selecionado
  mais o que o marcador tirou.
* **Rodar um arquivo só não pode reprovar o README.** ``pytest
  tests/test_readme.py`` coleta um punhado de itens, e cobrar 1.467 deles seria
  um falso vermelho que ensina a ignorar o teste. Quando a coleta é parcial o
  teste **pula dizendo isso**, em vez de passar calado — pular por coleta parcial
  e passar por concordância são fatos diferentes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.conftest import DESELECIONADOS

README = Path(__file__).resolve().parents[1] / "README.md"

# Qualquer "N testes" no README é uma afirmação sobre o tamanho da suíte, e todas
# têm de concordar entre si e com a coleta. O separador de milhar é o ponto, que
# é a convenção do documento (ele é escrito em pt-BR).
PADRAO = re.compile(r"(\d[\d.]*)\s+testes\b")


def _numeros_do_readme() -> list[tuple[str, int]]:
    texto = README.read_text(encoding="utf-8")
    return [(m.group(1), int(m.group(1).replace(".", ""))) for m in PADRAO.finditer(texto)]


def test_o_readme_cita_o_tamanho_da_suite(request: pytest.FixtureRequest) -> None:
    selecionados = list(request.session.items)
    total = len(selecionados) + len(DESELECIONADOS)

    # Coleta parcial: comparar contra o total da suíte não faria sentido. A
    # medida é por ARQUIVO, não por item — um `-k` que sobrasse dois testes de
    # cada arquivo ainda é uma coleta que enxergou a suíte toda.
    no_disco = {p.name for p in README.parent.joinpath("tests").glob("test_*.py")}
    vistos = {Path(str(item.path)).name for item in selecionados + DESELECIONADOS}
    if not no_disco <= vistos:
        pytest.skip(
            f"coleta parcial ({len(vistos)} de {len(no_disco)} arquivos de teste) — "
            "o número do README só é conferível sobre a suíte inteira"
        )

    citados = _numeros_do_readme()
    assert citados, (
        f"{README.name} não cita o tamanho da suíte. O número é uma promessa ao "
        "leitor e este teste é quem a sustenta — escreva 'N testes' na seção de "
        "instalação e na árvore do repositório."
    )

    divergentes = [t for t, n in citados if n != total]
    assert not divergentes, (
        f"{README.name} diz {', '.join(sorted(set(divergentes)))} onde a coleta "
        f"encontra {total:,} testes".replace(",", ".") + ". "
        "Atualize o README (e a data da medição ao lado do número). Este teste "
        "existe porque corrigir o número sem corrigir o processo já defasou o "
        "documento três vezes."
    )


def test_todas_as_citacoes_do_readme_concordam_entre_si() -> None:
    """Duas afirmações diferentes sobre o mesmo fato, no mesmo documento.

    Separado do teste acima de propósito: este vale mesmo numa coleta parcial,
    porque não depende da suíte — só de o documento não se contradizer. Foi
    exatamente esta a forma do defeito original (1.287 na seção de instalação,
    1.273 na árvore do repositório).
    """
    citados = _numeros_do_readme()
    distintos = {n for _, n in citados}
    assert len(distintos) <= 1, (
        f"{README.name} cita {len(distintos)} números diferentes de testes: "
        f"{sorted(distintos)}. Um documento que se contradiz sobre um número "
        "verificável faz o leitor duvidar dos números que ele não pode verificar."
    )
