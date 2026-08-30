#!/bin/sh
# Build a deterministic fixture and serve it for the render checks.
#
# The fixture is the same one tools/check_viewer.mjs reduces against, so the
# numeric harness and the render harness look at the same trace: a disagreement
# between them is then about drawing, never about data.
set -e
cd "$(dirname "$0")/.."
rm -rf .render-fixture
python3 -m tools.make_viewer_fixture .render-fixture
cp viewer.html .render-fixture/
cd .render-fixture
exec python3 ../serve.py 8123
