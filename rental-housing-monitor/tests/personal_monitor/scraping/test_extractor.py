from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from personal_monitor.domain.spec import ExtractSpec, FetchStrategy, MonitorSpec
from personal_monitor.engine.errors import ErrorClass, MonitorError
from personal_monitor.scraping.document import SourceDocument
from personal_monitor.scraping.extractor import DeclarativeExtractor
from personal_monitor.scraping.normalizers import normalize_url

FIXTURES = Path(__file__).parents[2] / "fixtures" / "personal_monitor"
pytestmark = pytest.mark.filterwarnings("ignore:The 'strip_cdata' option.*:DeprecationWarning")


@pytest.fixture
def product_spec() -> MonitorSpec:
    return MonitorSpec.model_validate_json((FIXTURES / "product-spec.json").read_bytes())


@pytest.fixture
def product_document() -> SourceDocument:
    return document((FIXTURES / "product.html").read_bytes())


def document(
    body: bytes,
    *,
    content_type: str = "text/html",
    final_url: str = "https://example.com/catalog",
) -> SourceDocument:
    return SourceDocument(
        final_url=final_url,
        status=200,
        content_type=content_type,
        headers={"content-type": content_type},
        body=body,
        strategy=FetchStrategy.HTTP,
    )


def extract_spec(payload: dict[str, object]) -> ExtractSpec:
    return ExtractSpec.model_validate(payload)


def test_declared_html_fields_are_typed_and_descendant_visible_text_is_selected(
    product_document: SourceDocument, product_spec: MonitorSpec
) -> None:
    items = DeclarativeExtractor().extract(product_document, product_spec.extract)

    assert len(items) == 1
    assert items[0].fields == {
        "title": "무선 키보드",
        "price": 99000,
        "stock": "재고 있음",
        "url": "https://example.com/products/sku-7",
    }
    assert "credential" not in repr(items)


def test_xpath_prefixes_are_supported_within_each_item_root() -> None:
    spec = extract_spec(
        {
            "item_scope": "//main",
            "fields": {
                "title": {"selector": "(.//h1)[1]", "type": "text"},
                "url": {
                    "selector": "(./a)[1]",
                    "type": "url",
                    "attribute": "href",
                },
            },
        }
    )
    result = DeclarativeExtractor().extract(
        document(b'<main><h1>One</h1><a href="/one">go</a></main>'), spec
    )

    assert result[0].fields == {
        "title": "One",
        "url": "https://example.com/one",
    }


def test_multiple_item_roots_are_supported_but_a_field_must_be_unambiguous() -> None:
    rows = DeclarativeExtractor().extract(
        document(b"<main><h1>A</h1></main><main><h1>B</h1></main>"),
        extract_spec(
            {"item_scope": "main", "fields": {"title": {"selector": "h1", "type": "text"}}}
        ),
    )
    assert [row.fields["title"] for row in rows] == ["A", "B"]

    with pytest.raises(MonitorError, match="ambiguous") as caught:
        DeclarativeExtractor().extract(
            document(b"<main><h1>A</h1><h1>B</h1></main>"),
            extract_spec(
                {
                    "item_scope": "main",
                    "fields": {"title": {"selector": "h1", "type": "text"}},
                }
            ),
        )
    assert caught.value.error_class is ErrorClass.STRUCTURE


@pytest.mark.parametrize("selector", ["//h1", ".//h1", "(//h1)[1]", "(/h1)[1]"])
def test_field_xpath_is_scoped_to_each_item_root(selector: str) -> None:
    rows = DeclarativeExtractor().extract(
        document(b"<main><h1>A</h1></main><main><h1>B</h1></main>"),
        extract_spec(
            {
                "item_scope": "main",
                "fields": {"title": {"selector": selector, "type": "text"}},
            }
        ),
    )

    assert [row.fields["title"] for row in rows] == ["A", "B"]


