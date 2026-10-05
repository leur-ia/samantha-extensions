#!/usr/bin/env python3
"""The registry's website, built from index/*.toml: one card per extension with what
it does, its ready-made setups (recipes), its tools and its sandbox, read from the
manifest at the newest release's pinned commit, the one Samantha installs.

    python3 scripts/site.py [out_dir]     # default _site; needs git and full history

Fails (exit 1) when an index file's `recipes` differ from its manifest's [[recipe]]
titles, or when a manifest can't be read: the site never shows what an install wouldn't.
"""

import html
import json
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def version_key(v):
    return [int(p) if p.isdigit() else 0 for p in v.split(".")]


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout


def manifest(release):
    """extension.toml at the release's pinned commit, fetched when it isn't here."""
    commit, path = release["commit"], release.get("path", "").strip("/")
    try:
        git("cat-file", "-e", f"{commit}^{{commit}}")
    except subprocess.CalledProcessError:
        git("fetch", "-q", "--depth", "1", release["source"].split("#")[0], commit)
    name = f"{path}/extension.toml" if path else "extension.toml"
    return tomllib.loads(git("show", f"{commit}:{name}"))


def card(id_, index, m, release):
    e = html.escape
    kind = "front" if m.get("capability") else ""
    provides = sorted({p["capability"] for p in m.get("provides", [])})
    if provides:
        kind = "provider: " + ", ".join(provides)
    needs = m.get("needs", {})
    sandbox = [w for w, on in [("network", needs.get("network")), ("session bus", needs.get("dbus_session")),
                               ("system bus", needs.get("dbus_system"))] if on]
    sandbox += [f"reads {p}" for p in needs.get("read", [])] + [f"writes {p}" for p in needs.get("write", [])]
    sandbox += [f"secret {s}" for s in needs.get("secrets", [])]
    sandbox += [f"{o['provider']} sign-in" for o in m.get("oauth", [])]
    tools = m.get("tools", {})
    by_class = {}
    for name, policy in sorted(tools.items()):
        by_class.setdefault(policy.get("class", "confirm"), []).append(name.split(".", 1)[-1])
    recipes = "".join(
        f"<li><b>{e(r['title'])}</b> — {e(r['description'])}</li>" for r in m.get("recipe", [])
    )
    tool_rows = "".join(
        f"<li><span class='class {e(c)}'>{e(c)}</span> {e(', '.join(names))}</li>"
        for c, names in sorted(by_class.items())
    )
    words = " ".join([id_, m.get("name", ""), index["description"], kind,
                      *(r["title"] for r in m.get("recipe", []))]).lower()
    return f"""<article data-search="{e(words)}">
  <header><h2>{e(m.get('name', id_))}</h2><span class="kind">{e(kind)}</span><span class="version">{e(release['version'])}</span></header>
  <p>{e(index['description'])}</p>
  {f'<h3>Ready-made</h3><ul class="recipes">{recipes}</ul>' if recipes else ''}
  {f'<details><summary>{len(tools)} tools</summary><ul class="tools">{tool_rows}</ul></details>' if tools else ''}
  <p class="sandbox">{e(' · '.join(sandbox) or 'no network, no home access')}</p>
  <code>samantha extension install {e(id_)}</code>
</article>"""


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Samantha extensions</title>
<style>
:root {{ --bg: #fbfaf7; --card: #fff; --text: #1d1c1a; --muted: #6b6760; --line: #e6e2da; --accent: #8a4b2a; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg: #161514; --card: #1f1e1c; --text: #ece9e3; --muted: #a29d94; --line: #34312d; --accent: #e0a07c; }} }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--text); font: 15px/1.5 system-ui, sans-serif; }}
main {{ max-width: 1100px; margin: 0 auto; padding: 32px 16px; }}
h1 {{ margin: 0 0 4px; font-size: 28px; }}
.lede {{ color: var(--muted); margin: 0 0 20px; }}
input {{ width: 100%; padding: 10px 12px; font: inherit; color: var(--text); background: var(--card); border: 1px solid var(--line); border-radius: 8px; margin-bottom: 20px; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 16px; }}
article {{ background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 16px; display: flex; flex-direction: column; gap: 8px; }}
article header {{ display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }}
h2 {{ margin: 0; font-size: 18px; }} h3 {{ margin: 4px 0 0; font-size: 13px; text-transform: uppercase; letter-spacing: .04em; color: var(--accent); }}
p {{ margin: 0; }} ul {{ margin: 0; padding-left: 18px; }}
.kind, .version, .sandbox, summary {{ color: var(--muted); font-size: 13px; }}
.version {{ margin-left: auto; }}
.class {{ font-size: 12px; padding: 0 6px; border-radius: 4px; border: 1px solid var(--line); }}
.class.confirm {{ color: var(--accent); }}
code {{ font-size: 13px; background: var(--bg); border: 1px solid var(--line); border-radius: 6px; padding: 4px 8px; overflow-wrap: anywhere; margin-top: auto; }}
</style></head>
<body><main>
<h1>Samantha extensions</h1>
<p class="lede">{count} extensions for <a href="https://gitlab.com/jeanbaptiste2/samantha">Samantha</a>. Each install fetches the pinned commit shown here, then asks you after showing its tools and sandbox. Or just ask Samantha: “installe l'extension spotify”.</p>
<input type="search" placeholder="Search: mail, music, calendar, every morning…" aria-label="Search extensions" oninput="filter(this.value)">
<div class="grid">
{cards}
</div>
</main>
<script>
function filter(q) {{
  const words = q.toLowerCase().split(/\\s+/).filter(Boolean);
  for (const a of document.querySelectorAll("article"))
    a.hidden = !words.every(w => a.dataset.search.includes(w));
}}
</script>
</body></html>
"""


def main():
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "_site")
    cards, catalog, errors = [], [], []
    for file in sorted((ROOT / "index").glob("*.toml")):
        id_ = file.stem
        try:
            index = tomllib.loads(file.read_text())
            release = max(index["release"], key=lambda r: version_key(r["version"]))
            m = manifest(release)
        except Exception as err:  # one broken entry is reported, the others still build
            errors.append(f"{file.name}: {err}")
            continue
        titles = [r["title"] for r in m.get("recipe", [])]
        if index.get("recipes", []) != titles:
            errors.append(f"{file.name}: recipes = {json.dumps(titles, ensure_ascii=False)} in the manifest at "
                          f"{release['commit'][:12]}, {json.dumps(index.get('recipes', []), ensure_ascii=False)} here")
        cards.append(card(id_, index, m, release))
        catalog.append({"id": id_, "description": index["description"], "version": release["version"],
                        "recipes": titles})
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(PAGE.format(count=len(cards), cards="\n".join(cards)))
    (out / "index.json").write_text(json.dumps(catalog, ensure_ascii=False, indent=1))
    for err in errors:
        print(f"error: {err}", file=sys.stderr)
    print(f"{len(cards)} extensions → {out}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
