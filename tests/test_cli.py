"""Unit tests for the CLI: tools, approvals, streaming helpers. No network."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
from rich.console import Console

from baymon.cli import tools
from baymon.cli.tools import Decision, ToolContext
from baymon.cli.ui import MarkdownStream
from baymon.completions import _ThinkSplitter


class FakeUI:
    def __init__(self, answer: Decision | None = None) -> None:
        self.answer = answer or Decision(True)
        self.asked: list[str] = []
        self.output: list[str] = []
        self.plans: list[list[dict[str, str]]] = []

    def approve(self, tool: str, title: str, preview: str | None, preview_lang: str) -> Decision:
        self.asked.append(title)
        return self.answer

    def command_output(self, line: str) -> None:
        self.output.append(line)

    def show_plan(self, steps: list[dict[str, str]]) -> None:
        self.plans.append(steps)


def ctx(tmp_path: Path, ui: FakeUI | None = None, auto: bool = False) -> ToolContext:
    return ToolContext(cwd=tmp_path, client=None, ui=ui or FakeUI(), auto_approve=auto)  # type: ignore[arg-type]


def call(name: str, c: ToolContext, **args):  # type: ignore[no-untyped-def]
    return tools.run_tool(tools.BY_NAME[name], args, c)


# ------------------------------------------------------------------ files


def test_read_file_numbers_lines_and_pages(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("\n".join(f"line {i}" for i in range(1, 11)))
    r = call("read_file", ctx(tmp_path), path="a.txt", offset=3, limit=2)
    assert "     3\tline 3" in r.content and "     4\tline 4" in r.content
    assert "line 5" not in r.content and "offset=5" in r.content


def test_read_outside_workspace_asks(tmp_path: Path) -> None:
    inner = tmp_path / "proj"
    inner.mkdir()
    (tmp_path / "secret.txt").write_text("x")
    ui = FakeUI(Decision(False, "not that file"))
    r = call("read_file", ctx(inner, ui), path="../secret.txt")
    assert r.error and "not that file" in r.content and ui.asked


def test_write_file_asks_then_writes(tmp_path: Path) -> None:
    ui = FakeUI()
    r = call("write_file", ctx(tmp_path, ui), path="sub/new.py", content="print(1)\n")
    assert not r.error and (tmp_path / "sub/new.py").read_text() == "print(1)\n"
    assert ui.asked == ["Create sub/new.py (1 lines)?"]


def test_write_file_declined_does_nothing(tmp_path: Path) -> None:
    r = call("write_file", ctx(tmp_path, FakeUI(Decision(False))), path="x.py", content="x")
    assert r.error and not (tmp_path / "x.py").exists()


def test_auto_approve_skips_prompt(tmp_path: Path) -> None:
    ui = FakeUI()
    call("write_file", ctx(tmp_path, ui, auto=True), path="x.py", content="x")
    assert ui.asked == []


def test_always_allows_rest_of_session(tmp_path: Path) -> None:
    ui = FakeUI(Decision(True, always=True))
    c = ctx(tmp_path, ui)
    call("write_file", c, path="a.py", content="a")
    call("write_file", c, path="b.py", content="b")
    assert len(ui.asked) == 1


def test_edit_file_unique_match(tmp_path: Path) -> None:
    f = tmp_path / "m.py"
    f.write_text("a = 1\nb = 2\n")
    r = call("edit_file", ctx(tmp_path), path="m.py", old_string="b = 2", new_string="b = 3")
    assert not r.error and f.read_text() == "a = 1\nb = 3\n" and r.summary == "+1 -1"


def test_edit_file_rejects_ambiguous_and_missing(tmp_path: Path) -> None:
    f = tmp_path / "m.py"
    f.write_text("x\nx\n")
    assert "appears 2 times" in call("edit_file", ctx(tmp_path), path="m.py", old_string="x", new_string="y").content
    assert "not found" in call("edit_file", ctx(tmp_path), path="m.py", old_string="z", new_string="y").content
    r = call("edit_file", ctx(tmp_path), path="m.py", old_string="x", new_string="y", replace_all=True)
    assert not r.error and f.read_text() == "y\ny\n"


def test_edit_preserves_crlf(tmp_path: Path) -> None:
    f = tmp_path / "w.txt"
    f.write_bytes(b"one\r\ntwo\r\n")
    call("edit_file", ctx(tmp_path), path="w.txt", old_string="two", new_string="three")
    assert f.read_bytes() == b"one\r\nthree\r\n"


def test_grep_and_glob_skip_ignored_dirs(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/app.py").write_text("def hello():\n    return 1\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules/lib.py").write_text("def hello(): pass\n")
    g = call("grep", ctx(tmp_path), pattern=r"def hello")
    assert "src/app.py:1:" in g.content.replace("\\", "/") and "node_modules" not in g.content
    m = call("glob", ctx(tmp_path), pattern="**/*.py")
    assert "src/app.py" in m.content.replace("\\", "/") and "node_modules" not in m.content


def test_list_dir(tmp_path: Path) -> None:
    (tmp_path / "d").mkdir()
    (tmp_path / "f.txt").write_text("hi")
    r = call("list_dir", ctx(tmp_path))
    assert "d/" in r.content and "f.txt" in r.content


# ------------------------------------------------------------------ shell


def test_run_command_captures_output_and_exit_code(tmp_path: Path) -> None:
    ui = FakeUI()
    cmd = "echo hello; exit 3" if sys.platform != "win32" else "Write-Output hello; exit 3"
    r = call("run_command", ctx(tmp_path, ui), command=cmd)
    assert "hello" in r.content and "exit code 3" in r.content and r.error
    assert ui.output == ["hello"]


@pytest.mark.skipif(sys.platform == "win32", reason="uses sleep")
def test_run_command_times_out_silent_process(tmp_path: Path) -> None:
    r = call("run_command", ctx(tmp_path), command="sleep 30", timeout=1)
    assert "timed out after 1s" in r.content


def test_run_command_declined(tmp_path: Path) -> None:
    r = call("run_command", ctx(tmp_path, FakeUI(Decision(False))), command="echo nope")
    assert r.error and "declined" in r.content


# --------------------------------------------------------------- dispatch


def test_execute_handles_bad_json_and_unknown_tool(tmp_path: Path) -> None:
    _, _, r = tools.execute("read_file", "{not json", ctx(tmp_path))
    assert r.error and "Invalid arguments" in r.content
    _, _, r = tools.execute("rm_rf", "{}", ctx(tmp_path))
    assert r.error and "Unknown tool" in r.content


def test_switch_mode_and_plan(tmp_path: Path) -> None:
    ui = FakeUI()
    c = ctx(tmp_path, ui)
    call("switch_mode", c, mode="talk")
    assert c.requested_mode == "talk"
    call("update_plan", c, steps=[{"step": "a", "status": "done"}, {"step": "b", "status": "pending"}])
    assert ui.plans[-1][0] == {"step": "a", "status": "done"}


def test_schemas_are_valid_function_tools() -> None:
    for schema in tools.schemas():
        fn = schema["function"]
        assert schema["type"] == "function" and fn["name"] and fn["parameters"]["type"] == "object"


# ------------------------------------------------------------- streaming


def test_markdown_stream_splits_outside_code_fences() -> None:
    buf = io.StringIO()
    stream = MarkdownStream(Console(file=buf, width=80, color_system=None))
    for chunk in ["Intro para.\n", "\n```py\nx = 1\n\ny = 2\n", "```\n\nAfter."]:
        stream.feed(chunk)
    printed = buf.getvalue()
    assert "Intro para." in printed and "y = 2" in printed  # fence was printed whole
    assert "After." not in printed  # trailing paragraph still buffered
    stream.flush()
    assert "After." in buf.getvalue()


def test_think_splitter_handles_split_tags() -> None:
    s = _ThinkSplitter()
    out = []
    for chunk in ["Hi <thi", "nk>pondering</th", "ink> there"]:
        out += s.feed(chunk)
    out += s.flush()
    text = "".join(t for k, t in out if k == "text")
    reasoning = "".join(t for k, t in out if k == "reasoning")
    assert text == "Hi  there" and reasoning == "pondering"


def test_listen_splits_speech_and_stops_on_silence() -> None:
    import wave

    from baymon.cli import audio

    with wave.open(str(Path(__file__).parent / "fixtures" / "trip.wav"), "rb") as w:
        speech = w.readframes(w.getnframes())
    silence = b"\x00\x00" * audio.RATE * 3
    segments: list[bytes] = []

    def fake_transcribe(wav: bytes, prompt: str) -> str:
        segments.append(wav)
        return f"segment {len(segments)}"

    heard = audio.listen(fake_transcribe, lambda t: None, source=io.BytesIO(silence[:16000] + speech + silence),
                         no_speech_timeout_ms=None)
    assert heard.text.startswith("segment 1") and not heard.typed
    assert all(seg[:4] == b"RIFF" for seg in segments)


def test_listen_raises_when_nothing_said() -> None:
    from baymon.cli import audio

    with pytest.raises(audio.NothingHeard):
        audio.listen(lambda w, p: "x", lambda t: None, source=io.BytesIO(b"\x00\x00" * audio.RATE * 2))