def test_compound_field_xpath_cannot_escape_to_sibling_items() -> None:
    rows = DeclarativeExtractor().extract(
        document(b"<main><h2>A</h2></main><main><h1>B</h1></main>"),
        extract_spec(
            {
                "item_scope": "main",
                "fields": {"title": {"selector": "(/h1 | //h2)[1]", "type": "text"}},
            }
        ),
    )

    assert [row.fields["title"] for row in rows] == ["A", "B"]


@pytest.mark.parametrize(
    ("selector", "body"),
    [
        ("(/h1[./x or /y])[1]", b"<main><h1>A<y/></h1></main>"),
        ("(/h1[./x and /y])[1]", b"<main><h1>A<x/><y/></h1></main>"),
    ],
)
def test_absolute_xpath_operands_after_boolean_operators_are_item_scoped(
    selector: str, body: bytes
) -> None:
    rows = DeclarativeExtractor().extract(
        document(body),
        extract_spec(
            {
                "item_scope": "main",
                "fields": {"title": {"selector": selector, "type": "text"}},
            }
        ),
    )

    assert rows[0].fields["title"] == "A"


@pytest.mark.parametrize(
    "selector",
    [
        "(/h1[10 div /value = 5])[1]",
        "(/h1[5 mod /value = 1])[1]",
    ],
)
def test_absolute_xpath_operands_after_numeric_word_operators_are_item_scoped(
    selector: str,
) -> None:
    rows = DeclarativeExtractor().extract(
        document(b"<main><h1>A<value>2</value></h1></main>"),
        extract_spec(
            {
                "item_scope": "main",
                "fields": {"title": {"selector": selector, "type": "text"}},
            }
        ),
    )

    assert rows[0].fields["title"] == "A 2"


@pytest.mark.parametrize(
    ("selector", "body"),
    [
        ("(/div / h1)[1]", b"<main><div><h1>C</h1></div></main>"),
        (
            "(/or / and / mod / h1)[1]",
            b"<main><or><and><mod><h1>C</h1></mod></and></or></main>",
        ),
        ("(/my-div / h1)[1]", b"<main><my-div><h1>C</h1></my-div></main>"),
    ],
)
def test_operator_named_and_hyphenated_path_steps_remain_names(selector: str, body: bytes) -> None:
    rows = DeclarativeExtractor().extract(
        document(body),
        extract_spec(
            {
                "item_scope": "main",
                "fields": {"title": {"selector": selector, "type": "text"}},
            }
        ),
    )

    assert rows[0].fields["title"] == "C"


@pytest.mark.parametrize(
    "selector",
    [
        "(/section / *[@data-key='x' and @*])[1]",
        "(/section / child::h1[@data-key='x'])[1]",
    ],
)
def test_wildcard_attribute_axis_and_quoted_xpath_tokens_are_preserved(selector: str) -> None:
    rows = DeclarativeExtractor().extract(
        document(b'<main><section><h1 data-key="x">C</h1></section></main>'),
        extract_spec(
            {
                "item_scope": "main",
                "fields": {"title": {"selector": selector, "type": "text"}},
            }
        ),
    )

    assert rows[0].fields["title"] == "C"


def test_xpath_slashes_inside_quoted_literals_are_preserved() -> None:
    rows = DeclarativeExtractor().extract(
        document(b'<main><a href="https://example.com/p">Link</a></main>'),
        extract_spec(
            {
                "item_scope": "main",
                "fields": {
                    "title": {
                        "selector": "(./a[@href='https://example.com/p'])[1]",
                        "type": "text",
                    }
                },
            }
        ),
    )

    assert rows[0].fields["title"] == "Link"


def test_visible_text_excludes_explicitly_hidden_descendants() -> None:
    body = b"""<main><h1>Shown
      <span hidden>hidden-secret</span>
      <i aria-hidden="true">aria-secret</i>
      <b style="display: none !important">display-secret</b>
      <em style="visibility:hidden">visibility-secret</em>
      <strong>Text</strong>
    </h1></main>"""

    rows = DeclarativeExtractor().extract(
        document(body),
        extract_spec(
            {
                "item_scope": "main",
                "fields": {"title": {"selector": "h1", "type": "text"}},
            }
        ),
    )

    assert rows[0].fields["title"] == "Shown Text"


