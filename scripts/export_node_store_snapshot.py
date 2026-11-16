"""Export a stopped node store without node identity or signing state.

Run under the selected node image. The backend checks managed containers; the
native read-only lease prevents Comet writers throughout the copy. Source file
fingerprints and staged app/native verification reject an incoherent copy.
"""

from __future__ import annotations

import argparse
import contextlib
import gzip
import hashlib
import json
import os
import select
import shutil
import stat
import subprocess
import tarfile
import tempfile
import unicodedata
from pathlib import Path

EXCLUDED_TREES = ("data/cs.wal", "xian/logs", "xian/snapshots")
EXCLUDED_NAMES = {
    "priv_validator_key.json",
    "priv_validator_state.json",
    "node_key.json",
    "lock.mdb",
}


def path_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def excluded(relative: str, protected: set[str]) -> bool:
    key = path_key(relative)
    if Path(relative).name in EXCLUDED_NAMES:
        return True
    if any(key == item or key.startswith(item + "/") for item in EXCLUDED_TREES):
        return True
    return any(key == item or key.startswith(item + "/") for item in protected)


def directory_files(
    home: Path, directory: Path, dirs: list[str], files: list[str], protected: set[str]
) -> list[str]:
    dirs[:] = [
        name
        for name in sorted(dirs)
        if not excluded((directory / name).relative_to(home).as_posix(), protected)
    ]
    selected = []
    for name in [*dirs, *sorted(files)]:
        path = directory / name
        relative = path.relative_to(home).as_posix()
        if excluded(relative, protected):
            continue
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise ValueError("snapshot source contains a symlink: " + relative)
        if stat.S_ISREG(mode):
            selected.append(relative)
        elif not stat.S_ISDIR(mode):
            raise ValueError("snapshot source contains a non-regular path: " + relative)
    return selected


def validate_absolute_identity_paths(home: Path) -> None:
    from xian.snapshot_restore import _IDENTITY_PATHS
    from xian.toml_utils import load

    with (home / "config/config.toml").open() as stream:
        config = load(stream)
    for key, default in _IDENTITY_PATHS.items():
        path = Path(config.get(key, default))
        if path.is_absolute() and not path.is_file():
            raise ValueError(
                "configured absolute identity path is unavailable in export namespace: " + str(path)
            )


def store_files(home: Path, *, aliases=()) -> list[str]:
    from xian.snapshot_restore import _protected_paths

    validate_absolute_identity_paths(home)
    protected = {path_key(p.as_posix()) for p in _protected_paths(home)}
    for alias in aliases:
        root = Path(alias)
        if not root.is_dir() or not root.samefile(home):
            raise ValueError(
                "source home alias is inaccessible or does not identify the donor home"
            )
        protected.update(path_key(p.as_posix()) for p in _protected_paths(root))
    selected = []
    for root in ("data", "xian"):
        start = home / root
        if not start.is_dir() or start.is_symlink():
            raise ValueError("snapshot source requires plain data/ and xian/ directories")
        for folder, dirs, files in os.walk(start):
            selected.extend(directory_files(home, Path(folder), dirs, files, protected))
    return sorted(selected)


def source_fingerprint(home: Path, files: list[str]) -> dict:
    result = {}
    for name in files:
        value = (home / name).stat()
        result[name] = (
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )
    return result


def require_unchanged_source(home: Path, files: list[str], before: dict, *, aliases=()) -> None:
    if store_files(home, aliases=aliases) != files or source_fingerprint(home, files) != before:
        raise ValueError("snapshot source changed during copy; stop all writers and retry")


def normalized_member(member: tarfile.TarInfo) -> tarfile.TarInfo:
    member.uid = member.gid = member.mtime = 0
    member.uname = member.gname = ""
    member.mode = 0o644
    member.pax_headers = {}
    return member


class SizedWriter:
    def __init__(
        self,
        target,
        maximum: int,
        label: str,
        *,
        disk_directory: Path | None = None,
        reserve: int = 0,
    ):
        self.target = target
        self.maximum = maximum
        self.label = label
        self.count = 0
        self.disk_directory = disk_directory
        self.reserve = reserve

    def write(self, data: bytes) -> int:
        if self.count + len(data) > self.maximum:
            raise ValueError("snapshot " + self.label + " exceeds configured limit")
        if (
            self.disk_directory is not None
            and shutil.disk_usage(self.disk_directory).free < len(data) + self.reserve
        ):
            raise ValueError("insufficient free disk while writing snapshot")
        size = self.target.write(data)
        self.count += size
        return size


