"""
RxRef — Auto Evidence Update Pipeline
Runs weekly via GitHub Actions.
Queries PubMed for new high-impact papers, sends abstracts to Claude,
extracts structured evidence, updates data/evidence.json.
"""

import json
import os
import time
import urllib.request
import urllib.parse
from datetime import datetime, timedelta

# ── CONFIG ────────────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Diseases to monitor. Add more as you expand.
DISEASES = [
    {
        "id": "cap",
        "name": "Community-Acquired Pneumonia",
        "pubmed_terms": [
            "community-acquired pneumonia treatment",
            "CAP antibiotic duration",
            "pneumonia corticosteroids",
            "pneumonia procalcitonin",
            "MRSA pneumonia community",
        ],
        "guideline": "IDSA/ATS 2019 CAP Guidelines",
        "key_sections": ["antibiotic choice", "duration", "steroids", "workup", "severity"]
    },
    {
        "id": "chf",
        "name": "Acute Decompensated Heart Failure",
        "pubmed_terms": [
            "acute decompensated heart failure treatment",
            "HFrEF diuresis",
            "heart failure SGLT2",
            "heart failure decongestion",
        ],
        "guideline": "ACC/AHA 2022 Heart Failure Guidelines",
        "key_sections": ["diuresis", "vasodilators", "SGLT2", "discharge criteria"]
    },
    {
        "id": "dka",
        "name": "Diabetic Ketoacidosis",
        "pubmed_terms": [
            "diabetic ketoacidosis treatment",
            "DKA insulin protocol",
            "DKA bicarbonate",
            "DKA fluid resuscitation",
        ],
        "guideline": "ADA DKA Management Guidelines 2023",
        "key_sections": ["fluids", "insulin", "potassium", "bicarbonate", "monitoring"]
    },
]

# Only pull from top journals
HIGH_IMPACT_JOURNALS = [
    "N Engl J Med", "JAMA", "Lancet", "BMJ",
    "Ann Intern Med", "JAMA Intern Med", "Crit Care Med",
    "Chest", "Clin Infect Dis", "Circulation", "J Am Coll Cardiol"
]

# How far back to look (days)
LOOKBACK_DAYS = 90


# ── PUBMED SEARCH ─────────────────────────────────────────────────────────────

def search_pubmed(query, max_results=10):
    """Search PubMed and return list of PMIDs."""
    date_from = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y/%m/%d")
    date_to = datetime.now().strftime("%Y/%m/%d")

    journal_filter = " OR ".join([f'"{j}"[Journal]' for j in HIGH_IMPACT_JOURNALS])
    full_query = f'({query}) AND ({journal_filter}) AND ("{date_from}"[Date - Publication] : "{date_to}"[Date - Publication])'

    params = urllib.parse.urlencode({
        "db": "pubmed",
        "term": full_query,
        "retmax": max_results,
        "retmode": "json",
        "sort": "relevance"
    })

    url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?{params}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
            return data.get("esearchresult", {}).get("idlist", [])
    except Exception as e:
        print(f"  PubMed search error: {e}")
        return []


def fetch_abstract(pmid):
    """Fetch title, authors, journal, year, abstract for a PMID."""
    params = urllib.parse.urlencode({
        "db": "pubmed",
        "id": pmid,
        "retmode": "json"
    })
    url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?{params}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
            article = data.get("result", {}).get(pmid, {})
            title = article.get("title", "")
            journal = article.get("source", "")
            pub_date = article.get("pubdate", "")
            authors = ", ".join([a.get("name", "") for a in article.get("authors", [])[:3]])
            if len(article.get("authors", [])) > 3:
                authors += " et al."
            return {
                "pmid": pmid,
                "title": title,
                "authors": authors,
                "journal": journal,
                "year": pub_date[:4] if pub_date else "",
                "pubmed_url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
            }
    except Exception as e:
        print(f"  Fetch error for PMID {pmid}: {e}")
        return None


def fetch_full_abstract(pmid):
    """Fetch the full abstract text for a PMID."""
    params = urllib.parse.urlencode({
        "db": "pubmed",
        "id": pmid,
        "retmode": "text",
        "rettype": "abstract"
    })
    url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?{params}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return resp.read().decode("utf-8")
    except Exception as e:
        print(f"  Abstract fetch error for PMID {pmid}: {e}")
        return ""


