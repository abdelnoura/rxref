"""
RxRef Evidence Surveillance Pipeline
=====================================
Runs weekly via GitHub Actions. FREE — no API costs.
Uses targeted PubMed queries + keyword filtering. No AI screening needed.
Physician reviews results before any clinical action.
"""
import json, os, re, requests, time
from datetime import datetime, timedelta

DISEASES = {
    "cap": {
        "name": "Community-Acquired Pneumonia",
        "disease_name": "Community-Acquired Pneumonia",
        "queries": [
            "community acquired pneumonia treatment randomized controlled trial",
            "community acquired pneumonia antibiotic duration",
            "CAP corticosteroids randomized trial",
            "community acquired pneumonia procalcitonin",
            "pneumonia severity score",
        ],
        "exclude_keywords": [
            "pediatric", "children", "infant", "neonatal", "animal", "mouse", "rat",
            "in vitro", "cell culture", "case report", "letter to the editor",
            "hospital-acquired", "ventilator-associated", "nursing home"
        ],
        "include_keywords": [
            "pneumonia", "cap", "respiratory", "antibiotic", "treatment",
            "mortality", "severity", "clinical", "randomized", "trial", "cohort"
        ],
        "existing_pmids": []
    }
}

PUBMED_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
LOOKBACK_DAYS = 90
MAX_PER_QUERY = 15


def search_pubmed(query, days_back=LOOKBACK_DAYS):
    since = (datetime.now() - timedelta(days=days_back)).strftime("%Y/%m/%d")
    try:
        r = requests.get(f"{PUBMED_BASE}/esearch.fcgi", params={
            "db": "pubmed",
            "term": f"{query} AND {since}[PDAT]:3000[PDAT]",
            "retmax": MAX_PER_QUERY,
            "retmode": "json",
            "sort": "relevance",
        }, timeout=15)
        r.raise_for_status()
        pmids = r.json().get("esearchresult", {}).get("idlist", [])
        if not pmids:
            return []
        time.sleep(1)
        r2 = requests.get(f"{PUBMED_BASE}/efetch.fcgi", params={
            "db": "pubmed", "id": ",".join(pmids),
            "retmode": "xml", "rettype": "abstract",
        }, timeout=20)
        r2.raise_for_status()
        return parse_xml(r2.text)
    except Exception as e:
        print(f"    Search failed: {e}")
        return []


def parse_xml(xml_text):
    articles = []
    for block in re.split(r'<PubmedArticle>', xml_text)[1:]:
        try:
            pmid = re.search(r'<PMID[^>]*>(\d+)</PMID>', block)
            title = re.search(r'<ArticleTitle[^>]*>(.*?)</ArticleTitle>', block, re.DOTALL)
            abstract = re.search(r'<AbstractText[^>]*>(.*?)</AbstractText>', block, re.DOTALL)
            journal = re.search(r'<Title>(.*?)</Title>', block)
            year = re.search(r'<PubDate>.*?<Year>(\d{4})</Year>', block, re.DOTALL)
            authors_raw = re.findall(r'<LastName>(.*?)</LastName>', block)
            pub_types = re.findall(r'<PublicationType[^>]*>(.*?)</PublicationType>', block)
            if not pmid or not title:
                continue
            articles.append({
                "pmid": pmid.group(1),
                "title": re.sub(r'<[^>]+>', '', title.group(1)),
                "abstract": re.sub(r'<[^>]+>', '', abstract.group(1))[:1500] if abstract else "",
                "journal": re.sub(r'<[^>]+>', '', journal.group(1)) if journal else "",
                "year": year.group(1) if year else "",
                "authors": f"{authors_raw[0]} et al." if authors_raw else "",
                "pub_types": pub_types,
                "pubmed_url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid.group(1)}/",
            })
        except Exception:
            continue
    return articles


