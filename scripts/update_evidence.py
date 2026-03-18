"""
RxRef Evidence Surveillance Pipeline
=====================================
Runs weekly via GitHub Actions. Does NOT change clinical recommendations automatically.
Surfaces new studies for physician review and updates the "What's New" feed.

Flow:
  1. Query PubMed for recent high-quality studies on each disease
  2. Use Claude to screen for clinical relevance and extract structured data
  3. Write to data/evidence.json — app displays these in the homepage feed
  4. Physician reviews flagged studies and manually updates REFS in index.html if warranted

Cost: ~$0/week in Claude API calls depending on volume of new papers.
"""

import json
import os
import re
import requests
from datetime import datetime, timedelta
from anthropic import Anthropic

client = Anthropic()

# ─── SEARCH CONFIGURATION ─────────────────────────────────────────────────────
# Each disease has targeted PubMed queries. Narrow searches = higher relevance.
DISEASES = {
    "cap": {
        "name": "Community-Acquired Pneumonia",
        "queries": [
            "community acquired pneumonia treatment randomized controlled trial",
            "community acquired pneumonia antibiotic duration RCT",
            "CAP corticosteroids randomized trial",
            "community acquired pneumonia procalcitonin guided therapy",
            "pneumonia severity score validation",
        ],
        "key_journals": ["NEJM", "JAMA", "Lancet", "CHEST", "CID", "Am J Respir Crit Care Med"],
        "existing_refs": [
            "IDSA/ATS 2019", "CAPE-COD 2023", "Uranga 2016", "PROACTIVE 2023",
            "SCOUT-CAP 2023", "Metlay 1997", "Wipf 1999", "Lim 2003", "Fine 1997"
        ]
    }
}

PUBMED_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
LOOKBACK_DAYS = 90  # How far back to search (overlap prevents gaps)
MAX_PER_QUERY = 20  # Max results per PubMed query


# ─── PUBMED SEARCH ─────────────────────────────────────────────────────────────
def search_pubmed(query: str, days_back: int = LOOKBACK_DAYS) -> list[dict]:
    """Search PubMed for recent studies matching query. Returns list of PMIDs + metadata."""
    since = (datetime.now() - timedelta(days=days_back)).strftime("%Y/%m/%d")

    # Search for PMIDs
    search_params = {
        "db": "pubmed",
        "term": f"{query} AND {since}[PDAT]:3000[PDAT]",
        "retmax": MAX_PER_QUERY,
        "retmode": "json",
        "sort": "relevance",
    }
    search_url = f"{PUBMED_BASE}/esearch.fcgi"
    r = requests.get(search_url, params=search_params, timeout=15)
    r.raise_for_status()
    pmids = r.json().get("esearchresult", {}).get("idlist", [])

    if not pmids:
        return []

    # Fetch abstracts for found PMIDs
    fetch_params = {
        "db": "pubmed",
        "id": ",".join(pmids),
        "retmode": "xml",
        "rettype": "abstract",
    }
    fetch_url = f"{PUBMED_BASE}/efetch.fcgi"
    r = requests.get(fetch_url, params=fetch_params, timeout=20)
    r.raise_for_status()

    return parse_pubmed_xml(r.text, pmids)


def parse_pubmed_xml(xml_text: str, pmids: list[str]) -> list[dict]:
    """Extract key fields from PubMed XML response."""
    articles = []

    # Split on article boundaries
    article_blocks = re.split(r'<PubmedArticle>', xml_text)[1:]

    for block in article_blocks:
        try:
            pmid = re.search(r'<PMID[^>]*>(\d+)</PMID>', block)
            title = re.search(r'<ArticleTitle[^>]*>(.*?)</ArticleTitle>', block, re.DOTALL)
            abstract = re.search(r'<AbstractText[^>]*>(.*?)</AbstractText>', block, re.DOTALL)
            journal = re.search(r'<Title>(.*?)</Title>', block)
            year = re.search(r'<PubDate>.*?<Year>(\d{4})</Year>', block, re.DOTALL)
            authors_raw = re.findall(r'<LastName>(.*?)</LastName>', block)

            if not pmid or not title:
                continue

            # Clean HTML tags from abstract
            abstract_text = re.sub(r'<[^>]+>', '', abstract.group(1)) if abstract else ""

            articles.append({
                "pmid": pmid.group(1),
                "title": re.sub(r'<[^>]+>', '', title.group(1)),
                "abstract": abstract_text[:2000],  # Truncate for API call
                "journal": re.sub(r'<[^>]+>', '', journal.group(1)) if journal else "",
                "year": year.group(1) if year else "",
                "authors": f"{authors_raw[0]} et al." if authors_raw else "",
                "pubmed_url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid.group(1)}/",
            })
        except Exception:
            continue

    return articles


