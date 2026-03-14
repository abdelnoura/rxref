# RxRef — Resident Clinical Reference

Guideline-based bedside management with a live new-evidence overlay.

**Live at:** *(your Netlify URL goes here after deploy)*

## What it does
- Disease pages with full guideline-based management
- **Delta View** — compares current guidelines to newest evidence
- Interactive severity calculators (CURB-65, SOFA, etc.)
- Patient profile → study applicability matching
- Auto-updating evidence pipeline (runs every Sunday via GitHub Actions)

## File structure
```
rxref/
├── index.html                        ← Full app
├── data/
│   └── evidence.json                 ← Auto-updated weekly
├── scripts/
│   └── update_evidence.py            ← PubMed → Claude pipeline
├── .github/
│   └── workflows/
│       └── weekly_update.yml         ← Sunday 2am auto-run
└── netlify.toml                      ← Netlify deploy config
```

## Setup (one-time)

### 1. Add Anthropic API key
GitHub repo → Settings → Secrets and variables → Actions → New secret
- Name: `ANTHROPIC_API_KEY`
- Value: your key from console.anthropic.com

### 2. Deploy on Netlify
netlify.com → Add new site → Import from GitHub → select this repo → Deploy

### 3. Test the pipeline
GitHub → Actions tab → Weekly Evidence Update → Run workflow

After that it runs automatically every Sunday. Done.

## Cost
- GitHub, Netlify, PubMed API: **free**
- Anthropic API: **~$1–3/week**
