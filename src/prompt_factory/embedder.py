"""Wrapper único do modelo de embeddings (e5-small multilíngue).

**Todo mundo passa por aqui**: o s05 (que gera o `.npy`), o s06 (que não gera
nada novo, mas depende do mesmo espaço vetorial), o treino do M7 e a busca
semântica do app. Se cada um chamasse o `sentence-transformers` do seu jeito,
uma consulta acabaria comparada com vetores gerados por outra receita — e o
erro seria silencioso, porque cossenos continuam saindo.

A receita, verificada contra o repositório do modelo:

* **``prompt="query: "``**, não ``prompt_name="query"``. O repo do
  `intfloat/multilingual-e5-small` não publica `config_sentence_transformers.json`
  com prompts nomeados, então `prompt_name` levanta erro; `prompt=` prepende a
  string por chamada, que é exatamente o que o modelo espera. O prefixo importa:
  o e5 foi treinado com ``query:``/``passage:`` e sem ele os vetores mudam.
* **``normalize_embeddings=True``** ainda que o repo já traga um módulo
  `2_Normalize`: é barato, é explícito, e protege caso o repo mude.
* **fp32 na saída**. O `.npy` em disco é f16 (metade do espaço, precisão de
  sobra para cosseno), mas a conta é sempre feita em f32 — numpy não tem BLAS
  para f16 e a multiplicação sairia ordens de magnitude mais lenta.
* **Corte em ``[embed] truncate_chars`` caracteres antes de tokenizar.** O
  modelo trunca em 512 tokens de qualquer jeito; o corpus tem prompts de até 1
  milhão de caracteres e tokenizar isso inteiro para jogar 99,7% fora derruba a
  vazão do estágio.

O import de `torch`/`sentence_transformers` acontece **dentro** das funções: o
`pf --help` e os testes que não tocam em modelo não pagam ~3 s de import.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from . import config

if TYPE_CHECKING:  # pragma: no cover
    import numpy as np

_LOCK = threading.Lock()
_modelo: Any = None

#: Prefixo exigido pelo e5 para textos de consulta.
PREFIXO_QUERY = "query: "


def model_name() -> str:
    """Repo do modelo (``[embed] model``)."""
    return str(config.get("embed", "model", default="intfloat/multilingual-e5-small"))


def dim() -> int:
    """Dimensão dos vetores (``[embed] dim``)."""
    return int(config.get("embed", "dim", default=384))


def batch_size() -> int:
    return int(config.get("embed", "batch_size", default=64))


def truncate_chars() -> int:
    return int(config.get("embed", "truncate_chars", default=3000))


def _aplicar_threads() -> None:
    """Fixa o número de threads do torch se ``[embed] threads`` > 0."""
    n = int(config.get("embed", "threads", default=0))
    if n <= 0:
        return
    import torch

    torch.set_num_threads(n)


def get_model() -> Any:
    """Modelo único do processo (lazy, thread-safe). CPU.

    Garante o CA bundle do sistema antes de carregar: o TLS desta máquina é
    interceptado e o `sentence-transformers` revalida os arquivos no Hub mesmo
    com o modelo em cache. Quem entra por ``pf`` já passou pelo
    ``cli._bootstrap_env``; quem entra por script solto ou por pytest, não.
    """
    global _modelo
    if _modelo is not None:
        return _modelo
    with _LOCK:
        if _modelo is None:
            try:
                from .certs import ensure_ca_bundle

                ensure_ca_bundle()
            except Exception as exc:  # pragma: no cover - nunca derruba o passe
                print(f"[embedder] aviso: CA bundle do sistema não gerado ({exc})")
            _aplicar_threads()
            from sentence_transformers import SentenceTransformer

            _modelo = SentenceTransformer(model_name(), device="cpu")
    return _modelo


def modelo_em_cache() -> bool:
    """Diz se o modelo já está no cache local do HuggingFace (sem baixar nada).

    Usado pelos testes lentos para pular sozinhos numa máquina limpa.
    """
    import os
    from pathlib import Path

    slug = "models--" + model_name().replace("/", "--")
    raizes = [os.environ.get("HF_HOME"), config.hf_home()]
    for raiz in raizes:
        if not raiz:
            continue
        if (Path(raiz) / "hub" / slug).is_dir() or (Path(raiz) / slug).is_dir():
            return True
    return False


def embed_texts(texts: Iterable[str], *, batch_size_: int | None = None) -> np.ndarray:
    """Vetores fp32 ``(n, dim)`` já L2-normalizados, na ordem da entrada."""
    import numpy as np

    corte = truncate_chars()
    lote = [(t or "")[:corte] for t in texts]
    if not lote:
        return np.zeros((0, dim()), dtype=np.float32)
    emb = get_model().encode(
        lote,
        prompt=PREFIXO_QUERY,
        batch_size=batch_size_ or batch_size(),
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
    )
    return np.asarray(emb, dtype=np.float32)


__all__ = [
    "PREFIXO_QUERY",
    "batch_size",
    "dim",
    "embed_texts",
    "get_model",
    "model_name",
    "modelo_em_cache",
    "truncate_chars",
]
