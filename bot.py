"""Post new items from an RSS feed to Bluesky, with link-preview cards."""
import html
import io
import json
import os
import time

import feedparser
import requests
from atproto import Client, models
from bs4 import BeautifulSoup
from PIL import Image

FEED = "https://theoldreader.com/profile/19f3e2b78dcc6a81ae1cc236.rss"
SEEN_FILE = "seen.json"
MAX_POSTS_PER_RUN = 5
DRY_RUN = os.environ.get("DRY_RUN") == "1"
BACKFILL = int(os.environ.get("BACKFILL") or 0)  # one-off: post the latest N items
UA = ("Mozilla/5.0 (compatible; biocat-papers-bot/1.0; "
      "+https://bsky.app/profile/biocat-papers.bsky.social)")


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return json.load(f)
    return None


def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(sorted(seen), f, indent=0)


def trim(text, n):
    text = " ".join(html.unescape(text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def page_preview(url):
    """Return (title, description, image_url) from the article's Open Graph tags."""
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
        r.raise_for_status()
    except Exception as e:
        print(f"  could not fetch page: {e}")
        return None, None, None
    soup = BeautifulSoup(r.text, "html.parser")

    def meta(*names):
        for n in names:
            tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
            if tag and tag.get("content"):
                return tag["content"].strip()
        return None

    img = meta("og:image", "twitter:image", "twitter:image:src")
    if img:
        img = requests.compat.urljoin(r.url, img)
    return (meta("og:title", "twitter:title", "citation_title"),
            meta("og:description", "twitter:description", "description"),
            img)


def thumbnail(client, img_url):
    """Download the preview image, shrink it under Bluesky's 1 MB limit, upload it."""
    try:
        r = requests.get(img_url, headers={"User-Agent": UA}, timeout=20)
        r.raise_for_status()
        im = Image.open(io.BytesIO(r.content)).convert("RGB")
        im.thumbnail((1200, 1200))
        for q in (85, 75, 65, 50):
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=q, optimize=True)
            if buf.tell() < 950_000:
                break
        if DRY_RUN:
            print(f"  thumbnail ok ({buf.tell() // 1024} KB)")
            return None
        return client.upload_blob(buf.getvalue()).blob
    except Exception as e:
        print(f"  no thumbnail: {e}")
        return None


def already_posted(client):
    """Links the account has already posted, so a backfill never duplicates a post."""
    links, texts, cursor = set(), [], None
    while True:
        r = client.get_author_feed(actor=client.me.did, cursor=cursor, limit=100)
        for item in r.feed:
            rec = item.post.record
            texts.append(rec.text or "")
            ext = getattr(getattr(rec, "embed", None), "external", None)
            if ext:
                links.add(ext.uri)
            for facet in rec.facets or []:
                for feat in facet.features:
                    if getattr(feat, "uri", None):
                        links.add(feat.uri)
        cursor = r.cursor
        if not cursor:
            return links, texts


def main():
    feed = feedparser.parse(FEED, agent=UA)
    entries = list(reversed(feed.entries))  # oldest first
    print(f"{len(entries)} items in feed")

    seen = load_seen()
    if seen is None and not BACKFILL:
        # First run: remember what's already in the feed, post nothing.
        save_seen({e.get("id") or e.link for e in entries})
        print("First run: recorded existing items, nothing posted.")
        return
    seen = set(seen or [])

    client = None
    if not DRY_RUN:
        client = Client()
        client.login(os.environ["BLUESKY_USERNAME"], os.environ["BLUESKY_PASSWORD"])

    if BACKFILL:
        links, texts = already_posted(client) if client else (set(), [])
        new = []
        for e in entries[-BACKFILL:]:
            if e.link in links or any(e.link in t for t in texts):
                print(f"Already on Bluesky, skipping: {e.title}")
                seen.add(e.get("id") or e.link)
            else:
                new.append(e)
        print(f"Backfill: posting {len(new)} items, oldest first")
    else:
        new = [e for e in entries if (e.get("id") or e.link) not in seen][-MAX_POSTS_PER_RUN:]
    if not new:
        save_seen(seen)
        print("Nothing new.")
        return

    for e in new:
        uid = e.get("id") or e.link
        print(f"Posting: {e.title}")
        og_title, og_desc, og_img = page_preview(e.link)
        card = models.AppBskyEmbedExternal.External(
            uri=e.link,
            title=trim(og_title or e.title, 300),
            description=trim(og_desc or BeautifulSoup(e.get("summary", ""), "html.parser").get_text(), 500),
            thumb=thumbnail(client, og_img) if og_img else None,
        )
        text = trim(e.title, 300)
        if DRY_RUN:
            print(f"  [dry run] text={text!r}\n  card title={card.title!r}")
        else:
            client.send_post(text=text, embed=models.AppBskyEmbedExternal.Main(external=card))
        seen.add(uid)
        save_seen(seen)  # save after each post so a failure never causes a repost
        if BACKFILL:
            time.sleep(3)  # pace the backfill gently


if __name__ == "__main__":
    main()
