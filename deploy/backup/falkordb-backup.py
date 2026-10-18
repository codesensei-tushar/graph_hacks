#!/usr/bin/env python3
"""Nightly FalkorDB backups to GCS, and the check that a backup restores.

    falkordb-backup.py backup  [--container ID] [--bucket NAME | --out-dir DIR]
    falkordb-backup.py verify  RDB MANIFEST [--image IMAGE] [--keep]

``backup`` runs on brain-vm as root, from brain-falkordb-backup.service. It counts the
nodes per label and the edges per relationship type in every graph, runs BGSAVE on the
FalkorDB container, waits for it to finish, counts again, and copies the RDB out. If the two
counts differ (a write landed while the snapshot was taken) it tries again, up to three
times, and records ``"consistent": false`` if they never agree. It then uploads
``brain-falkordb-<UTC timestamp>.rdb`` and a JSON manifest of the same name (the counts,
the image, the RDB's size and sha256) to ``gs://<bucket>/falkordb/YYYY/MM/DD/``. The upload
uses the VM service account's token from the metadata server and the JSON API with
``ifGenerationMatch=0``, so it needs only ``storage.objects.create`` and never overwrites.

``verify`` runs anywhere with Docker. It checks the RDB's sha256 against the manifest, loads
the RDB into a throwaway FalkorDB container (no network, the manifest's image), counts the
same way, compares per graph, per label and per type, prints the differences, and removes
the container. It exits 0 only when every count matches.

Standard library only: the VM has python3 but no pip packages. See docs/DEPLOY.md,
"Backups and restore".
"""

import argparse
import base64
import datetime
import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

COMPOSE_DIR = "/opt/brain"
OBJECT_PREFIX = "falkordb"
ATTEMPTS = 3
SAVE_TIMEOUT_SECONDS = 900
READY_TIMEOUT_SECONDS = 300
METADATA_TOKEN_URL = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
UPLOAD_URL = "https://storage.googleapis.com/upload/storage/v1/b/{bucket}/o"
MANIFEST_FORMAT = 1


def log(message: str) -> None:
    print(f"[falkordb-backup] {message}", file=sys.stderr, flush=True)


class BackupError(Exception):
    pass


# --- talking to a FalkorDB container -----------------------------------------------------


def docker(*args: str, timeout: float = 120) -> str:
    done = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    if done.returncode != 0:
        raise BackupError(f"docker {args[0]} failed: {done.stderr.strip()}")
    return done.stdout


def redis(container: str, *args: str, as_json: bool = False):
    """One redis-cli command inside the container. REDISCLI_AUTH, set in the production
    container, supplies the password, so it never appears on a command line."""
    out = docker("exec", container, "redis-cli", *(["--json"] if as_json else []), *args).strip()
    # redis-cli exits 0 on an error reply; it prints `error:"..."` (--json) or `(error) ...`.
    if out.startswith(("error:", "(error)", "ERR ", "NOAUTH", "WRONGPASS")):
        raise BackupError(f"redis-cli {args[0]}: {out}")
    return json.loads(out) if as_json else out


def info(container: str, section: str) -> dict[str, str]:
    lines = redis(container, "INFO", section).splitlines()
    return dict(line.split(":", 1) for line in lines if ":" in line and not line.startswith("#"))


def ident(name: str) -> str:
    """A label or relationship type as a Cypher identifier."""
    return "`" + name.replace("`", "``") + "`"


def ro_rows(container: str, graph: str, query: str) -> list:
    reply = redis(container, "GRAPH.RO_QUERY", graph, query, as_json=True)
    # [header, rows, stats]; a procedure with no rows can come back as [header, [], stats].
    return reply[1] if len(reply) >= 2 else []


def count(container: str, graph: str, query: str) -> int:
    rows = ro_rows(container, graph, query)
    return int(rows[0][0]) if rows else 0


