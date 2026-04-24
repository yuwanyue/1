#!/usr/bin/env python3
import argparse
import json
from pathlib import Path


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
    content = template_path.read_text(encoding="utf-8")
    rendered = (
        content.replace("__START_URL__", args.start_url.replace("\\", "\\\\").replace('"', '\\"'))
        .replace("__SEARCH_TERM__", args.search_term.replace("\\", "\\\\").replace('"', '\\"'))
    )

    if args.json:
        print(json.dumps(rendered, ensure_ascii=False))
    else:
        print(rendered)


if __name__ == "__main__":
    main()
