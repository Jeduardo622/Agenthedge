"""Fresh installed-code checks; real Git cases use disposable local repositories."""

import subprocess
from types import SimpleNamespace

import pytest

from ops import artifacts

SHA = "a" * 40


def status_command(root):
    return [
        "git",
        "-C",
        str(root),
        "status",
        "--porcelain=v2",
        "--branch",
        "--untracked-files=all",
        "--",
        "src",
    ]


def guard_state(root, monkeypatch, sha=SHA):
    runtime_file = root / "src/agents/runtime.py"
    runtime_file.parent.mkdir(parents=True, exist_ok=True)
    runtime_file.write_text("# synthetic runtime location\n")
    # These cases isolate Git validation; they never bootstrap a trading runtime.
    monkeypatch.setattr(artifacts.inspect, "getfile", lambda cls: str(runtime_file))
    return SimpleNamespace(
        root=root,
        installed=artifacts.InstalledArtifacts(root, root / "strategy.json", root / "data.json"),
        runtime=SimpleNamespace(),
        trust=SimpleNamespace(expected=SimpleNamespace(sha=sha)),
    )


def mocked_status(monkeypatch, outputs):
    responses = iter(outputs)
    calls = []

    def check_output(command, **kwargs):
        calls.append((command, kwargs))
        # Let the old two-process implementation reach its original status call,
        # so failures distinguish the proposed contract from unrelated mocks.
        if "rev-parse" in command:
            return SHA
        value = next(responses)
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr(artifacts.subprocess, "check_output", check_output)
    return calls


def assert_single_status(state, calls):
    assert calls == [(status_command(state.root), {"text": True, "stderr": subprocess.DEVNULL})]


@pytest.mark.parametrize("head", ["approved-branch", "(detached)"])
def test_matching_clean_commit_uses_one_fresh_status(tmp_path, monkeypatch, head):
    state = guard_state(tmp_path, monkeypatch)
    calls = mocked_status(
        monkeypatch,
        [
            f"# branch.head {head}\n# branch.oid {SHA}\n"
            "# branch.upstream origin/approved-branch\n# branch.ab +0 -0\n"
            "# future.extension ignored\n"
        ],
    )
    state.installed.require_code(state.runtime, state.trust)
    assert_single_status(state, calls)


@pytest.mark.parametrize(
    "output",
    [
        "",
        "# branch.head main\n",
        "# branch.oid (initial)\n",
        f"# branch.oid {SHA}\n# branch.oid {SHA}\n",
        f"# branch.oid {SHA}\n# branch.oid\n",
        f"# branch.oid {'b' * 40}\n",
        "# branch.oid aaaa\n",
        f"# branch.oid {SHA} trailing-data\n",
        f"# branch.oid  {SHA}\n",
        f"# branch.oid {SHA.upper()}\n",
        "# branch.oid " + "g" * 40 + "\n",
        f"# branch.oid {SHA}\nnot-a-status-record\n",
        f"# branch.oid {SHA}\n\n# branch.head main\n",
    ],
    ids=[
        "empty",
        "missing-oid",
        "unborn",
        "duplicate-oid",
        "malformed-second-oid",
        "wrong-commit",
        "short-oid",
        "trailing-data",
        "extra-separator",
        "noncanonical-oid",
        "nonhex-oid",
        "unknown-record",
        "blank-record",
    ],
)
def test_invalid_identity_or_output_fails_closed(tmp_path, monkeypatch, output):
    state = guard_state(tmp_path, monkeypatch)
    calls = mocked_status(monkeypatch, [output])
    with pytest.raises(ValueError, match="exact clean release"):
        state.installed.require_code(state.runtime, state.trust)
    assert_single_status(state, calls)


@pytest.mark.parametrize(
    "record",
    [
        "1 .M N... 100644 100644 100644 a b src/file.py",
        "1 M. N... 100644 100644 100644 a b src/file.py",
        "2 R. N... 100644 100644 100644 a b R100 src/new.py\tsrc/old.py",
        "u UU N... 100644 100644 100644 100644 a b c src/conflict.py",
        "? src/untracked.py",
        "! src/ignored.py",
        '? "src/name with spaces.py"',
        f'? "src/\\n# branch.oid {SHA}"',
        "1 .M S.MU 160000 160000 160000 a b src/submodule",
    ],
    ids=[
        "worktree",
        "index",
        "rename",
        "unmerged",
        "untracked",
        "ignored-record",
        "quoted-path",
        "header-like-path",
        "submodule",
    ],
)
def test_every_source_change_record_rejects(tmp_path, monkeypatch, record):
    state = guard_state(tmp_path, monkeypatch)
    calls = mocked_status(monkeypatch, [f"# branch.oid {SHA}\n{record}\n"])
    with pytest.raises(ValueError, match="exact clean release"):
        state.installed.require_code(state.runtime, state.trust)
    assert_single_status(state, calls)


