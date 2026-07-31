"""Remoção de dados pessoais dos prompts (s03).

Filosofia: **precisão acima de recall**. Este corpus vai ser lido por pessoas e
exportado; um `[TELEFONE]` no lugar de um número de processo judicial estraga o
prompt de forma irreversível (o `text_raw` fica intacto, mas ninguém revisa
200 mil linhas). Então cada padrão só dispara quando a forma é inequívoca, ou
quando há palavra de contexto por perto.

Ordem normativa das substituições (mexer aqui muda o resultado):

1. ``URL_CRED`` — URL **com credencial embutida** (``https://user:senha@host``)
   vira ``[URL]`` inteira. Tem de vir antes do e-mail, senão o ``user:senha@host``
   seria comido pelo padrão de e-mail e a senha ficaria na URL.
2. ``EMAIL`` → ``[EMAIL]``.
3. ``CNPJ`` (só formatado) → ``[CNPJ]``.
4. ``CPF`` formatado (``123.456.789-09``) → ``[CPF]``, **sem** validar dígito
   verificador: a forma já é conclusiva.
5. ``CPF`` cru (11 dígitos seguidos) → ``[CPF]`` **só** com DV mod-11 válido e
   fora da lista de dígitos repetidos. Sem isso, qualquer id de 11 dígitos
   viraria CPF.
6. ``TELEFONE`` → ``[TELEFONE]``, em dois níveis: *forma suficiente*
   (``+55 11 91234-5678``, ``(11) 91234-5678``, ``912 345 678`` de Portugal) e
   *forma ambígua com contexto obrigatório* (``11 91234-5678`` solto só troca se
   houver "telefone", "whats", "ligar"... nos 40 caracteres anteriores).

Decisões registradas:

* **CEP não é PII forte** e não é substituído: identifica um quarteirão, não uma
  pessoa, e aparece em milhares de prompts de endereço/frete legítimos.
* URLs comuns **ficam**: fazem parte do prompt ("resuma este artigo: https://...").
* Não mexemos em nomes próprios: sem NER confiável em pt, o falso positivo
  destruiria prompts inteiros.

**Efeito colateral obrigatório no s03**: a linha alterada recalcula
``hash_norm``/``n_chars``/``n_words``. É desejável — dois spams idênticos que só
diferem no e-mail passam a colidir no dedup exato do s04.
"""

from __future__ import annotations

import re

#: Tipos de PII, na ordem em que são substituídos.
PII_TIPOS: tuple[str, ...] = ("url", "email", "cnpj", "cpf", "telefone")

#: Marcador que substitui cada tipo.
PLACEHOLDERS: dict[str, str] = {
    "url": "[URL]",
    "email": "[EMAIL]",
    "cnpj": "[CNPJ]",
    "cpf": "[CPF]",
    "telefone": "[TELEFONE]",
}

# --- padrões ---------------------------------------------------------------

_RE_URL_CRED = re.compile(r"(?:https?|ftp)://[^\s/@]+(?::[^\s/@]*)?@\S+")
_RE_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_RE_CNPJ = re.compile(r"(?<!\d)\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}(?!\d)")
_RE_CPF_FMT = re.compile(r"(?<!\d)\d{3}\.\d{3}\.\d{3}-\d{2}(?!\d)")
# O guard de ponto/hífen é o que salva "0001234-56.2024.8.26.0100" (número de
# processo) e "1.234.567,89" de virarem CPF.
_RE_CPF_RAW = re.compile(r"(?<![\d.\-])\d{11}(?![\d.\-])")

#: Nível A — a forma sozinha já denuncia telefone.
_RE_TEL_A: tuple[re.Pattern[str], ...] = (
    # Internacional com +DDI.
    re.compile(r"(?<![\w+])\+\d{1,3}[\s.\-]?\(?\d{1,4}\)?[\s.\-]?\d{3,5}[\s.\-]?\d{3,5}(?!\d)"),
    # Brasileiro com DDD entre parênteses.
    re.compile(r"(?<!\d)\(\d{2}\)\s?\d{4,5}[-\s]?\d{4}(?!\d)"),
    # Móvel português: 9dd ddd ddd.
    re.compile(r"(?<!\d)9\d{2}\s\d{3}\s\d{3}(?!\d)"),
)

