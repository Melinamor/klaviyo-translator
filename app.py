import os, json, re, time, uuid, threading
from flask import Flask, render_template, request, Response, session, redirect, url_for, jsonify
from dotenv import load_dotenv
import anthropic, requests
from bs4 import BeautifulSoup, NavigableString

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "please-change-this-secret")

PASSWORD        = os.environ.get("APP_PASSWORD", "changeme")
ANTHROPIC_KEY   = os.environ.get("ANTHROPIC_API_KEY")
SOURCE_API_KEY  = os.environ.get("SOURCE_KLAVIYO_API_KEY")
SOURCE_COUNTRY  = os.environ.get("SOURCE_COUNTRY", "DK")
SOURCE_LANGUAGE = os.environ.get("SOURCE_LANGUAGE", "Danish")
ACCOUNTS        = json.loads(os.environ.get("ACCOUNTS_JSON", "[]"))

KLAVIYO_REV = "2024-10-15"

# In-memory job store (single-user tool — no persistence needed)
_jobs: dict = {}

# Country → TLD/lang-path for auto-suggesting localized links
COUNTRY_DOMAINS = {
    "DK": {"tld": ".dk",     "lang": "/da/"},
    "SE": {"tld": ".se",     "lang": "/sv/"},
    "NO": {"tld": ".no",     "lang": "/nb/"},
    "FI": {"tld": ".fi",     "lang": "/fi/"},
    "DE": {"tld": ".de",     "lang": "/de/"},
    "NL": {"tld": ".nl",     "lang": "/nl/"},
    "FR": {"tld": ".fr",     "lang": "/fr/"},
    "UK": {"tld": ".co.uk",  "lang": "/en/"},
    "ES": {"tld": ".es",     "lang": "/es/"},
    "IT": {"tld": ".it",     "lang": "/it/"},
    "PL": {"tld": ".pl",     "lang": "/pl/"},
    "PT": {"tld": ".pt",     "lang": "/pt/"},
}

# ──────────────────────────────────────────────
# Klaviyo API helpers
# ──────────────────────────────────────────────