# ── CLAUDE EXTRACTION ─────────────────────────────────────────────────────────

EXTRACTION_PROMPT = """You are a clinical medicine expert reviewing a research abstract for relevance to residency-level clinical practice.

Disease context: {disease_name}
Current guideline: {guideline}
Key clinical sections this guideline covers: {sections}

Abstract to evaluate:
{abstract}

Your job:
1. Decide if this paper is clinically relevant to residents managing {disease_name}
2. If relevant, extract structured information

Return ONLY valid JSON, no markdown, no explanation:

{{
  "relevant": true or false,
  "reason_if_not": "why not relevant (only if relevant=false)",
  "title": "paper title",
  "authors": "authors",
  "journal": "journal name",
  "year": "year",
  "study_type": "RCT / Meta-analysis / Observational / Guideline / Case series / etc",
  "n": number of participants or null,
  "population": "who was studied (age, setting, condition severity)",
  "intervention": "what was done",
  "comparator": "what it was compared to",
  "primary_outcome": "what they measured",
  "key_finding": "one sentence: what they found, with numbers if available",
  "clinical_takeaway": "one sentence: what a resident should do differently or know",
  "guideline_relationship": "supports / adds_nuance / conflicts / early_signal",
  "guideline_relationship_explanation": "specifically how this relates to current {guideline}",
  "practice_change": "no_change / consider_in_select / potential_change / likely_change",
  "action_label": "one of: No action change / Consider in select patients / Watch for guideline update / Potential practice change",
  "evidence_grade": "A / B / C",
  "section_tag": "which section this belongs to: severity / workup / treatment / special_situations / followup",
  "caveats": "important limitations or applicability concerns for a resident"
}}

Be strict about relevance — only mark relevant=true if this directly impacts how a resident would manage {disease_name} today or in the near future. Reject review articles, basic science, and papers that are tangential."""


