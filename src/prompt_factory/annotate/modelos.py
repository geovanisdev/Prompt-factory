"""A camada de inferência da Bancada: falar com os modelos LOCAIS, por HTTP.

**Este módulo não importa torch, nem transformers, nem nada de ML.** Ele fala
com o **Ollama**, um servidor separado, por HTTP em ``127.0.0.1``. O motivo é
específico deste projeto e não é preferência de estilo: o ``torch`` do venv é
**CPU** e foi ele que gerou os embeddings do corpus (s05). Trocá-lo por
torch-CUDA para rodar um modelo aqui dentro arriscaria a reprodutibilidade do
s05 e a invariante posicional de que a busca semântica depende — dois artefatos
de horas de máquina, por causa de uma aba de conversa. O Ollama fica do lado de
fora, com a GPU, e a fronteira entre os dois é uma requisição HTTP.

MODELO AUSENTE É ESTADO, NÃO É ERRO
===================================
A máquina que abre esta plataforma pode não ter o Ollama rodando, ou pode tê-lo
rodando sem os modelos baixados (são ~5 GB cada; o download leva minutos). Os
dois casos são **normais** e a tela precisa dizer o que falta **e o comando que
conserta** (``ollama pull gemma3n:e4b``). Um 500 aqui transformaria "faltou
baixar um arquivo" em "a plataforma está quebrada", que é a leitura errada e a
mais cara.

Por isso ``estado()`` **nunca levanta**: ela devolve um retrato, com ``ok``
falso e o motivo, e quem a chama desenha o retrato.

O TEXTO QUE VOLTA DO MODELO É DADO, NUNCA INSTRUÇÃO
====================================================
O prompt vem do corpus (que contém ``<script>`` de verdade, e contém texto que
manda o leitor ignorar instruções anteriores) e a resposta vem de um modelo que
leu esse prompt. **Nada neste módulo, nas rotas ou na tela interpreta a saída do
modelo como comando**: ela é gravada como texto, devolvida como texto e
desenhada por ``textContent``. Não há parser de "ação", não há ferramenta
exposta ao modelo, não há campo do JSON de saída que vire caminho de código.

NADA AQUI CONSERTA O MODELO
===========================
**Sem prompt de sistema, sem instrução escondida, sem filtro na saída.** É a
regra mais importante deste módulo, e ela é contraintuitiva: medido nesta
máquina, o ``qwen3:4b`` **ignorou a restrição de tamanho** ("em duas frases") e
**respondeu em inglês a uma pergunta em português**, enquanto o ``gemma3n:e4b``
respeitou as duas. Um ``system`` mandando responder em português e ser conciso
apagaria exatamente o sinal que o anotador existe para medir — e o duelo viraria
dois textos indistinguíveis. As famílias de defeito são o produto, não o
problema. Se um dia houver prompt de sistema, ele vem da TAREFA e fica visível e
editável na tela; nunca escondido aqui.

Pelo mesmo motivo o ``message.thinking`` do Ollama **não é descartado**: ele é
gravado ao lado do turno e a tela o mostra recolhido, com marcador. Filtrar em
silêncio seria decidir pelo anotador que o raciocínio não conta.

O QUE ESTE MÓDULO CONTROLA, E POR QUÊ
=====================================
Só duas coisas, iguais para os dois modelos (equilibrar o duelo por parâmetro
seria conserto por outro caminho):

* ``num_predict`` — teto de tokens do turno. Medido: o ``qwen3:4b`` gera ~2.000
  tokens para uma pergunta que pede duas frases, e um turno de 17 s com uma
  parede de texto torna a aba insuportável. O teto é **nosso**, não do modelo, e
  por isso o turno truncado sai **marcado** (``truncado``): a tela diz que a
  parede foi cortada por nós, em vez de deixar parecer que o modelo parou ali.
* ``timeout`` — 180 s por turno. Estourar é **estado tratável**: o turno do
  humano já está gravado, e a tela oferece "tentar de novo" sobre ele. Uma
  conversa de trinta turnos não se perde por causa de um relógio.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from ..config import get as _cfg

#: Onde o Ollama atende. ``127.0.0.1``, nunca ``0.0.0.0``: a plataforma é local,
#: e um runtime de inferência exposto na rede é superfície de ataque de graça.
URL_PADRAO = "http://127.0.0.1:11434"

#: Os dois modelos do duelo. Gerações e famílias diferentes, portes parecidos,
#: ambos com português decente — as diferenças que o anotador vai julgar são de
#: FAMÍLIA, não de porte. O primeiro é o default da conversa de um modelo só.
MODELOS_PADRAO: tuple[str, ...] = ("gemma3n:e4b", "qwen3:4b")

#: Timeout de uma GERAÇÃO. Folgado o bastante para a primeira chamada depois de
#: uma subida (que carrega de 2,5 a 7,5 GB para a VRAM antes do primeiro token) e
#: curto o bastante para não deixar a aba pendurada. Medido nesta máquina, com
#: os dois modelos frios: 1,4 s no ``gemma3n:e4b`` e 15,1 s no ``qwen3:4b``.
TIMEOUT_PADRAO_S = 180

#: Teto de tokens de UM turno. Ver o cabeçalho: é um teto NOSSO, e o turno que
#: bate nele sai MARCADO como truncado.
#:
#: **Ele existe contra o turno que não termina, não contra o modelo prolixo.**
#: Apertá-lo para forçar concisão seria consertar o modelo por outro caminho — e
#: a verbosidade é justamente um dos sinais que o anotador vai julgar. Medido no
#: navegador com 800: o ``qwen3:4b`` gastava o teto inteiro no raciocínio e
#: devolvia turno VAZIO, o que não é "modelo prolixo", é "modelo mudo por culpa
#: nossa". 2.000 é o que ele consome para responder uma frase, e é folga
#: suficiente para a resposta caber depois do raciocínio. ``0`` desliga o teto.
NUM_PREDICT_PADRAO = 2000

#: Timeout da SONDA (``/api/tags``). Curto: ela está no caminho do
#: ``/api/health``, que a interface chama a cada troca de tela. Se o Ollama não
#: responde em três segundos ele não vai gerar em trezentos.
TIMEOUT_SONDA_S = 3

#: O comando que conserta um modelo ausente. É um COMANDO, e por isso sai igual
#: nas duas línguas (é a exceção declarada do dicionário do front).
COMANDO_PULL = "ollama pull {modelo}"

#: E o que conserta um servidor fora do ar.
COMANDO_SERVE = "ollama serve"

#: As chaves do dicionário da TELA para as frases deste módulo. A rota devolve a
#: frase em português (para a API se explicar em ``/docs`` e para o terminal) e a
#: CHAVE (para a tela, que fala duas línguas). Mesmo contrato de ``pool.py``.
T_FORA_DO_AR = "modelos.fora_do_ar"
T_FALTAM = "modelos.faltam"
T_PRONTO = "modelos.pronto"
T_NENHUM = "modelos.nenhum"
T_FALHOU = "modelos.falhou"
T_SEM_RESPOSTA = "modelos.sem_resposta"


class FalhaDeInferencia(RuntimeError):
    """Base das falhas desta camada. Sempre com a frase de conserto junto.

    ``chave`` é a do dicionário da TELA, e cada subclasse tem a sua **com os
    marcadores que os próprios ``dados`` preenchem**. Herdar a chave de outra
    falha é o defeito que apareceu no navegador: uma resposta vazia do modelo
    aparecia como "o servidor não responde em {url}", com o ``{url}`` literal na
    tela, porque os dados daquela falha não tinham ``url`` nenhuma.
    """

    #: A chave do dicionário da tela. Subclasses a especializam.
    chave = T_FALHOU

    def __init__(self, mensagem: str, **dados: Any) -> None:
        self.dados = dados
        super().__init__(mensagem)


class OllamaIndisponivel(FalhaDeInferencia):
    """O servidor não respondeu (não está de pé, ou está em outra porta)."""

    chave = T_FORA_DO_AR


class ModeloAusente(FalhaDeInferencia):
    """O servidor respondeu, mas aquele modelo não foi baixado nesta máquina."""

    chave = T_FALTAM


class SemResposta(FalhaDeInferencia):
    """O modelo gastou o teto de tokens no raciocínio e não chegou a responder.

    Estado com conserto conhecido (subir ``ollama_num_predict``), e por isso uma
    exceção própria: ele não é "o modelo falhou" — é o NOSSO teto cortando o
    turno antes da resposta, e a tela precisa dizer exatamente isso.
    """

    chave = T_SEM_RESPOSTA


# ---------------------------------------------------------------------------
# configuração
# ---------------------------------------------------------------------------


def base_url() -> str:
    """A URL do Ollama, sem barra no fim."""
    return str(_cfg("annotate", "ollama_url", default=URL_PADRAO)).rstrip("/")


def modelos_configurados() -> tuple[str, ...]:
    """Os modelos que esta instalação espera ter. Aceita string ou lista."""
    bruto = _cfg("annotate", "ollama_modelos", default=list(MODELOS_PADRAO))
    if isinstance(bruto, str):
        bruto = [bruto]
    nomes: list[str] = []
    for x in bruto:
        nome = str(x).strip()
        # Sem duplicata: o duelo pede DOIS modelos, e a mesma família nos dois
        # lados produziria uma preferência sobre a temperatura, não sobre o texto.
        if nome and nome not in nomes:
            nomes.append(nome)
    return tuple(nomes)


def timeout_s() -> int:
    return int(_cfg("annotate", "ollama_timeout_s", default=TIMEOUT_PADRAO_S))


def temperatura() -> float:
    return float(_cfg("annotate", "ollama_temperature", default=0.7))


def num_ctx() -> int:
    return int(_cfg("annotate", "ollama_num_ctx", default=4096))


def num_predict() -> int:
    """Teto de tokens do turno. ``<= 0`` desliga (o Ollama entende ``-1``)."""
    valor = int(_cfg("annotate", "ollama_num_predict", default=NUM_PREDICT_PADRAO))
    return valor if valor > 0 else -1


def max_turnos() -> int:
    """Teto de turnos de uma conversa (contando os do humano e os do modelo).

    Existe por dois motivos, e o segundo é o que importa: uma conversa sem teto
    cresce o histórico a cada turno, e o histórico inteiro volta para o modelo em
    toda geração — o custo por turno é quadrático. O primeiro é humano: uma
    conversa de 80 turnos não é avaliável com rubrica.
    """
    return int(_cfg("annotate", "max_turnos_conversa", default=24))


# ---------------------------------------------------------------------------
# HTTP — a ÚNICA costura com o mundo
# ---------------------------------------------------------------------------


def _http(caminho: str, corpo: dict[str, Any] | None, timeout: float) -> dict[str, Any]:
    """A única função deste pacote que abre um socket. **É ela que os testes trocam.**

    Uma costura só, e explícita: com duas (uma para ``/api/tags`` e outra para
    ``/api/chat``) um teste que trocasse uma e esquecesse a outra passaria
    dependendo de o Ollama estar de pé na máquina de quem rodou — que é
    exatamente o tipo de teste que só falha na máquina de outra pessoa.

    ``urllib`` e não ``requests``: é HTTP em ``127.0.0.1``, sem TLS, e portanto
    sem nada a ver com o bundle de CA que o resto do projeto precisa manter
    (ver ``certs.ensure_ca_bundle``). Uma dependência a menos no caminho.
    """
    dados = None if corpo is None else json.dumps(corpo, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        base_url() + caminho,
        data=dados,
        headers={"Content-Type": "application/json"},
        method="GET" if dados is None else "POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resposta:
            bruto = resposta.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        # O Ollama responde 404 com {"error": "model ... not found"} — um estado
        # de primeira classe, e não uma falha de transporte.
        detalhe = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        raise _do_http(exc.code, detalhe) from exc
    # `socket.timeout` é alias de `TimeoutError` desde o 3.10, e `URLError` já é
    # `OSError`: a tupla é redundante de propósito só onde o nome ajuda a ler.
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise OllamaIndisponivel(
            f"o Ollama não respondeu em {base_url()} ({exc}). "
            f"Suba-o com `{COMANDO_SERVE}`.",
            url=base_url(),
        ) from exc
    try:
        conteudo = json.loads(bruto) if bruto else {}
    except ValueError as exc:  # pragma: no cover - servidor devolvendo lixo
        raise FalhaDeInferencia(
            f"o Ollama respondeu algo que não é JSON ({bruto[:120]!r})"
        ) from exc
    return conteudo if isinstance(conteudo, dict) else {}


def _do_http(codigo: int, detalhe: str) -> FalhaDeInferencia:
    """Traduz o corpo de um erro HTTP do Ollama na exceção certa."""
    try:
        msg = str(json.loads(detalhe).get("error") or detalhe)
    except ValueError:
        msg = detalhe
    if codigo == 404 or "not found" in msg.lower():
        modelo = _modelo_do_erro(msg)
        return ModeloAusente(
            f"o modelo {modelo!r} não está baixado nesta máquina. "
            f"Baixe com `{COMANDO_PULL.format(modelo=modelo)}`.",
            modelo=modelo,
            comando=COMANDO_PULL.format(modelo=modelo),
        )
    return FalhaDeInferencia(f"o Ollama recusou a chamada (HTTP {codigo}): {msg[:300]}")


def _modelo_do_erro(msg: str) -> str:
    """``model "qwen3:4b" not found`` → ``qwen3:4b``. Best-effort, nunca levanta."""
    partes = msg.replace('"', " ").replace("'", " ").split()
    for i, palavra in enumerate(partes):
        if palavra == "model" and i + 1 < len(partes):
            return partes[i + 1]
    return partes[0] if partes else "?"


# ---------------------------------------------------------------------------
# leitura: o que existe nesta máquina
# ---------------------------------------------------------------------------


def listar(timeout: float | None = None) -> list[dict[str, Any]]:
    """Os modelos instalados. Levanta ``OllamaIndisponivel`` se o servidor sumiu.

    ``digest`` sai daqui e é o que faz a preferência valer daqui a seis meses:
    ``qwen3:4b`` é uma TAG, e uma tag é reescrita quando o autor republica o
    modelo. O digest é o conteúdo.
    """
    bruto = _http("/api/tags", None, TIMEOUT_SONDA_S if timeout is None else timeout)
    saida: list[dict[str, Any]] = []
    for m in bruto.get("models") or []:
        if not isinstance(m, dict):  # pragma: no cover - servidor fora do contrato
            continue
        detalhes = m.get("details") or {}
        saida.append(
            {
                "nome": str(m.get("name") or m.get("model") or ""),
                "digest": str(m.get("digest") or ""),
                "tamanho": int(m.get("size") or 0),
                "familia": str(detalhes.get("family") or ""),
                "parametros": str(detalhes.get("parameter_size") or ""),
                "capacidades": [str(c) for c in (m.get("capabilities") or [])],
            }
        )
    return sorted(saida, key=lambda x: x["nome"])


def estado(timeout: float | None = None) -> dict[str, Any]:
    """O retrato do runtime. **Nunca levanta** — ver o cabeçalho do módulo.

    Devolve, além do inventário: ``faltando`` (os configurados que não estão
    instalados) e ``comandos`` (uma linha de ``ollama pull`` por ausente). É a
    tela dizendo o que falta e como consertar, em vez de um 500 dizendo que a
    plataforma quebrou.
    """
    esperados = modelos_configurados()
    retrato: dict[str, Any] = {
        "url": base_url(),
        "ok": False,
        "configurados": list(esperados),
        "instalados": [],
        "faltando": list(esperados),
        "comandos": [COMANDO_PULL.format(modelo=m) for m in esperados],
        "timeout_s": timeout_s(),
        "erro": "",
        "erro_chave": T_FORA_DO_AR,
        "erro_dados": {"url": base_url(), "comando": COMANDO_SERVE},
    }
    try:
        instalados = listar(timeout)
    except FalhaDeInferencia as exc:
        retrato["erro"] = str(exc)
        return retrato

    nomes = {m["nome"] for m in instalados}
    # `qwen3:4b` e `qwen3` são o mesmo modelo para o Ollama (`:latest` implícito).
    # Sem esta normalização, um settings.toml escrito sem a tag diria "faltando"
    # sobre um modelo que está bem ali.
    def presente(alvo: str) -> bool:
        return alvo in nomes or (alvo + ":latest") in nomes

    faltando = [m for m in esperados if not presente(m)]
    retrato.update(
        {
            "instalados": instalados,
            "faltando": faltando,
            "comandos": [COMANDO_PULL.format(modelo=m) for m in faltando],
            "ok": not faltando and bool(esperados),
            "erro_dados": {"n": len(faltando), "modelos": ", ".join(faltando)},
        }
    )
    if faltando:
        retrato["erro"] = (
            f"{len(faltando)} modelo(s) ainda não baixado(s): {', '.join(faltando)}. "
            + "  ".join(retrato["comandos"])
        )
        retrato["erro_chave"] = T_FALTAM
    elif not esperados:  # pragma: no cover - settings.toml com a lista vazia
        retrato["erro"] = "nenhum modelo configurado em [annotate] ollama_modelos"
        retrato["erro_chave"] = T_NENHUM
        retrato["erro_dados"] = {}
    else:
        retrato["erro_chave"] = T_PRONTO
        retrato["erro_dados"] = {"n": len(esperados)}
    return retrato


def digests(instalados: list[dict[str, Any]] | None = None) -> dict[str, str]:
    """``{nome: digest}`` — lido UMA vez por turno e passado a cada geração.

    Uma consulta por turno, e não uma por resposta: no duelo são duas gerações
    contra o mesmo inventário, e ele não muda entre elas.
    """
    lista = listar() if instalados is None else instalados
    mapa = {m["nome"]: m["digest"] for m in lista}
    # A forma sem tag também resolve, pelo motivo de `presente()` acima.
    for nome, digest in list(mapa.items()):
        if nome.endswith(":latest"):
            mapa.setdefault(nome[: -len(":latest")], digest)
    return mapa


# ---------------------------------------------------------------------------
# geração
# ---------------------------------------------------------------------------


def conversar(
    modelo: str,
    mensagens: list[dict[str, str]],
    *,
    digest: str = "",
    timeout: float | None = None,
) -> dict[str, Any]:
    """Um turno de resposta. Devolve ``{texto, raciocinio, modelo, digest, ...}``.

    ``mensagens`` é o histórico inteiro no formato do Ollama
    (``{"role": "user"|"assistant", "content": ...}``) — o servidor não guarda
    estado de conversa, e mandar só o último turno produziria um modelo com
    amnésia, que é justamente o que a rubrica multi-turno mede.

    **Sem ``system``, e sem nada mais**: nenhuma instrução nossa entra na
    conversa. O que o modelo lê é o que o anotador escreveu — ver o cabeçalho.
    """
    opcoes: dict[str, Any] = {
        # Iguais para os dois modelos, sempre. A assimetria entre eles é o dado.
        "temperature": temperatura(),
        "num_ctx": num_ctx(),
        "num_predict": num_predict(),
    }
    corpo: dict[str, Any] = {
        "model": modelo,
        "messages": mensagens,
        "stream": False,
        "options": opcoes,
    }
    inicio = time.monotonic()
    bruto = _http("/api/chat", corpo, timeout_s() if timeout is None else timeout)
    ms = int((time.monotonic() - inicio) * 1000)
    msg = bruto.get("message") or {}
    texto = str(msg.get("content") or "")
    # O RACIOCÍNIO NÃO É DESCARTADO. Ver o cabeçalho: filtrá-lo em silêncio seria
    # decidir pelo anotador que ele não conta. Ele sai à parte, e a tela o mostra
    # recolhido — separado da resposta porque não é a resposta.
    raciocinio = str(msg.get("thinking") or "")
    motivo = str(bruto.get("done_reason") or "")

    if not texto.strip():
        if raciocinio.strip():
            # O teto de tokens foi inteiro para o rascunho. É estado tratável,
            # com conserto conhecido — e não "o modelo falhou". Medido: o
            # `qwen3:4b` gasta ~2.000 tokens de raciocínio para responder uma
            # frase, e com o teto em 800 ele nunca chegava à resposta.
            raise SemResposta(
                f"{modelo} gastou o teto de {num_predict()} tokens no raciocínio e não "
                "chegou a responder. Suba `[annotate] ollama_num_predict`.",
                modelo=modelo,
                n=num_predict(),
            )
        raise FalhaDeInferencia(
            f"{modelo} devolveu uma resposta vazia (done_reason={motivo!r})",
            modelo=modelo,
        )
    return {
        "texto": texto,
        "raciocinio": raciocinio,
        # O nome que o SERVIDOR reporta, não o que pedimos: se o Ollama resolveu
        # `qwen3` para `qwen3:4b`, quem fica gravado é o que de fato respondeu.
        "modelo": str(bruto.get("model") or modelo),
        "digest": digest,
        "duracao_ms": ms,
        "tokens": int(bruto.get("eval_count") or 0),
        # NOSSO teto cortou o turno? A tela marca, para não parecer que o modelo
        # decidiu parar no meio da frase.
        "truncado": motivo == "length",
    }


__all__ = [
    "COMANDO_PULL",
    "COMANDO_SERVE",
    "MODELOS_PADRAO",
    "NUM_PREDICT_PADRAO",
    "TIMEOUT_PADRAO_S",
    "TIMEOUT_SONDA_S",
    "T_FALHOU",
    "T_FALTAM",
    "T_FORA_DO_AR",
    "T_NENHUM",
    "T_PRONTO",
    "T_SEM_RESPOSTA",
    "URL_PADRAO",
    "FalhaDeInferencia",
    "ModeloAusente",
    "OllamaIndisponivel",
    "SemResposta",
    "base_url",
    "conversar",
    "digests",
    "estado",
    "listar",
    "max_turnos",
    "modelos_configurados",
    "num_ctx",
    "num_predict",
    "temperatura",
    "timeout_s",
]
