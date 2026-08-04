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
    """O roteiro do admin lista o que AINDA NÃO existe — em linguagem de
    produto, nunca em código de marco. "P4"/"P6" são vocabulário do
    repositório, e quem abre o painel não tem o mapa que os decifra: um
    roteiro que promete "P6" promete um mistério.

    A régua é a que este teste sempre aplicou, e o P5a a exerce sobre si mesmo:
    **entregou, sai da lista**. `roteiro.metricas` prometia "métricas por
    anotador e geração de tarefas"; as duas coisas existem agora, então a linha
    saiu — e as duas chaves saíram do dicionário junto, porque uma promessa
    cumprida que continua escrita na tela é pior que uma promessa que ninguém
    fez.
    """
    assert "em construção" not in js.lower()
    roteiro = js.split("const ROTEIRO = [", 1)[1].split("];", 1)[0]
    assert not re.search(r'"P\d', roteiro)
    for chave in ("roteiro.criar", "roteiro.ingestao", "roteiro.acabamento"):
        assert f'"{chave}"' in roteiro
    assert '"roteiro.metricas"' not in js, "o painel de métricas existe: a linha tinha de sair"
    assert 'marco + " — "' not in js


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
        "/api/modelos-rubrica",
        # P9: o outro eixo da instrução, e o vocabulário em que o
        # escopo dele é escrito.
        "/api/briefs",
        "/api/taxonomia",
        "/api/projetos",
        "/api/tarefas/proxima",
        "/api/tarefas/livre",
        "/api/tarefas/contagens",
        "/api/catalogo",
        "/api/atribuicoes",
        "/api/atribuicoes/",
        "/api/revisao/fila",
        "/api/revisao/",
        # P3b: a passagem 2 e a escalação.
        "/api/avaliacao/fila",
        "/api/avaliacao/",
        "/api/escalacao/fila",
        "/api/escalacao/",
        # P4d: o runtime de modelos locais e a conversa ao vivo.
        "/api/modelos",
        "/api/conversa/",
        # P5a: o painel do admin — métricas, geração em lote e a trilha.
        "/api/admin/metricas",
        "/api/admin/tarefas/previa",
        "/api/admin/tarefas",
        "/api/admin/eventos",
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


# ---------------------------------------------------------------------------
# 3b-bis. as duas abas de inferência ao vivo (P4d)
# ---------------------------------------------------------------------------


def test_os_dois_workspaces_de_conversa_existem(js: str) -> None:
    for fn in ("function wsConversa(", "function wsDuelo("):
        assert fn in js


def test_o_nome_do_modelo_nao_e_lido_pelo_desenho_do_duelo(js: str) -> None:
    """A METADE DE CIMA da garantia do rótulo cego.

    A outra metade é o servidor não mandar ``modelo``/``digest`` nos turnos de um
    duelo (``conversa._dict``, provado em ``test_annotate_p4d``). Esta prova é
    complementar e não redundante: sem ela, o dia em que alguém "só
    acrescentasse o campo no envelope" transformaria a preferência numa
    preferência por marca, e nenhum teste de backend pegaria.

    ``blocoRevelacao`` é função à parte justamente para que esta varredura seja
    possível — ela é a ÚNICA do caminho do duelo que lê o nome, e só é chamada
    depois do envio.
    """
    for fn in ("function wsDuelo(", "function fluxoDuelo(", "function rodadaDuelo("):
        corpo = js.split(fn, 1)[1].split("\n}", 1)[0]
        assert ".modelo" not in corpo, f"{fn} lê o nome do modelo"
        assert ".digest" not in corpo, f"{fn} lê o digest do modelo"


def test_a_conversa_inteira_entra_no_rascunho(js: str) -> None:
    """Perder trinta turnos é o pior desfecho possível nesta aba.

    Os turnos moram no SERVIDOR (é o que faz o duelo ser cego de verdade), e o
    rascunho guarda uma cópia mesmo assim: uma segunda rede não custa nada, e o
    que está no `est.form` é o que `salvarRascunho` grava.
    """
    assert "c0.form.turnos = doServidor" in js
    assert "function turnosDe(env, c0)" in js


def test_o_unico_relogio_a_vista_e_o_da_geracao(js: str) -> None:
    """A REGRA DO BRIEF continua valendo, e este contador não a viola.

    "Nada de cronômetro para o anotador" existe porque um relógio sobre a PESSOA
    adiciona pressão sem melhorar decisão. Este contador é sobre a MÁQUINA:
    medido, um turno leva de 1,4 s a 17,6 s, e dezessete segundos sem indicador
    são indistinguíveis de um travamento.

    Por isso ele fica com `setTimeout` recursivo e não com `setInterval` — que
    `test_nenhum_cronometro_para_quem_anota` continua proibindo —, e por isso ele
    diz `decorrido`, nunca "restante": não há prazo a vencer aqui.
    """
    assert "function tiquetaque()" in js
    assert 'el("span", "decorrido"' in js
    assert "conversa.decorrido_titulo" in js
    # E ele morre sozinho quando o indicador sai da tela.
    corpo = js.split("function tiquetaque()", 1)[1].split("\n}", 1)[0]
    assert "pararTique()" in corpo


