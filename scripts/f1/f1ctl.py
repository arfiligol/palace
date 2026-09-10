#!/usr/bin/env python3
"""Local cache and receipt operations for the Forerunner 1 install scripts."""

from __future__ import print_function

import argparse
import datetime
import hashlib
import json
import os
import posixpath
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile


def fail(message):
    raise RuntimeError(message)


def run(command, cwd=None, stdout=None):
    result = subprocess.Popen(
        command, cwd=cwd, stdout=stdout or subprocess.PIPE, stderr=subprocess.PIPE
    )
    output, error = result.communicate()
    if result.returncode != 0:
        fail("command failed ({}): {}\n{}".format(
            result.returncode, " ".join(command), error.decode("utf-8", "replace").strip()
        ))
    if stdout is not None:
        return ""
    return output.decode("utf-8", "replace").strip()


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path, value):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".receipt-", dir=parent or ".")
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o444)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path):
    with open(path, "r") as stream:
        return json.load(stream)


def receipt_field(receipt, field):
    value = receipt
    for part in field.split("."):
        if not isinstance(value, dict) or part not in value or value[part] is None:
            fail("prepared receipt field is unavailable: {}".format(field))
        value = value[part]
    return value


def utc_now():
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def normalized_archive_path(name):
    if not name or "\\" in name or name.startswith("/"):
        fail("unsafe archive path: {!r}".format(name))
    normalized = posixpath.normpath(name)
    if normalized in (".", "..") or normalized.startswith("../"):
        fail("unsafe archive path: {!r}".format(name))
    first = normalized.split("/", 1)[0]
    if ":" in first:
        fail("unsafe archive drive path: {!r}".format(name))
    return normalized


def validate_link(member_name, target, hardlink=False):
    if not target or "\\" in target or target.startswith("/"):
        fail("absolute archive link target: {!r}".format(target))
    if ":" in target.split("/", 1)[0]:
        fail("unsafe archive link drive target: {!r}".format(target))
    base = "" if hardlink else posixpath.dirname(member_name)
    resolved = posixpath.normpath(posixpath.join(base, target))
    if resolved == ".." or resolved.startswith("../") or resolved.startswith("/"):
        fail("archive link escapes extraction root: {} -> {}".format(member_name, target))


def validate_archive(path):
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path, "r") as archive:
            for member in archive.infolist():
                name = normalized_archive_path(member.filename)
                kind = stat.S_IFMT(member.external_attr >> 16)
                if kind == stat.S_IFLNK:
                    target = archive.read(member).decode("utf-8")
                    validate_link(name, target)
                elif kind not in (0, stat.S_IFREG, stat.S_IFDIR):
                    fail("unsupported special file in zip: {}".format(name))
        return "zip"
    try:
        with tarfile.open(path, "r:*") as archive:
            for member in archive.getmembers():
                name = normalized_archive_path(member.name)
                if member.issym():
                    validate_link(name, member.linkname)
                elif member.islnk():
                    validate_link(name, member.linkname, hardlink=True)
                elif not (member.isfile() or member.isdir()):
                    fail("unsupported special file in tar: {}".format(name))
        return "tar"
    except tarfile.TarError as error:
        fail("unsupported or corrupt archive {}: {}".format(path, error))


def extract_archive(path, destination):
    archive_kind = validate_archive(path)
    parent = os.path.dirname(os.path.abspath(destination))
    os.makedirs(parent, exist_ok=True)
    temporary = tempfile.mkdtemp(prefix=".extract-", dir=parent)
    try:
        if archive_kind == "zip":
            with zipfile.ZipFile(path, "r") as archive:
                archive.extractall(temporary)
        else:
            with tarfile.open(path, "r:*") as archive:
                archive.extractall(temporary)
        if os.path.lexists(destination):
            fail("refusing to replace existing extraction: {}".format(destination))
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary and os.path.exists(temporary):
            shutil.rmtree(temporary)


def tree_digest(root):
    digest = hashlib.sha256()
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        directories.sort()
        files.sort()
        entries = directories + files
        for entry in entries:
            path = os.path.join(current, entry)
            relative = os.path.relpath(path, root).replace(os.sep, "/")
            metadata = os.lstat(path)
            mode = stat.S_IMODE(metadata.st_mode)
            if stat.S_ISLNK(metadata.st_mode):
                record = "L\0{}\0{:o}\0{}\0".format(relative, mode, os.readlink(path))
                digest.update(record.encode("utf-8"))
            elif stat.S_ISDIR(metadata.st_mode):
                digest.update("D\0{}\0{:o}\0".format(relative, mode).encode("utf-8"))
            elif stat.S_ISREG(metadata.st_mode):
                digest.update("F\0{}\0{:o}\0{}\0".format(
                    relative, mode, metadata.st_size
                ).encode("utf-8"))
                with open(path, "rb") as stream:
                    while True:
                        block = stream.read(1024 * 1024)
                        if not block:
                            break
                        digest.update(block)
            else:
                fail("unsupported file in tree: {}".format(path))
    return digest.hexdigest()


