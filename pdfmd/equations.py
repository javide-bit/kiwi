# -*- coding: utf-8 -*-
"""
pdfmd.equations — Detección avanzada de fórmulas matemáticas y conversión de Unicode a LaTeX.
"""

import re
from typing import Callable, List, Optional
from pdfmd.models import Block, Line, Options, PageText, Span

UNICODE_MATH_MAP = {
    # Letras griegas minúsculas
    "α": r"\alpha", "β": r"\beta", "γ": r"\gamma", "δ": r"\delta", "ε": r"\epsilon",
    "ζ": r"\zeta", "η": r"\eta", "θ": r"\theta", "ι": r"\iota", "κ": r"\kappa",
    "λ": r"\lambda", "μ": r"\mu", "ν": r"\nu", "ξ": r"\xi", "π": r"\pi",
    "ρ": r"\rho", "σ": r"\sigma", "τ": r"\tau", "υ": r"\upsilon", "φ": r"\phi",
    "χ": r"\chi", "ψ": r"\psi", "ω": r"\omega",
    # Letras griegas mayúsculas
    "Γ": r"\Gamma", "Δ": r"\Delta", "Θ": r"\Theta", "Λ": r"\Lambda", "Ξ": r"\Xi",
    "Π": r"\Pi", "Σ": r"\Sigma", "Υ": r"\Upsilon", "Φ": r"\Phi", "Ψ": r"\Psi", "Ω": r"\Omega",

    # Fracciones Unicode comunes
    "½": r"\frac{1}{2}", "⅓": r"\frac{1}{3}", "⅔": r"\frac{2}{3}", "¼": r"\frac{1}{4}",
    "¾": r"\frac{3}{4}", "⅕": r"\frac{1}{5}", "⅖": r"\frac{2}{5}", "⅗": r"\frac{3}{5}",
    "⅘": r"\frac{4}{5}", "⅙": r"\frac{1}{6}", "⅚": r"\frac{5}{6}", "⅛": r"\frac{1}{8}",
    "⅜": r"\frac{3}{8}", "⅝": r"\frac{5}{8}", "⅞": r"\frac{7}{8}",

    # Operadores y símbolos matemáticos
    "∫": r"\int ", "∬": r"\iint ", "∭": r"\iiint ", "∮": r"\oint ",
    "∑": r"\sum ", "∏": r"\prod ", "√": r"\sqrt", "∛": r"\sqrt[3]", "∜": r"\sqrt[4]",
    "∞": r"\infty", "±": r"\pm", "∓": r"\mp", "×": r"\times", "÷": r"\div",
    "≠": r"\neq", "≤": r"\le", "≥": r"\ge", "≈": r"\approx", "≡": r"\equiv",
    "∈": r"\in", "∉": r"\notin", "⊂": r"\subset", "⊆": r"\subseteq",
    "∪": r"\cup", "∩": r"\cap", "∅": r"\emptyset", "∇": r"\nabla",
    "∂": r"\partial", "→": r"\to", "⇒": r"\implies", "⇔": r"\iff",
    "∀": r"\forall", "∃": r"\exists", "∄": r"\nexists", "°": r"^\circ",
    "·": r"\cdot ", "∝": r"\propto", "∠": r"\angle", "⊥": r"\perp",

    # Superíndices y subíndices
    "⁰": "^{0}", "¹": "^{1}", "²": "^{2}", "³": "^{3}", "⁴": "^{4}",
    "⁵": "^{5}", "⁶": "^{6}", "⁷": "^{7}", "⁸": "^{8}", "⁹": "^{9}",
    "⁺": "^{+}", "⁻": "^{-}", "⁼": "^{=}", "⁽": "^{(}", "⁾": "^{)}",
    "ⁿ": "^{n}", "ⁱ": "^{i}",
    "₀": "_{0}", "₁": "_{1}", "₂": "_{2}", "₃": "_{3}", "₄": "_{4}",
    "₅": "_{5}", "₆": "_{6}", "₇": "_{7}", "₈": "_{8}", "₉": "_{9}",
    "₊": "_{+}", "₋": "_{-}", "₌": "_{=}", "₍": "_{(}", "₎": "_{)}"
}