def write_archive(home: Path, files: list[str], output: Path, limits) -> dict:
    with output.open("xb") as target:
        packed = SizedWriter(
            target,
            limits.max_archive_bytes,
            "compressed archive",
            disk_directory=output.parent,
            reserve=limits.min_free_disk_bytes,
        )
        with gzip.GzipFile(filename="", fileobj=packed, mode="wb", mtime=0) as compressed:
            unpacked = SizedWriter(
                compressed, limits.max_uncompressed_bytes, "uncompressed tar stream"
            )
            with tarfile.open(fileobj=unpacked, mode="w|", format=tarfile.PAX_FORMAT) as archive:
                for relative in files:
                    archive.add(home / relative, arcname=relative, filter=normalized_member)
    return {"snapshot_bytes": packed.count, "uncompressed_bytes": unpacked.count}


def sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def staged_output(source: Path, destination: Path) -> Path:
    descriptor, name = tempfile.mkstemp(prefix="." + destination.name + ".", dir=destination.parent)
    target = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_stream:
            shutil.copyfileobj(input_stream, output)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        target.unlink()
        raise
    return target


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish_outputs(
    archive: Path, manifest: Path, archive_output: Path, manifest_output: Path
) -> None:
    staged_archive = staged_output(archive, archive_output)
    staged_manifest = None
    try:
        staged_manifest = staged_output(manifest, manifest_output)
        staged_archive.chmod(0o644)
        staged_manifest.chmod(0o644)
        os.link(staged_archive, archive_output)
        try:
            sync_directory(archive_output.parent)
            os.link(staged_manifest, manifest_output)
        except BaseException:
            if archive_output.samefile(staged_archive):
                archive_output.unlink()
            raise
        sync_directory(manifest_output.parent)
    finally:
        staged_archive.unlink()
        if staged_manifest is not None:
            staged_manifest.unlink()


def lease_payload(process, timeout: float) -> dict:
    readable, _, _ = select.select([process.stdout], [], [], timeout)
    if not readable:
        raise ValueError("native store inspection timed out")
    raw = process.stdout.readline(65537)
    if not raw or len(raw) > 65536:
        raise ValueError("native store inspector returned invalid output")
    return json.loads(raw)


def reap_lease(process) -> int:
    try:
        return process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise ValueError("native store lease did not close")


def close_lease(process) -> None:
    process.stdin.close()
    try:
        code = reap_lease(process)
        if code != 0:
            raise ValueError("native store inspection failed: " + process.stderr.read(4096))
    finally:
        process.stdout.close()
        process.stderr.close()


