#!/usr/bin/env python3
"""Elenca i modelli Vertex AI a cui le tue credenziali hanno effettivamente accesso.

Per ogni publisher (default: anthropic, google) scarica il catalogo Model Garden e
poi prova ogni modello con una richiesta minima (max 1 token di output): il
catalogo dice cosa esiste, solo la prova dice cosa puoi usare davvero.

Config letta da env (o da .env, caricato a runtime se python-dotenv c'e'):
  ANTHROPIC_VERTEX_PROJECT_ID   (o GOOGLE_CLOUD_PROJECT)
  ANTHROPIC_VERTEX_LOCATION     (default: us-east5)
  GOOGLE_APPLICATION_CREDENTIALS (opzionale, usato da gcloud ADC)

Uso:
  python3 list_vertex_models.py
  python3 list_vertex_models.py --location global --publishers anthropic
  python3 list_vertex_models.py --extra claude-sonnet-4-5@20250929
"""
import argparse
import os
import subprocess
import sys

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Usati se il catalogo non e' leggibile (permessi mancanti / API non abilitata)
FALLBACK = {
    "anthropic": [
        "claude-opus-4-5@20251101",
        "claude-sonnet-4-5@20250929",
        "claude-haiku-4-5@20251001",
        "claude-opus-4-1@20250805",
        "claude-opus-4@20250514",
        "claude-sonnet-4@20250514",
        "claude-3-7-sonnet@20250219",
        "claude-3-5-haiku@20241022",
    ],
    "google": [
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gemini-2.0-flash-001",
    ],
}


def get_token():
    try:
        import google.auth
        import google.auth.transport.requests
        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        creds.refresh(google.auth.transport.requests.Request())
        return creds.token
    except ImportError:
        pass
    for cmd in (["gcloud", "auth", "application-default", "print-access-token"],
                ["gcloud", "auth", "print-access-token"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return out.stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
    sys.exit("Impossibile ottenere un access token (installa google-auth o configura gcloud).")


def base_url(location):
    host = "aiplatform.googleapis.com" if location == "global" else f"{location}-aiplatform.googleapis.com"
    return f"https://{host}"


def list_catalog(publisher, headers, location):
    """Ritorna una lista di id modello dal Model Garden, o None se non leggibile."""
    url = f"{base_url(location)}/v1beta1/publishers/{publisher}/models"
    ids, page = [], None
    while True:
        params = {"pageSize": 100, "listAllVersions": "true"}
        if page:
            params["pageToken"] = page
        r = requests.get(url, headers=headers, params=params, timeout=30)
        if r.status_code != 200:
            print(f"  [catalogo {publisher}] HTTP {r.status_code}: {r.text[:200]}")
            return None
        data = r.json()
        for m in data.get("publisherModels", []):
            mid = m["name"].split("/")[-1]
            ver = m.get("versionId")
            ids.append(f"{mid}@{ver}" if publisher == "anthropic" and ver and "@" not in mid else mid)
        page = data.get("nextPageToken")
        if not page:
            return sorted(set(ids))


def probe(publisher, model, headers, project, location):
    root = f"{base_url(location)}/v1/projects/{project}/locations/{location}/publishers/{publisher}/models/{model}"
    if publisher == "anthropic":
        url = f"{root}:rawPredict"
        body = {"anthropic_version": "vertex-2023-10-16", "max_tokens": 1,
                "messages": [{"role": "user", "content": "hi"}]}
    else:
        url = f"{root}:generateContent"
        body = {"contents": [{"role": "user", "parts": [{"text": "hi"}]}],
                "generationConfig": {"maxOutputTokens": 1}}
    try:
        r = requests.post(url, headers=headers, json=body, timeout=60)
    except requests.RequestException as e:
        return False, f"errore di rete: {e}"
    if r.status_code == 200:
        return True, "OK"
    try:
        msg = r.json().get("error", {}).get("message", r.text)
    except ValueError:
        msg = r.text
    return False, f"HTTP {r.status_code}: {msg.strip().splitlines()[0][:120]}"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", default=os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID")
                    or os.environ.get("GOOGLE_CLOUD_PROJECT"))
    ap.add_argument("--location", default=os.environ.get("ANTHROPIC_VERTEX_LOCATION", "us-east5"))
    ap.add_argument("--publishers", nargs="+", default=["anthropic", "google"])
    ap.add_argument("--extra", nargs="*", default=[], help="model id aggiuntivi (publisher anthropic)")
    ap.add_argument("--no-catalog", action="store_true", help="usa solo la lista di fallback")
    args = ap.parse_args()

    if not args.project:
        sys.exit("Project non trovato: imposta ANTHROPIC_VERTEX_PROJECT_ID o usa --project.")

    headers = {"Authorization": f"Bearer {get_token()}", "x-goog-user-project": args.project,
               "Content-Type": "application/json"}
    print(f"Project: {args.project}   Location: {args.location}\n")

    available = []
    for pub in args.publishers:
        models = None if args.no_catalog else list_catalog(pub, headers, args.location)
        if not models:
            models = FALLBACK.get(pub, [])
        if pub == "anthropic":
            models = sorted(set(models) | set(args.extra))
        print(f"== {pub} ({len(models)} modelli da provare) ==")
        for m in models:
            ok, msg = probe(pub, m, headers, args.project, args.location)
            print(f"  {'✔' if ok else '✘'} {m:45s} {msg}")
            if ok:
                available.append(f"{pub}/{m}")
        print()

    print("Modelli accessibili:")
    for m in available or ["(nessuno)"]:
        print(f"  {m}")


if __name__ == "__main__":
    main()