def download(url, destination, expected):
    if os.path.exists(destination):
        actual = sha256(destination)
        if actual != expected:
            fail("cached archive checksum mismatch; refusing to replace {}".format(destination))
        print("reuse archive {}".format(os.path.basename(destination)))
        return
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    temporary = destination + ".part"
    if os.path.lexists(temporary):
        os.unlink(temporary)
    print("download {}".format(url))
    try:
        run([
            "curl", "--fail", "--location", "--retry", "3",
            "--connect-timeout", "30", "--output", temporary, url
        ])
        actual = sha256(temporary)
        if actual != expected:
            fail("download checksum mismatch for {}: expected {}, got {}".format(
                url, expected, actual
            ))
        os.chmod(temporary, 0o444)
        os.replace(temporary, destination)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def verify_git_mirror(path, item):
    if run(["git", "--git-dir", path, "rev-parse", "--is-bare-repository"]) != "true":
        fail("not a bare Git mirror: {}".format(path))
    resolved = run([
        "git", "--git-dir", path, "rev-parse", "refs/heads/palace-f1-lock^{commit}"
    ])
    if resolved != item["commit"]:
        fail("Git lock mismatch for {}: expected {}, got {}".format(
            item["name"], item["commit"], resolved
        ))
    run(["git", "--git-dir", path, "cat-file", "-e", item["commit"] + "^{commit}"])


def ensure_git_mirror(cache_dir, item):
    destination = os.path.join(cache_dir, "git", item["name"] + ".git")
    if os.path.exists(destination):
        verify_git_mirror(destination, item)
        print("reuse Git mirror {}".format(item["name"]))
        return destination
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    temporary = destination + ".part"
    if os.path.lexists(temporary):
        shutil.rmtree(temporary)
    print("cache locked Git commit {}".format(item["name"]))
    try:
        run(["git", "init", "--bare", temporary])
        run([
            "git", "--git-dir", temporary, "fetch", "--depth=1",
            item["url"], item["commit"]
        ])
        run([
            "git", "--git-dir", temporary, "update-ref",
            "refs/heads/palace-f1-lock", "FETCH_HEAD"
        ])
        verify_git_mirror(temporary, item)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            shutil.rmtree(temporary)
    return destination


def script_identity(repo):
    script_root = os.path.join(repo, "scripts", "f1")
    tracked_output = run(["git", "ls-files", "-z", "--", "scripts/f1"], cwd=repo)
    tracked = sorted([value for value in tracked_output.split("\0") if value])
    disk = []
    for current, directories, files in os.walk(script_root):
        directories[:] = sorted([entry for entry in directories if entry != "__pycache__"])
        for filename in sorted(files):
            if filename.endswith(".pyc"):
                continue
            disk.append(os.path.relpath(os.path.join(current, filename), repo).replace(os.sep, "/"))
    if not tracked or tracked != sorted(disk):
        fail("scripts/f1 must be fully tracked with no extra files before preparation")
    if subprocess.call(["git", "diff", "--quiet", "--", "scripts/f1"], cwd=repo) != 0:
        fail("scripts/f1 has unstaged changes")
    if subprocess.call(["git", "diff", "--cached", "--quiet", "--", "scripts/f1"], cwd=repo) != 0:
        fail("scripts/f1 has staged changes")
    files = []
    for relative in tracked:
        path = os.path.join(repo, relative)
        if os.path.isfile(path) and not os.path.islink(path):
            files.append({"path": relative, "sha256": sha256(path)})
        elif os.path.islink(path):
            files.append({"path": relative, "symlink": os.readlink(path)})
        else:
            fail("tracked F1 script is missing: {}".format(relative))
    return {"git_commit": run(["git", "rev-parse", "HEAD"], cwd=repo), "files": files}


def verify_script_identity(repo, expected):
    actual = script_identity(repo)
    if actual != expected:
        fail("F1 installer scripts do not match the prepared receipt")


def prepare_source(repo, root, source_lock):
    commit = source_lock["commit"]
    actual_tree = run(["git", "rev-parse", commit + "^{tree}"], cwd=repo)
    if actual_tree != source_lock["tree"]:
        fail("Palace source tree mismatch: expected {}, got {}".format(
            source_lock["tree"], actual_tree
        ))
    cache_archive = os.path.join(root, "cache", "archives", source_lock["archive"])
    if not os.path.exists(cache_archive):
        os.makedirs(os.path.dirname(cache_archive), exist_ok=True)
        temporary = cache_archive + ".part"
        with open(temporary, "wb") as stream:
            run([
                "git", "archive", "--format=tar",
                "--prefix={}/".format(source_lock["archive_prefix"]), commit
            ], cwd=repo, stdout=stream)
        actual = sha256(temporary)
        if actual != source_lock["sha256"]:
            os.unlink(temporary)
            fail("Palace git archive checksum mismatch: expected {}, got {}".format(
                source_lock["sha256"], actual
            ))
        os.chmod(temporary, 0o444)
        os.replace(temporary, cache_archive)
    elif sha256(cache_archive) != source_lock["sha256"]:
        fail("cached Palace source archive checksum mismatch")
    validate_archive(cache_archive)

    source_parent = os.path.join(root, "source")
    source_dir = os.path.join(source_parent, source_lock["archive_prefix"])
    receipt_path = source_dir + ".receipt.json"
    if os.path.isdir(source_dir):
        receipt = read_json(receipt_path)
        expected = {
            "archive_sha256": source_lock["sha256"],
            "commit": commit,
            "source_dir": source_dir,
            "tree": source_lock["tree"]
        }
        for key, value in expected.items():
            if receipt.get(key) != value:
                fail("existing Palace source receipt has a different {}".format(key))
        if receipt.get("tree_digest") != tree_digest(source_dir):
            fail("prepared Palace source tree was modified: {}".format(source_dir))
    else:
        container = source_dir + ".extract"
        extract_archive(cache_archive, container)
        extracted = os.path.join(container, source_lock["archive_prefix"])
        if sorted(os.listdir(container)) != [source_lock["archive_prefix"]] or not os.path.isdir(extracted):
            shutil.rmtree(container)
            fail("Palace source archive root mismatch")
        os.replace(extracted, source_dir)
        os.rmdir(container)
        for current, directories, files in os.walk(source_dir):
            for name in directories:
                path = os.path.join(current, name)
                if not os.path.islink(path):
                    os.chmod(path, stat.S_IMODE(os.lstat(path).st_mode) & ~0o222)
            for name in files:
                path = os.path.join(current, name)
                if not os.path.islink(path):
                    os.chmod(path, stat.S_IMODE(os.lstat(path).st_mode) & ~0o222)
        os.chmod(source_dir, stat.S_IMODE(os.lstat(source_dir).st_mode) & ~0o222)
        digest = tree_digest(source_dir)
        receipt = {
            "archive_sha256": source_lock["sha256"],
            "commit": commit,
            "source_dir": source_dir,
            "tree": source_lock["tree"],
            "tree_digest": digest
        }
        atomic_json(receipt_path, receipt)
    return read_json(receipt_path)


