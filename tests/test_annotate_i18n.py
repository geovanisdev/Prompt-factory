"""P3i — a camada de idioma da Bancada, provada sem navegador.

O portfólio é lido por avaliadores estrangeiros, então **o inglês é a língua
primária** e o pt-BR é um toggle. Isto não é um embrulho: as telas do P1/P2/P3a
foram escritas em português e foram traduzidas.

Estes testes existem porque uma camada de i18n apodrece de três jeitos, e os três
são silenciosos:

1. **Uma língua ganha uma chave que a outra não tem.** A tela abre com um buraco
   em uma delas, e quem trabalha na outra nunca vê.
2. **Chave órfã nos dois sentidos.** Uma chave usada e inexistente vira texto
   cru no botão; uma chave que existe e ninguém usa vira tradução mantida à toa —
   e, pior, esconde que a string de verdade ficou literal no código.
3. **Português vazando para o modo inglês.** É o defeito que este marco existe
   para impedir, e o único que o avaliador vê.

A prova de (3) é ESTÁTICA e vale porque o arquivo tem uma disciplina só: todo
texto de cromo sai de `TEXTOS[idioma]` ou de um `data-t*`. Não há terceira via —
e é isso que os testes de literal solto conferem. O que sobra na tela além do
dicionário é **dado** (prompt, resposta de modelo, texto que o usuário escreveu),
e a língua do dado é do dado.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest

from prompt_factory.annotate import db as adb
from prompt_factory.annotate import diretrizes as dirmod
from prompt_factory.annotate.main import STATIC

INDEX = STATIC / "index.html"


@pytest.fixture(scope="module")
def html() -> str:
    return INDEX.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def js(html: str) -> str:
    """Só o corpo do ``<script>`` (mesmo recorte de ``test_annotate_frontend``)."""
    return re.split(r"^<script>$", html, maxsplit=1, flags=re.M)[1]


@pytest.fixture(scope="module")
def textos(js: str) -> dict[str, dict[str, str]]:
    """O dicionário, carregado com ``json.loads``.

    É POR ISSO que ele é escrito em JSON estrito: sem um parser de JavaScript,
    esta é a única forma de um teste em Python afirmar qualquer coisa sobre ele.
    Se este fixture falhar, alguém pôs aspas simples, vírgula sobrando ou
    comentário dentro do bloco — e o dicionário deixou de ser verificável.
    """
    bruto = js.split("const TEXTOS = {", 1)[1].split("\n};", 1)[0]
    return json.loads("{" + bruto + "}")


# ---------------------------------------------------------------------------
# 1. paridade
# ---------------------------------------------------------------------------


def test_o_dicionario_tem_as_duas_linguas(textos: dict[str, dict[str, str]]) -> None:
    assert set(textos) == {"en", "pt"}
    assert len(textos["en"]) > 200, "o dicionário está pequeno demais para a tela inteira"


def test_paridade_exata_entre_as_duas_linguas(textos: dict[str, dict[str, str]]) -> None:
    """Sem sobra e sem falta. Uma chave a mais em um lado é um buraco no outro."""
    so_en = sorted(set(textos["en"]) - set(textos["pt"]))
    so_pt = sorted(set(textos["pt"]) - set(textos["en"]))
    assert not so_en, f"chaves só em inglês: {so_en}"
    assert not so_pt, f"chaves só em português: {so_pt}"


def test_nenhum_valor_vazio(textos: dict[str, dict[str, str]]) -> None:
    for lang, dicionario in textos.items():
        vazias = sorted(k for k, v in dicionario.items() if not str(v).strip())
        assert not vazias, f"{lang}: chave sem texto {vazias}"


def test_os_marcadores_batem_entre_as_linguas(textos: dict[str, dict[str, str]]) -> None:
    """``{n}``, ``{erro}``, ``{versao}``: o mesmo conjunto nas duas línguas.

    Um marcador que existe só em inglês aparece como ``{n}`` literal na tela em
    português — e um que existe só em português deixa o número de fora do texto
    em inglês, que é pior porque ninguém percebe.
    """
    marcador = re.compile(r"\{(\w+)\}")
    for chave, en in textos["en"].items():
        assert marcador.findall(en).sort() == marcador.findall(textos["pt"][chave]).sort()
        assert set(marcador.findall(en)) == set(marcador.findall(textos["pt"][chave])), chave


# ---------------------------------------------------------------------------
# 2. nenhuma chave órfã, nos dois sentidos
# ---------------------------------------------------------------------------


def _sem_comentarios(js: str) -> str:
    """O código sem a prosa. A prosa CITA chaves (``t("tela." + papel)``) para
    explicar por que elas não existem, e uma varredura ingênua as pegaria."""
    sem = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"^\s*//.*$", "", sem, flags=re.M)


def _chaves_pedidas(js: str, html: str) -> set[str]:
    """Só o que passa por ``t()`` ou por um ``data-t*`` — o que a tela EXECUTA."""
    de_t = set(re.findall(r'\bt\(\s*"([^"]+)"', _sem_comentarios(js)))
    do_html = set(re.findall(r'data-t(?:-ph|-title|-aria)?="([^"]+)"', html))
    return de_t | do_html


def _chaves_usadas(js: str, html: str) -> set[str]:
    """Toda chave que o código pode pedir — inclusive pelas TABELAS.

    ``TIPOS``, ``ROTULO_STATUS``, ``ROTEIRO`` e companhia guardam a chave
    INTEIRA como literal (em vez de montá-la por concatenação) justamente para
    caberem aqui: uma chave montada seria invisível para este teste, e o teste
    passaria a aprovar um dicionário com entradas mortas.
    """
    literais = set(
        re.findall(r'"([a-z][a-zA-Z0-9_]*(?:\.[a-zA-Z0-9_]+)+)"', _sem_comentarios(js))
    )
    return _chaves_pedidas(js, html) | literais


def test_toda_chave_pedida_existe_no_dicionario(
    js: str, html: str, textos: dict[str, dict[str, str]]
) -> None:
    """Um ``t()`` sem entrada no dicionário escreve a chave crua no botão."""
    faltando = sorted(_chaves_pedidas(js, html) - set(textos["en"]))
    assert not faltando, f"chave usada e inexistente: {faltando}"


def test_toda_chave_do_dicionario_e_usada(
    js: str, html: str, textos: dict[str, dict[str, str]]
) -> None:
    """O simétrico. Uma chave que ninguém pede é tradução mantida à toa — e
    costuma ser o rastro de uma string que voltou a ser literal no código."""
    usadas = _chaves_usadas(js, html)
    sobrando = sorted(set(textos["en"]) - usadas)
    assert not sobrando, f"chave no dicionário e nunca usada: {sobrando}"


# ---------------------------------------------------------------------------
# 3. em modo `en`, nada de português no cromo
# ---------------------------------------------------------------------------

#: Marcas de português que não existem em inglês. Acentos e cedilha pegam quase
#: tudo; as palavras funcionais pegam o resto ("de", "para", "com" sem acento).
_ACENTOS = re.compile(r"[áàâãéêíóôõúüçÁÀÂÃÉÊÍÓÔÕÚÜÇ]")
_PALAVRAS_PT = re.compile(
    r"\b(?:não|você|para|pela|pelo|com|sem|uma|dos|das|aqui|isso|"
    r"que|quem|onde|quando|porque|ainda|depois|antes|mesmo|"
    r"anotador|revisor|rubrica|tarefa|fila|prompt_uid)\b",
    re.IGNORECASE,
)

#: A exceção declarada, e a única: comandos de CLI aparecem literalmente nas
#: duas línguas ("run `pf annotate seed`"), porque um comando não se traduz.
_COMANDOS = re.compile(r"`[^`]*`")


def test_o_ingles_nao_tem_portugues(textos: dict[str, dict[str, str]]) -> None:
    """A prova de que o modo `en` abre sem português — na fonte de todo o cromo.

    Se todo texto de cromo sai daqui (e os testes da seção 4 provam que sai),
    então um dicionário inglês sem português é uma tela inglesa sem português.
    """
    sujas = []
    for chave, valor in textos["en"].items():
        limpo = _COMANDOS.sub("", str(valor))
        if _ACENTOS.search(limpo) or _PALAVRAS_PT.search(limpo):
            sujas.append((chave, valor))
    assert not sujas, f"português no dicionário inglês: {sujas}"


def test_o_portugues_continua_portugues(textos: dict[str, dict[str, str]]) -> None:
    """O simétrico grosseiro: o pt não pode ter virado cópia do inglês."""
    iguais = [k for k, v in textos["pt"].items() if v == textos["en"][k]]
    # Siglas e rótulos de idioma são iguais nas duas de propósito ("EN", "en").
    assert len(iguais) < 12, f"tradução esquecida (valor idêntico ao inglês): {sorted(iguais)}"
    com_acento = sum(1 for v in textos["pt"].values() if _ACENTOS.search(str(v)))
    assert com_acento > 100, "o dicionário português perdeu a acentuação"


# ---------------------------------------------------------------------------
# 4. não existe terceira via: todo cromo passa pelo dicionário
# ---------------------------------------------------------------------------


def test_o_html_estatico_nao_carrega_texto(html: str) -> None:
    """O corpo da página não tem frase nenhuma: só `data-t`.

    Um texto solto no HTML é invisível para o toggle — ele ficaria em português
    para sempre, inclusive no modo inglês, e nenhum teste de dicionário o
    pegaria.
    """
    corpo = html.split("<div id=\"app\">", 1)[1].split("<script>", 1)[0]
    # Fora das tags e dos comentários, só pode sobrar espaço em branco e a marca.
    sem_comentario = re.sub(r"<!--.*?-->", "", corpo, flags=re.S)
    solto = re.sub(r"<[^>]+>", "\x00", sem_comentario).replace("\x00", " ")
    restante = solto.strip()
    assert restante == "Bancada", f"texto solto no HTML: {restante[:200]!r}"


def test_o_js_nao_escreve_frase_em_portugues_fora_do_dicionario(js: str) -> None:
    """Nenhum literal acentuado no CÓDIGO — só dentro de `TEXTOS`.

    Este é o teste que faz o de cima valer: sem ele, a próxima tela poderia
    voltar a montar `el("h2", null, "Detalhe")` e a paridade do dicionário
    continuaria verde enquanto a tela quebrava em inglês.
    """
    antes, resto = js.split("const TEXTOS = {", 1)
    depois = resto.split("\n};", 1)[1]
    codigo = antes + depois
    # Comentários e docstrings do código continuam em pt-BR — é a convenção do
    # repo, e eles não chegam ao DOM.
    codigo = re.sub(r"/\*.*?\*/", "", codigo, flags=re.S)
    codigo = re.sub(r"^\s*//.*$", "", codigo, flags=re.M)
    literais = re.findall(r'"((?:[^"\\]|\\.)*)"', codigo)
    acentuados = [s for s in literais if _ACENTOS.search(s)]
    assert not acentuados, f"frase em português solta no código: {acentuados}"


def test_trocar_de_idioma_nao_recarrega_nem_refaz_chamada(js: str) -> None:
    """A troca é um REDESENHO a partir de `est` — não um reload nem um refetch.

    É isto que garante o item de aceite "trocar de idioma não perde rascunho":
    o rascunho vive em `est.form`, e `redesenharTela` desenha `est`.
    """
    assert "function escolherIdioma(lang)" in js
    assert "function redesenharTela()" in js
    troca = js.split("function escolherIdioma(lang)", 1)[1].split("\n}", 1)[0]
    for proibido in ("location.reload", "location.href", "await ", "fetch(", "abrirApp("):
        assert proibido not in troca, f"{proibido!r} na troca de idioma"


def test_o_rascunho_e_o_comentario_de_devolucao_moram_no_estado(js: str) -> None:
    """O que está escrito e ainda não foi enviado precisa estar em `est`.

    Três campos que um redesenho apagaria se vivessem só no DOM: o formulário
    (já era assim), a busca do catálogo e o comentário da devolução.
    """
    assert "tri.comentario = area.value" in js
    assert "area.value = tri.comentario" in js
    assert "est.catalogo.q = busca.value; });" in js


def test_nenhuma_frase_do_servidor_chega_crua_a_tela(js: str) -> None:
    """As rotas falam UMA língua; a tela fala duas.

    Duas frases vinham prontas do backend e apareciam no lugar mais visível que
    existe — o estado vazio da fila e o convite da continuação. Agora a rota
    manda a CHAVE (``motivo_chave``) e a tela escolhe a língua. O campo
    ``motivo`` continua saindo na resposta para que a API se explique em
    ``/docs``, e é justamente por isso que este teste existe: ele proíbe a tela
    de voltar a usá-lo.
    """
    assert "r.motivo_chave" in js and "r.motivo_dados" in js
    assert "aceitarEnvelope(r.tarefa, r.motivo_chave, r.motivo_dados)" in js
    assert "r.motivo," not in js, "a frase em português do servidor voltou para a tela"
    assert 'est.motivoChave ? t(est.motivoChave, est.motivoDados)' in js
    # E o convite da continuação também sai do dicionário, não da rota.
    assert 't("cont.convite")' in js and 't("cont.porque")' in js
    assert "cont.convite" not in js.split("const TEXTOS = {", 1)[1].split("\n};", 1)[1].replace(
        't("cont.convite")', ""
    )


def test_a_rota_da_proxima_manda_chave_e_frase(tmp_path: Path) -> None:
    """O contrato dos dois formatos, contra a app de verdade."""
    from fastapi.testclient import TestClient

    from prompt_factory import db as dbmod
    from prompt_factory.annotate import seed as seedmod
    from prompt_factory.annotate import tarefas as tmod
    from prompt_factory.annotate.main import criar_app

    from .test_api import montar_banco

    corpus = tmp_path / "prompts.sqlite"
    montar_banco(corpus)
    conn = dbmod.connect(tmp_path / "annotate.sqlite")
    corpo = dbmod.connect(corpus, readonly=True)
    try:
        adb.init_db(conn)
        seedmod.semear(conn, corpo)
    finally:
        conn.close()
        corpo.close()
    with TestClient(criar_app(tmp_path / "annotate.sqlite", corpus)) as c:
        quem = next(
            p["id"] for p in c.get("/api/perfis").json()["items"] if p["papel"] == "anotador"
        )
        r = c.post(
            "/api/tarefas/proxima", json={"anotador_id": quem, "tipo": "avaliar_rubrica"}
        ).json()
        # A frase em português continua saindo (a API se explica sozinha)…
        assert r["motivo"]
        # …e a chave também, que é o que a tela bilíngue consome.
        assert r["motivo_chave"] in {
            tmod.CHAVE_MOTIVO_VAZIA, tmod.CHAVE_MOTIVO_SUMIDOS, tmod.CHAVE_MOTIVO_SERVIDA
        }
        assert isinstance(r["motivo_dados"], dict)


def test_as_chaves_de_motivo_do_backend_existem_no_dicionario(
    textos: dict[str, dict[str, str]],
) -> None:
    """As três chaves que a ROTA nomeia têm de existir dos dois lados.

    É o único ponto em que o backend escolhe uma chave do dicionário do front, e
    por isso é o único que os testes de chave órfã não pegariam sozinhos.
    """
    from prompt_factory.annotate import tarefas as tmod

    for chave in (
        tmod.CHAVE_MOTIVO_VAZIA, tmod.CHAVE_MOTIVO_SUMIDOS, tmod.CHAVE_MOTIVO_SERVIDA
    ):
        assert chave in textos["en"], chave
        assert chave in textos["pt"], chave


def test_o_padrao_e_ingles_e_so_o_navegador_em_pt_desvia(js: str) -> None:
    assert 'const IDIOMA_PADRAO = "en"' in js
    inicial = js.split("function idiomaInicial()", 1)[1].split("\n}", 1)[0]
    assert "localStorage.getItem(CHAVE " in inicial      # preferência salva vence
    assert "navigator.languages" in inicial
    assert 'indexOf("pt") === 0 ? "pt" : IDIOMA_PADRAO' in inicial


def test_a_preferencia_persiste(js: str) -> None:
    assert 'localStorage.setItem(CHAVE + "idioma", lang)' in js


def test_o_seletor_de_idioma_esta_na_barra(html: str, js: str) -> None:
    assert 'id="linguas"' in html
    assert "function desenharIdiomas()" in js
    # O `title` de cada botão sai na PRÓPRIA língua dele: quem procura o
    # português não deveria ter de ler inglês para achar o botão.
    assert "TEXTOS[lang][lang === \"pt\" ? \"idioma.pt_title\" : \"idioma.en_title\"]" in js


# ---------------------------------------------------------------------------
# 5. a convenção de língua POR CAMPO, dita na tela
# ---------------------------------------------------------------------------


def test_a_convencao_de_lingua_aparece_nos_campos(js: str) -> None:
    """Metadado em inglês, dado na língua do prompt — e a tela diz isso ONDE a
    dúvida ocorre, não numa página de ajuda."""
    assert "function dicaLinguaMeta(chave)" in js
    assert "function dicaLinguaDado(lang)" in js
    # Os cinco lugares onde a dúvida de fato ocorre.
    assert js.count('dicaLinguaMeta("lingua.meta")') >= 4   # justificativa, geral, A/B, devolução
    assert 'dicaLinguaMeta("lingua.rubrica")' in js         # a rubrica inteira
    assert "dicaLinguaDado(env.prompt.lang)" in js          # a resposta SFT


def test_a_convencao_e_diretriz_e_nao_validacao(js: str) -> None:
    """Nada bloqueia o envio por causa da língua: o anotador pode ter razão para
    desviar, e a plataforma não conhece o motivo dele."""
    falta = js.split("function oQueFalta()", 1)[1].split("\n}\n", 1)[0]
    for proibido in ("lingua", "idioma", "lang"):
        assert proibido not in falta, f"{proibido!r} virou validação em oQueFalta()"


def test_o_selo_do_campo_de_dado_segue_a_lingua_do_prompt(js: str) -> None:
    dica = js.split("function dicaLinguaDado(lang)", 1)[1].split("\n}", 1)[0]
    assert '"lingua.selo_pt"' in dica and '"lingua.selo_en"' in dica
    assert '"lingua.dado_pt"' in dica and '"lingua.dado_en"' in dica


def test_os_modelos_de_rubrica_saem_em_ingles(js: str) -> None:
    """Um critério de rubrica é METADADO. Um modelo em português produziria, a
    cada clique, uma rubrica que nasce violando a convenção que a tela acabou de
    declarar ao lado do campo."""
    modelos = js.split("const MODELOS_RUBRICA = [", 1)[1].split("\n];", 1)[0]
    assert not _ACENTOS.search(modelos), "modelo de rubrica com texto em português"
    assert "The answer does what was asked" in modelos


# ---------------------------------------------------------------------------
# 6. as diretrizes, nas duas línguas, na mesma VERSÃO
# ---------------------------------------------------------------------------


def _banco(tmp_path: Path) -> sqlite3.Connection:
    from prompt_factory import db as dbmod

    conn = dbmod.connect(tmp_path / "annotate.sqlite")
    adb.init_db(conn)
    return conn


def test_o_arquivo_de_diretrizes_cobre_as_duas_linguas() -> None:
    dados = dirmod.carregar()
    assert set(dados["tipos"]) == set(adb.TIPOS_TAREFA)
    for tipo, textos in dados["tipos"].items():
        assert set(textos) == set(dirmod.IDIOMAS), tipo
        for lang in dirmod.IDIOMAS:
            assert textos[lang], f"{tipo}/{lang} vazio"


def test_diretriz_com_uma_lingua_so_e_recusada(tmp_path: Path) -> None:
    """Paridade de língua é do CARREGAMENTO, não da tela: um tipo publicado só
    em inglês deixaria a tela em português muda, e mudo é pior que traduzido
    pela metade."""
    alvo = tmp_path / "diretrizes.json"
    dados = dirmod.carregar()
    dados["tipos"]["comparar_ab"] = {"en": ["only english"]}
    alvo.write_text(json.dumps(dados, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="sem sobra nem falta"):
        dirmod.carregar(alvo)


def test_o_formato_de_lista_do_p3a_e_recusado(tmp_path: Path) -> None:
    alvo = tmp_path / "diretrizes.json"
    dados = dirmod.carregar()
    dados["tipos"]["comparar_ab"] = ["uma linha só, no formato antigo"]
    alvo.write_text(json.dumps(dados, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="diretrizes@1"):
        dirmod.carregar(alvo)


def test_a_versao_e_do_conteudo_e_nao_da_traducao(tmp_path: Path) -> None:
    """`v1` em inglês e `v1` em português são a MESMA regra, e por isso moram na
    mesma linha: ``anotacoes.versao_diretriz`` aponta para a versão, nunca para
    o par (versão, língua)."""
    conn = _banco(tmp_path)
    dirmod.semear(conn)
    linhas = conn.execute("SELECT tipo, versao, texto_json FROM diretrizes").fetchall()
    assert len(linhas) == len(adb.TIPOS_TAREFA)
    for linha in linhas:
        conteudo = json.loads(str(linha["texto_json"]))
        assert conteudo["schema"] == dirmod.SCHEMA_DIRETRIZ
        assert set(conteudo["textos"]) == set(dirmod.IDIOMAS)
    # Uma linha por (tipo, versao) — a língua NÃO multiplica as linhas.
    assert conn.execute("SELECT count(DISTINCT versao) AS n FROM diretrizes").fetchone()["n"] == 1
    conn.close()


def test_a_api_entrega_as_duas_linguas_de_uma_vez(tmp_path: Path) -> None:
    """Trocar de idioma não pode custar uma ida ao servidor."""
    conn = _banco(tmp_path)
    dirmod.semear(conn)
    vigentes = dirmod.vigentes(conn)
    assert set(vigentes) == set(adb.TIPOS_TAREFA)
    for tipo, dados in vigentes.items():
        assert dados["versao"] == 1
        assert set(dados["textos"]) == set(dirmod.IDIOMAS), tipo
        assert all(dados["textos"][lang] for lang in dirmod.IDIOMAS), tipo
    conn.close()


def test_o_banco_do_p3a_e_promovido_sem_perder_a_versao(tmp_path: Path) -> None:
    """O caso REAL: um banco que já gravou trabalho sob a v1 monolíngue.

    Acrescentar a tradução não é editar a regra — o texto em português continua
    palavra por palavra o mesmo —, então a promoção é permitida e a versão não
    sobe. Se subisse, toda anotação com ``versao_diretriz = 1`` passaria a
    apontar para uma regra que não é mais a vigente, por causa de uma tradução.
    """
    conn = _banco(tmp_path)
    pacote = dirmod.carregar()
    for tipo, textos in pacote["tipos"].items():
        antigo = json.dumps(
            {"schema": dirmod.SCHEMA_DIRETRIZ_V1, "linhas": textos[dirmod.IDIOMA_HERDADO]},
            ensure_ascii=False,
        )
        conn.execute(
            "INSERT INTO diretrizes (tipo, versao, texto_json) VALUES (?, 1, ?)", (tipo, antigo)
        )

    promovidas = dirmod.semear(conn)

    assert promovidas == len(adb.TIPOS_TAREFA)
    assert conn.execute("SELECT count(*) AS n FROM diretrizes").fetchone()["n"] == len(
        adb.TIPOS_TAREFA
    )
    assert dirmod.versao_vigente(conn, "comparar_ab") == 1
    vigentes = dirmod.vigentes(conn)
    assert vigentes["comparar_ab"]["textos"]["pt"] == pacote["tipos"]["comparar_ab"]["pt"]
    assert vigentes["comparar_ab"]["textos"]["en"] == pacote["tipos"]["comparar_ab"]["en"]
    # E semear de novo não muda nada.
    assert dirmod.semear(conn) == 0
    conn.close()


def test_mudar_a_regra_continua_proibido(tmp_path: Path) -> None:
    """A promoção é estreita de propósito: ela só aceita quando o português
    continua idêntico. Regra diferente na mesma versão é o erro que o P3a já
    recusava, e ele continua recusado."""
    conn = _banco(tmp_path)
    pacote = dirmod.carregar()
    for tipo, textos in pacote["tipos"].items():
        adulterado = list(textos[dirmod.IDIOMA_HERDADO])
        adulterado[0] = "uma regra diferente, escrita por cima da que gravou trabalho"
        conn.execute(
            "INSERT INTO diretrizes (tipo, versao, texto_json) VALUES (?, 1, ?)",
            (tipo, json.dumps({"schema": dirmod.SCHEMA_DIRETRIZ_V1, "linhas": adulterado},
                              ensure_ascii=False)),
        )
    with pytest.raises(RuntimeError, match="não se edita"):
        dirmod.semear(conn)
    conn.close()


# ---------------------------------------------------------------------------
# 7. o pacote de demonstração: estrutura bilíngue sobre dado monolíngue
# ---------------------------------------------------------------------------


def test_o_seed_traduz_uma_fixture_ja_semeada_sem_mexer_no_canonico(tmp_path: Path) -> None:
    """O caso REAL: o banco do dono já tem as oito rubricas na forma monolíngue.

    ``semear_pacote`` é idempotente pela chave natural, então "já existe, pula"
    as deixaria monolíngues para sempre — a interface abriria em inglês com
    rubricas em português. A atualização é permitida porque o CANÔNICO é
    idêntico: só os sidecares de exibição entram.
    """
    from prompt_factory.annotate import seed as seedmod

    conn = _banco(tmp_path)
    pacote = seedmod.carregar_pacote()

    # Semeia a versão MONOLÍNGUE (como o P3a deixou o banco).
    monolingue = json.loads(json.dumps(pacote))
    for item in monolingue["itens"]:
        item["rubrica"] = json.loads(
            json.dumps(seedmod._canonico(item["rubrica"]))
        )
        for r in item["respostas"]:
            r["meta"] = seedmod._canonico(r["meta"])
    seedmod.semear_pacote(conn, monolingue)
    antes = conn.execute(
        "SELECT titulo, criterios_json FROM rubricas ORDER BY titulo"
    ).fetchall()
    assert all("_i18n" not in str(linha["criterios_json"]) for linha in antes)

    conta = seedmod.semear_pacote(conn)

    assert conta["traduzidas"] == len(pacote["itens"]) * 3  # 1 rubrica + 2 respostas
    assert conta["rubricas"] == 0 and conta["respostas_modelo"] == 0
    depois = conn.execute("SELECT titulo, criterios_json FROM rubricas ORDER BY titulo").fetchall()
    # Os TÍTULOS (a identidade) não mudaram, e nenhuma rubrica foi duplicada.
    assert [linha["titulo"] for linha in depois] == [linha["titulo"] for linha in antes]
    for velho, novo in zip(antes, depois, strict=True):
        assert "_i18n" in str(novo["criterios_json"])
        assert seedmod._canonico(json.loads(str(velho["criterios_json"]))) == seedmod._canonico(
            json.loads(str(novo["criterios_json"]))
        )
    # E de novo é no-op.
    assert seedmod.semear_pacote(conn)["traduzidas"] == 0
    conn.close()


def test_o_seed_nao_reescreve_uma_fixture_cujo_criterio_mudou(tmp_path: Path) -> None:
    """A brecha da tradução é estreita: mudar um critério continua proibido.

    O nome do critério é a identidade que ``anotacoes.payload_json`` grava em
    ``notas[].criterio``. Trocá-lo por baixo de uma anotação já gravada faria a
    triagem abrir o formulário sem nota nenhuma.
    """
    from prompt_factory.annotate import seed as seedmod

    conn = _banco(tmp_path)
    seedmod.semear_pacote(conn)
    linha = conn.execute("SELECT id, criterios_json FROM rubricas LIMIT 1").fetchone()
    conteudo = json.loads(str(linha["criterios_json"]))
    conteudo["criterios"][0]["nome"] = "um critério com outro nome"
    conn.execute(
        "UPDATE rubricas SET criterios_json = ? WHERE id = ?",
        (json.dumps(conteudo, ensure_ascii=False), int(linha["id"])),
    )

    seedmod.semear_pacote(conn)

    depois = json.loads(
        str(conn.execute("SELECT criterios_json FROM rubricas WHERE id = ?", (int(linha["id"]),))
            .fetchone()["criterios_json"])
    )
    assert depois["criterios"][0]["nome"] == "um critério com outro nome"
    conn.close()


def test_o_pacote_tem_ingles_no_topo_das_duas_filas() -> None:
    """Um avaliador que não lê português tem de conseguir TRABALHAR, e não só
    ler a interface traduzida. Nas duas filas de tarefa do pacote, um item em
    inglês está entre os dois primeiros."""
    from prompt_factory.annotate import seed as seedmod

    pacote = seedmod.carregar_pacote()
    lang = {i["chave"]: i["lang"] for i in pacote["itens"]}
    for tipo in ("avaliar_rubrica", "comparar_ab"):
        do_tipo = sorted(
            (t for t in pacote["tarefas"] if t["tipo"] == tipo),
            key=lambda t: -int(t["prioridade"]),
        )
        assert "en" in [lang[t["item"]] for t in do_tipo[:2]], tipo


def test_um_banco_sem_seed_do_p3i_ainda_desenha_alguma_coisa(tmp_path: Path) -> None:
    """Texto na língua errada é ruim; NENHUM texto é pior — e o seed conserta na
    primeira execução."""
    conn = _banco(tmp_path)
    conn.execute(
        "INSERT INTO diretrizes (tipo, versao, texto_json) VALUES ('comparar_ab', 1, ?)",
        (json.dumps({"schema": dirmod.SCHEMA_DIRETRIZ_V1, "linhas": ["regra velha"]}),),
    )
    vigentes = dirmod.vigentes(conn)
    assert vigentes["comparar_ab"]["textos"] == {"en": ["regra velha"], "pt": ["regra velha"]}
    conn.close()


# ---------------------------------------------------------------------------
# 8. o pool nas duas línguas — e a licença continuando a valer
# ---------------------------------------------------------------------------


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    from .test_api import montar_banco

    caminho = tmp_path / "prompts.sqlite"
    montar_banco(caminho)
    return caminho


def test_pool_fallback_lang_aceita_string_e_lista() -> None:
    """Compatibilidade: todo ``settings.toml`` de antes do P3i trazia uma string."""
    from prompt_factory.annotate import pool as poolmod

    assert poolmod.normalizar_langs("pt") == ("pt",)
    assert poolmod.normalizar_langs(["pt", "en"]) == ("pt", "en")
    # Ordem preservada (ela é a precedência das cotas) e duplicata fora.
    assert poolmod.normalizar_langs(["en", "pt", "en"]) == ("en", "pt")
    assert poolmod.normalizar_langs([]) == ("pt",)


def test_a_cota_e_igual_por_lingua_e_a_sobra_e_redistribuida() -> None:
    """Proporcional ao corpus deixaria o pool monolíngue — são 116.051 linhas em
    inglês contra uma fração disso em português."""
    from prompt_factory.annotate.pool import _cotas

    # Material de sobra nas duas: metade para cada.
    assert _cotas(("pt", "en"), 500, {"pt": 9000, "en": 9000}) == {"pt": 250, "en": 250}
    # O resto ímpar vai para a primeira da ordem DECLARADA.
    assert _cotas(("pt", "en"), 501, {"pt": 9000, "en": 9000}) == {"pt": 251, "en": 250}
    # Uma língua sem material devolve a diferença — o pool não encolhe por causa dela.
    assert _cotas(("pt", "en"), 500, {"pt": 40, "en": 9000}) == {"pt": 40, "en": 460}
    # Com UMA língua o resultado é o de antes do P3i: min(teto, disponível).
    assert _cotas(("pt",), 500, {"pt": 9000}) == {"pt": 500}
    assert _cotas(("pt",), 500, {"pt": 12}) == {"pt": 12}
    # Sem material nenhum não entra em laço.
    assert _cotas(("pt", "en"), 500, {"pt": 0, "en": 0}) == {"pt": 0, "en": 0}


def test_o_pool_bilingue_traz_as_duas_linguas_e_continua_deterministico(
    corpus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prompt_factory import config
    from prompt_factory import db as dbmod
    from prompt_factory.annotate import pool as poolmod

    monkeypatch.setitem(config.settings()["annotate"], "pool_fallback_lang", ["pt", "en"])
    monkeypatch.setitem(config.settings()["annotate"], "pool_fallback_min_chars", 10)
    conn = dbmod.connect(corpus, readonly=True)
    try:
        primeira = poolmod.resolver(conn, com_uids=True)
        segunda = poolmod.resolver(conn, com_uids=True)
        assert primeira.uids == segunda.uids, "o pool deixou de ser determinístico"
        assert len(primeira.uids) == primeira.n_pool, "o health mentiria sobre o catálogo"
        marcas = ", ".join("?" * len(primeira.uids))
        langs = {
            str(linha["lang"])
            for linha in conn.execute(
                f"SELECT lang FROM prompts WHERE uid IN ({marcas})", primeira.uids
            )
        }
        assert langs == {"pt", "en"}, f"o pool bilíngue saiu com {langs}"
    finally:
        conn.close()


def test_a_politica_de_licenca_continua_valendo_com_a_lista(
    corpus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O defeito 1.1 do P3a, agora pelo caminho novo.

    Antes ele aparecia ao trocar ``pool_fallback_lang`` para ``"en"``. Com a
    lista, o inglês entra por CONFIGURAÇÃO PADRÃO — e é a política de licença,
    não o filtro de idioma, que mantém as 9.148 linhas ``cc-by-nc-4.0`` de fora.
    """
    from prompt_factory import config
    from prompt_factory import db as dbmod
    from prompt_factory.annotate import pool as poolmod

    monkeypatch.setitem(config.settings()["annotate"], "pool_fallback_lang", ["pt", "en"])
    monkeypatch.setitem(config.settings()["annotate"], "pool_fallback_min_chars", 10)
    conn = dbmod.connect(corpus, readonly=True)
    try:
        def licencas() -> dict[str, int]:
            uids = poolmod.resolver(conn, com_uids=True).uids
            marcas = ", ".join("?" * len(uids))
            return {
                str(linha["license"]): int(linha["n"])
                for linha in conn.execute(
                    f"SELECT license, count(*) AS n FROM prompts WHERE uid IN ({marcas}) "
                    "GROUP BY license",
                    uids,
                )
            }

        com_politica = licencas()
        assert com_politica, "o pool bilíngue não pode sair vazio"
        assert com_politica.get("cc-by-nc-4.0", 0) == 0
        assert poolmod.resolver(conn).excluidas_licenca >= 1
        # E é MESMO a política que a exclui.
        monkeypatch.setitem(config.settings()["annotate"], "pool_exigir_commercial_ok", False)
        assert licencas().get("cc-by-nc-4.0", 0) >= 1
    finally:
        conn.close()


