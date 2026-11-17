from botoffice.voice.text import CHUNK_CHARS, chunks, speakable


def test_markdown_becomes_plain_speech():
    md = (
        "# Plan\nSee **bold**, _it_, `code` and [the docs](https://example.com/x).\n"
        "- one\n- two\n```python\nprint('secret')\n```\n| a | b |\n|---|---|\n| 1 | 2 |\n"
        "> quoted\nVisit https://example.com/long/path now"
    )
    text = speakable(md)
    assert "Plan." in text and "See bold, it, code and the docs." in text
    assert "print" not in text and "(code block omitted)" in text
    assert "https" not in text and "(link)" in text
    assert "a, b." in text and "1, 2." in text and "|" not in text and "#" not in text
    assert "one.\ntwo." in text and "quoted." in text


def test_identifiers_and_arithmetic_are_not_mangled():
    assert speakable("snake_case_name and 2*3*4") == "snake_case_name and 2*3*4."


def test_unterminated_code_fence_is_omitted():
    assert speakable("Intro\n```\nhalf a block") == "Intro.\n(code block omitted)"


def test_chunks_respect_the_limit_and_sentences():
    long = " ".join(f"Sentence number {i} is here." for i in range(100)) + " " + "x" * 900
    parts = chunks(long)
    assert all(0 < len(part) <= CHUNK_CHARS for part in parts)
    assert parts[0].endswith("here.")
    assert "".join(parts).replace(" ", "").count("x") == 900


def test_nothing_speakable_gives_no_chunks():
    assert chunks("```\ncode only\n```") == ["(code block omitted)"]
    assert chunks("---\n***") == []


def test_adversarial_markdown_is_linear_time():
    import time

    for unit in ("*a ", "[[[[", "_a ", "![", "[a](", "<a ", "` ", " ", "|--", "**a *b", "~~a ", "```\n", "*a\n"):
        text = (unit * (100_000 // len(unit) + 1))[:100_000]
        started = time.perf_counter()
        chunks(text)
        assert time.perf_counter() - started < 1.0, unit


def test_nested_and_unclosed_emphasis():
    assert speakable("**bold *it* x** and *open") == "bold it x and *open."
    assert speakable("~~gone~~ [a [b](c)") == "gone [a b."