def test_o_runtime_ausente_e_estado_com_conserto(js: str) -> None:
    """Modelo não baixado não é erro: é uma instrução com um comando ao lado."""
    assert "function blocoRuntime()" in js
    assert "function reconferirModelos()" in js
    assert '"/api/modelos"' in js
    assert "runtime.conserto" in js
    # As chaves que o SERVIDOR escolhe ficam numa tabela com a chave INTEIRA,
    # senão o teste de chave órfã não as enxerga (regra do P3i).
    assert "const CHAVES_RUNTIME" in js
    for chave in ("modelos.fora_do_ar", "modelos.faltam", "modelos.pronto", "modelos.nenhum"):
        assert f'"{chave}"' in js


def test_marcar_um_turno_nao_abre_modal(js: str) -> None:
    """O mesmo padrão do motivo por edição do P3b: o editor abre ALI, embaixo do
    turno. Um modal esconderia justamente o texto sobre o qual a nota é dada."""
    assert "function marcacaoDeTurno(" in js
    corpo = js.split("function marcacaoDeTurno(", 1)[1].split("\n}\n", 1)[0]
    for proibido in ("dialog", "modal", "showModal"):
        assert proibido not in corpo.lower()


def test_o_raciocinio_do_modelo_nao_e_filtrado(js: str) -> None:
    """Ele vem RECOLHIDO e com marcador, nunca descartado em silêncio: é conteúdo
    que o anotador pode querer avaliar, e jogá-lo fora seria decidir por ele."""
    assert "function blocoRaciocinio(turno)" in js
    corpo = js.split("function blocoRaciocinio(turno)", 1)[1].split("\n}", 1)[0]
    assert "details" in corpo
    assert "turno.raciocinio" in corpo


def test_os_modelos_de_partida_sao_DADO_e_nao_literais_na_tela(js: str) -> None:
    """Matar a página em branco é a maior redução de fricção da aba de rubrica —
    e o banco de perguntas que a mata é DADO desde o P8.

    Enquanto ele era um array de JavaScript, um quarto modelo exigia editar a
    tela; é exatamente o que o construtor do admin (P5a) existe para evitar. A
    tela agora só sabe pedir e desenhar.
    """
    from prompt_factory.annotate import rubricas_modelo

    assert "MODELOS_RUBRICA" not in js, "os modelos voltaram para dentro do JS"
    assert '"/api/modelos-rubrica"' in js
    assert "est.modelosRubrica" in js
    modelos = rubricas_modelo.carregar()
    assert len(modelos) >= 3
    # `nome_chave` é CROMO (vai para o dicionário); o resto é metadado e viaja
    # como dado. Um modelo sem a chave desenharia um botão sem rótulo.
    for m in modelos:
        assert m["nome_chave"] and m["criterios"]
    # A tela filtra pelo que o FORMULÁRIO sabe expressar: aplicar um instrumento
    # com grupo/catálogo/overall ali o mutilaria em silêncio.
    assert "cabe_no_formulario" in js
    assert len([m for m in modelos if m["cabe_no_formulario"]]) >= 3


def test_o_instrumento_de_severidade_e_dado_e_nao_tipo_de_tarefa_novo() -> None:
    """A prova de que severidade é uma RUBRICA: escala ancorada de três pontos,
    catálogo de tipos de issue por critério, e o overall como critério.

    Nenhum tipo de tarefa novo, nenhuma coluna, nenhuma migração — e é isso que
    faz o construtor do admin (P5a) ser barato: ele escreve JSON num blob.
    """
    from prompt_factory.annotate import rubricas_modelo

    sev = next(m for m in rubricas_modelo.carregar() if m["id"] == "severidade")
    assert sev["cabe_no_formulario"] is False, "o formulário não expressa este instrumento"
    principais = [c for c in sev["criterios"] if not c.get("resumo")]
    overall = [c for c in sev["criterios"] if c.get("resumo")]
    assert overall, "sem overall, o rail não tem o que trancar"
    for c in principais:
        assert c["escala"]["min"] == 1 and c["escala"]["max"] == 3
        # AS TRÊS ÂNCORAS, não só as pontas: numa escala de 3 todo o desacordo
        # entre anotadores mora no ponto do meio.
        assert [a["valor"] for a in c["escala"]["ancoras"]] == [1, 2, 3]
        assert c["tipos_issue"], f"{c['nome']}: sem catálogo, a regra de obrigação não dispara"
        assert all(t["id"] for t in c["tipos_issue"])
    assert len({c["grupo"] for c in principais}) >= 2, "sem grupo, não há abas nem cabeçalho"


def test_a_fixture_de_modelos_falha_alto_quando_esta_torta(tmp_path: Path) -> None:
    """Um modelo pela metade vira uma rubrica pela metade, e ninguém descobre
    até alguém tentar aplicar uma nota nela."""
    import json

    from prompt_factory.annotate import rubricas_modelo

    ruim = tmp_path / "m.json"
    ruim.write_text(
        json.dumps({"schema": "modelos_rubrica@1", "modelos": [
            {"id": "x", "escala_min": 1, "escala_max": 2, "criterios": [{"nome": "a"}]}]}),
        encoding="utf-8",
    )
    with pytest.raises(rubricas_modelo.ModeloInvalido, match="três posições"):
        rubricas_modelo.carregar(ruim)


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
# 3d. F1 — gating, a11y, prazo
#
# Os cinco defeitos que esta seção congela têm uma coisa em comum: nenhum deles
# aparece na tela. Um atalho que age na tela errada, um contraste que só reprova
# no tema que ninguém abriu, um campo sem nome acessível, um erro anunciado
# "quando der" e um comentário de 300 caracteres que morre num F5 — todos passam
# por uma leitura visual e nenhum passa por uma medição.
# ---------------------------------------------------------------------------


