"""Camada 1 da detecção de idioma: wrapper fino sobre ``fast-langdetect``.

Por que um wrapper e não a função ``detect()`` da biblioteca:

* **``max_input_length`` vale 80 por padrão** em ``LangDetectConfig`` — a lib
  trunca o texto em 80 caracteres *em silêncio*. Aqui passamos ``None`` e
  fatiamos nós mesmos em ``[langid] max_input_chars`` (1.000), que é o que a
  decisão de idioma precisa e ainda evita pagar por prompts de 1 MB.
* O modelo ``"full"`` (lid.176.bin, ~126 MB) é baixado na primeira chamada e
  esta máquina tem o TLS interceptado: quando o download falha, a lib levanta
  ``FastLangdetectError``. O wrapper avisa ALTO e reconstrói com ``"lite"``
  (lid.176.ftz, EMBUTIDO no wheel, zero rede), para que a pipeline nunca pare
  por causa de rede.
* O detector é caro de construir e barato de reusar: instância única, criada
  na primeira chamada (``pf --help`` não paga por ela).

A função legada ``detect_language()`` da lib **não** é usada: ela não devolve
score e tem um hack ja→zh que não interessa aqui.

Saída sempre normalizada: ``(rótulo minúsculo sem "__label__", score <= 1.0)``;
texto vazio ou sem predição vira ``("und", 0.0)``.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

from . import config

#: Rótulo de "não sei" — nunca chega ao Parquet final (o s02 descarta).
UNKNOWN = "und"

_LOCK = threading.Lock()
_detector: Any = None
_modelo_ativo: str = ""


def _slice_len() -> int:
    return int(config.get("langid", "max_input_chars", default=1000))


def _build(model: str) -> Any:
    """Constrói o ``LangDetector`` com o modelo pedido."""
    from fast_langdetect import LangDetectConfig, LangDetector

    cache_dir = str(config.get("langid", "cache_dir", default="G:/hf-cache/fasttext-langdetect"))
    # A lib NÃO cria o diretório do usuário: sem isto o "full" morre no download.
    os.makedirs(cache_dir, exist_ok=True)
    logging.getLogger("fast_langdetect").setLevel(logging.WARNING)
    return LangDetector(
        LangDetectConfig(cache_dir=cache_dir, model=model, max_input_length=None)
    )


def get_detector() -> Any:
    """Detector único do processo (lazy, thread-safe)."""
    global _detector, _modelo_ativo
    if _detector is not None:
        return _detector
    with _LOCK:
        if _detector is None:
            modelo = str(config.get("langid", "model", default="lite"))
            _detector = _build(modelo)
            _modelo_ativo = modelo
    return _detector


def modelo_ativo() -> str:
    """Qual modelo está de fato carregado (``"full"`` ou ``"lite"``)."""
    get_detector()
    return _modelo_ativo


def _cair_para_lite(exc: Exception) -> Any:
    """Reconstrói o detector com o modelo embutido, avisando alto."""
    global _detector, _modelo_ativo
    with _LOCK:
        if _modelo_ativo != "lite":
            print(
                "[langdetect] AVISO: modelo 'full' indisponível "
                f"({type(exc).__name__}: {exc}); caindo para o 'lite' embutido no wheel. "
                "Para usar o 'full', baixe lid.176.bin manualmente para "
                f"{config.get('langid', 'cache_dir', default='')!s} ou confira o CA bundle."
            )
            _detector = _build("lite")
            _modelo_ativo = "lite"
    return _detector


def detect1(text: str) -> tuple[str, float]:
    """``(idioma, score)`` do texto. Nunca levanta: falha vira ``("und", 0.0)``."""
    from fast_langdetect import FastLangdetectError

    if not text:
        return (UNKNOWN, 0.0)
    trecho = text[: _slice_len()]
    # fastText decide por linha: quebras viram espaço para o texto virar um só
    # documento (a lib normaliza \n, mas só quando normalize_input está ligado).
    trecho = trecho.replace("\n", " ").replace("\r", " ").strip()
    if not trecho:
        return (UNKNOWN, 0.0)
    detector = get_detector()
    try:
        predicoes = detector.detect(trecho, k=1)
    except FastLangdetectError as exc:
        detector = _cair_para_lite(exc)
        try:
            predicoes = detector.detect(trecho, k=1)
        except Exception:  # pragma: no cover - lite é embutido, não deve falhar
            return (UNKNOWN, 0.0)
    except Exception:  # pragma: no cover - texto patológico não derruba o passe
        return (UNKNOWN, 0.0)
    if not predicoes:
        return (UNKNOWN, 0.0)
    top = predicoes[0]
    return (str(top.get("lang", UNKNOWN)).lower(), float(top.get("score", 0.0)))


def detect_many(texts: list[str]) -> list[tuple[str, float]]:
    """``detect1`` linha a linha (o fastText não tem API de lote)."""
    return [detect1(t) for t in texts]


# ---------------------------------------------------------------------------
# camada 2 — árbitro (lingua)
# ---------------------------------------------------------------------------

#: Idiomas do árbitro. São os seis que de fato se confundem com pt/en neste
#: corpus. **lingua 2.2.0 não tem GALEGO** — o mais próximo do galego no
#: conjunto é o CATALÃO, que entra no lugar para absorver os textos ibéricos que
#: não são português nem espanhol.
IDIOMAS_ARBITRO: tuple[str, ...] = (
    "PORTUGUESE",
    "ENGLISH",
    "SPANISH",
    "CATALAN",
    "ITALIAN",
    "FRENCH",
)

_arbitro: Any = None


def get_arbiter() -> Any:
    """Detector do lingua com os modelos pré-carregados (lazy, ~1 s e ~200 MB)."""
    global _arbitro
    if _arbitro is not None:
        return _arbitro
    with _LOCK:
        if _arbitro is None:
            from lingua import Language, LanguageDetectorBuilder

            idiomas = [getattr(Language, nome) for nome in IDIOMAS_ARBITRO]
            _arbitro = (
                LanguageDetectorBuilder.from_languages(*idiomas)
                .with_preloaded_language_models()
                .build()
            )
    return _arbitro


def arbitrar(texts: list[str]) -> list[tuple[str, float]]:
    """Top-1 do árbitro para um lote inteiro (threads Rust, sem GIL).

    Sem ``with_minimum_relative_distance``: quem decide a margem é a tabela do
    s02, não o detector.
    """
    if not texts:
        return []
    corte = _slice_len()
    lote = [(t or "")[:corte] for t in texts]
    resultado = get_arbiter().compute_language_confidence_values_in_parallel(lote)
    saida: list[tuple[str, float]] = []
    for confiancas in resultado:
        if not confiancas:
            saida.append((UNKNOWN, 0.0))
            continue
        topo = confiancas[0]
        saida.append((topo.language.iso_code_639_1.name.lower(), float(topo.value)))
    return saida


__all__ = [
    "IDIOMAS_ARBITRO",
    "UNKNOWN",
    "arbitrar",
    "detect1",
    "detect_many",
    "get_arbiter",
    "get_detector",
    "modelo_ativo",
]
