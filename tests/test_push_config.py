"""Tests for push_config.load_commands and the ready-made files in configs/push/."""

from pathlib import Path

import pytest

from push_config import load_commands

PUSH_DIR = Path(__file__).resolve().parent.parent / "configs" / "push"


def test_comments_and_blank_lines_are_dropped(tmp_path):
    f = tmp_path / "cfg.txt"
    f.write_text("! comment\n\nrouter rip\n   ! indented comment\n version 2\n")
    # Indentation is kept: IOS ignores it, but it keeps the echo readable.
    assert load_commands(f) == ["router rip", " version 2"]


@pytest.mark.parametrize("path", sorted(PUSH_DIR.glob("*.txt")), ids=lambda p: p.name)
def test_push_files_hold_config_mode_lines_only(path):
    commands = [c.strip() for c in load_commands(path)]
    assert commands, "file has no commands"
    # send_config_set adds these itself; a second 'end' would drop out early.
    assert not {"enable", "configure terminal", "conf t", "end", "write memory"} & set(commands)
