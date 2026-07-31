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


def test_o_seletor_de_papel_esta_na_barra(html: str, js: str) -> None:
    """P3a: os três papéis viraram um controle da BARRA, não uma tela de entrada.

    A tela de três cards custava os primeiros 15 segundos de quem abre o
    portfólio — e esses 15 segundos são os únicos garantidos. A honestidade de
    "sem login" continua à vista, agora na própria barra.
    """
    for papel in ("anotador", "revisor", "admin"):
        assert f'id: "{papel}"' in js
    assert "Demonstração — sem login" in html
    assert 'id="papeis"' in html
    # A app abre direto: nada de um `<div id="entrada">` escondendo o produto.
    assert 'id="entrada"' not in html
    assert '<div id="app">' in html


def test_marca_o_que_ainda_nao_existe(js: str) -> None:
    """Nada de "em construção": o que não funciona é desabilitado e marcado com
    o marco em que chega. Com o P3a entregue, "P3" sai da lista e entra "P3b"."""
    assert '"marco"' in js
    assert "em construção" not in js.lower()
    for marco in ("P3b", "P4", "P5", "P6", "P7"):
        assert f'"{marco}"' in js
    roteiro = js.split("const ROTEIRO = [", 1)[1].split("];", 1)[0]
    assert '"P3"' not in roteiro


def test_o_p2_saiu_do_roteiro_do_que_falta(js: str) -> None:
    """O roteiro do admin lista o que AINDA NÃO existe. Com o P2 entregue, ele
    não pode continuar prometendo a fila e o catálogo como futuros — um roteiro
    que lista o que já está na tela é pior que roteiro nenhum."""
    roteiro = js.split("const ROTEIRO = [", 1)[1].split("];", 1)[0]
    assert '"P2"' not in roteiro


def test_estados_vazios_tem_saida(js: str) -> None:
    assert "Nada na fila de comparação A/B." in js
    assert "Nada esperando revisão." in js
    assert "Escolher no catálogo" in js
    assert "Entrar como administrador" in js
    # P2: o vazio do catálogo e o de "minhas" também bifurcam.
    assert "Limpar os filtros" in js
    assert "Voltar para a fila" in js
    assert "Ir para a fila" in js


#: As rotas que a tela chama. Um caminho construído por concatenação
#: (``"/api/atribuicoes/" + id + "/submeter"``) aparece aqui pelo PREFIXO, que é
#: o pedaço que continua sendo literal no código.
ROTAS_DO_FRONT: frozenset[str] = frozenset(
    {
        "/api/health",
        "/api/perfis",
        "/api/diretrizes",
        "/api/projetos",
        "/api/tarefas/proxima",
        "/api/tarefas/livre",
        "/api/tarefas/contagens",
        "/api/catalogo",
        "/api/atribuicoes",
        "/api/atribuicoes/",
        "/api/revisao/fila",
        "/api/revisao/",
    }
)


@pytest.mark.parametrize("rota", sorted(ROTAS_DO_FRONT))
def test_so_chama_rotas_que_existem(js: str, rota: str) -> None:
    assert rota in js


def test_nao_chama_rota_que_o_backend_nao_tem(js: str) -> None:
    """A tela não pode disparar um contrato que ainda não existe: um 404 no
    primeiro paint é indistinguível, para quem olha, de uma app quebrada.

    Esta lista **tem de crescer junto com as rotas** — é o único lugar onde o
    contrato entre os dois lados está escrito de uma vez só.
    """
    chamadas = set(re.findall(r'"(/api/[a-z0-9/_-]+)"', js))
    assert chamadas <= ROTAS_DO_FRONT, chamadas - ROTAS_DO_FRONT


def test_as_rotas_do_front_existem_no_backend(tmp_path: Path) -> None:
    """O simétrico do teste acima: a lista branca não pode virar ficção.

    Confere contra o roteador REAL da app, e não contra um segundo inventário
    escrito à mão — que seria mais uma coisa a manter em dia.
    """
    corpus = tmp_path / "prompts.sqlite"
    montar_banco(corpus)
    app = criar_app(tmp_path / "annotate.sqlite", corpus)
    # Pelo OpenAPI, e não por ``app.routes``: as versões recentes do FastAPI
    # guardam os routers incluídos dentro de um invólucro, e iterar ``routes``
    # devolveria só as rotas de documentação. O ``/openapi.json`` É o contrato.
    caminhos = set(app.openapi()["paths"])
    for rota in ROTAS_DO_FRONT:
        if rota.endswith("/"):
            # Prefixo de caminho construído: basta existir alguma rota sob ele.
            assert any(c.startswith(rota) for c in caminhos), rota
        else:
            assert rota in caminhos, rota


