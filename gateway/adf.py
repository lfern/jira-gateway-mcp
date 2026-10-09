"""
Conversión Markdown <-> ADF (Atlassian Document Format) para descripciones.

Cubre el subconjunto de Markdown que tiene sentido en una tarea: párrafos,
títulos, listas (anidadas), bloques de código, citas, separadores y, en
línea, negrita, cursiva, tachado, código y enlaces. Un salto de línea simple
dentro de un párrafo se respeta (hardBreak), como espera quien escribe en
Jira. Lo que ADF tiene y Markdown no (menciones, tablas, paneles,
adjuntos, subrayado...) se aplana a texto al leer y se devuelve aparte como
"no soportado", para que quien vaya a reescribir la descripción sepa que
esos elementos se perderían.
"""
import re
import textwrap

_FENCE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})\s*([\w+#.-]*)\s*$")
_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)(?:\s+#+)?\s*$")
_RULE_RE = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")
_LIST_RE = re.compile(r"^(\s*)([-*+]|\d{1,9}[.)])(?:\s+(.*))?$")
_QUOTE_RE = re.compile(r"^\s{0,3}>\s?(.*)$")


def _span(delim: str, name: str, edge: str = r"\s") -> str:
    """Contenido entre delimitadores: sin espacios (ni `edge`) en los
    extremos, y consumiendo cada escape (barra + carácter) de una vez para que un
    delimitador escapado nunca cierre el span."""
    return rf"{delim}(?P<{name}>(?![{edge}])(?:\\.|[^\\])*?(?:\\.|[^\\{edge}])){delim}"


# Destino de enlace sin <>: sin espacios y con paréntesis equilibrados (un nivel).
_HREF = r"(?:[^()\s]|\([^()\s]*\))+"

_INLINE_RE = re.compile(
    r"(?P<escape>\\[!-/:-@\[-`{-~])"
    r"|(?<!`)(?P<code>`+)(?P<code_text>[^`](?:.*?[^`])?)(?P=code)(?!`)"
    r"|\[(?P<link_text>(?:\\.|[^\]\\])+)\]"
    r"\((?:<(?P<link_href_angle>[^<>\n]*)>|(?P<link_href>" + _HREF + r"))\)"
    + "|" + _span(r"\*\*", "strong")
    + "|" + r"(?<!\w)(?:" + _span("__", "strong2") + r")(?!\w)"
    + "|" + _span("~~", "strike")
    + "|" + _span(r"\*", "em", r"\s*")
    + "|" + r"(?<!\w)(?:" + _span("_", "em2", r"\s_") + r")(?!\w)"
)

_INLINE_TYPES = {"text", "hardBreak", "mention", "emoji", "inlineCard", "date", "status", "placeholder"}


# --- Markdown -> ADF ---------------------------------------------------------


def markdown_to_adf(markdown: str) -> dict:
    blocks = _parse_blocks(markdown.splitlines())
    return {"type": "doc", "version": 1, "content": blocks or [{"type": "paragraph", "content": []}]}


def _indent(line: str) -> int:
    expanded = line.expandtabs(4)
    return len(expanded) - len(expanded.lstrip())


def _strip_indent(line: str, n: int) -> str:
    """Quita hasta `n` columnas de sangría (tab = hasta la siguiente columna
    múltiplo de 4, como `_indent`), sin expandir los tabs del resto."""
    col = 0
    pos = 0
    while pos < len(line) and line[pos] in " \t":
        width = 4 - col % 4 if line[pos] == "\t" else 1
        if col + width > n:
            break
        col += width
        pos += 1
    return line[pos:]


def _starts_block(line: str) -> bool:
    return bool(
        _FENCE_RE.match(line)
        or _HEADING_RE.match(line)
        or _RULE_RE.match(line)
        or _QUOTE_RE.match(line)
        or _LIST_RE.match(line)
    )


def _parse_blocks(lines: list[str]) -> list[dict]:
    blocks = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue

        if m := _FENCE_RE.match(line):
            indent, fence, language = _indent(m.group(1)), m.group(2), m.group(3)
            body = []
            i += 1
            # Cierra una valla del mismo carácter y al menos igual de larga.
            while i < len(lines) and not re.fullmatch(
                rf"\s*{re.escape(fence[0])}{{{len(fence)},}}\s*", lines[i]
            ):
                body.append(lines[i])
                i += 1
            i += 1  # salta el cierre (o el final, si no lo había)
            # Solo se quita la sangría de la propia valla, no la del código.
            text = "\n".join(_strip_indent(b, indent) for b in body)
            node = {"type": "codeBlock", "content": [{"type": "text", "text": text}] if text else []}
            if language:
                node["attrs"] = {"language": language}
            blocks.append(node)
            continue

        if m := _HEADING_RE.match(line):
            blocks.append(
                {
                    "type": "heading",
                    "attrs": {"level": len(m.group(1))},
                    "content": _parse_inline(m.group(2)),
                }
            )
            i += 1
            continue

        if _RULE_RE.match(line):
            blocks.append({"type": "rule"})
            i += 1
            continue

        if _QUOTE_RE.match(line):
            inner = []
            while i < len(lines) and (m := _QUOTE_RE.match(lines[i])):
                inner.append(m.group(1))
                i += 1
            blocks.append({"type": "blockquote", "content": _parse_blocks(inner)})
            continue

        if _LIST_RE.match(line):
            node, i = _parse_list(lines, i)
            blocks.append(node)
            continue

        para = []
        while i < len(lines) and lines[i].strip() and (not para or not _starts_block(lines[i])):
            para.append(lines[i].strip())
            i += 1
        blocks.append({"type": "paragraph", "content": _join_lines(para)})
    return blocks


def _join_lines(lines: list[str]) -> list[dict]:
    content = []
    for n, line in enumerate(lines):
        if n:
            content.append({"type": "hardBreak"})
        content.extend(_parse_inline(line))
    return content


def _is_ordered(marker: str) -> bool:
    return marker[0].isdigit()


def _parse_list(lines: list[str], i: int) -> tuple[dict, int]:
    first = _LIST_RE.match(lines[i])
    base = _indent(first.group(1))
    ordered = _is_ordered(first.group(2))
    items = []

    while i < len(lines):
        m = _LIST_RE.match(lines[i])
        if not m or _indent(m.group(1)) != base or _is_ordered(m.group(2)) != ordered:
            break
        i += 1
        rest = []
        while i < len(lines):
            line = lines[i]
            if line.strip():
                if _indent(line) <= base:
                    break
                rest.append(line)
                i += 1
                continue
            # Línea en blanco: sigue dentro del item solo si lo siguiente está más indentado.
            j = i
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines) and _indent(lines[j]) > base:
                rest.extend([""] * (j - i))
                i = j
                continue
            break

        content = _parse_blocks([m.group(3) or ""] + textwrap.dedent("\n".join(rest)).split("\n"))
        if not content or content[0]["type"] != "paragraph":
            content.insert(0, {"type": "paragraph", "content": []})
        items.append({"type": "listItem", "content": content})

        # Lista "suelta" (items separados por líneas en blanco): sigue si el
        # siguiente item es del mismo nivel y tipo.
        j = i
        while j < len(lines) and not lines[j].strip():
            j += 1
        nxt = _LIST_RE.match(lines[j]) if j < len(lines) else None
        if nxt and _indent(nxt.group(1)) == base and _is_ordered(nxt.group(2)) == ordered:
            i = j

    node = {"type": "orderedList" if ordered else "bulletList", "content": items}
    if ordered:
        start = int(first.group(2)[:-1])
        if start != 1:
            node["attrs"] = {"order": start}
    return node, i


def _text(text: str, marks: tuple) -> dict:
    node = {"type": "text", "text": text}
    if marks:
        node["marks"] = list(marks)
    return node


def _match_href(m: re.Match) -> str:
    """Destino del enlace de un match de _INLINE_RE (forma desnuda o <...>)."""
    return m.group("link_href") if m.group("link_href") is not None else m.group("link_href_angle")


def _parse_inline(text: str, marks: tuple = ()) -> list[dict]:
    nodes = []
    pending = ""
    pos = 0

    def flush():
        nonlocal pending
        if pending:
            nodes.append(_text(pending, marks))
            pending = ""

    for m in _INLINE_RE.finditer(text):
        pending += text[pos : m.start()]
        pos = m.end()
        if m.group("escape"):
            pending += m.group("escape")[1]
            continue
        flush()
        if m.group("code"):
            code = m.group("code_text")
            # Como en CommonMark: un espacio a cada lado se quita (permite `` `x` ``).
            if len(code) > 2 and code[0] == code[-1] == " " and code.strip():
                code = code[1:-1]
            # En ADF el mark `code` solo se puede combinar con `link`.
            link_marks = tuple(mk for mk in marks if mk["type"] == "link")
            nodes.append(_text(code, link_marks + ({"type": "code"},)))
            continue
        if m.group("link_text"):
            href = _match_href(m)
            link = {"type": "link", "attrs": {"href": href}}
            nodes.extend(_parse_inline(m.group("link_text"), marks + (link,)))
            continue
        for name, mark in (("strong", "strong"), ("strong2", "strong"), ("strike", "strike"), ("em", "em"), ("em2", "em")):
            inner = m.group(name)
            if inner:
                nodes.extend(_parse_inline(inner, marks + ({"type": mark},)))
                break
    pending += text[pos:]
    flush()
    return nodes


# --- ADF -> Markdown ---------------------------------------------------------


def adf_to_markdown(doc: dict | None) -> tuple[str, list[str]]:
    """Devuelve (markdown, tipos_no_soportados). La segunda lista enumera los
    nodos/marks de ADF que no tienen equivalente en Markdown y que se han
    aplanado a texto (o perdido): si se reescribe la descripción a partir de
    este markdown, eso desaparece de Jira."""
    if not doc:
        return "", []
    lossy: set[str] = set()
    return _blocks(doc.get("content", []), lossy, "\n\n"), sorted(lossy)


def _blocks(nodes: list[dict], lossy: set, sep: str) -> str:
    rendered = [r for r in (_block(n, lossy) for n in nodes) if r]
    return sep.join(rendered)


def _block(node: dict, lossy: set) -> str:
    kind = node.get("type")
    content = node.get("content", [])
    attrs = node.get("attrs") or {}

    if kind == "paragraph":
        lines = _inline(content, lossy).split("\n")
        if any(not line.strip() for line in lines) and len(lines) > 1:
            lossy.add("saltos de línea consecutivos")
        if any(line[:1].isspace() for line in lines):
            lossy.add("espacios al inicio de línea")
        return "\n".join(_escape_line(line) for line in lines)
    if kind == "heading":
        text = _inline(content, lossy)
        if "\n" in text:
            lossy.add("saltos de línea en títulos")
        text = text.replace("\n", " ")
        if text != text.strip():
            lossy.add("espacios al inicio o final de títulos")
        text = text.strip()
        if text.endswith("#"):
            text = text[:-1] + "\\#"  # si no, se leería como cierre del título
        return "#" * attrs.get("level", 1) + " " + text
    if kind == "bulletList":
        return "\n".join(_list_item(item, "- ", lossy) for item in content)
    if kind == "orderedList":
        start = attrs.get("order", 1)
        return "\n".join(_list_item(item, f"{start + n}. ", lossy) for n, item in enumerate(content))
    if kind == "codeBlock":
        text = "".join(c.get("text", "") for c in content)
        fence = "`" * max(3, max((len(r) for r in re.findall(r"`+", text)), default=0) + 1)
        return f"{fence}{attrs.get('language') or ''}\n{text}\n{fence}"
    if kind == "blockquote":
        inner = _blocks(content, lossy, "\n\n")
        return "\n".join(f"> {line}" if line else ">" for line in inner.split("\n"))
    if kind == "rule":
        return "---"

    # Cualquier otra cosa (tabla, panel, expand, media...) se aplana.
    lossy.add(kind)
    if any(c.get("type") in _INLINE_TYPES for c in content):
        return _inline(content, lossy)
    return _blocks(content, lossy, "\n\n")


def _list_item(item: dict, marker: str, lossy: set) -> str:
    parts = []
    for n, child in enumerate(item.get("content", [])):
        rendered = _block(child, lossy)
        if n == 0:
            # Si el primer bloque no es texto (o está vacío), el marcador va
            # solo en su línea y el resto debajo, indentado.
            parts.append(rendered if child.get("type") == "paragraph" else "\n" + rendered)
            continue
        if not rendered:
            continue
        # Una sublista va pegada; otro párrafo necesita línea en blanco.
        is_list = child.get("type") in ("bulletList", "orderedList")
        parts.append(("\n" if is_list else "\n\n") + rendered)
    lines = "".join(parts).split("\n")
    pad = " " * len(marker)
    first = (marker + lines[0]).rstrip()
    return "\n".join([first] + [pad + line if line else "" for line in lines[1:]])


def _link_href(node: dict) -> str | None:
    if node.get("type") != "text":
        return None
    link = next((mk for mk in node.get("marks", []) if mk["type"] == "link"), None)
    return link.get("attrs", {}).get("href", "") if link else None


def _render_href(href: str, lossy: set) -> str:
    # Forma desnuda o entre <...>: la primera que se vuelva a leer igual.
    for candidate in (href, f"<{href}>"):
        m = _INLINE_RE.fullmatch(f"[x]({candidate})")
        if m and _match_href(m) == href:
            return candidate
    lossy.add("URLs de enlace no representables en markdown")
    return href


def _inline(nodes: list[dict], lossy: set) -> str:
    out = []
    i = 0
    while i < len(nodes):
        node = nodes[i]
        kind = node.get("type")
        href = _link_href(node)
        if href is not None:
            # Un enlace con formato dentro son varios nodos seguidos con el
            # mismo mark `link`: se pintan juntos dentro de un solo [...](...).
            group = []
            while i < len(nodes) and _link_href(nodes[i]) == href:
                n = nodes[i]
                group.append({**n, "marks": [mk for mk in n["marks"] if mk["type"] != "link"]})
                i += 1
            if any("]" in n.get("text", "") for n in group if any(mk["type"] == "code" for mk in n["marks"])):
                lossy.add("código con ] dentro de un enlace")
            out.append(f"[{_inline(group, lossy)}]({_render_href(href, lossy)})")
            continue
        i += 1
        if kind == "text":
            out.append(_render_text(node, lossy))
        elif kind == "hardBreak":
            out.append("\n")
        else:
            lossy.add(kind)
            attrs = node.get("attrs") or {}
            out.append(attrs.get("text") or attrs.get("url") or attrs.get("shortName") or "")
    return "".join(out)


def _render_text(node: dict, lossy: set) -> str:
    text = node.get("text", "")
    marks = {mk["type"]: mk for mk in node.get("marks", [])}
    for kind in marks.keys() - {"code", "em", "strong", "strike", "link"}:
        lossy.add(f"mark:{kind}")

    if "code" in marks:
        ticks = "`" * (max((len(r) for r in re.findall(r"`+", text)), default=0) + 1)
        # Igual que al leer: si empieza/acaba en ` (o ya va entre espacios), se rodea de espacios.
        needs_pad = text[:1] == "`" or text[-1:] == "`" or (text[:1] == text[-1:] == " " and text.strip())
        pad = " " if needs_pad else ""
        s = f"{ticks}{pad}{text}{pad}{ticks}"
    else:
        s = _escape(text)

    # Los delimitadores no pueden pegarse a espacios: los sacamos fuera.
    core = s.strip()
    if not core:
        return s
    lead = s[: len(s) - len(s.lstrip())]
    trail = s[len(s.rstrip()) :]
    if "em" in marks:
        core = f"_{core}_" if "strong" in marks else f"*{core}*"
    if "strong" in marks:
        core = f"**{core}**"
    if "strike" in marks:
        core = f"~~{core}~~"
    return lead + core + trail


def _escape(text: str) -> str:
    out = []
    for n, ch in enumerate(text):
        if ch in "\\`*[]~":
            out.append("\\" + ch)
        elif ch == "_" and not (text[n - 1 : n].isalnum() and text[n + 1 : n + 2].isalnum()):
            out.append("\\_")  # snake_case se queda tal cual; _esto_ no
        else:
            out.append(ch)
    return "".join(out)


def _escape_line(line: str) -> str:
    """Evita que una línea de texto normal se lea como título/lista/cita."""
    m = _LIST_RE.match(line)
    if m and _is_ordered(m.group(2)):
        end = m.end(2)
        return line[: end - 1] + "\\" + line[end - 1 :]
    if _starts_block(line):
        return "\\" + line
    return line