def graph_counts(container: str) -> dict:
    """Per graph: nodes per label, edges per relationship type, and both totals. A node with
    two labels counts once under each label and once in the total."""
    counts = {}
    for graph in sorted(redis(container, "GRAPH.LIST", as_json=True) or []):
        labels = sorted(row[0] for row in ro_rows(container, graph, "CALL db.labels()"))
        types = sorted(row[0] for row in ro_rows(container, graph, "CALL db.relationshipTypes()"))
        counts[graph] = {
            "node_total": count(container, graph, "MATCH (n) RETURN count(n)"),
            "edge_total": count(container, graph, "MATCH ()-[r]->() RETURN count(r)"),
            "nodes": {label: count(container, graph, f"MATCH (n:{ident(label)}) RETURN count(n)") for label in labels},
            "edges": {kind: count(container, graph, f"MATCH ()-[r:{ident(kind)}]->() RETURN count(r)") for kind in types},
        }
    return counts


def wait_for(predicate, timeout: float, what: str, interval: float = 1.0):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise BackupError(f"timed out after {timeout:.0f}s waiting for {what}")
        time.sleep(interval)


def sha256_of(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# --- backup ------------------------------------------------------------------------------


def compose_container(compose_dir: str) -> str:
    container = docker("compose", "--project-directory", compose_dir, "ps", "-q", "falkordb").strip()
    if not container:
        raise BackupError(f"no running falkordb service in {compose_dir}")
    return container


def snapshot(container: str, dest: str) -> tuple[dict, dict | None]:
    """BGSAVE, then copy the RDB it wrote to ``dest``. Returns the counts taken just before
    the save and, if they changed by the time it had forked, the counts after (else None)."""
    rdb_dir = redis(container, "CONFIG", "GET", "dir").splitlines()[-1]
    rdb_name = redis(container, "CONFIG", "GET", "dbfilename").splitlines()[-1]

    # An automatic save may be running; BGSAVE would be refused until it ends.
    wait_for(lambda: info(container, "persistence").get("rdb_bgsave_in_progress") == "0", SAVE_TIMEOUT_SECONDS, "an earlier BGSAVE")
    saves_before = int(info(container, "persistence").get("rdb_saves", "0"))
    before = graph_counts(container)
    # SCHEDULE: if an AOF rewrite is running, save as soon as it ends instead of failing.
    log(f"BGSAVE: {redis(container, 'BGSAVE', 'SCHEDULE')}")
    after = graph_counts(container)

    def finished():
        persistence = info(container, "persistence")
        if persistence.get("rdb_bgsave_in_progress") != "0" or int(persistence.get("rdb_saves", "0")) <= saves_before:
            return None
        return persistence

    persistence = wait_for(finished, SAVE_TIMEOUT_SECONDS, "BGSAVE to finish", interval=2)
    if persistence.get("rdb_last_bgsave_status") != "ok":
        raise BackupError(f"BGSAVE failed: rdb_last_bgsave_status={persistence.get('rdb_last_bgsave_status')}")
    saves_done = persistence.get("rdb_saves")

    docker("cp", f"{container}:{rdb_dir}/{rdb_name}", dest, timeout=SAVE_TIMEOUT_SECONDS)
    # If another save finished while we copied, the file may be that one, not ours.
    now = info(container, "persistence")
    if now.get("rdb_saves") != saves_done or now.get("rdb_bgsave_in_progress") != "0":
        raise BackupError("another save ran while the RDB was copied")
    return before, (None if after == before else after)


def metadata_token() -> str:
    request = urllib.request.Request(METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)["access_token"]


def upload(bucket: str, name: str, path: str, content_type: str, token: str) -> None:
    query = urllib.parse.urlencode({"uploadType": "media", "name": name, "ifGenerationMatch": "0"})
    size = os.path.getsize(path)
    md5 = hashlib.md5(usedforsecurity=False)
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            md5.update(block)
    with open(path, "rb") as body:
        request = urllib.request.Request(
            f"{UPLOAD_URL.format(bucket=bucket)}?{query}",
            data=body,
            method="POST",
            headers={"Authorization": f"Bearer {token}", "Content-Type": content_type, "Content-Length": str(size)},
        )
        try:
            with urllib.request.urlopen(request, timeout=SAVE_TIMEOUT_SECONDS) as response:
                stored = json.load(response)
        except urllib.error.HTTPError as error:
            raise BackupError(f"upload of {name} failed: HTTP {error.code} {error.read()[:300]!r}") from None
    if stored.get("md5Hash") != base64.b64encode(md5.digest()).decode():
        raise BackupError(f"upload of {name}: GCS md5 {stored.get('md5Hash')} does not match the local file")
    log(f"uploaded gs://{bucket}/{name} ({size} bytes)")


def run_backup(args) -> int:
    container = args.container or compose_container(args.compose_dir)
    image = docker("inspect", "--format", "{{.Config.Image}}", container).strip()
    stamp = datetime.datetime.now(datetime.UTC)
    base = f"brain-falkordb-{stamp:%Y%m%dT%H%M%SZ}"
    work = tempfile.mkdtemp(prefix="falkordb-backup-", dir=args.work_dir)
    try:
        rdb = os.path.join(work, f"{base}.rdb")
        before = after = None
        for attempt in range(1, ATTEMPTS + 1):
            try:
                before, after = snapshot(container, rdb)
            except BackupError as error:
                if attempt == ATTEMPTS:
                    raise
                log(f"attempt {attempt}: {error}; retrying")
                time.sleep(5)
                continue
            if after is None:
                break
            if attempt < ATTEMPTS:
                log(f"attempt {attempt}: counts changed while the snapshot was taken; retrying")
                time.sleep(5)

        manifest = {
            "format": MANIFEST_FORMAT,
            "created_at": stamp.isoformat(timespec="seconds"),
            "host": socket.gethostname(),
            "image": image,
            "rdb": {"name": f"{base}.rdb", "bytes": os.path.getsize(rdb), "sha256": sha256_of(rdb)},
            # True when the counts before and after the BGSAVE fork agree, so they are the RDB's.
            "consistent": after is None,
            "graphs": before,
        }
        if after is not None:
            manifest["graphs_after"] = after
            log("counts never settled; the manifest records both sets and consistent=false")
        manifest_path = os.path.join(work, f"{base}.json")
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2, sort_keys=True)
            f.write("\n")
        totals = ", ".join(f"{g} {c['node_total']}n/{c['edge_total']}e" for g, c in before.items())
        log(f"snapshot {base}: {manifest['rdb']['bytes']} bytes, graphs: {totals or 'none'}")

        if args.out_dir:
            os.makedirs(args.out_dir, exist_ok=True)
            for path in (rdb, manifest_path):
                shutil.copy2(path, args.out_dir)
            log(f"kept in {args.out_dir} (no upload)")
        else:
            prefix = f"{OBJECT_PREFIX}/{stamp:%Y/%m/%d}"
            token = metadata_token()
            # The RDB first: a manifest in the bucket means its RDB is there too.
            upload(args.bucket, f"{prefix}/{base}.rdb", rdb, "application/octet-stream", token)
            upload(args.bucket, f"{prefix}/{base}.json", manifest_path, "application/json", token)
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --- verify ------------------------------------------------------------------------------