# ---------------------------------------------------------------------------
# 3b. os workspaces do P2, e as regras de carga cognitiva que eles carregam
# ---------------------------------------------------------------------------


def test_uma_acao_primaria_com_atalho(js: str) -> None:
    """"Enviar e próxima" + Ctrl+Enter. Uma ação primária por tela."""
    assert '"Enviar e próxima"' in js
    assert "Ctrl+Enter" in js
    assert 'ev.key === "Enter"' in js


def test_o_botao_diz_o_que_falta_antes_do_clique(js: str) -> None:
    """Validação que só existe depois do clique é fricção pura."""
    assert "function oQueFalta()" in js
    assert "critérios sem nota" in js
    assert "Escolha A, B ou empate" in js
    assert "caracteres na justificativa" in js


def test_os_limites_vem_do_servidor(js: str) -> None:
    """Uma cópia dos limiares no JS divergiria do settings.toml na primeira vez
    que alguém o ajustasse — e a tela passaria a prometer o que o 422 desmente."""
    assert "function lim(chave, padrao)" in js
    assert "est.saude.limites" in js
    for chave in ("min_chars_justificativa", "min_criterios_rubrica", "max_criterios_rubrica"):
        assert f'"{chave}"' in js


def test_rascunho_automatico_por_atribuicao(js: str) -> None:
    """Perder trabalho é a maior fricção que existe."""
    assert "function rascunhoChave(id)" in js
    assert 'CHAVE + "rascunho."' in js
    assert "rascunhoRecuperado" in js


def test_nenhum_cronometro_para_quem_anota(js: str) -> None:
    """O tempo é medido e mandado ao servidor — e nunca mostrado ao anotador.

    Cronômetro à vista adiciona pressão sem melhorar decisão, e aqui não há
    pagamento por hora que o justifique.
    """
    assert "tempo_ativo_ms" in js
    for proibido in ("setInterval", "restante", "cronometro", "tempo restante"):
        assert proibido not in js.lower(), proibido


def test_os_quatro_workspaces_existem(js: str) -> None:
    for fn in ("function wsAvaliar(", "function wsEscrever(", "function wsSft(", "function wsComparar("):
        assert fn in js


def test_tres_modelos_de_partida_colapsados(js: str) -> None:
    """Matar a página em branco é a maior redução de fricção da aba de rubrica."""
    assert "const MODELOS_RUBRICA" in js
    assert js.count('nome: "') >= 3
    modelos = js.split("const MODELOS_RUBRICA = [", 1)[1].split("\n];", 1)[0]
    assert modelos.count("titulo:") == 3


def test_ab_tem_rolagem_sincronizada_e_atalhos(js: str) -> None:
    assert "function sincronizarRolagem(" in js
    assert 'ev.key === "ArrowLeft"' in js
    assert 'ev.key === "ArrowRight"' in js
    assert 'ev.key === "e" || ev.key === "E"' in js


def test_o_gabarito_nao_e_sequer_nomeado_na_tela(js: str) -> None:
    """A tela do anotador não conhece o conceito — nem para escondê-lo."""
    assert "gabarito" not in js.lower()
    assert "defeito_plantado" not in js


# ---------------------------------------------------------------------------
# 3c. a triagem do P3a (passagem 1 da revisão)
# ---------------------------------------------------------------------------


def test_as_diretrizes_vem_do_servidor_com_versao(js: str) -> None:
    """P3a: a regra sob a qual se anota deixou de ser literal no HTML.

    O cenário que isto resolve: a regra do A/B é ajustada na terça, o cliente
    reclama de inconsistência na quinta — sem a versão gravada por anotação, a
    única saída é refazer o lote inteiro.
    """
    assert "function carregarDiretrizes()" in js
    assert '"/api/diretrizes"' in js
    assert "est.diretrizes[tp.id]" in js
    assert "diretrizes.titulo_versao" in js
    # E o texto NÃO volta a morar no JS: os TIPOS não carregam mais `diretrizes`.
    tipos = js.split("const TIPOS = [", 1)[1].split("\n];", 1)[0]
    assert "diretrizes:" not in tipos