def find_unique(root, filename):
    matches = []
    for current, directories, files in os.walk(root, followlinks=False):
        directories.sort()
        if filename in files:
            matches.append(os.path.join(current, filename))
    if len(matches) != 1:
        fail("expected exactly one {}, found {}".format(filename, len(matches)))
    return matches[0]


def verify_case_dir(case_dir, lock):
    receipt_path = case_dir + ".receipt.json"
    receipt = read_json(receipt_path)
    archive = receipt["archive_path"]
    if os.path.getsize(archive) != lock["archive_size"] or sha256(archive) != lock["archive_sha256"]:
        fail("cached public case archive identity mismatch")
    if receipt.get("tree_digest") != tree_digest(case_dir):
        fail("imported public case tree was modified")
    config = os.path.join(case_dir, receipt["config_relative_path"])
    mesh = os.path.join(case_dir, receipt["mesh_relative_path"])
    if sha256(config) != lock["config_sha256"]:
        fail("public case config checksum mismatch")
    if sha256(mesh) != lock["mesh_sha256"]:
        fail("public case mesh checksum mismatch")
    return receipt


def import_case(root, archive_path, lock):
    archive_path = os.path.abspath(archive_path)
    if os.path.getsize(archive_path) != lock["archive_size"]:
        fail("public case archive size mismatch")
    if sha256(archive_path) != lock["archive_sha256"]:
        fail("public case archive checksum mismatch")
    validate_archive(archive_path)
    cached = os.path.join(root, "cache", "cases", lock["archive_sha256"] + ".archive")
    os.makedirs(os.path.dirname(cached), exist_ok=True)
    if os.path.exists(cached):
        if sha256(cached) != lock["archive_sha256"]:
            fail("cached public case archive checksum mismatch")
    else:
        temporary = cached + ".part"
        shutil.copyfile(archive_path, temporary)
        os.chmod(temporary, 0o444)
        os.replace(temporary, cached)

    case_dir = os.path.join(root, "source", "cases", lock["name"])
    if os.path.isdir(case_dir):
        return verify_case_dir(case_dir, lock)
    os.makedirs(os.path.dirname(case_dir), exist_ok=True)
    extract_archive(cached, case_dir)
    config = find_unique(case_dir, lock["config_name"])
    mesh = find_unique(case_dir, lock["mesh_name"])
    for current, directories, files in os.walk(case_dir):
        for name in directories:
            path = os.path.join(current, name)
            if not os.path.islink(path):
                os.chmod(path, stat.S_IMODE(os.lstat(path).st_mode) & ~0o222)
        for name in files:
            path = os.path.join(current, name)
            if not os.path.islink(path):
                os.chmod(path, stat.S_IMODE(os.lstat(path).st_mode) & ~0o222)
    os.chmod(case_dir, stat.S_IMODE(os.lstat(case_dir).st_mode) & ~0o222)
    receipt = {
        "archive_path": cached,
        "archive_sha256": lock["archive_sha256"],
        "archive_size": lock["archive_size"],
        "case_dir": case_dir,
        "config_relative_path": os.path.relpath(config, case_dir),
        "config_sha256": lock["config_sha256"],
        "mesh_relative_path": os.path.relpath(mesh, case_dir),
        "mesh_sha256": lock["mesh_sha256"],
        "tree_digest": tree_digest(case_dir)
    }
    atomic_json(case_dir + ".receipt.json", receipt)
    return receipt


