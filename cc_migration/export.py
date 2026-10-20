#!/usr/bin/env python3
"""Export every project (active and archived) from the source CompanyCam account.

Usage:
    CC_OLD_TOKEN=... python3 export.py              # everything
    CC_OLD_TOKEN=... python3 export.py --limit 2    # first 2 projects only (test run)
    CC_OLD_TOKEN=... python3 export.py --project 123 --project 456
    CC_OLD_TOKEN=... python3 export.py --only projects_to_migrate.txt   # IDs or exact names, one per line
    CC_OLD_TOKEN=... python3 export.py --mine --list                    # preview projects assigned to/created by me

Output layout (./cc_archive by default):
    manifest.json
    <project-id>_<project-name>/project.json
                               /comments.json
                               /photos.json
                               /photos/<photo-id>.<ext>

Re-running is safe: existing photo files are skipped, and comments/tags are only fetched for
photos not already recorded in photos.json.
"""
import argparse
import glob
import mimetypes
import os
import re
import sys
from urllib.parse import urlparse

from cc_api import ApiError, Client, log, read_json, write_json

IMAGE_TYPE_PREFERENCE = ["original", "large", "web"]


def safe_name(name, max_len=80):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name or "untitled").strip().strip(".")
    return re.sub(r"\s+", " ", name)[:max_len] or "untitled"


def project_dir(root, project):
    # Reuse an existing folder for this ID even if the project was renamed since the last run.
    existing = glob.glob(os.path.join(glob.escape(root), f"{project['id']}_*"))
    if existing:
        return existing[0]
    return os.path.join(root, f"{project['id']}_{safe_name(project.get('name'))}")


def list_all_projects(api):
    """Default listing plus archived=true, merged by ID, so archived projects are always included."""
    projects = {}
    for p in api.paginate("/projects"):
        projects[p["id"]] = p
    try:
        for p in api.paginate("/projects", archived="true"):
            projects.setdefault(p["id"], p)
    except ApiError as e:
        log(f"WARNING: could not list archived projects separately ({e}); archived projects may be missing")
    return sorted(projects.values(), key=lambda p: int(p["id"]))


def my_project_ids(api):
    """IDs of projects assigned to, or created by, the user who owns CC_OLD_TOKEN."""
    me = str(api.get("/users/current")["id"])
    ids = set()
    for archived in (None, "true"):
        params = {"assigned_user_ids[]": me, **({"archived": archived} if archived else {})}
        ids.update(str(p["id"]) for p in api.paginate("/projects", **params))
    return me, ids


def read_selection(path, projects):
    """Match each line of path (a project ID or exact project name) to projects. Exits if any line is unresolved."""
    by_id = {str(p["id"]): p for p in projects}
    by_name = {}
    for p in projects:
        by_name.setdefault((p.get("name") or "").strip().casefold(), []).append(p)
    selected, problems = {}, []
    with open(path, encoding="utf-8") as f:
        for line in f:
            entry = line.strip()
            if not entry or entry.startswith("#"):
                continue
            leading_id = re.match(r"(\d+)(\s|#|$)", entry)  # "123456  # optional note" -> 123456
            if leading_id:
                entry = leading_id.group(1)
            if entry in by_id:
                selected[entry] = by_id[entry]
                continue
            matches = by_name.get(entry.casefold(), [])
            if len(matches) == 1:
                selected[str(matches[0]["id"])] = matches[0]
            elif not matches:
                problems.append(f"no project named {entry!r}")
            else:
                problems.append(f"{len(matches)} projects named {entry!r} (IDs {', '.join(str(m['id']) for m in matches)}); "
                                f"list the IDs instead")
    if problems:
        sys.exit("Could not resolve every line of " + path + ":\n  " + "\n  ".join(problems))
    return list(selected.values())


def pick_image_url(photo):
    uris = {u.get("type"): (u.get("url") or u.get("uri")) for u in photo.get("uris") or []}
    for kind in IMAGE_TYPE_PREFERENCE:
        if uris.get(kind):
            return kind, uris[kind]
    if photo.get("photo_url"):
        return "photo_url", photo["photo_url"]
    return None, None


def existing_photo_file(photos_dir, photo_id):
    for path in glob.glob(os.path.join(glob.escape(photos_dir), f"{photo_id}.*")):
        if not path.endswith((".part", ".tmp", ".download")) and os.path.getsize(path) > 0:
            return path
    return None


def extension_for(url, content_type):
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    if ext in (".jpg", ".jpeg", ".png", ".heic", ".webp", ".gif", ".tif", ".tiff"):
        return ext
    guessed = mimetypes.guess_extension((content_type or "").split(";")[0].strip())
    return ".jpg" if guessed in (None, ".jpe") else guessed


