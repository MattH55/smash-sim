#!/usr/bin/env python3
"""Step 2a — GAP SEARCH: run the search strings against PubMed, Europe PMC and arXiv; write data/20_hits.jsonl.

Each hit: {source, query_tag, doi, pmid, pmcid, arxiv_id, title, abstract, year, journal, is_oa}
Run:  python gap_search.py [--only pubmed|europepmc|arxiv]
"""
from __future__ import annotations

import argparse
import xml.etree.ElementTree as ET

from common import RateLimiter, http_get, load_config, norm_doi, write_jsonl

cfg = load_config()
EMAIL = cfg["contact_email"]


def search_pubmed(tag: str, query: str) -> list[dict]:
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
    params = {"db": "pubmed", "term": query, "retmax": 10000, "retmode": "json", "email": EMAIL}
    if cfg.get("ncbi_api_key"):
        params["api_key"] = cfg["ncbi_api_key"]
    ids = http_get(base + "esearch.fcgi", params).json()["esearchresult"]["idlist"]
    print(f"  pubmed/{tag}: {len(ids)} ids")
    rl = RateLimiter(3 if not cfg.get("ncbi_api_key") else 10)
    hits = []
    for i in range(0, len(ids), 200):
        rl.wait()
        batch = ids[i : i + 200]
        xml = http_get(base + "efetch.fcgi", {"db": "pubmed", "id": ",".join(batch), "retmode": "xml", "email": EMAIL}).text
        root = ET.fromstring(xml)
        for art in root.findall(".//PubmedArticle"):
            pmid = art.findtext(".//PMID")
            title = "".join(art.find(".//ArticleTitle").itertext()) if art.find(".//ArticleTitle") is not None else ""
            abstract = " ".join("".join(a.itertext()) for a in art.findall(".//AbstractText"))
            year = art.findtext(".//PubDate/Year") or art.findtext(".//PubDate/MedlineDate", "")[:4]
            journal = art.findtext(".//Journal/Title")
            doi = pmcid = None
            for aid in art.findall(".//ArticleId"):
                if aid.get("IdType") == "doi":
                    doi = norm_doi(aid.text)
                if aid.get("IdType") == "pmc":
                    pmcid = aid.text
            hits.append(dict(source="pubmed", query_tag=tag, doi=doi, pmid=pmid, pmcid=pmcid, arxiv_id=None,
                             title=title, abstract=abstract, year=int(year) if year and year.isdigit() else None,
                             journal=journal, is_oa=bool(pmcid)))
    return hits


def search_europepmc(tag: str, query: str) -> list[dict]:
    url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
    hits, cursor = [], "*"
    while True:
        js = http_get(url, {"query": query, "format": "json", "pageSize": 1000, "cursorMark": cursor, "resultType": "lite"}).json()
        for r in js.get("resultList", {}).get("result", []):
            hits.append(dict(source="europepmc", query_tag=tag, doi=norm_doi(r.get("doi")), pmid=r.get("pmid"),
                             pmcid=r.get("pmcid"), arxiv_id=None, title=r.get("title", ""), abstract=r.get("abstractText", ""),
                             year=int(r["pubYear"]) if r.get("pubYear") else None, journal=r.get("journalTitle"),
                             is_oa=r.get("isOpenAccess") == "Y"))
        nxt = js.get("nextCursorMark")
        if not nxt or nxt == cursor:
            break
        cursor = nxt
    print(f"  europepmc/{tag}: {len(hits)}")
    return hits


def search_arxiv(tag: str, query: str) -> list[dict]:
    ns = {"a": "http://www.w3.org/2005/Atom", "ar": "http://arxiv.org/schemas/atom"}
    hits, start = [], 0
    rl = RateLimiter(0.33)
    while True:
        rl.wait()
        xml = http_get("http://export.arxiv.org/api/query",
                       {"search_query": query, "start": start, "max_results": 500}).text
        root = ET.fromstring(xml)
        entries = root.findall("a:entry", ns)
        if not entries:
            break
        for e in entries:
            aid = e.findtext("a:id", "", ns).rsplit("/", 1)[-1]
            doi_el = e.find("ar:doi", ns)
            hits.append(dict(source="arxiv", query_tag=tag, doi=norm_doi(doi_el.text) if doi_el is not None else None,
                             pmid=None, pmcid=None, arxiv_id=aid, title=e.findtext("a:title", "", ns).strip(),
                             abstract=e.findtext("a:summary", "", ns).strip(),
                             year=int(e.findtext("a:published", "0000", ns)[:4]), journal="arXiv", is_oa=True))
        start += 500
    print(f"  arxiv/{tag}: {len(hits)}")
    return hits


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["pubmed", "europepmc", "arxiv"])
    args = ap.parse_args()
    all_hits = []
    for src, fn in [("pubmed", search_pubmed), ("europepmc", search_europepmc), ("arxiv", search_arxiv)]:
        if args.only and args.only != src:
            continue
        print(f"[{src}]")
        for tag, q in cfg["searches"].get(src, {}).items():
            all_hits.extend(fn(tag, " ".join(q.split())))
    out = cfg["_data"] / "20_hits.jsonl"
    write_jsonl(out, all_hits)
    print(f"wrote {len(all_hits)} raw hits -> {out}")