def filter_articles(articles, config):
    """Simple keyword-based filtering — no API needed."""
    relevant = []
    seen_pmids = set()

    for a in articles:
        pmid = a["pmid"]
        if pmid in seen_pmids or pmid in config.get("existing_pmids", []):
            continue
        seen_pmids.add(pmid)

        text = (a["title"] + " " + a["abstract"]).lower()

        # Skip if exclude keywords present
        if any(kw in text for kw in config["exclude_keywords"]):
            continue

        # Must have at least 2 include keywords
        matches = sum(1 for kw in config["include_keywords"] if kw in text)
        if matches < 2:
            continue

        # Classify study type from pub_types or abstract
        study_type = "other"
        pt_text = " ".join(a.get("pub_types", [])).lower()
        if "randomized" in pt_text or "randomized controlled" in text or " rct " in text:
            study_type = "RCT"
        elif "meta-analysis" in pt_text or "meta-analysis" in text:
            study_type = "meta-analysis"
        elif "systematic review" in pt_text or "systematic review" in text:
            study_type = "systematic-review"
        elif "cohort" in text or "observational" in text:
            study_type = "cohort"
        elif "guideline" in pt_text or "guideline" in text:
            study_type = "guideline"

        # Guess practice change potential
        pcp = "low"
        if study_type in ("RCT", "meta-analysis"):
            pcp = "moderate"
        if any(w in text for w in ["mortality", "survival", "death", "superior", "inferior", "significant"]):
            if study_type in ("RCT", "meta-analysis"):
                pcp = "high"

        # Extract a short key finding from abstract
        key_finding = ""
        if a["abstract"]:
            sentences = re.split(r'(?<=[.!?]) +', a["abstract"])
            result_sentences = [s for s in sentences if any(w in s.lower() for w in
                ["significant", "reduced", "increased", "improved", "no difference",
                 "superior", "inferior", "mortality", "p=", "p <", "odds ratio", "hazard", "rr ", "arr"])]
            key_finding = result_sentences[0][:200] if result_sentences else sentences[-1][:200] if sentences else ""

        # Guess clinical relevance
        relevance = "new_data"
        if any(w in text for w in ["no significant", "no difference", "similar to", "non-inferior"]):
            relevance = "supports"
        elif any(w in text for w in ["superior", "better than", "improved", "reduced mortality"]):
            relevance = "adds_nuance"

        a["study_type"] = study_type
        a["practice_change_potential"] = pcp
        a["key_finding"] = key_finding
        a["clinical_relevance"] = relevance
        a["relates_to"] = "treatment"
        a["reviewer_note"] = f"{study_type} study — review full abstract before applying to practice."
        a["is_new"] = True
        a["found_date"] = datetime.now().strftime("%Y-%m-%d")
        del a["pub_types"]
        relevant.append(a)

    return relevant


def run_pipeline():
    output_path = "data/evidence.json"
    existing = {}
    if os.path.exists(output_path):
        try:
            with open(output_path) as f:
                existing = json.load(f)
        except Exception:
            pass

    all_results = {}

    for disease_id, config in DISEASES.items():
        print(f"\n=== {config['name']} ===")

        # Load existing PMIDs to avoid duplicates
        prior_articles = existing.get("diseases", {}).get(disease_id, {}).get("articles", [])
        config["existing_pmids"] = [a["pmid"] for a in prior_articles]

        all_articles = []
        for query in config["queries"]:
            print(f"  Searching: {query}")
            found = search_pubmed(query)
            print(f"    Found {len(found)} articles")
            all_articles.extend(found)
            time.sleep(1)

        print(f"  Total raw: {len(all_articles)}")
        relevant = filter_articles(all_articles, config)
        print(f"  Relevant after filtering: {len(relevant)}")

        # Keep prior articles (unmark as new) + add new ones
        new_pmids = {a["pmid"] for a in relevant}
        for a in prior_articles:
            if a["pmid"] not in new_pmids:
                a["is_new"] = False
                relevant.append(a)

        pri = {"high": 0, "moderate": 1, "low": 2}
        relevant.sort(key=lambda x: (
            0 if x.get("is_new") else 1,
            pri.get(x.get("practice_change_potential", "low"), 2)
        ))

        all_results[disease_id] = {
            "disease_name": config["name"],
            "last_updated": datetime.now().strftime("%Y-%m-%d"),
            "article_count": len(relevant),
            "articles": relevant[:50]
        }

    output = {
        "pipeline_version": "3.0-free",
        "generated": datetime.now().isoformat(),
        "diseases": all_results
    }
    os.makedirs("data", exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)

    total = sum(d["article_count"] for d in all_results.values())
    print(f"\nDone. {total} articles written to {output_path}")


if __name__ == "__main__":
    run_pipeline()