def test_a_triagem_aprova_devolve_e_anda_pelo_teclado(js: str) -> None:
    """`A` aprova, `R` devolve, `J`/`K` andam — o teclado é o caminho principal."""
    assert "function tecladoRevisor(" in js
    assert 'ev.key === "a" || ev.key === "A"' in js
    assert 'ev.key === "r" || ev.key === "R"' in js
    assert 'ev.key === "j" || ev.key === "J"' in js
    assert "function triar(veredito, comentario)" in js


def test_devolver_pede_comentario_com_foco(js: str) -> None:
    """A microcopy diz PARA QUEM o comentário vai — é isso que muda o que o
    revisor escreve. E o campo recebe foco: quem apertou R já decidiu devolver."""
    assert "O comentário volta para o anotador — diga o que corrigir." in js
    assert "min_chars_devolucao" in js
    assert "area.focus()" in js


def test_o_revisor_ve_no_mesmo_layout_em_que_foi_produzido(js: str) -> None:
    """RECONHECIMENTO, não releitura: os MESMOS `ws*`, em modo leitura.

    Um segundo desenho "de revisão" seria um segundo lugar onde a rubrica pode
    divergir de si mesma — e a divergência apareceria justamente no item que o
    revisor está julgando.
    """
    assert "function ctxLeitura(form)" in js
    assert "desenharWorkspace(env, ctxLeitura(tri.form))" in js
    assert "function formDoPayload(env, anterior)" in js
    # E o mesmo mapa de payload → formulário serve aos dois lados.
    assert "return formDoPayload(env, env.versao_anterior && env.versao_anterior.payload)" in js


def test_o_revisor_e_avisado_das_proprias_anotacoes_excluidas(js: str) -> None:
    """Fila vazia sem explicação faria um revisor que anotou achar que a
    plataforma perdeu o trabalho dele."""
    assert "minhas_excluidas" in js
    assert "quem revisa nunca é quem anotou" in js


def test_o_modo_solo_se_declara_na_tela(js: str) -> None:
    """A regra de QC afrouxada precisa APARECER, e a faixa não se fecha.

    Um aviso dispensável vira um aviso que ninguém viu na hora que importava —
    e esconder que o revisor é o autor seria mentir sobre o QC, que é
    exatamente o que este projeto existe para não fazer.
    """
    assert "function faixaSolo()" in js
    assert "Modo solo" in js
    assert "você está revisando trabalho seu." in js
    assert "fica gravada como autorrevisão" in js
    # Nada de botão de fechar/dispensar na faixa.
    faixa = js.split("function faixaSolo()", 1)[1].split("\n}", 1)[0]
    assert "addEventListener" not in faixa
    # E o painel do admin declara qual dos dois números ele está mostrando.
    assert "quem revisa nunca é quem anotou" in js


def test_o_seletor_de_projeto_separa_fixture_de_trabalho_real(html: str, js: str) -> None:
    """Sem o seletor, "trabalhar só num projeto" seria uma promessa do schema
    que a interface não cumpre."""
    assert 'id="projeto"' in html
    assert "function paramsProjeto(base)" in js
    assert "function trocarProjeto()" in js
    assert '"/api/projetos"' in js
    # O número de tarefas abertas no rótulo: um seletor que não diz quanto
    # trabalho tem cada projeto obriga a entrar em todos para descobrir.
    assert "p.nome +" in js and "p.n_abertas" in js


def test_criar_persona_convida_o_autor_pelo_nome(html: str) -> None:
    """O trabalho é do autor; as personas da lista são fixtures. A tela diz
    isso e oferece o caminho, em vez de empurrar um pseudônimo."""
    assert "+ meu nome" in html
    assert "para trabalhar de verdade, entre com o seu nome nos três papéis" in html


def test_a_devolucao_usa_o_vocabulario_novo(js: str) -> None:
    """``rejeitada`` saiu do vocabulário: nada é recusado na triagem, o trabalho
    volta para ajuste — e o status da anotação tem exatamente este nome."""
    assert 'anterior.veredito !== "devolvida"' in js
    assert "rejeitada" not in js


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
