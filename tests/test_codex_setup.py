"""A24: Codex MCP migration preserves unrelated configuration and hook trust."""

import tomllib

import pytest

from khipumaq import setup


LEGACY = '''[mcp_servers.llm-memory]
command = "python"
args = [
    "-m",
    "llm_memory.mcp_server",
]

'''
LEGACY_ENV = '''[mcp_servers.llm-memory.env]
PYTHONPATH = "/old/checkout"

'''
UNRELATED = '''model = "test-model"

[mcp_servers.other]
command = "other-server"
args = ["--flag"]

[mcp_servers.other.env]
TOKEN = "test-only"

[projects."/tmp/project with spaces"]
trust_level = "trusted"

[hooks.state."unrelated-hook"]
enabled = false
trusted_hash = "keep-this-hash"

[tui]
notifications = ["agent-turn-complete"]

'''
TRUST = '''[hooks.state."test-session-end"]
enabled = true
trusted_hash = "session-hash"

[hooks.state."test-subagent-stop"]
enabled = true
trusted_hash = "subagent-hash"

'''
STALE_SERVER = '''[mcp_servers.khipumaq]
command = "old-khipumaq"
args = ["old-argument"]

[mcp_servers.khipumaq.env]
STALE = "remove-me"

'''


@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    home.mkdir()

    def hooks_list(binary, requested_home):
        assert binary == "test-codex-never-executed"
        assert requested_home == home
        return [
            {"command": setup._codex_hook_command(), "key": key,
             "currentHash": digest}
            for key, digest in (
                ("test-session-end", "session-hash"),
                ("test-subagent-stop", "subagent-hash"),
            )
        ]

    monkeypatch.setattr(setup, "_hooks_list", hooks_list)
    return home


def _install(home):
    setup.install_codex(home, "test-codex-never-executed")
    return (home / "config.toml").read_text(encoding="utf-8")


def _assert_mcp(document):
    entry = setup._mcp_entry()
    assert document["mcp_servers"]["khipumaq"] == {
        "command": entry["command"], "args": entry["args"],
    }


@pytest.mark.parametrize("subtable", ["", LEGACY_ENV], ids=["parent", "with-env"])
def test_install_migrates_multiline_legacy_server(codex_home, subtable):
    config = codex_home / "config.toml"
    config.write_text(LEGACY + subtable, encoding="utf-8")

    text = _install(codex_home)

    document = tomllib.loads(text)
    assert "llm-memory" not in document["mcp_servers"]
    assert "[mcp_servers.llm-memory" not in text
    _assert_mcp(document)


@pytest.mark.parametrize("operation", ["helper", "install"])
def test_unrelated_same_name_server_is_byte_identical(codex_home, operation):
    legacy = LEGACY.replace("llm_memory.mcp_server", "someone_else.server") + LEGACY_ENV
    before = UNRELATED + legacy
    if operation == "helper":
        after = setup._set_codex_mcp(
            before, "llm-memory", only_if="llm_memory.mcp_server",
        )
        assert after.encode() == before.encode()
    else:
        config = codex_home / "config.toml"
        config.write_bytes(before.encode())
        after = _install(codex_home)
        assert legacy.encode() in config.read_bytes()
        _assert_mcp(tomllib.loads(after))
    assert tomllib.loads(after)["mcp_servers"]["llm-memory"] == (
        tomllib.loads(before)["mcp_servers"]["llm-memory"]
    )


def test_install_preserves_other_tables_and_is_byte_idempotent(codex_home):
    before = UNRELATED + LEGACY + LEGACY_ENV + STALE_SERVER + TRUST
    config = codex_home / "config.toml"
    config.write_text(before, encoding="utf-8")

    first = tomllib.loads(_install(codex_home))
    first_bytes = config.read_bytes()
    second = tomllib.loads(_install(codex_home))

    assert config.read_bytes() == first_bytes
    _assert_mcp(first)
    expected = tomllib.loads(before)
    for document in (expected, first):
        for name in ("llm-memory", "khipumaq"):
            document["mcp_servers"].pop(name, None)
    assert first == expected
    for key, value in tomllib.loads(TRUST)["hooks"]["state"].items():
        assert second["hooks"]["state"][key] == value


@pytest.mark.parametrize("exists", [False, True], ids=["missing", "empty"])
def test_install_initializes_config(codex_home, exists):
    if exists:
        (codex_home / "config.toml").touch()

    document = tomllib.loads(_install(codex_home))

    _assert_mcp(document)
    assert document["hooks"] == tomllib.loads(TRUST)["hooks"]


@pytest.mark.parametrize("operation", ["helper", "uninstall"])
def test_remove_khipumaq_preserves_rest_including_hook_trust(codex_home, operation):
    remainder = UNRELATED + LEGACY + LEGACY_ENV + TRUST
    before = UNRELATED + STALE_SERVER + LEGACY + LEGACY_ENV + TRUST
    if operation == "helper":
        after = setup._set_codex_mcp(before, "khipumaq")
    else:
        config = codex_home / "config.toml"
        config.write_text(before, encoding="utf-8")
        setup.uninstall_codex(codex_home)
        after = config.read_text(encoding="utf-8")
    assert after == remainder
    document = tomllib.loads(after)
    assert "khipumaq" not in document["mcp_servers"]
    assert document == tomllib.loads(remainder)


def test_uninstall_does_not_create_missing_config(tmp_path):
    home = tmp_path / "absent-codex-home"

    setup.uninstall_codex(home)

    assert not (home / "config.toml").exists()
    assert not home.exists()
