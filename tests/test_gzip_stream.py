import gzip
import io
import re
import zlib

import pytest
from support import builder_mod


@pytest.mark.parametrize("size", [None, -1, -5, 100])
def test_gzip_stream_caps_each_read_before_decompression(size: int | None, monkeypatch: pytest.MonkeyPatch) -> None:
    asks: list[int] = []
    read1 = gzip.GzipFile.read1

    def spy(stream: gzip.GzipFile, size: int = -1) -> bytes:
        asks.append(size)
        return read1(stream, size)

    monkeypatch.setattr(gzip.GzipFile, "read1", spy)
    message = "test: decompresses past 3 bytes; raise max_bytes in sources.toml if expected"
    with (
        builder_mod._CappedGzip({"id": "test", "max_bytes": 3}, io.BytesIO(gzip.compress(b"abcdef"))) as stream,  # noqa: SLF001
        pytest.raises(ValueError, match=f"^{re.escape(message)}$"),
    ):
        stream.read1() if size is None else stream.read1(size)
    assert asks == [4]


def test_gzip_stream_zero_read_preserves_the_next_read() -> None:
    with builder_mod._CappedGzip({"id": "test", "max_bytes": 3}, io.BytesIO(gzip.compress(b"abc"))) as stream:  # noqa: SLF001
        assert stream.read1(0) == b""
        assert stream.read1(100) == b"abc"
        assert stream.read1(100) == b""


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        (gzip.compress(b"abcdef")[:-1], EOFError),
        (b"not gzip", gzip.BadGzipFile),
        (b"\x1f\x8b\x08\x00" + bytes(6) + b"\x07junk", zlib.error),
    ],
)
def test_gzip_stream_names_damaged_input(payload: bytes, error: type[Exception]) -> None:
    with (
        builder_mod._CappedGzip({"id": "test"}, io.BytesIO(payload)) as stream,  # noqa: SLF001
        pytest.raises(ValueError, match=r"^test: not a readable gzip file \(.+\)$") as caught,
    ):
        list(iter(lambda: stream.read1(2), b""))
    assert isinstance(caught.value.__cause__, error)