def export_photos(api, project, pdir):
    photos_dir = os.path.join(pdir, "photos")
    os.makedirs(photos_dir, exist_ok=True)
    meta_path = os.path.join(pdir, "photos.json")
    records = {str(r["id"]): r for r in read_json(meta_path, [])}
    failures = []

    for photo in api.paginate(f"/projects/{project['id']}/photos"):
        pid = str(photo["id"])
        record = records.get(pid)
        try:
            if record is None:
                description = photo.get("description") or {}
                record = {
                    "id": pid,
                    "captured_at": photo.get("captured_at"),
                    "created_at": photo.get("created_at"),
                    "creator_id": photo.get("creator_id"),
                    "creator_name": photo.get("creator_name"),
                    "description": description.get("plain_text_content") or "",
                    "description_html": description.get("html_content") or "",
                    "coordinates": photo.get("coordinates"),
                    "tags": [t.get("display_value") or t.get("value") for t in api.paginate(f"/photos/{pid}/tags")],
                    "comments": [
                        {k: c.get(k) for k in ("id", "creator_id", "creator_name", "content", "created_at")}
                        for c in (api.paginate(f"/photos/{pid}/comments") if photo.get("comment_count") else [])
                    ],
                    "file": None,
                }
                records[pid] = record

            path = existing_photo_file(photos_dir, pid)
            if path is None:
                kind, url = pick_image_url(photo)
                if not url:
                    raise RuntimeError("photo has no downloadable URL")
                tmp_dest = os.path.join(photos_dir, f"{pid}.download")
                content_type = api.download(url, tmp_dest)
                path = os.path.join(photos_dir, pid + extension_for(url, content_type))
                os.replace(tmp_dest, path)
                record["source_image_type"] = kind
            record["file"] = os.path.relpath(path, pdir)
        except Exception as e:  # keep going; a re-run picks up whatever failed
            failures.append(f"project {project['id']} photo {pid}: {e}")
            log(f"  FAILED photo {pid}: {e}")
        write_json(meta_path, sorted(records.values(), key=lambda r: (str(r.get("captured_at")), r["id"])))

    downloaded = sum(1 for r in records.values() if r.get("file"))
    return downloaded, failures


def export_project(api, project, root):
    pdir = project_dir(root, project)
    os.makedirs(pdir, exist_ok=True)
    log(f"Project {project['id']} '{project.get('name')}' "
        f"({project.get('photo_count', '?')} photos{', archived' if project.get('archived') else ''})")

    # Project metadata is already in hand from the listing, so it is cheap to refresh every run.
    write_json(os.path.join(pdir, "project.json"), project)

    comments_path = os.path.join(pdir, "comments.json")
    if not os.path.exists(comments_path):
        comments = list(api.paginate(f"/projects/{project['id']}/comments"))
        write_json(comments_path, sorted(comments, key=lambda c: str(c.get("created_at"))))

    downloaded, failures = export_photos(api, project, pdir)
    log(f"  {downloaded} photo files on disk, {len(failures)} failures")
    return {
        "id": project["id"],
        "name": project.get("name"),
        "folder": os.path.basename(pdir),
        "archived": project.get("archived"),
        "status": project.get("status"),
        "photo_count": project.get("photo_count"),
        "photos_downloaded": downloaded,
    }, failures


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="./cc_archive")
    ap.add_argument("--limit", type=int, help="only export the first N projects (for testing)")
    ap.add_argument("--project", action="append", default=[], help="only export this project ID (repeatable)")
    ap.add_argument("--only", metavar="FILE",
                    help="only export projects listed in FILE (one ID or exact name per line; text after an ID is ignored)")
    ap.add_argument("--mine", action="store_true", help="only export projects assigned to or created by the token's user")
    ap.add_argument("--list", action="store_true", help="print the selected projects and exit without downloading")
    args = ap.parse_args()

    api = Client("CC_OLD_TOKEN")
    os.makedirs(args.out, exist_ok=True)

    projects = list_all_projects(api)
    log(f"Found {len(projects)} projects ({sum(1 for p in projects if p.get('archived'))} archived)")
    selected = projects
    if args.only:
        selected = read_selection(args.only, selected)
    if args.mine:
        me, assigned = my_project_ids(api)
        selected = [p for p in selected if str(p["id"]) in assigned or str(p.get("creator_id")) == me]
    if args.project:
        selected = [p for p in selected if str(p["id"]) in args.project]
    selected.sort(key=lambda p: int(p["id"]))
    if args.limit:
        selected = selected[:args.limit]
    log(f"Selected {len(selected)} projects")

    if args.list:
        for p in selected:
            a = p.get("address") or {}
            print(f"{p['id']}\t{p.get('name')}\t{a.get('street_address_1') or ''}, {a.get('city') or ''}"
                  f"\t{p.get('photo_count', '?')} photos{chr(9) + 'archived' if p.get('archived') else ''}")
        return

    entries, failures = [], []
    for project in selected:
        try:
            entry, errs = export_project(api, project, args.out)
            entries.append(entry)
            failures.extend(errs)
        except Exception as e:
            failures.append(f"project {project['id']}: {e}")
            log(f"  FAILED project {project['id']}: {e}")

    manifest_path = os.path.join(args.out, "manifest.json")
    # Merge with any previous manifest so partial runs (--limit/--project) don't drop earlier entries.
    previous = {str(e["id"]): e for e in (read_json(manifest_path, {}) or {}).get("projects", [])}
    previous.update({str(e["id"]): e for e in entries})
    write_json(manifest_path, {
        "source_project_total": len(projects),
        "selected_project_count": len(selected),
        "exported_project_count": len(previous),
        "total_photos": sum(e.get("photos_downloaded") or 0 for e in previous.values()),
        "projects": sorted(previous.values(), key=lambda e: int(e["id"])),
    })

    log(f"Done: {len(entries)} projects this run, manifest at {manifest_path}")
    if failures:
        log(f"{len(failures)} failures (re-run to retry):")
        for f in failures:
            log(f"  {f}")
        sys.exit(1)


if __name__ == "__main__":
    main()
