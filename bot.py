"""Post new items from an RSS feed to Bluesky, with link-preview cards."""
import html
import io
import json
import os

import feedparser
import requests
from atproto import Client, models
from bs4 import BeautifulSoup
from PIL import Image

FEED = "https://theoldreader.com/profile/19f3e2b78dcc6a81ae1cc236.rss"
SEEN_FILE = "seen.json"
FAILED_FILE = "failed.json"  # retry counts for items that failed to post
MAX_ATTEMPTS = 3
MAX_POSTS_PER_RUN = 5
DRY_RUN = os.environ.get("DRY_RUN") == "1"
KEEP_SEEN = 2000  # how many posted-item IDs to remember
UA = ("Mozilla/5.0 (compatible; biocat-papers-bot/1.0; "
      "+https://bsky.app/profile/biocat-papers.bsky.social)")


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return json.load(f)
    return None


def save_seen(seen):
    # seen is a list in the order items were recorded; keep only the most recent IDs
    with open(SEEN_FILE, "w") as f:
        json.dump(list(dict.fromkeys(seen))[-KEEP_SEEN:], f, indent=0)


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


def keys(e):
    """An item counts as posted if either its feed ID or its link was recorded."""
    return {k for k in (e.get("id"), e.get("link")) if k}


def main():
    feed = feedparser.parse(FEED, agent=UA)
    if not feed.entries:
        print(f"Feed returned no items (status {feed.get('status')}); trying again next run.")
        return
    entries = list(reversed(feed.entries))  # oldest first
    print(f"{len(entries)} items in feed")

    seen = load_seen()
    if seen is None:
        # First run: remember what's already in the feed, post nothing.
        save_seen([k for e in entries for k in keys(e)])
        print("First run: recorded existing items, nothing posted.")
        return
    seen_set = set(seen)

    # Oldest unposted items first; anything over the limit waits for the next run.
    new = [e for e in entries if not keys(e) & seen_set][:MAX_POSTS_PER_RUN]
    if not new:
        print("Nothing new.")
        return

    client = None
    if not DRY_RUN:
        client = Client()
        client.login(os.environ["BLUESKY_USERNAME"], os.environ["BLUESKY_PASSWORD"])

    attempts = json.load(open(FAILED_FILE)) if os.path.exists(FAILED_FILE) else {}
    failures = 0
    for e in new:
        print(f"Posting: {e.title}")
        try:
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
        except Exception as err:  # skip this one, retry it next run, keep going
            n = attempts[e.link] = attempts.get(e.link, 0) + 1
            print(f"  FAILED (attempt {n} of {MAX_ATTEMPTS}): {err}")
            if n < MAX_ATTEMPTS:
                failures += 1
            else:
                print("  Giving up on this item.")
                del attempts[e.link]
                seen.extend(keys(e))
                save_seen(seen)
            with open(FAILED_FILE, "w") as f:
                json.dump(attempts, f, indent=0)
            continue
        attempts.pop(e.link, None)
        seen.extend(keys(e))
        save_seen(seen)  # save after each post so a failure never causes a repost

    if attempts or os.path.exists(FAILED_FILE):
        with open(FAILED_FILE, "w") as f:
            json.dump(attempts, f, indent=0)
    if failures:
        raise SystemExit(f"{failures} item(s) failed to post; they will be retried next run.")


if __name__ == "__main__":
    main()