def test_o_painel_do_admin_nao_recebe_frase_pronta_do_servidor(
    corpus: Path, tmp_path: Path
) -> None:
    """As quatro frases diagnósticas do pool saem como CHAVE + dados.

    Este painel é onde a proveniência fica visível — a segunda coisa que o brief
    manda o avaliador procurar. Deixá-lo em português numa tela em inglês tiraria
    dele exatamente o que ele veio ler.
    """
    from fastapi.testclient import TestClient

    from prompt_factory.annotate import pool as poolmod
    from prompt_factory.annotate.main import criar_app

    with TestClient(criar_app(tmp_path / "annotate.sqlite", corpus)) as c:
        p = c.get("/api/health").json()["pool"]
    # A frase em português continua saindo (a API se explica em /docs)…
    assert p["motivo"] and p["filtro_fallback"] and p["nota_nsfw"]
    # …e a chave também, que é o que a tela consome.
    assert p["motivo_chave"].startswith("pool.")
    assert isinstance(p["motivo_dados"], dict) and p["motivo_dados"].get("colecao")
    assert p["filtro"]["chave"] in (
        poolmod.T_FILTRO_UMA_LINGUA, poolmod.T_FILTRO_VARIAS_LINGUAS
    )
    assert p["politica_licenca"]["descricao_chave"].startswith("pool.")
    assert p["nota_nsfw_chave"] == poolmod.T_NOTA_NSFW


