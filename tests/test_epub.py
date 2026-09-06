"""EPUB builder tests: structural validity + determinism."""

import io
import zipfile

from epub.builder import build_epub

DECKS = [
    ("Spanish", [("hola", "hello"), ("gato", "cat")]),
    ("French", [("bonjour", "hello")]),
    ("Empty", []),
]


def _zip(data):
    return zipfile.ZipFile(io.BytesIO(data))


def test_mimetype_is_first_and_stored():
    data = build_epub(DECKS)
    with _zip(data) as zf:
        infos = zf.infolist()
        assert infos[0].filename == "mimetype"
        assert infos[0].compress_type == zipfile.ZIP_STORED
        assert zf.read("mimetype") == b"application/epub+zip"


def test_container_points_at_opf():
    data = build_epub(DECKS)
    with _zip(data) as zf:
        container = zf.read("META-INF/container.xml").decode()
        assert "OEBPS/content.opf" in container
        assert zf.read("OEBPS/content.opf")


def test_both_toc_formats_present():
    data = build_epub(DECKS)
    with _zip(data) as zf:
        names = zf.namelist()
        assert "OEBPS/toc.ncx" in names
        assert "OEBPS/nav.xhtml" in names


def test_one_chapter_per_nonempty_deck_sorted():
    data = build_epub(DECKS)
    with _zip(data) as zf:
        names = zf.namelist()
        chapters = sorted(n for n in names if n.startswith("OEBPS/deck"))
        # 3 decks provided but all three become chapters (empty decks still get
        # a chapter here; the *sync* layer skips empties via read_all()).
        assert len(chapters) == 3
        opf = zf.read("OEBPS/content.opf").decode()
        # decks are sorted case-insensitively: Empty, French, Spanish
        first_chapter = zf.read("OEBPS/deck1.xhtml").decode()
        assert "<h1>Empty</h1>" in first_chapter
        assert opf.index("deck1") < opf.index("deck2") < opf.index("deck3")


def test_html_escaping():
    decks = [("XSS", [("<b>front</b>", "a & b <script>")])]
    data = build_epub(decks)
    with _zip(data) as zf:
        chapter = zf.read("OEBPS/deck1.xhtml").decode()
    assert "&lt;b&gt;front&lt;/b&gt;" in chapter
    assert "a &amp; b &lt;script&gt;" in chapter
    assert "<script>" not in chapter.split("<body>")[1]


def test_deterministic_identical_input():
    a = build_epub(DECKS, title="My Anki Decks")
    b = build_epub(list(reversed(DECKS)), title="My Anki Decks")
    # Same content, different input order -> byte-identical (decks are sorted).
    assert a == b


def test_changes_alter_bytes():
    a = build_epub(DECKS)
    changed = [("Spanish", [("hola", "HELLO")])] + DECKS[1:]
    b = build_epub(changed)
    assert a != b


def test_structurally_valid_zip():
    data = build_epub(DECKS)
    with _zip(data) as zf:
        assert zf.testzip() is None
