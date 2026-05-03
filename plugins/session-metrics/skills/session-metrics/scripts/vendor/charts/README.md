# Vendored chart libraries

Files in this tree are checked into the repo so the HTML export works
fully offline (no CDN round-trip, no runtime `~/.cache/` writes).
[`manifest.json`](manifest.json) lists each file's expected SHA-256;
`session-metrics.py` verifies the hash before inlining the JS (and CSS,
for libraries that need it).

## Layout

```
vendor/charts/
  manifest.json            — version, SHA-256, license per library
  highcharts/v12/          — non-commercial license (see LICENSE.txt)
    highcharts.js
    highcharts-3d.js
    exporting.js
    export-data.js
  uplot/v1/                — MIT (see LICENSE.txt)
    uPlot.iife.min.js
    uPlot.min.css
  chartjs/v4/              — MIT (see LICENSE.txt)
    chart.umd.js
```

## Refreshing the vendored files

```bash
cd scripts/vendor/charts/highcharts/v12
for f in highcharts.js highcharts-3d.js; do
  curl -fsSL -o "$f" "https://cdn.jsdelivr.net/npm/highcharts@12/$f"
done
for f in exporting.js export-data.js; do
  curl -fsSL -o "$f" "https://cdn.jsdelivr.net/npm/highcharts@12/modules/$f"
done
shasum -a 256 *.js   # update manifest.json with the new digests

cd ../../uplot/v1
curl -fsSL -o uPlot.iife.min.js https://cdn.jsdelivr.net/npm/uplot@1/dist/uPlot.iife.min.js
curl -fsSL -o uPlot.min.css     https://cdn.jsdelivr.net/npm/uplot@1/dist/uPlot.min.css
shasum -a 256 *.js *.css

cd ../../chartjs/v4
curl -fsSL -o chart.umd.js https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.js
shasum -a 256 *.js
```

Bump the version directory (`v12` → `v13`, etc.) if the major release
changes; the script auto-discovers via the manifest.

## Licenses

| Library    | License                | Notes                                                                 |
|------------|------------------------|-----------------------------------------------------------------------|
| Highcharts | non-commercial-free    | Commercial use needs a paid Highsoft AS license. See LICENSE.txt.     |
| uPlot      | MIT                    | [github.com/leeoniya/uPlot](https://github.com/leeoniya/uPlot/blob/master/LICENSE) |
| Chart.js   | MIT                    | [github.com/chartjs/Chart.js](https://github.com/chartjs/Chart.js/blob/master/LICENSE.md) |

Pick the renderer with `--chart-lib {highcharts|uplot|chartjs|none}`.
Default is `highcharts` (richest visualization, 3D sliders). Use
`uplot` or `chartjs` for a lighter, MIT-licensed output; `none` for
a no-JS detail page.

## Upgrade procedure

Vendored bundles are immutable on disk — every byte is hashed before
inlining. Any drift between the file and the manifest entry triggers
`VendorChartVerificationError` (fail-closed) inside
[`_charts.py`](../../_charts.py) at the call site
`_read_vendor_files(library, suffix)`. The check runs on every HTML
render; there is no skip path other than the explicit
`--allow-unverified-charts` operator override (which only degrades the
failure to a stderr warning — the file still ships unverified, so the
override is for emergency recovery, not routine bumps).

### When to bump

| Trigger | Action |
|---------|--------|
| Upstream patch (e.g. `12.1.0` → `12.1.1`) | Refresh in place under the same `vN/` directory. Update the SHA-256s in `manifest.json`. No `version` field change. |
| Upstream minor (e.g. `12.1` → `12.2`) | Same as patch — `vN/` directories track major only. |
| Upstream major (e.g. `12.x` → `13.x`) | Create a new `highcharts/v13/` directory, populate, regen hashes, update `manifest.json` `version` field and each `path` entry. Keep the old `vN/` until the next release for fall-back; delete on the version bump after that. |
| Upstream licence change | Read carefully (see Licence-renewal awareness below). May force a re-vendor decision. |
| CVE / security advisory | Patch immediately even if mid-cycle. |

For Highcharts, follow the upstream changelog at
<https://www.highcharts.com/blog/changelog/>. uPlot and Chart.js track
their GitHub releases — both ship MIT-licensed and have no
licence-renewal concern.

### Step-by-step bump

1. **Fetch.** Use the curl block in *Refreshing the vendored files*
   above. Always pin the major-version path segment (`@12`, `@1`, `@4`)
   so transitive updates land predictably.
2. **Regen the SHA-256s.** Run `shasum -a 256` on each file you replaced
   and paste the new hex digest into the matching `files[].sha256` field
   in `manifest.json`. The manifest is the *only* place hashes live —
   no secondary lockfile to update.
3. **Verify locally.** From the repo root:

   ```bash
   uv run python -c "
   import sys; sys.path.insert(0, '.claude/skills/session-metrics/scripts')
   import importlib.util
   spec = importlib.util.spec_from_file_location('sm', '.claude/skills/session-metrics/scripts/session-metrics.py')
   sm = importlib.util.module_from_spec(spec); sys.modules['sm'] = sm; spec.loader.exec_module(sm)
   from _charts import _read_vendor_files
   for lib in ('highcharts','uplot','chartjs'):
       _ = _read_vendor_files(lib, '.js')   # raises if any hash mismatches
       print(f'{lib}: OK')
   "
   ```

   On mismatch you'll see `VendorChartVerificationError: SHA-256
   mismatch for <file>: expected …, got …` — paste the *got* digest
   into the manifest, not the *expected* (the *got* is what's actually
   on disk).
4. **Run the test suite.** `uv run python -m pytest tests/` — the
   `T1.3` chart-vendor tests in `test_session_metrics.py` (search for
   `vendor_charts`) re-verify every library through `_read_vendor_files`
   and catch a stale-manifest commit. Browser tests (`pytest tests/browser/
   --browser=chromium` after `playwright install chromium` and
   `SESSION_METRICS_RUN_BROWSER_TESTS=1`) are recommended for major
   bumps to catch DOM-API drift that hash verification can't see.
5. **Bump `_SKILL_VERSION`** per the *Version strings* table in
   [`CLAUDE.md`](../../../../../CLAUDE.md). A vendor file change is a
   skill-payload byte change, so the same patch-vs-minor reasoning
   applies as for any other code edit.

### Licence-renewal awareness

Highcharts ships under a **non-commercial-free** licence (the
"Highcharts Non-Commercial License"). Two operational consequences:

- **Personal / educational / open-source non-commercial use is
  permitted** under the upstream terms, and that is the use case the
  default install targets. No renewal action is required for these
  users.
- **Commercial use requires a paid Highsoft AS licence** — if the
  downstream operator ships session-metrics output as part of a
  commercial product, *they* are responsible for procuring that
  licence. Re-distribution of the vendored bundle does not transfer
  any licence; the `LICENSE.txt` file inside `highcharts/v12/`
  preserves Highsoft's notice intact.
- **Watch the upstream licence text on each major bump.** Highsoft has
  historically tightened the non-commercial wording. The bump procedure
  above includes a "read the licence" implicit step — if upstream
  changes from non-commercial-free to anything more restrictive (e.g.
  evaluation-only), the right response is to drop Highcharts as the
  default and document the migration in the dev-repo README. The MIT
  alternatives (`--chart-lib uplot|chartjs`) exist precisely as the
  fall-back path.

uPlot and Chart.js are MIT — no renewal, no commercial-use carve-out,
no upstream-text watch is required for them.
