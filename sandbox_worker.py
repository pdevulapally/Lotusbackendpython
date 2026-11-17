"""Linux sandbox entry point. Requires Python and Git, no cloud credentials."""

import argparse
import contextlib
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath
from uuid import UUID


def digest(data):
    return hashlib.sha256(data).hexdigest()


def component_path(value):
    if not isinstance(value, str):
        raise ValueError("Invalid project path.")
    parts = value.split("/")
    if any(not p or p.startswith(".") or "\\" in p or not p.isprintable() for p in parts):
        raise ValueError("Choose a visible project file.")
    return parts


def validate_command(job, limit):
    fields = {
        "begin": set(),
        "list": set(),
        "read": {"path"},
        "write": {"path", "text", "expected"},
    }
    if (
        not isinstance(job, dict)
        or not isinstance(job.get("action"), str)
        or job["action"] not in fields
    ):
        raise ValueError("Unknown workspace command.")
    if (
        set(job) != fields[job["action"]] | {"action", "generation"}
        or type(job["generation"]) is not int
        or not 0 < job["generation"] <= (1 << 63) - 1
    ):
        raise ValueError("Invalid workspace command.")
    if "path" in job:
        component_path(job["path"])
    if job["action"] == "write":
        expected = job["expected"]
        if expected is not None and (
            not isinstance(expected, str) or not re.fullmatch("[0-9a-f]{64}", expected)
        ):
            raise ValueError("Invalid file revision.")
        if not isinstance(job["text"], str) or len(job["text"].encode()) > limit:
            raise ValueError("Invalid workspace text.")


@contextlib.contextmanager
def parent_directory(root, path):
    parts = component_path(path)
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
    fd = os.open(root, flags)
    try:
        for part in parts[:-1]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd, parts[-1]
    finally:
        os.close(fd)


def contents(fd, name, limit):
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise ValueError("Unsupported project file.")
        value = stream.read(limit + 1)
        if len(value) > limit:
            raise ValueError("File exceeds workspace limit.")
        return value


def inventory(root, limit):
    files_at_start = {}
    size = 0
    for folder, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(
            d for d in dirs if not d.startswith(".") and not (Path(folder) / d).is_symlink()
        )
        for name in sorted(files):
            file = Path(folder) / name
            if name.startswith(".") or file.is_symlink():
                continue
            path = file.relative_to(root).as_posix()
            component_path(path)
            size += len(path.encode()) + 1
            if size > limit:
                raise ValueError("Workspace listing exceeds configured limit.")
            info = file.stat(follow_symlinks=False)
            if stat.S_ISREG(info.st_mode):
                files_at_start[path] = stamp(info)
    return files_at_start