def verify_prepared(repo, root, lock):
    receipt_path = os.path.join(root, "prepared.json")
    receipt = read_json(receipt_path)
    if receipt.get("root") != root:
        fail("prepared receipt belongs to a different installation root")
    if receipt.get("lock_sha256") != sha256(os.path.join(repo, "scripts", "f1", "sources.lock.json")):
        fail("source lock does not match prepared receipt")
    verify_script_identity(repo, receipt["installer"])
    source_receipt = receipt["palace_source"]
    if source_receipt["tree_digest"] != tree_digest(source_receipt["source_dir"]):
        fail("prepared Palace source tree was modified")
    for item in lock["archives"]:
        path = os.path.join(root, "cache", "archives", item["filename"])
        if sha256(path) != item["sha256"]:
            fail("archive cache mismatch: {}".format(item["name"]))
    for item in lock["git_mirrors"]:
        path = os.path.join(root, "cache", "git", item["name"] + ".git")
        verify_git_mirror(path, item)
    cmake_dir = receipt["cmake_directory"]
    if tree_digest(cmake_dir) != receipt["cmake_tree_digest"]:
        fail("prepared CMake bootstrap tree was modified")
    if receipt.get("public_case"):
        verify_case_dir(receipt["public_case"]["case_dir"], lock["public_case"])
    return receipt


def command_prepare(arguments):
    repo = os.path.abspath(arguments.repo)
    root = os.path.abspath(os.path.expanduser(arguments.root))
    lock_path = os.path.join(repo, "scripts", "f1", "sources.lock.json")
    lock = read_json(lock_path)
    installer = script_identity(repo)
    for name in ("source", "cache", "build", "versions", "logs", "runs"):
        os.makedirs(os.path.join(root, name), exist_ok=True)
    archives = []
    for item in lock["archives"]:
        path = os.path.join(root, "cache", "archives", item["filename"])
        download(item["url"], path, item["sha256"])
        validate_archive(path)
        archives.append({"name": item["name"], "path": path, "sha256": item["sha256"]})
    mirrors = []
    for item in lock["git_mirrors"]:
        path = ensure_git_mirror(os.path.join(root, "cache"), item)
        mirrors.append({"name": item["name"], "path": path, "commit": item["commit"]})
    source_receipt = prepare_source(repo, root, lock["palace_source"])

    cmake_lock = next(item for item in lock["archives"] if item["name"] == "cmake")
    cmake_dir = os.path.join(root, "cache", "tools", cmake_lock["archive_prefix"])
    cmake_archive = os.path.join(root, "cache", "archives", cmake_lock["filename"])
    cmake_receipt_path = cmake_dir + ".receipt.json"
    if not os.path.isdir(cmake_dir):
        tools_parent = os.path.join(root, "cache", "tools")
        os.makedirs(tools_parent, exist_ok=True)
        container = cmake_dir + ".extract"
        extract_archive(cmake_archive, container)
        extracted = os.path.join(container, cmake_lock["archive_prefix"])
        if sorted(os.listdir(container)) != [cmake_lock["archive_prefix"]] or not os.path.isdir(extracted):
            shutil.rmtree(container)
            fail("CMake archive root mismatch")
        os.replace(extracted, cmake_dir)
        os.rmdir(container)
        cmake_receipt = {
            "archive_sha256": cmake_lock["sha256"],
            "directory": cmake_dir,
            "tree_digest": tree_digest(cmake_dir)
        }
        atomic_json(cmake_receipt_path, cmake_receipt)
    else:
        cmake_receipt = read_json(cmake_receipt_path)
        if cmake_receipt.get("archive_sha256") != cmake_lock["sha256"]:
            fail("existing CMake bootstrap came from a different archive")
        if cmake_receipt.get("directory") != cmake_dir:
            fail("existing CMake bootstrap receipt has a different directory")
        if cmake_receipt.get("tree_digest") != tree_digest(cmake_dir):
            fail("prepared CMake bootstrap tree was modified")
    cmake_executable = os.path.join(cmake_dir, "bin", "cmake")
    if not os.path.isfile(cmake_executable):
        fail("CMake bootstrap executable missing after extraction")

    case_receipt = None
    existing_case = os.path.join(root, "source", "cases", lock["public_case"]["name"])
    if arguments.case_archive:
        case_receipt = import_case(root, arguments.case_archive, lock["public_case"])
    elif os.path.isdir(existing_case):
        case_receipt = verify_case_dir(existing_case, lock["public_case"])

    receipt = {
        "schema_version": 1,
        "prepared_at": utc_now(),
        "root": root,
        "lock_path": lock_path,
        "lock_sha256": sha256(lock_path),
        "installer": installer,
        "palace_source": source_receipt,
        "archives": archives,
        "git_mirrors": mirrors,
        "cmake_executable": cmake_executable,
        "cmake_directory": cmake_dir,
        "cmake_tree_digest": cmake_receipt["tree_digest"],
        "public_case": case_receipt
    }
    atomic_json(os.path.join(root, "prepared.json"), receipt)
    print("prepared offline cache: {}".format(root))
    if case_receipt:
        print("verified public case: {}".format(case_receipt["case_dir"]))
    else:
        print("public case pending custodian transfer; rerun with --case-archive PATH")


def command_verify(arguments):
    repo = os.path.abspath(arguments.repo)
    root = os.path.abspath(os.path.expanduser(arguments.root))
    lock = read_json(os.path.join(repo, "scripts", "f1", "sources.lock.json"))
    receipt = verify_prepared(repo, root, lock)
    if arguments.field:
        print(receipt_field(receipt, arguments.field))
    else:
        print("prepared cache verified")