def _corpo(js: str, assinatura: str) -> str:
    """O corpo de uma função top-level.

    Vale porque toda função top-level deste arquivo fecha com ``}`` na coluna 0 —
    a disciplina que proíbe rodar formatador nele.
    """
    return js.split(assinatura, 1)[1].split("\n}", 1)[0]


def test_o_teclado_despacha_pelo_papel_da_BARRA(js: str) -> None:
    """O defeito: ``teclado()`` roteava por ``est.persona.papel``.

    No MODO SOLO a mesma pessoa veste os três chapéus, então a persona pode dizer
    "revisor" com o painel do admin aberto — e o painel do admin caía em
    ``tecladoRevisor``, que despacha pela sub-aba do revisor e pelo ``est.triagem``
    que ninguém zerava. Um ``a`` digitado fora de um campo aprovava, em silêncio,
    o item que a triagem tinha selecionado da última vez.

    Quem manda é a BARRA, e é o mesmo ``||`` que ``abrirApp`` usa para decidir
    QUAL TELA está aberta — duas fontes de verdade para a mesma pergunta é o que
    produziu o defeito.
    """
    corpo = _corpo(js, "function teclado(ev) {")
    assert "est.papelEscolhido" in corpo, "o teclado voltou a rotear pela persona"
    # E o admin não herda atalho nenhum: ele SAI, não cai no do revisor.
    assert 'if (papel === "admin") return;' in corpo


def test_o_admin_nao_herda_a_fila_do_revisor(js: str) -> None:
    """A outra metade do mesmo defeito, e a que sobrevive a um chamador novo.

    Zerar as duas filas ao entrar no painel do admin é simétrico ao ramo do
    revisor (que já as zerava por outra razão: elas mudam por fora). Sem isso,
    uma seleção viva atravessa para uma tela que não tem o que fazer com ela.
    """
    ramo = _corpo(js, "async function abrirApp() {").split('if (papel === "admin") {', 1)[1]
    assert "est.triagem = null" in ramo
    assert "est.rr = null" in ramo
    # E a guarda de dentro, para o dia em que houver um segundo chamador.
    assert 'if (est.papelEscolhido !== "revisor") return;' in js


def test_o_seletor_de_papel_anda_pelo_teclado(js: str) -> None:
    """Um ``radiogroup`` sem setas é um radiogroup só no nome.

    O padrão já existia em ``escalaRR`` (roving tabindex + setas) e não tinha sido
    aplicado ao controle mais visível da plataforma. E o foco tem de ser
    DEVOLVIDO depois do redesenho: escolher um papel reconstrói os três botões, e
    sem isso a segunda seta seguida não chega a lugar nenhum.
    """
    corpo = _corpo(js, "function desenharPapeis() {")
    assert "tabIndex" in corpo
    assert "ArrowRight" in corpo and "ArrowLeft" in corpo
    assert "document.activeElement" in corpo and ".focus()" in corpo


def test_o_estado_selecionado_nao_usa_branco_cravado(html: str) -> None:
    """``#fff`` sobre ``--acento`` reprova o AA no tema escuro (~2,4:1).

    O acento CLAREIA no escuro (#6FA8D8) justamente para continuar legível sobre
    fundo escuro — e é por isso que o texto por cima dele tem de escurecer. Quem
    sabe disso é ``--sobre-acento``, que já existia e já era usada pelo botão
    primário.
    """
    for regra in ('.papel[aria-checked="true"]', '.leitura-modo .nota[aria-pressed="true"]'):
        bloco = html.split(regra, 1)[1].split("}", 1)[0]
        assert "var(--sobre-acento)" in bloco, regra
        assert "#fff" not in bloco, regra
    # E o toast de erro, que é branco sobre um vermelho que também clareia.
    assert ':root[data-tema="escuro"] .recado.ruim' in html


def test_todo_campo_criado_por_JS_tem_nome_acessivel(js: str) -> None:
    """Dezenove campos nasciam sem rótulo nenhum.

    A tela é montada inteira por ``el()``, e ``el()`` não sabe de rótulo: o texto
    que explica um campo é quase sempre um ``<span>`` ou um ``placeholder``
    vizinho, e nenhum dos dois vira nome acessível sozinho — o placeholder ainda
    some ao primeiro caractere. Numa matriz de vinte critérios isso são vinte
    caixas idênticas para quem navega por leitor de tela.
    """
    assert js.count('setAttribute("aria-label"') >= 24
    # Onde o rótulo carrega DADO (o nome do critério), a composição sai do
    # dicionário como as `a11y.*` que já existiam — nunca por concatenação.
    for chave in ("a11y.justificativa", "a11y.motivo_na", "a11y.trecho",
                  "a11y.campo_do_criterio"):
        assert f't("{chave}"' in js, chave