@contextlib.contextmanager
def native_lease(db_dir: Path, backend: str, genesis_file: Path):
    from xian.full_node_store import NativeStoreInspectionLimits

    limits = NativeStoreInspectionLimits.from_environment()
    process = subprocess.Popen(
        [
            "xian-node-store-inspect",
            "--db-dir",
            str(db_dir),
            "--db-backend",
            backend,
            "--genesis-file",
            str(genesis_file),
            "--hold-open",
            *limits.command_arguments(),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        yield lease_payload(process, limits.timeout_seconds + 10)
        if process.poll() is not None:
            raise ValueError("native store lease ended before copy verification")
    finally:
        close_lease(process)


def source_backend(home: Path) -> str:
    from xian.toml_utils import load

    with (home / "config/config.toml").open() as stream:
        config = load(stream)
    if config.get("db_dir", "data") != "data":
        raise ValueError("full-store snapshots require db_dir=data")
    return config.get("db_backend", "goleveldb")


def source_genesis(home: Path) -> Path:
    from xian.toml_utils import load

    with (home / "config/config.toml").open() as stream:
        config = load(stream)
    path = Path(config.get("genesis_file", "config/genesis.json"))
    return (home / path).resolve(strict=True)


def signing_wallet(key_file: Path):
    from xian_py.wallet import Wallet

    if key_file.stat().st_mode & 0o077:
        raise ValueError("signing key file must be private (mode 0600)")
    with key_file.open() as stream:
        key = stream.read(257).strip()
    if len(key) != 64:
        raise ValueError("signing key file must contain one 32-byte hexadecimal private key")
    return Wallet(private_key=key)


def require_output_paths(args) -> None:
    if args.offline is not True:
        raise ValueError("snapshot export requires explicit offline acknowledgement")
    for output in (args.archive_output, args.manifest_output):
        if output.exists() or output.is_symlink():
            raise ValueError("snapshot output already exists")
    if args.archive_output == args.manifest_output:
        raise ValueError("snapshot outputs must differ")


def export_inputs(args):
    from xian.full_node_store import FullNodeStoreLimits
    from xian.snapshot_restore_status import validate_snapshot_restore_complete

    require_output_paths(args)
    validate_snapshot_restore_complete(args.home)
    wallet = signing_wallet(args.signing_key_file)
    limits = FullNodeStoreLimits()
    files = store_files(args.home, aliases=getattr(args, "source_home_alias", []))
    if len(files) > limits.max_members:
        raise ValueError("snapshot has too many source files")
    total = sum((args.home / name).stat().st_size for name in files)
    if total > limits.max_uncompressed_bytes:
        raise ValueError("snapshot source exceeds uncompressed limit")
    if shutil.disk_usage(args.work).free < 3 * total + limits.min_free_disk_bytes:
        raise ValueError("insufficient disk for staged full-store export")
    return wallet, limits, files


def copy_stopped_store(home: Path, staged: Path, files: list[str], *, aliases=()) -> tuple:
    from xian.node_admin import read_full_node_store_checkpoint

    with native_lease(home / "data", source_backend(home), source_genesis(home)) as native:
        before = source_fingerprint(home, files)
        for relative in files:
            target = staged / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(home / relative, target)
        require_unchanged_source(home, files, before, aliases=aliases)
        checkpoint = read_full_node_store_checkpoint(staged, reference_home=home)
        if (
            checkpoint["height"] != int(native["height"])
            or checkpoint["app_hash"] != native["app_hash"]
        ):
            raise ValueError("staged checkpoint differs from the locked source store")
    return checkpoint, native


def signed_manifest(checkpoint: dict, sizes: dict, archive: Path, wallet, url: str) -> dict:
    from xian.node_admin import full_node_store_manifest_payload, verify_full_node_store_manifest

    manifest = {
        "manifest_version": 2,
        "snapshot_kind": "full_node_store",
        **checkpoint,
        **sizes,
        "snapshot_url": url,
        "snapshot_sha256": sha256_file(archive),
        "signing_public_key": wallet.public_key,
    }
    manifest["signature"] = wallet.sign_msg(full_node_store_manifest_payload(manifest))
    verify_full_node_store_manifest(
        manifest,
        trusted_manifest_public_keys=[wallet.public_key],
        expected_chain_id=checkpoint["chain_id"],
    )
    return manifest


def export_snapshot(args) -> dict:
    from xian.node_admin import verify_full_node_store_checkpoint

    wallet, limits, files = export_inputs(args)
    with tempfile.TemporaryDirectory(prefix="store-", dir=args.work) as workspace:
        root = Path(workspace)
        staged = root / "home"
        staged.mkdir()
        checkpoint, native = copy_stopped_store(
            args.home, staged, files, aliases=getattr(args, "source_home_alias", [])
        )
        archive = root / "store.tar.gz"
        sizes = write_archive(staged, files, archive, limits)
        manifest = signed_manifest(checkpoint, sizes, archive, wallet, args.snapshot_url)
        verify_full_node_store_checkpoint(staged, manifest, reference_home=args.home)
        document = root / "manifest.json"
        document.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        publish_outputs(archive, document, args.archive_output, args.manifest_output)
    return {
        "ok": True,
        "archive_output": str(args.archive_output),
        "manifest_output": str(args.manifest_output),
        "manifest": manifest,
        "native_store": native,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("home", "work", "archive-output", "manifest-output", "signing-key-file"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--source-home-alias", action="append", default=[])
    parser.add_argument("--snapshot-url", required=True)
    parser.add_argument("--offline", action="store_true")
    print(json.dumps(export_snapshot(parser.parse_args()), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