def kv_headers(api_key):
    return {
        "Authorization": f"Klaviyo-API-Key {api_key}",
        "revision": KLAVIYO_REV,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

def kv_find_template(api_key, name):
    url = "https://a.klaviyo.com/api/templates/"
    r = requests.get(url, headers=kv_headers(api_key),
                     params={"filter": f"equals(name,'{name}')"}, timeout=15)
    r.raise_for_status()
    data = r.json().get("data", [])
    if data:
        return data[0]
    # Fallback: scan all (up to 100)
    r2 = requests.get(url, headers=kv_headers(api_key),
                      params={"page[size]": 100}, timeout=15)
    r2.raise_for_status()
    for t in r2.json().get("data", []):
        if t.get("attributes", {}).get("name", "").lower() == name.lower():
            return t
    return None

def kv_get_template(api_key, tid):
    r = requests.get(f"https://a.klaviyo.com/api/templates/{tid}/",
                     headers=kv_headers(api_key), timeout=15)
    r.raise_for_status()
    return r.json().get("data", {})

def kv_create_template(api_key, name, html):
    r = requests.post(
        "https://a.klaviyo.com/api/templates/",
        headers=kv_headers(api_key),
        json={"data": {"type": "template", "attributes": {"name": name, "html": html}}},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()

def kv_upload_image(api_key, filename, file_bytes, content_type):
    """Upload image file to Klaviyo's CDN and return the public URL."""
    headers = {"Authorization": f"Klaviyo-API-Key {api_key}", "revision": KLAVIYO_REV}
    r = requests.post(
        "https://a.klaviyo.com/api/images/",
        headers=headers,
        files={"upload": (filename, file_bytes, content_type)},
        data={"name": filename.rsplit(".", 1)[0]},
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("data", {}).get("attributes", {}).get("image_url", "")

# ──────────────────────────────────────────────
# HTML parsing
# ──────────────────────────────────────────────

SKIP_TAGS   = {"script", "style", "head", "code", "pre"}
SKIP_RE     = re.compile(r"^\s*(\{%.*?%\}|\{\{.*?\}\}|https?://\S+)\s*$", re.DOTALL)
NUMBER_RE   = re.compile(
    r"\b(\d+(?:[.,]\d+)?)\s*(?:kr\.?|,-|-,|€|£|\$|DKK|SEK|NOK|EUR|GBP)\b"
    r"|\b(\d+)\s*%",
    re.IGNORECASE,
)

def _is_translatable(text):
    t = text.strip()
    return bool(t) and len(t) > 1 and not SKIP_RE.match(t) and not re.fullmatch(r"[\d\s\W]+", t)

def _text_nodes(soup):
    return [
        e for e in soup.find_all(string=True)
        if isinstance(e, NavigableString)
        and e.parent and e.parent.name not in SKIP_TAGS
        and _is_translatable(str(e))
    ]

def parse_elements(html):
    """Return (images, links, numbers) extracted from HTML."""
    soup = BeautifulSoup(html, "html.parser")

    images, seen_srcs = [], set()
    for img in soup.find_all("img"):
        src = img.get("src", "").strip()
        if src and src not in seen_srcs and not src.startswith("data:"):
            seen_srcs.add(src)
            images.append({"id": f"img_{len(images)}", "src": src,
                           "alt": img.get("alt", "").strip()})

    links, seen_hrefs = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if (href and href not in seen_hrefs
                and not href.startswith(("#", "mailto:", "{%", "tel:"))):
            seen_hrefs.add(href)
            txt = a.get_text(strip=True)
            links.append({"id": f"link_{len(links)}", "href": href,
                          "text": (txt[:45] + "…") if len(txt) > 45 else txt})

    numbers, seen_nums = [], set()
    for el in soup.find_all(string=True):
        if el.parent and el.parent.name in SKIP_TAGS:
            continue
        text = str(el).strip()
        for m in NUMBER_RE.finditer(text):
            full = m.group(0).strip()
            if full not in seen_nums:
                seen_nums.add(full)
                s, e = max(0, m.start() - 28), min(len(text), m.end() + 28)
                numbers.append({"id": f"num_{len(numbers)}", "original": full,
                                "context": f"…{text[s:e]}…"})

    return images, links, numbers

# ──────────────────────────────────────────────
# Translation
# ──────────────────────────────────────────────

def _batch_translate(texts, src, tgt, client):
    prompt = (
        f"Translate from {src} to {tgt} for a marketing email.\n"
        "Return ONLY a JSON array with translations in the same order.\n"
        "Keep Klaviyo variables ({{name}}, {% if %} etc.), URLs, and brand names unchanged.\n\n"
        + json.dumps(texts, ensure_ascii=False)
    )
    msg = client.messages.create(
        model="claude-opus-4-6", max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text.strip()
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if not m:
        raise ValueError(f"Unexpected response: {raw[:120]}")
    result = json.loads(m.group())
    if len(result) != len(texts):
        raise ValueError(f"Count mismatch: sent {len(texts)}, got {len(result)}")
    return result

def translate_html(html, src_lang, tgt_lang, client, chunk=40):
    soup = BeautifulSoup(html, "html.parser")
    nodes = _text_nodes(soup)
    for i in range(0, len(nodes), chunk):
        ch = nodes[i:i + chunk]
        translated = _batch_translate([str(n) for n in ch], src_lang, tgt_lang, client)
        for node, new_text in zip(ch, translated):
            node.replace_with(NavigableString(new_text))
    return str(soup)

# ──────────────────────────────────────────────
# Suggestions
# ──────────────────────────────────────────────

def generate_alt_suggestions(images, client):
    alts = [(img["id"], img["alt"]) for img in images if img["alt"]]
    if not alts:
        return {}
    ids, texts = zip(*alts)
    langs = ", ".join(f"{a['language']} ({a['country']})" for a in ACCOUNTS)
    prompt = (
        f"Translate these image alt texts from {SOURCE_LANGUAGE} to: {langs}.\n"
        "Return JSON: {\"original\": {\"CC\": \"translation\", ...}, ...}\n"
        "Only valid JSON, no markdown.\n\n"
        + json.dumps(list(texts), ensure_ascii=False)
    )
    msg = client.messages.create(
        model="claude-opus-4-6", max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text.strip()
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return {}
    try:
        by_text = json.loads(m.group())
        return {img_id: by_text.get(alt, {}) for img_id, alt in zip(ids, texts)}
    except Exception:
        return {}

def suggest_link(href, target_country):
    src = COUNTRY_DOMAINS.get(SOURCE_COUNTRY, {})
    tgt = COUNTRY_DOMAINS.get(target_country, {})
    result = href
    if src.get("tld") and src["tld"] in result:
        result = result.replace(src["tld"], tgt.get("tld", src["tld"]))
    if src.get("lang") and src["lang"] in result:
        result = result.replace(src["lang"], tgt.get("lang", src["lang"]))
    return result

# ──────────────────────────────────────────────
# Apply overrides
# ──────────────────────────────────────────────

def apply_overrides(html, src_replacements, alt_by_src, link_by_href, num_replacements):
    soup = BeautifulSoup(html, "html.parser")

    for img in soup.find_all("img"):
        src = img.get("src", "")
        if src in src_replacements:
            img["src"] = src_replacements[src]
        if src in alt_by_src:
            img["alt"] = alt_by_src[src]

    for a in soup.find_all("a", href=True):
        if a["href"] in link_by_href:
            a["href"] = link_by_href[a["href"]]

    result = str(soup)
    for original, replacement in num_replacements.items():
        if original and replacement and original != replacement:
            result = result.replace(original, replacement)
    return result

# ──────────────────────────────────────────────
# Auth decorator
# ──────────────────────────────────────────────

def auth_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

# ──────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    error = False
    if request.method == "POST":
        if request.form.get("password") == PASSWORD:
            session["authenticated"] = True
            return redirect(url_for("index"))
        error = True
    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/")
@auth_required
def index():
    return render_template("index.html")

@app.route("/review")
@auth_required
def review():
    template_name = request.args.get("template", "").strip()
    if not template_name:
        return redirect(url_for("index"))
    return render_template("review.html",
                           template_name=template_name,
                           accounts=ACCOUNTS,
                           source_country=SOURCE_COUNTRY)

# ── SSE: fetch + translate + parse ──

@app.route("/api/start", methods=["POST"])
@auth_required
def api_start():
    data = request.get_json() or {}
    template_name = data.get("template_name", "").strip()
    if not template_name:
        return jsonify({"error": "Template name required"}), 400

    def generate():
        def sse(t, msg, **extra):
            return f"data: {json.dumps({'type': t, 'message': msg, **extra})}\n\n"

        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

        yield sse("progress", f"Søger efter \"{template_name}\"…")
        try:
            tmpl = kv_find_template(SOURCE_API_KEY, template_name)
        except Exception as e:
            yield sse("error", f"Fejl: {e}"); return

        if not tmpl:
            yield sse("error", f"Template \"{template_name}\" ikke fundet."); return

        try:
            full = kv_get_template(SOURCE_API_KEY, tmpl["id"])
            source_html = full.get("attributes", {}).get("html", "")
        except Exception as e:
            yield sse("error", f"Kunne ikke hente HTML: {e}"); return

        if not source_html:
            yield sse("error", "Template er tom."); return

        yield sse("progress", f"Template fundet ({len(source_html):,} tegn). Oversætter…")

        translated = {}
        for i, acc in enumerate(ACCOUNTS, 1):
            yield sse("progress", f"[{i}/{len(ACCOUNTS)}] {acc['language']} ({acc['country']})…")
            try:
                translated[acc["country"]] = translate_html(
                    source_html, SOURCE_LANGUAGE, acc["language"], client)
            except Exception as e:
                yield sse("warning", f"Fejl {acc['country']}: {e}")
                translated[acc["country"]] = source_html

        yield sse("progress", "Analyserer billeder, links og tal…")
        images, links, numbers = parse_elements(source_html)

        alt_suggestions = {}
        if any(img["alt"] for img in images):
            yield sse("progress", "Genererer alt-tekst forslag…")
            try:
                alt_suggestions = generate_alt_suggestions(images, client)
            except Exception as e:
                yield sse("warning", f"Alt-tekst fejl: {e}")

        link_suggestions = {
            lnk["id"]: {acc["country"]: suggest_link(lnk["href"], acc["country"])
                        for acc in ACCOUNTS}
            for lnk in links
        }

        yield sse("done", "Klar til gennemgang!", data={
            "template_name": template_name,
            "translated_html": translated,
            "images": images,
            "alt_suggestions": alt_suggestions,
            "links": links,
            "link_suggestions": link_suggestions,
            "numbers": numbers,
        })

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

# ── Image upload to Klaviyo CDN ──

@app.route("/api/upload-image", methods=["POST"])
@auth_required
def api_upload_image():
    file    = request.files.get("file")
    img_id  = request.form.get("img_id", "")
    country = request.form.get("country", "").upper()

    if not file or not img_id or not country:
        return jsonify({"error": "Missing file, img_id or country"}), 400

    acc_map = {a["country"]: a["api_key"] for a in ACCOUNTS}
    api_key = acc_map.get(country)
    if not api_key:
        return jsonify({"error": f"Unknown country: {country}"}), 400

    try:
        cdn_url = kv_upload_image(
            api_key, file.filename, file.read(),
            file.content_type or "image/jpeg")
        return jsonify({"cdn_url": cdn_url, "img_id": img_id, "country": country})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Finalize: apply overrides + push to all accounts ──

@app.route("/api/finalize", methods=["POST"])
@auth_required
def api_finalize():
    data   = request.get_json() or {}
    job_id = str(uuid.uuid4())[:8]
    _jobs[job_id] = {"status": "pending", "messages": [], "data": data}
    threading.Thread(target=_run_finalize, args=(job_id,), daemon=True).start()
    return jsonify({"job_id": job_id})

@app.route("/api/finalize/<job_id>/stream")
@auth_required
def api_finalize_stream(job_id):
    if job_id not in _jobs:
        return "Not found", 404

    def generate():
        last = 0
        while True:
            job  = _jobs.get(job_id, {})
            msgs = job.get("messages", [])
            for msg in msgs[last:]:
                yield f"data: {json.dumps(msg)}\n\n"
                last += 1
            if job.get("status") in ("done", "error"):
                break
            time.sleep(0.25)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

def _run_finalize(job_id):
    job  = _jobs[job_id]
    data = job["data"]

    def emit(t, msg):
        job["messages"].append({"type": t, "message": msg})

    job["status"] = "running"

    template_name      = data.get("template_name", "Template")
    translated_html    = data.get("translated_html", {})
    images             = data.get("images", [])
    links              = data.get("links", [])
    numbers            = data.get("numbers", [])
    alt_overrides      = data.get("alt_overrides", {})       # {country: {img_id: alt}}
    link_overrides     = data.get("link_overrides", {})      # {country: {link_id: href}}
    number_overrides   = data.get("number_overrides", {})    # {country: {num_id: val}}
    image_replacements = data.get("image_replacements", {})  # {country: {img_id: cdn_url}}

    src_by_id  = {img["id"]:  img["src"]      for img in images}
    href_by_id = {lnk["id"]:  lnk["href"]     for lnk in links}
    orig_by_id = {num["id"]:  num["original"] for num in numbers}
    acc_keys   = {a["country"]: a["api_key"]  for a in ACCOUNTS}

    success = 0
    for i, acc in enumerate(ACCOUNTS, 1):
        country = acc["country"]
        lang    = acc["language"]
        api_key = acc_keys.get(country)
        if not api_key:
            emit("warning", f"Ingen API-nøgle for {country}"); continue

        emit("progress", f"[{i}/{len(ACCOUNTS)}] Forbereder {lang} ({country})…")
        html = translated_html.get(country, "")
        if not html:
            emit("warning", f"Ingen HTML for {country}"); continue

        # Build override maps keyed by original value (not ID)
        src_repl  = {src_by_id[k]: v for k, v in (image_replacements.get(country) or {}).items() if k in src_by_id}
        alt_repl  = {src_by_id[k]: v for k, v in (alt_overrides.get(country) or {}).items()      if k in src_by_id}
        link_repl = {href_by_id[k]: v for k, v in (link_overrides.get(country) or {}).items()    if k in href_by_id}
        num_repl  = {orig_by_id[k]: v for k, v in (number_overrides.get(country) or {}).items()  if k in orig_by_id}

        try:
            final_html = apply_overrides(html, src_repl, alt_repl, link_repl, num_repl)
        except Exception as e:
            emit("error", f"Override fejl {country}: {e}"); continue

        new_name = f"{template_name} — {country}"
        try:
            kv_create_template(api_key, new_name, final_html)
            emit("account_success", f"[{i}/{len(ACCOUNTS)}] ✓ {lang} ({country}) — \"{new_name}\"")
            success += 1
        except Exception as e:
            emit("account_error", f"[{i}/{len(ACCOUNTS)}] ✗ {lang} ({country}) — {e}")

        time.sleep(0.3)

    emit("done", f"Færdig! {success}/{len(ACCOUNTS)} templates oprettet.")
    job["status"] = "done"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port, threaded=True)