def test_erro_tem_regiao_viva_propria(html: str, js: str) -> None:
    """``role="status"`` espera a vez do leitor de tela — pode nunca ser anunciado.

    Aceitável para "enviado"; é o defeito inteiro para "não foi possível enviar",
    que ainda some da tela em quatro segundos. São DOIS nós porque trocar o
    ``role`` de um nó já vivo não é confiável em leitor nenhum.
    """
    assert 'id="recado-erro"' in html
    assert 'role="alert"' in html
    assert 'role="status"' in html
    corpo = _corpo(js, "function recado(msg, ruim) {")
    assert '#recado-erro' in corpo
    # Os dois nunca ficam na tela ao mesmo tempo — eles ocupam o mesmo lugar.
    assert "outro.hidden = true" in corpo


def test_a_faixa_de_papel_publica_a_propria_altura(html: str, js: str) -> None:
    """Ela é a SEGUNDA barra grudada no topo, e as duas de dentro da tarefa (o
    cabeçalho da matriz e o rail) paravam ATRÁS dela.

    Só o JS sabe se ela está na tela — ela existe apenas na primeira visita a
    cada papel —, então ele publica a altura e o CSS soma. É o mesmo precedente
    de ``--cols``: o que é dado sai do JS, o que é regra fica no CSS.
    """
    assert "--faixa-h:0px" in html
    assert 'setProperty("--faixa-h"' in js
    for regra in (".matriz-cabeca{", ".rail{"):
        bloco = html.split(regra, 1)[1].split("}", 1)[0]
        assert "var(--faixa-h, 0px)" in bloco, regra
    # E ela é da PRIMEIRA VEZ, com o mesmo padrão de `diretrizesVistas`.
    assert "faixaVista: new Set()" in js
    assert "function medirFaixa()" in js


def test_o_css_nao_tem_dois_seletores_com_o_mesmo_nome(html: str) -> None:
    """``.guia`` estava definida DUAS vezes, a 200 linhas de distância, e o
    navegador aplicava as duas: o checklist do SFT herdava ``position:sticky`` da
    faixa-guia da matriz, e a faixa-guia herdava o ``display:flex`` do checklist.
    Cada regra parecia certa sozinha e nenhuma dizia o que estava desenhando."""
    assert html.count(".guia{") == 1
    assert ".guia-marcaveis{" in html
    # E a variável que nunca existiu: `var(--mono)` caía sempre no fallback.
    assert "var(--mono," not in html
    assert "--fonte-mono" in html


def test_o_JS_nao_escreve_estilo_a_nao_ser_dado(js: str) -> None:
    """Doze ``no.style.marginTop = "14px"`` espalhados por doze funções são um
    sistema de espaçamento que só existe na cabeça de quem o escreveu.

    O que CONTINUA saindo do JS é ``setProperty``, e só ele: ``--cols`` é a escala
    da rubrica (dado — o CSS não pode saber se são 3 ou 5) e ``--faixa-h`` é uma
    medida do DOM. Dado não cabe numa classe.
    """
    escritas = re.findall(r"\.style\.(\w+)", js)
    assert set(escritas) <= {"setProperty"}, sorted(set(escritas))


def test_a_celula_da_matriz_carrega_a_ancora_para_o_telefone(html: str, js: str) -> None:
    """Abaixo de 900px o cabeçalho de coluna deixa de existir (a matriz vira
    ficha) e as células ficavam com o número pelado — uma escala ancorada sem
    âncora nenhuma.

    O nó nasce SEMPRE e o CSS o esconde acima de 900px: girar o aparelho não pode
    depender de redesenhar a matriz.
    """
    assert ".celula-ancora{display:none}" in html
    movel = html.split("@media (max-width: 900px){", 1)[1].split("\n}", 1)[0]
    assert ".celula-ancora{display:block}" in movel
    assert 'el("span", "celula-rotulo celula-ancora", rotulo)' in js


def test_o_prazo_aparece_sem_nenhum_relogio(js: str) -> None:
    """A vaga volta para a fila quando o prazo vence, e a primeira notícia disso
    era o aviso DEPOIS do envio.

    A etiqueta diz a HORA, calculada uma vez no desenho. Nada conta para trás,
    nada pisca, nada se atualiza sozinho — a regra do brief ("ninguém cronometra
    a pessoa") continua valendo inteira, e ``test_nenhum_cronometro_para_quem_anota``
    continua sendo quem a prova.
    """
    assert "function etiquetaDePrazo(env)" in js
    assert 't("ws.expira"' in js
    corpo = _corpo(js, "function etiquetaDePrazo(env) {")
    assert "env.expira_em" in corpo
    for proibido in ("setInterval", "setTimeout", "requestAnimationFrame"):
        assert proibido not in corpo, proibido
    # Só quem está COM a vaga vê o prazo: quem tria lê o mesmo envelope, e um
    # prazo de outra pessoa é um número sem dono.
    assert "meu ? etiquetaDePrazo(env) : null" in js


