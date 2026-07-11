"""G1: the embedded panel frontend (~1,600 lines of HTML+JS in gui/server.py)
had no parse gate — only two brittle substring assertions — so a real JavaScript
syntax error would ship silently and only surface as a blank panel in the
browser. Extract every <script> block and run it through node's parser
(node --check), so a syntax error fails the suite instead."""
import os
import re
import shutil
import subprocess
import tempfile

import pytest

NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node not installed — JS syntax gate skipped")
def test_embedded_frontend_js_parses():
    from gui import server
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", server.HTML, re.S)
    assert scripts, "no <script> block found in the embedded panel HTML"
    for i, js in enumerate(scripts):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(js)
            path = f.name
        try:
            r = subprocess.run([NODE, "--check", path], capture_output=True, text=True)
        finally:
            os.unlink(path)
        assert r.returncode == 0, (
            f"embedded <script> block {i} has a JS syntax error:\n{r.stderr}")