def compare(expected: dict, actual: dict, after: dict | None = None) -> list[str]:
    """Every difference between the manifest's counts and the restored ones. With ``after``
    (a manifest whose counts moved during the snapshot), a count passes if it lies between
    its before and after values, since the fork happened somewhere in that window."""
    after = after or expected
    problems = []
    for graph in sorted(set(expected) | set(actual)):
        if graph not in actual:
            problems.append(f"{graph}: missing from the restore")
            continue
        if graph not in expected:
            problems.append(f"{graph}: in the restore but not in the manifest")
            continue
        want, got, later = expected[graph], actual[graph], after.get(graph, expected[graph])

        def check(what: str, a, b, c) -> None:
            bounds = [x for x in (a, c) if x is not None]
            if b is None or not bounds or not min(bounds) <= b <= max(bounds):
                shown = a if a == c else f"{a}..{c}"
                problems.append(f"{graph}: {what} {shown} expected, {b} restored")

        for total in ("node_total", "edge_total"):
            check(total, want[total], got[total], later[total])
        for kind in ("nodes", "edges"):
            for name in sorted(set(want[kind]) | set(got[kind]) | set(later[kind])):
                check(f"{kind} :{name}", want[kind].get(name), got[kind].get(name), later[kind].get(name))
    return problems