def test_o_comentario_do_revisor_sobrevive_a_um_F5(js: str) -> None:
    """Os dois campos livres que moravam SÓ em ``est``.

    Trocar de idioma já não os perdia (é o que ``est`` resolve); fechar a aba,
    sim. E são justamente os dois textos que alguém reescreve com mais cuidado:
    o da devolução é a única instrução que o anotador recebe para consertar o
    trabalho, e o da decisão do admin é a única que ele recebe pelo outro caminho.
    """
    assert 'CHAVE + "rascunho.triagem."' in js
    assert 'CHAVE + "rascunho.escalacao."' in js
    assert "function lerRascunhoTexto(chave)" in js
    assert "function salvarRascunhoTexto(chave, texto)" in js
    # O estado continua sendo a fonte do que está na tela (P3i) — o disco é a
    # segunda rede, e só entra quando o estado está vazio.
    assert "tri.comentario = area.value" in js
    assert "area.value = tri.comentario" in js


def test_nenhuma_data_chega_crua_a_tela(js: str) -> None:
    """``2026-08-03T14:07:22`` é vocabulário de máquina: diz UTC sem avisar,
    escreve o mês na ordem de uma convenção só e gasta metade do espaço com o que
    ninguém lê. ``Intl`` resolve as três coisas — e segue a língua da interface,
    como o ``NUM`` já fazia."""
    assert '.slice(0, 19).replace("T"' not in js
    assert "function dataHora(iso)" in js and "function horaDe(iso)" in js
    # Os três formatadores são refeitos JUNTOS, num lugar só: dois lugares
    # divergem no primeiro que alguém acrescentar.
    assert "function formatadores(lang)" in js
    troca = _corpo(js, "function escolherIdioma(lang) {")
    assert "formatadores(lang)" in troca


# ---------------------------------------------------------------------------
# 3e. F2 — o conteúdo da tela, e não a mecânica dela
#
# Esta seção congela cinco decisões que um leitor casual leria como "cosmético"
# e que decidem se a plataforma parece um produto ou um protótipo: a folha de
# atalhos que revela os gestos invisíveis, o caminho do payload dito em
# português de gente, e a forma que ocupa o lugar da frase que pisca.
# ---------------------------------------------------------------------------


def test_a_folha_de_atalhos_existe_e_abre_pelo_teclado(html: str, js: str) -> None:
    """A matriz de critérios é operável inteira pelos dígitos, por ``N``, por
    ``0`` e pelas setas, e NADA disso aparecia em lugar nenhum da tela: quem não
    leu o código gradua vinte critérios com o mouse.

    É painel e não modal — um diálogo que rouba o foco para ensinar a usar o
    teclado desfaz o que está ensinando. E o nó nasce sem texto: quem o escreve é
    ``desenharAtalhos()``, a partir do PAPEL aberto.
    """
    assert '"folha-atalhos"' in html
    assert 'ev.key === "?"' in js
    assert "function desenharAtalhos()" in js and "function alternarAtalhos()" in js
    # As seções são filtradas por papel e guardam a chave INTEIRA — chave montada
    # por concatenação é invisível para o teste de chave órfã (regra do P3i).
    atalhos = js.split("const ATALHOS = [", 1)[1].split("\n];", 1)[0]
    for chave in ("atalhos.matriz_nota", "atalhos.tri_aprovar", "atalhos.escala_setas"):
        assert f'"{chave}"' in atalhos, chave
    # Nada de modal: sem overlay, sem trava de foco.
    corpo = _corpo(js, "function desenharAtalhos() {")
    for proibido in ("dialog", "showModal", "inert"):
        assert proibido not in corpo.lower(), proibido


def test_os_atalhos_invisiveis_da_matriz_tem_chip(js: str) -> None:
    """Os do A/B e da triagem sempre tiveram chip ao lado do botão; os da matriz
    não tinham onde caber, e por isso eram os únicos que ninguém descobria."""
    assert js.count('el("span", "atalho"') >= 12


def test_o_caminho_do_payload_ganhou_nome_de_gente(js: str) -> None:
    """``notas.2.nota`` é o endereço no payload e é o que vai para
    ``edicoes_avaliacao.campo`` — ele FICA, porque é a trilha de auditoria. O que
    faltava era a outra metade: sozinho, ele obrigava quem justifica uma mudança
    a decorar a ordem dos critérios.

    O nome do critério sai do DADO (a rubrica do envelope, ou o formulário quando
    a tarefa é escrever a rubrica), nunca de uma segunda cópia no JS.
    """
    assert "function rotuloDoCampo(" in js
    assert "const CAMPOS_LEGIVEIS" in js
    corpo = _corpo(js, "function rotuloDoCampo(campo, env) {")
    assert "bi(c.nome_i18n, c.nome)" in corpo
    assert 't("ws.criterio_n"' in corpo
    # A escalação lê `edicoes_avaliacao` sem o payload em mãos: ela chama com
    # `null` e cai no número do critério, que já é melhor que o caminho cru.
    assert "rotuloDoCampo(ed.campo, null)" in js


def test_carregar_mostra_a_forma_do_que_vem(js: str) -> None:
    """"Carregando…" numa área vazia dá dois quadros: nada, e a tela inteira. A
    frase não some — ela vira o ``aria-label`` da região, que é onde ela sempre
    foi útil de verdade."""
    assert 'esqueleto("fila.puxando")' in js
    for chave in ("catalogo.carregando", "minhas.carregando", "triagem.abrindo", "rr.abrindo"):
        assert f'esqueleto("{chave}")' in js, chave
    corpo = _corpo(js, "function esqueleto(chave) {")
    assert 'setAttribute("aria-label", t(chave))' in corpo