def extract_with_claude(abstract_text, disease):
    """Send abstract to Claude and get structured extraction."""
    if not ANTHROPIC_API_KEY:
        print("  No API key found — skipping Claude extraction")
        return None

    prompt = EXTRACTION_PROMPT.format(
        disease_name=disease["name"],
        guideline=disease["guideline"],
        sections=", ".join(disease["key_sections"]),
        abstract=abstract_text[:4000]  # cap at 4000 chars
    )

    payload = json.dumps({
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 1000,
        "messages": [{"role": "user", "content": prompt}]
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            raw = data["content"][0]["text"].strip()
            raw = raw.replace("```json", "").replace("```", "").strip()
            return json.loads(raw)
    except Exception as e:
        print(f"  Claude extraction error: {e}")
        return None


# ── DELTA COMPARISON ──────────────────────────────────────────────────────────

DELTA_PROMPT = """You are a clinical medicine expert. Given new research findings for a disease, 
generate a Delta View entry comparing the new evidence to the current guideline.

Disease: {disease_name}
Guideline: {guideline}
New finding: {finding}
Guideline relationship: {relationship}

Return ONLY valid JSON:
{{
  "topic": "brief topic label (3-5 words)",
  "guideline_says": "what the current guideline recommends on this topic",
  "new_evidence": "what the new study found, with numbers",
  "practical_interpretation": "plain language: does this change what I should do? For whom?",
  "action": "no-change / reinforces / consider / change"
}}"""


def generate_delta(finding, disease, relationship):
    """Generate a Delta View entry for a new finding."""
    if not ANTHROPIC_API_KEY:
        return None

    prompt = DELTA_PROMPT.format(
        disease_name=disease["name"],
        guideline=disease["guideline"],
        finding=finding,
        relationship=relationship
    )

    payload = json.dumps({
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 400,
        "messages": [{"role": "user", "content": prompt}]
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            raw = data["content"][0]["text"].strip()
            raw = raw.replace("```json", "").replace("```", "").strip()
            return json.loads(raw)
    except Exception as e:
        print(f"  Delta generation error: {e}")
        return None


# ── LOAD / SAVE EVIDENCE ─────────────────────────────────────────────────────

def load_existing_evidence():
    """Load existing evidence.json if it exists."""
    path = "data/evidence.json"
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return {"last_updated": "", "diseases": {}}


def save_evidence(data):
    """Save updated evidence.json."""
    os.makedirs("data", exist_ok=True)
    data["last_updated"] = datetime.now().strftime("%Y-%m-%d")
    with open("data/evidence.json", "w") as f:
        json.dump(data, f, indent=2)
    print(f"\n✓ Saved data/evidence.json")


# ── MAIN PIPELINE ─────────────────────────────────────────────────────────────

def run():
    print("=" * 60)
    print("RxRef Evidence Update Pipeline")
    print(f"Running: {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"Looking back {LOOKBACK_DAYS} days")
    print("=" * 60)

    evidence = load_existing_evidence()
    seen_pmids = set()

    # Collect all existing PMIDs to avoid duplicates
    for disease_data in evidence.get("diseases", {}).values():
        for study in disease_data.get("new_studies", []):
            if study.get("pmid"):
                seen_pmids.add(study["pmid"])

    for disease in DISEASES:
        print(f"\n── {disease['name']} ──")
        disease_id = disease["id"]

        if disease_id not in evidence["diseases"]:
            evidence["diseases"][disease_id] = {
                "name": disease["name"],
                "guideline": disease["guideline"],
                "last_updated": "",
                "new_studies": [],
                "delta_updates": []
            }

        new_studies = []
        all_pmids = []

        # Gather PMIDs from all search terms
        for term in disease["pubmed_terms"]:
            print(f"  Searching: {term}")
            pmids = search_pubmed(term, max_results=5)
            all_pmids.extend(pmids)
            time.sleep(0.4)  # respect NCBI rate limits

        # Deduplicate
        unique_pmids = list(dict.fromkeys(all_pmids))
        new_pmids = [p for p in unique_pmids if p not in seen_pmids]
        print(f"  Found {len(unique_pmids)} papers, {len(new_pmids)} new")

        for pmid in new_pmids[:12]:  # cap at 12 per disease per run
            print(f"  Processing PMID {pmid}...")

            meta = fetch_abstract(pmid)
            if not meta:
                continue

            abstract_text = fetch_full_abstract(pmid)
            if not abstract_text:
                continue

            time.sleep(0.5)

            extraction = extract_with_claude(abstract_text, disease)
            if not extraction:
                continue

            if not extraction.get("relevant"):
                print(f"    → Not relevant: {extraction.get('reason_if_not','')[:60]}")
                continue

            print(f"    ✓ Relevant: {extraction.get('key_finding','')[:80]}")

            # Merge metadata
            extraction["pmid"] = pmid
            extraction["pubmed_url"] = meta["pubmed_url"]
            extraction["added_date"] = datetime.now().strftime("%Y-%m-%d")

            new_studies.append(extraction)
            seen_pmids.add(pmid)

            # Generate delta entry if this has guideline implications
            relationship = extraction.get("guideline_relationship", "")
            if relationship in ["conflicts", "adds_nuance", "early_signal"] and extraction.get("key_finding"):
                delta = generate_delta(
                    extraction["key_finding"] + " " + extraction.get("clinical_takeaway", ""),
                    disease,
                    relationship
                )
                if delta:
                    delta["study_ref"] = extraction.get("title", "")[:50]
                    delta["pmid"] = pmid
                    evidence["diseases"][disease_id]["delta_updates"].append(delta)
                    print(f"    + Delta entry added: {delta.get('topic','')}")

            time.sleep(1)  # Claude rate limit buffer

        # Prepend new studies (newest first), keep last 30
        existing = evidence["diseases"][disease_id]["new_studies"]
        combined = new_studies + existing
        evidence["diseases"][disease_id]["new_studies"] = combined[:30]

        # Keep last 10 delta updates
        evidence["diseases"][disease_id]["delta_updates"] = \
            evidence["diseases"][disease_id]["delta_updates"][-10:]

        evidence["diseases"][disease_id]["last_updated"] = datetime.now().strftime("%Y-%m-%d")
        print(f"  Added {len(new_studies)} new studies for {disease['name']}")

    save_evidence(evidence)
    print("\n✓ Pipeline complete.")


if __name__ == "__main__":
    run()
