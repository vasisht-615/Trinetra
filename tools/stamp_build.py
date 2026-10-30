"""Re-stamp the site after editing files in site/engine: checksums in engine/files.json, the engine version, and one build
id in sw.js (the offline cache name) and app.js (BUILD), so installed apps fetch the new files instead of the cached ones.

    python3 tools/stamp_build.py
"""
import hashlib
import json
import os
import re

SITE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "site")


def main():
    os.chdir(SITE)
    fj = "engine/files.json"
    d = json.load(open(fj))
    for f in d["files"]:
        b = open(os.path.join("engine", f["path"]), "rb").read()
        f["sha256"], f["bytes"] = hashlib.sha256(b).hexdigest(), len(b)
    ver = re.search(r'^VERSION = "([^"]+)"', open("engine/scripts/desk.py").read(), re.M).group(1)
    d["engine"] = ver
    with open(fj, "w") as fh:
        fh.write(json.dumps(d, indent=1))
    # one digest over every shipped file except the two that carry the id (app.js without its BUILD line)
    h = hashlib.sha256()
    for root, dirs, files in os.walk("."):
        dirs.sort()
        for name in sorted(files):
            p = os.path.join(root, name)[2:]
            if p in ("sw.js", "app.js") or "__pycache__" in p:
                continue
            h.update(p.encode() + b"\0" + hashlib.sha256(open(p, "rb").read()).digest())
    app = open("app.js").read()
    h.update(re.sub(r'^const BUILD = "[^"]*";', "", app, count=1, flags=re.M).encode())
    build = ver + "-" + h.hexdigest()[:12]
    sw = open("sw.js").read()
    with open("sw.js", "w") as fh:
        fh.write(re.sub(r'^const CACHE = "desk-[^"]*";', 'const CACHE = "desk-%s";' % build, sw, count=1, flags=re.M))
    with open("app.js", "w") as fh:
        fh.write(re.sub(r'^const BUILD = "[^"]*";', 'const BUILD = "%s";' % build, app, count=1, flags=re.M))
    print("engine %s, build %s" % (ver, build))


if __name__ == "__main__":
    main()
