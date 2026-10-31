"""Scrape the quest guides of dofuspourlesnoobs.com into dofus_net/knowledge/dpln_quests.json (resumable, polite: robots.txt respected, ~1 request/s).

  python tools/scrape_dpln.py [--limit N] [--delay S]

  dpln_quests.json  {"quests": {normalized name: {name, url, prereq, start, rewards, steps: [paragraph, ...], coords: [[x, y], ...]}}}
Raw pages are cached in tools/.dpln_cache/ (not versioned) so a rerun only fetches what is missing."""
import argparse, html, json, os, re, sys, time, unicodedata, urllib.request, urllib.robotparser

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "client"))
import knowledge

BASE = "https://www.dofuspourlesnoobs.com"
UA = "Mozilla/5.0 (compatible; dofus-bot-knowledge/1.0)"
CACHE = os.path.join(HERE, ".dpln_cache")
FILE = "dpln_quests.json"


def norm(v):
    s = unicodedata.normalize("NFKD", str(v or "")).casefold()
    return re.sub(r"[^a-z0-9]+", " ", "".join(c for c in s if not unicodedata.combining(c))).strip()


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    raw = urllib.request.urlopen(req, timeout=30).read()
    try: return raw.decode("utf8")
    except UnicodeDecodeError: return raw.decode("cp1252", "replace")


def text(fragment):
    fragment = re.sub(r"<br\s*/?>|</li>|</p>|</div>", "\n", fragment, flags=re.I)
    fragment = re.sub(r"<li[^>]*>", "- ", fragment, flags=re.I)
    t = html.unescape(re.sub(r"<[^>]+>", "", fragment)).replace("\xa0", " ")
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", t)).strip()


def field(body, label):
    m = re.search(label + r"\s*:?\s*</font></strong>(.*?)(?:<br\s*/?>\s*<br\s*/?>|<strong>|</div>)", body, flags=re.S | re.I)
    return text(m.group(1)) if m else None


def parse(page, url):
    """-> quest dict or None when the page is not a quest guide."""
    body = html.unescape(page)
    if "Position de lancement" not in body: return None
    title = re.search(r"<title>(.*?)</title>", page, flags=re.S)
    name = text(title.group(1)) if title else None
    if not name: return None
    start = field(page.replace("&eacute;", "é").replace("&Eacute;", "É"), "Position de lancement")
    prereq = re.search(r"Pr(?:&eacute;|é)requis\s*:?\s*</font></strong>(.*?)<strong>", page, flags=re.S | re.I)
    reward = re.search(r"R(?:&eacute;|é)compenses\s*:?\s*</font></strong>(.*?)<strong>", page, flags=re.S | re.I)
    content = page.split('class="wsite-content-title"', 1)[-1]
    content = re.sub(r"<script.*?</script>|<style.*?</style>", "", content, flags=re.S)
    paras = [text(p) for p in re.findall(r'<div class="paragraph"[^>]*>(.*?)</div>', content, flags=re.S)]
    steps = [p for p in paras if len(p) > 25 and not p.startswith(("Mis en ligne", "Pr"))]
    coords = sorted({(int(x), int(y)) for x, y in re.findall(r"\[\s*(-?\d{1,3})\s*[,;]\s*(-?\d{1,3})\s*\]", "\n".join(steps))})
    return {"name": name, "url": url, "prereq": text(prereq.group(1)) if prereq else None, "start": start,
            "rewards": text(reward.group(1)) if reward else None, "steps": steps, "coords": [list(c) for c in coords]}


def save(book):
    try: knowledge.save(FILE, book)
    except PermissionError:   # Windows: something (indexer, antivirus, editor) holds the target, so os.replace is refused: write in place
        with open(knowledge.path(FILE), "w", encoding="utf8") as f: json.dump(book, f, indent=1, ensure_ascii=False, sort_keys=True)
        try: os.remove(knowledge.path(FILE) + ".tmp")
        except OSError: pass


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--limit", type=int, default=0); ap.add_argument("--delay", type=float, default=1.0)
    args = ap.parse_args()
    rp = urllib.robotparser.RobotFileParser(BASE + "/robots.txt"); rp.read()
    urls = re.findall(r"<loc>(.*?)</loc>", get(BASE + "/sitemap.xml"))
    urls = [u for u in urls if u.endswith(".html") and rp.can_fetch(UA, u)]
    if args.limit: urls = urls[:args.limit]
    os.makedirs(CACHE, exist_ok=True)
    book = knowledge.load(FILE, {}); book.setdefault("quests", {})
    for i, url in enumerate(urls, 1):
        cf = os.path.join(CACHE, re.sub(r"[^\w.-]", "_", url.rsplit("/", 1)[-1]))
        if os.path.exists(cf):
            with open(cf, encoding="utf8") as f: page = f.read()
        else:
            try: page = get(url)
            except Exception as e: print(f"[{i}/{len(urls)}] ERR {url}: {e}"); time.sleep(args.delay); continue
            with open(cf, "w", encoding="utf8") as f: f.write(page)
            time.sleep(args.delay)
        q = parse(page, url)
        if q: book["quests"][norm(q["name"])] = q
        if i % 100 == 0: save(book); print(f"[{i}/{len(urls)}] {len(book['quests'])} quêtes")
    save(book); print(f"terminé : {len(book['quests'])} quêtes -> knowledge/{FILE}")


if __name__ == "__main__": main()
