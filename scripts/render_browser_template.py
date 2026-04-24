#!/usr/bin/env python3
import argparse
import json
from pathlib import Path


def render_browser_template(template_path: Path, start_url: str, search_term: str) -> str:
    content = template_path.read_text(encoding="utf-8")
    return (
        content.replace("__START_URL_JSON__", json.dumps(start_url, ensure_ascii=False))
        .replace("__SEARCH_TERM_JSON__", json.dumps(search_term, ensure_ascii=False))
    )


def main():
    parser = argparse.ArgumentParser(description="Render a reusable browser_script template.")
    parser.add_argument(
        "--template",
        default="templates/browser-human-search.js.tmpl",
        help="Path to the template file.",
    )
    parser.add_argument("--start-url", required=True, help="Initial page URL to open.")
    parser.add_argument("--search-term", required=True, help="Search term to type.")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print as a JSON-escaped string for embedding into command args.",
    )
    args = parser.parse_args()

    root = Path.cwd()
    template_path = (root / args.template).resolve()
    rendered = render_browser_template(template_path, args.start_url, args.search_term)

    if args.json:
        print(json.dumps(rendered, ensure_ascii=False))
    else:
        print(rendered)


if __name__ == "__main__":
    main()