@pytest.mark.parametrize(
    "hidden_fragment",
    [
        '<span hidden>hidden<i aria-hidden="true"><template>template</template></i></span>',
        '<span aria-hidden="true">aria<template><b hidden>nested</b></template></span>',
        "<template>template<span hidden>nested</span></template>",
        '<span style="display:/**/none">comment-hidden<i hidden>nested</i></span>',
        '<span style="visibility:/**/hidden">comment-hidden<template>nested</template></span>',
        '<span style="display:none/*">unterminated-comment-hidden</span>',
    ],
)
def test_nested_hidden_descendants_are_removed_without_destroyed_tag_errors(
    hidden_fragment: str,
) -> None:
    body = f"<main><h1>A {hidden_fragment}<strong>B</strong></h1></main>".encode()

    rows = DeclarativeExtractor().extract(
        document(body),
        extract_spec(
            {
                "item_scope": "main",
                "fields": {"title": {"selector": "h1", "type": "text"}},
            }
        ),
    )

    assert rows[0].fields["title"] == "A B"


def test_overlong_inline_style_is_bounded_and_fails_closed() -> None:
    style = "color:red;" * 500
    body = f'<main><h1>A <span style="{style}">hidden</span><strong>B</strong></h1></main>'.encode()

    rows = DeclarativeExtractor().extract(
        document(body),
        extract_spec(
            {
                "item_scope": "main",
                "fields": {"title": {"selector": "h1", "type": "text"}},
            }
        ),
    )

    assert rows[0].fields["title"] == "A B"


def test_hidden_optional_field_is_treated_as_absent() -> None:
    rows = DeclarativeExtractor().extract(
        document(b'<main><span class="note" hidden>secret</span></main>'),
        extract_spec(
            {
                "item_scope": "main",
                "fields": {
                    "note": {
                        "selector": ".note",
                        "type": "text",
                        "required": False,
                    }
                },
            }
        ),
    )

    assert rows[0].fields == {}


def test_missing_required_field_is_structure_error_without_source_leak(
    product_document: SourceDocument, product_spec: MonitorSpec
) -> None:
    secret = "token=private-source-value"
    changed = replace(
        product_document,
        body=f"<main><h1>무선 키보드</h1><p>{secret}</p></main>".encode(),
    )

    with pytest.raises(MonitorError) as caught:
        DeclarativeExtractor().extract(changed, product_spec.extract)

    assert caught.value.error_class is ErrorClass.STRUCTURE
    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)


def test_optional_absence_is_deterministically_omitted() -> None:
    spec = extract_spec(
        {
            "item_scope": "main",
            "fields": {
                "title": {"selector": "h1", "type": "text"},
                "subtitle": {"selector": "h2", "type": "text", "required": False},
            },
        }
    )

    item = DeclarativeExtractor().extract(document(b"<main><h1>Title</h1></main>"), spec)[0]

    assert item.fields == {"title": "Title"}


@pytest.mark.parametrize(
    ("field", "raw_value"),
    [
        ({"selector": ".value", "type": "integer"}, "secret-raw-input"),
        ({"selector": ".value", "type": "decimal"}, "secret-raw-input"),
        ({"selector": ".value", "type": "krw"}, "secret-raw-input"),
        ({"selector": ".value", "type": "date"}, "secret-raw-input"),
        ({"selector": ".value", "type": "datetime"}, "secret-raw-input"),
        ({"selector": ".value", "type": "boolean"}, "secret-raw-input"),
        ({"selector": ".value", "type": "url"}, "javascript:secret-raw-input"),
    ],
)
def test_empty_or_malformed_normalizer_input_is_a_safe_validation_error(
    field: dict[str, object], raw_value: str
) -> None:
    spec = extract_spec({"item_scope": "main", "fields": {"value": field}})

    with pytest.raises(MonitorError) as caught:
        DeclarativeExtractor().extract(
            document(f'<main><span class="value">{raw_value}</span></main>'.encode()), spec
        )

    assert caught.value.error_class is ErrorClass.VALIDATION
    assert raw_value not in str(caught.value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("true", True), ("YES", True), ("1", True), ("false", False), ("no", False), ("0", False)],
)
def test_boolean_normalization_is_explicit(value: str, expected: bool) -> None:
    spec = extract_spec(
        {
            "item_scope": "main",
            "fields": {"active": {"selector": ".value", "type": "boolean"}},
        }
    )

    item = DeclarativeExtractor().extract(
        document(f'<main><span class="value">{value}</span></main>'.encode()), spec
    )[0]

    assert item.fields["active"] is expected