# Símbolos que se leen perfectamente en texto corrido: no se tocan dentro de prosa,
# solo se convierten cuando la línea completa es una fórmula.
PROSE_SAFE_SYMBOLS = set("∞±∓×÷≠≤≥≈≡→°·½⅓⅔¼¾⅕⅖⅗⅘⅙⅚⅛⅜⅝⅞⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱ₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎")

# Símbolos que carecen de lectura natural en texto plano y siempre merecen LaTeX
HARD_MATH_SYMBOLS = [c for c in UNICODE_MATH_MAP if c not in PROSE_SAFE_SYMBOLS]
_RE_HARD_RUN = re.compile("[" + re.escape("".join(HARD_MATH_SYMBOLS)) + r"]+")

# Palabras de tres o más letras que no delatan prosa por aparecer en fórmulas
MATH_WORDS = {
    "sin", "cos", "tan", "cot", "sec", "csc", "sinh", "cosh", "tanh",
    "log", "ln", "exp", "lim", "max", "min", "det", "arg", "mod", "div",
    "sup", "inf", "dim", "gcd", "lcm",
}
_RE_WORD = re.compile(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]{3,}")
_RE_LABEL = re.compile(r"^(?P<label>[^:$]{1,40}:\s+)(?P<rest>\S.*)$")


def unicode_to_latex(text: str) -> str:
    """Convierte caracteres matemáticos Unicode en su equivalente LaTeX."""
    if text.startswith("$$") and text.endswith("$$"):
        return text
    if text.startswith("$") and text.endswith("$"):
        return text

    res = text
    for u_char, latex_equiv in UNICODE_MATH_MAP.items():
        if u_char in res:
            res = res.replace(u_char, latex_equiv)
    return res


def has_math_symbols(text: str) -> bool:
    return any(c in text for c in UNICODE_MATH_MAP)


def count_prose_words(text: str) -> int:
    """Cuenta palabras de prosa, ignorando los nombres de función matemática."""
    return sum(1 for w in _RE_WORD.findall(text) if w.lower() not in MATH_WORDS)


def is_pure_formula(text: str) -> bool:
    """Una línea es fórmula pura si no contiene prosa y sí notación matemática."""
    stripped = text.strip()
    if not stripped or count_prose_words(stripped) > 0:
        return False
    return has_math_symbols(stripped) or "=" in stripped


def inline_math(text: str) -> str:
    """Envuelve en $...$ solo los símbolos matemáticos incrustados en texto corrido."""
    return _RE_HARD_RUN.sub(
        lambda m: "$" + "".join(UNICODE_MATH_MAP[c] for c in m.group(0)).strip() + "$",
        text
    )


def convert_line_text(text: str) -> str:
    """
    Devuelve el texto convertido a Markdown+LaTeX según su naturaleza:
      - fórmula pura       -> bloque display $$...$$
      - "Etiqueta: fórmula" -> etiqueta + fórmula en línea $...$
      - prosa con símbolos  -> símbolos aislados en línea $...$
    """
    stripped = text.strip()
    if not stripped or "$" in stripped:
        return text

    if is_pure_formula(stripped):
        return f"$${unicode_to_latex(stripped)}$$"

    match = _RE_LABEL.match(stripped)
    if match and is_pure_formula(match.group("rest")):
        return f"{match.group('label')}${unicode_to_latex(match.group('rest'))}$"

    return inline_math(text)


def process_equations(
    pages: List[PageText],
    options: Options,
    log_cb: Optional[Callable[[str], None]] = None
) -> List[PageText]:
    """Detecta y convierte expresiones matemáticas en las páginas."""
    if not options.convert_equations:
        return pages

    eq_count = 0
    for page in pages:
        for block in page.blocks:
            if block.block_type in ("table", "image", "code"):
                continue

            for line in block.lines:
                raw = line.text()
                if not has_math_symbols(raw):
                    continue
                converted = convert_line_text(raw)
                if converted != raw:
                    eq_count += 1
                    line.spans = [Span(text=converted)]

    if eq_count and log_cb:
        log_cb(f"Fórmulas y símbolos matemáticos convertidos a LaTeX: {eq_count}")

    return pages
