"""Testes da casca da Bancada (P1) — o que dá para provar sem navegador.

O ``index.html`` é um arquivo só, sem build e sem rede. Isso é o que torna estes
testes possíveis: o artefato entregue É o fonte, então dá para afirmar coisas
sobre ele lendo o arquivo.

Três famílias:

1. **Zero referência externa.** A plataforma tem de abrir offline, hoje e daqui
   a dois anos. Um ``<script>`` de CDN que funcionou no dia da entrega vira tela
   branca no primeiro voo de avião.
2. **Nenhum HTML nasce de string.** Ao contrário da interface de curadoria, aqui
   não há sequer a exceção do destaque de busca: não existe ``innerHTML``,
   ponto. O corpus tem ``<script>`` de verdade dentro dos prompts, e esta app vai
   mostrar prompts do corpus a partir do P2.
3. **É outro produto.** Título distinto, identidade do brief (fundo quente,
   acento azul-ardósia, serifa no conteúdo) e as rotas que a tela chama existem
   de fato no backend.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from prompt_factory.annotate.main import STATIC, criar_app

from .test_api import montar_banco

INDEX = STATIC / "index.html"


@pytest.fixture(scope="module")
def html() -> str:
    return INDEX.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def js(html: str) -> str:
    """Só o corpo do ``<script>``.

    A prosa dos comentários fala de ``<script>`` (é o exemplo de XSS do corpus),
    então recortar por ``split("<script>")`` pegaria o texto errado. A tag de
    verdade é a única que ocupa uma linha inteira sozinha.
    """
    return re.split(r"^<script>$", html, maxsplit=1, flags=re.M)[1]


# ---------------------------------------------------------------------------
# 1. offline: zero rede
# ---------------------------------------------------------------------------

#: Padrões que denunciam dependência externa. ``src=`` está aqui porque pega
#: ``<script src>``, ``<img src>`` e ``<iframe src>`` de uma vez.
PROIBIDOS: tuple[str, ...] = (
    "http://",
    "https://",
    "//cdn",
    "cdn.",
    "unpkg",
    "jsdelivr",
    "cdnjs",
    "googleapis",
    "@import",
    "src=",
    "<link",
    "integrity=",
    "@font-face",
)


def test_zero_referencia_externa(html: str) -> None:
    achados = {p: html.lower().count(p) for p in PROIBIDOS}
    assert not any(achados.values()), f"referência externa no index.html: {achados}"


def test_um_script_e_um_style_sem_atributo(html: str) -> None:
    assert re.findall(r"<script\s[^>]*>", html) == []
    assert re.findall(r"<style\s[^>]*>", html) == []
    assert len(re.findall(r"^<script>$", html, re.M)) == 1
    assert len(re.findall(r"^<style>$", html, re.M)) == 1


def test_declara_utf8_e_viewport(html: str) -> None:
    assert '<meta charset="utf-8">' in html
    assert "viewport" in html
    # Acentuação de verdade no arquivo: se o encoding escorregar, isto quebra.
    assert "anotação" in html
    assert "diretrizes" in html.lower()


# ---------------------------------------------------------------------------
# 2. nenhum HTML nasce de string
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "proibido",
    ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"],
)
def test_nada_monta_html_por_string(js: str, proibido: str) -> None:
    """A regra desta app é ABSOLUTA — sem a exceção do ``snippetSeguro`` da
    curadoria. Uma regra sem exceção é mais fácil de manter do que uma com."""
    assert proibido not in js, f"{proibido!r} no JS: o corpus tem <script> de verdade"


def test_existe_o_construtor_de_elemento(js: str) -> None:
    """``el()`` é a única forma de criar nó aqui, e ela usa ``textContent``."""
    assert "function el(tag, cls, texto)" in js
    corpo = js.split("function el(tag, cls, texto)", 1)[1].split("\n}", 1)[0]
    assert "textContent" in corpo
    assert "innerHTML" not in corpo


def test_todo_texto_entra_por_textcontent(js: str) -> None:
    """Contagem grosseira, mas o que importa: só existe uma via de escrita."""
    assert js.count("textContent") >= 10


# ---------------------------------------------------------------------------
# 3. identidade e contrato
# ---------------------------------------------------------------------------


def test_titulo_distinto_da_ferramenta_de_curadoria(html: str) -> None:
    """As duas sobem juntas na demonstração, em abas vizinhas. Se as duas se
    chamassem "Prompt Factory", a aba errada seria clicada o tempo todo."""
    titulo = re.search(r"<title>(.*?)</title>", html, re.S)
    assert titulo is not None
    assert "Bancada" in titulo.group(1)
    assert "Prompt Factory" not in titulo.group(1)


def test_identidade_do_brief(html: str) -> None:
    """Claro por padrão, fundo quente, acento azul-ardósia, serifa no conteúdo."""
    assert "color-scheme: light" in html
    assert "#FCFBF9" in html          # fundo branco levemente quente
    assert "#1F4E79" in html          # o acento, e só ele
    assert "--fonte-leitura:" in html  # serifa de leitura, para prompt e resposta
    assert 'data-tema="escuro"' in html  # o escuro existe, mas não é o padrão
    assert "tabular-nums" in html


def test_semanticas_separadas_do_acento(html: str) -> None:
    """Aprovado, devolvido e pendente são outra família de cor de propósito: se
    fossem tons do acento, competiriam com o botão de ação primária."""
    for token in ("--aprovado:", "--devolvido:", "--pendente:"):
        assert token in html


def test_respeita_movimento_reduzido_e_mostra_o_foco(html: str) -> None:
    assert "prefers-reduced-motion: no-preference" in html
    assert ":focus-visible" in html
    assert "outline:2px solid var(--acento)" in html


def test_a_tela_de_entrada_tem_os_tres_papeis_e_o_rodape(html: str, js: str) -> None:
    for papel in ("anotador", "revisor", "admin"):
        assert f'id: "{papel}"' in js
    assert "Demonstração — sem login" in html
    assert 'id="papeis"' in html


def test_marca_o_que_ainda_nao_existe(js: str) -> None:
    """Nada de "em construção": o que não funciona é desabilitado e marcado com
    o marco em que chega."""
    assert '"marco"' in js
    assert "em construção" not in js.lower()
    for marco in ("P2", "P3", "P4", "P5", "P6", "P7"):
        assert f'"{marco}"' in js


def test_estados_vazios_tem_saida(js: str) -> None:
    assert "Nada na fila de comparação A/B." in js
    assert "Nada esperando revisão." in js
    assert "Escolher no catálogo" in js
    assert "Entrar como administrador" in js


@pytest.mark.parametrize("rota", ["/api/health", "/api/perfis"])
def test_so_chama_rotas_que_existem(js: str, rota: str) -> None:
    assert rota in js


def test_nao_chama_rota_que_o_p1_nao_tem(js: str) -> None:
    """A casca não pode disparar um contrato que ainda não existe: um 404 no
    primeiro paint é indistinguível, para quem olha, de uma app quebrada."""
    chamadas = set(re.findall(r'"(/api/[a-z0-9/_-]+)"', js))
    assert chamadas <= {"/api/health", "/api/perfis"}, chamadas


# ---------------------------------------------------------------------------
# 4. servido de verdade
# ---------------------------------------------------------------------------


def test_a_casca_e_servida_na_raiz(tmp_path: Path) -> None:
    corpus = tmp_path / "prompts.sqlite"
    montar_banco(corpus)
    with TestClient(criar_app(tmp_path / "annotate.sqlite", corpus)) as c:
        r = c.get("/")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/html")
        assert "Bancada" in r.text
        assert "Prompt Factory" not in r.text.split("</title>", 1)[0]