# ─── CLAUDE SCREENING ──────────────────────────────────────────────────────────
def screen_with_claude(articles: list[dict], disease_config: dict) -> list[dict]:
    """
    Use Claude to screen articles for clinical relevance.
    Returns only articles that meet relevance threshold with structured extraction.
    """
    if not articles:
        return []

    # Deduplicate by PMID
    seen = set()
    unique = []
    for a in articles:
        if a["pmid"] not in seen:
            seen.add(a["pmid"])
            unique.append(a)

    # Batch articles into groups of 5 for efficient screening
    batches = [unique[i:i+5] for i in range(0, len(unique), 5)]
    relevant = []

    for batch in batches:
        articles_text = "\n\n".join([
            f"PMID: {a['pmid']}\nTitle: {a['title']}\nJournal: {a['journal']} ({a['year']})\nAuthors: {a['authors']}\nAbstract: {a['abstract']}"
            for a in batch
        ])

        prompt = f"""You are a clinical evidence screener for a physician reference tool about {disease_config['name']}.

Review these {len(batch)} PubMed abstracts and identify which are CLINICALLY RELEVANT for a practicing hospitalist or resident.

A study is relevant if it:
- Is an RCT, meta-analysis, or large cohort study (n>200)
- Directly addresses treatment decisions, risk stratification, or diagnostic workup
- Could plausibly change or reinforce a clinical recommendation
- Covers {disease_config['name']} in adult inpatient or outpatient settings

A study is NOT relevant if it:
- Is a case report, small pilot, or editorial
- Focuses on pediatric populations exclusively
- Is a basic science / mechanistic study
- Duplicates already-integrated evidence: {', '.join(disease_config['existing_refs'])}

ARTICLES TO SCREEN:
{articles_text}

For each RELEVANT article, respond with JSON only (no markdown, no preamble):
{{
  "relevant_articles": [
    {{
      "pmid": "string",
      "title": "string",
      "authors": "string",
      "journal": "string",
      "year": "string",
      "pubmed_url": "https://pubmed.ncbi.nlm.nih.gov/PMID/",
      "study_type": "RCT|meta-analysis|cohort|guideline|other",
      "patient_population": "brief description",
      "key_finding": "1-2 sentence summary of primary result with effect size if available",
      "clinical_relevance": "supports|conflicts|adds_nuance|new_data",
      "relates_to": "treatment|risk_stratification|workup|prevention|prognosis",
      "practice_change_potential": "high|moderate|low",
      "reviewer_note": "1 sentence explaining why this matters for {disease_config['name']} management"
    }}
  ]
}}

If no articles are relevant, return: {{"relevant_articles": []}}"""

        try:
            response = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=2000,
                messages=[{"role": "user", "content": prompt}]
            )

            text = response.content[0].text.strip()
            # Strip any accidental markdown fences
            text = re.sub(r'^```json\s*', '', text)
            text = re.sub(r'\s*```$', '', text)

            data = json.loads(text)
            for item in data.get("relevant_articles", []):
                # Match back to original article for URL
                orig = next((a for a in batch if a["pmid"] == item["pmid"]), {})
                item["pubmed_url"] = orig.get("pubmed_url", f"https://pubmed.ncbi.nlm.nih.gov/{item['pmid']}/")
                relevant.append(item)

        except (json.JSONDecodeError, KeyError, IndexError) as e:
            print(f"Screening batch parse error: {e}")
            continue

    return relevant


# ─── MAIN PIPELINE ─────────────────────────────────────────────────────────────
def run_pipeline():
    output_path = "data/evidence.json"

    # Load existing data to preserve previously found articles
    existing = {}
    if os.path.exists(output_path):
        with open(output_path) as f:
            existing = json.load(f)

    all_disease_results = {}

    for disease_id, config in DISEASES.items():
        print(f"\n=== Processing {config['name']} ===")
        all_articles = []

        # Run all queries for this disease
        for query in config["queries"]:
            print(f"  Searching: {query}")
            try:
                articles = search_pubmed(query)
                print(f"    Found {len(articles)} articles")
                all_articles.extend(articles)
                time.sleep(1.5)
            except Exception as e:
                print(f"    Search failed: {e}")
                continue

        print(f"  Total raw articles: {len(all_articles)}")

        # Screen with Claude
        relevant = screen_with_claude(all_articles, config)
        print(f"  Relevant after screening: {len(relevant)}")

        # Merge with existing — keep prior articles, add new ones (dedup by pmid)
        prior = {a["pmid"]: a for a in existing.get(disease_id, {}).get("articles", [])}
        new_pmids = {a["pmid"] for a in relevant}

        # Mark new articles
        for a in relevant:
            a["found_date"] = datetime.now().strftime("%Y-%m-%d")
            a["is_new"] = True

        # Keep prior articles but unmark them as new
        for pmid, a in prior.items():
            if pmid not in new_pmids:
                a["is_new"] = False
                relevant.append(a)

        # Sort: new first, then by practice_change_potential
        priority_map = {"high": 0, "moderate": 1, "low": 2}
        relevant.sort(key=lambda x: (
            0 if x.get("is_new") else 1,
            priority_map.get(x.get("practice_change_potential", "low"), 2)
        ))

        all_disease_results[disease_id] = {
            "disease_name": config["name"],
            "last_updated": datetime.now().strftime("%Y-%m-%d"),
            "article_count": len(relevant),
            "articles": relevant[:50]  # Cap at 50 stored articles per disease
        }

    # Write output
    output = {
        "pipeline_version": "2.0",
        "generated": datetime.now().isoformat(),
        "diseases": all_disease_results
    }

    os.makedirs("data", exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)

    total = sum(d["article_count"] for d in all_disease_results.values())
    print(f"\n✓ Pipeline complete. {total} total relevant articles written to {output_path}")


if __name__ == "__main__":
    run_pipeline()
