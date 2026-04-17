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