def test_o_botao_primario_nao_muda_de_nome_enquanto_alguem_digita(html: str, js: str) -> None:
    """O rótulo do botão ERA ``oQueFalta()``: "faltam 12 caracteres na
    justificativa" virava "Enviar e próxima" no meio da digitação — um alvo que
    mudava de nome e de tamanho a cada tecla, e um aviso que ia embora no instante
    em que deixava de ser verdade, sem nunca ter dito o nome da ação.

    ``oQueFalta()``/``rrOQueFalta()`` continuam intocados: o que mudou foi ONDE a
    frase deles aparece.
    """
    assert '"falta-envio"' in js and '"rr-falta"' in js
    assert ".acoes .falta" in html
    for fn in ("function atualizarBotaoEnviar() {", "function rrAtualizarBotao() {"):
        corpo = _corpo(js, fn)
        assert "b.textContent" not in corpo, fn
        assert "aviso.hidden = !falta" in corpo, fn
    # E o identificador do contrato de payload saiu do texto visível do cabeçalho
    # — ele continua no `title`, que é onde vocabulário de máquina pertence.
    assert js.count("carimbo.title = a.payload_schema;") == 2


# ---------------------------------------------------------------------------
# 3f. F3 — o painel do admin (P5a): abas, gráficos e a trilha
#
# Duas coisas que só se provam aqui: que o dashboard não voltou a nascer de
# string (é a mesma regra absoluta do resto da app, e um gráfico é justamente o
# lugar onde a tentação de montar HTML aparece) e que as barras não viraram SVG
# — o namespace do SVG é uma URL, e `test_zero_referencia_externa` proíbe uma
# única ocorrência de `http://` no arquivo inteiro.
# ---------------------------------------------------------------------------


def test_o_painel_do_admin_virou_cinco_abas(js: str, html: str) -> None:
    """Sete painéis empilhados + os dois maiores do P5a seriam dois mil pixels
    de rolagem na única tela que ninguém percorre por prazer.

    As chaves são LITERAIS inteiras na tabela (padrão de `TIPOS`/`ROTEIRO`), e a
    navegação por seta reusa o mesmo `andarNasAbas` das sub-abas do revisor e
    das seções do brief — um segundo tablist com regra própria divergiria do
    primeiro no dia em que alguém ajustasse uma das duas.
    """
    for chave in ("abas_admin.visao", "abas_admin.metricas", "abas_admin.gerar",
                  "abas_admin.escalacoes", "abas_admin.pool"):
        assert f'"{chave}"' in js, chave
    assert "est.abaAdmin" in js
    assert "andarNasAbas(ev, ABAS_ADMIN" in js
    assert 'id="abas-admin"' in html
    # Cada aba tem o bloco dela no HTML — a alternância é `hidden`, não estilo.
    for sec in ("admin-sec-visao", "admin-sec-metricas", "admin-sec-gerar",
                "admin-sec-escalacoes", "admin-sec-pool"):
        assert f'id="{sec}"' in html, sec


def test_as_barras_do_dashboard_sao_DIV_e_nao_SVG(js: str, html: str) -> None:
    """O namespace do SVG é uma URL do w3.org, e esta plataforma prova que não
    existe UMA referência externa no arquivo — ela tem de abrir offline, hoje e
    daqui a dois anos. Um `createElementNS` traria a URL junto e derrubaria
    `test_zero_referencia_externa` por causa de um gráfico.

    A largura é DADO (o CSS não sabe quanto vale 37%) e sai por `setProperty`,
    o mesmo canal de `--cols` e `--faixa-h` — nunca por estilo inline, que
    `test_o_JS_nao_escreve_estilo_a_nao_ser_dado` continua proibindo.
    """
    assert "function barrasH(" in js
    corpo = _corpo(js, "function barrasH(pares, chaveAria) {")
    assert "innerHTML" not in corpo
    for proibido in ("createElementNS", "svg", "NS_SVG"):
        assert proibido not in corpo, proibido
    assert 'setProperty("--w"' in corpo
    assert ".grafico-barra{width:var(--w, 0%)" in html
    # E a matriz de confusão é uma <table> com classes de calor — zero estilo.
    assert "function matrizConfusao(cal)" in js
    assert ".calor-1{" in html and ".mc-acerto{" in html


def test_o_dashboard_le_a_rota_de_metricas_uma_vez_e_so_quando_precisa(js: str) -> None:
    """Buscar as métricas na abertura do admin cobraria a varredura de todas as
    anotações entregues de quem só veio conferir o pool."""
    assert '"/api/admin/metricas"' in js
    assert 'esqueleto("metricas.abrindo")' in js
    corpo = _corpo(js, "function escolherAbaAdmin(id) {")
    assert 'id === "metricas" && !est.metricas' in corpo


def test_a_previa_da_geracao_e_obrigatoria_antes_de_criar(js: str) -> None:
    """O botão primário tem rótulo FIXO e o que falta é uma linha acima dele —
    o mesmo contrato de `barraAcoes` e do cartão da escalação. E a prévia
    caduca a cada parâmetro mexido: criar sob uma prévia velha entregaria um
    número diferente do prometido, que é o único jeito de um formulário deste
    tipo perder a confiança de quem o usa."""
    assert '"/api/admin/tarefas/previa"' in js
    corpo = _corpo(js, "function atualizarBotaoGerar() {")
    assert "falta.previa" in corpo and "falta.nada" in corpo
    assert "botao.textContent" not in corpo
    mudou = _corpo(js, "function mudouGerar() {")
    assert "g.previa = null" in mudou


