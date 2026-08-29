"""vault 路径穿越回归测试（issue #2）。

隔离策略:
  - monkeypatch vault.VAULT_ROOT 到 tmp_path（CI 上 OBSIDIAN_VAULT 默认 D:/MYWORLD 不存在）
  - vault.py 是纯文件操作，无需 mock LLM
"""
from __future__ import annotations

import pytest

from app.obsidian import vault


@pytest.fixture
def vroot(tmp_path, monkeypatch):
    """构造最小 vault + 一个 vault 外的 secret 文件。"""
    root = tmp_path / "vault"
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "ok.md").write_text("hello vault", encoding="utf-8")
    outside = tmp_path / "secret.txt"
    outside.write_text("TOP-SECRET", encoding="utf-8")
    monkeypatch.setattr(vault, "VAULT_ROOT", root)
    return root, outside


def test_read_file_traversal_blocked(vroot):
    root, outside = vroot
    result = vault.read_file("../secret.txt")
    assert "路径越界" in result
    assert "TOP-SECRET" not in result  # 内容未泄露


def test_read_file_absolute_path_blocked(vroot):
    root, outside = vroot
    result = vault.read_file(str(outside))  # 绝对路径（linux /tmp/... 或 windows D:\\...）
    assert "路径越界" in result
    assert "TOP-SECRET" not in result


def test_write_file_traversal_blocked(vroot):
    root, outside = vroot
    result = vault.write_file("../evil.txt", "pwned")
    assert "路径越界" in result
    assert not (root.parent / "evil.txt").exists()


def test_append_to_file_traversal_blocked(vroot):
    root, outside = vroot
    result = vault.append_to_file("../secret.txt", "pwned")
    assert "路径越界" in result
    assert outside.read_text(encoding="utf-8") == "TOP-SECRET"  # 未修改


def test_search_notes_folder_traversal_blocked(vroot):
    root, outside = vroot
    result = vault.search_notes("TOP-SECRET", folder="../")
    assert "路径越界" in result
    assert "TOP-SECRET" not in result


def test_read_folder_traversal_blocked(vroot):
    root, outside = vroot
    result = vault.read_folder("../../")
    assert "路径越界" in result


def test_read_file_inside_vault_ok(vroot):
    root, outside = vroot
    result = vault.read_file("notes/ok.md")
    assert "路径越界" not in result
    assert "hello vault" in result


def test_write_file_inside_vault_ok(vroot):
    root, outside = vroot
    result = vault.write_file("notes/new.md", "created")
    assert "路径越界" not in result
    assert (root / "notes" / "new.md").read_text(encoding="utf-8") == "created"


def test_read_folder_inside_vault_ok(vroot):
    root, outside = vroot
    result = vault.read_folder("notes")
    assert "路径越界" not in result
    assert "hello vault" in result
