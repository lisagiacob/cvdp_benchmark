#!/usr/bin/env python3
"""Elenca i modelli AWS Bedrock a cui le tue credenziali hanno effettivamente accesso.

Scarica il catalogo dei foundation model (solo modelli testuali) e prova ognuno
con una richiesta minima (max 1 token di output). I modelli piu' recenti non
supportano l'invocazione on-demand diretta: per quelli lo script prova anche
l'inference profile cross-region (es. us.anthropic.claude-...).

Credenziali (lette da env o da .env, caricato a runtime se python-dotenv c'e'):
  BEDROCK_AWS_ACCESS_KEY_ID / BEDROCK_AWS_SECRET_ACCESS_KEY /
  BEDROCK_AWS_REGION_NAME
  altrimenti la catena standard di boto3 (AWS_ACCESS_KEY_ID, AWS_PROFILE, ...).

Uso:
  python3 list_bedrock_models.py
  python3 list_bedrock_models.py --region eu-central-1 --providers anthropic
"""
import argparse
import json
import os
import sys

import boto3
from botocore.exceptions import BotoCoreError, ClientError

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def geo_prefix(region):
    if region.startswith("us-gov"):
        return "us-gov"
    if region.startswith("ap-"):
        return "apac"
    return region.split("-")[0]  # us, eu, ca, ...


def make_session(region):
    key = os.environ.get("BEDROCK_AWS_ACCESS_KEY_ID")
    secret = os.environ.get("BEDROCK_AWS_SECRET_ACCESS_KEY")
    if key and secret:
        return boto3.Session(aws_access_key_id=key, aws_secret_access_key=secret, region_name=region)
    return boto3.Session(region_name=region)


def list_inference_profiles(bedrock):
    """id dei system inference profile, o set vuoto se l'API non e' disponibile."""
    if not hasattr(bedrock, "list_inference_profiles"):
        return set()
    ids, token = set(), None
    try:
        while True:
            kw = {"maxResults": 100, "typeEquals": "SYSTEM_DEFINED"}
            if token:
                kw["nextToken"] = token
            resp = bedrock.list_inference_profiles(**kw)
            ids |= {p["inferenceProfileId"] for p in resp.get("inferenceProfileSummaries", [])}
            token = resp.get("nextToken")
            if not token:
                return ids
    except ClientError as e:
        print(f"  [inference profiles] {e.response['Error']['Code']}: {e.response['Error']['Message'][:120]}")
        return ids


def probe(runtime, model_id, provider):
    try:
        if provider == "anthropic":
            body = {"anthropic_version": "bedrock-2023-05-31", "max_tokens": 1,
                    "messages": [{"role": "user", "content": "hi"}]}
            runtime.invoke_model(modelId=model_id, body=json.dumps(body))
        elif hasattr(runtime, "converse"):
            runtime.converse(modelId=model_id,
                             messages=[{"role": "user", "content": [{"text": "hi"}]}],
                             inferenceConfig={"maxTokens": 1})
        else:
            return None, "saltato (boto3 troppo vecchio per Converse: pip install -U boto3)"
        return True, "OK"
    except ClientError as e:
        err = e.response["Error"]
        return False, f"{err['Code']}: {err['Message'].strip().splitlines()[0][:110]}"
    except BotoCoreError as e:
        return False, str(e)[:120]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--region", default=os.environ.get("BEDROCK_AWS_REGION_NAME")
                    or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1")
    ap.add_argument("--providers", nargs="*", default=None,
                    help="filtra per provider, es. anthropic meta amazon (default: tutti)")
    args = ap.parse_args()

    session = make_session(args.region)
    bedrock = session.client("bedrock")
    runtime = session.client("bedrock-runtime")

    try:
        ident = session.client("sts").get_caller_identity()
        print(f"Account: {ident['Account']}   Identity: {ident['Arn']}")
    except (ClientError, BotoCoreError) as e:
        sys.exit(f"Credenziali AWS non valide: {e}")
    print(f"Region: {args.region}\n")

    try:
        models = bedrock.list_foundation_models(byOutputModality="TEXT")["modelSummaries"]
    except ClientError as e:
        sys.exit(f"Impossibile leggere il catalogo: {e}")
    if args.providers:
        wanted = {p.lower() for p in args.providers}
        models = [m for m in models if m["providerName"].lower() in wanted
                  or m["modelId"].split(".")[0] in wanted]

    profiles = list_inference_profiles(bedrock)
    prefix = geo_prefix(args.region)

    available = []
    for m in sorted(models, key=lambda m: m["modelId"]):
        mid = m["modelId"]
        provider = mid.split(".")[0]
        candidates = []
        if "ON_DEMAND" in m.get("inferenceTypesSupported", ["ON_DEMAND"]):
            candidates.append(mid)
        profile_id = f"{prefix}.{mid}"
        if profile_id in profiles or (not profiles and "ON_DEMAND" not in m.get("inferenceTypesSupported", [])):
            candidates.append(profile_id)
        if not candidates:
            print(f"  - {mid:55s} solo provisioned throughput, non provato")
            continue
        for cid in candidates:
            ok, msg = probe(runtime, cid, provider)
            mark = "✔" if ok else ("-" if ok is None else "✘")
            print(f"  {mark} {cid:55s} {msg}")
            if ok:
                available.append(cid)

    print("\nModelli accessibili (usa questi come modelId):")
    for m in available or ["(nessuno)"]:
        print(f"  {m}")


if __name__ == "__main__":
    main()
