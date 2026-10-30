#!/usr/bin/env python3
"""
Remove Aurora clusters created outside Terraform (TEMP, OLD) before lab teardown.

Invoked from terraform destroy via local-exec; credentials use the standard boto3 chain.
"""

from __future__ import annotations

import argparse
import sys
import time

import boto3
from botocore.exceptions import ClientError

POLL_INTERVAL_SECONDS = 15
DEFAULT_WAIT_TIMEOUT_SECONDS = 3600


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Delete extra Aurora lab clusters (TEMP/OLD) if they exist."
    )
    parser.add_argument("--region", required=True)
    parser.add_argument("--prod", required=True, help="PROD cluster identifier (protected)")
    parser.add_argument("--dev", required=True, help="DEV cluster identifier (protected)")
    parser.add_argument("--temp", default="", help="TEMP cluster identifier")
    parser.add_argument("--old", default="", help="OLD cluster identifier")
    parser.add_argument(
        "--wait-timeout-seconds",
        type=int,
        default=DEFAULT_WAIT_TIMEOUT_SECONDS,
    )
    return parser.parse_args()


def _cluster_exists(rds, identifier: str) -> bool:
    try:
        rds.describe_db_clusters(DBClusterIdentifier=identifier)
        return True
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "DBClusterNotFoundFault":
            return False
        raise


def _instance_exists(rds, identifier: str) -> bool:
    try:
        rds.describe_db_instances(DBInstanceIdentifier=identifier)
        return True
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "DBInstanceNotFoundFault":
            return False
        raise


def _wait_gone(
    rds,
    *,
    identifier: str,
    exists_fn,
    label: str,
    timeout_seconds: int,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline and exists_fn(rds, identifier):
        print(f"Aguardando remoção {label} {identifier}...", flush=True)
        time.sleep(POLL_INTERVAL_SECONDS)
    if exists_fn(rds, identifier):
        raise TimeoutError(f"Timeout removendo {label} {identifier}")


def _delete_cluster(rds, identifier: str, *, wait_timeout_seconds: int) -> None:
    if not identifier:
        return
    if not _cluster_exists(rds, identifier):
        print(f"Cluster {identifier} não existe; ignorando.", flush=True)
        return

    response = rds.describe_db_clusters(DBClusterIdentifier=identifier)
    cluster = (response.get("DBClusters") or [None])[0]
    status = (cluster or {}).get("Status") or ""

    if status == "deleting":
        print(f"Cluster {identifier} já em deleting; aguardando...", flush=True)
        _wait_gone(
            rds,
            identifier=identifier,
            exists_fn=_cluster_exists,
            label="do cluster",
            timeout_seconds=wait_timeout_seconds,
        )
        return

    if _instance_exists(rds, identifier):
        print(f"Deletando instância {identifier}...", flush=True)
        rds.delete_db_instance(
            DBInstanceIdentifier=identifier,
            SkipFinalSnapshot=True,
            DeleteAutomatedBackups=True,
        )
        _wait_gone(
            rds,
            identifier=identifier,
            exists_fn=_instance_exists,
            label="da instância",
            timeout_seconds=wait_timeout_seconds,
        )

    if _cluster_exists(rds, identifier):
        print(f"Deletando cluster {identifier}...", flush=True)
        rds.delete_db_cluster(
            DBClusterIdentifier=identifier,
            SkipFinalSnapshot=True,
        )
        _wait_gone(
            rds,
            identifier=identifier,
            exists_fn=_cluster_exists,
            label="do cluster",
            timeout_seconds=wait_timeout_seconds,
        )


def main() -> int:
    args = _parse_args()
    protected = {(args.prod or "").strip(), (args.dev or "").strip()} - {""}
    extras: list[str] = []
    seen: set[str] = set()
    for raw in (args.temp, args.old):
        ident = (raw or "").strip()
        if not ident or ident in seen:
            continue
        seen.add(ident)
        if ident in protected:
            print(
                f"Recusando apagar {ident}: coincide com PROD ou DEV do lab.",
                file=sys.stderr,
                flush=True,
            )
            return 1
        extras.append(ident)

    if not extras:
        return 0

    rds = boto3.client("rds", region_name=args.region)
    for ident in extras:
        _delete_cluster(
            rds,
            ident,
            wait_timeout_seconds=args.wait_timeout_seconds,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
