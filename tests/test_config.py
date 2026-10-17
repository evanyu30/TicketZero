"""The .env loader: quoting, comments, export prefix, no interpolation, shell variables win."""
import os

from ticketzero.config import load_env_file


def test_load_env_file(tmp_path, monkeypatch):
    for k in ("TZ_A", "TZ_B", "TZ_C", "TZ_D", "TZ_E", "TZ_SHELL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TZ_SHELL", "from-shell")
    env = tmp_path / ".env"
    env.write_text(
        "# comment\n"
        "\n"
        "TZ_A='abc$def$ghi'\n"
        'export TZ_B="quoted # not a comment"\n'
        "TZ_C=plain$value # trailing comment\n"
        "TZ_D=\n"
        "not a valid line\n"
        "TZ_SHELL=from-file\n"
        "TZ_E = spaced \n"
    )
    load_env_file(env)
    assert os.environ["TZ_A"] == "abc$def$ghi"
    assert os.environ["TZ_B"] == "quoted # not a comment"
    assert os.environ["TZ_C"] == "plain$value"
    assert os.environ["TZ_D"] == ""
    assert os.environ["TZ_E"] == "spaced"
    assert os.environ["TZ_SHELL"] == "from-shell"


def test_missing_env_file_is_ignored(tmp_path):
    load_env_file(tmp_path / "nope.env")
