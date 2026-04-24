import tempfile
import unittest
from pathlib import Path

from scripts.render_browser_template import render_browser_template


class RenderBrowserTemplateTests(unittest.TestCase):
    def test_render_browser_template_uses_json_string_literals(self):
        template = """await page.goto(__START_URL_JSON__);\nawait page.keyboard.type(__SEARCH_TERM_JSON__);\n"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "template.js.tmpl"
            path.write_text(template, encoding="utf-8")
            rendered = render_browser_template(
                path,
                'https://example.com?q="x"\nnext',
                '周树人 "鲁迅"\n下一行',
            )

        self.assertIn('await page.goto("https://example.com?q=\\"x\\"\\nnext");', rendered)
        self.assertIn('await page.keyboard.type("周树人 \\"鲁迅\\"\\n下一行");', rendered)


if __name__ == "__main__":
    unittest.main()
