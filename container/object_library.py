"""Persistent, searchable 3D object catalog. Only the trusted bot process publishes files."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import struct
import time
import uuid

MAX_MODEL_BYTES = 75_000_000


def root_path():
    return Path(os.environ.get("OBJECT_LIBRARY_DIR") or "/library3d").resolve()


def inside(path, root):
    resolved = Path(path).resolve()
    if not resolved.is_relative_to(Path(root).resolve()):
        raise ValueError("asset path leaves its directory")
    return resolved


def inspect_glb(path):
    path = Path(path)
    if not 20 < path.stat().st_size <= MAX_MODEL_BYTES:
        raise ValueError("invalid GLB size")
    with path.open("rb") as file:
        magic, version, length = struct.unpack("<4sII", file.read(12))
        size, kind = struct.unpack("<II", file.read(8))
        if magic != b"glTF" or version != 2 or length != path.stat().st_size or kind != 0x4E4F534A or size > 10_000_000:
            raise ValueError("invalid GLB container")
        data = json.loads(file.read(size))
    if not data.get("meshes"):
        raise ValueError("GLB has no geometry")
    if any(str(item.get("uri") or "") and not str(item["uri"]).startswith("data:")
           for item in data.get("buffers", []) + data.get("images", [])):
        raise ValueError("library models must embed their buffers and textures")
    parts = []
    tagged_parts = []
    for node in data.get("nodes", []):
        tag = (node.get("extras") or {}).get("library_part")
        if tag:
            tagged_parts.append(str(tag)[:100])
        name = tag or node.get("name")
        if name:
            parts.append(str(name)[:100])
    return {"meshes": len(data["meshes"]), "parts": list(dict.fromkeys(tagged_parts or parts))[:150]}


def connect(create=False):
    root = root_path()
    db_path = root / "catalog.sqlite3"
    if not create and not db_path.exists():
        return None
    root.mkdir(parents=True, exist_ok=True, mode=0o755)
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("""CREATE TABLE IF NOT EXISTS objects (
        id TEXT PRIMARY KEY, logical_key TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL,
        tags TEXT NOT NULL, parts TEXT NOT NULL, model_file TEXT NOT NULL, preview_file TEXT,
        sha256 TEXT UNIQUE NOT NULL, fingerprint TEXT UNIQUE, credit TEXT NOT NULL, source_url TEXT NOT NULL,
        license TEXT NOT NULL, origin TEXT NOT NULL, created_at REAL NOT NULL, last_used REAL,
        uses INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'active')""")
    conn.commit()
    db_path.chmod(0o600)
    return conn


def row_asset(row):
    root = root_path()
    result = dict(row)
    result["path"] = str(inside(root / result.pop("model_file"), root))
    preview = result.pop("preview_file")
    result["preview"] = str(inside(root / preview, root)) if preview else None
    result["tags"] = json.loads(result["tags"])
    result["parts"] = json.loads(result["parts"])
    return result


def search(query="", limit=20, include_archived=False):
    conn = connect()
    if conn is None:
        return []
    try:
        rows = conn.execute("SELECT * FROM objects" + ("" if include_archived else " WHERE status='active'")).fetchall()
    finally:
        conn.close()
    terms = set(re.findall(r"[\w-]{2,}", query.casefold()))
    ranked = []
    for row in rows:
        asset = row_asset(row)
        if not Path(asset["path"]).is_file():
            continue
        score = 0
        for value, weight in ((asset["title"] + " " + asset["logical_key"] + " " + asset["id"], 8),
                              (" ".join(asset["tags"]), 6), (asset["description"], 2)):
            words = re.findall(r"[\w-]{2,}", value.casefold())
            score += weight * sum(any(word == term or (min(len(word), len(term)) >= 4 and
                                  (word.startswith(term) or term.startswith(word))) for word in words) for term in terms)
        if terms and not score:
            continue
        ranked.append((score, asset["uses"], asset["created_at"], asset))
    return [entry[3] for entry in sorted(ranked, key=lambda entry: entry[:3], reverse=True)[:limit]]


def catalog(query="", limit=12):
    matches = search(query, limit)
    ids = {item["id"] for item in matches}
    for item in search("", limit):
        if item["id"] not in ids and len(matches) < limit:
            matches.append(item)
    return matches


def publish(model_path, metadata, preview_path=None, origin="generated"):
    info = inspect_glb(model_path)
    key = str(metadata.get("key") or metadata.get("id") or "").lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,49}", key):
        raise ValueError("invalid object key")
    digest = hashlib.sha256(Path(model_path).read_bytes()).hexdigest()
    fingerprint = metadata.get("fingerprint")
    if fingerprint and not re.fullmatch(r"[a-f0-9]{64}", str(fingerprint)):
        raise ValueError("invalid object fingerprint")
    tags = list(dict.fromkeys(str(tag).strip()[:80] for tag in metadata.get("tags", []) if str(tag).strip()))[:30]
    title = str(metadata.get("title") or key)[:120]
    description = str(metadata.get("description") or "")[:1200]
    root = root_path()
    conn = connect(create=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        found = conn.execute("SELECT * FROM objects WHERE sha256=? OR fingerprint=?", (digest, fingerprint)).fetchone()
        if found:
            conn.rollback()
            return row_asset(found), False
        count = conn.execute("SELECT count(*) FROM objects").fetchone()[0]
        if count >= int(os.environ.get("OBJECT_LIBRARY_MAX_ASSETS") or 500):
            raise ValueError("object library count limit reached")
        used = sum(p.stat().st_size for p in root.glob("assets/*/model.glb"))
        if used + Path(model_path).stat().st_size > float(os.environ.get("OBJECT_LIBRARY_MAX_MB") or 512) * 1024 * 1024:
            raise ValueError("object library storage limit reached")
        asset_id = key if not conn.execute("SELECT 1 FROM objects WHERE id=?", (key,)).fetchone() else key[:40] + "-" + digest[:8]
        directory = root / "assets" / digest
        directory.mkdir(parents=True, exist_ok=True, mode=0o755)
        target = directory / "model.glb"
        temporary = directory / (uuid.uuid4().hex + ".tmp")
        shutil.copyfile(model_path, temporary)
        temporary.chmod(0o644)
        temporary.replace(target)
        preview_file = None
        if preview_path and Path(preview_path).is_file() and Path(preview_path).stat().st_size <= 5_000_000:
            preview = directory / "preview.png"
            shutil.copyfile(preview_path, preview)
            preview.chmod(0o644)
            preview_file = str(preview.relative_to(root))
        record = {"id":asset_id, "logical_key":key, "title":title, "description":description,
                  "tags":json.dumps(tags, ensure_ascii=False), "parts":json.dumps(info["parts"], ensure_ascii=False),
                  "model_file":str(target.relative_to(root)), "preview_file":preview_file, "sha256":digest,
                  "fingerprint":fingerprint, "credit":str(metadata.get("credit") or "")[:300],
                  "source_url":str(metadata.get("source_url") or "")[:1000], "license":str(metadata.get("license") or "")[:200],
                  "origin":origin, "created_at":time.time()}
        conn.execute("INSERT INTO objects (" + ",".join(record) + ") VALUES (" + ",".join("?" for _ in record) + ")", tuple(record.values()))
        (directory / "metadata.json").write_text(json.dumps({**metadata, "id":asset_id, "sha256":digest, "parts":info["parts"]}, ensure_ascii=False, indent=2))
        conn.commit()
        return row_asset(conn.execute("SELECT * FROM objects WHERE id=?", (asset_id,)).fetchone()), True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def publish_candidates(result, staging):
    staging = Path(staging).resolve()
    saved = []
    for item in result.get("candidates", [])[:4]:
        model = inside(staging / item["file"], staging)
        preview = inside(staging / item["preview"], staging) if item.get("preview") else None
        asset, created = publish(model, item, preview)
        saved.append({"id":asset["id"], "created":created})
    return saved


def mark_used(ids):
    conn = connect()
    if conn is None:
        return
    try:
        with conn:
            conn.executemany("UPDATE objects SET uses=uses+1,last_used=? WHERE id=?", [(time.time(), value) for value in set(ids)])
    finally:
        conn.close()


def set_archived(asset_id, archived=True):
    conn = connect()
    if conn is None:
        return False
    try:
        with conn:
            cursor = conn.execute("UPDATE objects SET status=? WHERE id=?", ("archived" if archived else "active", asset_id))
        return cursor.rowcount == 1
    finally:
        conn.close()


def describe(query=""):
    items = search(query, 10)
    if not items:
        return "В библиотеке пока нет подходящих объектов. Удачные новые 3D-объекты сохраняются после рендера."
    lines = ["📦 **Библиотека 3D-объектов**"]
    for item in items:
        line = f"\n`{item['id']}` — **{item['title']}**\n{item['description'][:130]} · использован {item['uses']} раз"
        if sum(len(s) for s in lines) + len(line) > 1850:
            break
        lines.append(line)
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Manage the persistent 3D object library")
    sub = parser.add_subparsers(dest="command", required=True)
    listing = sub.add_parser("list"); listing.add_argument("query", nargs="?", default="")
    adding = sub.add_parser("import"); adding.add_argument("file"); adding.add_argument("--id", required=True)
    adding.add_argument("--title", required=True); adding.add_argument("--description", default="")
    adding.add_argument("--tags", default=""); adding.add_argument("--credit", default="")
    adding.add_argument("--source-url", default=""); adding.add_argument("--license", default="")
    for command in ("archive", "restore"):
        sub.add_parser(command).add_argument("id")
    args = parser.parse_args()
    if args.command == "list":
        print(json.dumps(search(args.query, 100), ensure_ascii=False, indent=2))
    elif args.command == "import":
        asset, created = publish(args.file, {"key":args.id, "title":args.title, "description":args.description,
            "tags":args.tags.split(","), "credit":args.credit, "source_url":args.source_url, "license":args.license}, origin="imported")
        print(json.dumps({"id":asset["id"], "created":created}))
    else:
        print(json.dumps({"updated":set_archived(args.id, args.command == "archive")}))


if __name__ == "__main__":
    main()