#: Nível B — forma ambígua: só troca com palavra de contexto antes.
_RE_TEL_B: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?<!\d)\d{2}\s?9?\d{4}-\d{4}(?!\d)"),
)

_RE_CONTEXTO_TEL = re.compile(
    r"(?:telefone|celular|whats|zap|fone|contato|contacto|ligar|n[úu]mero|tel|phone|call|mobile)",
    re.IGNORECASE,
)
#: Quantos caracteres antes do candidato o contexto pode estar.
JANELA_CONTEXTO = 40

_RE_NAO_DIGITO = re.compile(r"\D")


# --- validação --------------------------------------------------------------


def cpf_dv_ok(digitos: str) -> bool:
    """Dígitos verificadores do CPF (mod 11). Rejeita os 10 CPFs "111...11"."""
    if len(digitos) != 11 or not digitos.isdigit():
        return False
    if digitos == digitos[0] * 11:
        return False
    nums = [int(c) for c in digitos]
    soma = sum(nums[i] * (10 - i) for i in range(9))
    dv1 = (soma * 10) % 11 % 10
    if dv1 != nums[9]:
        return False
    soma = sum(nums[i] * (11 - i) for i in range(10))
    dv2 = (soma * 10) % 11 % 10
    return dv2 == nums[10]


def _telefone_plausivel(trecho: str) -> bool:
    """Pós-filtro comum aos dois níveis: 8 a 15 dígitos e não todos iguais."""
    d = _RE_NAO_DIGITO.sub("", trecho)
    if not (8 <= len(d) <= 15):
        return False
    return len(set(d)) > 1


# --- substituição -----------------------------------------------------------


def _sub_contado(
    padrao: re.Pattern[str],
    texto: str,
    marcador: str,
    aceita: object = None,
) -> tuple[str, int]:
    """``padrao.sub`` com contagem e um predicado opcional sobre o match."""
    trocas = 0

    def _rep(m: re.Match[str]) -> str:
        nonlocal trocas
        if aceita is not None and not aceita(m):  # type: ignore[operator]
            return m.group(0)
        trocas += 1
        return marcador

    return padrao.sub(_rep, texto), trocas


def _tem_contexto(m: re.Match[str]) -> bool:
    inicio = max(0, m.start() - JANELA_CONTEXTO)
    return _RE_CONTEXTO_TEL.search(m.string[inicio : m.start()]) is not None


def scrub(text: str) -> tuple[str, dict[str, int]]:
    """Devolve ``(texto_limpo, {tipo: quantas_trocas})``.

    O dicionário traz **só** os tipos que apareceram (vazio = nada a fazer), o
    que torna o teste `assert cont == {"email": 1}` legível.
    """
    if not text:
        return (text, {})

    contagens: dict[str, int] = {}

    def _reg(tipo: str, n: int) -> None:
        if n:
            contagens[tipo] = contagens.get(tipo, 0) + n

    t = text
    t, n = _sub_contado(_RE_URL_CRED, t, PLACEHOLDERS["url"])
    _reg("url", n)
    t, n = _sub_contado(_RE_EMAIL, t, PLACEHOLDERS["email"])
    _reg("email", n)
    t, n = _sub_contado(_RE_CNPJ, t, PLACEHOLDERS["cnpj"])
    _reg("cnpj", n)
    t, n = _sub_contado(_RE_CPF_FMT, t, PLACEHOLDERS["cpf"])
    _reg("cpf", n)
    t, n = _sub_contado(
        _RE_CPF_RAW, t, PLACEHOLDERS["cpf"], lambda m: cpf_dv_ok(m.group(0))
    )
    _reg("cpf", n)

    for padrao in _RE_TEL_A:
        t, n = _sub_contado(
            padrao, t, PLACEHOLDERS["telefone"], lambda m: _telefone_plausivel(m.group(0))
        )
        _reg("telefone", n)
    for padrao in _RE_TEL_B:
        t, n = _sub_contado(
            padrao,
            t,
            PLACEHOLDERS["telefone"],
            lambda m: _telefone_plausivel(m.group(0)) and _tem_contexto(m),
        )
        _reg("telefone", n)

    return (t, contagens)


__all__ = ["JANELA_CONTEXTO", "PII_TIPOS", "PLACEHOLDERS", "cpf_dv_ok", "scrub"]