def command_extract(arguments):
    if sha256(arguments.archive) != arguments.sha256:
        fail("archive checksum mismatch: {}".format(arguments.archive))
    receipt_path = arguments.destination + ".receipt.json"
    if os.path.isdir(arguments.destination):
        receipt = read_json(receipt_path)
        if receipt.get("archive_sha256") != arguments.sha256:
            fail("existing extraction came from a different archive: {}".format(
                arguments.destination
            ))
        if receipt.get("expected_root") != arguments.expected_root:
            fail("existing extraction has a different root contract: {}".format(
                arguments.destination
            ))
        if receipt.get("tree_digest") != tree_digest(arguments.destination):
            fail("existing extraction was modified: {}".format(arguments.destination))
        print(arguments.destination)
        return
    extract_archive(arguments.archive, arguments.destination)
    entries = sorted(os.listdir(arguments.destination))
    if arguments.expected_root:
        if entries != [arguments.expected_root]:
            fail("archive root mismatch: expected {}, got {}".format(
                arguments.expected_root, ", ".join(entries)
            ))
    receipt = {
        "archive": os.path.abspath(arguments.archive),
        "archive_sha256": arguments.sha256,
        "destination": os.path.abspath(arguments.destination),
        "expected_root": arguments.expected_root,
        "tree_digest": tree_digest(arguments.destination)
    }
    atomic_json(receipt_path, receipt)
    print(arguments.destination)


def command_case(arguments):
    lock = read_json(arguments.lock)["public_case"]
    receipt = verify_case_dir(os.path.abspath(arguments.case_dir), lock)
    if arguments.field:
        print(receipt[arguments.field])
    else:
        print("public case verified")


def command_claim_prefix(arguments):
    prefix = os.path.abspath(arguments.prefix)
    owner_path = os.path.abspath(arguments.owner_receipt)
    owner = {
        "schema_version": 1,
        "status": "CLAIMED",
        "prefix": prefix,
        "plan_sha256": sha256(arguments.plan),
        "installer_commit": arguments.installer_commit,
        "source_lock_sha256": arguments.source_lock_sha256
    }
    if os.path.lexists(prefix):
        if os.path.islink(prefix) or not os.path.isdir(prefix):
            fail("installation prefix is not a real directory: {}".format(prefix))
        if os.path.exists(owner_path):
            if read_json(owner_path) != owner:
                fail("installation prefix ownership differs from the current build plan")
        elif os.listdir(prefix):
            fail("refusing unowned nonempty installation prefix: {}".format(prefix))
        else:
            atomic_json(owner_path, owner)
    else:
        if os.path.exists(owner_path):
            fail("prefix ownership receipt exists but the installation prefix is missing")
        os.makedirs(prefix)
        atomic_json(owner_path, owner)
    print("installation prefix claimed: {}".format(prefix))


def command_write_stage(arguments):
    prefix = os.path.abspath(arguments.prefix)
    files = install_files(prefix)
    if not files:
        fail("cannot complete an empty install stage: {}".format(arguments.label))
    receipt = {
        "schema_version": 1,
        "status": "STAGE_COMPLETE",
        "label": arguments.label,
        "prefix": prefix,
        "files": files
    }
    atomic_json(arguments.receipt, receipt)
    print("install stage recorded: {}".format(arguments.label))


def command_verify_stage(arguments):
    prefix = os.path.abspath(arguments.prefix)
    receipt = read_json(arguments.receipt)
    if (receipt.get("status") != "STAGE_COMPLETE" or
            receipt.get("label") != arguments.label or receipt.get("prefix") != prefix):
        fail("install stage receipt does not match: {}".format(arguments.label))
    current = dict((item["path"], item) for item in install_files(prefix))
    recorded = receipt.get("files")
    if not isinstance(recorded, list) or not recorded:
        fail("install stage receipt has no files: {}".format(arguments.label))
    for item in recorded:
        if current.get(item.get("path")) != item:
            fail("install stage output changed: {} ({})".format(
                arguments.label, item.get("path", "unknown")
            ))
    print("install stage verified: {}".format(arguments.label))


def command_install(arguments):
    prefix = os.path.abspath(arguments.prefix)
    receipt = read_json(os.path.join(prefix, "BUILD-RECEIPT.json"))
    if receipt.get("status") != "COMPLETE" or receipt.get("prefix") != prefix:
        fail("build receipt does not describe this completed installation")
    if arguments.prepared:
        prepared = read_json(arguments.prepared)
        if receipt.get("installer") != prepared.get("installer"):
            fail("installed Palace was built by a different installer revision")
        if receipt.get("source") != prepared.get("palace_source"):
            fail("installed Palace was built from a different source snapshot")
        if receipt.get("source_lock_sha256") != prepared.get("lock_sha256"):
            fail("installed Palace was built with a different source lock")
    executable = receipt["executable"]["path"]
    if not os.path.isfile(executable):
        fail("installed Palace executable missing: {}".format(executable))
    if sha256(executable) != receipt["executable"]["sha256"]:
        fail("installed Palace executable checksum mismatch")
    current = install_files(prefix)
    if current != receipt["install_files"]:
        fail("installed file manifest differs from build receipt")
    paths = [prefix]
    for current_dir, directories, files in os.walk(prefix, followlinks=False):
        paths.extend(os.path.join(current_dir, name) for name in directories + files)
    for path in paths:
        if not os.path.islink(path) and stat.S_IMODE(os.lstat(path).st_mode) & 0o222:
            fail("completed installation is writable: {}".format(path))
    print(executable if arguments.field == "executable" else "installation verified")