def test_a_trilha_de_auditoria_abre_no_item_escalado(js: str) -> None:
    """O admin decide sobre um trabalho que atravessou duas passagens; sem a
    trilha, a única saída seria remontar a história por inferência a partir de
    carimbos de tempo espalhados por cinco tabelas.

    A AÇÃO SAI CRUA: `avaliacao_registrada` é o valor de `eventos.acao` no
    banco, e traduzi-lo faria a tela e a trilha discordarem sobre o nome da
    mesma coisa.
    """
    assert '"/api/admin/eventos"' in js
    assert "function blocoTrilha(item)" in js
    corpo = _corpo(js, "function desenharTrilha(corpo, estado) {")
    assert 'el("span", "trilha-acao", ev.acao)' in corpo
    # Cache por item: reabrir um `<details>` já lido não custa outra ida.
    assert "est.trilhas[chave]" in js


# ---------------------------------------------------------------------------
# 3g. F4 — a bússola de quatro passos do Rate and Review
#
# O detalhe da passagem 2 é um documento contínuo de quatro seções, e o
# documento FICA: recortá-lo em quatro telas esconderia a adjacência que faz a
# passagem valer. O que faltava era ORIENTAÇÃO — onde estou, o que já decidi, o
# que ainda falta — e é isso que a bússola grudada no topo entrega.
#
# Três coisas se provam aqui e em lugar nenhum: que ela lê a MESMA regra do
# botão primário (duas leituras divergentes fariam a tela se contradizer sobre
# o mesmo item), que a altura dela é publicada e ZERADA em todo caminho que
# desenha o mesmo nó sem ela, e que os overrides de `sticky` vieram DEPOIS dos
# blocos originais — a ordem é o que mantém de pé o teste de `--faixa-h`.
# ---------------------------------------------------------------------------


def test_a_bussola_tem_as_cinco_pecas_e_a_tabela_de_passos(js: str) -> None:
    """Construir, derivar, pintar, saltar e medir são cinco responsabilidades —
    e são cinco funções porque a de PINTAR roda a cada tecla e não pode
    reconstruir nada (o foco morreria no meio da palavra, como em
    ``rrSincronizar``).

    As chaves são LITERAIS inteiras na tabela, pelo mesmo motivo de ``TIPOS`` e
    de ``ESCALA_ANTES``: ``t("rr.passo_" + id)`` seria invisível para o teste de
    chave órfã, e o dicionário passaria a acumular entradas mortas.
    """
    for fn in ("function rrPassos()", "function bussolaRR()",
               "function rrAtualizarBussola()", "function irAoPassoRR(",
               "function medirBussolaRR()"):
        assert fn in js, fn
    assert "const PASSOS_RR = [" in js
    assert "const ESTADOS_BUSSOLA = {" in js
    for chave in ("rr.passo_antes", "rr.passo_corrigir", "rr.passo_depois", "rr.passo_just",
                  "rr.bussola_feito", "rr.bussola_atual", "rr.bussola_pendente",
                  "rr.bussola_opcional"):
        assert f'"{chave}"' in js, chave
    # A bússola é montada por `el()` como todo o resto — nunca de string.
    assert "innerHTML" not in _corpo(js, "function bussolaRR()")


def test_a_bussola_e_o_botao_leem_a_MESMA_regra(js: str) -> None:
    """``rrPassos`` e ``rrOQueFalta`` respondem à mesma pergunta em dois lugares.

    Se os pisos divergirem, a bússola declara um passo pronto enquanto a linha
    acima do botão continua dizendo que falta — e quem lê a bússola conclui que
    o botão está quebrado. Os dois ``lim()`` (chave E default) têm de aparecer
    nos DOIS corpos, no mesmo commit: é uma trava cruzada, não uma coincidência.
    """
    for fn in ("function rrPassos()", "function rrOQueFalta() {"):
        corpo = _corpo(js, fn)
        assert 'lim("min_chars_motivo_edicao", 10)' in corpo, fn
        assert 'lim("min_chars_avaliacao", 30)' in corpo, fn
    # E o resumo de cada escala sai das MESMAS tabelas que a desenham: zero
    # tradução nova, e nenhum segundo nome para a mesma opção.
    passos = _corpo(js, "function rrPassos()")
    assert "nomeDaEscala(ESCALA_ANTES, rr.antes)" in passos
    assert "nomeDaEscala(ESCALA_DEPOIS, rr.depois)" in passos
    # O contador da justificativa é o mesmo do campo, logo abaixo dela.
    assert 't("ws.contador_de"' in passos


