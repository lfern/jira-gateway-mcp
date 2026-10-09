import pytest

from gateway.adf import adf_to_markdown, markdown_to_adf


def _p(*content):
    return {"type": "paragraph", "content": list(content)}


def _t(text, *marks):
    node = {"type": "text", "text": text}
    if marks:
        node["marks"] = [m if isinstance(m, dict) else {"type": m} for m in marks]
    return node


def _doc(*blocks):
    return {"type": "doc", "version": 1, "content": list(blocks)}


# --- Markdown -> ADF ---------------------------------------------------------


def test_empty_markdown_gives_empty_paragraph():
    assert markdown_to_adf("") == _doc(_p())


def test_paragraphs_and_hard_breaks():
    assert markdown_to_adf("línea uno\nlínea dos\n\notro párrafo") == _doc(
        _p(_t("línea uno"), {"type": "hardBreak"}, _t("línea dos")),
        _p(_t("otro párrafo")),
    )


def test_heading_rule_and_quote():
    assert markdown_to_adf("## Contexto\n\n---\n\n> citado") == _doc(
        {"type": "heading", "attrs": {"level": 2}, "content": [_t("Contexto")]},
        {"type": "rule"},
        {"type": "blockquote", "content": [_p(_t("citado"))]},
    )


def test_inline_marks():
    adf = markdown_to_adf("**negrita** y *cursiva*, `código`, ~~fuera~~ y [enlace](https://x.io)")
    assert adf["content"][0]["content"] == [
        _t("negrita", "strong"),
        _t(" y "),
        _t("cursiva", "em"),
        _t(", "),
        _t("código", "code"),
        _t(", "),
        _t("fuera", "strike"),
        _t(" y "),
        _t("enlace", {"type": "link", "attrs": {"href": "https://x.io"}}),
    ]


def test_snake_case_is_not_italic():
    assert markdown_to_adf("usa mi_variable_larga")["content"][0]["content"] == [_t("usa mi_variable_larga")]


def test_nested_lists():
    adf = markdown_to_adf("- uno\n  - uno.a\n- dos\n\n3. tres\n4. cuatro")
    bullet, ordered = adf["content"]
    assert bullet["type"] == "bulletList"
    first = bullet["content"][0]["content"]
    assert first[0] == _p(_t("uno"))
    assert first[1]["type"] == "bulletList"
    assert first[1]["content"][0]["content"] == [_p(_t("uno.a"))]
    assert ordered["type"] == "orderedList"
    assert ordered["attrs"] == {"order": 3}
    assert len(ordered["content"]) == 2


def test_list_interrupts_paragraph():
    adf = markdown_to_adf("Pasos:\n- a\n- b")
    assert [b["type"] for b in adf["content"]] == ["paragraph", "bulletList"]


def test_code_block_keeps_content_verbatim():
    adf = markdown_to_adf("```python\nx = *a*\n\n# no es título\n```")
    assert adf["content"] == [
        {
            "type": "codeBlock",
            "attrs": {"language": "python"},
            "content": [_t("x = *a*\n\n# no es título")],
        }
    ]


# --- ADF -> Markdown ---------------------------------------------------------


def test_none_description_is_empty():
    assert adf_to_markdown(None) == ("", [])


def test_unsupported_nodes_are_reported():
    doc = _doc(
        _p(_t("hola "), {"type": "mention", "attrs": {"id": "1", "text": "@Ana"}}),
        {"type": "table", "content": []},
        _p(_t("sub", "underline")),
    )
    md, unsupported = adf_to_markdown(doc)
    assert md == "hola @Ana\n\nsub"
    assert unsupported == ["mark:underline", "mention", "table"]


def test_marks_move_whitespace_outside_delimiters():
    md, _ = adf_to_markdown(_doc(_p(_t("ojo ", "strong"), _t("aquí"))))
    assert md == "**ojo** aquí"


# --- Ida y vuelta --------------------------------------------------------------


@pytest.mark.parametrize(
    "markdown",
    [
        "Texto simple",
        "línea uno\nlínea dos\n\notro párrafo",
        "# Título\n\n## Subtítulo con `code`",
        "[Foo](https://en.wikipedia.org/wiki/Foo_(bar)) y [con \\] dentro](https://x.io)",
        "[espacio](<https://x.io/a b>)",
        "[menor](https://x.io/?a<b) y [mayor](https://x.io/a>b)",
        "[raro](<foo) y [más raro](<a>b)",
        "````\n```\ndentro\n```\n````",
        "```\n    def f():\n\treturn 1\n```",
        "- item\n\n  ```\n  \tcon tab\n  ```",
        "# C \\#",
        "**dos** **negritas** y [un](https://a.io) [enlace](https://b.io)",
        "**negrita**, *cursiva*, **_ambas_**, ~~tachado~~ y [enlace **fuerte**](https://x.io)",
        "- uno\n- dos\n  - dos.a\n  - dos.b\n- tres",
        "1. primero\n2. segundo\n\n   párrafo dentro del segundo\n3. tercero",
        "5. empieza en cinco\n6. seis",
        "```js\nconst a = `x`;\n```",
        "> cita\n> con dos líneas\n>\n> - y lista",
        "antes\n\n---\n\ndespués",
        "mi_variable y \\_esto\\_ no es cursiva, 2 \\* 3",
        "\\# no es título\n\\- ni lista\n1\\. ni numerada\n\\> ni cita",
        "precio: \\[WIP\\] sin enlace",
    ],
)
def test_roundtrip_markdown(markdown):
    md, unsupported = adf_to_markdown(markdown_to_adf(markdown))
    assert unsupported == []
    assert md == markdown


@pytest.mark.parametrize(
    "text",
    ["# hola", "- guion", "1. numerado", "> mayor", "---", "*asteriscos*", "_guiones_", "[a](b)", "`tick`", "a\\b"],
)
def test_roundtrip_literal_text(text):
    """Texto literal en Jira que parece markdown sobrevive a leer y reescribir."""
    doc = _doc(_p(_t(text)))
    md, _ = adf_to_markdown(doc)
    assert markdown_to_adf(md) == doc


@pytest.mark.parametrize(
    "doc, lost",
    [
        (_doc(_p(_t("  sangría"))), "espacios al inicio de línea"),
        (_doc(_p(_t("a"), {"type": "hardBreak"}, {"type": "hardBreak"}, _t("b"))), "saltos de línea consecutivos"),
        (
            _doc({"type": "heading", "attrs": {"level": 1}, "content": [_t("a"), {"type": "hardBreak"}, _t("b")]}),
            "saltos de línea en títulos",
        ),
    ],
)
def test_lossy_whitespace_is_reported(doc, lost):
    _, unsupported = adf_to_markdown(doc)
    assert lost in unsupported


def test_tab_indented_fence_keeps_code_indentation():
    adf = markdown_to_adf("\t```\n\t\tcode\n\t```")
    assert adf["content"][0]["content"] == [_t("\tcode")]


@pytest.mark.parametrize(
    "doc, lost",
    [
        (_doc(_p(_t("x", {"type": "link", "attrs": {"href": "<foo>"}}))), "URLs de enlace no representables en markdown"),
        (
            _doc(_p(_t("a]b", "code", {"type": "link", "attrs": {"href": "https://x.io"}}))),
            "código con ] dentro de un enlace",
        ),
        (
            _doc({"type": "heading", "attrs": {"level": 2}, "content": [_t(" título ")]}),
            "espacios al inicio o final de títulos",
        ),
    ],
)
def test_lossy_links_and_headings_are_reported(doc, lost):
    _, unsupported = adf_to_markdown(doc)
    assert lost in unsupported
