from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path


class LauncherError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PinnedLauncher:
    argv_prefix: tuple[str, ...]
    identities: tuple[tuple[str, int, int], ...]

    def verify(self) -> None:
        try:
            for path_text, device, inode in self.identities:
                path = Path(path_text)
                metadata = path.stat(follow_symlinks=False)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or (metadata.st_dev, metadata.st_ino) != (device, inode)
                    or stat.S_IMODE(metadata.st_mode) & 0o022
                    or not os.access(path, os.X_OK)
                ):
                    raise LauncherError
        except LauncherError:
            raise
        except Exception:
            raise LauncherError from None


def resolve_launcher(codex_binary: str, *, node_binary: str | None = None) -> PinnedLauncher:
    try:
        original = Path(codex_binary)
        if not original.is_absolute():
            raise LauncherError
        script = original.resolve(strict=True)
        script_metadata = script.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(script_metadata.st_mode)
            or stat.S_IMODE(script_metadata.st_mode) & 0o022
            or not os.access(script, os.X_OK)
        ):
            raise LauncherError
        with script.open("rb") as stream:
            prefix = stream.read(128)
        script_identity = (str(script), script_metadata.st_dev, script_metadata.st_ino)
        if not prefix.startswith(b"#!"):
            launcher = PinnedLauncher((str(script),), (script_identity,))
            launcher.verify()
            return launcher
        first_line = prefix.splitlines()[0]
        if first_line != b"#!/usr/bin/env node":
            raise LauncherError
        candidate = node_binary if node_binary is not None else shutil.which("node")
        if candidate is None:
            raise LauncherError
        node = Path(candidate)
        if not node.is_absolute():
            raise LauncherError
        node = node.resolve(strict=True)
        node_metadata = node.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(node_metadata.st_mode)
            or stat.S_IMODE(node_metadata.st_mode) & 0o022
            or not os.access(node, os.X_OK)
        ):
            raise LauncherError
        node_identity = (str(node), node_metadata.st_dev, node_metadata.st_ino)
        launcher = PinnedLauncher(
            (str(node), str(script)),
            (node_identity, script_identity),
        )
        launcher.verify()
        return launcher
    except LauncherError:
        raise
    except Exception:
        raise LauncherError from None