def test_regex_uses_first_capture_group_and_rejects_invalid_or_no_match_safely() -> None:
    capture = extract_spec(
        {
            "item_scope": "main",
            "fields": {
                "price": {
                    "selector": ".price",
                    "type": "integer",
                    "pattern": r"price:\s*([0-9]+)",
                }
            },
        }
    )
    assert (
        DeclarativeExtractor()
        .extract(document(b'<main><span class="price">price: 42 won</span></main>'), capture)[0]
        .fields["price"]
        == 42
    )

    for pattern in ("(", "does-not-match"):
        bad = ExtractSpec.model_validate(
            {
                "item_scope": "main",
                "fields": {"value": {"selector": ".value", "type": "text", "pattern": pattern}},
            }
        )
        with pytest.raises(MonitorError) as caught:
            DeclarativeExtractor().extract(
                document(b'<main><span class="value">raw-secret</span></main>'), bad
            )
        assert caught.value.error_class is ErrorClass.VALIDATION
        assert "raw-secret" not in str(caught.value)


def test_catastrophic_regex_is_bounded_by_timeout() -> None:
    spec = extract_spec(
        {
            "item_scope": "main",
            "fields": {
                "value": {
                    "selector": ".value",
                    "type": "text",
                    "pattern": r"(a+)+$",
                }
            },
        }
    )

    with pytest.raises(MonitorError, match="pattern") as caught:
        DeclarativeExtractor().extract(
            document(b'<main><span class="value">' + b"a" * 100_000 + b"!</span></main>"),
            spec,
        )

    assert caught.value.error_class is ErrorClass.VALIDATION


def test_json_extraction_uses_only_slash_object_and_numeric_list_traversal() -> None:
    spec = extract_spec(
        {
            "item_scope": "/products",
            "fields": {
                "title": {"selector": "/title", "type": "text"},
                "price": {"selector": "/prices/0", "type": "krw"},
                "url": {"selector": "/url", "type": "url"},
            },
        }
    )
    body = b'{"products":[{"title":"Keyboard","prices":["99,000 won"],"url":"/p/7"}]}'

    item = DeclarativeExtractor().extract(document(body, content_type="application/json"), spec)[0]

    assert item.fields == {
        "title": "Keyboard",
        "price": 99000,
        "url": "https://example.com/p/7",
    }


@pytest.mark.parametrize(
    "body",
    [
        b'{"products": [], "products": []}',
        b'{"products": [NaN]}',
        b'{"products": [Infinity]}',
        b'{"products": [1e9999]}',
        b'{"products": [{"title": "\\ud800"}]}',
        b'{"products": [',
        b'{"products": ["\xff"]}',
    ],
)
def test_json_rejects_duplicates_nonfinite_constants_invalid_syntax_and_malformed_utf8(
    body: bytes,
) -> None:
    spec = extract_spec(
        {"item_scope": "/products", "fields": {"title": {"selector": "/title", "type": "text"}}}
    )

    with pytest.raises(MonitorError) as caught:
        DeclarativeExtractor().extract(document(body, content_type="application/json"), spec)

    assert caught.value.error_class is ErrorClass.STRUCTURE
    assert "products" not in str(caught.value)