def stamp(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def review_patch(path, before, new):
    # Git's no-index diff handles missing trailing newlines and new files.
    with tempfile.TemporaryDirectory() as review:
        a, b = Path(review) / "a", Path(review) / "b"
        (b / PurePosixPath(path)).parent.mkdir(parents=True, exist_ok=True)
        (b / path).write_bytes(new)
        if before is not None:
            (a / PurePosixPath(path)).parent.mkdir(parents=True, exist_ok=True)
            (a / path).write_bytes(before)
        diff = subprocess.run(
            [
                "git",
                "-c",
                "core.autocrlf=false",
                "diff",
                "--no-index",
                "--no-prefix",
                "--no-ext-diff",
                "--no-textconv",
                "--",
                f"a/{path}" if before is not None else "/dev/null",
                f"b/{path}",
            ],
            cwd=review,
            capture_output=True,
        )
        if diff.returncode not in (0, 1):
            raise ValueError("Cannot generate the review.")
    return diff.stdout.decode()


def execute(root, db, key, job, limit):
    action = job["action"]
    generation = job["generation"]
    if action == "begin":
        if (
            db.execute("SELECT 1 FROM baselines WHERE generation=?", (generation,)).fetchone()
            is None
        ):
            manifest = inventory(root, limit)
            db.execute("INSERT INTO baselines VALUES (?,?)", (generation, json.dumps(manifest)))
            db.commit()
        return {"workspace": "ready"}
    if action == "list":
        return {"files": list(inventory(root, limit))}
    path = job["path"]
    with parent_directory(root, path) as (fd, name):
        tmp = ".lotus-" + key
        # A crash after link() but before unlink() may leave our second name.
        if action == "write":
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp, dir_fd=fd)
        old = contents(fd, name, limit)
        if action == "read":
            if old is None:
                return {"exists": False}
            return {"text": old.decode("utf-8"), "sha256": digest(old)}
        if action != "write":
            raise ValueError("Unknown action.")
        new = job["text"].encode()
        if len(new) > limit:
            raise ValueError("File exceeds workspace limit.")
        prior = db.execute("SELECT baseline FROM edits WHERE id=?", (key,)).fetchone()
        if prior is None:
            baseline = db.execute(
                "SELECT manifest FROM baselines WHERE generation=?", (generation,)
            ).fetchone()
            if baseline is None:
                raise ValueError("Missing starting workspace.")
            current = (
                stamp(os.stat(name, dir_fd=fd, follow_symlinks=False)) if old is not None else None
            )
            if json.loads(baseline[0]).get(path) != current:
                raise ValueError("The desktop changed this file during the run.")
            if (digest(old) if old is not None else None) != job["expected"]:
                raise ValueError("The file changed. Reopen it before editing.")
            db.execute("INSERT INTO edits VALUES (?,?)", (key, old))
            db.commit()
        before = old if prior is None else prior[0]
        patch = review_patch(path, before, new)
        if old != new:
            if old != before:
                raise ValueError("The file changed during recovery.")
            # Create within the opened directory and replace relative to that same fd.
            mode = (
                stat.S_IMODE(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode)
                if old is not None
                else 0o600
            )
            output = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=fd)
            try:
                with os.fdopen(output, "wb") as handle:
                    handle.write(new)
                    os.fchmod(handle.fileno(), mode & 0o777)
                    handle.flush()
                    os.fsync(handle.fileno())
                if contents(fd, name, limit) != old:
                    raise ValueError("The file changed during the edit.")
                if old is None:
                    os.link(tmp, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
                else:
                    os.replace(tmp, name, src_dir_fd=fd, dst_dir_fd=fd)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(tmp, dir_fd=fd)
            os.fsync(fd)
        baseline = db.execute(
            "SELECT manifest FROM baselines WHERE generation=?", (generation,)
        ).fetchone()
        manifest = json.loads(baseline[0])
        manifest[path] = stamp(os.stat(name, dir_fd=fd, follow_symlinks=False))
        db.execute(
            "UPDATE baselines SET manifest=? WHERE generation=?", (json.dumps(manifest), generation)
        )
        return {"path": path, "patch": patch, "sha256": digest(new)}


def serve(root, state, limit, poll):
    import fcntl

    with (state / "daemon.lock").open("ab") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        root.mkdir(exist_ok=True, mode=0o700)
        marker = state / "initialized"
        if marker.exists() and not (root / ".git").is_dir():
            raise RuntimeError("Existing Git workspace requires restoration.")
        if not marker.exists() and not (root / ".git").exists():
            subprocess.run(["git", "init", "--quiet", "--template=", "--", str(root)], check=True)
        if not (root / ".git").is_dir() or (root / ".git").is_symlink():
            raise RuntimeError("Invalid workspace Git directory.")
        marker.touch(exist_ok=True)
        with contextlib.closing(sqlite3.connect(state / "worker.sqlite")) as db:
            db.execute("CREATE TABLE IF NOT EXISTS edits(id TEXT PRIMARY KEY, baseline BLOB)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY, hash TEXT, result BLOB)"
            )
            db.execute("CREATE TABLE IF NOT EXISTS writer(generation INTEGER PRIMARY KEY)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS baselines(generation INTEGER PRIMARY KEY, manifest TEXT)"
            )
            workspace_errors = (OSError, ValueError, KeyError, TypeError)
            while True:
                for entry in state.glob("*.ready"):
                    key = UUID(entry.stem).hex
                    payload = entry.read_bytes()
                    receipt = db.execute(
                        "SELECT hash,result FROM receipts WHERE id=?", (key,)
                    ).fetchone()
                    if receipt is None:
                        try:
                            job = json.loads(payload)
                            validate_command(job, limit)
                            latest = db.execute("SELECT max(generation) FROM writer").fetchone()[0]
                            generation = job["generation"]
                            if latest is not None and generation < latest:
                                raise ValueError("Superseded workspace writer.")
                            if latest != generation:
                                db.execute("DELETE FROM writer")
                                db.execute("INSERT INTO writer VALUES (?)", (generation,))
                                db.commit()
                            result = {"result": execute(root, db, key, job, limit)}
                        except workspace_errors:
                            # A write journals its baseline before touching the target.
                            # After that point an error may follow a successful replace.
                            prepared = db.execute(
                                "SELECT 1 FROM edits WHERE id=?", (key,)
                            ).fetchone()
                            result = (
                                {"error": "Workspace outcome requires inspection."}
                                if prepared
                                else {"rejected": True}
                            )
                        encoded = json.dumps(result).encode()
                        db.execute(
                            "INSERT INTO receipts VALUES (?,?,?)", (key, digest(payload), encoded)
                        )
                        db.commit()
                    else:
                        encoded = (
                            receipt[1]
                            if receipt[0] == digest(payload)
                            else b'{"error":"Conflicting replay."}'
                        )
                    temp = state / (key + ".out.tmp")
                    temp.write_bytes(encoded)
                    temp.replace(state / (key + ".out"))
                    entry.unlink(missing_ok=True)
                time.sleep(poll)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["serve", "submit"])
    parser.add_argument("root", type=Path)
    parser.add_argument("state", type=Path)
    parser.add_argument("limit", type=int)
    parser.add_argument("poll", type=float)
    parser.add_argument("timeout", type=float)
    parser.add_argument("key", nargs="?")
    parser.add_argument("upload_id", nargs="?")
    args = parser.parse_args()
    root, state = args.root.resolve(), args.state.resolve()
    if (
        root.is_relative_to(state)
        or state.is_relative_to(root)
        or min(args.limit, args.poll, args.timeout) <= 0
    ):
        raise ValueError("Invalid sandbox configuration.")
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.mode == "serve":
        serve(root, state, args.limit, args.poll)
        return
    key = UUID(args.key).hex
    # Input is uploaded by the dispatcher; only an opaque ID appears in process arguments.
    incoming = state / (UUID(args.upload_id).hex + ".in")
    incoming.replace(state / (key + ".ready"))
    import fcntl

    with (state / "daemon.lock").open("ab") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass  # The running daemon will pick up this command.
        else:
            fcntl.flock(lock, fcntl.LOCK_UN)
            subprocess.Popen(
                [sys.executable, "-I", __file__, "serve", *sys.argv[2:7]],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        if (state / (key + ".out")).is_file():
            return
        time.sleep(args.poll)
    raise TimeoutError("Workspace action pending.")


if __name__ == "__main__":
    main()
