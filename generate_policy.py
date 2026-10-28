"""Gera a policy IAM do role.md com ARNs reais (conta STS + .env) e imprime JSON."""

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


def _ssm_parameter_arn(region: str, account: str, path: str) -> str:
    normalized = path if path.startswith("/") else f"/{path}"
    return f"arn:aws:ssm:{region}:{account}:parameter{normalized}"


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


def build_policy(
    region: str,
    account: str,
    prod_id: str,
    temp_id: str,
    dev_id: str,
    old_id: str,
    ssm_param: str,
) -> dict:
    statements: list[dict] = [
        {
            "Sid": "DescribeRds",
            "Effect": "Allow",
            "Action": [
                "rds:DescribeDBClusters",
                "rds:DescribeDBInstances",
            ],
            "Resource": "*",
        },
        {
            "Sid": "CloneProdToTemp",
            "Effect": "Allow",
            "Action": ["rds:RestoreDBClusterToPointInTime"],
            "Resource": [
                _rds_cluster_arn(region, account, prod_id),
                _rds_cluster_arn(region, account, temp_id),
            ],
        },
        {
            "Sid": "ManageTempDevOld",
            "Effect": "Allow",
            "Action": [
                "rds:CreateDBInstance",
                "rds:ModifyDBCluster",
                "rds:ModifyDBInstance",
                "rds:DeleteDBInstance",
                "rds:DeleteDBCluster",
                "rds:AddTagsToResource",
            ],
            "Resource": [
                _rds_cluster_arn(region, account, temp_id),
                _rds_instance_arn(region, account, temp_id),
                _rds_cluster_arn(region, account, dev_id),
                _rds_instance_arn(region, account, dev_id),
                _rds_cluster_arn(region, account, old_id),
                _rds_instance_arn(region, account, old_id),
            ],
        },
        {
            "Sid": "DenyDeleteOrRenameProd",
            "Effect": "Deny",
            "Action": [
                "rds:DeleteDBCluster",
                "rds:DeleteDBInstance",
                "rds:ModifyDBCluster",
                "rds:ModifyDBInstance",
            ],
            "Resource": [
                _rds_cluster_arn(region, account, prod_id),
                _rds_instance_arn(region, account, prod_id),
            ],
        },
    ]

    if ssm_param:
        statements.append(
            {
                "Sid": "ReadDbPasswordFromSsm",
                "Effect": "Allow",
                "Action": [
                    "ssm:GetParameter",
                    "ssm:GetParameters",
                ],
                "Resource": [
                    _ssm_parameter_arn(region, account, ssm_param),
                ],
            }
        )

    return {"Version": "2012-10-17", "Statement": statements}


def main() -> None:
    region = _env("AWS_DEFAULT_REGION", "AWS_REGION")
    prod_id = _env("PROD_IDENTIFIER")
    temp_id = _env("TEMP_IDENTIFIER")
    dev_id = _env("DEV_IDENTIFIER")
    old_id = _env("OLD_IDENTIFIER")
    ssm_param = _env("DB_PASSWORD_SSM_PARAM")

    _require_env(
        {
            "AWS_DEFAULT_REGION|AWS_REGION": region,
            "PROD_IDENTIFIER": prod_id,
            "DEV_IDENTIFIER": dev_id,
            "TEMP_IDENTIFIER": temp_id,
            "OLD_IDENTIFIER": old_id,
        }
    )

    account = _account_id()
    policy = build_policy(
        region=region,
        account=account,
        prod_id=prod_id,
        temp_id=temp_id,
        dev_id=dev_id,
        old_id=old_id,
        ssm_param=ssm_param,
    )
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