def test_o_passo_atual_sai_do_aria_current_e_nao_de_uma_classe(js: str, html: str) -> None:
    """O estado é UM. Uma classe ``.atual`` ao lado do ``aria-current`` seriam
    duas escritas da mesma verdade — e é assim que as duas metades divergem,
    com a tela pintando um passo e o leitor de tela anunciando outro.

    ``opcional`` também é estado e não ausência: nada mudou, e não há o que
    corrigir. Marcá-lo como pendência inventaria uma tarefa que a plataforma
    diz, em ``rr.diff_vazio``, que não existe.
    """
    assert '"aria-current", "step"' in js
    assert '.rr-bussola-passo[aria-current="step"]{' in html
    assert '.rr-bussola-passo[data-estado="feito"]{' in html
    assert '.rr-bussola-passo[data-estado="opcional"]{border-style:dashed}' in html
    # E as quatro seções viram alvo de salto: id, região COM nome e foco de
    # último recurso (uma `div` não é focável sozinha).
    marcar = js.split("const marcarPasso = (no, id) => {", 1)[1].split("\n  };", 1)[0]
    assert "no.tabIndex = -1" in marcar
    assert 'no.setAttribute("role", "region")' in marcar
    assert 'no.setAttribute("aria-label", t(p.rotulo))' in marcar
    for passo in ("antes", "corrigir", "depois", "just"):
        assert f'marcarPasso({passo}, "{passo}")' in js, passo


def test_a_bussola_publica_a_altura_e_a_zera_onde_ela_nao_existe(js: str, html: str) -> None:
    """Ela é a TERCEIRA barra grudada, e as duas de dentro da tarefa parariam
    atrás dela — o mesmo defeito que ``--faixa-h`` consertou um andar acima.

    O ponto fino é o zero: o MESMO ``#revisor-detalhe`` desenha a triagem e os
    três estados vazios do Rate and Review. Um ``--bussola-h`` sobrando ali
    grudaria o cabeçalho da matriz uma bússola abaixo de onde ela está. Por isso
    a medida vem logo depois do ``limpar(det)`` das duas funções de desenho: um
    ponto só, antes de qualquer ``return`` antecipado.
    """
    assert 'setProperty("--bussola-h"' in js
    # Três chamadas: zera na triagem, zera no início do RR, mede no fim dele.
    assert js.count("medirBussolaRR();") == 3
    assert js.count("function medirBussolaRR()") == 1
    # Os overrides são ESCOPADOS e vêm DEPOIS dos blocos originais — é essa
    # ordem que mantém `test_a_faixa_de_papel_publica_a_propria_altura` medindo
    # o bloco que ele acha que está medindo.
    for regra in (".matriz-cabeca{", ".rail{"):
        assert html.index("#revisor-detalhe " + regra) > html.index(regra), regra
        bloco = html.split("#revisor-detalhe " + regra, 1)[1].split("}", 1)[0]
        assert "var(--bussola-h, 0px)" in bloco, regra
        assert "var(--faixa-h, 0px)" in bloco, regra
    # E o sticky sobrevive ao telefone: some o RÓTULO, não a orientação.
    assert html.count("@media (max-width: 900px){") == 1
    movel = html.split("@media (max-width: 900px){", 1)[1].split("\n}", 1)[0]
    assert ".rr-bussola-rotulo{display:none}" in movel
    assert ".rr-bussola{padding:6px 10px}" in movel
    assert "position:sticky" not in movel


def test_o_salto_de_um_passo_leva_o_foco_junto(js: str, html: str) -> None:
    """Rolar sem levar o foco deixa o teclado para trás: a tecla seguinte agiria
    na bússola, e não no controle que acabou de aparecer.

    O alvo é o primeiro interativo VIVO, com ``[tabindex="0"]`` encabeçando a
    lista de propósito — dentro de um radiogroup ele é o botão que o roving
    elegeu, que é onde a pessoa espera cair. E quem rola é o ``scrollIntoView``,
    com o ``scroll-margin-top`` do CSS descontando as três barras grudadas:
    sem ele o título da seção para atrás da bússola que acabou de ser clicada.
    """
    corpo = _corpo(js, "function irAoPassoRR(")
    assert '[tabindex=\\"0\\"],button,input,textarea,select' in corpo
    assert "focus({ preventScroll: true })" in corpo
    assert '"(prefers-reduced-motion: reduce)"' in corpo
    assert "scrollIntoView" in corpo
    assert "#revisor-detalhe .rr-secao{" in html
    assert "scroll-margin-top:calc(" in html


def test_a_atualizacao_da_bussola_nunca_rouba_o_foco(js: str) -> None:
    """Ela roda a cada tecla, por um listener DELEGADO no ``#revisor-detalhe``.

    Duas consequências que só se veem aqui: a função tem de ser NOMEADA (mesmo
    alvo, mesmo tipo e a mesma referência não empilham, e o detalhe é redesenhado
    a cada clique de escala) e ela tem de sair cedo quando ``est.rr`` é nulo — o
    mesmo nó serve a triagem, e um ``input`` na caixa de devolução chega aqui.
    """
    assert 'det.addEventListener("input", rrAtualizarBussola)' in js
    corpo = _corpo(js, "function rrAtualizarBussola()")
    assert "!nav || !est.rr || !est.rr.detalhe || !est.rr.form" in corpo
    # O roving segue quem está focado; só na ausência de foco ele vai ao atual.
    assert "nav.contains(document.activeElement)" in corpo
    assert "focado ? (b === focado ? 0 : -1)" in corpo
    # E o redesenho estrutural devolve o foco ao passo equivalente, como
    # `desenharPapeis` faz com os papéis.
    detalhe = _corpo(js, "function desenharDetalheRR() {")
    assert 'focado.closest("#rr-bussola")' in detalhe


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
