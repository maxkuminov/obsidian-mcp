"""Mutating tools act on the path as named — never on a symlink's target.

`validate_path` resolves the whole path, so an in-vault alias
`alias.md -> important.md` made every write tool operate on `important.md`
while reporting success for `alias.md`: a destructive write on a path nobody
named. `validate_mutable_path` resolves only the *parent* (keeping symlinked
folders inside the vault usable, which is a common Obsidian setup) and refuses
a symlinked final component, naming the target so the agent can act on the real
note instead.

Reads are deliberately unchanged — an alias reading as its target is what a
user expects from an alias.
"""
from pathlib import Path

import pytest

from src.services import vault as vault_service
from src.services.vault import validate_mutable_path


@pytest.fixture
def vault(monkeypatch, tmp_path):
    monkeypatch.setattr(vault_service.settings, "vault_path", str(tmp_path))
    return tmp_path


# ── the alias case, and what the error says ─────────────────────────────────


def test_alias_at_the_vault_root_is_refused_and_names_its_target(vault):
    (vault / "important.md").write_text("real", encoding="utf-8")
    (vault / "alias.md").symlink_to(vault / "important.md")

    with pytest.raises(ValueError) as excinfo:
        validate_mutable_path("alias.md")

    message = str(excinfo.value)
    assert "alias.md is a symbolic link to important.md" in message
    assert "operate on the target instead" in message


def test_nested_alias_names_the_canonical_vault_relative_target(vault):
    (vault / "real.md").write_text("real", encoding="utf-8")
    (vault / "Folder").mkdir()
    # A relative link with a `..` hop: the message must show where it lands in
    # the vault, not the link's literal (unusable) text.
    (vault / "Folder" / "alias.md").symlink_to(Path("..") / "real.md")

    with pytest.raises(ValueError) as excinfo:
        validate_mutable_path("Folder/alias.md")

    assert "is a symbolic link to real.md" in str(excinfo.value)


def test_dangling_link_is_refused(vault):
    (vault / "alias.md").symlink_to(vault / "gone.md")

    with pytest.raises(ValueError) as excinfo:
        validate_mutable_path("alias.md")

    # Still names the intended target: the caller learns what the alias meant.
    assert "is a symbolic link to gone.md" in str(excinfo.value)


def test_link_pointing_outside_the_vault_is_refused_without_leaking_the_path(
    vault, tmp_path_factory
):
    secret = tmp_path_factory.mktemp("outside") / "secret.md"
    secret.write_text("secret", encoding="utf-8")
    (vault / "alias.md").symlink_to(secret)

    with pytest.raises(ValueError) as excinfo:
        validate_mutable_path("alias.md")

    message = str(excinfo.value)
    assert "outside the vault" in message
    assert str(secret) not in message


# ── symlinked directories: allowed inside, rejected escaping ────────────────


def test_symlinked_ancestor_inside_the_vault_resolves_to_the_real_directory(vault):
    (vault / "Real").mkdir()
    (vault / "Shared").symlink_to(vault / "Real")

    resolved = validate_mutable_path("Shared/new.md")

    assert resolved == (vault / "Real" / "new.md").resolve()


def test_symlinked_ancestor_leaving_the_vault_is_refused(vault, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    (vault / "Escape").symlink_to(outside)

    with pytest.raises(ValueError, match="Path traversal denied"):
        validate_mutable_path("Escape/new.md")


def test_a_plain_path_returns_the_named_entry(vault):
    (vault / "Folder").mkdir()
    assert validate_mutable_path("Folder/note.md") == (
        vault / "Folder" / "note.md"
    ).resolve()


# ── the guards inherited from the existing validators ───────────────────────


@pytest.mark.parametrize(
    "bad_path",
    ["../escape.md", "Folder/../../escape.md", "/etc/passwd", "note\x00.md"],
)
def test_traversal_shapes_are_denied(vault, bad_path):
    with pytest.raises(ValueError, match="Path traversal denied"):
        validate_mutable_path(bad_path)


@pytest.mark.parametrize("bad_path", ["", ".", "Folder/"])
def test_non_file_shapes_are_denied(vault, bad_path):
    with pytest.raises(ValueError, match="Not a file path"):
        validate_mutable_path(bad_path)


def test_hidden_paths_are_denied(vault):
    (vault / ".obsidian").mkdir()
    with pytest.raises(ValueError, match="Hidden path denied"):
        validate_mutable_path(".obsidian/config.md")


def test_a_hidden_directory_reached_through_a_link_is_denied(vault):
    """The hidden check runs on the *resolved* relative path, so a symlinked
    folder cannot be used to smuggle a write into `.obsidian`."""
    (vault / ".obsidian").mkdir()
    (vault / "Config").symlink_to(vault / ".obsidian")

    with pytest.raises(ValueError, match="Hidden path denied"):
        validate_mutable_path("Config/app.json")


# ── multi-user vault roots ──────────────────────────────────────────────────


def test_per_user_root_is_used_for_both_the_check_and_the_target_name(
    vault, tmp_path_factory
):
    user_vault = tmp_path_factory.mktemp("user-7")
    (user_vault / "important.md").write_text("real", encoding="utf-8")
    (user_vault / "alias.md").symlink_to(user_vault / "important.md")
    vault_service._user_vault_cache[7] = Path(user_vault)
    try:
        # The single-user root has no such link — resolution must use user 7's.
        with pytest.raises(ValueError) as excinfo:
            validate_mutable_path("alias.md", user_id=7)
        assert "is a symbolic link to important.md" in str(excinfo.value)

        (user_vault / "Real").mkdir()
        (user_vault / "Shared").symlink_to(user_vault / "Real")
        assert validate_mutable_path("Shared/n.md", user_id=7) == (
            user_vault / "Real" / "n.md"
        ).resolve()
    finally:
        vault_service.clear_user_vault_cache()
