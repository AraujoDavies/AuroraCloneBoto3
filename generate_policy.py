"""Gera a policy AvoidDeleteAuroraCloneBoto3: Deny de delete/rename só na PROD."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import boto3
from dotenv import load_dotenv

load_dotenv(override=True)

YELLOW = "\033[33m"
RED = "\033[31m"
RESET = "\033[0m"
OUTPUT_PATH = Path(__file__).resolve().parent / "policy.json"


def _env(*keys: str) -> str:
    for key in keys:
        value = (os.getenv(key) or "").strip()
        if value:
            return value
    return ""


def _require_env(names: dict[str, str]) -> None:
    missing = [name for name, value in names.items() if not value]
    if missing:
        print(
            f"Variáveis obrigatórias ausentes: {', '.join(missing)}",
            file=sys.stderr,
        )
        sys.exit(1)


def _rds_cluster_arn(region: str, account: str, identifier: str) -> str:
    return f"arn:aws:rds:{region}:{account}:cluster:{identifier}"


def _rds_instance_arn(region: str, account: str, identifier: str) -> str:
    return f"arn:aws:rds:{region}:{account}:db:{identifier}"


def _account_id() -> str:
    try:
        identity = boto3.client("sts").get_caller_identity()
    except Exception as error:
        print(f"Falha ao obter conta via STS: {error}", file=sys.stderr)
        sys.exit(1)
    account = (identity.get("Account") or "").strip()
    if not account:
        print("STS retornou Account vazio.", file=sys.stderr)
        sys.exit(1)
    return account


def build_policy(region: str, account: str, prod_id: str) -> dict:
    """Deny de delete e modify no cluster e na instância de PROD."""
    prod_arns = [
        _rds_cluster_arn(region, account, prod_id),
        _rds_instance_arn(region, account, prod_id),
    ]
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "DenyDeleteOrRenameProd",
                "Effect": "Deny",
                "Action": [
                    "rds:DeleteDBCluster",
                    "rds:DeleteDBInstance",
                    "rds:ModifyDBCluster",
                    "rds:ModifyDBInstance",
                ],
                "Resource": prod_arns,
            }
        ],
    }


def main() -> None:
    region = _env("AWS_DEFAULT_REGION", "AWS_REGION")
    prod_id = _env("PROD_IDENTIFIER")

    _require_env(
        {
            "AWS_DEFAULT_REGION|AWS_REGION": region,
            "PROD_IDENTIFIER": prod_id,
        }
    )

    account = _account_id()
    policy = build_policy(region=region, account=account, prod_id=prod_id)
    payload = json.dumps(policy, indent=2) + "\n"
    OUTPUT_PATH.write_text(payload, encoding="utf-8")
    saved_msg = f"Arquivo Salvo: {OUTPUT_PATH}"
    if sys.stdout.isatty():
        print(f"{YELLOW}{payload.rstrip()}{RESET}")
        print(f"\n{RED}{saved_msg}{RESET}")
    else:
        print(payload, end="")
        print(f"\n{saved_msg}", file=sys.stderr)


if __name__ == "__main__":
    main()
