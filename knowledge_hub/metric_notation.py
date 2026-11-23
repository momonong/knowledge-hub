"""Display a small set of equation symbols used in measured results."""
import re
import unicodedata


UNIT_PATTERN = (
    r'percentage points?|percent(?![a-z])|百分點|%|times(?![a-z])|[x×](?![a-z])|倍|'
    r'milliseconds?(?![a-z])|ms(?![a-z])|毫秒|seconds?(?![a-z])|s(?![a-z])|秒|'
    r'(?:[kmgt]i?b|tokens?)(?![a-z])'
)
_TEX_UNIT = re.compile(rf'\\(?:text|mathrm)\{{\s*({UNIT_PATTERN})\s*\}}', re.I)
_TEX_SYMBOL = re.compile(r'\\(?:(times|cdot|rightarrow|to|pm|leq|geq)(?![a-zA-Z])|([%,;! ]))')
_TEX_PLAIN = {'times': '×', 'cdot': '·', 'rightarrow': '→', 'to': '→', 'pm': '±',
              'leq': '≤', 'geq': '≥', '%': '%', ',': ' ', ';': ' ', '!': '', ' ': ' '}
_TEX_INLINE = re.compile(r'(?<![\\$])\$([^$\n]{1,160})\$(?!\$)|\\\(([^\n]{1,160}?)\\\)')
_NUMERIC_MATH = re.compile(rf'(?=.*\d)(?>[\d\s.,+\-−–—%×·→±≤≥=^/()]|{UNIT_PATTERN})+', re.I)


def plain_math(text):
    """Render recognized numeric TeX notation without changing its values or units."""
    display = _TEX_UNIT.sub(r'\1', text)
    display = _TEX_SYMBOL.sub(lambda match: _TEX_PLAIN[match[1] or match[2]], display)

    def unwrap(match):
        inner = match[1] or match[2]
        return inner.strip() if _NUMERIC_MATH.fullmatch(inner) else match[0]

    return _TEX_INLINE.sub(unwrap, display)


_TEX_GREEK = re.compile(r'(?<!\\)\\([A-Za-z]+)(?:_\{(\d+)\}|_(\d+))?(?![A-Za-z_])')
_GREEK_INDEX = re.compile(r'([\u0370-\u03ff])_(?:\{(\d+)\}|(\d+))(?!\w)')
_GREEK_MATH = re.compile(
    r'[\u0370-\u03ff]\d*(?:\s*[<>=≤≥≈±]\s*[-+−]?\d+(?:\.\d+)?%?)?'
)


def plain_metric(text):
    """Convert recognized numeric units and Greek metric labels for display."""
    text = plain_math(text)

    def greek(match):
        name = match[1].upper().replace('LAMBDA', 'LAMDA')
        case = 'CAPITAL' if match[1][0].isupper() else 'SMALL'
        try:
            symbol = unicodedata.lookup(f'GREEK {case} LETTER {name}') + (match[2] or match[3] or '')
        except KeyError:
            return match[0]
        return '' if text[:match.start()].endswith(symbol) else symbol

    display = _TEX_GREEK.sub(greek, text)
    display = _GREEK_INDEX.sub(lambda match: match[1] + (match[2] or match[3]), display)

    def unwrap(match):
        inner = (match[1] or match[2]).strip()
        return inner if _GREEK_MATH.fullmatch(inner) else match[0]

    return _TEX_INLINE.sub(unwrap, display)


def contains_displayed(text, selected):
    """Match the exact legacy prose or the exact prose offered for selection."""
    return selected in text or selected in plain_metric(text)


def display_section(section):
    """Project generated prose while preserving citation objects and identifiers."""
    return {**section, 'text': plain_metric(section['text'])}