def test_um_pool_materializado_antes_do_p3i_nao_mostra_marcador_cru(tmp_path: Path) -> None:
    """Um banco cujo pool foi materializado antes deste marco não tem a chave
    gravada, e a próxima materialização só vem quando o corpus mudar. O default
    tem de sair de ``app_meta``, e não como ``{colecao}`` cru na tela."""
    from prompt_factory.annotate import db as adb_
    from prompt_factory.annotate import pool as poolmod

    conn = _banco(tmp_path)
    adb_.set_meta(conn, poolmod.CHAVE_ORIGEM, poolmod.FALLBACK)
    adb_.set_meta(conn, poolmod.CHAVE_COLECAO, "anotacao")
    adb_.set_meta(conn, poolmod.CHAVE_MOTIVO, "frase antiga em português")
    p = poolmod._do_banco(conn)
    assert p.motivo_chave == poolmod.T_MOTIVO_SEM_COLECAO
    assert p.motivo_dados == {"colecao": "anotacao"}
    conn.close()


def test_as_chaves_do_runtime_de_modelos_existem_no_dicionario(
    textos: dict[str, dict[str, str]],
) -> None:
    """P4d: o backend escolhe a chave do estado do runtime, como faz com o pool.

    E os MARCADORES de cada uma têm de ser preenchíveis pelos dados que a
    exceção correspondente carrega — foi assim que ``{url}`` apareceu literal na
    tela: uma falha herdou a chave de outra, cujos dados não tinham ``url``.
    """
    from prompt_factory.annotate import modelos as modmod

    chaves = [v for k, v in vars(modmod).items() if k.startswith("T_") and isinstance(v, str)]
    assert len(chaves) == 6, "acrescentou uma frase e esqueceu de listá-la?"
    for chave in chaves:
        assert chave in textos["en"], chave
        assert chave in textos["pt"], chave
    # A genérica não promete marcador nenhum: ela é o destino de toda falha
    # imprevista, e uma promessa que os dados não cumprem vira `{x}` na tela.
    assert not re.search(r"\{\w+\}", textos["en"][modmod.T_FALHOU])


