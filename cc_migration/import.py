#!/usr/bin/env python3
"""Import ./cc_archive (written by export.py) into the destination CompanyCam account.

Usage:
    python3 import.py --dry-run                         # no API calls; lists what would be created
    CC_NEW_TOKEN=... python3 import.py --project 123     # import one source project (by original ID)
    CC_NEW_TOKEN=... python3 import.py                   # import everything

Every created project, comment and photo is recorded in import_log.json the moment it is created,
keyed by its original ID. Anything already in the log is skipped, so re-running never duplicates it.
"""
import argparse
import base64
import hashlib
import mimetypes
import os
import sys
from collections import Counter
from datetime import datetime, timezone

from cc_api import ApiError, Client, log, read_json, write_json

mimetypes.add_type("image/heic", ".heic")


def fmt_date(value):
    """Render an API timestamp (ISO string or Unix seconds) as 'YYYY-MM-DD HH:MM UTC'."""
    if value in (None, ""):
        return "unknown date"
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            dt = datetime.fromtimestamp(int(value), tz=timezone.utc)
        else:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            dt = dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return str(value)


def iso_date(value):
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) or str(value).isdigit():
        return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat()
    return str(value)


def unix_time(value):
    """The photos endpoint takes captured_at as integer Unix seconds, not an ISO string."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) or str(value).isdigit():
        return int(value)
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return int((dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp())


def project_payload(project):
    addr = project.get("address") or {}
    street = ", ".join(s for s in (addr.get("street_address_1"), addr.get("street_address_2")) if s)
    payload = {
        "name": project.get("name") or "Untitled",
        "street_address_1": street,
        "city": addr.get("city") or "",
        "state": addr.get("state") or "",
        "postal_code": addr.get("postal_code") or "",
        "country": addr.get("country") or "",
    }
    coords = project.get("coordinates") or {}
    if coords.get("lat") is not None and coords.get("lon") is not None and (coords["lat"] or coords["lon"]):
        payload["coordinates"] = {"lat": str(coords["lat"]), "lon": str(coords["lon"])}
    return payload


def comment_text(comment):
    author = comment.get("creator_name") or "Unknown author"
    return f"[Originally posted by {author} on {fmt_date(comment.get('created_at'))}]\n{comment.get('content') or ''}"


def photo_description(photo):
    original = (photo.get("description") or "").strip()
    footer = (f"Originally captured {fmt_date(photo.get('captured_at'))} "
              f"by {photo.get('creator_name') or 'unknown photographer'}. "
              f"(Original CompanyCam photo ID {photo['id']})")
    return f"{original}\n\n{footer}" if original else footer


def load_archive(root, only_ids, limit):
    manifest = read_json(os.path.join(root, "manifest.json"))
    if not manifest:
        sys.exit(f"No manifest.json in {root}; run export.py first.")
    items = []
    for entry in manifest["projects"]:
        if only_ids and str(entry["id"]) not in only_ids:
            continue
        pdir = os.path.join(root, entry["folder"])
        items.append({
            "dir": pdir,
            "project": read_json(os.path.join(pdir, "project.json")),
            "comments": read_json(os.path.join(pdir, "comments.json"), []),
            "photos": read_json(os.path.join(pdir, "photos.json"), []),
        })
    return items[:limit] if limit else items


class ImportLog:
    def __init__(self, path):
        self.path = path
        self.data = read_json(path, {"projects": {}})

    def project(self, old_id):
        return self.data["projects"].setdefault(str(old_id), {"comments": {}, "photos": {}})

    def owner_of(self, new_project_id):
        for old_id, rec in self.data["projects"].items():
            if rec.get("new_id") == str(new_project_id):
                return old_id
        return None

    def save(self):
        write_json(self.path, self.data)


def upload_photo(api, new_project_id, photo, path):
    content_type = mimetypes.guess_type(path)[0] or "image/jpeg"
    with open(path, "rb") as f:
        checksum = base64.b64encode(hashlib.md5(f.read()).digest()).decode()

    def create(extra):
        upload = api.post("/uploads", {"upload": {
            "filename": os.path.basename(path), "content_type": content_type, "checksum": checksum}})
        headers = upload.get("headers") or {"Content-Type": content_type, "Content-MD5": checksum}
        api.put_file(upload["upload_url"], path, headers)
        return api.post(f"/projects/{new_project_id}/photos",
                        {"photo": {"upload_id": upload["upload_id"], "description": description, **extra}})

    description = photo_description(photo)
    extra = {k: v for k, v in (("captured_at", unix_time(photo.get("captured_at"))),
                               ("tags", [t for t in photo.get("tags") or [] if t])) if v}
    try:
        created = create(extra)
    except ApiError as e:
        if e.status != 422 or not extra:
            raise
        log(f"    photo {photo['id']}: API rejected captured_at/tags ({e}); retrying with description only")
        created = create({})

    # Make sure the description landed; fall back to the dedicated endpoint if it didn't.
    desc = created.get("description") or {}
    if not (desc.get("plain_text_content") or desc.get("html_content")):
        api.post(f"/photos/{created['id']}/descriptions", {"description": {"content": description}})
    return str(created["id"])


def import_project(api, item, ilog):
    project = item["project"]
    old_id = str(project["id"])
    rec = ilog.project(old_id)
    failures = []

    if not rec.get("new_id"):
        created = api.post("/projects", {"project": project_payload(project)})
        new_id = str(created["id"])
        owner = ilog.owner_of(new_id)
        if owner and owner != old_id:
            # The API returns an existing project when name+address match; don't merge two originals.
            raise RuntimeError(f"destination returned project {new_id}, already used for source project {owner} "
                               f"(same name and address); skipped to avoid merging")
        rec["new_id"] = new_id
        rec["name"] = project.get("name")
        ilog.save()
        log(f"  created project {new_id}")
    new_id = rec["new_id"]

    notepad = (project.get("description") or "").strip()
    if notepad and not rec.get("notepad_copied"):
        api.patch(f"/projects/{new_id}/description", {"description": notepad})
        rec["notepad_copied"] = True
        ilog.save()

    for c in item["comments"]:
        cid = str(c["id"])
        if cid in rec["comments"]:
            continue
        try:
            created = api.post(f"/projects/{new_id}/comments", {"comment": {"content": comment_text(c)}})
            rec["comments"][cid] = str(created["id"])
            ilog.save()
        except Exception as e:
            failures.append(f"project {old_id} comment {cid}: {e}")

    for p in item["photos"]:
        pid = str(p["id"])
        if pid in rec["photos"]:
            continue
        if not p.get("file"):
            failures.append(f"project {old_id} photo {pid}: no file in archive (re-run export.py)")
            continue
        try:
            rec["photos"][pid] = upload_photo(api, new_id, p, os.path.join(item["dir"], p["file"]))
            ilog.save()
        except Exception as e:
            failures.append(f"project {old_id} photo {pid}: {e}")
            log(f"    FAILED photo {pid}: {e}")

    log(f"  {len(rec['comments'])}/{len(item['comments'])} comments, "
        f"{len(rec['photos'])}/{len(item['photos'])} photos imported")
    rec["complete"] = not failures
    ilog.save()
    return failures


def identity(project):
    """Name + address: what the API uses to decide a create matches an existing project."""
    payload = project_payload(project)
    return tuple(payload[k] for k in ("name", "street_address_1", "city", "state", "postal_code", "country"))


def dry_run(items, ilog):
    keys = Counter(identity(i["project"]) for i in items)
    totals = Counter()
    for item in items:
        p = item["project"]
        rec = ilog.data["projects"].get(str(p["id"]), {})
        payload = project_payload(p)
        new_comments = [c for c in item["comments"] if str(c["id"]) not in rec.get("comments", {})]
        new_photos = [x for x in item["photos"] if str(x["id"]) not in rec.get("photos", {})]
        missing_files = [x for x in new_photos if not x.get("file")]
        notepad = (p.get("description") or "").strip()

        action = f"exists as {rec['new_id']}" if rec.get("new_id") else "CREATE"
        addr = ", ".join(v for k, v in payload.items() if k not in ("name", "coordinates") and v) or "(no address)"
        print(f"[{action}] {p['id']} '{payload['name']}' — {addr}")
        print(f"    notepad: {'copy ' + str(len(notepad)) + ' chars' if notepad and not rec.get('notepad_copied') else '-'}"
              f" | comments: {len(new_comments)} | photos: {len(new_photos)}"
              f"{' | archived in source' if p.get('archived') else ''}")
        if missing_files:
            print(f"    WARNING: {len(missing_files)} photos have no file in the archive and will be skipped")
        missing = [k for k in ("street_address_1", "city", "state", "postal_code", "country") if not payload[k]]
        if missing:
            print(f"    WARNING: address is missing {', '.join(missing)}; the API may reject the project")
        if keys[identity(p)] > 1:
            print("    WARNING: another project has the same name and address; the API would merge them")
        totals["projects"] += 0 if rec.get("new_id") else 1
        totals["comments"] += len(new_comments)
        totals["photos"] += len(new_photos) - len(missing_files)
        totals["bytes"] += sum(os.path.getsize(os.path.join(item["dir"], x["file"]))
                               for x in new_photos if x.get("file"))
    print(f"\nWould create {totals['projects']} projects, {totals['comments']} comments, "
          f"{totals['photos']} photos ({totals['bytes'] / 1e6:.1f} MB to upload).")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--archive", default="./cc_archive")
    ap.add_argument("--log", default="./import_log.json")
    ap.add_argument("--dry-run", action="store_true", help="list what would be created; makes no API calls")
    ap.add_argument("--project", action="append", default=[], help="only import this original project ID (repeatable)")
    ap.add_argument("--limit", type=int, help="only import the first N projects")
    args = ap.parse_args()

    items = load_archive(args.archive, set(args.project), args.limit)
    ilog = ImportLog(args.log)
    if args.dry_run:
        dry_run(items, ilog)
        return

    api = Client("CC_NEW_TOKEN")
    failures = []
    for item in items:
        p = item["project"]
        log(f"Project {p['id']} '{p.get('name')}'")
        try:
            failures.extend(import_project(api, item, ilog))
        except Exception as e:
            failures.append(f"project {p['id']}: {e}")
            log(f"  FAILED project {p['id']}: {e}")

    log(f"Done. Log: {args.log}")
    if failures:
        log(f"{len(failures)} failures (re-run to retry):")
        for f in failures:
            log(f"  {f}")
        sys.exit(1)


if __name__ == "__main__":
    main()