def run_verify(args) -> int:
    with open(args.manifest) as f:
        manifest = json.load(f)
    digest = sha256_of(args.rdb)
    if digest != manifest["rdb"]["sha256"]:
        log(f"sha256 mismatch: {digest} on disk, {manifest['rdb']['sha256']} in the manifest")
        return 1
    image = args.image or manifest["image"]
    name = f"brain-restore-check-{secrets.token_hex(4)}"
    log(f"loading {os.path.basename(args.rdb)} into {name} ({image})")
    # No network and no published port: the container is reached through docker exec only.
    docker("create", "--name", name, "--network", "none", "-e", "REDIS_ARGS=--appendonly no", image)
    staging = tempfile.mkdtemp(prefix="falkordb-restore-")
    try:
        # The data directory exists only once the container has started, so copy a whole
        # directory holding dump.rdb, which docker cp creates in place.
        staged = os.path.join(staging, "data", "dump.rdb")
        os.makedirs(os.path.dirname(staged))
        try:
            os.link(args.rdb, staged)
        except OSError:
            shutil.copyfile(args.rdb, staged)
        docker("cp", os.path.join(staging, "data"), f"{name}:/var/lib/falkordb/", timeout=SAVE_TIMEOUT_SECONDS)
        docker("start", name)

        def ready():
            try:
                return redis(name, "PING") == "PONG" and info(name, "persistence").get("loading") == "0"
            except BackupError:
                return False

        wait_for(ready, READY_TIMEOUT_SECONDS, "the restore container to load the RDB")
        restored = graph_counts(name)
    finally:
        if args.keep:
            log(f"kept container {name}; remove it with: docker rm -f -v {name}")
        else:
            subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True)
        shutil.rmtree(staging, ignore_errors=True)

    problems = compare(manifest["graphs"], restored, manifest.get("graphs_after"))
    for graph, c in restored.items():
        log(f"restored {graph}: {c['node_total']} nodes in {len(c['nodes'])} labels, {c['edge_total']} edges in {len(c['edges'])} types")
    if problems:
        if not manifest.get("consistent", True):
            log("note: writes landed during this snapshot; each count was checked against its before..after range")
        for problem in problems:
            log(f"MISMATCH {problem}")
        return 1
    log(f"OK: {len(restored)} graphs match the manifest of {manifest['created_at']}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    backup = sub.add_parser("backup", help="snapshot FalkorDB and upload the RDB and its manifest")
    backup.add_argument("--container", help="FalkorDB container (default: the falkordb service of --compose-dir)")
    backup.add_argument("--compose-dir", default=COMPOSE_DIR)
    where = backup.add_mutually_exclusive_group()
    where.add_argument("--bucket", default=os.environ.get("BRAIN_BACKUP_BUCKET"), help="GCS bucket (env BRAIN_BACKUP_BUCKET)")
    where.add_argument("--out-dir", help="keep the RDB and manifest here instead of uploading")
    backup.add_argument("--work-dir", default=None, help="scratch space for the RDB (default: the system temp dir)")

    verify = sub.add_parser("verify", help="load an RDB into a throwaway container and compare counts")
    verify.add_argument("rdb")
    verify.add_argument("manifest")
    verify.add_argument("--image", help="FalkorDB image (default: the one in the manifest)")
    verify.add_argument("--keep", action="store_true", help="leave the restore container running")

    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            if not args.bucket and not args.out_dir:
                parser.error("backup needs --bucket (or BRAIN_BACKUP_BUCKET) or --out-dir")
            return run_backup(args)
        return run_verify(args)
    except (BackupError, subprocess.TimeoutExpired, OSError) as error:
        log(f"failed: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