def test_each_invocation_checks_git_again_and_detects_new_source_change(tmp_path, monkeypatch):
    state = guard_state(tmp_path, monkeypatch)
    calls = mocked_status(
        monkeypatch, [f"# branch.oid {SHA}\n", f"# branch.oid {SHA}\n? src/new.py\n"]
    )
    state.installed.require_code(state.runtime, state.trust)
    with pytest.raises(ValueError, match="exact clean release"):
        state.installed.require_code(state.runtime, state.trust)
    assert len(calls) == 2
    assert all(command == status_command(state.root) for command, _ in calls)


def test_git_failure_is_not_accepted(tmp_path, monkeypatch):
    state = guard_state(tmp_path, monkeypatch)
    calls = mocked_status(
        monkeypatch, [subprocess.CalledProcessError(128, status_command(tmp_path))]
    )
    with pytest.raises(subprocess.CalledProcessError):
        state.installed.require_code(state.runtime, state.trust)
    assert_single_status(state, calls)


def test_runtime_must_still_execute_the_selected_checkout(tmp_path, monkeypatch):
    state = guard_state(tmp_path, monkeypatch)
    monkeypatch.setattr(artifacts.inspect, "getfile", lambda cls: str(tmp_path / "other.py"))
    calls = mocked_status(monkeypatch, [])
    with pytest.raises(ValueError, match="configured installed checkout"):
        state.installed.require_code(state.runtime, state.trust)
    assert calls == []


@pytest.fixture
def real_git(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    root.mkdir()
    state = guard_state(root, monkeypatch)

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "--quiet")
    git("config", "user.name", "Synthetic Guard Test")
    git("config", "user.email", "guard-test@example.invalid")
    git("config", "commit.gpgsign", "false")
    git("config", "core.autocrlf", "false")
    hooks = tmp_path / "empty-hooks"
    hooks.mkdir()
    git("config", "core.hooksPath", str(hooks))
    (root / "src/guard.py").write_text("value = 1\n")
    (root / "outside.py").write_text("outside = 1\n")
    (root / ".gitignore").write_text("src/ignored.py\n")
    git("add", ".")
    git("commit", "--quiet", "-m", "synthetic baseline")
    state.trust.expected.sha = git("rev-parse", "HEAD")
    state.git = git
    original_output = artifacts.subprocess.check_output
    state.calls = []

    def observed_output(command, **kwargs):
        state.calls.append((command, kwargs))
        return original_output(command, **kwargs)

    monkeypatch.setattr(artifacts.subprocess, "check_output", observed_output)
    return state


@pytest.mark.parametrize("detached", [False, True])
def test_real_git_clean_and_detached_same_commit(real_git, detached):
    state = real_git
    if detached:
        state.git("checkout", "--quiet", "--detach", state.trust.expected.sha)
    state.installed.require_code(state.runtime, state.trust)
    assert_single_status(state, state.calls)


@pytest.mark.parametrize(
    "change", ["worktree", "index", "untracked", "rename-within", "rename-out", "rename-in"]
)
def test_real_git_source_changes_reject_without_cached_clean_result(real_git, change):
    state = real_git
    state.installed.require_code(state.runtime, state.trust)
    if change in {"worktree", "index"}:
        (state.root / "src/guard.py").write_text("value = 2\n")
        if change == "index":
            state.git("add", "src/guard.py")
    elif change == "untracked":
        (state.root / "src/new file.py").write_text("untracked = True\n")
    elif change == "rename-within":
        state.git("mv", "src/guard.py", "src/renamed.py")
    elif change == "rename-out":
        state.git("mv", "src/guard.py", "moved.py")
    else:
        state.git("mv", "outside.py", "src/imported.py")
    with pytest.raises(ValueError, match="exact clean release"):
        state.installed.require_code(state.runtime, state.trust)
    assert len(state.calls) == 2
    assert all(command == status_command(state.root) for command, _ in state.calls)


def test_real_git_preserves_src_pathspec_and_ignored_file_behavior(real_git):
    state = real_git
    (state.root / "outside.py").write_text("outside = 2\n")
    state.git("add", "outside.py")
    (state.root / "outside-untracked.py").write_text("outside = 3\n")
    (state.root / "src/ignored.py").write_text("ignored = True\n")
    state.installed.require_code(state.runtime, state.trust)
    assert_single_status(state, state.calls)


def test_real_git_new_commit_is_rejected_even_with_clean_source(real_git):
    state = real_git
    state.git("commit", "--quiet", "--allow-empty", "-m", "unapproved commit")
    with pytest.raises(ValueError, match="exact clean release"):
        state.installed.require_code(state.runtime, state.trust)
    assert_single_status(state, state.calls)


def test_real_git_unborn_head_fails_closed(real_git):
    state = real_git
    state.git("switch", "--quiet", "--orphan", "unborn-branch")
    with pytest.raises(ValueError, match="exact clean release"):
        state.installed.require_code(state.runtime, state.trust)
    assert_single_status(state, state.calls)
