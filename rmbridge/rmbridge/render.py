"""Obsidian Markdown note -> PDF laid out for the reMarkable screen.

PDFs keep their layout on the tablet (an EPUB gets a title page and the
tablet's own huge reflow), and ink on them comes back as a normal PDF.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

from markdown_it import MarkdownIt
from mdit_py_plugins.footnote import footnote_plugin
from mdit_py_plugins.front_matter import front_matter_plugin
from mdit_py_plugins.tasklists import tasklists_plugin

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"}

# 4:3 like the screen; at this size the text is about book-sized on an rM2 and
# a bit bigger on a Paper Pro.
CSS = """
@page { size: 157mm 210mm; margin: 11mm 12mm 12mm; }
html { font-family: "DejaVu Serif", "Noto Serif", serif; font-size: 11pt; line-height: 1.4; }
h1, h2, h3, h4, h5, h6 { font-family: "DejaVu Sans", "Noto Sans", sans-serif; line-height: 1.2;
  margin: 1.1em 0 0.4em; break-after: avoid; }
h1 { font-size: 1.6em; margin-top: 0; }
h2 { font-size: 1.3em; }
h3 { font-size: 1.1em; }
p, ul, ol, blockquote, pre, table { margin: 0 0 0.7em; }
ul, ol { padding-left: 1.4em; }
li > p { margin: 0; }
li.task-list-item { list-style: none; margin-left: -1.2em; }
blockquote { margin-left: 0; padding-left: 0.8em; border-left: 2px solid #999; color: #333; }
code, pre { font-family: "DejaVu Sans Mono", monospace; font-size: 0.85em; }
pre { white-space: pre-wrap; background: #eee; padding: 0.5em 0.6em; }
table { border-collapse: collapse; }
th, td { border: 1px solid #999; padding: 0.2em 0.5em; }
img { max-width: 100%; }
hr { border: 0; border-top: 1px solid #999; }
mark { background: #ddd; }
"""

_md = (
    MarkdownIt("commonmark", {"html": True, "linkify": False, "typographer": False})
    .enable(["table", "strikethrough"])
    .use(front_matter_plugin)
    .use(footnote_plugin)
    .use(tasklists_plugin)
)

_EMBED = re.compile(r"!\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]*))?\]\]")
_WIKILINK = re.compile(r"\[\[([^\]|#]*)(?:#([^\]|]*))?(?:\|([^\]]*))?\]\]")
_HIGHLIGHT = re.compile(r"==(?=\S)(.+?)(?<=\S)==")
_CALLOUT = re.compile(r"^(\s*>\s*)\[!([\w-]+)\][+-]?\s*", re.MULTILINE)
_CHECKBOX = re.compile(r'<input class="task-list-item-checkbox"( checked="checked")?[^>]*>')
_FENCE = re.compile(r"^(```|~~~).*?^\1", re.MULTILINE | re.DOTALL)


def _obsidian_to_commonmark(text: str, note: Path, vault_dir: Path) -> str:
    def embed(m: re.Match) -> str:
        name = m.group(1).strip()
        if Path(name).suffix.lower() not in IMAGE_EXT:
            return f"*{name}*"  # embedded note/PDF: can't inline it
        found = _find(name, note, vault_dir)
        if found is None:
            return f"*{name}*"
        width = m.group(2) or ""
        size = f' width="{int(width.split("x")[0])}"' if width.split("x")[0].isdigit() else ""
        return f'<img src="{html.escape(found.as_uri())}" alt="{html.escape(name)}"{size}>'

    def link(m: re.Match) -> str:
        target, heading, alias = m.groups()
        return alias or " › ".join(p for p in (target, heading) if p)

    def convert(chunk: str) -> str:
        chunk = _EMBED.sub(embed, chunk)
        chunk = _WIKILINK.sub(link, chunk)
        chunk = _HIGHLIGHT.sub(r"<mark>\1</mark>", chunk)
        return _CALLOUT.sub(lambda m: f"{m.group(1)}**{m.group(2).capitalize()}:** ", chunk)

    # Leave fenced code alone.
    out, pos = [], 0
    for m in _FENCE.finditer(text):
        out += [convert(text[pos : m.start()]), m.group(0)]
        pos = m.end()
    out.append(convert(text[pos:]))
    return "".join(out)


def _find(name: str, note: Path, vault_dir: Path) -> Path | None:
    for candidate in (note.parent / name, vault_dir / name):
        if candidate.is_file():
            return candidate.resolve()
    return next((p.resolve() for p in vault_dir.rglob(Path(name).name) if p.is_file()), None)


def note_html(src: Path, vault_dir: Path) -> str:
    body = _md.render(_obsidian_to_commonmark(src.read_text(errors="replace"), src, vault_dir))
    # WeasyPrint draws <input> as a block; a glyph keeps the task on one line.
    body = _CHECKBOX.sub(lambda m: "☑" if m.group(1) else "☐", body)
    if not body.lstrip().startswith("<h1"):
        # Obsidian shows the file name as the title; do the same.
        body = f"<h1>{html.escape(src.stem)}</h1>\n{body}"
    return (
        f'<!doctype html><html><head><meta charset="utf-8"><title>{html.escape(src.stem)}</title>'
        f"<style>{CSS}</style></head><body>{body}</body></html>"
    )


def markdown_to_pdf(src: Path, out: Path, vault_dir: Path) -> Path:
    import weasyprint  # slow import; only needed when a note changes
    from urllib.parse import unquote, urlsplit

    root = vault_dir.resolve()

    class VaultOnly(weasyprint.URLFetcher):
        """Images from the vault only; never touch the network or the rest of the disk."""

        def fetch(self, url, headers=None):
            parts = urlsplit(url)
            if parts.scheme == "file" and not Path(unquote(parts.path)).resolve().is_relative_to(root):
                raise ValueError(f"not loading {url}")
            return super().fetch(url, headers)

    weasyprint.HTML(
        string=note_html(src, vault_dir),
        base_url=str(src.parent) + "/",
        url_fetcher=VaultOnly(allowed_protocols={"file", "data"}),
    ).write_pdf(out)
    return out
