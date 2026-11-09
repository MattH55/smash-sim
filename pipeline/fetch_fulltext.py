#!/usr/bin/env python3
"""Step 3 — obtain full text for (a) SMASH-BIO skeleton records and (b) screened gap-search papers.
Writes data/fulltext/<doi>.txt and data/30_fulltext_index.jsonl (with source_set = smash | gap).

Order: Europe PMC OA XML (best: structured, includes methods) -> Unpaywall OA PDF -> local PDF dir.
Papers with no full text are listed in data/30_missing.csv for manual PDF drop-in.
Run:  python fetch_fulltext.py
"""
from __future__ import annotations

import csv
import io
import xml.etree.ElementTree as ET
from pathlib import Path

from common import RateLimiter, doi_to_fname, http_get, load_config, read_jsonl, write_jsonl

cfg = load_config()
FT = cfg["_data"] / "fulltext"
FT.mkdir(exist_ok=True)
rl = RateLimiter(5)


def europepmc_xml(pmcid: str) -> str | None:
    rl.wait()
    try:
        xml = http_get(f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML").text
    except Exception:
        return None
    root = ET.fromstring(xml)
    # keep section titles so the extractor can cite "Methods"
    parts = []
    for sec in root.iter():
        if sec.tag in ("title",):
            parts.append("\n## " + "".join(sec.itertext()).strip())
        elif sec.tag in ("p", "td", "caption"):
            parts.append("".join(sec.itertext()).strip())
    text = "\n".join(p for p in parts if p)
    return text if len(text) > 2000 else None


def pdf_to_text(data: bytes) -> str | None:
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        pages = [f"\n<<page {i+1}>>\n" + (p.extract_text() or "") for i, p in enumerate(reader.pages)]
        text = "".join(pages)
        return text if len(text) > 2000 else None
    except Exception:
        return None


def unpaywall_pdf(doi: str) -> str | None:
    rl.wait()
    try:
        js = http_get(f"https://api.unpaywall.org/v2/{doi}", {"email": cfg["contact_email"]}).json()
    except Exception:
        return None
    loc = js.get("best_oa_location") or {}
    url = loc.get("url_for_pdf")
    if not url:
        return None
    try:
        return pdf_to_text(http_get(url, headers={"User-Agent": "smash-sim-pipeline"}).content)
    except Exception:
        return None


def local_pdf(doi: str) -> str | None:
    p = Path(cfg["fulltext"]["local_pdf_dir"])
    if not p.is_absolute():
        p = cfg["_root"] / p
    f = p / f"{doi_to_fname(doi)}.pdf"
    return pdf_to_text(f.read_bytes()) if f.exists() else None


if __name__ == "__main__":
    recs = []
    for sk in read_jsonl(cfg["_data"] / "10_skeletons.jsonl"):
        recs.append(dict(doi=sk["doi"], pmcid=None, arxiv_id=None, title=sk.get("title") or "", year=sk.get("year"),
                         journal=None, dedupe_key=sk["doi"], source_set="smash"))
    for r in read_jsonl(cfg["_data"] / "20_screened.jsonl"):
        recs.append(dict(r, source_set="gap"))
    # resolve PMCIDs for skeleton DOIs via Europe PMC (needed for OA XML)
    for r in recs:
        if r["source_set"] == "smash" and r["doi"] and not r["doi"].startswith("smashbio:"):
            rl.wait()
            try:
                js = http_get("https://www.ebi.ac.uk/europepmc/webservices/rest/search",
                              {"query": f'DOI:"{r["doi"]}"', "format": "json", "pageSize": 1}).json()
                hit = (js.get("resultList", {}).get("result") or [{}])[0]
                r["pmcid"] = hit.get("pmcid")
            except Exception:
                pass
    index, missing = [], []
    for i, r in enumerate(recs):
        doi = r.get("doi")
        key = doi or r.get("arxiv_id") or r["dedupe_key"]
        out = FT / f"{doi_to_fname(key)}.txt"
        if out.exists():
            index.append(dict(key=key, doi=doi, path=str(out), source="cached", source_set=r["source_set"]))
            continue
        text, src = None, None
        for s in cfg["fulltext"]["sources"]:
            if s == "europepmc_oa" and r.get("pmcid"):
                text, src = europepmc_xml(r["pmcid"]), s
            elif s == "unpaywall" and doi:
                text, src = unpaywall_pdf(doi), s
            elif s == "local_pdf_dir" and doi:
                text, src = local_pdf(doi), s
            if text:
                break
        if not text and r.get("arxiv_id"):
            rl.wait()
            try:
                text, src = pdf_to_text(http_get(f"https://arxiv.org/pdf/{r['arxiv_id']}").content), "arxiv"
            except Exception:
                pass
        if text:
            out.write_text(text)
            index.append(dict(key=key, doi=doi, path=str(out), source=src, source_set=r["source_set"]))
        else:
            missing.append(dict(doi=doi, title=r["title"], year=r.get("year"), journal=r.get("journal"), source_set=r["source_set"]))
        if i % 25 == 0:
            print(f"  {i}/{len(recs)}  got={len(index)} missing={len(missing)}")

    write_jsonl(cfg["_data"] / "30_fulltext_index.jsonl", index)
    with open(cfg["_data"] / "30_missing.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["doi", "title", "year", "journal", "source_set"])
        w.writeheader()
        w.writerows(missing)
    print(f"full text for {len(index)}; missing {len(missing)} -> data/30_missing.csv (drop PDFs into {cfg['fulltext']['local_pdf_dir']} and rerun)")