def test_as_chaves_de_pool_do_backend_existem_no_dicionario(
    textos: dict[str, dict[str, str]],
) -> None:
    from prompt_factory.annotate import pool as poolmod

    chaves = [v for k, v in vars(poolmod).items() if k.startswith("T_") and isinstance(v, str)]
    assert len(chaves) == 12, "acrescentou uma frase e esqueceu de listá-la?"
    for chave in chaves:
        assert chave in textos["en"], chave
        assert chave in textos["pt"], chave


def test_a_convencao_dos_artefatos_de_entrega_esta_registrada() -> None:
    """O P5b constrói dataset card, relatório de qualidade e auditoria. A língua
    deles foi decidida AQUI, no P3i, e mora onde o P5b vai encontrá-la."""
    from prompt_factory import export

    assert export.IDIOMA_DOS_ARTEFATOS == "en"
    assert "IDIOMA_DOS_ARTEFATOS" in export.__all__


def test_o_filtro_do_fallback_declara_as_duas_linguas(monkeypatch: pytest.MonkeyPatch) -> None:
    """O painel do admin mostra esta frase. Um pool bilíngue que se descreve como
    monolíngue seria a mesma mentira de interface que o pool trocando em silêncio."""
    from prompt_factory import config
    from prompt_factory.annotate import pool as poolmod

    monkeypatch.setitem(config.settings()["annotate"], "pool_fallback_lang", ["pt", "en"])
    frase = poolmod.descrever_fallback()
    assert "idiomas pt, en" in frase
    assert "cota igual por língua" in frase
    monkeypatch.setitem(config.settings()["annotate"], "pool_fallback_lang", "pt")
    assert poolmod.descrever_fallback().startswith("idioma pt,")