def test_deep_json_nesting_maps_recursion_to_a_safe_structure_error() -> None:
    body = ("[" * 5000 + "0" + "]" * 5000).encode()
    spec = extract_spec(
        {
            "item_scope": "/products",
            "fields": {"title": {"selector": "/title", "type": "text"}},
        }
    )

    with pytest.raises(MonitorError) as caught:
        DeclarativeExtractor().extract(document(body, content_type="application/json"), spec)

    assert caught.value.error_class is ErrorClass.STRUCTURE


@pytest.mark.parametrize("path", ["products", "/products/*", "/products/-1", "/products/00"])
def test_json_traversal_rejects_undocumented_or_ambiguous_paths(path: str) -> None:
    spec = extract_spec(
        {"item_scope": path, "fields": {"title": {"selector": "/title", "type": "text"}}}
    )

    with pytest.raises(MonitorError) as caught:
        DeclarativeExtractor().extract(
            document((FIXTURES / "product.json").read_bytes(), content_type="application/json"),
            spec,
        )

    assert caught.value.error_class in {ErrorClass.STRUCTURE, ErrorClass.VALIDATION}


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "//user:password@example.com/p",
        "https://example.com/p?token=secret",
        "https://example.com:444/p",
        "https://example.com:/p",
        "https://example.com/p%ZZ",
        "https://example.com/%2",
        "https://example.com/a b",
        "https://[invalid/p",
    ],
)
def test_url_normalization_rejects_unsafe_urls_without_leaking_them(url: str) -> None:
    spec = extract_spec(
        {
            "item_scope": "main",
            "fields": {"url": {"selector": "a", "attribute": "href", "type": "url"}},
        }
    )

    with pytest.raises(MonitorError) as caught:
        DeclarativeExtractor().extract(
            document(f'<main><a href="{url}">x</a></main>'.encode()), spec
        )

    assert caught.value.error_class is ErrorClass.VALIDATION
    assert url not in str(caught.value)


def test_url_normalization_uses_idna_and_removes_tracking_fragment() -> None:
    spec = extract_spec(
        {
            "item_scope": "main",
            "fields": {"url": {"selector": "a", "attribute": "href", "type": "url"}},
        }
    )
    item = DeclarativeExtractor().extract(
        document(
            '<main><a href="https://예시.한국/상품?b=2&utm_medium=x&a=1#part">x</a></main>'.encode()
        ),
        spec,
    )[0]

    assert item.fields["url"] == "https://xn--vv4b11d.xn--3e0b707e/상품?a=1&b=2"


def test_url_normalization_reuses_strict_host_and_character_policy() -> None:
    for unsafe in (
        "https://127.1/path",
        "https://2130706433/path",
        "https://0x7f.0.0.1/path",
        "https://example.com\\@evil.example/path",
        "https://example.com/path\x00secret",
        "https://example.com:/path",
        "https://example.com/path%ZZ",
        "https://example.com/%2",
        "https://example.com/a b",
    ):
        with pytest.raises(MonitorError) as caught:
            normalize_url(unsafe)
        assert caught.value.error_class is ErrorClass.VALIDATION
        assert unsafe not in str(caught.value)


def test_url_normalization_accepts_a_blank_query_value_deterministically() -> None:
    assert normalize_url("https://example.com/path?flag") == "https://example.com/path?flag="


def test_url_normalization_maps_a_malformed_base_to_a_safe_error() -> None:
    with pytest.raises(MonitorError) as caught:
        normalize_url("/product", "https://[malformed")

    assert caught.value.error_class is ErrorClass.VALIDATION
    assert "malformed" not in str(caught.value)


def test_unsupported_document_type_is_policy_error() -> None:
    with pytest.raises(MonitorError) as caught:
        DeclarativeExtractor().extract(
            document(b"opaque", content_type="text/plain"),
            extract_spec(
                {"item_scope": "main", "fields": {"title": {"selector": "h1", "type": "text"}}}
            ),
        )

    assert caught.value.error_class is ErrorClass.POLICY
