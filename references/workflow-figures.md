# Figure Extraction & Agent-as-Engine Enrichment

This reference covers Workflow H, which is **MANDATORY** when automated LLM providers are unavailable (i.e., `direct-inference` mode). 

## Workflow H: Agent-as-Engine Enrichment (Complete Fallback Procedure)

When `enrich_wiki.py` writes `.prompt_<slug>.md` files, the Agent must step in.
**⚠️ FIGURES ARE NON-NEGOTIABLE during enrichment.** Text + figures must run in parallel in a single enrichment pass.

### Step-by-step

1. **Identify stubs**: Run `enrich_wiki.py --only-sources`. Read the generated prompt:
   `cat wiki/wiki/sources/.prompt_<slug>.md`

2. **Extract PDF text AND figures in parallel**:
   ```bash
   # --- Text (background) ---
   # Option A: pdftotext
   pdftotext pdfs/<id>.pdf pdfs/<id>.txt
   # Option B: pypdf
   pip install --user --break-system-packages pypdf

   # --- Figures (parallel, NOT optional) ---
   # Method 1 (preferred): arXiv source .tar.gz
   wget -O /tmp/<id>.tar.gz "https://arxiv.org/src/<id>"
   mkdir -p /tmp/<id>-src && tar xzf /tmp/<id>.tar.gz -C /tmp/<id>-src/
   # → See Figure Extraction steps below for full details
   ```

3. **Write enriched content + insert figures to source page**: Fill in all stub sections following the prompt's format requirements.
   ★ **Insert figures at correct positions** (at minimum: architecture diagram below method section, key result figure below experiments section). Image paths: `../../assets/<paper_name>/<figure>.<ext>`.

4. **Clean up and Verify**: 
   - `rm wiki/wiki/sources/.prompt_<slug>.md`
   - `grep "待补充\|待消化" wiki/wiki/sources/<slug>.md`
   - `grep -c "!\[.*\](.*assets/.*)" wiki/wiki/sources/<slug>.md`
   - `git add -A && git commit -m "auto-wiki-archive: [Enrich] <slug>"`

---

## Paper Figure Extraction & Insertion

Extract figures from an academic paper PDF and insert them into wiki source pages.

| Priority | Method | Quality | When |
|----------|--------|---------|------|
| **1** | arXiv source `.tar.gz` | ⭐⭐⭐ Original vectors | arXiv download succeeds |
| **2** | PDF page render + crop | ⭐⭐ High-res raster | arXiv download fails |

### Step 1 — Try arXiv source (Method 1)

```bash
wget -O /tmp/<arxiv_id>.tar.gz "https://arxiv.org/src/<arxiv_id>"
mkdir -p /tmp/<arxiv_id>-src && tar xzf /tmp/<arxiv_id>.tar.gz -C /tmp/<arxiv_id>-src/
find /tmp/<arxiv_id>-src -type f \( -name "*.pdf" -o -name "*.png" \) | sort
```
Source structure: `images/*.pdf` (vector figures), `figures/*.tex`. If download stalls, fall through to Method 2.

### Step 2 — PDF page render + crop (Method 2)

**2a. Render all pages at 300 DPI:**
```python
import fitz, os
doc = fitz.open("<wiki_root>/pdfs/<arxiv_id>.pdf")
out = "<wiki_root>/assets/<paper_name>/"
os.makedirs(out, exist_ok=True)
for i in range(doc.page_count):
    doc[i].get_pixmap(matrix=fitz.Matrix(300/72,300/72)).save(f"{out}/page_{i+1:02d}.png")
```

**2b. Find figure caption positions (caption is BELOW figure):**
```python
for pn in [5, 10, 26]:  # target pages
    page = doc[pn - 1]
    for b in page.get_text("dict")["blocks"]:
        if b["type"] != 0: continue
        text = "".join(s["text"] for l in b.get("lines", []) for s in l.get("spans", []))
        if "Figure" in text and len(text) > 20:
            print(f"Page {pn}: y0={b['bbox'][1]:.0f} '{text[:100]}'"); break
```

**2c. Crop from page top (y=90pt, below header) to just above caption (y0-5pt):**
```python
from PIL import Image
S = 300/72  # pts→px
crops = {5: ("fig1", 70,90,525,350), 10: ("fig2", 70,90,525,320)}
for pn, (name, l,t,r,b) in crops.items():
    img = Image.open(f"{out}/page_{pn:02d}.png")
    img.crop((int(l*S),int(t*S),int(r*S),int((b-5)*S))).save(f"{out}/{name}.png")
# Clean up: rm page_*.png
```

### Step 3 — Insert into wiki source

Images live in `assets/<paper_name>/`. Source pages in `wiki/wiki/sources/`.
Relative path: `../../assets/<paper_name>/<figure>.png`.

```markdown
![Figure X: description](../../assets/<paper_name>/fig2_architecture.png)
*Figure X: Caption from paper.*
```