def install_files(prefix):
    excluded = set(("BUILD-RECEIPT.json", "BUILD-RECEIPT.md", "INSTALL-MANIFEST.sha256"))
    result = []
    for current, directories, files in os.walk(prefix, topdown=True, followlinks=False):
        directories.sort()
        files.sort()
        for filename in files:
            path = os.path.join(current, filename)
            relative = os.path.relpath(path, prefix).replace(os.sep, "/")
            if relative in excluded:
                continue
            if os.path.islink(path):
                result.append({"path": relative, "symlink": os.readlink(path)})
            elif os.path.isfile(path):
                result.append({"path": relative, "sha256": sha256(path), "size": os.path.getsize(path)})
    return result


def first_line(command):
    return run(command).splitlines()[0]


def command_build_receipt(arguments):
    prefix = os.path.abspath(arguments.prefix)
    binaries = []
    bin_dir = os.path.join(prefix, "bin")
    for name in sorted(os.listdir(bin_dir)):
        path = os.path.join(bin_dir, name)
        if name.startswith("palace-") and name.endswith(".bin") and os.path.isfile(path):
            binaries.append(path)
    if len(binaries) != 1:
        fail("expected one direct Palace ELF executable, found {}".format(len(binaries)))
    executable = binaries[0]
    with open(arguments.plan, "r") as stream:
        plan_text = stream.read()
    prepared = read_json(os.path.join(arguments.root, "prepared.json"))
    files = install_files(prefix)
    manifest_path = os.path.join(prefix, "INSTALL-MANIFEST.sha256")
    with open(manifest_path, "w") as stream:
        for item in files:
            if "sha256" in item:
                stream.write("{}  {}\n".format(item["sha256"], item["path"]))
            else:
                stream.write("SYMLINK {}  {}\n".format(item["symlink"], item["path"]))
    os.chmod(manifest_path, 0o444)
    receipt = {
        "schema_version": 1,
        "status": "COMPLETE",
        "completed_at": utc_now(),
        "build_id": arguments.build_id,
        "root": os.path.abspath(arguments.root),
        "prefix": prefix,
        "build_dir": os.path.abspath(arguments.build_dir),
        "source": prepared["palace_source"],
        "installer": prepared["installer"],
        "source_lock_sha256": prepared["lock_sha256"],
        "plan_sha256": sha256(arguments.plan),
        "plan": plan_text.splitlines(),
        "toolchain": {
            "architecture": run(["uname", "-m"]),
            "gcc": first_line(["gcc", "--version"]),
            "gxx": first_line(["g++", "--version"]),
            "gfortran": first_line(["gfortran", "--version"]),
            "mpicc_command": run(["mpicc", "--showme:command"]),
            "mpicxx_command": run(["mpicxx", "--showme:command"]),
            "mpifort_command": run(["mpifort", "--showme:command"]),
            "openmpi": first_line(["ompi_info", "--version"]),
            "cmake": first_line([arguments.cmake, "--version"])
        },
        "slurm": {
            "job_id": os.environ.get("SLURM_JOB_ID"),
            "partition": os.environ.get("SLURM_JOB_PARTITION"),
            "nodes": os.environ.get("SLURM_JOB_NUM_NODES"),
            "ntasks": os.environ.get("SLURM_NTASKS"),
            "cpus_per_task": os.environ.get("SLURM_CPUS_PER_TASK")
        },
        "executable": {
            "path": executable,
            "sha256": sha256(executable),
            "size": os.path.getsize(executable)
        },
        "jit_header_directory": os.path.join(prefix, "include", "palace"),
        "runtime_environment": os.path.join(prefix, "run-env.sh"),
        "runtime_contract": {
            "compiler_runtime": "GCC 11.2.0",
            "mpi_runtime": "Open MPI 4.1.6",
            "shared_dependencies": [
                "OpenBLAS 0.3.20", "zlib 1.3.2", "libCEED", "LIBXSMM"
            ],
            "openblas_threads": "1",
            "default_openmp_threads": "1"
        },
        "elf_ldd": {"path": arguments.ldd, "sha256": sha256(arguments.ldd)},
        "elf_readelf": {"path": arguments.readelf, "sha256": sha256(arguments.readelf)},
        "cmake_cache": {
            "superbuild": sha256(os.path.join(arguments.build_dir, "superbuild", "CMakeCache.txt")),
            "native": sha256(os.path.join(arguments.build_dir, "native", "CMakeCache.txt"))
        },
        "install_files": files,
        "limitations": [
            "Built on F1 only when this receipt is emitted by a successful Human-operated job.",
            "No solver case is executed by the build job.",
            "F1 module and Slurm PMIx compatibility are runtime preflight facts, not locally established."
        ]
    }
    receipt_path = os.path.join(prefix, "BUILD-RECEIPT.json")
    atomic_json(receipt_path, receipt)
    markdown = os.path.join(prefix, "BUILD-RECEIPT.md")
    with open(markdown, "w") as stream:
        stream.write("# Palace F1 build receipt\n\n")
        stream.write("- Status: `COMPLETE`\n")
        stream.write("- Build ID: `{}`\n".format(arguments.build_id))
        stream.write("- Source commit: `{}`\n".format(receipt["source"]["commit"]))
        stream.write("- Source tree: `{}`\n".format(receipt["source"]["tree"]))
        stream.write("- Executable: `{}`\n".format(executable))
        stream.write("- Executable SHA-256: `{}`\n".format(receipt["executable"]["sha256"]))
        stream.write("- Install manifest: `{}`\n".format(manifest_path))
        stream.write("- Runtime environment: `{}`\n".format(receipt["runtime_environment"]))
        stream.write("- Runtime compiler: `GCC 11.2.0`\n")
        stream.write("- Runtime MPI: `Open MPI 4.1.6`\n")
        stream.write("- Shared dependencies: `OpenBLAS 0.3.20, zlib 1.3.2, libCEED, LIBXSMM`\n")
        stream.write("\nThis receipt records a build only; it does not claim an F1 solver run.\n")
    os.chmod(markdown, 0o444)
    print(executable)


