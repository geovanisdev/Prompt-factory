"""Testes da interface (M9, parte B) — o que dá para provar sem navegador.

O `index.html` é um arquivo só, sem build e sem rede. Isso é o que torna estes
testes possíveis: o artefato entregue É o fonte, então dá para afirmar coisas
sobre ele lendo o arquivo.

Três famílias, em ordem de importância:

1. **Nenhuma referência a host externo.** A interface tem de abrir offline, numa
   máquina só, hoje e daqui a dois anos. Um ``<script src=…>`` de CDN que
   funcionou no dia da entrega transforma a ferramenta em tela branca no
   primeiro voo de avião.
2. **Escape antes de injetar HTML.** O corpus tem ``<script>`` de verdade dentro
   dos prompts, e o backend devolve o snippet do FTS com ``<mark>`` cru no JSON.
   A única função autorizada a produzir HTML a partir de conteúdo é
   ``snippetSeguro``.
3. **O contrato não escorregou.** Os endpoints que a tela chama existem, a
   taxonomia não foi copiada para dentro do HTML, e a qualidade vai de 1 a 3 —
   não de 1 a 5.

E mais uma, que é regressão de um bug medido: a conexão por request nasce numa
worker thread do AnyIO e é fechada em outra, e isso **tem** de funcionar.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from prompt_factory import db as dbmod
from prompt_factory import schema
from prompt_factory.app import presenters
from prompt_factory.app.main import STATIC, criar_app
from prompt_factory.app.models import Filtros

from .test_api import montar_banco  # o mesmo banco sintético hostil da API

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

#: Padrões que denunciam uma dependência externa. ``src=`` está aqui porque
#: pega ``<script src>``, ``<img src>`` e ``<iframe src>`` de uma vez — a
#: interface não carrega NADA de lugar nenhum.
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
)


def test_zero_referencia_externa(html: str) -> None:
    """A prova do 'sem CDN' do briefing, escrita como teste."""
    achados = {p: html.lower().count(p) for p in PROIBIDOS}
    assert not any(achados.values()), f"referência externa no index.html: {achados}"


def test_nada_de_script_ou_estilo_externo(html: str) -> None:
    """Nenhum ``<script>``/``<style>`` com atributo: nada de src, type nem integrity."""
    assert re.findall(r"<script\s[^>]*>", html) == []
    assert re.findall(r"<style\s[^>]*>", html) == []
    # E exatamente uma tag de cada, cada uma sozinha na sua linha.
    assert len(re.findall(r"^<script>$", html, re.M)) == 1
    assert len(re.findall(r"^<style>$", html, re.M)) == 1


def test_declara_utf8_e_viewport(html: str) -> None:
    assert '<meta charset="utf-8">' in html
    assert "viewport" in html
    # Acentuação de verdade no arquivo: se o encoding escorregar, isto quebra.
    assert "coleção" in html
    assert "rotulagem pendente" in html


# ---------------------------------------------------------------------------
# 2. XSS: escape antes de injetar
# ---------------------------------------------------------------------------


def test_existe_um_escapador_completo(html: str) -> None:
    """`esc()` cobre os cinco caracteres que quebram HTML."""
    assert 'const MAPA_ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", ' in html
    assert "'\": \"&#39;\"" in html or "&#39;" in html
    assert "function esc(" in html


def test_snippet_e_o_unico_html_vindo_do_corpus(html: str) -> None:
    """`snippetSeguro` escapa tudo e reabre SÓ as marcas do FTS5."""
    assert "function snippetSeguro(" in html
    corpo = html.split("function snippetSeguro(", 1)[1].split("\n}", 1)[0]
    assert "esc(bruto)" in corpo, "o snippet tem de ser escapado ANTES de qualquer coisa"
    # As únicas duas tags reabertas.
    reabertas = re.findall(r'join\("(<[^"]+>)"\)', corpo)
    assert sorted(reabertas) == ["</mark>", "<mark>"], reabertas


def test_o_campo_snippet_nunca_vai_cru_para_o_html(html: str) -> None:
    """Toda leitura de ``.snippet`` é teste booleano ou passa por snippetSeguro."""
    for linha in html.splitlines():
        if ".snippet" not in linha or "snippet:" in linha:
            continue
        limpa = linha.strip()
        assert "snippetSeguro(" in limpa or limpa.startswith("if ") or "&&" in limpa, (
            f"snippet cru numa linha de HTML: {limpa!r}"
        )


def test_texto_do_prompt_sempre_escapado(html: str) -> None:
    """As três formas de mostrar o texto de um item usam ``esc()``."""
    for trecho in ("esc(trecho)", "esc(inteiro.slice(", "esc(it.text.slice("):
        assert trecho in html, trecho
    # O editor recebe o texto por `value`, nunca por innerHTML.
    assert "ta.value = it.text;" in html


# ---------------------------------------------------------------------------
# 3. âncoras da tela
# ---------------------------------------------------------------------------

#: Os elementos que a interface promete ter. Se um sumir, alguma seção inteira
#: sumiu junto — e é isso que o teste pega.
ANCORAS: tuple[str, ...] = (
    "topo", "marca", "form-busca", "busca", "ordem", "resumo", "total", "sel-topo",
    "btn-revisao", "btn-tema", "btn-atalhos",
    "grade", "facetas", "centro", "barra", "chips", "lista", "paginacao",
    "tamanho-pagina", "btn-marcar-pagina",
    "mesa", "contador-sel", "btn-limpar-sel", "btn-sel-colecao", "btn-sel-fora",
    "colecao-ativa", "btn-nova-colecao", "btn-renomear-colecao", "btn-apagar-colecao",
    "filtrar-colecao", "btn-filtro-mais", "btn-filtro-menos",
    "btn-export", "hist", "banco",
    "painel-item", "item-titulo", "item-selos", "item-corpo", "item-acoes",
    "painel-atalhos", "painel-export", "export-corpo", "export-acoes",
    "revisao", "rev-posicao", "rev-corpo", "recado", "carregando",
)


def test_todas_as_ancoras_existem(html: str) -> None:
    faltando = [a for a in ANCORAS if f'id="{a}"' not in html]
    assert not faltando, f"âncoras sumidas do index.html: {faltando}"


def test_as_quatro_classes_de_licenca_tem_cor(html: str) -> None:
    """A licença é sinal visual de primeira classe, não rodapé de card."""
    for classe in (
        presenters.LICENCA_LIVRE, presenters.LICENCA_VIRAL,
        presenters.LICENCA_NAO_COMERCIAL, presenters.LICENCA_BLOQUEADA,
    ):
        assert f".lic-{classe}{{" in html.replace(" ", ""), classe
        assert f".b-{classe}{{" in html.replace(" ", ""), classe


# ---------------------------------------------------------------------------
# 4. teclado
# ---------------------------------------------------------------------------

#: O contrato de teclado do briefing. A folha de atalhos (`?`) documenta TODOS.
TECLAS: tuple[tuple[str, str], ...] = (
    ("j", "próximo"), ("k", "anterior"), ("espaço", "marca"),
    ("x", "texto completo"), ("e", "editar"), ("c", "coleção"),
    ("/", "busca"), ("Esc", "fecha"), ("?", "folha"),
)


def test_a_folha_de_atalhos_documenta_o_contrato(html: str) -> None:
    folha = html.split('id="painel-atalhos"', 1)[1].split("</div>\n</div>", 1)[0]
    for tecla, _ in TECLAS:
        assert f"<kbd>{tecla}</kbd>" in folha, f"tecla {tecla!r} não está na folha"
    assert "<kbd>Shift</kbd>+<kbd>J</kbd>" in folha


def test_qualidade_vai_de_1_a_3(html: str) -> None:
    """`schema.QUALITY_VALUES` é (1, 2, 3) — a tela não pode oferecer 4 nem 5."""
    assert schema.QUALITY_VALUES == (1, 2, 3)
    folha = html.split('id="painel-atalhos"', 1)[1].split("</div>\n</div>", 1)[0]
    for v in schema.QUALITY_VALUES:
        assert f"<kbd>{v}</kbd>" in folha
    for v in (4, 5):
        assert f"<kbd>{v}</kbd>" not in folha
    # Os botões da fila de revisão: exatamente 1, 2 e 3, e nada além.
    graus = re.search(r'\[\[1, "1 ruim"\], \[2, "2 usável"\], \[3, "3 bom"\]\]', html)
    assert graus, "a fila de revisão tem de oferecer 1..3"
    # O atalho de teclado da fila também: só 1, 2 e 3.
    atalho = re.search(r'e\.key === "1" \|\| e\.key === "2" \|\| e\.key === "3"', html)
    assert atalho, "o atalho de qualidade da fila tem de cobrir 1..3 e só"
    # E o seletor de qualidade do editor.
    assert '["", "1", "2", "3"].map(' in html


def test_teclado_le_shiftKey_e_nao_a_caixa_da_tecla(html: str) -> None:
    """Regressão: Shift+J chega como key='J' num teclado e como key='j' em outro."""
    assert "if (e.shiftKey) estenderSelecao(passo);" in html
    assert 'e.code === "Space"' in html


# ---------------------------------------------------------------------------
# 5. contrato com o backend
# ---------------------------------------------------------------------------

#: Tudo que a tela chama. Cada linha é ``(método, template da rota)``.
ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("GET", "/api/health"),
    ("GET", "/api/stats"),
    ("GET", "/api/prompts"),
    ("GET", "/api/prompts/{uid}"),
    ("PATCH", "/api/prompts/{uid}"),
    ("POST", "/api/prompts/{uid}/revert"),
    ("GET", "/api/facets"),
    ("GET", "/api/collections"),
    ("POST", "/api/collections"),
    ("PATCH", "/api/collections/{colecao_id}"),
    ("DELETE", "/api/collections/{colecao_id}"),
    ("POST", "/api/collections/{colecao_id}/items"),
    ("POST", "/api/collections/{colecao_id}/items/remove"),
    ("POST", "/api/collections/{colecao_id}/items/from-filter"),
    ("POST", "/api/collections/{colecao_id}/items/remove-from-filter"),
    ("POST", "/api/export"),
    ("GET", "/api/exports"),
    ("GET", "/api/exports/{export_id}/download"),
)


def test_todo_endpoint_que_a_tela_chama_existe(tmp_path: Path) -> None:
    """Confere contra o OpenAPI, que é o contrato publicado em ``/docs``.

    Andar por ``app.routes`` não serve: esta versão do FastAPI guarda os routers
    incluídos em wrappers (``_IncludedRouter``) em vez de achatar as rotas, e o
    teste passaria vendo só a ``/api/health``.
    """
    montar_banco(tmp_path / "p.sqlite")
    app = criar_app(tmp_path / "p.sqlite", tmp_path / "exports")
    esquema = app.openapi()["paths"]
    reais = {
        (metodo.upper(), caminho)
        for caminho, ops in esquema.items()
        for metodo in ops
    }
    faltando = [e for e in ENDPOINTS if e not in reais]
    assert not faltando, f"a interface chama rotas que não existem: {faltando}"


def test_a_tela_menciona_cada_endpoint(html: str) -> None:
    """O inverso: o HTML tem de conter, literalmente, cada caminho que usa."""
    for _, template in ENDPOINTS:
        prefixo = template.split("{", 1)[0].rstrip("/") or "/api"
        assert prefixo in html, f"{template} não aparece no index.html"
    assert "items/from-filter" in html, "o from-filter é o motivo da ferramenta existir"
    assert "items/remove-from-filter" in html


def test_os_nomes_de_filtro_da_tela_existem_no_modelo(html: str) -> None:
    """``extra='forbid'``: um campo inventado devolveria 422 e a tela ficaria vazia."""
    mapa = html.split("const ROT_CAMPO = {", 1)[1].split("};", 1)[0]
    campos = set(re.findall(r"(\w+):", mapa))
    validos = set(Filtros.model_fields)
    assert campos <= validos, f"campos que não existem em Filtros: {campos - validos}"


def test_a_taxonomia_nao_foi_copiada_para_a_tela(js: str) -> None:
    """As 32 classes vêm de /api/stats, que as lê do taxonomy.json.

    Uma segunda cópia divergiria da primeira, e a que diverge é sempre a da tela.
    """
    # A chave como LITERAL de string, e só dentro do JS: "resumo", "outro" e
    # "saude" também são palavras normais em português e aparecem no cromo
    # (`id="resumo"`), o que daria falso positivo se a busca fosse no HTML todo.
    copiadas = [
        k for k in schema.TASK_TYPES + schema.DOMAINS if f'"{k}"' in js or f"'{k}'" in js
    ]
    assert not copiadas, f"taxonomia hardcoded no index.html: {copiadas}"
    assert "est.stats.taxonomia" in js
    # Idem para os rótulos legíveis: quem os resolve é routes_prompts._rotulo.
    assert "Geração criativa" not in js


# ---------------------------------------------------------------------------
# 6. a página é servida de verdade
# ---------------------------------------------------------------------------


def test_a_interface_e_servida_na_raiz(tmp_path: Path) -> None:
    montar_banco(tmp_path / "p.sqlite")
    with TestClient(criar_app(tmp_path / "p.sqlite", tmp_path / "exports")) as c:
        r = c.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        # É a interface, não o marcador do backend.
        assert "PLACEHOLDER" not in r.text
        assert 'id="facetas"' in r.text and 'id="lista"' in r.text
        # E o mount do StaticFiles continua DEPOIS dos routers.
        assert c.get("/api/health").json()["status"] == "ok"


# ---------------------------------------------------------------------------
# 7. regressão do bug de thread (o que impedia a tela de abrir)
# ---------------------------------------------------------------------------


def test_conexao_do_request_atravessa_threads(tmp_path: Path) -> None:
    """A dependência-gerador é aberta, usada e fechada em threads DIFERENTES.

    Não é hipótese: o FastAPI resolve um gerador síncrono com
    ``contextmanager_in_threadpool``, e o ``__enter__``, o corpo da rota e o
    ``__exit__`` são três ``anyio.to_thread.run_sync`` independentes, sem
    afinidade de thread. Com ``check_same_thread=True`` isto levantava
    ``ProgrammingError`` e derrubava a interface no primeiro paint (que dispara
    três requisições de uma vez).
    """
    montar_banco(tmp_path / "p.sqlite")
    caixa: dict[str, Any] = {}

    def abrir() -> None:
        caixa["conn"] = dbmod.connect(tmp_path / "p.sqlite", check_same_thread=False)

    def consultar() -> None:
        caixa["n"] = caixa["conn"].execute("SELECT count(*) AS n FROM prompts").fetchone()["n"]

    def fechar() -> None:
        caixa["conn"].close()

    for passo in (abrir, consultar, fechar):
        t = threading.Thread(target=passo)
        t.start()
        t.join()
        assert "erro" not in caixa

    assert caixa["n"] > 0


def test_check_same_thread_ligado_ainda_quebraria(tmp_path: Path) -> None:
    """O contraprova: é o parâmetro que resolve, não sorte de agendamento."""
    montar_banco(tmp_path / "p.sqlite")
    conn = dbmod.connect(tmp_path / "p.sqlite")  # padrão: check_same_thread=True
    erro: list[BaseException] = []

    def fechar() -> None:
        try:
            conn.close()
        except BaseException as exc:
            erro.append(exc)

    t = threading.Thread(target=fechar)
    t.start()
    t.join()
    assert erro and isinstance(erro[0], sqlite3.ProgrammingError)
    conn.close()


def test_muitas_requisicoes_em_voo_nao_dao_500(tmp_path: Path) -> None:
    """O cenário do navegador: várias rotas do banco disparadas juntas."""
    import concurrent.futures as cf

    montar_banco(tmp_path / "p.sqlite")
    rotas = [
        "/api/health", "/api/stats", "/api/collections",
        "/api/prompts?page_size=5", "/api/facets", "/api/prompts?q=cora%C3%A7%C3%A3o",
    ] * 4
    with TestClient(criar_app(tmp_path / "p.sqlite", tmp_path / "exports")) as c:
        with cf.ThreadPoolExecutor(max_workers=8) as ex:
            codigos = list(ex.map(lambda r: c.get(r).status_code, rotas))
    assert set(codigos) == {200}, codigos
