# Manual Crop → Draw.io

A desktop tool for reconstructing complex figures (architecture diagrams, paper figures, etc.) into **editable [draw.io](https://www.diagrams.net/) files**.

You manually crop regions from a source image, then export them as image layers and/or OCR-based editable text cells.

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

## Features

- **Three crop modes** (pick after drawing a box; shortcuts `1` / `2` / `3`):
  1. **Image** — save PNG and punch out the region
  2. **White-background text** — Baidu OCR → review → editable draw.io text
  3. **Colored-background text** — OCR + auto neighbor fill / eyedropper
- Adjustable selection (handles, arrow keys) before confirming mode
- Floating mode menu next to the cursor after drawing
- Pan: **right-drag**, middle mouse, or Space + left-drag; wheel zoom
- OCR profiles: **Standard** (`general_basic`, default) and **Accurate** (`accurate_basic`)
- On the OCR review dialog (Standard profile): re-run with Accurate OCR; bold / italic / underline
- Auto-naming for crops; Times New Roman text export with color estimation and box-fit font size
- Export layer order: earlier crops appear **above** later ones
- Multi-select delete in the crop list
- **One-click export & open** in draw.io Desktop
- Open the crops folder for manual edits
- **Image replace processor**: drag / paste a new image, scale with aspect ratio (contain + pad), overwrite a target crop PNG

## Requirements

- Python 3.10+
- [PySide6](https://pypi.org/project/PySide6/), [Pillow](https://pypi.org/project/Pillow/)
- Optional: [Baidu AI Cloud OCR](https://cloud.baidu.com/product/ocr) API keys (text modes only)
- Optional: [draw.io Desktop](https://github.com/jgraph/drawio-desktop/releases) for “export and open”

## Install

```bash
git clone https://github.com/<YOUR_USERNAME>/manual-crop-to-drawio.git
cd manual-crop-to-drawio
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
# source .venv/bin/activate

pip install -r requirements.txt
```

## Configure OCR (optional)

Text modes need Baidu OCR credentials. **Do not commit real keys.**

1. Copy the example file:

   ```bash
   # Windows
   copy secrets.json.example secrets.json
   # macOS / Linux
   cp secrets.json.example secrets.json
   ```

2. Edit `secrets.json` and fill in your keys:

   | Field | Baidu product |
   | --- | --- |
   | `baidu_api_key` / `baidu_secret_key` | Accurate OCR (`accurate_basic`) |
   | `baidu_standard_api_key` / `baidu_standard_secret_key` | General OCR (`general_basic`) |
   | `ocr_profile` | `"standard"` (default) or `"accurate"` |

3. `secrets.json` is listed in `.gitignore`. Never push it to GitHub.

Image-only workflows work **without** OCR keys.

## Run

```bash
python crop_to_drawio.py
# or open a figure directly
python crop_to_drawio.py path/to/figure.png
```

Windows: double-click `run.bat`, or:

```bat
run.bat path\to\figure.png
```

## Typical workflow

1. Open a source figure.
2. Draw a box → choose mode `1` / `2` / `3` (or click the floating menu).
3. Prefer cutting **inner** elements before outer frames (punch-out helps).
4. For text: review OCR, optionally bold, optionally re-run Accurate OCR.
5. Use **Image replace processor** if you need a higher-quality icon of a different pixel size.
6. **Export and open** (or Export Draw.io).

### Output layout

Next to the source image (by default):

```text
<figure_stem>_manual_crops/
  crops/           # PNG crops
  icons.json       # manifest
  <stem>_manual.drawio
```

## draw.io Desktop path (optional)

The “export and open” action looks for draw.io in this order:

1. Environment variable `DRAWIO_PATH` (full path to `draw.io.exe` / binary)
2. Common install locations
3. OS file association (`os.startfile` / `open` / `xdg-open`)

```bat
set DRAWIO_PATH=C:\Path\to\draw.io.exe
```

## Security notes

- **Never commit `secrets.json`.**
- Rotate any keys that were previously shared in chat, logs, or screenshots.
- This repository ships only `secrets.json.example` with placeholders.

## Project layout

```text
manual-crop-to-drawio/
  crop_to_drawio.py      # PySide6 UI + export
  baidu_ocr.py           # Baidu OCR client (reads secrets.json)
  secrets.json.example   # placeholder credentials
  requirements.txt
  run.bat / run.sh
  LICENSE                # MIT
  README.md
```

## License

[MIT](LICENSE)

## Acknowledgements

- [draw.io / diagrams.net](https://www.diagrams.net/)
- Baidu AI Cloud OCR API (optional dependency for text modes)