def command_run_start(arguments):
    receipt_path = os.path.join(arguments.run_dir, "RUN-RECEIPT.json")
    if os.path.exists(receipt_path):
        fail("run receipt already exists: {}".format(receipt_path))
    launcher_command = [
        "srun", "--mpi=pmix", "--cpu-bind=cores", arguments.executable,
        os.path.basename(arguments.config)
    ]
    measured_command = [
        "/usr/bin/time", "-v", "-o", os.path.abspath(arguments.resource_usage)
    ] + launcher_command
    receipt = {
        "schema_version": 1,
        "status": "PROCESS_RUNNING",
        "semantic_state": "CONVERGING",
        "scientific_acceptance": None,
        "evidence_scope": "execution process and uninterpreted outputs only",
        "started_at": utc_now(),
        "run_dir": os.path.abspath(arguments.run_dir),
        "working_directory": os.path.dirname(os.path.abspath(arguments.config)),
        "source_case_dir": os.path.abspath(arguments.case_dir),
        "source_case_archive_sha256": arguments.case_archive_sha256,
        "config": {"path": arguments.config, "sha256": sha256(arguments.config)},
        "mesh": {"path": arguments.mesh, "sha256": sha256(arguments.mesh)},
        "executable": {"path": arguments.executable, "sha256": sha256(arguments.executable)},
        "slurm": {
            "job_id": os.environ.get("SLURM_JOB_ID"),
            "restart_count": os.environ.get("SLURM_RESTART_COUNT", "0"),
            "partition": os.environ.get("SLURM_JOB_PARTITION"),
            "nodes": os.environ.get("SLURM_JOB_NUM_NODES"),
            "ntasks": arguments.ranks,
            "cpus_per_task": arguments.threads
        },
        "environment": {
            "OMP_NUM_THREADS": os.environ.get("OMP_NUM_THREADS"),
            "OPENBLAS_NUM_THREADS": os.environ.get("OPENBLAS_NUM_THREADS"),
            "OMP_PROC_BIND": os.environ.get("OMP_PROC_BIND"),
            "OMP_PLACES": os.environ.get("OMP_PLACES")
        },
        "launcher_command": launcher_command,
        "measured_command": measured_command,
        "redirection": {
            "stdout": os.path.abspath(arguments.log),
            "stderr": os.path.abspath(arguments.log),
            "mode": "stdout and stderr combined"
        },
        "timing_scope": "/usr/bin/time measurement of the local srun launcher process"
    }
    atomic_json(receipt_path, receipt)


