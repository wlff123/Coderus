from __future__ import annotations

import ctypes
import json
import os
import sys
from pathlib import Path

_SYS_LANDLOCK_CREATE_RULESET = 444
_SYS_LANDLOCK_ADD_RULE = 445
_SYS_LANDLOCK_RESTRICT_SELF = 446
_LANDLOCK_CREATE_RULESET_VERSION = 1
_LANDLOCK_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38

_EXECUTE = 1 << 0
_WRITE_FILE = 1 << 1
_READ_FILE = 1 << 2
_READ_DIR = 1 << 3
_REMOVE_DIR = 1 << 4
_REMOVE_FILE = 1 << 5
_MAKE_CHAR = 1 << 6
_MAKE_DIR = 1 << 7
_MAKE_REG = 1 << 8
_MAKE_SOCK = 1 << 9
_MAKE_FIFO = 1 << 10
_MAKE_BLOCK = 1 << 11
_MAKE_SYM = 1 << 12
_REFER = 1 << 13
_TRUNCATE = 1 << 14

_READ_ONLY = _READ_FILE | _READ_DIR
_READ_EXECUTE = _READ_ONLY | _EXECUTE
_READ_WRITE = (
    _WRITE_FILE
    | _READ_FILE
    | _READ_DIR
    | _REMOVE_DIR
    | _REMOVE_FILE
    | _MAKE_CHAR
    | _MAKE_DIR
    | _MAKE_REG
    | _MAKE_SOCK
    | _MAKE_FIFO
    | _MAKE_BLOCK
    | _MAKE_SYM
    | _REFER
    | _TRUNCATE
)
_HANDLED_ACCESS = _READ_WRITE | _EXECUTE


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _PathBeneathAttr(ctypes.Structure):
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int),
    ]


def landlock_abi() -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.syscall(
        _SYS_LANDLOCK_CREATE_RULESET,
        0,
        0,
        _LANDLOCK_CREATE_RULESET_VERSION,
    )
    if result < 0:
        raise OSError(ctypes.get_errno(), "landlock ABI query failed")
    return int(result)


def apply_landlock(
    *,
    workspace: Path,
    workspace_writable: bool,
    workspace_executable: bool,
    run_root: Path,
    runtime_paths: list[Path],
) -> None:
    if sys.platform != "linux":
        raise RuntimeError("Landlock is required on Linux only")
    abi = landlock_abi()
    if abi < 3:
        raise RuntimeError("Landlock ABI 3 or newer is required")

    libc = ctypes.CDLL(None, use_errno=True)
    ruleset_attr = _RulesetAttr(_HANDLED_ACCESS)
    ruleset_fd = libc.syscall(
        _SYS_LANDLOCK_CREATE_RULESET,
        ctypes.byref(ruleset_attr),
        ctypes.sizeof(ruleset_attr),
        0,
    )
    if ruleset_fd < 0:
        raise OSError(ctypes.get_errno(), "landlock ruleset creation failed")

    try:
        run_root = run_root.resolve(strict=True)
        workspace_access = _READ_WRITE if workspace_writable else _READ_ONLY
        if workspace_executable:
            workspace_access |= _EXECUTE
        _add_path_rule(libc, ruleset_fd, workspace, workspace_access)
        _add_path_rule(libc, ruleset_fd, run_root, _READ_WRITE)
        for path in _deduplicate_paths([*_system_runtime_paths(), *runtime_paths]):
            if path.exists():
                if path == Path("/dev/null"):
                    access = _READ_FILE | _WRITE_FILE
                elif path.is_relative_to(run_root):
                    access = _READ_WRITE | _EXECUTE
                else:
                    access = _READ_EXECUTE
                _add_path_rule(libc, ruleset_fd, path, access)

        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) < 0:
            raise OSError(ctypes.get_errno(), "no_new_privs failed")
        if libc.syscall(_SYS_LANDLOCK_RESTRICT_SELF, ruleset_fd, 0) < 0:
            raise OSError(ctypes.get_errno(), "landlock restriction failed")
    finally:
        os.close(ruleset_fd)


def _add_path_rule(libc: ctypes.CDLL, ruleset_fd: int, path: Path, access: int) -> None:
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        access &= ~_READ_DIR
        access &= ~(_REFER | _REMOVE_DIR | _MAKE_DIR | _MAKE_REG | _MAKE_CHAR)
        access &= ~(_MAKE_SOCK | _MAKE_FIFO | _MAKE_BLOCK | _MAKE_SYM)
    parent_fd = os.open(resolved, os.O_PATH | os.O_CLOEXEC)
    try:
        path_attr = _PathBeneathAttr(access, parent_fd)
        result = libc.syscall(
            _SYS_LANDLOCK_ADD_RULE,
            ruleset_fd,
            _LANDLOCK_RULE_PATH_BENEATH,
            ctypes.byref(path_attr),
            0,
        )
        if result < 0:
            raise OSError(ctypes.get_errno(), "landlock path rule failed")
    finally:
        os.close(parent_fd)


def _system_runtime_paths() -> list[Path]:
    candidates = (
        "/usr",
        "/bin",
        "/sbin",
        "/lib",
        "/lib64",
        "/etc/ssl",
        "/etc/pki",
        "/etc/ca-certificates",
        "/etc/resolv.conf",
        "/etc/hosts",
        "/etc/nsswitch.conf",
        "/etc/passwd",
        "/etc/group",
        "/etc/localtime",
        "/etc/ld.so.cache",
        "/etc/ld.so.conf",
        "/etc/ld.so.conf.d",
        "/dev/null",
        "/dev/zero",
        "/dev/random",
        "/dev/urandom",
        "/proc/self",
        "/proc/thread-self",
        "/proc/cpuinfo",
        "/proc/meminfo",
        "/proc/version",
        "/sys/devices/system/cpu",
    )
    return [Path(candidate) for candidate in candidates]


def _deduplicate_paths(paths: list[Path]) -> list[Path]:
    unique: dict[str, Path] = {}
    for path in paths:
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            continue
        unique.setdefault(str(resolved), resolved)
    return list(unique.values())


def _launcher_main() -> int:
    if len(sys.argv) < 4 or sys.argv[2] != "--":
        print("invalid Coderus Landlock launcher arguments", file=sys.stderr)
        return 125
    try:
        policy = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        workspace = Path(policy["workspace"])
        run_root = Path(policy["run_root"])
        runtime_paths = [Path(value) for value in policy["runtime_paths"]]
        apply_landlock(
            workspace=workspace,
            workspace_writable=bool(policy["workspace_writable"]),
            workspace_executable=bool(policy["workspace_executable"]),
            run_root=run_root,
            runtime_paths=runtime_paths,
        )
        command = sys.argv[3:]
        os.execvpe(command[0], command, os.environ)
    except BaseException as exc:
        print(f"Coderus Landlock isolation failed: {type(exc).__name__}", file=sys.stderr)
        return 125


if __name__ == "__main__":
    raise SystemExit(_launcher_main())
