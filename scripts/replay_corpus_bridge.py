"""Import a pinned ciphertext pair; decrypt private replay data only in a secret step.

The public Git transport contains blocks.tar.gz.enc and manifest.json. Public
configuration and native ledger records are structurally checked before sealing
and after authentication; immutable ledger payloads may contain sensitive data.
This module does not publish, build images, run replay, or start a network.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

MAGIC = b"XIAN-REPLAY-AEAD\x01"
HEADER_LIMIT = 4096
AEAD_OVERHEAD = 24 + 16
PUBLIC_PAIR = frozenset({"blocks.tar.gz.enc", "manifest.json"})
PLAIN_PAIR = frozenset({"blocks.tar.gz", "manifest.json"})
REPOSITORIES = frozenset({"xian-stack", "xian-abci", "xian-contracting", "xian-py", "xian-configs"})
DB_NAMES = frozenset({"blockstore.db", "state.db", "evidence.db", "tx_index.db"})
REQUIRED_CONFIG = frozenset(
    {"home/config/config.toml", "home/config/genesis.json", "home/config/xian.toml"}
)
PUBLIC_CONFIG = REQUIRED_CONFIG | {"home/config/addrbook.json"}
DIRECTORIES = frozenset({"home", "home/config", "home/config/state-patches", "home/data"}) | {
    f"home/data/{name}" for name in DB_NAMES
}
CREDENTIAL_KEYS = frozenset(
    {
        "privatekey",
        "privkey",
        "secretkey",
        "signingkey",
        "seed",
        "walletseed",
        "seedphrase",
        "mnemonic",
        "password",
        "apikey",
        "accesstoken",
        "refreshtoken",
        "authorization",
        "clientsecret",
        "spendingkey",
        "viewingkey",
        "dsn",
        "databaseurl",
        "connectionstring",
        "psqlconn",
    }
)
POLICY_PATTERNS = {
    "execution_revision": r"xvm-execution-[1-9][0-9]*",
    "gas_schedule": r"xvm-gas-[1-9][0-9]*",
    "bytecode_version": r"xvm-[1-9][0-9]*",
    "set_codec": r"xian-set-[1-9][0-9]*",
    "compiler_admission_revision": r"xian-admission-[1-9][0-9]*",
    "tx_admission_revision": r"xian-fastpath-admission-[1-9][0-9]*",
}
ENV_LINK = re.compile(r"(?:[A-Za-z0-9_-]+/)*\.venv")
DB_FILE = re.compile(
    r"(?:CURRENT(?:\.bak)?|LOCK|LOG(?:\.old)?|MANIFEST-[0-9]{6,}|[0-9]{6,}\.(?:ldb|sst|log))"
)
PATCH_FILE = re.compile(r"home/config/state-patches/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\.json")


@dataclass(frozen=True)
class Inputs:
    mode: str
    replay_run_id: str | None = None
    replay_corpus_ref: str | None = None
    archive_sha256: str | None = None
    manifest_sha256: str | None = None


@dataclass(frozen=True)
class Limits:
    max_archive_bytes: int = 256 * 1024 * 1024
    max_manifest_bytes: int = 16 * 1024 * 1024

    def __post_init__(self):
        for value in (self.max_archive_bytes, self.max_manifest_bytes):
            require(type(value) is int and value > 0, "Size limits must be positive integers")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def matches(value, pattern):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def digest(data):
    return hashlib.sha256(data).hexdigest()


def parse_inputs(environment):
    names = (
        "REPLAY_RUN_ID",
        "REPLAY_CORPUS_REF",
        "REPLAY_ARCHIVE_SHA256",
        "REPLAY_MANIFEST_SHA256",
    )
    values = [environment.get(name, "") for name in names]
    require(all(isinstance(value, str) for value in values), "Replay inputs must be strings")
    run, ref, archive_sha, manifest_sha = values
    require(not (run and any(values[1:])), "Prior-run and corpus-ref modes cannot be mixed")
    if run:
        require(matches(run, r"[1-9][0-9]*"), "REPLAY_RUN_ID must be a canonical positive integer")
        return Inputs("prior", replay_run_id=run)
    if any(values[1:]):
        require(all(values[1:]), "Corpus ref and both SHA256 inputs are required together")
        require(
            matches(ref, r"[0-9a-f]{40}"), "REPLAY_CORPUS_REF must be an immutable lowercase commit"
        )
        require(matches(archive_sha, r"[0-9a-f]{64}"), "Invalid REPLAY_ARCHIVE_SHA256")
        require(matches(manifest_sha, r"[0-9a-f]{64}"), "Invalid REPLAY_MANIFEST_SHA256")
        return Inputs(
            "ref", replay_corpus_ref=ref, archive_sha256=archive_sha, manifest_sha256=manifest_sha
        )
    return Inputs("normal")


def ref_inputs(environment):
    inputs = parse_inputs(environment)
    require(inputs.mode == "ref", "This command requires corpus-ref mode")
    return inputs


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "Duplicate JSON field")
        result[key] = value
    return result


def json_object(data):
    value = json.loads(data, object_pairs_hook=unique_object)
    require(isinstance(value, dict), "Expected a JSON object")
    return value


def exact_fields(value, fields, label):
    require(isinstance(value, dict) and set(value) == set(fields), f"Invalid {label} fields")


def validate_map(mapping, height, label):
    require(isinstance(mapping, dict) and len(mapping) == height, f"Incomplete {label} map")
    for key, value in mapping.items():
        require(matches(key, r"[1-9][0-9]*") and int(key) <= height, f"Noncanonical {label} height")
        require(matches(value, r"[0-9a-f]{64}"), f"Invalid {label} digest")


def validate_audit(audit):
    fields = {
        "schema_version",
        "original_manifest_sha256",
        "original_dirty_repositories",
        "excluded_untracked_environment_links",
        "tracked_source_unchanged",
        "archive_unchanged",
        "expected_results_unchanged",
        "expected_app_hashes_unchanged",
        "image_source_proof_sha256",
    }
    exact_fields(audit, fields, "source environment audit")
    require(
        type(audit["schema_version"]) is int and audit["schema_version"] == 1,
        "Invalid source audit schema",
    )
    for name in ("original_manifest_sha256", "image_source_proof_sha256"):
        require(matches(audit[name], r"[0-9a-f]{64}"), "Invalid source audit digest")
    for name in (
        "tracked_source_unchanged",
        "archive_unchanged",
        "expected_results_unchanged",
        "expected_app_hashes_unchanged",
    ):
        require(audit[name] is True, "Source audit must attest unchanged payloads")
    validate_audit_links(audit)


def validate_audit_links(audit):
    dirty, links = (
        audit["original_dirty_repositories"],
        audit["excluded_untracked_environment_links"],
    )
    require(
        isinstance(dirty, list)
        and all(isinstance(name, str) for name in dirty)
        and len(dirty) == len(set(dirty))
        and set(dirty) <= REPOSITORIES,
        "Invalid original dirty repositories",
    )
    require(
        isinstance(links, dict) and set(links) == set(dirty),
        "Invalid audited environment repositories",
    )
    for paths in links.values():
        require(isinstance(paths, list) and 0 < len(paths) <= 8, "Invalid environment link list")
        require(
            all(
                matches(path, ENV_LINK) and ".." not in PurePosixPath(path).parts for path in paths
            ),
            "Environment links must be relative .venv paths",
        )


def validate_manifest(data):
    required = {
        "height",
        "app_hash",
        "chain_id",
        "expected_results",
        "expected_app_hashes",
        "repositories",
        "dirty_repositories",
        "image_id",
        "execution_policy",
    }
    require(
        isinstance(data, dict) and required <= set(data) <= required | {"source_environment_audit"},
        "Invalid replay manifest fields",
    )
    height = data["height"]
    require(
        type(height) is int and 0 < height < 2**63, "Replay height must be a positive native height"
    )
    require(
        matches(data["chain_id"], r"[A-Za-z0-9][A-Za-z0-9_.-]{0,49}"), "Invalid replay chain ID"
    )
    require(matches(data["app_hash"], r"[0-9a-f]{64}"), "Invalid final replay root")
    validate_map(data["expected_results"], height, "transaction results")
    validate_map(data["expected_app_hashes"], height, "application roots")
    require(
        data["expected_app_hashes"][str(height)] == data["app_hash"],
        "Final application root mismatch",
    )
    validate_manifest_sources(data)
    if "source_environment_audit" in data:
        validate_audit(data["source_environment_audit"])
    return data


def validate_manifest_sources(data):
    refs = data["repositories"]
    exact_fields(refs, REPOSITORIES, "source repository")
    require(
        all(matches(ref, r"[0-9a-f]{40}") for ref in refs.values()),
        "Source references must be immutable commits",
    )
    require(
        data["dirty_repositories"] == [], "Dirty source repositories cannot enter the replay bridge"
    )
    require(matches(data["image_id"], r"sha256:[0-9a-f]{64}"), "Invalid source image ID")
    policy = data["execution_policy"]
    exact_fields(policy, POLICY_PATTERNS, "execution policy")
    require(
        all(matches(policy[name], pattern) for name, pattern in POLICY_PATTERNS.items()),
        "Invalid source execution policy",
    )


def read_regular(path, limit):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode), "Replay input must be a regular non-symlink file")
    require(info.st_size <= limit, "Replay input exceeds its size limit")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    require(
        len(data) <= limit and len(data) == info.st_size,
        "Replay input changed or exceeds its size limit",
    )
    return data


def check_pair_directory(directory, names, *, git_metadata=False):
    require(
        directory.is_dir() and not directory.is_symlink(), "Replay input must be a plain directory"
    )
    entries = {path.name: path for path in directory.iterdir()}
    if git_metadata:
        metadata = entries.pop(".git", None)
        require(
            metadata is not None
            and not metadata.is_symlink()
            and (metadata.is_file() or metadata.is_dir()),
            "Missing plain Git metadata",
        )
    require(set(entries) == set(names), "Replay input contains unexpected files or directories")
    require(
        all(path.is_file() and not path.is_symlink() for path in entries.values()),
        "Replay pair cannot contain symlinks or directories",
    )


def git(directory, *arguments):
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_") and key != "REPLAY_TRANSPORT_KEY"
    }
    environment.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_OPTIONAL_LOCKS="0",
    )
    command = ["git", "-c", "core.fsmonitor=false", "-C", str(directory), *arguments]
    return subprocess.run(
        command, check=True, capture_output=True, timeout=30, env=environment
    ).stdout


def checked_git_tree(directory, inputs):
    check_pair_directory(directory, PUBLIC_PAIR, git_metadata=True)
    require(
        git(directory, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()
        == inputs.replay_corpus_ref,
        "Git HEAD differs from pinned replay ref",
    )
    require(
        Path(git(directory, "rev-parse", "--show-toplevel").decode().strip()).resolve()
        == directory.resolve(),
        "Git worktree differs from input directory",
    )
    require(
        not git(directory, "status", "--porcelain", "--untracked-files=all"),
        "Replay checkout has working-tree changes",
    )
    records = git(directory, "ls-tree", "-r", "-t", "-z", "--full-tree", "HEAD").split(b"\0")
    entries = {}
    for record in filter(None, records):
        metadata, name = record.split(b"\t", 1)
        mode, kind, object_id = metadata.split()
        require(
            mode == b"100644" and kind == b"blob" and name.decode() in PUBLIC_PAIR,
            "Replay ref must contain only two regular blob files",
        )
        entries[name.decode()] = object_id.decode()
    require(set(entries) == PUBLIC_PAIR, "Replay ref does not contain the exact ciphertext pair")
    return entries


def check_git_bytes(files, tree):
    for name, data in files.items():
        blob = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
        require(blob == tree[name], "Replay working bytes differ from committed blobs")


def checked_public_pair(directory, inputs, limits):
    encrypted = read_regular(
        directory / "blocks.tar.gz.enc",
        limits.max_archive_bytes + HEADER_LIMIT + len(MAGIC) + 4 + AEAD_OVERHEAD,
    )
    manifest_bytes = read_regular(directory / "manifest.json", limits.max_manifest_bytes)
    require(digest(encrypted) == inputs.archive_sha256, "Encrypted archive SHA256 mismatch")
    require(digest(manifest_bytes) == inputs.manifest_sha256, "Manifest SHA256 mismatch")
    manifest = validate_manifest(json_object(manifest_bytes))
    header, message, aad = parse_envelope(encrypted, manifest_bytes, limits)
    return encrypted, manifest_bytes, manifest, header, message, aad


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def parse_envelope(encrypted, manifest_bytes, limits):
    prefix_length = len(MAGIC) + 4
    require(
        encrypted.startswith(MAGIC) and len(encrypted) >= prefix_length,
        "Invalid replay envelope magic",
    )
    size = struct.unpack(">I", encrypted[len(MAGIC) : prefix_length])[0]
    require(
        0 < size <= HEADER_LIMIT and prefix_length + size <= len(encrypted),
        "Invalid replay envelope header length",
    )
    raw_header = encrypted[prefix_length : prefix_length + size]
    header = json_object(raw_header)
    exact_fields(
        header,
        {"schema_version", "archive_sha256", "archive_bytes", "manifest_sha256"},
        "envelope header",
    )
    require(
        type(header["schema_version"]) is int and header["schema_version"] == 1,
        "Invalid envelope schema",
    )
    require(
        type(header["archive_bytes"]) is int
        and 0 < header["archive_bytes"] <= limits.max_archive_bytes,
        "Invalid inner archive size",
    )
    require(matches(header["archive_sha256"], r"[0-9a-f]{64}"), "Invalid inner archive SHA256")
    require(
        header["manifest_sha256"] == digest(manifest_bytes), "Envelope manifest digest mismatch"
    )
    require(raw_header == canonical_json(header), "Envelope header must be canonical JSON")
    end = prefix_length + size
    require(
        len(encrypted) - end == header["archive_bytes"] + AEAD_OVERHEAD,
        "Replay ciphertext length mismatch",
    )
    return header, encrypted[end:], encrypted[:end] + manifest_bytes


def credential_key(key):
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    if normalized.endswith(("file", "path")):
        return False
    return normalized in CREDENTIAL_KEYS


def check_credentials(value):
    if isinstance(value, dict):
        for key, child in value.items():
            require(
                not (credential_key(key) and child),
                "Public replay configuration contains literal credentials",
            )
            check_credentials(child)
    elif isinstance(value, list):
        for child in value:
            check_credentials(child)
    elif isinstance(value, str) and re.match(r"[A-Za-z][A-Za-z0-9+.-]*://", value):
        require(
            urlsplit(value).username is None, "Public replay configuration contains URL credentials"
        )


def member_name(member):
    name = member.name
    path = PurePosixPath(name)
    require(
        not path.is_absolute() and ".." not in path.parts and "\\" not in name,
        "Unsafe replay archive member path",
    )
    require(
        name == path.as_posix() or (member.isdir() and name == path.as_posix() + "/"),
        "Noncanonical replay archive member path",
    )
    require(
        not member.linkname and member.sparse is None,
        "Replay archive links or sparse members are forbidden",
    )
    require(
        set(member.pax_headers) <= {"mtime", "atime", "ctime", "path"},
        "Unexpected replay archive metadata",
    )
    return path.as_posix()


def allowed_file(name):
    if name in PUBLIC_CONFIG or PATCH_FILE.fullmatch(name):
        return True
    parts = PurePosixPath(name).parts
    return (
        len(parts) == 4
        and parts[:2] == ("home", "data")
        and parts[2] in DB_NAMES
        and DB_FILE.fullmatch(parts[3]) is not None
    )


def checked_members(archive, limits):
    seen = {}
    total = 0
    for member in archive:
        name = member_name(member)
        require(name not in seen, "Duplicate replay archive member")
        require(member.isdir() or member.isfile(), "Unsupported replay archive member type")
        allowed = name in DIRECTORIES and member.size == 0 if member.isdir() else allowed_file(name)
        require(allowed, "Replay archive contains a forbidden member")
        total += 512 + ((member.size + 511) // 512) * 512
        require(
            total <= limits.max_archive_bytes * 64 and len(seen) < 100_000,
            "Replay archive exceeds expanded limits",
        )
        seen[name] = member
    require(REQUIRED_CONFIG <= set(seen), "Replay archive lacks required public configuration")
    for name in ("blockstore.db", "state.db", "evidence.db"):
        require(
            f"home/data/{name}/CURRENT" in seen, "Replay archive lacks a required native database"
        )
    return seen


def check_tar_tail(archive, limit):
    require(archive.offset <= limit, "Replay archive exceeds expanded limits")
    archive.fileobj.seek(archive.offset)
    total = archive.offset
    while chunk := archive.fileobj.read(65536):
        total += len(chunk)
        require(
            total <= limit and not any(chunk),
            "Replay archive has trailing payload or excessive padding",
        )


def parse_public_config(archive, member, limit):
    require(
        member.isfile() and member.size <= limit, "Public replay configuration exceeds its limit"
    )
    with archive.extractfile(member) as stream:
        data = stream.read(limit + 1)
    require(len(data) <= limit, "Public replay configuration exceeds its limit")
    value = tomllib.loads(data.decode()) if member.name.endswith(".toml") else json_object(data)
    check_credentials(value)
    return value


def config_table(config, name):
    table = config.get(name, {})
    require(isinstance(table, dict), "Public replay configuration table is malformed")
    return table


def validate_public_config(configs, manifest):
    comet, runtime = configs["home/config/config.toml"], configs["home/config/xian.toml"]
    require(
        configs["home/config/genesis.json"].get("chain_id") == manifest["chain_id"],
        "Replay genesis chain ID mismatch",
    )
    require(
        config_table(comet, "statesync").get("enable") is False,
        "Replay P2P state sync must be disabled",
    )
    p2p = config_table(comet, "p2p")
    require(
        p2p.get("persistent_peers") == "" and p2p.get("seeds") == "" and p2p.get("pex") is False,
        "Replay P2P discovery must be disabled",
    )
    require(
        config_table(p2p, "libp2p").get("enabled", False) is False, "Replay libp2p must be disabled"
    )
    require(runtime.get("bds_enabled") is False, "Replay BDS must be disabled")


class MetadataReader:
    """Reject auxiliary-header allocations before delegating to gzip.

    Seeks skip native DB bodies through gzip's bounded decompression loop. Public
    config reads are enabled only after the complete header/tail validation.
    """

    def __init__(self, stream, payload_limit):
        self.stream = stream
        self.payload_limit = payload_limit
        self.metadata_mode = True
        self.metadata_bytes = 0

    def read(self, size=-1):
        limit = 65536 if self.metadata_mode else self.payload_limit + 1
        require(0 <= size <= limit, "Replay tar metadata/payload read exceeds its bound")
        data = self.stream.read(size)
        if self.metadata_mode:
            self.metadata_bytes += len(data)
            require(
                self.metadata_bytes <= max(self.payload_limit, 65536),
                "Replay tar metadata exceeds its budget",
            )
        return data

    def seek(self, offset, whence=0):
        return self.stream.seek(offset, whence)

    def tell(self):
        return self.stream.tell()


def validate_archive(data, manifest, limits):
    with gzip.GzipFile(fileobj=io.BytesIO(data), mode="rb") as decompressed:
        reader = MetadataReader(decompressed, limits.max_manifest_bytes)
        with tarfile.open(fileobj=reader, mode="r:") as archive:
            members = checked_members(archive, limits)
            check_tar_tail(archive, limits.max_archive_bytes * 64)
            reader.metadata_mode = False
            configs = {}
            for name, member in members.items():
                if name in PUBLIC_CONFIG or PATCH_FILE.fullmatch(name):
                    configs[name] = parse_public_config(archive, member, limits.max_manifest_bytes)
            validate_public_config(configs, manifest)
            return len(members)


def aead_class():
    try:
        from nacl.secret import Aead
    except ImportError as error:
        raise ValueError("PyNaCl1.6.2 is required only for encryption/decryption") from error
    return Aead


def transport_key(environment):
    value = environment.get("REPLAY_TRANSPORT_KEY", "")
    require(
        isinstance(value, str) and 0 < len(value) <= 128, "Missing or invalid replay transport key"
    )
    try:
        key = base64.b64decode(value.strip(), validate=True)
    except (ValueError, binascii.Error) as error:
        raise ValueError("Invalid replay transport key encoding") from error
    require(
        len(key) == 32 and base64.b64encode(key).decode() == value.strip(),
        "Invalid replay transport key",
    )
    return key


def open_payload(message, aad, key):
    aead = aead_class()
    from nacl.exceptions import CryptoError

    try:
        return aead(key).decrypt(message, aad)
    except CryptoError as error:
        raise ValueError("Replay transport authentication failed") from error


def outside(path, directory):
    require(
        not path.resolve().is_relative_to(directory.resolve()),
        "Private key/proof must be outside the artifact directory",
    )


def fresh_path(path):
    require(not path.exists() and not path.is_symlink(), "Replay output must not already exist")


def write_private(path, data):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def publish_files(output, files, *, proof_path=None, proof=None):
    fresh_path(output)
    if proof_path is not None:
        outside(proof_path, output)
        fresh_path(proof_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir(mode=0o700)
    try:
        for name, data in files.items():
            write_private(output / name, data)
            require(
                digest((output / name).read_bytes()) == digest(data), "Copied replay bytes changed"
            )
        if proof_path is not None:
            proof_path.parent.mkdir(parents=True, exist_ok=True)
            write_private(proof_path, canonical_json(proof) + b"\n")
    except BaseException:
        shutil.rmtree(output)
        raise


def public_proof(inputs, manifest):
    return {
        "mode": "ref",
        "replay_corpus_ref": inputs.replay_corpus_ref,
        "archive_sha256": inputs.archive_sha256,
        "manifest_sha256": inputs.manifest_sha256,
        "repositories": manifest["repositories"],
        "height": manifest["height"],
        "app_hash": manifest["app_hash"],
        "chain_id": manifest["chain_id"],
    }


def import_pair(directory, output, proof_path, environment, limits=Limits()):
    inputs = ref_inputs(environment)
    tree = checked_git_tree(directory, inputs)
    encrypted, raw, manifest, *_ = checked_public_pair(directory, inputs, limits)
    files = {"blocks.tar.gz.enc": encrypted, "manifest.json": raw}
    check_git_bytes(files, tree)
    proof = public_proof(inputs, manifest) | {"ciphertext_only": True, "decryption_required": True}
    publish_files(output, files, proof_path=proof_path, proof=proof)
    return proof


def encrypt_pair(directory, output, key_file, limits=Limits()):
    check_pair_directory(directory, PLAIN_PAIR)
    archive = read_regular(directory / "blocks.tar.gz", limits.max_archive_bytes)
    raw = read_regular(directory / "manifest.json", limits.max_manifest_bytes)
    manifest = validate_manifest(json_object(raw))
    validate_archive(archive, manifest, limits)
    outside(key_file, output)
    fresh_path(output)
    fresh_path(key_file)
    key = os.urandom(32)
    header = canonical_json(
        {
            "schema_version": 1,
            "archive_sha256": digest(archive),
            "archive_bytes": len(archive),
            "manifest_sha256": digest(raw),
        }
    )
    prefix = MAGIC + struct.pack(">I", len(header)) + header
    encrypted = prefix + bytes(aead_class()(key).encrypt(archive, prefix + raw))
    key_file.parent.mkdir(parents=True, exist_ok=True)
    write_private(key_file, base64.b64encode(key) + b"\n")
    try:
        publish_files(output, {"blocks.tar.gz.enc": encrypted, "manifest.json": raw})
    except BaseException:
        key_file.unlink()
        raise
    return {
        "encrypted_transport": True,
        "archive_sha256": digest(encrypted),
        "manifest_sha256": digest(raw),
        "archive_bytes": len(archive),
        "height": manifest["height"],
    }


def decrypt_pair(directory, output, proof_path, environment, limits=Limits()):
    inputs = ref_inputs(environment)
    check_pair_directory(directory, PUBLIC_PAIR)
    encrypted, raw, manifest, header, message, aad = checked_public_pair(directory, inputs, limits)
    archive = open_payload(message, aad, transport_key(environment))
    require(
        len(archive) == header["archive_bytes"] and digest(archive) == header["archive_sha256"],
        "Authenticated inner archive digest/length mismatch",
    )
    members = validate_archive(archive, manifest, limits)
    proof = public_proof(inputs, manifest) | {
        "authenticated": True,
        "plaintext_archive_sha256": header["archive_sha256"],
        "archive_bytes": len(archive),
        "archive_members": members,
    }
    publish_files(
        output, {"blocks.tar.gz": archive, "manifest.json": raw}, proof_path=proof_path, proof=proof
    )
    return proof


def positive_integer(value):
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Expected a positive byte limit") from error
    if number <= 0:
        raise argparse.ArgumentTypeError("Expected a positive byte limit")
    return number


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    commands = cli.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs")
    for name in ("encrypt", "import", "decrypt"):
        sub = commands.add_parser(name)
        sub.add_argument("--input", type=Path, required=True)
        sub.add_argument("--output", type=Path, required=True)
        sub.add_argument("--max-archive-bytes", type=positive_integer, default=256 * 1024 * 1024)
        sub.add_argument("--max-manifest-bytes", type=positive_integer, default=16 * 1024 * 1024)
        if name == "encrypt":
            sub.add_argument("--key-file", type=Path, required=True)
        else:
            sub.add_argument("--proof-output", type=Path, required=True)
    return cli


def main(arguments=None):
    args = parser().parse_args(arguments)
    try:
        if args.command == "validate-inputs":
            result = asdict(parse_inputs(os.environ))
        else:
            limits = Limits(args.max_archive_bytes, args.max_manifest_bytes)
            if args.command == "encrypt":
                result = encrypt_pair(args.input, args.output, args.key_file, limits)
            elif args.command == "import":
                result = import_pair(args.input, args.output, args.proof_output, os.environ, limits)
            else:
                result = decrypt_pair(
                    args.input, args.output, args.proof_output, os.environ, limits
                )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ValueError, OSError, subprocess.CalledProcessError, tarfile.TarError) as error:
        print(f"Replay bridge rejected input: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