def command_run_finish(arguments):
    path = os.path.join(arguments.run_dir, "RUN-RECEIPT.json")
    receipt = read_json(path)
    if receipt.get("status") != "PROCESS_RUNNING":
        fail("run receipt is not in PROCESS_RUNNING state")
    if receipt.get("source_case_archive_sha256") != arguments.case_archive_sha256:
        fail("run completion archive identity differs from run start")
    if (str(receipt.get("slurm", {}).get("ntasks")) != str(arguments.ranks) or
            str(receipt.get("slurm", {}).get("cpus_per_task")) != str(arguments.threads)):
        fail("run completion resources differ from run start")
    receipt["status"] = "PROCESS_COMPLETE" if arguments.exit_code == 0 else "PROCESS_FAILED"
    receipt["exit_code"] = arguments.exit_code
    receipt["completed_at"] = utc_now()
    receipt["log"] = {
        "path": os.path.abspath(arguments.log),
        "sha256": sha256(arguments.log),
        "size": os.path.getsize(arguments.log)
    }
    with open(arguments.resource_usage, "r") as stream:
        resource_usage = stream.read().splitlines()
    receipt["resource_usage"] = {
        "path": os.path.abspath(arguments.resource_usage),
        "sha256": sha256(arguments.resource_usage),
        "scope": "local srun launcher process; not aggregate per-rank resource usage",
        "text": resource_usage
    }
    selected = []
    terms = ("frequen", "eigen", "energy", "adaptive", "refin", "amr", "iteration")
    with open(arguments.log, "r", errors="replace") as stream:
        for number, line in enumerate(stream, 1):
            text_value = line.rstrip("\n")
            if any(term in text_value.lower() for term in terms):
                selected.append({"line": number, "text": text_value})
    receipt["scientific_log_lines"] = selected
    artifacts = []
    work = os.path.join(arguments.run_dir, "work")
    for item in install_files(work):
        artifacts.append(item)
    receipt["work_files"] = artifacts
    atomic_json(path, receipt)
    markdown = os.path.join(arguments.run_dir, "RUN-RECEIPT.md")
    with open(markdown, "w") as stream:
        stream.write("# Palace F1 run receipt\n\n")
        stream.write("- Status: `{}`\n".format(receipt["status"]))
        stream.write("- Exit code: `{}`\n".format(arguments.exit_code))
        stream.write("- Slurm job: `{}`\n".format(receipt["slurm"]["job_id"]))
        stream.write("- MPI ranks × threads: `{} × {}`\n".format(arguments.ranks, arguments.threads))
        stream.write("- Executable SHA-256: `{}`\n".format(receipt["executable"]["sha256"]))
        stream.write("- Input archive SHA-256: `{}`\n".format(arguments.case_archive_sha256))
        stream.write("- Semantic state: `CONVERGING`\n")
        stream.write("- Scientific acceptance: `UNSET`\n")
        stream.write("- Full solver log: `{}`\n".format(receipt["log"]["path"]))
        stream.write("- Resource usage: `{}`\n".format(receipt["resource_usage"]["path"]))
        stream.write("- Resource scope: `local srun launcher; not aggregate per-rank usage`\n")
    os.chmod(markdown, 0o444)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command")

    prepare = commands.add_parser("prepare")
    prepare.add_argument("--repo", required=True)
    prepare.add_argument("--root", required=True)
    prepare.add_argument("--case-archive")
    prepare.set_defaults(function=command_prepare)

    verify = commands.add_parser("verify-prepared")
    verify.add_argument("--repo", required=True)
    verify.add_argument("--root", required=True)
    verify.add_argument("--field")
    verify.set_defaults(function=command_verify)

    extract = commands.add_parser("extract")
    extract.add_argument("--archive", required=True)
    extract.add_argument("--destination", required=True)
    extract.add_argument("--sha256", required=True)
    extract.add_argument("--expected-root")
    extract.set_defaults(function=command_extract)

    case = commands.add_parser("verify-case")
    case.add_argument("--lock", required=True)
    case.add_argument("--case-dir", required=True)
    case.add_argument("--field")
    case.set_defaults(function=command_case)

    install = commands.add_parser("verify-install")
    install.add_argument("--prefix", required=True)
    install.add_argument("--prepared")
    install.add_argument("--field")
    install.set_defaults(function=command_install)

    claim_prefix = commands.add_parser("claim-prefix")
    claim_prefix.add_argument("--prefix", required=True)
    claim_prefix.add_argument("--owner-receipt", required=True)
    claim_prefix.add_argument("--plan", required=True)
    claim_prefix.add_argument("--installer-commit", required=True)
    claim_prefix.add_argument("--source-lock-sha256", required=True)
    claim_prefix.set_defaults(function=command_claim_prefix)

    write_stage = commands.add_parser("write-stage-receipt")
    write_stage.add_argument("--prefix", required=True)
    write_stage.add_argument("--receipt", required=True)
    write_stage.add_argument("--label", required=True)
    write_stage.set_defaults(function=command_write_stage)

    verify_stage = commands.add_parser("verify-stage-receipt")
    verify_stage.add_argument("--prefix", required=True)
    verify_stage.add_argument("--receipt", required=True)
    verify_stage.add_argument("--label", required=True)
    verify_stage.set_defaults(function=command_verify_stage)

    build_receipt = commands.add_parser("write-build-receipt")
    build_receipt.add_argument("--prefix", required=True)
    build_receipt.add_argument("--build-dir", required=True)
    build_receipt.add_argument("--root", required=True)
    build_receipt.add_argument("--build-id", required=True)
    build_receipt.add_argument("--plan", required=True)
    build_receipt.add_argument("--cmake", required=True)
    build_receipt.add_argument("--ldd", required=True)
    build_receipt.add_argument("--readelf", required=True)
    build_receipt.set_defaults(function=command_build_receipt)

    run_start = commands.add_parser("run-start")
    run_start.add_argument("--run-dir", required=True)
    run_start.add_argument("--case-dir", required=True)
    run_start.add_argument("--case-archive-sha256", required=True)
    run_start.add_argument("--config", required=True)
    run_start.add_argument("--mesh", required=True)
    run_start.add_argument("--executable", required=True)
    run_start.add_argument("--log", required=True)
    run_start.add_argument("--resource-usage", required=True)
    run_start.add_argument("--ranks", required=True)
    run_start.add_argument("--threads", required=True)
    run_start.set_defaults(function=command_run_start)

    run_finish = commands.add_parser("run-finish")
    run_finish.add_argument("--run-dir", required=True)
    run_finish.add_argument("--log", required=True)
    run_finish.add_argument("--resource-usage", required=True)
    run_finish.add_argument("--exit-code", required=True, type=int)
    run_finish.add_argument("--case-archive-sha256", required=True)
    run_finish.add_argument("--ranks", required=True)
    run_finish.add_argument("--threads", required=True)
    run_finish.set_defaults(function=command_run_finish)
    return result


def main():
    arguments = parser().parse_args()
    if not hasattr(arguments, "function"):
        parser().error("a command is required")
    try:
        arguments.function(arguments)
    except (IOError, OSError, RuntimeError, KeyError, ValueError) as error:
        print("f1ctl: {}".format(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
